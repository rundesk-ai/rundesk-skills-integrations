#!/usr/bin/env python3
"""
Read a Monarch Money household: accounts, net worth, spending, budgets, and holdings.

Usage:
  monarch profiles
  monarch status [--profile household | --all-profiles]
  monarch accounts [--profile household] [--limit 50]
  monarch networth [--profile household] [--days 90]
  monarch transactions [--profile household] [--days 30] [--limit 25]
                       [--account NAME] [--category NAME]
  monarch categories [--profile household] [--limit 200]
  monarch budgets [--profile household] [--month 2026-08] [--limit 100]
  monarch cashflow [--profile household] [--days 30] [--limit 25]
  monarch holdings --account NAME [--profile household] [--limit 50]

Inputs:
  Reads process env or an explicit/shared/isolated dotenv. Configure MONARCH_PROFILES
  and MONARCH_<PROFILE>_* keys; see references/cli.md for setup. Monarch issues no scoped
  or read-only key, so these values are full account access and must stay in an
  owner-only environment file.

Outputs:
  Writes compact text summaries to stdout. List output is CSV-style rows, account mask
  digits are redacted to the last two, and bounded reads report `showing N of M` on
  stderr. No raw JSON unless --json is provided.

  Every command reads. This package contains no command that creates, updates, or
  deletes a transaction, budget, goal, holding, or account.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import calendar
import csv
import datetime
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


API_BASE = "https://api.monarch.com"
LOGIN_PATH = "/auth/login/"
GRAPHQL_PATH = "/graphql"
WEB_ORIGIN = "https://app.monarch.com"
USER_AGENT = "rundesk-monarch/1.0"

TOTP_STEP = 30
TOTP_DIGITS = 6

# What `truncate` appends, and what `match_one` strips back off. One literal, because a
# name this tool shortened for display is a name an agent will hand straight back to it.
ELLIPSIS = "..."

# Monarch reports a login challenge three different ways depending on which edge served
# the request: an HTTP status, or an `error_code` inside an otherwise-200 body.
MFA_ERROR_CODES = frozenset({"MFA_REQUIRED", "EMAIL_OTP_REQUIRED"})
CAPTCHA_ERROR_CODE = "CAPTCHA_REQUIRED"

ACCOUNT_COLUMNS = ["type", "subtype", "institution", "name", "mask", "balance", "updated"]
NETWORTH_COLUMNS = ["point", "date", "assets", "liabilities", "net"]
TRANSACTION_COLUMNS = ["date", "merchant", "category", "account", "amount", "pending"]
CATEGORY_COLUMNS = ["group", "type", "name", "id"]
BUDGET_COLUMNS = ["group", "category", "budgeted", "actual", "remaining"]
CASHFLOW_COLUMNS = ["scope", "name", "type", "amount"]
HOLDING_COLUMNS = ["ticker", "name", "quantity", "price", "value"]

MASK_PATTERN = re.compile(r"\d")

QUERY_ACCOUNTS = """
query GetAccounts {
  accounts {
    id
    displayName
    mask
    isAsset
    isHidden
    includeInNetWorth
    currentBalance
    displayBalance
    displayLastUpdatedAt
    updatedAt
    type { name display }
    subtype { name display }
    institution { name }
  }
}
"""

# `assetsBalance`/`liabilitiesBalance` are attested by one source only; NETWORTH_FALLBACK
# is the two-source-attested shape used when the server rejects them.
QUERY_NETWORTH = """
query Common_GetAggregateSnapshots($filters: AggregateSnapshotFilters) {
  aggregateSnapshots(filters: $filters) {
    date
    balance
    assetsBalance
    liabilitiesBalance
  }
}
"""

QUERY_NETWORTH_FALLBACK = """
query GetAggregateSnapshots($filters: AggregateSnapshotFilters) {
  aggregateSnapshots(filters: $filters) {
    date
    balance
  }
}
"""

QUERY_TRANSACTIONS = """
query GetTransactionsList($offset: Int, $limit: Int, $filters: TransactionFilterInput, \
$orderBy: TransactionOrdering) {
  allTransactions(filters: $filters) {
    totalCount
    results(offset: $offset, limit: $limit, orderBy: $orderBy) {
      id
      date
      amount
      pending
      category { id name }
      merchant { id name }
      account { id displayName }
    }
  }
}
"""

QUERY_CATEGORIES = """
query GetCategories {
  categories {
    id
    name
    isDisabled
    group { id name type }
  }
}
"""

QUERY_BUDGETS = """
query Common_GetJointPlanningData($startDate: Date!, $endDate: Date!) {
  budgetData(startMonth: $startDate, endMonth: $endDate) {
    monthlyAmountsByCategory {
      category { id name group { id name type } }
      monthlyAmounts {
        month
        plannedCashFlowAmount
        actualAmount
        remainingAmount
      }
    }
  }
}
"""

QUERY_CASHFLOW = """
query Web_GetCashFlowPage($filters: TransactionFilterInput) {
  summary: aggregates(filters: $filters, fillEmptyValues: true) {
    summary { sumIncome sumExpense savings savingsRate }
  }
  byCategoryGroup: aggregates(filters: $filters, groupBy: ["categoryGroup"]) {
    groupBy { categoryGroup { id name type } }
    summary { sum }
  }
}
"""

QUERY_HOLDINGS = """
query Web_GetHoldings($input: PortfolioInput) {
  portfolio(input: $input) {
    aggregateHoldings {
      edges {
        node {
          id
          quantity
          totalValue
          security { id name ticker currentPrice currentPriceUpdatedAt }
          holdings { id name ticker closingPrice }
        }
      }
    }
  }
}
"""


class MonarchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    email: str
    password: str
    mfa_secret: str
    label: str

    @property
    def masked_email(self) -> str:
        return mask_email(self.email)

    @property
    def has_mfa(self) -> bool:
        return bool(self.mfa_secret)


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("MONARCH_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "monarch" / "env")
    candidates.append(xdg / "monarch" / "env")
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
    return f"MONARCH_{normalized}_{suffix}"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("MONARCH_PROFILES"))
    default = os.environ.get("MONARCH_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    names = set()
    pattern = re.compile(r"^MONARCH_([A-Z0-9_]+)_(EMAIL|PASSWORD|MFA_SECRET|LABEL)$")
    for key in os.environ:
        match = pattern.match(key)
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return sorted(names)


def get_profile(name: str) -> Profile:
    email = os.environ.get(env_name(name, "EMAIL"), "").strip()
    password = os.environ.get(env_name(name, "PASSWORD"), "")
    missing = [
        env_name(name, suffix)
        for suffix, value in (("EMAIL", email), ("PASSWORD", password))
        if not value
    ]
    if missing:
        raise MonarchError(
            f"Missing Monarch config for profile {name!r}: {', '.join(missing)}. "
            "Add it to the integration dotenv or export it in the shell."
        )

    return Profile(
        name=name,
        email=email,
        password=password,
        mfa_secret=os.environ.get(env_name(name, "MFA_SECRET"), "").strip(),
        label=os.environ.get(env_name(name, "LABEL"), name),
    )


def selected_profile_name(args: argparse.Namespace) -> str:
    profile_name = getattr(args, "profile", None) or os.environ.get("MONARCH_DEFAULT_PROFILE", "")
    if profile_name:
        return profile_name

    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if names:
        raise MonarchError(
            "Multiple Monarch profiles configured. Pass --profile or set MONARCH_DEFAULT_PROFILE."
        )
    raise MonarchError("No Monarch profile selected. Pass --profile or set MONARCH_DEFAULT_PROFILE.")


def selected_profiles(args: argparse.Namespace) -> list[Profile]:
    if getattr(args, "all_profiles", False):
        names = configured_profile_names()
        if not names:
            raise MonarchError(
                "No Monarch profiles configured. Set MONARCH_PROFILES or MONARCH_DEFAULT_PROFILE."
            )
        return [get_profile(name) for name in names]
    return [get_profile(selected_profile_name(args))]


def mask_email(value: str) -> str:
    """Show only the first character of the local part: `a***@example.test`."""
    if "@" not in value:
        return "***" if value else "-"
    local, _, domain = value.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def mask_account(value: Any) -> str:
    """Reduce an account mask to its last two digits, so no full number is ever printed."""
    digits = MASK_PATTERN.findall(str(value or ""))
    if not digits:
        return "-"
    return "····" + "".join(digits[-2:])


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    rendered = str(value).replace("\n", " ").strip()
    return rendered if rendered else fallback


def truncate(value: Any, limit: int = 120) -> str:
    rendered = text(value)
    if len(rendered) <= limit:
        return rendered
    if limit <= len(ELLIPSIS):
        return rendered[:limit]
    return rendered[: limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


def format_amount(value: Any) -> str:
    """Render a Monarch major-unit amount with two decimal places."""
    if value in (None, ""):
        return "-"
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return text(value)


def format_quantity(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        quantity = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return text(value)
    return format(quantity, "f")


def compact_date(value: Any) -> str:
    """Trim an ISO-8601 timestamp to `YYYY-MM-DD HH:MM`, leaving plain dates alone."""
    rendered = text(value)
    if rendered == "-":
        return rendered
    normalized = rendered.replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return rendered[:16]
    if moment.hour or moment.minute:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d")


def parse_day(value: str, label: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise MonarchError(f"Invalid --{label} date {value!r}. Use YYYY-MM-DD.") from exc


def parse_month(value: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise MonarchError(f"Invalid --month {value!r}. Use YYYY-MM.") from exc


def today() -> datetime.date:
    return datetime.date.today()


def window_days(days: int) -> tuple[str, str]:
    if days < 1:
        raise MonarchError("--days must be at least 1.")
    end = today()
    return (end - datetime.timedelta(days=days)).isoformat(), end.isoformat()


def month_bounds(first: datetime.date) -> tuple[str, str]:
    last_day = calendar.monthrange(first.year, first.month)[1]
    return first.isoformat(), first.replace(day=last_day).isoformat()


def print_csv(columns: list[str], rows: list[dict]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: text(row.get(column)) for column in columns})


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def print_kv(data: dict, keys: list[str]) -> None:
    for key in keys:
        print(f"{key}\t{text(data.get(key))}")


def note_truncation(shown: int, total: Any, what: str) -> None:
    try:
        available = int(total)
    except (TypeError, ValueError):
        return
    if available > shown:
        print(
            f"note: showing {shown} of {available} {what}; raise --limit or narrow the window.",
            file=sys.stderr,
        )


def totp(secret: str, at: float | None = None, digits: int = TOTP_DIGITS) -> str:
    """RFC 6238 time-based one-time password: SHA-1, 30-second step, 6 digits."""
    normalized = re.sub(r"\s+", "", secret).upper()
    padded = normalized + "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise MonarchError(
            "MFA secret is not valid base32. Copy the seed Monarch shows when setting up an "
            "authenticator app, not the six-digit code."
        ) from exc
    if not key:
        raise MonarchError("MFA secret decoded to an empty key.")

    counter = int((time.time() if at is None else at) // TOTP_STEP)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def state_dir() -> Path:
    base = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ).expanduser()
    return base / "rundesk" / "integrations" / "monarch"


def session_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return base / "rundesk" / "integrations" / "monarch"


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def read_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def device_uuid() -> str:
    """A stable per-install device id; a changing one re-triggers MFA on every call."""
    path = state_dir() / "device.json"
    stored = read_json(path).get("device")
    if isinstance(stored, str) and stored:
        return stored

    generated = str(uuid.uuid4())
    try:
        write_private_json(path, {"device": generated})
    except OSError:
        pass
    return generated


def session_path(profile: Profile) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", profile.name).strip("-") or "default"
    return session_dir() / f"session-{safe}.json"


def read_session(profile: Profile) -> str:
    token = read_json(session_path(profile)).get("token")
    return token if isinstance(token, str) else ""


def write_session(profile: Profile, token: str, device: str) -> None:
    try:
        write_private_json(
            session_path(profile),
            {"token": token, "device": device, "saved": int(time.time())},
        )
    except OSError as exc:
        print(f"note: could not cache the Monarch session ({exc.strerror}).", file=sys.stderr)


def discard_session(profile: Profile) -> None:
    try:
        session_path(profile).unlink()
    except OSError:
        pass


def base_headers(device: str) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Client-Platform": "web",
        "User-Agent": USER_AGENT,
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
        "device-uuid": device,
    }


def url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(value)
    port = parsed.port
    if port is None:
        scheme = parsed.scheme.lower()
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and url_origin(req.full_url) != url_origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


def open_url(req: urllib.request.Request, timeout: int):
    return urllib.request.build_opener(SameOriginRedirectHandler()).open(req, timeout=timeout)


def post_json(url: str, payload: dict, headers: dict, timeout: int = 30) -> tuple[int, dict]:
    """POST JSON and return (status, decoded body). HTTP errors are values, not exceptions."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with open_url(req, timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.getcode() or 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise MonarchError(f"Monarch API request failed: {exc.reason}") from exc

    if not raw.strip():
        return status, {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return status, {"detail": raw[:500]}
    return status, decoded if isinstance(decoded, dict) else {"data": decoded}


def login(profile: Profile, device: str) -> str:
    """Exchange email/password (plus TOTP when configured) for a long-lived API token."""
    payload = {
        "username": profile.email,
        "password": profile.password,
        "trusted_device": True,
        "supports_mfa": True,
    }
    if profile.mfa_secret:
        payload["totp"] = totp(profile.mfa_secret)

    status, body = post_json(API_BASE + LOGIN_PATH, payload, base_headers(device))
    error_code = str(body.get("error_code") or "")

    if error_code == CAPTCHA_ERROR_CODE:
        raise MonarchError(
            f"Monarch blocked the login for profile {profile.name!r} with a CAPTCHA challenge. "
            "This is a rate-limit response; wait and retry rather than repeating the call."
        )

    challenged = status in (401, 403) or error_code in MFA_ERROR_CODES
    if challenged and not profile.mfa_secret:
        raise MonarchError(
            f"Monarch requires multi-factor authentication for profile {profile.name!r}. "
            f"Set {env_name(profile.name, 'MFA_SECRET')} to the base32 seed from Monarch's "
            "authenticator setup, then retry."
        )
    if challenged:
        # A code computed a moment ago can land in the next 30-second step; answer the
        # challenge once with a freshly generated one before giving up.
        payload["totp"] = totp(profile.mfa_secret)
        status, body = post_json(API_BASE + LOGIN_PATH, payload, base_headers(device))
        error_code = str(body.get("error_code") or "")

    if status != 200 or error_code:
        raise MonarchError(
            f"Monarch login failed for profile {profile.name!r} "
            f"(HTTP {status}): {login_error(body)}"
        )

    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise MonarchError(
            f"Monarch login for profile {profile.name!r} returned no token."
        )
    if token.count(".") == 2:
        raise MonarchError(
            f"Monarch returned a short-lived feature token for profile {profile.name!r} "
            "instead of a session token. Retry; if it persists, Monarch changed the login flow."
        )
    return token


def login_error(body: dict) -> str:
    for key in ("detail", "error_code", "message"):
        value = body.get(key)
        if value:
            return truncate(value, 200)
    return "check the email and password for this profile"


# Tokens are resolved once per process so a multi-command run logs in at most once.
_TOKENS: dict = {}
_TOKEN_SOURCE: dict = {}


def token_for(profile: Profile, refresh: bool = False) -> str:
    device = device_uuid()
    if refresh:
        _TOKENS.pop(profile.name, None)
        discard_session(profile)
    elif profile.name in _TOKENS:
        return _TOKENS[profile.name]

    if not refresh:
        cached = read_session(profile)
        if cached:
            _TOKENS[profile.name] = cached
            _TOKEN_SOURCE[profile.name] = "cached"
            return cached

    token = login(profile, device)
    write_session(profile, token, device)
    _TOKENS[profile.name] = token
    _TOKEN_SOURCE[profile.name] = "fresh"
    return token


def graphql(profile: Profile, operation: str, document: str, variables: dict | None = None) -> dict:
    """POST one GraphQL operation, re-logging in exactly once if the session is rejected."""
    payload = {
        "operationName": operation,
        "query": document,
        "variables": variables or {},
    }
    device = device_uuid()

    for attempt in range(2):
        headers = base_headers(device)
        headers["Authorization"] = "Token " + token_for(profile, refresh=attempt > 0)
        status, body = post_json(API_BASE + GRAPHQL_PATH, payload, headers)

        if status in (401, 403) and attempt == 0:
            continue
        if status in (401, 403):
            raise MonarchError(
                f"Monarch rejected the session for profile {profile.name!r} (HTTP {status}). "
                "The stored credentials may no longer be valid."
            )
        if status != 200:
            raise MonarchError(
                f"Monarch {operation} failed for profile {profile.name!r} "
                f"(HTTP {status}): {login_error(body)}"
            )

        errors = body.get("errors")
        if errors:
            raise MonarchError(
                f"Monarch {operation} returned errors for profile {profile.name!r}: "
                + graphql_error(errors)
            )

        data = body.get("data")
        if not isinstance(data, dict):
            raise MonarchError(f"Monarch {operation} returned no data.")
        return data

    raise MonarchError(f"Monarch {operation} could not be authenticated.")


def graphql_error(errors: Any) -> str:
    if not isinstance(errors, list):
        return truncate(errors, 300)
    messages = [
        truncate(entry.get("message"), 200)
        for entry in errors
        if isinstance(entry, dict) and entry.get("message")
    ]
    return "; ".join(messages) if messages else truncate(errors, 300)


def match_one(candidates: list[dict], key: str, wanted: str, what: str) -> dict:
    """Resolve a user-supplied name to exactly one record, refusing to guess."""
    needle = wanted.strip().casefold()
    exact = [item for item in candidates if str(item.get(key) or "").casefold() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise MonarchError(
            f"{what} name {wanted!r} matches {len(exact)} records exactly. "
            "Rename one in Monarch, or use --json and filter by id."
        )

    # Text output shortens long names, so a name read back from `accounts` or
    # `transactions` carries this tool's own ellipsis. Substring matching can never find
    # it — the real name continues past the cut — but what precedes the mark is a prefix.
    stem = needle[: -len(ELLIPSIS)].rstrip() if needle.endswith(ELLIPSIS) else ""
    if stem:
        prefixed = [item for item in candidates
                    if str(item.get(key) or "").casefold().startswith(stem)]
        if len(prefixed) == 1:
            return prefixed[0]
        if len(prefixed) > 1:
            raise MonarchError(
                f"{what} name {wanted!r} was shortened for display and now matches "
                f"{len(prefixed)} records. Use --json and filter by id."
            )

    partial = [item for item in candidates if needle in str(item.get(key) or "").casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise MonarchError(
            f"No {what.lower()} matches {wanted!r}. "
            f"List the available names first."
        )
    names = ", ".join(sorted(truncate(item.get(key), 40) for item in partial)[:8])
    raise MonarchError(
        f"{what} name {wanted!r} is ambiguous; it matches: {names}. "
        "Pass more of the name, or use --json and filter by id."
    )


def fetch_accounts(profile: Profile) -> list[dict]:
    data = graphql(profile, "GetAccounts", QUERY_ACCOUNTS)
    accounts = data.get("accounts")
    return [item for item in accounts if isinstance(item, dict)] if isinstance(accounts, list) else []


def account_row(item: dict) -> dict:
    kind = item.get("type") or {}
    subtype = item.get("subtype") or {}
    institution = item.get("institution") or {}
    balance = item.get("displayBalance")
    if balance is None:
        balance = item.get("currentBalance")
    return {
        "type": text((kind or {}).get("display") or (kind or {}).get("name")),
        "subtype": text((subtype or {}).get("display") or (subtype or {}).get("name")),
        "institution": truncate((institution or {}).get("name"), 40),
        "name": truncate(item.get("displayName"), 40),
        "mask": mask_account(item.get("mask")),
        "balance": format_amount(balance),
        "updated": compact_date(item.get("displayLastUpdatedAt") or item.get("updatedAt")),
    }


def command_profiles(args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print("No Monarch profiles configured. Set MONARCH_PROFILES or MONARCH_DEFAULT_PROFILE.")
        return 0

    print("Monarch profiles")
    for name in names:
        try:
            profile = get_profile(name)
        except MonarchError as exc:
            print(f"- profile={name} | error={exc}")
            continue
        print(
            "- "
            + " | ".join(
                [
                    f"profile={profile.name}",
                    f"label={profile.label}",
                    f"email={profile.masked_email}",
                    f"mfa={'yes' if profile.has_mfa else 'no'}",
                ]
            )
        )
    return 0


def command_status(args: argparse.Namespace) -> int:
    failed = False
    for profile in selected_profiles(args):
        row = {
            "profile": profile.name,
            "label": profile.label,
            "email": profile.masked_email,
            "auth": "failed",
            "session": "-",
            "accounts": "-",
        }
        try:
            accounts = fetch_accounts(profile)
        except MonarchError as exc:
            message = str(exc)
            row["auth"] = "mfa-required" if "multi-factor" in message else "failed"
            row["error"] = message
            failed = True
        else:
            row["auth"] = "ok"
            row["session"] = _TOKEN_SOURCE.get(profile.name, "cached")
            row["accounts"] = len(accounts)

        if args.json:
            print_json(row)
            continue

        print_kv(row, ["profile", "label", "email", "auth", "session", "accounts"])
        if "error" in row:
            print(f"error: {row['error']}", file=sys.stderr)
        print()
    return 1 if failed else 0


def command_accounts(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    accounts = fetch_accounts(profile)
    if args.json:
        print_json(accounts)
        return 0

    shown = accounts[: args.limit]
    print_csv(ACCOUNT_COLUMNS, [account_row(item) for item in shown])
    note_truncation(len(shown), len(accounts), "accounts")
    return 0


def networth_snapshots(profile: Profile, start: str, end: str) -> tuple[list[dict], bool]:
    """Daily net-worth snapshots, degrading to net-only when the split fields are refused."""
    variables = {"filters": {"startDate": start, "endDate": end}}
    try:
        data = graphql(profile, "Common_GetAggregateSnapshots", QUERY_NETWORTH, variables)
        split = True
    except MonarchError as exc:
        message = str(exc)
        if "assetsBalance" not in message and "liabilitiesBalance" not in message:
            raise
        data = graphql(profile, "GetAggregateSnapshots", QUERY_NETWORTH_FALLBACK, variables)
        split = False

    snapshots = data.get("aggregateSnapshots")
    rows = [item for item in snapshots if isinstance(item, dict)] if isinstance(snapshots, list) else []
    return rows, split


def command_networth(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    start, end = window_days(args.days)
    snapshots, split = networth_snapshots(profile, start, end)

    if args.json:
        print_json(snapshots)
        return 0

    if not snapshots:
        print(f"No net worth snapshots between {start} and {end}.")
        return 0
    if not split:
        print(
            "note: this Monarch schema does not expose the asset/liability split; "
            "reporting net only.",
            file=sys.stderr,
        )

    first, last = snapshots[0], snapshots[-1]
    rows = [
        {
            "point": "first",
            "date": text(first.get("date")),
            "assets": format_amount(first.get("assetsBalance")),
            "liabilities": format_amount(first.get("liabilitiesBalance")),
            "net": format_amount(first.get("balance")),
        },
        {
            "point": "last",
            "date": text(last.get("date")),
            "assets": format_amount(last.get("assetsBalance")),
            "liabilities": format_amount(last.get("liabilitiesBalance")),
            "net": format_amount(last.get("balance")),
        },
        {
            "point": "change",
            "date": f"{text(first.get('date'))}..{text(last.get('date'))}",
            "assets": delta(first.get("assetsBalance"), last.get("assetsBalance")),
            "liabilities": delta(first.get("liabilitiesBalance"), last.get("liabilitiesBalance")),
            "net": delta(first.get("balance"), last.get("balance")),
        },
    ]
    print_csv(NETWORTH_COLUMNS, rows)
    return 0


def delta(first: Any, last: Any) -> str:
    if first in (None, "") or last in (None, ""):
        return "-"
    try:
        return f"{Decimal(str(last)) - Decimal(str(first)):.2f}"
    except (InvalidOperation, ValueError):
        return "-"


def command_transactions(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    start, end = window_days(args.days)
    if args.limit < 1:
        raise MonarchError("--limit must be at least 1.")

    filters: dict = {
        "search": "",
        "categories": [],
        "accounts": [],
        "tags": [],
        "startDate": start,
        "endDate": end,
    }
    if args.account:
        accounts = fetch_accounts(profile)
        chosen = match_one(accounts, "displayName", args.account, "Account")
        filters["accounts"] = [str(chosen.get("id"))]
    if args.category:
        categories = fetch_categories(profile)
        chosen = match_one(categories, "name", args.category, "Category")
        filters["categories"] = [str(chosen.get("id"))]

    data = graphql(
        profile,
        "GetTransactionsList",
        QUERY_TRANSACTIONS,
        {"offset": 0, "limit": args.limit, "orderBy": "date", "filters": filters},
    )
    page = data.get("allTransactions") or {}
    results = page.get("results")
    items = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    if args.json:
        print_json(items)
        return 0

    print(f"window\t{start} .. {end}", file=sys.stderr)
    print_csv(TRANSACTION_COLUMNS, [transaction_row(item) for item in items])
    note_truncation(len(items), page.get("totalCount"), "transactions")
    return 0


def transaction_row(item: dict) -> dict:
    merchant = item.get("merchant") or {}
    category = item.get("category") or {}
    account = item.get("account") or {}
    return {
        "date": text(item.get("date")),
        "merchant": truncate(merchant.get("name"), 40),
        "category": truncate(category.get("name"), 30),
        "account": truncate(account.get("displayName"), 30),
        "amount": format_amount(item.get("amount")),
        "pending": "yes" if item.get("pending") else "no",
    }


def fetch_categories(profile: Profile) -> list[dict]:
    data = graphql(profile, "GetCategories", QUERY_CATEGORIES)
    categories = data.get("categories")
    if not isinstance(categories, list):
        return []
    return [item for item in categories if isinstance(item, dict)]


def command_categories(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    categories = fetch_categories(profile)
    if args.json:
        print_json(categories)
        return 0

    rows = []
    for item in sorted(
        categories,
        key=lambda entry: (
            str(((entry.get("group") or {}).get("name")) or ""),
            str(entry.get("name") or ""),
        ),
    ):
        group = item.get("group") or {}
        rows.append(
            {
                "group": truncate(group.get("name"), 30),
                "type": text(group.get("type")),
                "name": truncate(item.get("name"), 40),
                "id": text(item.get("id")),
            }
        )
    shown = rows[: args.limit]
    print_csv(CATEGORY_COLUMNS, shown)
    note_truncation(len(shown), len(rows), "categories")
    return 0


def command_budgets(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    first = parse_month(args.month) if args.month else today().replace(day=1)
    start, end = month_bounds(first)
    month_key = first.isoformat()

    data = graphql(
        profile,
        "Common_GetJointPlanningData",
        QUERY_BUDGETS,
        {"startDate": start, "endDate": end},
    )
    budget_data = data.get("budgetData") or {}
    by_category = budget_data.get("monthlyAmountsByCategory")
    entries = (
        [item for item in by_category if isinstance(item, dict)]
        if isinstance(by_category, list)
        else []
    )

    if args.json:
        print_json(entries)
        return 0

    rows = []
    for entry in entries:
        category = entry.get("category") or {}
        group = category.get("group") or {}
        amounts = entry.get("monthlyAmounts")
        amounts = amounts if isinstance(amounts, list) else []
        for amount in amounts:
            if not isinstance(amount, dict):
                continue
            if str(amount.get("month") or "")[:7] != month_key[:7]:
                continue
            rows.append(
                {
                    "group": truncate(group.get("name"), 30),
                    "category": truncate(category.get("name"), 30),
                    "budgeted": format_amount(amount.get("plannedCashFlowAmount")),
                    "actual": format_amount(amount.get("actualAmount")),
                    "remaining": format_amount(amount.get("remainingAmount")),
                }
            )

    rows.sort(key=lambda row: (row["group"], row["category"]))
    shown = rows[: args.limit]
    print(f"month\t{month_key[:7]}", file=sys.stderr)
    print_csv(BUDGET_COLUMNS, shown)
    note_truncation(len(shown), len(rows), "budget categories")
    return 0


def command_cashflow(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    start, end = window_days(args.days)
    filters = {
        "search": "",
        "categories": [],
        "accounts": [],
        "tags": [],
        "startDate": start,
        "endDate": end,
    }

    data = graphql(profile, "Web_GetCashFlowPage", QUERY_CASHFLOW, {"filters": filters})
    if args.json:
        print_json(data)
        return 0

    summary = {}
    buckets = data.get("summary")
    if isinstance(buckets, list) and buckets and isinstance(buckets[0], dict):
        summary = buckets[0].get("summary") or {}

    rows = [
        {"scope": "total", "name": "income", "type": "-", "amount": format_amount(summary.get("sumIncome"))},
        {"scope": "total", "name": "expense", "type": "-", "amount": format_amount(summary.get("sumExpense"))},
        {"scope": "total", "name": "savings", "type": "-", "amount": format_amount(summary.get("savings"))},
    ]

    groups = data.get("byCategoryGroup")
    group_rows = []
    for entry in groups if isinstance(groups, list) else []:
        if not isinstance(entry, dict):
            continue
        group = (entry.get("groupBy") or {}).get("categoryGroup") or {}
        total = (entry.get("summary") or {}).get("sum")
        group_rows.append(
            {
                "scope": "group",
                "name": truncate(group.get("name"), 30),
                "type": text(group.get("type")),
                "amount": format_amount(total),
                "_sort": abs(to_decimal(total)),
            }
        )
    group_rows.sort(key=lambda row: row["_sort"], reverse=True)
    shown_groups = group_rows[: args.limit]
    rows.extend(shown_groups)

    print(f"window\t{start} .. {end}", file=sys.stderr)
    print_csv(CASHFLOW_COLUMNS, rows)
    note_truncation(len(shown_groups), len(group_rows), "category groups")
    return 0


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def command_holdings(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    accounts = fetch_accounts(profile)
    chosen = match_one(accounts, "displayName", args.account, "Account")
    day = today().isoformat()

    data = graphql(
        profile,
        "Web_GetHoldings",
        QUERY_HOLDINGS,
        {
            "input": {
                "accountIds": [str(chosen.get("id"))],
                "startDate": day,
                "endDate": day,
                "includeHiddenHoldings": True,
            }
        },
    )
    edges = ((data.get("portfolio") or {}).get("aggregateHoldings") or {}).get("edges")
    nodes = [
        edge.get("node")
        for edge in (edges if isinstance(edges, list) else [])
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
    ]

    if args.json:
        print_json(nodes)
        return 0

    rows = [holding_row(node) for node in nodes]
    rows.sort(key=lambda row: to_decimal(row["value"]), reverse=True)
    shown = rows[: args.limit]
    print(f"account\t{truncate(chosen.get('displayName'), 40)}", file=sys.stderr)
    print_csv(HOLDING_COLUMNS, shown)
    note_truncation(len(shown), len(rows), "holdings")
    return 0


def holding_row(node: dict) -> dict:
    security = node.get("security") or {}
    holdings = node.get("holdings")
    manual = holdings[0] if isinstance(holdings, list) and holdings and isinstance(holdings[0], dict) else {}
    price = security.get("currentPrice")
    if price in (None, ""):
        price = manual.get("closingPrice")
    return {
        "ticker": text(security.get("ticker") or manual.get("ticker")),
        "name": truncate(security.get("name") or manual.get("name"), 40),
        "quantity": format_quantity(node.get("quantity")),
        "price": format_amount(price),
        "value": format_amount(node.get("totalValue")),
    }


def add_env_option(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--env-file",
        default=default,
        help="Path to dotenv file. Defaults to the configured shared or isolated Monarch env.",
    )


def add_profile_option(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--profile",
        default=default,
        help="Monarch profile name from MONARCH_<PROFILE>_* env vars.",
    )


def add_all_profiles_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="Run the command across every configured Monarch profile.",
    )


def add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw Monarch payload. Unredacted household financial data.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monarch",
        description="Read a Monarch Money household: accounts, net worth, spending, and budgets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              monarch profiles
              monarch status --all-profiles
              monarch accounts --profile household
              monarch networth --profile household --days 90
              monarch transactions --profile household --days 30 --limit 25
              monarch transactions --profile household --account "Joint Checking"
              monarch budgets --profile household --month 2026-08
              monarch cashflow --profile household --days 30
              monarch holdings --profile household --account "Brokerage"

            Every command reads. Nothing in this package creates, edits, or deletes a
            transaction, budget, goal, holding, or account.
            """
        ),
    )

    add_env_option(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List configured Monarch profiles.")
    add_env_option(profiles, suppress_defaults=True)
    profiles.set_defaults(handler=command_profiles)

    status = subparsers.add_parser("status", help="Check each profile's login and household size.")
    add_env_option(status, suppress_defaults=True)
    add_profile_option(status, suppress_defaults=True)
    add_all_profiles_option(status)
    add_json_option(status)
    status.set_defaults(handler=command_status)

    accounts = subparsers.add_parser("accounts", help="List accounts with balances and sync times.")
    add_env_option(accounts, suppress_defaults=True)
    add_profile_option(accounts, suppress_defaults=True)
    accounts.add_argument("--limit", type=int, default=50, help="Maximum accounts to print.")
    add_json_option(accounts)
    accounts.set_defaults(handler=command_accounts)

    networth = subparsers.add_parser("networth", help="Show net worth at the ends of a window.")
    add_env_option(networth, suppress_defaults=True)
    add_profile_option(networth, suppress_defaults=True)
    networth.add_argument("--days", type=int, default=90, help="Look back this many days.")
    add_json_option(networth)
    networth.set_defaults(handler=command_networth)

    transactions = subparsers.add_parser("transactions", help="List transactions in a window.")
    add_env_option(transactions, suppress_defaults=True)
    add_profile_option(transactions, suppress_defaults=True)
    transactions.add_argument("--days", type=int, default=30, help="Look back this many days.")
    transactions.add_argument("--limit", type=int, default=25, help="Maximum transactions to print.")
    transactions.add_argument("--account", help="Restrict to one account by display name.")
    transactions.add_argument("--category", help="Restrict to one category by name.")
    add_json_option(transactions)
    transactions.set_defaults(handler=command_transactions)

    categories = subparsers.add_parser("categories", help="List transaction categories by group.")
    add_env_option(categories, suppress_defaults=True)
    add_profile_option(categories, suppress_defaults=True)
    categories.add_argument("--limit", type=int, default=200, help="Maximum categories to print.")
    add_json_option(categories)
    categories.set_defaults(handler=command_categories)

    budgets = subparsers.add_parser("budgets", help="Compare budgeted and actual amounts.")
    add_env_option(budgets, suppress_defaults=True)
    add_profile_option(budgets, suppress_defaults=True)
    budgets.add_argument("--month", help="Budget month as YYYY-MM. Defaults to the current month.")
    budgets.add_argument("--limit", type=int, default=100, help="Maximum categories to print.")
    add_json_option(budgets)
    budgets.set_defaults(handler=command_budgets)

    cashflow = subparsers.add_parser("cashflow", help="Summarize income, expense, and savings.")
    add_env_option(cashflow, suppress_defaults=True)
    add_profile_option(cashflow, suppress_defaults=True)
    cashflow.add_argument("--days", type=int, default=30, help="Look back this many days.")
    cashflow.add_argument("--limit", type=int, default=25, help="Maximum category groups to print.")
    add_json_option(cashflow)
    cashflow.set_defaults(handler=command_cashflow)

    holdings = subparsers.add_parser("holdings", help="List one investment account's holdings.")
    add_env_option(holdings, suppress_defaults=True)
    add_profile_option(holdings, suppress_defaults=True)
    holdings.add_argument("--account", required=True, help="Investment account display name.")
    holdings.add_argument("--limit", type=int, default=50, help="Maximum holdings to print.")
    add_json_option(holdings)
    holdings.set_defaults(handler=command_holdings)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    load_dotenv(resolve_env_file(getattr(args, "env_file", None)))

    try:
        handler: Callable[[argparse.Namespace], int] = args.handler
        return handler(args)
    except MonarchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
