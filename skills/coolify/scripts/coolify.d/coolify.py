#!/usr/bin/env python3
"""
Coolify resource management for rundesk agents.

Usage:
  coolify profiles
  coolify status [--profile example]
  coolify teams [--profile example]
  coolify servers [--profile example] [--limit 50]
  coolify server SERVER_UUID [--profile example]
  coolify applications [--profile example] [--limit 50]
  coolify application APP_UUID [--profile example]
  coolify services [--profile example] [--limit 50]
  coolify service SERVICE_UUID [--profile example]
  coolify databases [--profile example] [--limit 50]
  coolify database DB_UUID [--profile example]
  coolify projects [--profile example] [--limit 50]
  coolify project PROJECT_UUID [--profile example]
  coolify resources [--profile example] [--limit 50]
  coolify deployments [--uuid APP_UUID] [--profile example] [--limit 25]
  coolify deployment DEPLOY_UUID [--profile example]
  coolify logs application|service|database UUID [--profile example] [--lines 100]
  coolify envs application|service|database UUID [--profile example] [--show-values]
  coolify start application|service|database UUID [--profile example] [--confirm]
  coolify stop application|service|database UUID [--profile example] [--confirm]
  coolify restart application|service|database UUID [--profile example] [--confirm]
  coolify deploy --uuid APP_OR_RESOURCE_UUID [--force] [--profile example] [--confirm]

Inputs:
  Reads dotenv outside the Rundesk script library. Configure COOLIFY_PROFILES
  and COOLIFY_<PROFILE>_* keys; see README. Secrets stay in local .env only.

Outputs:
  Compact text / CSV for agent context. --json for structured payloads.
  Mutations are dry-run by default and require --confirm for the exact action.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SERVER_COLUMNS = [
    "uuid",
    "name",
    "ip",
    "description",
    "is_reachable",
    "is_usable",
    "profile",
]
APP_COLUMNS = [
    "uuid",
    "name",
    "status",
    "fqdn",
    "git_repository",
    "git_branch",
    "server",
    "profile",
]
SERVICE_COLUMNS = [
    "uuid",
    "name",
    "status",
    "fqdn",
    "server",
    "profile",
]
DB_COLUMNS = [
    "uuid",
    "name",
    "type",
    "status",
    "server",
    "profile",
]
PROJECT_COLUMNS = [
    "uuid",
    "name",
    "description",
    "profile",
]
DEPLOY_COLUMNS = [
    "uuid",
    "application_id",
    "status",
    "commit",
    "created_at",
    "profile",
]
RESOURCE_KINDS = ("application", "service", "database")


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("COOLIFY_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "coolify" / "env")
    candidates.append(xdg / "coolify" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


class CoolifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    token: str
    base_url: str
    label: str

    def auth_headers(self) -> dict[str, str]:
        if not self.token:
            raise CoolifyError(
                f"Profile {self.name!r} missing {env_name(self.name, 'TOKEN')}."
            )
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "rundesk-coolify/1.0",
        }


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
        if not key or not value:
            continue
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = value


def env_name(profile: str, suffix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile).strip("_").upper()
    return f"COOLIFY_{normalized}_{suffix}"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("COOLIFY_PROFILES"))
    default = os.environ.get("COOLIFY_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    names: set[str] = set()
    pattern = re.compile(r"^COOLIFY_([A-Z0-9_]+)_(TOKEN|BASE_URL|LABEL)$")
    for key in os.environ:
        match = pattern.match(key)
        if not match:
            continue
        raw = match.group(1)
        if raw in {"DEFAULT", "API"}:
            continue
        names.add(raw.lower().replace("_", "-"))
    if os.environ.get("COOLIFY_TOKEN") or os.environ.get("COOLIFY_API_TOKEN"):
        names.add(os.environ.get("COOLIFY_DEFAULT_PROFILE", "default") or "default")
    return sorted(names)


def validate_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise CoolifyError(f"Invalid Coolify base URL: {value!r}") from exc
    if (
        not value
        or any(character.isspace() or ord(character) < 32 for character in value)
        or parsed.scheme.lower() not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CoolifyError(
            f"Invalid Coolify base URL: {value!r}. Use an origin like https://coolify.example.com"
        )
    # Allow path only if empty or trailing slash; strip /api/v1 if user included it.
    path = (parsed.path or "").rstrip("/")
    if path in ("",):
        origin = f"{parsed.scheme.lower()}://{parsed.hostname}"
        if parsed.port:
            origin += f":{parsed.port}"
        return origin
    if path.endswith("/api/v1"):
        origin = f"{parsed.scheme.lower()}://{parsed.hostname}"
        if parsed.port:
            origin += f":{parsed.port}"
        return origin
    raise CoolifyError(
        f"Invalid Coolify base URL path {path!r}. Use the host origin only "
        "(API path /api/v1 is added automatically)."
    )


def get_profile(name: str) -> Profile:
    token = (
        os.environ.get(env_name(name, "TOKEN"))
        or os.environ.get("COOLIFY_TOKEN")
        or os.environ.get("COOLIFY_API_TOKEN")
        or ""
    )
    base_url = (
        os.environ.get(env_name(name, "BASE_URL"))
        or os.environ.get("COOLIFY_BASE_URL")
        or os.environ.get("COOLIFY_URL")
        or ""
    )
    label = os.environ.get(env_name(name, "LABEL"), name)
    missing = []
    if not token:
        missing.append(env_name(name, "TOKEN"))
    if not base_url:
        missing.append(env_name(name, "BASE_URL"))
    if missing:
        raise CoolifyError(
            "Missing Coolify config: "
            + ", ".join(missing)
            + ". Add to secrets dotenv or export in the shell."
        )
    return Profile(name=name, token=token, base_url=validate_base_url(base_url), label=label)


def selected_profile_name(args: argparse.Namespace) -> str:
    if getattr(args, "profile", None):
        return args.profile
    default = os.environ.get("COOLIFY_DEFAULT_PROFILE", "")
    names = configured_profile_names()
    if default:
        return default
    if len(names) == 1:
        return names[0]
    if not names:
        raise CoolifyError(
            "No Coolify profiles configured. Set COOLIFY_PROFILES and "
            "COOLIFY_<PROFILE>_TOKEN / COOLIFY_<PROFILE>_BASE_URL."
        )
    raise CoolifyError(
        "Multiple Coolify profiles configured; pass --profile. "
        f"Available: {', '.join(names)}"
    )


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    value = str(value).replace("\n", " ").strip()
    return value if value else fallback


def print_csv(columns: list[str], rows: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: text(row.get(column)) for column in columns})


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def print_kv(data: dict[str, Any], keys: list[str] | None = None) -> None:
    if keys is None:
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                continue
            print(f"{key}\t{text(value)}")
        return
    for key in keys:
        print(f"{key}\t{text(data.get(key))}")


def as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "applications", "servers", "services", "databases", "projects", "deployments"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    return []


def as_obj(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
        return data
    return {}


def request(
    profile: Profile,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    base = profile.base_url.rstrip("/") + "/api/v1/"
    url = base + path.lstrip("/")
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
    body = None
    headers = profile.auth_headers()
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw[:2000]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"message": raw[:500]}
        detail = data.get("message") or data.get("error") or data.get("errors") or raw[:500]
        raise CoolifyError(f"Coolify API {exc.code} profile={profile.name}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CoolifyError(
            f"Coolify API request failed profile={profile.name}: {exc.reason}"
        ) from exc


def bound(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit < 1:
        raise CoolifyError("--limit must be >= 1")
    return items[:limit]


def server_name(item: dict[str, Any]) -> str:
    server = item.get("server")
    if isinstance(server, dict):
        return text(server.get("name") or server.get("uuid"))
    return text(item.get("server_id") or item.get("destination_id"))


def app_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "uuid": item.get("uuid"),
        "name": item.get("name") or item.get("human_name"),
        "status": item.get("status"),
        "fqdn": item.get("fqdn"),
        "git_repository": item.get("git_repository"),
        "git_branch": item.get("git_branch"),
        "server": server_name(item),
        "profile": profile.name,
    }


def service_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "uuid": item.get("uuid"),
        "name": item.get("name") or item.get("human_name"),
        "status": item.get("status"),
        "fqdn": item.get("fqdn"),
        "server": server_name(item),
        "profile": profile.name,
    }


def server_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "uuid": item.get("uuid"),
        "name": item.get("name"),
        "ip": item.get("ip"),
        "description": item.get("description"),
        "is_reachable": item.get("is_reachable") if "is_reachable" in item else item.get("settings", {}).get("is_reachable") if isinstance(item.get("settings"), dict) else None,
        "is_usable": item.get("is_usable") if "is_usable" in item else item.get("settings", {}).get("is_usable") if isinstance(item.get("settings"), dict) else None,
        "profile": profile.name,
    }


def db_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "uuid": item.get("uuid"),
        "name": item.get("name") or item.get("human_name"),
        "type": item.get("type") or item.get("database_type"),
        "status": item.get("status"),
        "server": server_name(item),
        "profile": profile.name,
    }


def project_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "uuid": item.get("uuid"),
        "name": item.get("name"),
        "description": item.get("description"),
        "profile": profile.name,
    }


def deploy_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "uuid": item.get("uuid") or item.get("deployment_uuid"),
        "application_id": item.get("application_id") or item.get("application_uuid"),
        "status": item.get("status"),
        "commit": item.get("commit") or item.get("commit_sha") or item.get("git_commit_sha"),
        "created_at": item.get("created_at"),
        "profile": profile.name,
    }


def cmd_profiles(_args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print("No Coolify profiles configured.")
        return 0
    default = os.environ.get("COOLIFY_DEFAULT_PROFILE", "")
    for name in names:
        marker = " (default)" if name == default or (not default and len(names) == 1) else ""
        try:
            profile = get_profile(name)
            print(f"{name}{marker}\t{profile.label}\tbase={profile.base_url}\ttoken=set")
        except CoolifyError as exc:
            print(f"{name}{marker}\tERROR\t{exc}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    print(f"profile\t{profile.name}")
    print(f"label\t{profile.label}")
    print(f"base_url\t{profile.base_url}")
    version = request(profile, "GET", "version")
    health = request(profile, "GET", "health")
    # /version often returns plain text (e.g. 4.1.2), not JSON.
    if isinstance(version, dict):
        print(
            f"version\t{text(version.get('version') or version.get('coolify') or version.get('raw') or version)}"
        )
    else:
        print(f"version\t{text(version)}")
    if isinstance(health, dict):
        print(f"health\t{text(health.get('status') or health.get('message') or health.get('raw') or 'ok')}")
    else:
        print(f"health\t{text(health)}")
    try:
        team = as_obj(request(profile, "GET", "teams/current"))
        print(f"team\t{text(team.get('name') or team.get('id'))}")
        print(f"team_id\t{text(team.get('id') or team.get('uuid'))}")
    except CoolifyError as exc:
        print(f"team\t{exc}")
    print("api\tok")
    return 0


def cmd_teams(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "teams")
    if args.json:
        print_json(data)
        return 0
    rows = as_list(data)
    print_csv(
        ["id", "name", "description", "personal_team", "profile"],
        [
            {
                "id": r.get("id") or r.get("uuid"),
                "name": r.get("name"),
                "description": r.get("description"),
                "personal_team": r.get("personal_team"),
                "profile": profile.name,
            }
            for r in rows
        ],
    )
    return 0


def cmd_servers(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "servers")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    print_csv(SERVER_COLUMNS, [server_row(r, profile) for r in rows])
    return 0


def cmd_server(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = as_obj(request(profile, "GET", f"servers/{args.uuid}"))
    if args.json:
        print_json(data)
        return 0
    print_csv(SERVER_COLUMNS, [server_row(data, profile)])
    return 0


def cmd_applications(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "applications")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    print_csv(APP_COLUMNS, [app_row(r, profile) for r in rows])
    return 0


def cmd_application(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = as_obj(request(profile, "GET", f"applications/{args.uuid}"))
    if args.json:
        print_json(data)
        return 0
    print_csv(APP_COLUMNS, [app_row(data, profile)])
    return 0


def cmd_services(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "services")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    print_csv(SERVICE_COLUMNS, [service_row(r, profile) for r in rows])
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = as_obj(request(profile, "GET", f"services/{args.uuid}"))
    if args.json:
        print_json(data)
        return 0
    print_csv(SERVICE_COLUMNS, [service_row(data, profile)])
    return 0


def cmd_databases(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "databases")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    print_csv(DB_COLUMNS, [db_row(r, profile) for r in rows])
    return 0


def cmd_database(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = as_obj(request(profile, "GET", f"databases/{args.uuid}"))
    if args.json:
        print_json(data)
        return 0
    print_csv(DB_COLUMNS, [db_row(data, profile)])
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "projects")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    print_csv(PROJECT_COLUMNS, [project_row(r, profile) for r in rows])
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = as_obj(request(profile, "GET", f"projects/{args.uuid}"))
    if args.json:
        print_json(data)
        return 0
    print_csv(PROJECT_COLUMNS, [project_row(data, profile)])
    return 0


def cmd_resources(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "resources")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    out = []
    for r in rows:
        out.append(
            {
                "uuid": r.get("uuid"),
                "name": r.get("name") or r.get("human_name"),
                "type": r.get("type") or r.get("resource_type"),
                "status": r.get("status"),
                "profile": profile.name,
            }
        )
    print_csv(["uuid", "name", "type", "status", "profile"], out)
    return 0


def cmd_deployments(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    if args.uuid:
        data = request(profile, "GET", f"deployments/applications/{args.uuid}")
    else:
        data = request(profile, "GET", "deployments")
    rows = bound(as_list(data), args.limit)
    if args.json:
        print_json(rows)
        return 0
    print_csv(DEPLOY_COLUMNS, [deploy_row(r, profile) for r in rows])
    return 0


def cmd_deployment(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = as_obj(request(profile, "GET", f"deployments/{args.uuid}"))
    if args.json:
        print_json(data)
        return 0
    print_csv(DEPLOY_COLUMNS, [deploy_row(data, profile)])
    return 0


def resource_path(kind: str, uuid: str, action: str | None = None) -> str:
    if kind not in RESOURCE_KINDS:
        raise CoolifyError(f"kind must be one of {', '.join(RESOURCE_KINDS)}")
    plural = {
        "application": "applications",
        "service": "services",
        "database": "databases",
    }[kind]
    path = f"{plural}/{uuid}"
    if action:
        path += f"/{action}"
    return path


def cmd_logs(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    path = resource_path(args.kind, args.uuid, "logs")
    data = request(profile, "GET", path)
    if args.json:
        print_json(data)
        return 0
    # Coolify may return string logs or structured payload.
    if isinstance(data, str):
        lines = data.splitlines()
        for line in lines[-args.lines :]:
            print(line)
        return 0
    if isinstance(data, dict):
        logs = data.get("logs") or data.get("message") or data.get("data")
        if isinstance(logs, str):
            for line in logs.splitlines()[-args.lines :]:
                print(line)
            return 0
        if isinstance(logs, list):
            for item in logs[-args.lines :]:
                print(text(item if not isinstance(item, dict) else item.get("message") or item))
            return 0
    print(text(data))
    return 0


def cmd_envs(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    path = resource_path(args.kind, args.uuid, "envs")
    data = request(profile, "GET", path)
    rows = as_list(data)
    if not rows and isinstance(data, dict):
        rows = as_list(data.get("data") or data)
    if args.json and args.show_values:
        print_json(data)
        return 0
    out = []
    for r in rows:
        key = r.get("key") or r.get("name")
        value = r.get("value") or r.get("real_value")
        if args.show_values:
            shown = value
        else:
            shown = "<set>" if value not in (None, "") else "<empty>"
        out.append(
            {
                "uuid": r.get("uuid"),
                "key": key,
                "value": shown,
                "is_preview": r.get("is_preview"),
                "is_literal": r.get("is_literal"),
                "profile": profile.name,
            }
        )
    print_csv(["uuid", "key", "value", "is_preview", "is_literal", "profile"], out)
    return 0


def mutation(
    args: argparse.Namespace,
    *,
    action: str,
    method: str = "POST",
    path: str,
    params: dict[str, Any] | None = None,
) -> int:
    profile = get_profile(selected_profile_name(args))
    print(f"action\t{action}")
    print(f"path\t{path}")
    print(f"method\t{method}")
    print(f"profile\t{profile.name}")
    if params:
        for key, value in params.items():
            print(f"param_{key}\t{value}")
    if not args.confirm:
        print("mode\tdry-run")
        print(
            f"next\tOwner approval required. Re-run with --confirm only after Tim approves "
            f"{action} on profile {profile.name}."
        )
        return 0
    data = request(profile, method, path, params=params)
    print("mode\tconfirmed")
    if args.json:
        print_json(data)
    else:
        if isinstance(data, dict):
            for key in ("message", "status", "uuid", "deployment_uuid"):
                if key in data:
                    print(f"{key}\t{text(data.get(key))}")
            if not any(k in data for k in ("message", "status", "uuid", "deployment_uuid")):
                print(f"result\t{text(json.dumps(data)[:300])}")
        else:
            print(f"result\t{text(data)}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    return mutation(
        args,
        action=f"start {args.kind} {args.uuid}",
        path=resource_path(args.kind, args.uuid, "start"),
    )


def cmd_stop(args: argparse.Namespace) -> int:
    return mutation(
        args,
        action=f"stop {args.kind} {args.uuid}",
        path=resource_path(args.kind, args.uuid, "stop"),
    )


def cmd_restart(args: argparse.Namespace) -> int:
    return mutation(
        args,
        action=f"restart {args.kind} {args.uuid}",
        path=resource_path(args.kind, args.uuid, "restart"),
    )


def cmd_deploy(args: argparse.Namespace) -> int:
    if not args.uuid and not args.tag:
        raise CoolifyError("deploy requires --uuid and/or --tag")
    params: dict[str, Any] = {}
    if args.uuid:
        params["uuid"] = args.uuid
    if args.tag:
        params["tag"] = args.tag
    if args.force:
        params["force"] = True
    return mutation(
        args,
        action=f"deploy uuid={args.uuid or '-'} tag={args.tag or '-'}",
        path="deploy",
        params=params,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coolify",
        description="Coolify resources (mutations dry-run without --confirm).",
    )
    parser.add_argument("--env-file", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, limit: bool = False) -> None:
        p.add_argument("--profile", default=None)
        p.add_argument("--json", action="store_true")
        if limit:
            p.add_argument("--limit", type=int, default=50)

    def add_resource_kind(p: argparse.ArgumentParser) -> None:
        p.add_argument("kind", choices=list(RESOURCE_KINDS))
        p.add_argument("uuid")

    p = sub.add_parser("profiles", help="List configured profiles")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("status", help="Version, health, current team (read-only)")
    add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("teams", help="List teams")
    add_common(p)
    p.set_defaults(func=cmd_teams)

    p = sub.add_parser("servers", help="List servers")
    add_common(p, limit=True)
    p.set_defaults(func=cmd_servers)

    p = sub.add_parser("server", help="Show one server")
    add_common(p)
    p.add_argument("uuid")
    p.set_defaults(func=cmd_server)

    p = sub.add_parser("applications", help="List applications")
    add_common(p, limit=True)
    p.set_defaults(func=cmd_applications)

    p = sub.add_parser("application", help="Show one application")
    add_common(p)
    p.add_argument("uuid")
    p.set_defaults(func=cmd_application)

    p = sub.add_parser("services", help="List one-click services")
    add_common(p, limit=True)
    p.set_defaults(func=cmd_services)

    p = sub.add_parser("service", help="Show one service")
    add_common(p)
    p.add_argument("uuid")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("databases", help="List databases")
    add_common(p, limit=True)
    p.set_defaults(func=cmd_databases)

    p = sub.add_parser("database", help="Show one database")
    add_common(p)
    p.add_argument("uuid")
    p.set_defaults(func=cmd_database)

    p = sub.add_parser("projects", help="List projects")
    add_common(p, limit=True)
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("project", help="Show one project")
    add_common(p)
    p.add_argument("uuid")
    p.set_defaults(func=cmd_project)

    p = sub.add_parser("resources", help="List all resources")
    add_common(p, limit=True)
    p.set_defaults(func=cmd_resources)

    p = sub.add_parser("deployments", help="List deployments")
    add_common(p, limit=True)
    p.add_argument("--uuid", default=None, help="Filter by application UUID")
    p.set_defaults(func=cmd_deployments)

    p = sub.add_parser("deployment", help="Show one deployment")
    add_common(p)
    p.add_argument("uuid")
    p.set_defaults(func=cmd_deployment)

    p = sub.add_parser("logs", help="Fetch recent logs for a resource")
    add_common(p)
    add_resource_kind(p)
    p.add_argument("--lines", type=int, default=100)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("envs", help="List env vars (values redacted unless --show-values)")
    add_common(p)
    add_resource_kind(p)
    p.add_argument(
        "--show-values",
        action="store_true",
        help="Include secret values (avoid pasting into chat)",
    )
    p.set_defaults(func=cmd_envs)

    for name, func, help_text in (
        ("start", cmd_start, "Start a resource (dry-run unless --confirm)"),
        ("stop", cmd_stop, "Stop a resource (dry-run unless --confirm)"),
        ("restart", cmd_restart, "Restart a resource (dry-run unless --confirm)"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_common(p)
        add_resource_kind(p)
        p.add_argument("--confirm", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("deploy", help="Deploy by uuid/tag (dry-run unless --confirm)")
    add_common(p)
    p.add_argument("--uuid", default=None, help="Resource UUID(s), comma-separated")
    p.add_argument("--tag", default=None, help="Tag name(s), comma-separated")
    p.add_argument("--force", action="store_true", help="Force rebuild without cache")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_deploy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(resolve_env_file(getattr(args, "env_file", None)))
    try:
        if getattr(args, "limit", None) is not None and (args.limit < 1 or args.limit > 500):
            raise CoolifyError("--limit must be between 1 and 500")
        if getattr(args, "lines", None) is not None and (args.lines < 1 or args.lines > 5000):
            raise CoolifyError("--lines must be between 1 and 5000")
        return int(args.func(args))
    except CoolifyError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
