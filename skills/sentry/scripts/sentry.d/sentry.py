#!/usr/bin/env python3
"""
Pull compact Sentry issue context for local workspace triage.

Usage:
  sentry profiles
  sentry projects [--profile example | --all-profiles] [--limit 25]
  sentry list [--profile example | --all-profiles] [--days 7] [--project slug] [--limit 25]
  sentry search --query "is:unresolved" [--profile example | --all-profiles] [--project slug]
  sentry detail ISSUE_ID_OR_SHORT_ID [--profile example]
  sentry events ISSUE_ID_OR_SHORT_ID [--profile example] [--limit 5] [--full]
  sentry inspect ISSUE_ID_OR_SHORT_ID [--profile example] [--event-limit 3]
  sentry resolve ISSUE_ID_OR_SHORT_ID [--profile example] [--confirm]

Inputs:
  Reads process env or an explicit/shared/isolated dotenv. Configure SENTRY_PROFILES
  and SENTRY_<PROFILE>_* keys; see references/cli.md for setup. Secrets must stay in an
  owner-only environment file.

Outputs:
  Writes compact text summaries to stdout. List/search output is CSV-style rows.
  No raw JSON unless --json is provided. Read commands do not mutate Sentry.
  resolve is dry-run by default and updates one Sentry issue only with --confirm.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
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
from pathlib import Path
from typing import Any, Callable


ISSUE_LIST_COLUMNS = [
    "id",
    "short_id",
    "title",
    "project",
    "level",
    "priority",
    "status",
    "events",
    "users",
    "first_seen",
    "last_seen",
    "profile",
]

EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
IP_CANDIDATE_PATTERN = re.compile(r"(?<![0-9A-Fa-f:.])\[?[0-9A-Fa-f:.]+\]?(?![0-9A-Fa-f:.])")


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("SENTRY_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "sentry" / "env")
    candidates.append(xdg / "sentry" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


DEFAULT_ENV = resolve_env_file()


class SentryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    token: str
    org: str
    base_url: str
    projects: list[str]
    label: str


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


def env_name(profile: str, suffix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile).strip("_").upper()
    return f"SENTRY_{normalized}_{suffix}"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("SENTRY_PROFILES"))
    default = os.environ.get("SENTRY_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    names = set()
    pattern = re.compile(r"^SENTRY_([A-Z0-9_]+)_(TOKEN|ORG|BASE_URL|PROJECTS|LABEL)$")
    for key in os.environ:
        match = pattern.match(key)
        if not match:
            continue
        raw_name = match.group(1)
        if raw_name == "AUTH":
            continue
        names.add(raw_name.lower().replace("_", "-"))
    return sorted(names)


def validate_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise SentryError(f"Invalid Sentry base URL: {value!r}. Configure an HTTPS origin only.") from exc

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
        raise SentryError(f"Invalid Sentry base URL: {value!r}. Configure an HTTPS origin only, without credentials or a path.")

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
    token = os.environ.get(env_name(name, "TOKEN")) or os.environ.get("SENTRY_AUTH_TOKEN", "")
    org = os.environ.get(env_name(name, "ORG"), "")
    base_url = os.environ.get(env_name(name, "BASE_URL"), "https://sentry.io").rstrip("/")
    projects = split_csv(os.environ.get(env_name(name, "PROJECTS")))
    label = os.environ.get(env_name(name, "LABEL"), name)

    missing = []
    if not token:
        missing.append(env_name(name, "TOKEN"))
    if not org:
        missing.append(env_name(name, "ORG"))

    if missing:
        raise SentryError(
            "Missing Sentry config: "
            + ", ".join(missing)
            + ". Add it to the secrets dotenv or export it in the shell."
        )

    base_url = validate_base_url(base_url)

    return Profile(name=name, token=token, org=org, base_url=base_url, projects=projects, label=label)


def redact_sensitive(value: str) -> str:
    """Redact email and IP values from human-readable output."""
    value = EMAIL_PATTERN.sub("[redacted-email]", value)

    def replace_ip(match: re.Match[str]) -> str:
        candidate = match.group(0)
        unwrapped = candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
        if "." not in unwrapped and ":" not in unwrapped:
            return candidate
        try:
            ipaddress.ip_address(unwrapped)
        except ValueError:
            return candidate
        return "[redacted-ip]"

    return IP_CANDIDATE_PATTERN.sub(replace_ip, value)


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback

    value = redact_sensitive(str(value).replace("\n", " ").strip())
    return value if value else fallback


def truncate(value: Any, limit: int = 180) -> str:
    value = text(value)
    if len(value) <= limit:
        return value

    if limit <= 3:
        return value[:limit]

    return value[: limit - 3].rstrip() + "..."


def compact_date(value: Any) -> str:
    value = text(value)
    if value == "-":
        return value

    match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", value)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value.replace("T", " ").replace("Z", "")


def request(
    profile: Profile,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    retries: int = 2,
) -> tuple[Any, dict[str, str]]:
    url = validate_base_url(profile.base_url) + "/api/0/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    body = None
    headers = {
        "Authorization": f"Bearer {profile.token}",
        "Accept": "application/json",
        "User-Agent": "workspace-sentry/1.0",
    }

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with open_url(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else None
                return data, dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if method == "GET" and exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(delay, 30))
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"detail": raw[:500]}

            detail = data.get("detail") if isinstance(data, dict) else data
            raise SentryError(f"Sentry API {exc.code} profile={profile.name}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SentryError(f"Sentry API request failed profile={profile.name}: {exc.reason}") from exc

    raise SentryError(f"Sentry API request exhausted retries profile={profile.name}")


def fetch_projects(profile: Profile) -> list[dict[str, Any]]:
    # Global /api/0/projects/ is gone (410). Org-scoped list is the supported path.
    projects, _ = request(profile, "GET", f"organizations/{profile.org}/projects/")
    return projects if isinstance(projects, list) else []


def resolve_project_ids(profile: Profile, project_slugs: list[str]) -> list[str]:
    if not project_slugs:
        return []

    projects = fetch_projects(profile)
    lookup = {project.get("slug"): str(project.get("id")) for project in projects}
    missing = [slug for slug in project_slugs if slug not in lookup]

    if missing:
        raise SentryError(
            "Unknown Sentry project slug(s): "
            + ", ".join(missing)
            + ". Run `sentry projects` to list accessible projects."
        )

    return [lookup[slug] for slug in project_slugs]


def selected_project_slugs(args: argparse.Namespace, profile: Profile) -> list[str]:
    explicit = getattr(args, "project", None) or []
    if explicit:
        return explicit
    if getattr(args, "all_projects", False):
        return []
    if profile.projects:
        return profile.projects
    raise SentryError(
        f"No Sentry projects configured for profile={profile.name}. "
        "Set SENTRY_<PROFILE>_PROJECTS, pass --project, or pass --all-projects for a broad search."
    )


def search_issues(
    profile: Profile,
    query: str,
    limit: int,
    sort: str = "date",
    project_slugs: list[str] | None = None,
    short_id_lookup: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "sort": sort, "limit": limit}
    if short_id_lookup:
        params["shortIdLookup"] = "1"

    project_ids = resolve_project_ids(profile, project_slugs or [])
    if project_ids:
        params["project"] = project_ids

    issues, _ = request(profile, "GET", f"organizations/{profile.org}/issues/", params=params)
    return issues if isinstance(issues, list) else []


def resolve_issue_identifier(profile: Profile, identifier: str) -> str:
    if re.fullmatch(r"\d+", identifier):
        return identifier

    issues = search_issues(
        profile,
        identifier,
        limit=2,
        sort="date",
        project_slugs=[],
        short_id_lookup=True,
    )
    normalized = identifier.upper()
    matches = [
        issue
        for issue in issues
        if text(issue.get("shortId"), "").upper() == normalized or text(issue.get("id"), "") == identifier
    ]
    if len(matches) == 1:
        return text(matches[0].get("id"), "")
    if not matches:
        raise SentryError(f"Could not resolve Sentry issue identifier {identifier!r}.")
    raise SentryError(f"Sentry issue identifier {identifier!r} matched multiple issues.")


def fetch_issue(profile: Profile, issue_identifier: str) -> dict[str, Any]:
    issue_id = resolve_issue_identifier(profile, issue_identifier)
    params = {"expand": ["integrationIssues", "latestEventHasAttachments", "owners"]}
    issue, _ = request(profile, "GET", f"organizations/{profile.org}/issues/{issue_id}/", params=params)
    return issue if isinstance(issue, dict) else {}


def fetch_external_issues(profile: Profile, issue_id: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        data, _ = request(profile, "GET", f"organizations/{profile.org}/issues/{issue_id}/external-issues/")
    except SentryError as exc:
        return [], str(exc)

    return data if isinstance(data, list) else [], None


def fetch_events(profile: Profile, issue_identifier: str, limit: int, full: bool = False) -> list[dict[str, Any]]:
    issue_id = resolve_issue_identifier(profile, issue_identifier)
    params: dict[str, Any] = {"per_page": limit}
    if full:
        params["full"] = "1"
    events, _ = request(profile, "GET", f"organizations/{profile.org}/issues/{issue_id}/events/", params=params)
    return events if isinstance(events, list) else []


def metadata_description(issue: dict[str, Any]) -> str:
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    parts = []

    for key in ("type", "value", "filename", "function"):
        value = metadata.get(key)
        if value:
            parts.append(f"{key}={truncate(value, 90)}")

    culprit = issue.get("culprit")
    if culprit:
        parts.append(f"culprit={truncate(culprit, 120)}")

    if not parts:
        return truncate(issue.get("description") or issue.get("logger") or issue.get("title"), 180)

    return "; ".join(parts)


def project_slug(issue: dict[str, Any]) -> str:
    project = issue.get("project")
    if isinstance(project, dict):
        return text(project.get("slug") or project.get("name"))

    return text(project)


def issue_row(issue: dict[str, Any], profile: Profile) -> list[str]:
    return [
        text(issue.get("id"), ""),
        text(issue.get("shortId") or issue.get("short_id"), ""),
        truncate(issue.get("title") or issue.get("culprit"), 160),
        project_slug(issue),
        text(issue.get("level")),
        text(issue.get("priority")),
        text(issue.get("status")),
        text(issue.get("count")),
        text(issue.get("userCount")),
        compact_date(issue.get("firstSeen")),
        compact_date(issue.get("lastSeen")),
        profile.name,
    ]


def print_issue_rows(rows: list[tuple[dict[str, Any], Profile]]) -> None:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(ISSUE_LIST_COLUMNS)
    for issue, profile in rows:
        writer.writerow(issue_row(issue, profile))


def issue_line(issue: dict[str, Any], profile: Profile) -> str:
    issue_id = text(issue.get("id"))
    short_id = text(issue.get("shortId"), issue_id)
    title = truncate(issue.get("title") or issue.get("culprit"), 160)
    description = truncate(metadata_description(issue), 220)
    permalink = text(issue.get("permalink"))

    count = text(issue.get("count"))
    users = text(issue.get("userCount"))
    level = text(issue.get("level"))
    status = text(issue.get("status"))
    priority = text(issue.get("priority"))
    first_seen = compact_date(issue.get("firstSeen"))
    last_seen = compact_date(issue.get("lastSeen"))

    return "\n".join(
        [
            f"- {short_id} | id={issue_id} | project={project_slug(issue)} | {level}/{priority} | {status} | events={count} users={users} | first={first_seen} last={last_seen}",
            f"  title: {title}",
            f"  description: {description}",
            f"  detail: sentry detail {short_id} --profile {profile.name}",
            f"  inspect: sentry inspect {short_id} --profile {profile.name}",
            f"  resolve_preview: sentry resolve {short_id} --profile {profile.name}",
            f"  link: {permalink}",
        ]
    )


def normalize_external_issue(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {}

    display_name = (
        item.get("displayName")
        or item.get("display_name")
        or item.get("title")
        or item.get("identifier")
        or item.get("key")
        or item.get("id")
    )
    web_url = item.get("webUrl") or item.get("web_url") or item.get("url")
    service_type = item.get("serviceType") or item.get("service_type") or item.get("provider") or item.get("service")
    issue_id = item.get("issueId") or item.get("issue_id")

    normalized = {
        "id": text(item.get("id"), ""),
        "issueId": text(issue_id, ""),
        "serviceType": text(service_type, ""),
        "displayName": text(display_name, ""),
        "webUrl": text(web_url, ""),
    }
    return normalized if any(normalized.values()) else {}


def collect_embedded_external_issues(issue: dict[str, Any]) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    for key in ("integrationIssues", "sentryAppIssues"):
        value = issue.get(key)
        if isinstance(value, list):
            for item in value:
                normalized = normalize_external_issue(item)
                if normalized:
                    collected.append(normalized)
    return collected


def merge_external_issues(issue: dict[str, Any], external_items: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, str]] = []
    for item in [*collect_embedded_external_issues(issue), *(normalize_external_issue(item) for item in external_items)]:
        key = (item.get("id", ""), item.get("displayName", ""), item.get("webUrl", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def print_external_issues(external_issues: list[dict[str, str]], error: str | None = None) -> None:
    print(f"external_issues: count={len(external_issues)}")
    if error:
        print(f"  warning: {truncate(error, 240)}")
    for item in external_issues:
        print(
            "  - "
            + " | ".join(
                [
                    f"service={text(item.get('serviceType'))}",
                    f"display={truncate(item.get('displayName'), 140)}",
                    f"url={text(item.get('webUrl'))}",
                ]
            )
        )


def normalized_issue(
    issue: dict[str, Any],
    profile: Profile,
    external_issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    assigned = issue.get("assignedTo")
    assigned_to = None
    if isinstance(assigned, dict):
        assigned_to = {
            "type": text(assigned.get("type"), ""),
            "id": text(assigned.get("id"), ""),
            "name": text(assigned.get("name"), ""),
        }

    return {
        "id": text(issue.get("id"), ""),
        "shortId": text(issue.get("shortId"), ""),
        "profile": profile.name,
        "label": profile.label,
        "org": profile.org,
        "project": project_slug(issue),
        "title": text(issue.get("title"), ""),
        "culprit": text(issue.get("culprit"), ""),
        "level": text(issue.get("level"), ""),
        "priority": text(issue.get("priority"), ""),
        "status": text(issue.get("status"), ""),
        "substatus": text(issue.get("substatus"), ""),
        "events": text(issue.get("count"), ""),
        "users": text(issue.get("userCount"), ""),
        "firstSeen": text(issue.get("firstSeen"), ""),
        "lastSeen": text(issue.get("lastSeen"), ""),
        "permalink": text(issue.get("permalink"), ""),
        "assignedTo": assigned_to,
        "externalIssues": external_issues or [],
    }


def event_title(event: dict[str, Any]) -> str:
    if event.get("title"):
        return truncate(event.get("title"), 180)

    if event.get("message"):
        return truncate(event.get("message"), 180)

    entries = event.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            data = entry.get("data") if isinstance(entry, dict) else None
            if isinstance(data, dict) and data.get("value"):
                return truncate(data.get("value"), 180)

    return "-"


def event_user_label(event: dict[str, Any]) -> str:
    user = event.get("user")
    if not isinstance(user, dict):
        return "-"

    has_user = any(user.get(key) for key in ("id", "email", "username", "ip_address", "name"))
    return "present" if has_user else "-"


def event_tag_value(event: dict[str, Any], key: str) -> str:
    tags = event.get("tags")
    if not isinstance(tags, list):
        return "-"

    for tag in tags:
        if isinstance(tag, dict) and tag.get("key") == key:
            return truncate(tag.get("value"), 120)
    return "-"


def event_release(event: dict[str, Any]) -> str:
    return truncate(event.get("release") or event_tag_value(event, "release"), 120)


def event_environment(event: dict[str, Any]) -> str:
    return truncate(event.get("environment") or event_tag_value(event, "environment"), 120)


def event_date(event: dict[str, Any]) -> str:
    for key in ("dateCreated", "datetime", "dateReceived", "date"):
        value = event.get(key)
        if value:
            return compact_date(value)
    return "-"


def iter_stack_frames(event: dict[str, Any]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    entries = event.get("entries")
    if not isinstance(entries, list):
        return frames

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        values = data.get("values")
        if isinstance(values, list):
            for value in values:
                stacktrace = value.get("stacktrace") if isinstance(value, dict) else None
                if isinstance(stacktrace, dict) and isinstance(stacktrace.get("frames"), list):
                    frames.extend(frame for frame in stacktrace["frames"] if isinstance(frame, dict))
        stacktrace = data.get("stacktrace")
        if isinstance(stacktrace, dict) and isinstance(stacktrace.get("frames"), list):
            frames.extend(frame for frame in stacktrace["frames"] if isinstance(frame, dict))
    return frames


def frame_label(frame: dict[str, Any]) -> str:
    filename = frame.get("filename") or frame.get("absPath") or frame.get("module") or "-"
    function = frame.get("function") or "-"
    line_no = frame.get("lineno") or frame.get("lineNo")
    location = text(filename)
    if line_no:
        location += f":{line_no}"
    return f"{truncate(location, 120)} in {truncate(function, 80)}"


def stack_summary(event: dict[str, Any], limit: int = 3) -> str:
    frames = iter_stack_frames(event)
    if not frames:
        location = event.get("location")
        return truncate(location, 180) if location else "-"

    app_frames = [frame for frame in frames if frame.get("inApp")]
    selected = (app_frames or frames)[-limit:]
    return "; ".join(frame_label(frame) for frame in selected) or "-"


def normalized_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": text(event.get("eventID") or event.get("id"), ""),
        "date": event_date(event),
        "title": text(event.get("title") or event.get("message"), ""),
        "environment": event_environment(event),
        "release": event_release(event),
        "user": event_user_label(event),
        "platform": text(event.get("platform"), ""),
        "stack": stack_summary(event),
    }


def print_event(event: dict[str, Any]) -> None:
    event_id = text(event.get("eventID") or event.get("id"))
    print(
        "- "
        + " | ".join(
            [
                f"event={event_id}",
                f"date={event_date(event)}",
                f"environment={event_environment(event)}",
                f"release={event_release(event)}",
                f"user={event_user_label(event)}",
            ]
        )
    )
    print(f"  title: {event_title(event)}")
    stack = stack_summary(event)
    if stack != "-":
        print(f"  stack: {stack}")


def command_profiles(args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print("No Sentry profiles configured. Set SENTRY_PROFILES or SENTRY_DEFAULT_PROFILE in .env.")
        return 0

    print("Sentry profiles")
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
                        f"org={profile.org}",
                        f"token=configured",
                        f"projects={','.join(profile.projects) or '-'}",
                    ]
                )
            )
        except SentryError as exc:
            print(f"- profile={name} | error={exc}")
    return 0


def selected_profiles(args: argparse.Namespace) -> list[Profile]:
    if getattr(args, "all_profiles", False):
        names = configured_profile_names()
        if not names:
            raise SentryError("No Sentry profiles configured. Set SENTRY_PROFILES or SENTRY_DEFAULT_PROFILE in .env.")
        return [get_profile(name) for name in names]
    return [get_profile(selected_profile_name(args))]


def selected_profile_name(args: argparse.Namespace) -> str:
    profile_name = getattr(args, "profile", None) or os.environ.get("SENTRY_DEFAULT_PROFILE", "")
    if profile_name:
        return profile_name

    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if names:
        raise SentryError("Multiple Sentry profiles configured. Pass --profile or set SENTRY_DEFAULT_PROFILE.")
    raise SentryError("No Sentry profile selected. Pass --profile or set SENTRY_DEFAULT_PROFILE.")


def command_projects(args: argparse.Namespace) -> int:
    profiles = selected_profiles(args)
    results: list[tuple[Profile, list[dict[str, Any]]]] = []
    for profile in profiles:
        projects = fetch_projects(profile)[: args.limit]
        results.append((profile, projects))

    if args.json:
        payload = [
            {"profile": profile.name, "org": profile.org, "projects": projects}
            for profile, projects in results
        ]
        print(json.dumps(payload if args.all_profiles else results[0][1], indent=2, sort_keys=True))
        return 0

    for profile, projects in results:
        print(f"Sentry projects | profile={profile.name} label={profile.label} org={profile.org} returned={len(projects)}")
        print(f"configured_projects={','.join(profile.projects) or '-'}")
        for project in projects:
            print(
                "- "
                + " | ".join(
                    [
                        f"slug={text(project.get('slug'))}",
                        f"id={text(project.get('id'))}",
                        f"name={text(project.get('name'))}",
                        f"platform={text(project.get('platform'))}",
                    ]
                )
            )
    return 0


def command_search(args: argparse.Namespace) -> int:
    profiles = selected_profiles(args)
    rows: list[tuple[dict[str, Any], Profile]] = []
    json_results: list[dict[str, Any]] = []
    for profile in profiles:
        issues = search_issues(
            profile,
            query=args.query,
            limit=args.limit,
            sort=args.sort,
            project_slugs=selected_project_slugs(args, profile),
        )
        rows.extend((issue, profile) for issue in issues)
        json_results.append({"profile": profile.name, "org": profile.org, "issues": issues})

    if args.json:
        print(json.dumps(json_results if args.all_profiles else json_results[0]["issues"], indent=2, sort_keys=True))
        return 0

    print_issue_rows(rows)
    return 0


def build_list_query(args: argparse.Namespace) -> str:
    if args.query is not None:
        return args.query

    days = str(args.days).strip()
    return f"is:unresolved lastSeen:-{days}d"


def command_list(args: argparse.Namespace) -> int:
    args.query = build_list_query(args)
    return command_search(args)


def command_detail(args: argparse.Namespace, profile: Profile) -> int:
    issue = fetch_issue(profile, args.issue)
    external_raw, external_error = fetch_external_issues(profile, text(issue.get("id"), args.issue))
    external_issues = merge_external_issues(issue, external_raw)

    if args.json:
        print(
            json.dumps(
                {
                    "issue": issue,
                    "externalIssues": external_raw,
                    "externalIssuesError": external_error,
                    "normalized": normalized_issue(issue, profile, external_issues),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Sentry issue detail | profile={profile.name} label={profile.label} org={profile.org}")
    print(issue_line(issue, profile))
    print("extra:")
    for key in ("firstSeen", "lastSeen", "logger", "numComments", "userReportCount", "substatus"):
        print(f"  {key}: {truncate(issue.get(key), 240)}")

    annotations = issue.get("annotations")
    if annotations:
        print("annotations:")
        for annotation in annotations[:5]:
            print(f"  - {truncate(annotation, 240)}")

    latest_event = issue.get("latestEvent")
    if isinstance(latest_event, dict):
        print("latest_event:")
        print(f"  eventID: {text(latest_event.get('eventID') or latest_event.get('id'))}")
        print(f"  title: {truncate(latest_event.get('title') or latest_event.get('message'), 220)}")
        print(f"  platform: {text(latest_event.get('platform'))}")

    print_external_issues(external_issues, external_error)
    print(f"events: sentry events {text(issue.get('id'), args.issue)} --profile {profile.name}")
    return 0


def command_events(args: argparse.Namespace, profile: Profile) -> int:
    events = fetch_events(profile, args.issue, args.limit, full=args.full)

    if args.json:
        print(json.dumps(events, indent=2, sort_keys=True))
        return 0

    print(
        f"Sentry issue events | profile={profile.name} label={profile.label} org={profile.org} "
        f"issue={args.issue} limit={args.limit} full={str(args.full).lower()}"
    )
    print(f"returned={len(events)}")
    for event in events[: args.limit]:
        print_event(event)
    return 0


def command_inspect(args: argparse.Namespace, profile: Profile) -> int:
    issue = fetch_issue(profile, args.issue)
    issue_id = text(issue.get("id"), args.issue)
    external_raw, external_error = fetch_external_issues(profile, issue_id)
    external_issues = merge_external_issues(issue, external_raw)
    events = fetch_events(profile, issue_id, args.event_limit, full=True)

    if args.json:
        print(
            json.dumps(
                {
                    "issue": issue,
                    "externalIssues": external_raw,
                    "externalIssuesError": external_error,
                    "events": events,
                    "normalized": {
                        "issue": normalized_issue(issue, profile, external_issues),
                        "events": [normalized_event(event) for event in events],
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Sentry inspect | profile={profile.name} label={profile.label} org={profile.org}")
    print(issue_line(issue, profile))
    print_external_issues(external_issues, external_error)
    print(f"events: count={len(events)}")
    for event in events[: args.event_limit]:
        print_event(event)
    return 0


def command_resolve(args: argparse.Namespace, profile: Profile) -> int:
    issue = fetch_issue(profile, args.issue)
    issue_id = text(issue.get("id"), args.issue)

    if not args.confirm:
        if args.json:
            print(
                json.dumps(
                    {"dryRun": True, "payload": {"status": "resolved"}, "normalized": normalized_issue(issue, profile)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        print(f"DRY-RUN Sentry resolve | profile={profile.name} label={profile.label} org={profile.org}")
        print(f"would_update={issue_id} status=resolved")
        print(issue_line(issue, profile))
        print("Add --confirm to update this one Sentry issue.")
        return 0

    payload: dict[str, Any] = {"status": "resolved"}
    updated, _ = request(
        profile,
        "PUT",
        f"organizations/{profile.org}/issues/{issue_id}/",
        payload=payload,
        retries=0,
    )
    updated_issue = updated if isinstance(updated, dict) else {}

    if args.json:
        print(json.dumps({"issue": updated_issue, "normalized": normalized_issue(updated_issue, profile)}, indent=2, sort_keys=True))
        return 0

    print(f"Resolved Sentry issue id={issue_id} profile={profile.name} label={profile.label} org={profile.org}")
    print(f"status={text(updated_issue.get('status'))} substatus={text(updated_issue.get('substatus'))}")
    print(f"title={truncate(updated_issue.get('title'), 180)}")
    return 0


def add_env_option(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--env-file",
        default=default,
        help="Path to dotenv file. Defaults to the configured shared or isolated Sentry env.",
    )


def add_profile_option(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--profile",
        default=default,
        help="Sentry profile name from SENTRY_<PROFILE>_* env vars.",
    )


def add_all_profiles_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all-profiles", action="store_true", help="Run the command across configured Sentry profiles.")


def add_search_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sort", default="date", help="Sentry sort, e.g. date, freq, user, new, inbox.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum issues to print.")
    parser.add_argument(
        "--project",
        action="append",
        help="Filter by Sentry project slug. Repeatable. Defaults to configured profile projects.",
    )
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="Do not auto-filter to SENTRY_<PROFILE>_PROJECTS.",
    )
    parser.add_argument(
        "--no-config-projects",
        dest="all_projects",
        action="store_true",
        help="Compatibility alias for --all-projects.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull compact Sentry issue context for workspace triage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              sentry profiles
              sentry list --profile example --days 7
              sentry search --profile example --query "is:unresolved" --limit 10
              sentry detail EXAMPLE-1 --profile example
              sentry inspect EXAMPLE-1 --profile example --event-limit 1
              sentry resolve EXAMPLE-1 --profile example
            """
        ),
    )

    add_env_option(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List configured Sentry profiles.")
    add_env_option(profiles, suppress_defaults=True)
    profiles.set_defaults(handler=command_profiles, needs_profile=False)

    projects = subparsers.add_parser("projects", help="List accessible Sentry projects.")
    add_env_option(projects, suppress_defaults=True)
    add_profile_option(projects, suppress_defaults=True)
    add_all_profiles_option(projects)
    projects.add_argument("--limit", type=int, default=25, help="Maximum projects to print.")
    projects.add_argument("--json", action="store_true", help="Print raw JSON.")
    projects.set_defaults(handler=command_projects, needs_profile=False)

    list_parser = subparsers.add_parser("list", help="List recent unresolved Sentry issues.")
    add_env_option(list_parser, suppress_defaults=True)
    add_profile_option(list_parser, suppress_defaults=True)
    add_all_profiles_option(list_parser)
    list_parser.add_argument("--days", type=int, default=7, help="Recent lastSeen window for default query.")
    list_parser.add_argument("--query", help="Override the default Sentry issue search query.")
    add_search_options(list_parser)
    list_parser.set_defaults(handler=command_list, needs_profile=False)

    search = subparsers.add_parser("search", help="Search Sentry issues with an explicit query.")
    add_env_option(search, suppress_defaults=True)
    add_profile_option(search, suppress_defaults=True)
    add_all_profiles_option(search)
    search.add_argument("--query", required=True, help="Sentry issue search query.")
    add_search_options(search)
    search.set_defaults(handler=command_search, needs_profile=False)

    detail = subparsers.add_parser("detail", help="Show compact issue detail.")
    add_env_option(detail, suppress_defaults=True)
    add_profile_option(detail, suppress_defaults=True)
    detail.add_argument("issue", metavar="ISSUE_ID_OR_SHORT_ID")
    detail.add_argument("--json", action="store_true", help="Print raw JSON plus normalized fields.")
    detail.set_defaults(handler=command_detail, needs_profile=True)

    events = subparsers.add_parser("events", help="Show compact recent events for an issue.")
    add_env_option(events, suppress_defaults=True)
    add_profile_option(events, suppress_defaults=True)
    events.add_argument("issue", metavar="ISSUE_ID_OR_SHORT_ID")
    events.add_argument("--limit", type=int, default=5, help="Maximum events to print.")
    events.add_argument("--full", action="store_true", help="Ask Sentry for full event payloads.")
    events.add_argument("--json", action="store_true", help="Print raw JSON.")
    events.set_defaults(handler=command_events, needs_profile=True)

    inspect = subparsers.add_parser("inspect", help="Show issue detail, external links, and recent event evidence.")
    add_env_option(inspect, suppress_defaults=True)
    add_profile_option(inspect, suppress_defaults=True)
    inspect.add_argument("issue", metavar="ISSUE_ID_OR_SHORT_ID")
    inspect.add_argument("--event-limit", type=int, default=3, help="Maximum full events to inspect.")
    inspect.add_argument("--json", action="store_true", help="Print raw JSON plus normalized fields.")
    inspect.set_defaults(handler=command_inspect, needs_profile=True)

    resolve = subparsers.add_parser("resolve", help="Mark one Sentry issue resolved; dry-run by default.")
    add_env_option(resolve, suppress_defaults=True)
    add_profile_option(resolve, suppress_defaults=True)
    resolve.add_argument("issue", metavar="ISSUE_ID_OR_SHORT_ID")
    resolve.add_argument("--confirm", action="store_true", help="Required to perform the Sentry mutation.")
    resolve.add_argument("--json", action="store_true", help="Print raw JSON plus normalized fields.")
    resolve.set_defaults(handler=command_resolve, needs_profile=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env_file = resolve_env_file(getattr(args, "env_file", None))
    load_dotenv(env_file)

    try:
        if getattr(args, "needs_profile", False):
            profile = get_profile(selected_profile_name(args))
            handler: Callable[[argparse.Namespace, Profile], int] = args.handler
            return handler(args, profile)

        handler_no_profile: Callable[[argparse.Namespace], int] = args.handler
        return handler_no_profile(args)
    except SentryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
