#!/usr/bin/env python3
"""Search Grafana Loki logs through a read-only Grafana data-source proxy."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


PROFILE_FIELDS = {
    "GRAFANA_BASE_URL": "BASE_URL",
    "GRAFANA_SERVICE_ACCOUNT_TOKEN": "TOKEN",
    "GRAFANA_LOKI_UID": "LOKI_UID",
    "GRAFANA_LABEL": "LABEL",
}
REQUIRED_FIELDS = ("GRAFANA_BASE_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN")
ACCOUNT_SUFFIX_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")
RESERVED_PROFILE_WORDS = frozenset({"DEFAULT", "ENV", "PROFILES"})
LABEL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SELECTOR_RE = re.compile(r"\{[^{}\r\n]{0,1000}\}")
EXACT_MATCHER_RE = re.compile(
    r'(?:^|,)\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*"(?:[^"\\]|\\.)+"\s*(?=,|$)'
)
DURATION_RE = re.compile(r"([1-9][0-9]*)([smhd])")
MAX_RANGE_SECONDS = 30 * 24 * 60 * 60
MAX_LIMIT = 1000
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
ALLOWED_PATHS = (
    "/api/datasources",
    "/api/datasources/proxy/uid/",
)
ERROR_PATTERN = "(?i)(error|exception|fatal|panic|failed)"
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+"),
)


class GrafanaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    token: str
    loki_uid: str
    label: str


def default_env_candidates() -> list[Path]:
    candidates: list[Path] = []
    for key in ("GRAFANA_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "grafana" / "env")
    candidates.append(xdg / "grafana" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        print(f"WARNING: dotenv file {path} is accessible by group or others (mode {mode:04o}); "
              "restrict it with chmod 600.", file=sys.stderr)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_profile(profile: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", profile or "").strip("_").upper()


def profile_label(suffix: str) -> str:
    return suffix.lower().replace("_", "-")


def split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def is_default_profile(profile: str) -> bool:
    normalized = normalize_profile(profile)
    if not normalized or normalized == "DEFAULT":
        return True
    return normalized == normalize_profile(os.environ.get("GRAFANA_DEFAULT_PROFILE", ""))


def profile_value(profile: str, field: str) -> str:
    normalized = normalize_profile(profile)
    candidates: list[str] = []
    if normalized:
        candidates.extend((f"{field}__{normalized}",
                           f"GRAFANA_{normalized}_{PROFILE_FIELDS[field]}"))
    if is_default_profile(profile):
        candidates.append(field)
    for name in candidates:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def discovered_profile_names() -> list[str]:
    suffixed: set[str] = set()
    infixed: set[str] = set()
    legacy = re.compile(
        rf"^GRAFANA_({ACCOUNT_SUFFIX_RE.pattern})_({'|'.join(PROFILE_FIELDS.values())})$"
    )
    for key in os.environ:
        for field in PROFILE_FIELDS:
            prefix = f"{field}__"
            if key.startswith(prefix) and ACCOUNT_SUFFIX_RE.fullmatch(key[len(prefix):]):
                suffixed.add(profile_label(key[len(prefix):]))
        if key in PROFILE_FIELDS:
            continue
        match = legacy.match(key)
        if not match:
            continue
        word = match.group(1)
        if word == "DEFAULT":
            infixed.add("default")
        elif word not in RESERVED_PROFILE_WORDS:
            infixed.add(profile_label(word))
    names = suffixed | infixed
    if not infixed and any(profile_value("", field) for field in REQUIRED_FIELDS):
        names.add(os.environ.get("GRAFANA_DEFAULT_PROFILE") or "default")
    return sorted(names)


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("GRAFANA_PROFILES"))
    default = os.environ.get("GRAFANA_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def selected_profile_name(args: argparse.Namespace | SimpleNamespace) -> str:
    requested = getattr(args, "profile", None)
    if requested:
        return requested
    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if not names:
        return "default"
    raise GrafanaError("Multiple Grafana profiles are configured; choose --profile from: "
                       + ", ".join(names))


def validate_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise GrafanaError("Grafana base URL must be an HTTPS origin without credentials, path, "
                           "query, or fragment.")
    return value.rstrip("/")


def get_profile(name: str) -> Profile:
    missing = [field for field in REQUIRED_FIELDS if not profile_value(name, field)]
    if missing:
        suffix = "" if is_default_profile(name) else f"__{normalize_profile(name)}"
        raise GrafanaError("Missing " + ", ".join(field + suffix for field in missing)
                           + ". Run rundesk skills configure "
                             "rundesk-skills-integrations/grafana for this account.")
    return Profile(
        name=name,
        base_url=validate_origin(profile_value(name, "GRAFANA_BASE_URL")),
        token=profile_value(name, "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
        loki_uid=profile_value(name, "GRAFANA_LOKI_UID"),
        label=profile_value(name, "GRAFANA_LABEL") or name,
    )


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (original.scheme, original.hostname, original.port) != (
                target.scheme, target.hostname, target.port):
            raise GrafanaError("Grafana API refused an unexpected cross-origin redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def allowed_api_path(path: str) -> bool:
    """Whether a route belongs to this command's fixed read-only surface."""
    return path == ALLOWED_PATHS[0] or path.startswith(ALLOWED_PATHS[1])


def api_get(profile: Profile, path: str, params: dict[str, Any] | None = None) -> Any:
    if not allowed_api_path(path):
        raise GrafanaError("Grafana API path is not allowed by this read-only command.")
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items()
                                    if value not in (None, "")})
    url = profile.base_url + path + ("?" + query if query else "")
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {profile.token}",
                 "Accept": "application/json", "User-Agent": "rundesk-grafana/1"},
        method="GET",
    )
    try:
        with urllib.request.build_opener(SameOriginRedirectHandler()).open(
                request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise GrafanaError("Grafana API response exceeded the 10 MiB safety limit.")
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GrafanaError(f"Grafana API HTTP error {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GrafanaError(f"Grafana API read failed ({type(exc).__name__}).") from None


def bounded_limit(value: int) -> int:
    if value < 1 or value > MAX_LIMIT:
        raise GrafanaError(f"--limit must be between 1 and {MAX_LIMIT}.")
    return value


def parse_duration(value: str) -> int:
    match = DURATION_RE.fullmatch(value)
    if not match:
        raise GrafanaError("--since must be a positive duration ending in s, m, h, or d.")
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if seconds > MAX_RANGE_SECONDS:
        raise GrafanaError("Log searches are limited to 30 days per request.")
    return seconds


def parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GrafanaError("--start and --end must be RFC 3339 timestamps.") from None
    if parsed.tzinfo is None:
        raise GrafanaError("--start and --end must include a timezone.")
    return parsed.astimezone(dt.timezone.utc)


def time_bounds(args: argparse.Namespace, now: dt.datetime | None = None) -> tuple[int, int]:
    now = now or dt.datetime.now(dt.timezone.utc)
    if args.since and (args.start or args.end):
        raise GrafanaError("Use --since or --start/--end, not both.")
    if args.start or args.end:
        if not args.start or not args.end:
            raise GrafanaError("Explicit ranges require both --start and --end.")
        start, end = parse_time(args.start), parse_time(args.end)
    else:
        end = now
        start = end - dt.timedelta(seconds=parse_duration(args.since or "1h"))
    if start >= end:
        raise GrafanaError("Log range start must be before its end.")
    if (end - start).total_seconds() > MAX_RANGE_SECONDS:
        raise GrafanaError("Log searches are limited to 30 days per request.")
    return int(start.timestamp() * 1_000_000_000), int(end.timestamp() * 1_000_000_000)


def selected_datasource(profile: Profile, requested: str | None) -> str:
    uid = requested or profile.loki_uid
    if not uid or len(uid) > 128 or not re.fullmatch(r"[A-Za-z0-9._~-]+", uid):
        raise GrafanaError(
            "Choose a valid Loki UID with --datasource or configure GRAFANA_LOKI_UID."
        )
    return uid


def loki_path(uid: str, suffix: str) -> str:
    return f"/api/datasources/proxy/uid/{urllib.parse.quote(uid, safe='')}/loki/api/v1/{suffix}"


def loki_result(profile: Profile, uid: str, suffix: str,
                params: dict[str, Any]) -> Any:
    payload = api_get(profile, loki_path(uid, suffix), params)
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise GrafanaError("Grafana Loki returned an unsuccessful response.")
    data = payload.get("data")
    if data is None:
        raise GrafanaError("Grafana Loki returned no data.")
    return data


def json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def filtered_query(selector: str, contains: list[str], excludes: list[str],
                   regexps: list[str], errors: bool = False) -> str:
    require_bounded_selector(selector)
    query = selector
    for value in contains:
        if not value or len(value) > 500 or "\n" in value:
            raise GrafanaError("--contains values must be 1-500 characters on one line.")
        query += " |= " + json_string(value)
    for value in excludes:
        if not value or len(value) > 500 or "\n" in value:
            raise GrafanaError("--exclude values must be 1-500 characters on one line.")
        query += " != " + json_string(value)
    for value in ([ERROR_PATTERN] if errors else []) + regexps:
        if not value or len(value) > 500 or "\n" in value:
            raise GrafanaError("--regexp values must be 1-500 characters on one line.")
        query += " |~ " + json_string(value)
    return query


def require_bounded_selector(selector: str) -> None:
    """Require an exact, non-empty label match to keep every log read deliberately scoped."""
    if not SELECTOR_RE.fullmatch(selector):
        raise GrafanaError("LogQL must begin with one stream selector in braces.")
    if not EXACT_MATCHER_RE.search(selector[1:-1]):
        raise GrafanaError(
            "LogQL selector must include an exact non-empty label match such as {service=\"api\"}."
        )


def selector_from_query(query: str) -> str:
    match = re.match(r"\s*(\{[^{}\r\n]{0,1000}\})", query)
    if not match:
        raise GrafanaError("LogQL query must begin with a bounded stream selector.")
    return match.group(1)


def compact_line(value: str, limit: int = 1000) -> str:
    text = " ".join(str(value or "").split())
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def flattened_streams(data: Any) -> list[dict[str, Any]]:
    streams = data.get("result") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        raise GrafanaError("Grafana Loki returned an invalid stream response.")
    rows: list[dict[str, Any]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream") if isinstance(stream.get("stream"), dict) else {}
        values = stream.get("values") if isinstance(stream.get("values"), list) else []
        for value in values:
            if isinstance(value, list) and len(value) >= 2:
                rows.append({"timestamp_ns": str(value[0]), "labels": labels,
                             "line": str(value[1])})
    try:
        rows.sort(key=lambda row: int(row["timestamp_ns"]), reverse=True)
    except ValueError:
        raise GrafanaError("Grafana Loki returned a non-numeric log timestamp.") from None
    return rows


def timestamp_text(value: str) -> str:
    try:
        stamp = dt.datetime.fromtimestamp(int(value) / 1_000_000_000, tz=dt.timezone.utc)
    except (ValueError, OverflowError):
        return value
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def print_profiles(as_json: bool) -> None:
    rows = [{"profile": name,
             "configured": all(profile_value(name, field) for field in REQUIRED_FIELDS)}
            for name in configured_profile_names()]
    if as_json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No Grafana profiles configured.")
    else:
        for row in rows:
            print(f"{row['profile']} | configured={'yes' if row['configured'] else 'no'}")


def datasource_rows(profile: Profile, limit: int) -> tuple[list[dict[str, Any]], bool]:
    payload = api_get(profile, "/api/datasources")
    if not isinstance(payload, list):
        raise GrafanaError("Grafana API returned an invalid data-source response.")
    loki = [item for item in payload if isinstance(item, dict) and item.get("type") == "loki"]
    return loki[:limit], len(loki) > limit


def verify_loki_datasource(profile: Profile, uid: str) -> None:
    rows, more = datasource_rows(profile, MAX_LIMIT)
    if any(str(row.get("uid") or "") == uid for row in rows):
        return
    if more:
        raise GrafanaError("Loki data-source verification was truncated; choose a narrower account.")
    raise GrafanaError(f"Grafana data source {uid} is not a visible Loki source for this profile.")


def print_datasources(profile: Profile, args: argparse.Namespace) -> None:
    rows, more = datasource_rows(profile, bounded_limit(args.limit))
    if more:
        print(f"WARNING: Grafana Loki data sources were truncated at --limit {args.limit}.",
              file=sys.stderr)
    normalized = [{"uid": str(row.get("uid") or ""), "name": str(row.get("name") or ""),
                   "default": bool(row.get("isDefault")), "read_only": bool(row.get("readOnly"))}
                  for row in rows]
    if args.json:
        print(json.dumps({"profile": profile.name, "count": len(normalized),
                          "limit": args.limit, "has_more": more,
                          "datasources": normalized}, indent=2))
        return
    print(f"Grafana Loki data sources | profile={profile.name} count={len(normalized)} "
          f"limit={args.limit} more={'yes' if more else 'no'}")
    for row in normalized:
        print(f"uid={row['uid']} name={row['name']} default={'yes' if row['default'] else 'no'} "
              f"read_only={'yes' if row['read_only'] else 'no'}")


def print_status(profile: Profile, args: argparse.Namespace) -> None:
    rows, more = datasource_rows(profile, MAX_LIMIT)
    result = {"profile": profile.name, "label": profile.label, "authenticated": True,
              "loki_datasources": len(rows), "configured_loki_uid": profile.loki_uid,
              "truncated": more}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Grafana status | profile={profile.name} authenticated=yes "
              f"loki_datasources={len(rows)} configured_uid={profile.loki_uid or '-'}")


def print_names(profile: Profile, args: argparse.Namespace, label: str | None = None) -> None:
    uid = selected_datasource(profile, args.datasource)
    verify_loki_datasource(profile, uid)
    start, end = time_bounds(args)
    suffix = "labels" if label is None else f"label/{urllib.parse.quote(label, safe='')}/values"
    if label is not None and not LABEL_RE.fullmatch(label):
        raise GrafanaError("Label must be a valid Loki label name.")
    values = loki_result(profile, uid, suffix, {"start": start, "end": end})
    if not isinstance(values, list):
        raise GrafanaError("Grafana Loki returned an invalid label response.")
    limit = bounded_limit(args.limit)
    rows, more = [str(value) for value in values[:limit]], len(values) > limit
    if more:
        print(f"WARNING: Grafana Loki values were truncated at --limit {limit}.", file=sys.stderr)
    kind = "labels" if label is None else f"values for {label}"
    if args.json:
        print(json.dumps({"profile": profile.name, "datasource": uid, "kind": kind,
                          "count": len(rows), "limit": limit, "has_more": more,
                          "values": rows}, indent=2))
    else:
        print(f"Grafana Loki {kind} | profile={profile.name} datasource={uid} "
              f"count={len(rows)} limit={limit} more={'yes' if more else 'no'}")
        for value in rows:
            print(value)


def print_logs(profile: Profile, args: argparse.Namespace, query: str) -> None:
    has_control = any(ord(char) < 32 and char not in "\t" for char in query)
    if not query.strip() or len(query) > 4000 or has_control:
        raise GrafanaError("LogQL query must be 1-4000 characters without control characters.")
    require_bounded_selector(selector_from_query(query))
    uid = selected_datasource(profile, args.datasource)
    verify_loki_datasource(profile, uid)
    limit = bounded_limit(args.limit)
    start, end = time_bounds(args)
    data = loki_result(profile, uid, "query_range", {
        "query": query, "start": start, "end": end, "limit": limit,
        "direction": args.direction,
    })
    rows = flattened_streams(data)
    if len(rows) >= limit:
        print(f"WARNING: Grafana Loki returned --limit {limit}; more log lines may exist.",
              file=sys.stderr)
    if args.json:
        print(json.dumps({"profile": profile.name, "datasource": uid, "query": query,
                          "start_ns": start, "end_ns": end, "limit": limit,
                          "direction": args.direction, "data": data}, indent=2))
        return
    print(f"Grafana Loki logs | profile={profile.name} datasource={uid} count={len(rows)} "
          f"limit={limit} direction={args.direction}")
    for row in rows:
        labels = ",".join(f"{key}={row['labels'][key]}" for key in sorted(row["labels"]))
        print(f"{timestamp_text(row['timestamp_ns'])} | {labels or '-'} | "
              f"{compact_line(row['line'])}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Read Grafana Loki logs through Grafana's API.")
    root.add_argument("--env-file", help="explicit dotenv path")
    sub = root.add_subparsers(dest="command", required=True)
    profiles = sub.add_parser("profiles", help="list configured accounts without a network call")
    profiles.add_argument("--json", action="store_true")

    def common(command: argparse.ArgumentParser, *, datasource: bool = False,
               ranged: bool = False) -> None:
        command.add_argument("--profile")
        command.add_argument("--json", action="store_true")
        if datasource:
            command.add_argument("--datasource", help="Grafana Loki data-source UID")
        if ranged:
            command.add_argument("--since")
            command.add_argument("--start")
            command.add_argument("--end")

    status = sub.add_parser("status", help="verify access and count visible Loki sources")
    common(status)
    datasources = sub.add_parser("datasources", help="list visible Loki data sources")
    common(datasources)
    datasources.add_argument("--limit", type=int, default=20)
    labels = sub.add_parser("labels", help="list Loki label names")
    common(labels, datasource=True, ranged=True)
    labels.add_argument("--limit", type=int, default=100)
    values = sub.add_parser("values", help="list values for one Loki label")
    values.add_argument("label")
    common(values, datasource=True, ranged=True)
    values.add_argument("--limit", type=int, default=50)

    def log_options(command: argparse.ArgumentParser) -> None:
        common(command, datasource=True, ranged=True)
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--direction", choices=("backward", "forward"), default="backward")

    for name, help_text in (("logs", "search logs with a selector and text filters"),
                            ("errors", "search common error terms within one selector")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--selector", required=True)
        command.add_argument("--contains", action="append", default=[])
        command.add_argument("--exclude", action="append", default=[])
        command.add_argument("--regexp", action="append", default=[])
        log_options(command)
    query = sub.add_parser("query", help="run an explicit bounded LogQL range query")
    query.add_argument("logql")
    log_options(query)
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    load_dotenv(resolve_env_file(args.env_file))
    try:
        if args.command == "profiles":
            print_profiles(args.json)
            return 0
        profile = get_profile(selected_profile_name(args))
        if args.command == "status":
            print_status(profile, args)
        elif args.command == "datasources":
            print_datasources(profile, args)
        elif args.command == "labels":
            print_names(profile, args)
        elif args.command == "values":
            print_names(profile, args, args.label)
        elif args.command in ("logs", "errors"):
            query = filtered_query(args.selector, args.contains, args.exclude, args.regexp,
                                   errors=args.command == "errors")
            print_logs(profile, args, query)
        elif args.command == "query":
            print_logs(profile, args, args.logql)
        else:
            root.error("unknown command")
        return 0
    except GrafanaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
