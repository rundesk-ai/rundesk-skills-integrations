#!/usr/bin/env python3
"""
Cloudflare zones, registrar domains, and DNS for workspace agents.

Usage:
  cloudflare profiles
  cloudflare status [--profile example]
  cloudflare accounts [--profile example] [--limit 25]
  cloudflare zones [--profile example] [--name example.com] [--limit 25]
  cloudflare zone example.com [--profile example]
  cloudflare domains [--profile example] [--limit 25]
  cloudflare check example.com [--profile example]
  cloudflare available example.com other.com [--profile example]
  cloudflare search "coffee shop" [--limit 10] [--profile example]
  cloudflare register example.com [--years 1] [--profile example] [--confirm]
  cloudflare dns example.com [--type A] [--name www] [--limit 50] [--profile example]
  cloudflare dns-add example.com --type A --name www --content 1.2.3.4 [--ttl 1] [--proxied] [--confirm]
  cloudflare dns-set example.com RECORD_ID [--type A] [--name www] [--content 1.2.3.4] [--ttl 1] [--proxied true|false] [--confirm]
  cloudflare dns-rm example.com RECORD_ID [--profile example] [--confirm]

Inputs:
  Reads dotenv outside the Rundesk script library. Configure CLOUDFLARE_PROFILES
  and CLOUDFLARE_<PROFILE>_* keys; see README. Secrets stay in local .env only.

Outputs:
  Compact text for agent context. List commands are CSV-style rows.
  Mutations are dry-run by default and require --confirm for the exact action.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


API_BASE = "https://api.cloudflare.com/client/v4"
ZONE_COLUMNS = [
    "id",
    "name",
    "status",
    "plan",
    "paused",
    "name_servers",
    "account",
    "profile",
]
DNS_COLUMNS = [
    "id",
    "type",
    "name",
    "content",
    "ttl",
    "proxied",
    "proxiable",
    "locked",
    "zone",
    "profile",
]
DOMAIN_COLUMNS = [
    "name",
    "current_registrar",
    "expires_at",
    "locked",
    "auto_renew",
    "privacy",
    "can_register",
    "available",
    "profile",
]
AVAILABLE_COLUMNS = [
    "name",
    "registrable",
    "reason",
    "tier",
    "currency",
    "registration_cost",
    "renewal_cost",
    "profile",
]
SEARCH_COLUMNS = [
    "name",
    "registrable",
    "tier",
    "currency",
    "registration_cost",
    "renewal_cost",
    "profile",
]


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("CLOUDFLARE_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "cloudflare" / "env")
    candidates.append(xdg / "cloudflare" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


DEFAULT_ENV = resolve_env_file()


class CloudflareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    token: str
    email: str
    global_key: str
    account_id: str
    label: str

    def has_bearer(self) -> bool:
        return bool(self.token)

    def has_global_key(self) -> bool:
        return bool(self.email and self.global_key)

    def auth_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "workspace-cloudflare/1.0",
        }
        if self.has_bearer():
            headers["Authorization"] = f"Bearer {self.token}"
            return headers
        if self.has_global_key():
            headers["X-Auth-Email"] = self.email
            headers["X-Auth-Key"] = self.global_key
            return headers
        raise CloudflareError(
            f"Profile {self.name!r} has no credentials. Set "
            f"{env_name(self.name, 'TOKEN')} or both "
            f"{env_name(self.name, 'EMAIL')} and {env_name(self.name, 'GLOBAL_KEY')}."
        )


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
        # Skip empties so a later filled assignment (or another env file) can win.
        if not key or not value:
            continue
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = value


def env_name(profile: str, suffix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile).strip("_").upper()
    return f"CLOUDFLARE_{normalized}_{suffix}"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("CLOUDFLARE_PROFILES"))
    default = os.environ.get("CLOUDFLARE_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    names: set[str] = set()
    pattern = re.compile(
        r"^CLOUDFLARE_([A-Z0-9_]+)_(TOKEN|EMAIL|GLOBAL_KEY|ACCOUNT_ID|LABEL)$"
    )
    for key in os.environ:
        match = pattern.match(key)
        if not match:
            continue
        raw = match.group(1)
        if raw in {"API", "DEFAULT"}:
            continue
        names.add(raw.lower().replace("_", "-"))
    # Legacy single-token shape
    if os.environ.get("CLOUDFLARE_API_TOKEN") and not names:
        names.add(os.environ.get("CLOUDFLARE_DEFAULT_PROFILE", "default") or "default")
    return sorted(names)


def get_profile(name: str) -> Profile:
    token = (
        os.environ.get(env_name(name, "TOKEN"))
        or os.environ.get("CLOUDFLARE_API_TOKEN", "")
        or os.environ.get("CF_API_TOKEN", "")
    )
    email = os.environ.get(env_name(name, "EMAIL"), "") or os.environ.get(
        "CLOUDFLARE_EMAIL", ""
    )
    global_key = (
        os.environ.get(env_name(name, "GLOBAL_KEY"))
        or os.environ.get("CLOUDFLARE_GLOBAL_KEY", "")
        or os.environ.get("CLOUDFLARE_API_KEY", "")
    )
    account_id = os.environ.get(env_name(name, "ACCOUNT_ID"), "") or os.environ.get(
        "CLOUDFLARE_ACCOUNT_ID", ""
    )
    label = os.environ.get(env_name(name, "LABEL"), name)

    if not token and not (email and global_key):
        raise CloudflareError(
            "Missing Cloudflare config: "
            f"{env_name(name, 'TOKEN')} (preferred) or "
            f"{env_name(name, 'EMAIL')}+{env_name(name, 'GLOBAL_KEY')}. "
            "Add to the secrets dotenv or export in the shell."
        )

    return Profile(
        name=name,
        token=token,
        email=email,
        global_key=global_key,
        account_id=account_id,
        label=label,
    )


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    value = str(value).replace("\n", " ").strip()
    return value if value else fallback


def compact_date(value: Any) -> str:
    value = text(value)
    if value == "-":
        return value
    match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", value)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value.replace("T", " ").replace("Z", "")


def print_csv(columns: list[str], rows: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: text(row.get(column)) for column in columns})


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not domain or " " in domain or "/" in domain:
        raise CloudflareError(f"Invalid domain name: {value!r}")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", domain):
        raise CloudflareError(f"Invalid domain name: {value!r}")
    return domain


def request(
    profile: Profile,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    retries: int = 2,
) -> Any:
    url = API_BASE.rstrip("/") + "/" + path.lstrip("/")
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

    body = None
    headers = profile.auth_headers()
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if method == "GET" and exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(delay, 30))
                continue
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            errors = data.get("errors") if isinstance(data, dict) else None
            detail = errors or data.get("messages") if isinstance(data, dict) else raw[:500]
            raise CloudflareError(
                f"Cloudflare API {exc.code} profile={profile.name}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CloudflareError(
                f"Cloudflare API request failed profile={profile.name}: {exc.reason}"
            ) from exc

        if not isinstance(data, dict):
            raise CloudflareError(f"Unexpected Cloudflare response profile={profile.name}")
        if data.get("success") is False:
            raise CloudflareError(
                f"Cloudflare API error profile={profile.name}: {data.get('errors') or data.get('messages')}"
            )
        return data

    raise CloudflareError(f"Cloudflare API request exhausted retries profile={profile.name}")


def result_list(data: Any) -> list[dict[str, Any]]:
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def result_obj(data: Any) -> dict[str, Any]:
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, dict) else {}


def paginate(
    profile: Profile,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    limit: int,
    per_page: int = 50,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    page = 1
    params = dict(params or {})
    while len(collected) < limit:
        page_size = min(per_page, limit - len(collected))
        params.update({"page": page, "per_page": page_size})
        data = request(profile, "GET", path, params=params)
        batch = result_list(data)
        collected.extend(batch)
        info = data.get("result_info") if isinstance(data, dict) else None
        total_pages = None
        if isinstance(info, dict):
            total_pages = info.get("total_pages")
            if total_pages is None and info.get("total_count") is not None:
                total = int(info["total_count"])
                per = int(info.get("per_page") or page_size) or page_size
                total_pages = max(1, (total + per - 1) // per)
        if not batch:
            break
        if total_pages is not None and page >= int(total_pages):
            break
        if len(batch) < page_size:
            break
        page += 1
    return collected[:limit]


def selected_profile_name(args: argparse.Namespace) -> str:
    if getattr(args, "profile", None):
        return args.profile
    default = os.environ.get("CLOUDFLARE_DEFAULT_PROFILE", "")
    names = configured_profile_names()
    if default:
        return default
    if len(names) == 1:
        return names[0]
    if not names:
        raise CloudflareError(
            "No Cloudflare profiles configured. Set CLOUDFLARE_PROFILES and "
            "CLOUDFLARE_<PROFILE>_TOKEN in local .env."
        )
    raise CloudflareError(
        "Multiple Cloudflare profiles configured; pass --profile. "
        f"Available: {', '.join(names)}"
    )


def resolve_account_id(profile: Profile) -> str:
    if profile.account_id:
        return profile.account_id
    accounts = paginate(profile, "accounts", limit=5)
    if len(accounts) == 1:
        return text(accounts[0].get("id"), "")
    if not accounts:
        raise CloudflareError(
            f"No Cloudflare accounts visible for profile={profile.name}. "
            f"Set {env_name(profile.name, 'ACCOUNT_ID')}."
        )
    raise CloudflareError(
        f"Multiple Cloudflare accounts for profile={profile.name}; set "
        f"{env_name(profile.name, 'ACCOUNT_ID')}. Visible: "
        + ", ".join(f"{text(a.get('name'))}:{text(a.get('id'))}" for a in accounts[:10])
    )


def zone_row(zone: dict[str, Any], profile: Profile) -> dict[str, Any]:
    plan = zone.get("plan") if isinstance(zone.get("plan"), dict) else {}
    account = zone.get("account") if isinstance(zone.get("account"), dict) else {}
    ns = zone.get("name_servers") or []
    if isinstance(ns, list):
        ns_text = " ".join(text(item, "") for item in ns if item)
    else:
        ns_text = text(ns)
    return {
        "id": zone.get("id"),
        "name": zone.get("name"),
        "status": zone.get("status"),
        "plan": plan.get("name") or plan.get("legacy_id"),
        "paused": zone.get("paused"),
        "name_servers": ns_text,
        "account": account.get("name") or account.get("id"),
        "profile": profile.name,
    }


def dns_row(record: dict[str, Any], zone_name: str, profile: Profile) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "type": record.get("type"),
        "name": record.get("name"),
        "content": record.get("content"),
        "ttl": record.get("ttl"),
        "proxied": record.get("proxied"),
        "proxiable": record.get("proxiable"),
        "locked": record.get("locked"),
        "zone": zone_name,
        "profile": profile.name,
    }


def domain_row(domain: dict[str, Any], profile: Profile) -> dict[str, Any]:
    name = domain.get("name") or domain.get("id")
    return {
        "name": name,
        "current_registrar": domain.get("current_registrar"),
        "expires_at": compact_date(domain.get("expires_at")),
        "locked": domain.get("locked"),
        "auto_renew": domain.get("auto_renew"),
        "privacy": domain.get("privacy"),
        "can_register": domain.get("can_register"),
        "available": domain.get("available"),
        "profile": profile.name,
    }


def find_zone(profile: Profile, domain: str) -> dict[str, Any] | None:
    domain = normalize_domain(domain)
    zones = paginate(profile, "zones", params={"name": domain}, limit=5)
    exact = [z for z in zones if text(z.get("name"), "").lower() == domain]
    if exact:
        return exact[0]
    # Walk parents for records on apex zones (www.example.com -> example.com)
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        zones = paginate(profile, "zones", params={"name": parent}, limit=5)
        exact = [z for z in zones if text(z.get("name"), "").lower() == parent]
        if exact:
            return exact[0]
    return None


def require_zone(profile: Profile, domain: str) -> dict[str, Any]:
    zone = find_zone(profile, domain)
    if not zone:
        raise CloudflareError(
            f"No Cloudflare zone for {domain!r} on profile={profile.name}. "
            "Run `cloudflare zones` to list owned zones."
        )
    return zone


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise CloudflareError(f"Invalid boolean: {value!r} (use true/false)")


def cmd_profiles(_args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print("No Cloudflare profiles configured.")
        return 0
    default = os.environ.get("CLOUDFLARE_DEFAULT_PROFILE", "")
    for name in names:
        marker = " (default)" if name == default or (not default and len(names) == 1) else ""
        try:
            profile = get_profile(name)
            auth = "token" if profile.has_bearer() else "global-key"
            account = profile.account_id or "-"
            print(f"{name}{marker}\t{profile.label}\tauth={auth}\taccount={account}")
        except CloudflareError as exc:
            print(f"{name}{marker}\tERROR\t{exc}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    print(f"profile\t{profile.name}")
    print(f"label\t{profile.label}")
    if profile.has_bearer():
        print("auth\ttoken")
        # User API tokens support /user/tokens/verify; account-owned tokens often 401 there
        # but still work on zones/accounts — prove auth with a cheap accounts read too.
        try:
            data = request(profile, "GET", "user/tokens/verify")
            status = result_obj(data)
            print(f"token_status\t{text(status.get('status'))}")
            print(f"token_id\t{text(status.get('id'))}")
        except CloudflareError as exc:
            print("token_status\tunverified")
            print(f"token_verify_note\t{exc}")
    else:
        print("auth\tglobal-key")
        data = request(profile, "GET", "user")
        user = result_obj(data)
        print(f"email\t{text(user.get('email') or profile.email)}")
        print(f"user_id\t{text(user.get('id'))}")
    try:
        account_id = resolve_account_id(profile)
        print(f"account_id\t{account_id}")
        accounts = paginate(profile, "accounts", limit=5)
        print(f"accounts_visible\t{len(accounts)}")
        print("api\tok")
    except CloudflareError as exc:
        print(f"account_id\t{exc}")
        print("api\tfailed")
        return 1
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    accounts = paginate(profile, "accounts", limit=args.limit)
    if args.json:
        print_json(accounts)
        return 0
    rows = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "type": a.get("type"),
            "profile": profile.name,
        }
        for a in accounts
    ]
    print_csv(["id", "name", "type", "profile"], rows)
    return 0


def cmd_zones(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    params: dict[str, Any] = {}
    if args.name:
        params["name"] = normalize_domain(args.name)
    if args.status:
        params["status"] = args.status
    zones = paginate(profile, "zones", params=params, limit=args.limit)
    if args.json:
        print_json(zones)
        return 0
    print_csv(ZONE_COLUMNS, [zone_row(z, profile) for z in zones])
    return 0


def cmd_zone(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    zone = find_zone(profile, domain)
    if not zone:
        raise CloudflareError(f"Zone not found for {domain!r} on profile={profile.name}")
    if args.json:
        print_json(zone)
        return 0
    row = zone_row(zone, profile)
    for key in ZONE_COLUMNS:
        print(f"{key}\t{text(row.get(key))}")
    return 0


def cmd_domains(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    account_id = resolve_account_id(profile)
    domains = paginate(
        profile,
        f"accounts/{account_id}/registrar/domains",
        limit=args.limit,
    )
    if args.json:
        print_json(domains)
        return 0
    print_csv(DOMAIN_COLUMNS, [domain_row(d, profile) for d in domains])
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    zone = find_zone(profile, domain)
    print(f"domain\t{domain}")
    print(f"profile\t{profile.name}")
    if zone and text(zone.get("name"), "").lower() == domain:
        print("on_account_zone\tyes")
        print(f"zone_id\t{text(zone.get('id'))}")
        print(f"zone_status\t{text(zone.get('status'))}")
    elif zone:
        print("on_account_zone\tparent")
        print(f"parent_zone\t{text(zone.get('name'))}")
        print(f"zone_id\t{text(zone.get('id'))}")
    else:
        print("on_account_zone\tno")

    # Registrar detail when account is known — best effort.
    try:
        account_id = resolve_account_id(profile)
    except CloudflareError as exc:
        print(f"registrar\tunavailable ({exc})")
        return 0

    try:
        data = request(
            profile,
            "GET",
            f"accounts/{account_id}/registrar/domains/{domain}",
        )
        info = result_obj(data)
        print("registrar_record\tyes")
        for key in (
            "current_registrar",
            "expires_at",
            "locked",
            "available",
            "can_register",
            "supported_tld",
        ):
            value = info.get(key)
            if key == "expires_at":
                value = compact_date(value)
            print(f"{key}\t{text(value)}")
    except CloudflareError as exc:
        message = str(exc)
        if "404" in message or "1000" in message:
            print("registrar_record\tno")
            print("note\tNot in Cloudflare Registrar for this account (or no registrar permission).")
        else:
            print(f"registrar\terror ({message})")

    # Real-time registry availability (Registrar API beta).
    try:
        check_data = request(
            profile,
            "POST",
            f"accounts/{account_id}/registrar/domain-check",
            payload={"domains": [domain]},
        )
        rows = (check_data.get("result") or {}).get("domains") or []
        if rows:
            item = rows[0] if isinstance(rows[0], dict) else {}
            print(f"registrable\t{text(item.get('registrable'))}")
            if item.get("reason"):
                print(f"reason\t{text(item.get('reason'))}")
            if item.get("tier"):
                print(f"tier\t{text(item.get('tier'))}")
            pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
            if pricing:
                print(f"currency\t{text(pricing.get('currency'))}")
                print(f"registration_cost\t{text(pricing.get('registration_cost'))}")
                print(f"renewal_cost\t{text(pricing.get('renewal_cost'))}")
    except CloudflareError as exc:
        print(f"availability_check\terror ({exc})")
    return 0


def available_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    return {
        "name": item.get("name"),
        "registrable": item.get("registrable"),
        "reason": item.get("reason"),
        "tier": item.get("tier"),
        "currency": pricing.get("currency"),
        "registration_cost": pricing.get("registration_cost"),
        "renewal_cost": pricing.get("renewal_cost"),
        "profile": profile.name,
    }


def cmd_available(args: argparse.Namespace) -> int:
    """Real-time registry availability via POST .../registrar/domain-check (beta)."""
    profile = get_profile(selected_profile_name(args))
    account_id = resolve_account_id(profile)
    domains = [normalize_domain(d) for d in args.domains]
    if not domains:
        raise CloudflareError("Pass at least one domain")
    if len(domains) > 20:
        raise CloudflareError("domain-check accepts at most 20 domains per request")

    data = request(
        profile,
        "POST",
        f"accounts/{account_id}/registrar/domain-check",
        payload={"domains": domains},
    )
    result = data.get("result") if isinstance(data, dict) else {}
    rows = result.get("domains") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    if args.json:
        print_json(result)
        return 0
    print_csv(AVAILABLE_COLUMNS, [available_row(r, profile) for r in rows if isinstance(r, dict)])
    return 0


def cmd_search_domains(args: argparse.Namespace) -> int:
    """Keyword domain discovery via GET .../registrar/domain-search (beta, cached)."""
    profile = get_profile(selected_profile_name(args))
    account_id = resolve_account_id(profile)
    query = (args.query or "").strip()
    if not query:
        raise CloudflareError("Search query is required")
    limit = args.limit
    if limit < 1 or limit > 50:
        raise CloudflareError("--limit must be between 1 and 50 for search")

    data = request(
        profile,
        "GET",
        f"accounts/{account_id}/registrar/domain-search",
        params={"q": query, "limit": limit},
    )
    result = data.get("result") if isinstance(data, dict) else {}
    rows = result.get("domains") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    if args.json:
        print_json(result)
        return 0
    # Search uses registrable + pricing; reuse available_row shape without reason.
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = available_row(r, profile)
        out.append(row)
    print_csv(SEARCH_COLUMNS, out)
    return 0


def contact_from_env(profile: Profile) -> dict[str, str]:
    mapping = {
        "first_name": "CONTACT_FIRST_NAME",
        "last_name": "CONTACT_LAST_NAME",
        "organization": "CONTACT_ORGANIZATION",
        "address": "CONTACT_ADDRESS",
        "address2": "CONTACT_ADDRESS2",
        "city": "CONTACT_CITY",
        "state": "CONTACT_STATE",
        "zip": "CONTACT_ZIP",
        "country": "CONTACT_COUNTRY",
        "phone": "CONTACT_PHONE",
        "email": "CONTACT_EMAIL",
    }
    contact: dict[str, str] = {}
    for field, suffix in mapping.items():
        value = os.environ.get(env_name(profile.name, suffix), "").strip()
        if value:
            contact[field] = value
    return contact


def cmd_register(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    years = args.years
    if years < 1 or years > 10:
        raise CloudflareError("--years must be between 1 and 10")

    account_id = resolve_account_id(profile)
    zone = find_zone(profile, domain)
    if zone and text(zone.get("name"), "").lower() == domain:
        raise CloudflareError(
            f"{domain} already has a Cloudflare zone on profile={profile.name} "
            f"(id={text(zone.get('id'))}). Refusing register."
        )

    contact = contact_from_env(profile)
    payload: dict[str, Any] = {
        "name": domain,
        "years": years,
        "auto_renew": bool(args.auto_renew),
        "privacy": bool(args.privacy),
    }
    if contact:
        payload["registrant_contact"] = contact

    print(f"action\tregister")
    print(f"domain\t{domain}")
    print(f"years\t{years}")
    print(f"auto_renew\t{payload['auto_renew']}")
    print(f"privacy\t{payload['privacy']}")
    print(f"account_id\t{account_id}")
    print(f"profile\t{profile.name}")
    print(f"contact_fields\t{','.join(sorted(contact)) if contact else '-'}")

    if not args.confirm:
        print("mode\tdry-run")
        print(
            "next\tOwner approval required. Re-run with --confirm only after the owner approves "
            f"registering {domain} for {years} year(s) on profile {profile.name}."
        )
        print(
            "note\tRegistrar purchase also needs contact fields in env "
            f"({env_name(profile.name, 'CONTACT_FIRST_NAME')}, …) when the API requires them."
        )
        return 0

    # Prefer the documented purchase endpoint; fall back messaging on failure.
    try:
        data = request(
            profile,
            "POST",
            f"accounts/{account_id}/registrar/domains/purchase",
            payload=payload,
        )
    except CloudflareError as exc:
        raise CloudflareError(
            f"Register failed for {domain}: {exc}. "
            "Confirm the token/global key has Registrar permissions and contact fields are complete."
        ) from exc

    if args.json:
        print_json(data)
        return 0
    result = result_obj(data) or data.get("result")
    print("mode\tconfirmed")
    print(f"result\t{text(result if not isinstance(result, (dict, list)) else json.dumps(result))}")
    return 0


def cmd_dns(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    zone = require_zone(profile, domain)
    zone_id = text(zone.get("id"), "")
    zone_name = text(zone.get("name"), domain)
    params: dict[str, Any] = {}
    if args.type:
        params["type"] = args.type.upper()
    if args.name:
        name = args.name
        if name != "@" and "." not in name:
            name = f"{name}.{zone_name}"
        elif name == "@":
            name = zone_name
        params["name"] = name
    records = paginate(
        profile,
        f"zones/{zone_id}/dns_records",
        params=params,
        limit=args.limit,
    )
    if args.json:
        print_json(records)
        return 0
    print_csv(DNS_COLUMNS, [dns_row(r, zone_name, profile) for r in records])
    return 0


def cmd_dns_add(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    zone = require_zone(profile, domain)
    zone_id = text(zone.get("id"), "")
    zone_name = text(zone.get("name"), domain)

    record_type = args.type.upper()
    name = args.name
    if name == "@":
        name = zone_name
    elif "." not in name:
        name = f"{name}.{zone_name}"

    payload: dict[str, Any] = {
        "type": record_type,
        "name": name,
        "content": args.content,
        "ttl": args.ttl,
    }
    if args.proxied is not None:
        payload["proxied"] = bool(args.proxied)
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.comment:
        payload["comment"] = args.comment

    print("action\tdns-add")
    print(f"zone\t{zone_name}")
    print(f"zone_id\t{zone_id}")
    for key in ("type", "name", "content", "ttl", "proxied", "priority", "comment"):
        if key in payload:
            print(f"{key}\t{payload[key]}")
    print(f"profile\t{profile.name}")

    if not args.confirm:
        print("mode\tdry-run")
        print(
            "next\tOwner approval required. Re-run with --confirm only after the owner approves "
            f"creating this {record_type} record on {zone_name}."
        )
        return 0

    data = request(profile, "POST", f"zones/{zone_id}/dns_records", payload=payload)
    record = result_obj(data)
    if args.json:
        print_json(record)
        return 0
    print("mode\tconfirmed")
    print_csv(DNS_COLUMNS, [dns_row(record, zone_name, profile)])
    return 0


def cmd_dns_set(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    zone = require_zone(profile, domain)
    zone_id = text(zone.get("id"), "")
    zone_name = text(zone.get("name"), domain)
    record_id = args.record_id.strip()
    if not record_id:
        raise CloudflareError("RECORD_ID is required")

    # Fetch current for dry-run context
    current = result_obj(request(profile, "GET", f"zones/{zone_id}/dns_records/{record_id}"))
    payload: dict[str, Any] = {}
    if args.type:
        payload["type"] = args.type.upper()
    if args.name:
        name = args.name
        if name == "@":
            name = zone_name
        elif "." not in name:
            name = f"{name}.{zone_name}"
        payload["name"] = name
    if args.content is not None:
        payload["content"] = args.content
    if args.ttl is not None:
        payload["ttl"] = args.ttl
    if args.proxied is not None:
        payload["proxied"] = parse_bool(args.proxied)
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.comment is not None:
        payload["comment"] = args.comment

    if not payload:
        raise CloudflareError("Nothing to update; pass at least one of --type/--name/--content/--ttl/--proxied/--priority/--comment")

    print("action\tdns-set")
    print(f"zone\t{zone_name}")
    print(f"record_id\t{record_id}")
    print(f"current_type\t{text(current.get('type'))}")
    print(f"current_name\t{text(current.get('name'))}")
    print(f"current_content\t{text(current.get('content'))}")
    for key, value in payload.items():
        print(f"set_{key}\t{value}")
    print(f"profile\t{profile.name}")

    if not args.confirm:
        print("mode\tdry-run")
        print(
            "next\tOwner approval required. Re-run with --confirm only after the owner approves "
            f"updating DNS record {record_id} on {zone_name}."
        )
        return 0

    data = request(
        profile,
        "PATCH",
        f"zones/{zone_id}/dns_records/{record_id}",
        payload=payload,
    )
    record = result_obj(data)
    if args.json:
        print_json(record)
        return 0
    print("mode\tconfirmed")
    print_csv(DNS_COLUMNS, [dns_row(record, zone_name, profile)])
    return 0


def cmd_dns_rm(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    domain = normalize_domain(args.domain)
    zone = require_zone(profile, domain)
    zone_id = text(zone.get("id"), "")
    zone_name = text(zone.get("name"), domain)
    record_id = args.record_id.strip()
    if not record_id:
        raise CloudflareError("RECORD_ID is required")

    current = result_obj(request(profile, "GET", f"zones/{zone_id}/dns_records/{record_id}"))
    print("action\tdns-rm")
    print(f"zone\t{zone_name}")
    print(f"record_id\t{record_id}")
    print(f"type\t{text(current.get('type'))}")
    print(f"name\t{text(current.get('name'))}")
    print(f"content\t{text(current.get('content'))}")
    print(f"profile\t{profile.name}")

    if not args.confirm:
        print("mode\tdry-run")
        print(
            "next\tOwner approval required. Re-run with --confirm only after the owner approves "
            f"deleting DNS record {record_id} on {zone_name}."
        )
        return 0

    request(profile, "DELETE", f"zones/{zone_id}/dns_records/{record_id}")
    print("mode\tconfirmed")
    print("deleted\tyes")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudflare",
        description="Cloudflare zones, registrar domains, and DNS (mutations dry-run without --confirm).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional dotenv path (default: tool override / shared env / isolated Cloudflare env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_profile(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", default=None, help="Cloudflare profile name from .env")
        p.add_argument("--json", action="store_true", help="Print raw/structured JSON")

    p = sub.add_parser("profiles", help="List configured profiles")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("status", help="Verify credentials (read-only)")
    add_profile(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("accounts", help="List accounts visible to the profile")
    add_profile(p)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("zones", help="List zones (domains) on the account")
    add_profile(p)
    p.add_argument("--name", default=None, help="Exact zone name filter")
    p.add_argument("--status", default=None, help="Zone status filter (active, pending, ...)")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_zones)

    p = sub.add_parser("zone", help="Show one zone by domain name")
    add_profile(p)
    p.add_argument("domain")
    p.set_defaults(func=cmd_zone)

    p = sub.add_parser("domains", help="List Cloudflare Registrar domains")
    add_profile(p)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_domains)

    p = sub.add_parser(
        "check",
        help="On-account zone + Registrar record + real-time availability for one domain",
    )
    add_profile(p)
    p.add_argument("domain")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser(
        "available",
        help="Real-time registry availability/pricing (Registrar API domain-check, up to 20)",
    )
    add_profile(p)
    p.add_argument("domains", nargs="+", help="FQDNs including TLD, e.g. example.com")
    p.set_defaults(func=cmd_available)

    p = sub.add_parser(
        "search",
        help="Keyword domain discovery (Registrar API domain-search; confirm with available)",
    )
    add_profile(p)
    p.add_argument("query", help='Keyword or phrase, e.g. "coffee shop"')
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search_domains)

    p = sub.add_parser(
        "register",
        help="Register a domain via Cloudflare Registrar (dry-run unless --confirm)",
    )
    add_profile(p)
    p.add_argument("domain")
    p.add_argument("--years", type=int, default=1)
    p.add_argument("--auto-renew", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--privacy", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually purchase/register after owner approval for this exact domain",
    )
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("dns", help="List DNS records for a zone")
    add_profile(p)
    p.add_argument("domain", help="Zone apex or hostname under a zone")
    p.add_argument("--type", default=None, help="Record type filter (A, CNAME, TXT, ...)")
    p.add_argument("--name", default=None, help="Record name filter (@, www, or FQDN)")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_dns)

    p = sub.add_parser("dns-add", help="Create a DNS record (dry-run unless --confirm)")
    add_profile(p)
    p.add_argument("domain")
    p.add_argument("--type", required=True)
    p.add_argument("--name", required=True, help="@ for apex, subdomain, or FQDN")
    p.add_argument("--content", required=True)
    p.add_argument("--ttl", type=int, default=1, help="TTL seconds; 1 = automatic")
    p.add_argument("--proxied", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--priority", type=int, default=None, help="MX/SRV priority")
    p.add_argument("--comment", default=None)
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_dns_add)

    p = sub.add_parser("dns-set", help="Update a DNS record (dry-run unless --confirm)")
    add_profile(p)
    p.add_argument("domain")
    p.add_argument("record_id")
    p.add_argument("--type", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--content", default=None)
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--proxied", default=None, help="true or false")
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--comment", default=None)
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_dns_set)

    p = sub.add_parser("dns-rm", help="Delete a DNS record (dry-run unless --confirm)")
    add_profile(p)
    p.add_argument("domain")
    p.add_argument("record_id")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_dns_rm)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env_path = resolve_env_file(getattr(args, "env_file", None))
    load_dotenv(env_path)
    try:
        if args.command != "profiles" and getattr(args, "limit", None) is not None:
            if args.limit < 1 or args.limit > 200:
                raise CloudflareError("--limit must be between 1 and 200")
        return int(args.func(args))
    except CloudflareError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
