#!/usr/bin/env python3
"""
Pull Confluence Cloud page context for local workspace triage.

Usage:
  confluence profiles
  confluence spaces [--profile example]
  confluence list --space DOCS [--profile example] [--limit 25]
  confluence tree --space DOCS [--profile example] [--depth 3]
  confluence tree --space DOCS --root EXAMPLE_PAGE_ID [--profile example]
  confluence search [--profile example] [--space DOCS] [--query "source quality"]
  confluence page PAGE_ID [--profile example] [--full] [--json]

Inputs:
  Reads process env or an explicit/shared/isolated dotenv. Rundesk-managed accounts use
  CONFLUENCE_<FIELD>__<PROFILE>, with the plain CONFLUENCE_<FIELD> as the default account;
  the older CONFLUENCE_<PROFILE>_<FIELD> keys still resolve. Either spelling of the shared
  Atlassian credential (JIRA_<FIELD>__<PROFILE>, JIRA_<PROFILE>_<FIELD>, or the plain
  JIRA_<FIELD>) is reused when no Confluence-specific value is set, and
  CONFLUENCE_SPACES maps the spaces. See references/cli.md. Secrets must stay in the
  process environment or a local dotenv only.

Outputs:
  Writes compact text summaries to stdout. No raw JSON unless --json is provided.
  The integration is read-only and does not mutate Confluence.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PAGE_LIST_COLUMNS = ["id", "title", "space", "status", "version", "parent", "profile"]


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("CONFLUENCE_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "confluence" / "env")
    candidates.append(xdg / "confluence" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


DEFAULT_ENV = resolve_env_file()


class ConfluenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    email: str
    token: str
    spaces: list[str]
    label: str


class HtmlTextExtractor(HTMLParser):
    block_tags = {
        "address",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "table",
        "tr",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape(" ".join(self.parts))
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r"\s+([,.;:!?])", r"\1", value)
        value = re.sub(r"\n\s+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        print(
            f"WARNING: dotenv file {path} is accessible by group or others (mode {mode:04o}); "
            "restrict it with chmod 600.",
            file=sys.stderr,
        )

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


# Each plain variable name Rundesk manages, paired with the per-profile suffix this
# repository has always used, so both spellings resolve to the same field. The keys are
# exactly the names declared in rundesk.json plus the optional ones a command only uses
# when present.
PROFILE_FIELDS = {
    "CONFLUENCE_BASE_URL": "BASE_URL",
    "CONFLUENCE_EMAIL": "EMAIL",
    "CONFLUENCE_API_TOKEN": "API_TOKEN",
    "CONFLUENCE_SPACES": "SPACES",
    "CONFLUENCE_LABEL": "LABEL",
}
REQUIRED_FIELDS = ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN")
# Fields shared with the jira package: the same Atlassian site credential serves both, so an
# account configured for jira works here without a second copy of the token. Spaces are
# Confluence-only and are never read from a jira key.
SHARED_ATLASSIAN_FIELDS = {
    "CONFLUENCE_BASE_URL": "JIRA_BASE_URL",
    "CONFLUENCE_EMAIL": "JIRA_EMAIL",
    "CONFLUENCE_API_TOKEN": "JIRA_API_TOKEN",
    "CONFLUENCE_LABEL": "JIRA_LABEL",
}
# A Rundesk account suffix: uppercase words joined by single underscores, because a
# double underscore is what separates the field name from the account name.
ACCOUNT_SUFFIX_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")
RESERVED_PROFILE_WORDS = frozenset({"DEFAULT", "ENV"})


def normalize_profile(profile: str) -> str:
    """A profile name as an environment-variable fragment: `example-two` to `EXAMPLE_TWO`."""
    return re.sub(r"[^A-Za-z0-9]+", "_", profile or "").strip("_").upper()


def profile_label(suffix: str) -> str:
    """The inverse of `normalize_profile`, so a discovered account reads as a profile name."""
    return suffix.lower().replace("_", "-")


def env_name(prefix: str, profile: str, suffix: str) -> str:
    return f"{prefix}_{normalize_profile(profile)}_{suffix}"


def is_default_profile(profile: str) -> bool:
    """Rundesk stores the default account under the plain, unsuffixed variable names."""
    normalized = normalize_profile(profile)
    if not normalized or normalized == "DEFAULT":
        return True
    return normalized in {
        normalize_profile(os.environ.get("CONFLUENCE_DEFAULT_PROFILE", "")),
        normalize_profile(os.environ.get("JIRA_DEFAULT_PROFILE", "")),
    }


def missing_name(profile: str, field: str) -> str:
    """The variable an owner must set, spelled the way Rundesk stores it."""
    return field if is_default_profile(profile) else f"{field}__{normalize_profile(profile)}"


def profile_value(profile: str, field: str) -> str:
    """Read one field for one profile.

    Every Confluence spelling is exhausted before the shared `JIRA_*` twin is consulted, so
    a Confluence-specific value always overrides the credential the two packages share —
    without that, a site whose Confluence lives on a different host than its Jira would
    silently read the wrong host. Within each family Rundesk's `<FIELD>__<PROFILE>` wins,
    then this repository's `<PREFIX>_<PROFILE>_<FIELD>`, then the plain `<FIELD>` — which
    belongs to the default account only, so a named account never pairs one site's URL with
    another site's token.
    """
    shared = SHARED_ATLASSIAN_FIELDS.get(field)
    families = [("CONFLUENCE", field)]
    if shared:
        families.append(("JIRA", shared))

    candidates: list[str] = []
    normalized = normalize_profile(profile)
    for prefix, plain in families:
        if normalized:
            candidates.append(f"{plain}__{normalized}")
            candidates.append(env_name(prefix, profile, PROFILE_FIELDS[field]))
        if is_default_profile(profile):
            candidates.append(plain)

    for name in candidates:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("CONFLUENCE_PROFILES")) or split_csv(os.environ.get("JIRA_PROFILES"))
    default = os.environ.get("CONFLUENCE_DEFAULT_PROFILE") or os.environ.get("JIRA_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    """Accounts present in the environment, so adding one needs no declaration.

    Both spellings are scanned: Rundesk's `<FIELD>__<ACCOUNT>` suffix and this
    repository's `CONFLUENCE_<PROFILE>_<FIELD>` infix, each for the Confluence field names
    and the shared jira twins.

    The plain names are one more account — the default one — listed even when only
    partly configured, so it carries its own error instead of vanishing. It is
    suppressed when the infix spelling is in use: there a plain value was a fallback
    shared by every profile, not an account of its own, and inventing one would make
    every command ambiguous for an owner whose dotenv predates Rundesk.
    """
    suffixed: set[str] = set()
    infixed: set[str] = set()
    declared = set(PROFILE_FIELDS) | set(SHARED_ATLASSIAN_FIELDS.values())
    legacy = re.compile(
        rf"^(?:CONFLUENCE|JIRA)_({ACCOUNT_SUFFIX_RE.pattern})_({'|'.join(PROFILE_FIELDS.values())})$"
    )
    for key in os.environ:
        for field in declared:
            prefix = f"{field}__"
            if key.startswith(prefix) and ACCOUNT_SUFFIX_RE.fullmatch(key[len(prefix):]):
                suffixed.add(profile_label(key[len(prefix):]))
        match = legacy.match(key)
        if not match:
            continue
        word = match.group(1)
        if word == "DEFAULT":
            # `<SKILL>_DEFAULT_<FIELD>` is the infix spelling of the default account, not
            # an account named `default` that resolution would then never find.
            infixed.add("default")
        elif word not in RESERVED_PROFILE_WORDS:
            infixed.add(profile_label(word))
    # A partly configured default account is listed with its error instead of vanishing.
    names = suffixed | infixed
    # The plain names are the default account. The infix spelling predates that idea and
    # treated a plain value as a fallback shared by every profile, so an environment
    # written that way gets no invented `default` account to make selection ambiguous.
    if not infixed and any(profile_value("", field) for field in REQUIRED_FIELDS):
        names.add(os.environ.get("CONFLUENCE_DEFAULT_PROFILE") or os.environ.get("JIRA_DEFAULT_PROFILE") or "default")
    return sorted(names)


def validate_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ConfluenceError(f"Invalid Confluence base URL: {value!r}. Configure an HTTPS origin only.") from exc

    if (
        not value
        or any(character.isspace() or ord(character) < 32 for character in value)
        or parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ConfluenceError(
            f"Invalid Confluence base URL: {value!r}. Configure an HTTPS origin only, without credentials or a path."
        )

    return value.rstrip("/")


def url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(value)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80 if parsed.scheme.lower() == "http" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and url_origin(req.full_url) != url_origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


def open_url(req: urllib.request.Request, timeout: int):
    return urllib.request.build_opener(SameOriginRedirectHandler()).open(req, timeout=timeout)


def get_profile(name: str) -> Profile:
    base_url = profile_value(name, "CONFLUENCE_BASE_URL")
    email = profile_value(name, "CONFLUENCE_EMAIL")
    token = profile_value(name, "CONFLUENCE_API_TOKEN")
    spaces = split_csv(profile_value(name, "CONFLUENCE_SPACES"))
    label = profile_value(name, "CONFLUENCE_LABEL") or name

    missing = [
        missing_name(name, field)
        for field, value in (
            ("CONFLUENCE_BASE_URL", base_url),
            ("CONFLUENCE_EMAIL", email),
            ("CONFLUENCE_API_TOKEN", token),
        )
        if not value
    ]

    if missing:
        raise ConfluenceError(
            "Missing Confluence config: "
            + ", ".join(missing)
            + ". Run `rundesk skills configure`, add it to the secrets dotenv, or export it in the shell."
        )

    base_url = validate_base_url(base_url)

    return Profile(
        name=name,
        base_url=base_url,
        email=email,
        token=token,
        spaces=spaces,
        label=label,
    )


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback

    value = str(value).replace("\n", " ").strip()
    return value if value else fallback


def truncate(value: Any, limit: int = 180) -> str:
    value = text(value)
    if len(value) <= limit:
        return value

    return value[: limit - 1].rstrip() + "..."


def email_label(email: Any, show: bool = False) -> str:
    email = text(email)
    if email == "-":
        return "-"
    return email if show else "configured"


def auth_header(profile: Profile) -> str:
    encoded = base64.b64encode(f"{profile.email}:{profile.token}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def link_header_next(value: str | None) -> str:
    if not value:
        return ""
    for part in value.split(","):
        section = part.strip()
        if 'rel="next"' not in section and "rel=next" not in section:
            continue
        match = re.search(r"<([^>]+)>", section)
        if match:
            return match.group(1)
    return ""


def request(
    profile: Profile,
    path: str,
    params: dict[str, Any] | None = None,
    retries: int = 2,
) -> Any:
    url = validate_base_url(profile.base_url) + "/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    headers = {
        "Authorization": auth_header(profile),
        "Accept": "application/json",
        "User-Agent": "workspace-confluence/1.0",
    }

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with open_url(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else None
                next_link = link_header_next(response.headers.get("Link"))
                if next_link and isinstance(data, dict):
                    links = data.setdefault("_links", {})
                    if isinstance(links, dict) and not links.get("next"):
                        links["next"] = next_link
                return data
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(delay, 30))
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"message": raw[:500]}

            message = data.get("message") or data.get("errorMessages") or data
            raise ConfluenceError(f"Confluence API {exc.code} profile={profile.name}: {message}") from exc
        except urllib.error.URLError as exc:
            raise ConfluenceError(f"Confluence API request failed profile={profile.name}: {exc.reason}") from exc

    raise ConfluenceError(f"Confluence API request exhausted retries profile={profile.name}")


def html_to_text(value: str) -> str:
    parser = HtmlTextExtractor()
    parser.feed(value or "")
    parser.close()
    return parser.text()


def web_url(profile: Profile, item: dict[str, Any]) -> str:
    links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
    webui = links.get("webui")
    if webui:
        return profile.base_url + "/wiki" + str(webui) if str(webui).startswith("/") else str(webui)
    return profile.base_url


def item_space_key(item: dict[str, Any]) -> str:
    space = item.get("space") if isinstance(item.get("space"), dict) else {}
    return text(space.get("key") or item.get("spaceKey") or item.get("spaceId"))


def item_version(item: dict[str, Any]) -> str:
    version = item.get("version") if isinstance(item.get("version"), dict) else {}
    return text(version.get("number"))


def page_line(page: dict[str, Any], profile: Profile) -> str:
    page_id = text(page.get("id"))
    parts = [
        f"id={page_id}",
        f"profile={profile.name}",
        f"site={profile.label}",
        f"space={item_space_key(page)}",
        f"type={text(page.get('type'))}",
        f"version={item_version(page)}",
    ]
    return "\n".join(
        [
            "- " + " | ".join(parts),
            f"  title: {truncate(page.get('title'), 220)}",
            f"  fetch: confluence page {page_id} --profile {profile.name}",
            f"  link: {web_url(profile, page)}",
        ]
    )


def page_list_row(page: dict[str, Any], profile: Profile, space_key: str = "") -> list[str]:
    return [
        text(page.get("id")),
        truncate(page.get("title"), 160),
        space_key or item_space_key(page),
        text(page.get("status")),
        item_version(page),
        text(page.get("parentId")),
        profile.name,
    ]


def print_page_list(pages: list[dict[str, Any]], profile: Profile, space_key: str = "") -> None:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(PAGE_LIST_COLUMNS)
    for page in pages:
        writer.writerow(page_list_row(page, profile, space_key))


def normalized_page(page: dict[str, Any], profile: Profile, body_text: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ancestors = page.get("ancestors") if isinstance(page.get("ancestors"), list) else []
    payload = {
        "id": text(page.get("id"), ""),
        "title": text(page.get("title"), ""),
        "profile": profile.name,
        "site": profile.label,
        "space": item_space_key(page),
        "type": text(page.get("type"), ""),
        "status": text(page.get("status"), ""),
        "version": item_version(page),
        "url": web_url(profile, page),
        "ancestors": [{"id": text(item.get("id"), ""), "title": text(item.get("title"), "")} for item in ancestors],
        "body_text": body_text,
    }
    if extra:
        payload.update(
            {
                "children": extra.get("children") or [],
                "attachments": extra.get("attachments") or [],
                "comments": extra.get("comments") or [],
            }
        )
    return payload


def cql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_search_cql(args: argparse.Namespace, profile: Profile) -> str:
    if args.cql:
        return args.cql

    spaces = args.space or profile.spaces
    if not spaces and not args.all_spaces:
        raise ConfluenceError(
            "No Confluence spaces configured for this profile. Add CONFLUENCE_SPACES__<PROFILE> "
            "(or CONFLUENCE_<PROFILE>_SPACES), pass --space, or use --all-spaces."
        )

    clauses = ["type = page"]
    if spaces:
        if len(spaces) == 1:
            clauses.append(f"space = {cql_string(spaces[0])}")
        else:
            clauses.append("space in (" + ", ".join(cql_string(space) for space in spaces) + ")")

    if args.query:
        clauses.append(f"text ~ {cql_string(args.query)}")

    return " and ".join(clauses) + " order by lastmodified desc"


def fetch_paginated(
    profile: Profile,
    path: str,
    results_key: str = "results",
    params: dict[str, Any] | None = None,
    start_param: str | None = "start",
) -> list[Any]:
    params = dict(params or {})
    params.setdefault("limit", 50)
    if start_param:
        params.setdefault(start_param, 0)
    items: list[Any] = []

    while True:
        data = request(profile, path, params=params)
        if not isinstance(data, dict):
            return items

        values = data.get(results_key) or []
        if isinstance(values, list):
            items.extend(values)

        links = data.get("_links") if isinstance(data.get("_links"), dict) else {}
        if not links.get("next") or not values:
            break

        next_query = urllib.parse.urlparse(str(links["next"])).query
        next_params = urllib.parse.parse_qs(next_query)
        if next_params:
            for key, value in next_params.items():
                if value:
                    params[key] = value[-1]
        elif start_param:
            params[start_param] = int(params.get(start_param) or 0) + int(params.get("limit") or 50)
        else:
            break

    return items


def get_space(profile: Profile, space_key: str) -> dict[str, Any]:
    data = request(profile, "wiki/api/v2/spaces", params={"keys": [space_key], "limit": 1})
    spaces = data.get("results", []) if isinstance(data, dict) else []
    if not spaces:
        raise ConfluenceError(f"Confluence space not found or not visible: {space_key}")
    return spaces[0]


def fetch_space_pages(profile: Profile, space_key: str, limit: int) -> list[dict[str, Any]]:
    space = get_space(profile, space_key)
    space_id = text(space.get("id"))
    return fetch_paginated(
        profile,
        f"wiki/api/v2/spaces/{urllib.parse.quote(space_id)}/pages",
        params={"limit": min(limit, 250)},
        start_param=None,
    )[:limit]


def selected_profile_name(args: argparse.Namespace) -> str:
    profile_name = (
        getattr(args, "profile", None)
        or os.environ.get("CONFLUENCE_DEFAULT_PROFILE")
        or os.environ.get("JIRA_DEFAULT_PROFILE", "")
    )
    if profile_name:
        return profile_name

    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if names:
        raise ConfluenceError(
            "Multiple Confluence profiles configured; pass --profile or set CONFLUENCE_DEFAULT_PROFILE. "
            f"Available: {', '.join(names)}"
        )
    raise ConfluenceError("No Confluence profile selected. Pass --profile or set CONFLUENCE_DEFAULT_PROFILE.")


def command_profiles(args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print(
            "No Confluence profiles configured. Run `rundesk skills configure`, or set "
            "CONFLUENCE_PROFILES and CONFLUENCE_DEFAULT_PROFILE in .env."
        )
        return 0

    print("Confluence profiles")
    for name in names:
        try:
            profile = get_profile(name)
            print(
                "- "
                + " | ".join(
                    [
                        f"profile={profile.name}",
                        f"label={profile.label}",
                        f"base_url={profile.base_url}",
                        f"email={email_label(profile.email, args.show_email)}",
                        f"spaces={','.join(profile.spaces) or '-'}",
                    ]
                )
            )
        except ConfluenceError as exc:
            print(f"- profile={name} | error={exc}")
    return 0


def command_spaces(args: argparse.Namespace, profile: Profile) -> int:
    data = request(profile, "wiki/api/v2/spaces", params={"limit": args.limit})
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    spaces = data.get("results", []) if isinstance(data, dict) else []
    print(f"Confluence spaces | profile={profile.name} site={profile.label} returned={len(spaces)}")
    print(f"configured_spaces={','.join(profile.spaces) or '-'}")
    for space in spaces:
        print(
            "- "
            + " | ".join(
                [
                    f"key={text(space.get('key'))}",
                    f"id={text(space.get('id'))}",
                    f"name={text(space.get('name'))}",
                    f"type={text(space.get('type'))}",
                ]
            )
        )
    return 0


def command_list(args: argparse.Namespace, profile: Profile) -> int:
    pages = fetch_space_pages(profile, args.space, args.limit)

    if args.json:
        print(json.dumps({"space": args.space, "pages": pages}, indent=2, sort_keys=True))
        return 0

    print_page_list(pages, profile, args.space)
    return 0


def command_search(args: argparse.Namespace, profile: Profile) -> int:
    cql = build_search_cql(args, profile)
    params = {"cql": cql, "limit": args.limit, "expand": "space,version"}
    data = request(profile, "wiki/rest/api/content/search", params=params)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    pages = data.get("results", []) if isinstance(data, dict) else []
    print(f"Confluence pages | profile={profile.name} site={profile.label} cql={cql!r} limit={args.limit}")
    print(f"count={len(pages)}")
    for page in pages:
        print(page_line(page, profile))
    return 0


def child_sort_key(page: dict[str, Any]) -> tuple[int, str]:
    raw_position = page.get("childPosition")
    if raw_position is None:
        raw_position = page.get("position")
    try:
        position = int(raw_position)
    except (TypeError, ValueError):
        position = 999999
    return (position, text(page.get("title")).lower())


def print_tree_page(page: dict[str, Any], level: int, space_key: str) -> None:
    indent = "  " * max(level, 0)
    page_id = text(page.get("id"))
    title = truncate(page.get("title"), 160)
    print(f"{indent}- id={page_id} | space={space_key} | type={text(page.get('type'))} | title={title}")


def command_tree(args: argparse.Namespace, profile: Profile) -> int:
    if args.root:
        space = get_space(profile, args.space)
        root = request(profile, f"wiki/api/v2/pages/{urllib.parse.quote(args.root)}")
        root_space_id = text(root.get("spaceId"), "")
        if root_space_id and root_space_id != text(space.get("id"), ""):
            raise ConfluenceError(f"Root page {args.root} is not in Confluence space {args.space}.")
        descendants = fetch_paginated(
            profile,
            f"wiki/api/v2/pages/{urllib.parse.quote(args.root)}/descendants",
            params={"limit": min(args.max_pages, 250), "depth": args.depth},
            start_param=None,
        )[: args.max_pages]
        if args.json:
            print(json.dumps({"space": args.space, "root": root, "descendants": descendants}, indent=2, sort_keys=True))
            return 0

        print(f"Confluence tree | profile={profile.name} site={profile.label} space={args.space} root={args.root}")
        print_tree_page(root, 0, args.space)
        for page in descendants:
            depth = int(page.get("depth") or 1)
            print_tree_page(page, depth, args.space)
        return 0

    pages = fetch_space_pages(profile, args.space, args.max_pages)
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        parent = text(page.get("parentId"), "")
        children_by_parent.setdefault(parent, []).append(page)

    for children in children_by_parent.values():
        children.sort(key=child_sort_key)

    roots = children_by_parent.get("", [])
    if not roots:
        page_ids = {text(page.get("id"), "") for page in pages}
        roots = [page for page in pages if text(page.get("parentId"), "") not in page_ids]
        roots.sort(key=child_sort_key)

    if args.json:
        print(json.dumps({"space": args.space, "pages": pages, "root_count": len(roots)}, indent=2, sort_keys=True))
        return 0

    print(f"Confluence tree | profile={profile.name} site={profile.label} space={args.space} pages={len(pages)}")

    def walk(page: dict[str, Any], level: int) -> None:
        if level > args.depth:
            return
        print_tree_page(page, level, args.space)
        for child in children_by_parent.get(text(page.get("id"), ""), []):
            walk(child, level + 1)

    for root in roots:
        walk(root, 0)
    return 0


def command_page(args: argparse.Namespace, profile: Profile) -> int:
    expand = "body.storage,body.view,version,space,ancestors"
    page = request(profile, f"wiki/rest/api/content/{urllib.parse.quote(args.page_id)}", params={"expand": expand})

    extra: dict[str, Any] = {}
    if args.full:
        page_path = f"wiki/rest/api/content/{urllib.parse.quote(args.page_id)}"
        extra["children"] = fetch_paginated(profile, page_path + "/child/page", params={"limit": 50, "expand": "space,version"})
        extra["attachments"] = fetch_paginated(profile, page_path + "/child/attachment", params={"limit": 50, "expand": "version"})
        extra["comments"] = fetch_paginated(profile, page_path + "/child/comment", params={"limit": 50, "expand": "version"})

    body = page.get("body") if isinstance(page.get("body"), dict) else {}
    storage = body.get("storage") if isinstance(body.get("storage"), dict) else {}
    page_text = html_to_text(text(storage.get("value"), ""))

    if args.json:
        print(json.dumps({"page": page, "normalized": normalized_page(page, profile, page_text, extra), **extra}, indent=2, sort_keys=True))
        return 0

    print(f"Confluence page | profile={profile.name} site={profile.label}")
    print(page_line(page, profile))
    ancestors = page.get("ancestors") if isinstance(page.get("ancestors"), list) else []
    if ancestors:
        print("ancestors: " + " > ".join(text(ancestor.get("title")) for ancestor in ancestors))

    if page_text and args.body_limit:
        print("body:")
        print(textwrap.indent(truncate(page_text, args.body_limit), "  "))

    if args.full:
        print(f"children={len(extra.get('children') or [])}")
        print(f"attachments={len(extra.get('attachments') or [])}")
        print(f"comments={len(extra.get('comments') or [])}")
    else:
        print(f"full: confluence page {args.page_id} --profile {profile.name} --full")
    return 0


def add_common_options(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--env-file",
        default=default,
        help="Path to dotenv file. Defaults to the configured shared or isolated Confluence env.",
    )
    parser.add_argument(
        "--profile",
        default=default,
        help=(
            "Confluence account name, from CONFLUENCE_<FIELD>__<PROFILE> or "
            "CONFLUENCE_<PROFILE>_<FIELD> env vars; the shared JIRA_* twins resolve too."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull compact Confluence page context for workspace triage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              confluence profiles
              confluence spaces --profile example
              confluence list --profile example --space DOCS --limit 10
              confluence tree --profile example --space DOCS --depth 3
              confluence search --profile example --space DOCS --query "source quality"
              confluence page EXAMPLE_PAGE_ID --profile example --full
            """
        ),
    )
    add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List configured Confluence profiles.")
    profiles.add_argument("--show-email", action="store_true", help="Show configured account emails in text output.")
    profiles.set_defaults(handler=command_profiles, no_profile=True)

    spaces = subparsers.add_parser("spaces", help="List visible Confluence spaces.")
    add_common_options(spaces, suppress_defaults=True)
    spaces.add_argument("--limit", type=int, default=50, help="Maximum spaces to print.")
    spaces.add_argument("--json", action="store_true", help="Print raw JSON.")
    spaces.set_defaults(handler=command_spaces)

    list_parser = subparsers.add_parser("list", help="List Confluence pages in one space as compact CSV-style rows.")
    add_common_options(list_parser, suppress_defaults=True)
    list_parser.add_argument("--space", required=True, help="Confluence space key to list.")
    list_parser.add_argument("--limit", type=int, default=25, help="Maximum pages to print.")
    list_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    list_parser.set_defaults(handler=command_list)

    tree = subparsers.add_parser("tree", help="Print a Confluence page tree for one space or root page.")
    add_common_options(tree, suppress_defaults=True)
    tree.add_argument("--space", required=True, help="Confluence space key for output labeling and rootless tree fetches.")
    tree.add_argument("--root", help="Optional root page id. When set, uses the descendants endpoint.")
    tree.add_argument("--depth", type=int, default=3, help="Maximum tree depth to print.")
    tree.add_argument("--max-pages", type=int, default=200, help="Maximum pages to fetch for the tree.")
    tree.add_argument("--json", action="store_true", help="Print raw JSON.")
    tree.set_defaults(handler=command_tree)

    search = subparsers.add_parser("search", help="Search Confluence pages with bounded CQL.")
    add_common_options(search, suppress_defaults=True)
    search.add_argument("--query", help="Confluence text query. Searches page text.")
    search.add_argument("--space", action="append", help="Confluence space key to search. Repeatable.")
    search.add_argument("--all-spaces", action="store_true", help="Allow searching every accessible space.")
    search.add_argument("--cql", help="Explicit CQL query. Use carefully; bypasses default space bounds.")
    search.add_argument("--limit", type=int, default=10, help="Maximum pages to print.")
    search.add_argument("--json", action="store_true", help="Print raw JSON.")
    search.set_defaults(handler=command_search)

    page = subparsers.add_parser("page", help="Fetch one Confluence page.")
    add_common_options(page, suppress_defaults=True)
    page.add_argument("page_id")
    page.add_argument("--full", action="store_true", help="Fetch children, attachments, and comments metadata too.")
    page.add_argument("--body-limit", type=int, default=4000, help="Maximum body text characters in text output.")
    page.add_argument("--json", action="store_true", help="Print raw JSON.")
    page.set_defaults(handler=command_page)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env_file = resolve_env_file(getattr(args, "env_file", None))
    load_dotenv(env_file)

    try:
        if getattr(args, "no_profile", False):
            return args.handler(args)
        args.profile = selected_profile_name(args)
        profile = get_profile(args.profile)
        return args.handler(args, profile)
    except ConfluenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
