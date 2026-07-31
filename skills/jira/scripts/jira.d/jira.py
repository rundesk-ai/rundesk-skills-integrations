#!/usr/bin/env python3
"""
Pull Jira Cloud issue context for local workspace triage.

Usage:
  jira profiles
  jira whoami [--profile example]
  jira projects [--profile example]
  jira list [--profile example] [--project APP] [--limit 25]
  jira search --jql 'project = APP ORDER BY updated DESC' [--profile example]
  jira detail APP-123 [--profile example] [--full] [--json]
  jira comments APP-123 [--profile example] [--json]
  jira attachments APP-123 [--profile example] [--json]
  jira attachment --id EXAMPLE_ATTACHMENT_ID --output /tmp/example.png [--profile example] [--confirm]
  jira identify "Fix APP-123" [--all-profiles]

Inputs:
  Reads process env or an explicit/shared/isolated dotenv. Configure JIRA_<PROFILE>_* keys;
  see the README ## Provider section and .env.example. Secrets must stay in local .env only.

Outputs:
  Writes compact text summaries to stdout. List/search output is CSV-style rows.
  No raw JSON unless --json is provided. The integration does not mutate Jira.
  Attachment bytes are downloaded only by the explicit attachment --output --confirm command.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_FIELDS = "summary,status,assignee,updated,project,issuetype,priority,creator,reporter"


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("JIRA_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "jira" / "env")
    candidates.append(xdg / "jira" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


DEFAULT_ENV = resolve_env_file()
DETAIL_FIELDS = DEFAULT_FIELDS + ",description,attachment,labels,components,fixVersions"
ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
ISSUE_LIST_COLUMNS = ["key", "title", "type", "status", "priority", "assignee", "updated", "project", "profile"]


class JiraError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    email: str
    token: str
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
    return f"JIRA_{normalized}_{suffix}"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("JIRA_PROFILES"))
    default = os.environ.get("JIRA_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names


def validate_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise JiraError(f"Invalid Jira base URL: {value!r}. Configure an HTTPS origin only.") from exc

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
        raise JiraError(f"Invalid Jira base URL: {value!r}. Configure an HTTPS origin only, without credentials or a path.")

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
    base_url = os.environ.get(env_name(name, "BASE_URL"), "")
    email = os.environ.get(env_name(name, "EMAIL"), "")
    token = os.environ.get(env_name(name, "API_TOKEN"), "")
    projects = split_csv(os.environ.get(env_name(name, "PROJECTS")))
    label = os.environ.get(env_name(name, "LABEL"), name)

    missing = []
    if not base_url:
        missing.append(env_name(name, "BASE_URL"))
    if not email:
        missing.append(env_name(name, "EMAIL"))
    if not token:
        missing.append(env_name(name, "API_TOKEN"))

    if missing:
        raise JiraError(
            "Missing Jira config: "
            + ", ".join(missing)
            + ". Add it to the secrets dotenv or export it in the shell."
        )

    base_url = validate_base_url(base_url)

    return Profile(name=name, base_url=base_url, email=email, token=token, projects=projects, label=label)


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback

    value = str(value).replace("\n", " ").strip()
    return value if value else fallback


def truncate(value: Any, limit: int = 180) -> str:
    value = text(value)
    if len(value) <= limit:
        return value

    if limit <= 3:
        return value[:limit]

    return value[: limit - 3].rstrip() + "..."


def compact_datetime(value: Any) -> str:
    value = text(value)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", value)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value


def compact_size(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "-"

    units = ["B", "KB", "MB", "GB"]
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024

    if unit == "B":
        return f"{int(amount)}B"

    formatted = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{unit}"


def auth_header(profile: Profile) -> str:
    encoded = base64.b64encode(f"{profile.email}:{profile.token}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


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
        "User-Agent": "workspace-jira/1.0",
    }

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with open_url(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
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
                data = {"errorMessages": [raw[:500]]}

            message = data.get("errorMessages") or data.get("errors") or data
            raise JiraError(f"Jira API {exc.code} profile={profile.name}: {message}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"Jira API request failed profile={profile.name}: {exc.reason}") from exc

    raise JiraError(f"Jira API request exhausted retries profile={profile.name}")


def request_bytes(
    profile: Profile,
    path: str,
    params: dict[str, Any] | None = None,
    retries: int = 2,
) -> bytes:
    url = validate_base_url(profile.base_url) + "/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    headers = {
        "Authorization": auth_header(profile),
        "Accept": "application/json",
        "User-Agent": "workspace-jira/1.0",
    }

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with open_url(req, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(delay, 30))
                continue
            raise JiraError(f"Jira attachment download {exc.code} profile={profile.name}: {raw[:500]}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"Jira attachment download failed profile={profile.name}: {exc.reason}") from exc

    raise JiraError(f"Jira attachment download exhausted retries profile={profile.name}")


def adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            part = adf_to_text(item)
            if part:
                parts.append(part)
        return "\n".join(parts)
    if not isinstance(value, dict):
        return str(value)

    node_type = value.get("type")
    pieces = []
    if value.get("text"):
        pieces.append(str(value["text"]))
    for child in value.get("content") or []:
        child_text = adf_to_text(child)
        if child_text:
            pieces.append(child_text)

    joined = join_inline_text(pieces) if node_type == "paragraph" else "\n".join(pieces)
    return joined.strip()


def join_inline_text(pieces: list[str]) -> str:
    output = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if not output:
            output = piece
            continue
        if output[-1].isspace() or piece[0].isspace() or piece[0] in ".,;:!?)]}":
            output += piece
        elif output[-1] in "([{/$":
            output += piece
        else:
            output += " " + piece
    return output


def user_label(user: Any) -> str:
    if not isinstance(user, dict):
        return "unassigned"
    return text(user.get("displayName") or user.get("accountId"), "unassigned")


def email_label(email: Any, show: bool = False) -> str:
    email = text(email)
    if email == "-":
        return "-"
    return email if show else "configured"


def field_name(fields: dict[str, Any], name: str) -> str:
    value = fields.get(name)
    return field_item_name(value)


def field_item_name(value: Any) -> str:
    if isinstance(value, dict):
        return text(value.get("name") or value.get("key") or value.get("displayName"))
    return text(value)


def list_field_names(items: Any) -> str:
    if not isinstance(items, list):
        return "-"

    names = []
    for item in items:
        if isinstance(item, dict):
            names.append(text(item.get("name") or item.get("key") or item.get("id")))
        else:
            names.append(text(item))

    return ", ".join(name for name in names if name != "-") or "-"


def issue_url(profile: Profile, key: str) -> str:
    return f"{profile.base_url}/browse/{key}"


def issue_line(issue: dict[str, Any], profile: Profile) -> str:
    fields = issue.get("fields") or {}
    key = text(issue.get("key"))
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    priority = fields.get("priority") if isinstance(fields.get("priority"), dict) else {}
    parts = [
        key,
        f"profile={profile.name}",
        f"site={profile.label}",
        f"project={text(project.get('key'))}",
        f"type={field_name(fields, 'issuetype')}",
        f"status={text(status.get('name'))}",
        f"priority={text(priority.get('name'))}",
        f"assignee={user_label(fields.get('assignee'))}",
        f"updated={compact_datetime(fields.get('updated'))}",
    ]

    return "\n".join(
        [
            "- " + " | ".join(parts),
            f"  summary: {truncate(fields.get('summary'), 220)}",
            f"  detail: jira detail {key} --profile {profile.name}",
            f"  link: {issue_url(profile, key)}",
        ]
    )


def issue_list_row(issue: dict[str, Any], profile: Profile) -> list[str]:
    fields = issue.get("fields") or {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}

    return [
        text(issue.get("key")),
        truncate(fields.get("summary"), 160),
        field_name(fields, "issuetype"),
        field_name(fields, "status"),
        field_name(fields, "priority"),
        user_label(fields.get("assignee")),
        compact_datetime(fields.get("updated")),
        text(project.get("key")),
        profile.name,
    ]


def print_issue_list(issues: list[dict[str, Any]], profile: Profile) -> None:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(ISSUE_LIST_COLUMNS)
    for issue in issues:
        writer.writerow(issue_list_row(issue, profile))


def attachment_line(attachment: Any) -> str:
    if not isinstance(attachment, dict):
        return "-"

    return " | ".join(
        [
            f"id={text(attachment.get('id'))}",
            f"file={truncate(attachment.get('filename'), 140)}",
            f"size={compact_size(attachment.get('size'))}",
            f"mime={text(attachment.get('mimeType'))}",
            f"author={user_label(attachment.get('author'))}",
            f"created={compact_datetime(attachment.get('created'))}",
        ]
    )


def normalized_user(user: Any) -> dict[str, Any] | None:
    if not isinstance(user, dict):
        return None
    return {
        "accountId": text(user.get("accountId"), ""),
        "displayName": text(user.get("displayName") or user.get("accountId"), ""),
        "active": user.get("active"),
    }


def normalized_attachment(attachment: Any) -> dict[str, Any]:
    if not isinstance(attachment, dict):
        return {}
    return {
        "id": text(attachment.get("id"), ""),
        "filename": text(attachment.get("filename"), ""),
        "mimeType": text(attachment.get("mimeType"), ""),
        "size": attachment.get("size"),
        "sizeLabel": compact_size(attachment.get("size")),
        "author": normalized_user(attachment.get("author")),
        "created": text(attachment.get("created"), ""),
        "content": text(attachment.get("content"), ""),
        "thumbnail": text(attachment.get("thumbnail"), ""),
        "self": text(attachment.get("self"), ""),
    }


def normalized_comment(comment: Any) -> dict[str, Any]:
    if not isinstance(comment, dict):
        return {}
    return {
        "id": text(comment.get("id"), ""),
        "author": normalized_user(comment.get("author")),
        "created": text(comment.get("created"), ""),
        "updated": text(comment.get("updated"), ""),
        "body": adf_to_text(comment.get("body")),
        "visibility": comment.get("visibility"),
        "self": text(comment.get("self"), ""),
    }


def normalized_issue(issue: dict[str, Any], profile: Profile, comments: list[Any] | None = None) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    attachments = fields.get("attachment") if isinstance(fields.get("attachment"), list) else []
    return {
        "key": text(issue.get("key"), ""),
        "id": text(issue.get("id"), ""),
        "profile": profile.name,
        "site": profile.label,
        "url": issue_url(profile, text(issue.get("key"), "")),
        "title": text(fields.get("summary"), ""),
        "status": field_name(fields, "status"),
        "description": adf_to_text(fields.get("description")),
        "assignee": normalized_user(fields.get("assignee")),
        "reporter": normalized_user(fields.get("reporter")),
        "creator": normalized_user(fields.get("creator")),
        "project": {"key": text(project.get("key"), ""), "name": text(project.get("name"), "")},
        "type": field_name(fields, "issuetype"),
        "priority": field_name(fields, "priority"),
        "updated": text(fields.get("updated"), ""),
        "labels": fields.get("labels") or [],
        "components": [field_item_name(item) for item in fields.get("components") or []],
        "fixVersions": [field_item_name(item) for item in fields.get("fixVersions") or []],
        "attachments": [normalized_attachment(attachment) for attachment in attachments],
        "comments": [normalized_comment(comment) for comment in comments or []],
    }


def build_project_jql(projects: list[str]) -> str:
    if not projects:
        raise JiraError(
            "No Jira projects configured for this profile. Add JIRA_<PROFILE>_PROJECTS or pass --jql/--project."
        )
    if len(projects) == 1:
        return f"project = {projects[0]} ORDER BY updated DESC"
    return f"project in ({', '.join(projects)}) ORDER BY updated DESC"


def fetch_issue(profile: Profile, issue_key: str, full: bool = False) -> dict[str, Any]:
    params = {
        "fields": "*all" if full else DETAIL_FIELDS,
        "expand": "renderedFields,names,schema",
    }
    return request(profile, f"rest/api/3/issue/{urllib.parse.quote(issue_key)}", params=params)


def fetch_comments(profile: Profile, issue_key: str) -> list[Any]:
    key = urllib.parse.quote(issue_key)
    return fetch_paginated(profile, f"rest/api/3/issue/{key}/comment", "comments")


def fetch_issue_attachments(profile: Profile, issue_key: str) -> list[Any]:
    issue = fetch_issue(profile, issue_key, full=False)
    fields = issue.get("fields") if isinstance(issue, dict) else {}
    attachments = fields.get("attachment") if isinstance(fields, dict) and isinstance(fields.get("attachment"), list) else []
    return attachments


def command_profiles(args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print("No Jira profiles configured. Set JIRA_PROFILES or JIRA_DEFAULT_PROFILE in .env.")
        return 0

    print("Jira profiles")
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
                        f"projects={','.join(profile.projects) or '-'}",
                    ]
                )
            )
        except JiraError as exc:
            print(f"- profile={name} | error={exc}")
    return 0


def command_whoami(args: argparse.Namespace, profile: Profile) -> int:
    me = request(profile, "rest/api/3/myself")
    if args.json:
        print(json.dumps(me, indent=2, sort_keys=True))
        return 0

    print(f"Jira account | profile={profile.name} site={profile.label} url={profile.base_url}")
    print(f"displayName={text(me.get('displayName'))}")
    print(f"accountId={text(me.get('accountId'))}")
    print(f"emailAddress={email_label(me.get('emailAddress'), args.show_email)}")
    return 0


def command_projects(args: argparse.Namespace, profile: Profile) -> int:
    params = {"maxResults": args.limit}
    if args.query:
        params["query"] = args.query

    data = request(profile, "rest/api/3/project/search", params=params)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    values = data.get("values", []) if isinstance(data, dict) else []
    print(f"Jira projects | profile={profile.name} site={profile.label} returned={len(values)}")
    print(f"configured_projects={','.join(profile.projects) or '-'}")
    for project in values:
        print(
            "- "
            + " | ".join(
                [
                    f"key={text(project.get('key'))}",
                    f"id={text(project.get('id'))}",
                    f"name={text(project.get('name'))}",
                    f"style={text(project.get('style'))}",
                ]
            )
        )
    return 0


def search_issues(profile: Profile, jql: str, limit: int, fields: str = DEFAULT_FIELDS) -> list[dict[str, Any]]:
    params = {"jql": jql, "maxResults": limit, "fields": fields}
    data = request(profile, "rest/api/3/search/jql", params=params)
    return data.get("issues", []) if isinstance(data, dict) else []


def command_list(args: argparse.Namespace, profile: Profile) -> int:
    projects = args.project or profile.projects
    jql = args.jql or build_project_jql(projects)
    issues = search_issues(profile, jql, args.limit)

    if args.json:
        print(json.dumps(issues, indent=2, sort_keys=True))
        return 0

    print_issue_list(issues, profile)
    return 0


def fetch_paginated(profile: Profile, path: str, collection_key: str, params: dict[str, Any] | None = None) -> list[Any]:
    params = dict(params or {})
    params.setdefault("maxResults", 100)
    params.setdefault("startAt", 0)
    items: list[Any] = []

    while True:
        data = request(profile, path, params=params)
        if not isinstance(data, dict):
            return items

        values = data.get(collection_key) or data.get("values") or []
        if isinstance(values, list):
            items.extend(values)

        start_at = int(data.get("startAt") or params.get("startAt") or 0)
        max_results = int(data.get("maxResults") or params.get("maxResults") or 100)
        total = data.get("total")
        if total is not None and start_at + max_results >= int(total):
            break
        if data.get("isLast") is True:
            break
        if not values:
            break

        params["startAt"] = start_at + max_results

    return items


def command_detail(args: argparse.Namespace, profile: Profile) -> int:
    issue = fetch_issue(profile, args.issue_key, full=args.full)
    comments = fetch_comments(profile, args.issue_key)

    extra: dict[str, Any] = {}
    if args.full:
        key = urllib.parse.quote(args.issue_key)
        extra["changelog"] = fetch_paginated(profile, f"rest/api/3/issue/{key}/changelog", "values")
        extra["worklogs"] = fetch_paginated(profile, f"rest/api/3/issue/{key}/worklog", "worklogs")

    if args.json:
        payload: dict[str, Any] = {
            "issue": issue,
            "normalized": normalized_issue(issue, profile, comments),
            **extra,
        }
        if args.full:
            payload["comments"] = comments
        print(
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    fields = issue.get("fields") or {}
    print(f"Jira issue detail | profile={profile.name} site={profile.label}")
    print(issue_line(issue, profile))
    print("fields:")
    for key in ("creator", "reporter"):
        print(f"  {key}: {user_label(fields.get(key))}")
    print(f"  labels: {', '.join(fields.get('labels') or []) or '-'}")
    print(f"  components: {list_field_names(fields.get('components'))}")
    print(f"  fixVersions: {list_field_names(fields.get('fixVersions'))}")

    attachments = fields.get("attachment") if isinstance(fields.get("attachment"), list) else []
    if attachments:
        print(f"attachments: count={len(attachments)}")
        for attachment in attachments[: args.attachment_limit]:
            print(f"  - {attachment_line(attachment)}")

    description = adf_to_text(fields.get("description"))
    if description:
        print("description:")
        print(textwrap.indent(truncate(description, args.description_limit), "  "))

    if comments:
        print(f"comments: count={len(comments)}")
        for comment in comments[: args.comment_limit]:
            author = user_label(comment.get("author"))
            created = text(comment.get("created"))
            body = truncate(adf_to_text(comment.get("body")), args.description_limit)
            print(f"  - author={author} created={created}")
            if body:
                print(textwrap.indent(body, "    "))

    if args.full:
        print(f"changelog_items={len(extra.get('changelog') or [])}")
        print(f"worklogs={len(extra.get('worklogs') or [])}")
    else:
        print(f"full: jira detail {args.issue_key} --profile {profile.name} --full")

    return 0


def command_comments(args: argparse.Namespace, profile: Profile) -> int:
    comments = fetch_comments(profile, args.issue_key)
    normalized = [normalized_comment(comment) for comment in comments]

    if args.json:
        print(json.dumps({"issue_key": args.issue_key, "comments": comments, "normalized": normalized}, indent=2, sort_keys=True))
        return 0

    print(f"Jira comments | profile={profile.name} issue={args.issue_key} count={len(comments)}")
    for comment in comments[: args.limit]:
        item = normalized_comment(comment)
        print(
            "- "
            + " | ".join(
                [
                    f"id={text(item.get('id'))}",
                    f"author={text((item.get('author') or {}).get('displayName'))}",
                    f"created={compact_datetime(item.get('created'))}",
                ]
            )
        )
        body = truncate(item.get("body"), args.body_limit)
        if body and body != "-":
            print(textwrap.indent(body, "  "))
    return 0


def command_attachments(args: argparse.Namespace, profile: Profile) -> int:
    attachments = fetch_issue_attachments(profile, args.issue_key)
    normalized = [normalized_attachment(attachment) for attachment in attachments]

    if args.json:
        print(json.dumps({"issue_key": args.issue_key, "attachments": attachments, "normalized": normalized}, indent=2, sort_keys=True))
        return 0

    print(f"Jira attachments | profile={profile.name} issue={args.issue_key} count={len(attachments)}")
    for attachment in attachments[: args.limit]:
        print(f"- {attachment_line(attachment)}")
    return 0


def command_attachment(args: argparse.Namespace, profile: Profile) -> int:
    attachment_id = urllib.parse.quote(args.id)
    metadata = request(profile, f"rest/api/3/attachment/{attachment_id}")
    output = Path(args.output).expanduser()

    if not args.confirm:
        print(
            "DRY-RUN Jira attachment download | "
            + " | ".join(
                [
                    f"profile={profile.name}",
                    f"id={text(metadata.get('id') or args.id)}",
                    f"file={truncate(metadata.get('filename'), 140)}",
                    f"output={output}",
                    "confirm=pass --confirm to write the file",
                ]
            )
        )
        return 0

    if output.is_symlink() or output.exists():
        raise JiraError(f"Refusing to overwrite existing output path: {output}")
    if not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)

    content = request_bytes(profile, f"rest/api/3/attachment/content/{attachment_id}")
    publish_bytes_exclusive(output, content)
    print(
        "Jira attachment downloaded | "
        + " | ".join(
            [
                f"profile={profile.name}",
                f"id={text(metadata.get('id') or args.id)}",
                f"file={truncate(metadata.get('filename'), 140)}",
                f"bytes={len(content)}",
                f"output={output}",
            ]
        )
    )
    return 0


def publish_bytes_exclusive(output: Path, content: bytes) -> None:
    if output.is_symlink() or output.exists():
        raise JiraError(f"Refusing to overwrite existing output path: {output}")

    temporary_path: Path | None = None
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary_path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, output)
        except FileExistsError as exc:
            raise JiraError(f"Refusing to overwrite existing output path: {output}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def candidate_profiles_for_key(issue_key: str, profiles: list[Profile]) -> list[Profile]:
    project_key = issue_key.split("-", 1)[0]
    matching = [profile for profile in profiles if project_key in profile.projects]
    remaining = [profile for profile in profiles if profile not in matching]
    return matching + remaining


def command_identify(args: argparse.Namespace) -> int:
    issue_keys = sorted(set(ISSUE_KEY_RE.findall(args.text)))
    if not issue_keys:
        print("No Jira issue keys found.")
        return 0

    if args.all_profiles:
        names = configured_profile_names()
        if not names:
            raise JiraError("No Jira profiles configured. Set JIRA_PROFILES in .env.")
        profiles = [get_profile(name) for name in names]
    else:
        profiles = [get_profile(args.profile)]

    print(f"Jira identify | keys={','.join(issue_keys)} profiles={','.join(profile.name for profile in profiles)}")
    for issue_key in issue_keys:
        matches = 0
        for profile in candidate_profiles_for_key(issue_key, profiles):
            try:
                issue = request(
                    profile,
                    f"rest/api/3/issue/{urllib.parse.quote(issue_key)}",
                    params={"fields": DEFAULT_FIELDS},
                )
                print(issue_line(issue, profile))
                matches += 1
                if not args.all_profiles:
                    break
            except JiraError as exc:
                if "404" not in str(exc):
                    print(f"- {issue_key} | profile={profile.name} | error={exc}")
        if not matches:
            print(f"- {issue_key} | not found in searched profiles")

    return 0


def add_common_options(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--env-file",
        default=default,
        help="Path to dotenv file. Defaults to the configured shared or isolated Jira env.",
    )
    parser.add_argument("--profile", default=default, help="Jira profile name from JIRA_<PROFILE>_* env vars.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull compact Jira issue context for workspace triage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              jira profiles
              jira projects --profile example
              jira list --profile example --project APP --limit 10
              jira search --profile example --jql 'project = APP ORDER BY updated DESC' --limit 10
              jira detail APP-252 --profile example --full
              jira comments APP-252 --profile example
              jira attachments APP-252 --profile example
              jira attachment --profile example --id EXAMPLE_ATTACHMENT_ID --output /tmp/example.png --confirm
              jira identify "Fix APP-252" --all-profiles
            """
        ),
    )
    add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List configured Jira profiles.")
    profiles.add_argument("--show-email", action="store_true", help="Show configured account emails in text output.")
    profiles.set_defaults(handler=command_profiles, no_profile=True)

    whoami = subparsers.add_parser("whoami", help="Verify Jira credentials.")
    add_common_options(whoami, suppress_defaults=True)
    whoami.add_argument("--show-email", action="store_true", help="Show account email in text output.")
    whoami.add_argument("--json", action="store_true", help="Print raw JSON.")
    whoami.set_defaults(handler=command_whoami)

    projects = subparsers.add_parser("projects", help="List visible Jira projects.")
    add_common_options(projects, suppress_defaults=True)
    projects.add_argument("--query", help="Filter projects by Jira project search query.")
    projects.add_argument("--limit", type=int, default=50, help="Maximum projects to print.")
    projects.add_argument("--json", action="store_true", help="Print raw JSON.")
    projects.set_defaults(handler=command_projects)

    list_parser = subparsers.add_parser("list", help="List Jira issues as compact CSV-style rows.")
    add_common_options(list_parser, suppress_defaults=True)
    list_parser.add_argument("--project", action="append", help="Project key to search. Repeatable.")
    list_parser.add_argument("--limit", type=int, default=25, help="Maximum issues to print.")
    list_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    list_parser.set_defaults(handler=command_list, jql=None)

    search = subparsers.add_parser("search", help="Search Jira issues with explicit JQL.")
    add_common_options(search, suppress_defaults=True)
    search.add_argument("--jql", required=True, help="JQL query to run.")
    search.add_argument("--limit", type=int, default=25, help="Maximum issues to print.")
    search.add_argument("--json", action="store_true", help="Print raw JSON.")
    search.set_defaults(handler=command_list, project=None)

    detail = subparsers.add_parser("detail", help="Fetch one Jira issue.")
    add_common_options(detail, suppress_defaults=True)
    detail.add_argument("issue_key")
    detail.add_argument("--full", action="store_true", help="Fetch all fields plus comments, changelog, and worklogs.")
    detail.add_argument("--comment-limit", type=int, default=10, help="Maximum comments to print in text output.")
    detail.add_argument("--attachment-limit", type=int, default=10, help="Maximum attachments to print in text output.")
    detail.add_argument("--description-limit", type=int, default=2000, help="Maximum description/comment characters.")
    detail.add_argument("--json", action="store_true", help="Print raw JSON.")
    detail.set_defaults(handler=command_detail)

    comments = subparsers.add_parser("comments", help="Fetch paginated comments for one Jira issue.")
    add_common_options(comments, suppress_defaults=True)
    comments.add_argument("issue_key")
    comments.add_argument("--limit", type=int, default=25, help="Maximum comments to print in text output.")
    comments.add_argument("--body-limit", type=int, default=1200, help="Maximum body characters per comment in text output.")
    comments.add_argument("--json", action="store_true", help="Print raw JSON.")
    comments.set_defaults(handler=command_comments)

    attachments = subparsers.add_parser("attachments", help="List attachment metadata for one Jira issue.")
    add_common_options(attachments, suppress_defaults=True)
    attachments.add_argument("issue_key")
    attachments.add_argument("--limit", type=int, default=25, help="Maximum attachments to print in text output.")
    attachments.add_argument("--json", action="store_true", help="Print raw JSON.")
    attachments.set_defaults(handler=command_attachments)

    attachment = subparsers.add_parser("attachment", help="Download one Jira attachment to an explicit local output path.")
    add_common_options(attachment, suppress_defaults=True)
    attachment.add_argument("--id", required=True, help="Jira attachment id.")
    attachment.add_argument("--output", required=True, help="Local file path to write. Existing files are not overwritten.")
    attachment.add_argument("--confirm", action="store_true", help="Actually download and write the attachment bytes.")
    attachment.set_defaults(handler=command_attachment)

    identify = subparsers.add_parser("identify", help="Find Jira issue keys in text and resolve them to profiles.")
    add_common_options(identify, suppress_defaults=True)
    identify.add_argument("text")
    identify.add_argument("--all-profiles", action="store_true", help="Search every configured Jira profile.")
    identify.set_defaults(handler=command_identify, no_profile=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env_file = resolve_env_file(getattr(args, "env_file", None))
    load_dotenv(env_file)

    if not getattr(args, "no_profile", False):
        args.profile = getattr(args, "profile", None) or os.environ.get("JIRA_DEFAULT_PROFILE", "")
        if not args.profile:
            print("error: Missing Jira profile. Set JIRA_DEFAULT_PROFILE or pass --profile.", file=sys.stderr)
            return 1

    try:
        if getattr(args, "no_profile", False):
            return args.handler(args)
        profile = get_profile(args.profile)
        return args.handler(args, profile)
    except JiraError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
