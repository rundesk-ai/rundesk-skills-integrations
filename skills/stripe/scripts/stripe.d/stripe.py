#!/usr/bin/env python3
"""
Pull compact Stripe account, revenue, payout, and report context for review.

Usage:
  stripe profiles
  stripe status [--profile example | --all-profiles]
  stripe balance [--profile example | --all-profiles]
  stripe revenue [--profile example | --all-profiles] [--days 30]
  stripe payouts [--profile example] [--days 30] [--limit 25]
  stripe charges [--profile example] [--days 7] [--limit 25]
  stripe subscriptions [--profile example] [--status active] [--limit 25]
  stripe disputes [--profile example] [--days 30] [--limit 25]
  stripe customer CUSTOMER_ID_OR_EMAIL [--profile example]
  stripe report types [--profile example]
  stripe report run --type balance.summary.1 --start 2026-07-01 --end 2026-08-01

Inputs:
  Reads process env or an explicit/shared/isolated dotenv. Rundesk-managed accounts use
  STRIPE_<FIELD>__<PROFILE>, with the plain STRIPE_<FIELD> as the default account; the
  older STRIPE_<PROFILE>_<FIELD> keys still resolve. See references/cli.md for setup.
  Secrets must stay in an owner-only environment file.

Outputs:
  Writes compact text summaries to stdout. List output is CSV-style rows and monetary
  values are converted from Stripe minor units. No raw JSON unless --json is provided.
  Every command is read-only except `report run`, which creates a Stripe report artifact
  and can change no balance, customer, subscription, or payment.
"""

from __future__ import annotations

import argparse
import csv
import datetime
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
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


API_BASE = "https://api.stripe.com"
FILES_HOST = "files.stripe.com"
USER_AGENT = "rundesk-stripe/1.0"

# Stripe expresses amounts in the currency's smallest unit. Most currencies have two
# decimal places, but these do not, so dividing by 100 reports them 100x too small.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
        "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
)
THREE_DECIMAL_CURRENCIES = frozenset({"bhd", "jod", "kwd", "omr", "tnd"})

PAYOUT_COLUMNS = [
    "id", "amount", "currency", "status", "arrival_date", "created",
    "method", "type", "description", "profile",
]
CHARGE_COLUMNS = [
    "id", "amount", "currency", "status", "captured", "refunded",
    "created", "description", "customer", "profile",
]
SUBSCRIPTION_COLUMNS = [
    "id", "customer", "status", "amount", "currency", "interval",
    "current_period_end", "cancel_at_period_end", "profile",
]
DISPUTE_COLUMNS = [
    "id", "amount", "currency", "reason", "status", "evidence_due_by",
    "charge", "created", "profile",
]
REVENUE_COLUMNS = ["currency", "type", "count", "gross", "fee", "net", "profile"]
REPORT_TYPE_COLUMNS = ["id", "name", "data_available_start", "data_available_end", "profile"]
BALANCE_COLUMNS = ["currency", "available", "pending", "profile"]

EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
IP_CANDIDATE_PATTERN = re.compile(r"(?<![0-9A-Fa-f:.])\[?[0-9A-Fa-f:.]+\]?(?![0-9A-Fa-f:.])")


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("STRIPE_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "stripe" / "env")
    candidates.append(xdg / "stripe" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


class StripeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    key: str
    account: str
    label: str
    api_version: str

    @property
    def mode(self) -> str:
        if "_test_" in self.key:
            return "test"
        if "_live_" in self.key:
            return "live"
        return "unknown"

    @property
    def key_kind(self) -> str:
        if self.key.startswith("rk_"):
            return "restricted"
        if self.key.startswith("sk_"):
            return "secret"
        return "unknown"

    def auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.account:
            headers["Stripe-Account"] = self.account
        if self.api_version:
            headers["Stripe-Version"] = self.api_version
        return headers


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
    "STRIPE_API_KEY": "KEY",
    "STRIPE_ACCOUNT": "ACCOUNT",
    "STRIPE_API_VERSION": "API_VERSION",
    "STRIPE_LABEL": "LABEL",
}
REQUIRED_FIELDS = ("STRIPE_API_KEY",)
# Bare names an older dotenv may still use for the single default account.
PLAIN_ALIASES = {"STRIPE_API_KEY": ("STRIPE_SECRET_KEY",)}
# A Rundesk account suffix: uppercase words joined by single underscores, because a
# double underscore is what separates the field name from the account name.
ACCOUNT_SUFFIX_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")
# A profile named `api` or `secret` would be indistinguishable from the conventional
# STRIPE_API_KEY / STRIPE_SECRET_KEY single-account variables during discovery, and
# `default` and `env` name the default account and the STRIPE_ENV_FILE setting.
RESERVED_PROFILE_WORDS = frozenset({"API", "SECRET", "DEFAULT", "ENV"})


def normalize_profile(profile: str) -> str:
    """A profile name as an environment-variable fragment: `platform-sub` to `PLATFORM_SUB`."""
    return re.sub(r"[^A-Za-z0-9]+", "_", profile or "").strip("_").upper()


def profile_label(suffix: str) -> str:
    """The inverse of `normalize_profile`, so a discovered account reads as a profile name."""
    return suffix.lower().replace("_", "-")


def env_name(profile: str, suffix: str) -> str:
    return f"STRIPE_{normalize_profile(profile)}_{suffix}"


def is_default_profile(profile: str) -> bool:
    """Rundesk stores the default account under the plain, unsuffixed variable names."""
    normalized = normalize_profile(profile)
    if not normalized or normalized == "DEFAULT":
        return True
    return normalized == normalize_profile(os.environ.get("STRIPE_DEFAULT_PROFILE", ""))


def missing_name(profile: str, field: str) -> str:
    """The variable an owner must set, spelled the way Rundesk stores it."""
    return field if is_default_profile(profile) else f"{field}__{normalize_profile(profile)}"


def profile_value(profile: str, field: str) -> str:
    """Read one field for one profile.

    Rundesk's `<FIELD>__<PROFILE>` wins, then this repository's `STRIPE_<PROFILE>_<FIELD>`,
    then the plain `<FIELD>` and its legacy aliases — which belong to the default account
    only, so a named account never pairs one business's key with another's connected
    account id.
    """
    normalized = normalize_profile(profile)
    if normalized:
        for name in (f"{field}__{normalized}", env_name(profile, PROFILE_FIELDS[field])):
            value = os.environ.get(name, "")
            if value:
                return value
    if not is_default_profile(profile):
        return ""
    for name in (field, *PLAIN_ALIASES.get(field, ())):
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("STRIPE_PROFILES"))
    default = os.environ.get("STRIPE_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    """Accounts present in the environment, so adding one needs no declaration.

    Both spellings are scanned: Rundesk's `<FIELD>__<ACCOUNT>` suffix and this
    repository's `STRIPE_<PROFILE>_<FIELD>` infix.

    The plain names are one more account — the default one — listed even when only
    partly configured, so it carries its own error instead of vanishing. It is
    suppressed when the infix spelling is in use: there a plain value was a fallback
    shared by every profile, not an account of its own, and inventing one would make
    every command ambiguous for an owner whose dotenv predates Rundesk.
    """
    suffixed: set[str] = set()
    infixed: set[str] = set()
    legacy = re.compile(
        rf"^STRIPE_({ACCOUNT_SUFFIX_RE.pattern})_({'|'.join(PROFILE_FIELDS.values())})$"
    )
    for key in os.environ:
        for field in PROFILE_FIELDS:
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
    # A plain name, or its legacy alias, configures the default account. List it even when
    # only some fields are set, so a partly configured default carries its own error
    # instead of vanishing from `profiles`.
    names = suffixed | infixed
    # The plain names are the default account. The infix spelling predates that idea and
    # treated a plain value as a fallback shared by every profile, so an environment
    # written that way gets no invented `default` account to make selection ambiguous.
    if not infixed and any(profile_value("", field) for field in REQUIRED_FIELDS):
        names.add(os.environ.get("STRIPE_DEFAULT_PROFILE") or "default")
    return sorted(names)


def validate_account(value: str) -> str:
    if value and not re.fullmatch(r"acct_[A-Za-z0-9]+", value):
        raise StripeError(
            f"Invalid connected account id: {value!r}. Use the acct_... id of the connected account."
        )
    return value


def validate_key(name: str, key: str) -> str:
    if key.startswith("pk_"):
        raise StripeError(
            f"Profile {name!r} is configured with a publishable key. "
            "Publishable keys cannot read account data; use a restricted key (rk_...)."
        )
    if key.startswith("whsec_"):
        raise StripeError(
            f"Profile {name!r} is configured with a webhook signing secret, not an API key."
        )
    return key


def get_profile(name: str) -> Profile:
    key = profile_value(name, "STRIPE_API_KEY")
    if not key:
        raise StripeError(
            f"Missing Stripe config: {missing_name(name, 'STRIPE_API_KEY')}. "
            "Run `rundesk skills configure`, add it to the secrets dotenv, or export it in the shell."
        )

    return Profile(
        name=name,
        key=validate_key(name, key),
        account=validate_account(profile_value(name, "STRIPE_ACCOUNT").strip()),
        label=profile_value(name, "STRIPE_LABEL") or name,
        api_version=profile_value(name, "STRIPE_API_VERSION").strip(),
    )


def selected_profile_name(args: argparse.Namespace) -> str:
    profile_name = getattr(args, "profile", None) or os.environ.get("STRIPE_DEFAULT_PROFILE", "")
    if profile_name:
        return profile_name

    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if names:
        raise StripeError(
            "Multiple Stripe profiles configured. Pass --profile or set STRIPE_DEFAULT_PROFILE."
        )
    raise StripeError("No Stripe profile selected. Pass --profile or set STRIPE_DEFAULT_PROFILE.")


def selected_profiles(args: argparse.Namespace) -> list[Profile]:
    if getattr(args, "all_profiles", False):
        names = configured_profile_names()
        if not names:
            raise StripeError(
                "No Stripe profiles configured. Run `rundesk skills configure`, or set "
                "STRIPE_PROFILES and STRIPE_DEFAULT_PROFILE in .env."
            )
        return [get_profile(name) for name in names]
    return [get_profile(selected_profile_name(args))]


def redact_sensitive(value: str) -> str:
    """Redact email and IP values from human-readable output."""
    value = EMAIL_PATTERN.sub("[redacted-email]", value)

    def replace_ip(match: re.Match[str]) -> str:
        candidate = match.group(0)
        unwrapped = (
            candidate[1:-1]
            if candidate.startswith("[") and candidate.endswith("]")
            else candidate
        )
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


def truncate(value: Any, limit: int = 120) -> str:
    value = text(value)
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."


def currency_exponent(currency: str) -> int:
    code = (currency or "").lower()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def format_amount(minor: Any, currency: str) -> str:
    """Convert a Stripe minor-unit integer to its major-unit decimal string."""
    if minor is None:
        return "-"
    try:
        units = Decimal(int(minor))
    except (TypeError, ValueError):
        return text(minor)

    exponent = currency_exponent(currency)
    if exponent == 0:
        return f"{units}"
    scaled = units / (Decimal(10) ** exponent)
    return f"{scaled:.{exponent}f}"


def compact_date(value: Any) -> str:
    """Format a Stripe unix timestamp as UTC `YYYY-MM-DD HH:MM`."""
    if value in (None, ""):
        return "-"
    try:
        moment = datetime.datetime.fromtimestamp(int(value), tz=datetime.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return text(value)
    return moment.strftime("%Y-%m-%d %H:%M")


def parse_day(value: str, label: str) -> int:
    """Parse YYYY-MM-DD as that day's UTC midnight, returned as a unix timestamp."""
    try:
        day = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise StripeError(f"Invalid --{label} date {value!r}. Use YYYY-MM-DD.") from exc
    return int(day.replace(tzinfo=datetime.timezone.utc).timestamp())


def days_ago(days: int) -> int:
    if days < 1:
        raise StripeError("--days must be at least 1.")
    return int(time.time()) - days * 86400


def print_csv(columns: list[str], rows: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: text(row.get(column)) for column in columns})


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def print_kv(data: dict[str, Any], keys: list[str] | None = None) -> None:
    for key in keys if keys is not None else list(data):
        value = data.get(key)
        if keys is None and isinstance(value, (dict, list)):
            continue
        print(f"{key}\t{text(value)}")


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


def error_message(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        parts = [error.get("message"), error.get("code"), error.get("type")]
        return " | ".join(str(part) for part in parts if part)
    return raw[:500]


def request(
    profile: Profile,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    retries: int = 2,
) -> Any:
    url = API_BASE + "/v1/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    headers = profile.auth_headers()
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with open_url(req, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(delay, 30))
                continue
            raise StripeError(
                f"Stripe API {exc.code} profile={profile.name}: {error_message(raw)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise StripeError(
                f"Stripe API request failed profile={profile.name}: {exc.reason}"
            ) from exc

    raise StripeError(f"Stripe API request exhausted retries profile={profile.name}")


def list_objects(
    profile: Profile,
    path: str,
    params: dict[str, Any] | None = None,
    limit: int = 25,
) -> tuple[list[dict[str, Any]], bool]:
    """Page through a Stripe list endpoint up to `limit`, reporting whether more remain."""
    if limit < 1:
        raise StripeError("--limit must be at least 1.")

    collected: list[dict[str, Any]] = []
    starting_after: str | None = None
    has_more = False

    while len(collected) < limit:
        page_params = dict(params or {})
        page_params["limit"] = min(100, limit - len(collected))
        if starting_after:
            page_params["starting_after"] = starting_after

        payload = request(profile, "GET", path, params=page_params)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            has_more = False
            break

        collected.extend(item for item in items if isinstance(item, dict))
        has_more = bool(payload.get("has_more"))
        if not has_more or not collected:
            break

        starting_after = collected[-1].get("id")
        if not starting_after:
            break

    return collected[:limit], has_more and len(collected) >= limit


def note_truncation(truncated: bool, limit: int, what: str) -> None:
    if truncated:
        print(
            f"note: more {what} exist beyond the {limit} shown; raise --limit or narrow the window.",
            file=sys.stderr,
        )


def command_profiles(args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print(
            "No Stripe profiles configured. Run `rundesk skills configure`, or set "
            "STRIPE_PROFILES and STRIPE_DEFAULT_PROFILE in .env."
        )
        return 0

    print("Stripe profiles")
    for name in names:
        try:
            profile = get_profile(name)
            print(
                "- "
                + " | ".join(
                    [
                        f"profile={profile.name}",
                        f"label={profile.label}",
                        f"mode={profile.mode}",
                        f"key={profile.key_kind}",
                        f"account={profile.account or 'own'}",
                        f"api_version={profile.api_version or 'account-default'}",
                    ]
                )
            )
        except StripeError as exc:
            print(f"- profile={name} | error={exc}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    for profile in selected_profiles(args):
        account = request(profile, "GET", "account")
        if args.json:
            print_json(account)
            continue

        business = account.get("business_profile") or {}
        print(f"profile\t{profile.name}")
        print(f"label\t{profile.label}")
        print(f"mode\t{profile.mode}")
        print_kv(
            {
                "account": account.get("id"),
                "name": business.get("name") or account.get("settings", {}).get(
                    "dashboard", {}
                ).get("display_name"),
                "country": account.get("country"),
                "default_currency": account.get("default_currency"),
                "charges_enabled": account.get("charges_enabled"),
                "payouts_enabled": account.get("payouts_enabled"),
                "details_submitted": account.get("details_submitted"),
            },
            [
                "account", "name", "country", "default_currency",
                "charges_enabled", "payouts_enabled", "details_submitted",
            ],
        )
        print()
    return 0


def command_balance(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    for profile in selected_profiles(args):
        balance = request(profile, "GET", "balance")
        payloads.append({"profile": profile.name, "balance": balance})

        totals: dict[str, dict[str, Any]] = {}
        for bucket in ("available", "pending"):
            for entry in balance.get(bucket) or []:
                if not isinstance(entry, dict):
                    continue
                currency = str(entry.get("currency") or "")
                slot = totals.setdefault(
                    currency, {"currency": currency, "profile": profile.name}
                )
                slot[bucket] = format_amount(entry.get("amount"), currency)
        rows.extend(totals[key] for key in sorted(totals))

    if args.json:
        print_json(payloads)
        return 0

    print_csv(BALANCE_COLUMNS, rows)
    return 0


def revenue_rows(
    profile: Profile, start: int, end: int, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    transactions, truncated = list_objects(
        profile,
        "balance_transactions",
        {"created[gte]": start, "created[lt]": end},
        limit=limit,
    )

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for item in transactions:
        currency = str(item.get("currency") or "")
        kind = str(item.get("type") or "unknown")
        slot = buckets.setdefault(
            (currency, kind),
            {
                "currency": currency,
                "type": kind,
                "count": 0,
                "gross_minor": 0,
                "fee_minor": 0,
                "net_minor": 0,
                "profile": profile.name,
            },
        )
        slot["count"] += 1
        for field, target in (("amount", "gross_minor"), ("fee", "fee_minor"), ("net", "net_minor")):
            try:
                slot[target] += int(item.get(field) or 0)
            except (TypeError, ValueError):
                continue

    rows: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Any]] = {}
    for key in sorted(buckets):
        slot = buckets[key]
        currency = slot["currency"]
        total = totals.setdefault(
            currency,
            {
                "currency": currency,
                "type": "TOTAL",
                "count": 0,
                "gross_minor": 0,
                "fee_minor": 0,
                "net_minor": 0,
                "profile": profile.name,
            },
        )
        for field in ("count", "gross_minor", "fee_minor", "net_minor"):
            total[field] += slot[field]
        rows.append(slot)

    rows.extend(totals[key] for key in sorted(totals))
    for row in rows:
        currency = row["currency"]
        row["gross"] = format_amount(row.pop("gross_minor"), currency)
        row["fee"] = format_amount(row.pop("fee_minor"), currency)
        row["net"] = format_amount(row.pop("net_minor"), currency)
    return rows, truncated


def command_revenue(args: argparse.Namespace) -> int:
    start, end = window_bounds(args)
    rows: list[dict[str, Any]] = []
    truncated = False
    for profile in selected_profiles(args):
        profile_rows, profile_truncated = revenue_rows(profile, start, end, args.limit)
        rows.extend(profile_rows)
        truncated = truncated or profile_truncated

    if args.json:
        print_json(rows)
    else:
        print(
            f"window\t{compact_date(start)} .. {compact_date(end)} UTC",
            file=sys.stderr,
        )
        print_csv(REVENUE_COLUMNS, rows)
    note_truncation(truncated, args.limit, "balance transactions")
    return 0


def window_bounds(args: argparse.Namespace) -> tuple[int, int]:
    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    if start or end:
        if not (start and end):
            raise StripeError("Pass both --start and --end, or use --days instead.")
        first, last = parse_day(start, "start"), parse_day(end, "end")
        if first >= last:
            raise StripeError("--start must be before --end.")
        return first, last
    return days_ago(args.days), int(time.time())


def payout_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    currency = str(item.get("currency") or "")
    return {
        "id": item.get("id"),
        "amount": format_amount(item.get("amount"), currency),
        "currency": currency,
        "status": item.get("status"),
        "arrival_date": compact_date(item.get("arrival_date")),
        "created": compact_date(item.get("created")),
        "method": item.get("method"),
        "type": item.get("type"),
        "description": truncate(item.get("description"), 60),
        "profile": profile.name,
    }


def command_payouts(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    params: dict[str, Any] = {"created[gte]": days_ago(args.days)}
    if args.status:
        params["status"] = args.status

    items, truncated = list_objects(profile, "payouts", params, limit=args.limit)
    if args.json:
        print_json(items)
    else:
        print_csv(PAYOUT_COLUMNS, [payout_row(item, profile) for item in items])
    note_truncation(truncated, args.limit, "payouts")
    return 0


def charge_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    currency = str(item.get("currency") or "")
    return {
        "id": item.get("id"),
        "amount": format_amount(item.get("amount"), currency),
        "currency": currency,
        "status": item.get("status"),
        "captured": item.get("captured"),
        "refunded": item.get("refunded"),
        "created": compact_date(item.get("created")),
        "description": truncate(item.get("description"), 60),
        "customer": item.get("customer"),
        "profile": profile.name,
    }


def command_charges(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    items, truncated = list_objects(
        profile, "charges", {"created[gte]": days_ago(args.days)}, limit=args.limit
    )
    if args.json:
        print_json(items)
    else:
        print_csv(CHARGE_COLUMNS, [charge_row(item, profile) for item in items])
    note_truncation(truncated, args.limit, "charges")
    return 0


def subscription_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    items = ((item.get("items") or {}).get("data") or [])
    price = items[0].get("price") if items and isinstance(items[0], dict) else None
    price = price if isinstance(price, dict) else {}
    recurring = price.get("recurring") if isinstance(price.get("recurring"), dict) else {}
    currency = str(price.get("currency") or item.get("currency") or "")
    interval = recurring.get("interval")
    count = recurring.get("interval_count")
    return {
        "id": item.get("id"),
        "customer": item.get("customer"),
        "status": item.get("status"),
        "amount": format_amount(price.get("unit_amount"), currency),
        "currency": currency,
        "interval": f"{count}{interval}" if interval and count and count != 1 else (interval or "-"),
        "current_period_end": compact_date(item.get("current_period_end")),
        "cancel_at_period_end": item.get("cancel_at_period_end"),
        "profile": profile.name,
    }


def command_subscriptions(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    items, truncated = list_objects(
        profile, "subscriptions", {"status": args.status}, limit=args.limit
    )
    if args.json:
        print_json(items)
    else:
        print_csv(SUBSCRIPTION_COLUMNS, [subscription_row(item, profile) for item in items])
    note_truncation(truncated, args.limit, "subscriptions")
    return 0


def dispute_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    currency = str(item.get("currency") or "")
    evidence = item.get("evidence_details") or {}
    return {
        "id": item.get("id"),
        "amount": format_amount(item.get("amount"), currency),
        "currency": currency,
        "reason": item.get("reason"),
        "status": item.get("status"),
        "evidence_due_by": compact_date(evidence.get("due_by")),
        "charge": item.get("charge"),
        "created": compact_date(item.get("created")),
        "profile": profile.name,
    }


def command_disputes(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    items, truncated = list_objects(
        profile, "disputes", {"created[gte]": days_ago(args.days)}, limit=args.limit
    )
    if args.json:
        print_json(items)
    else:
        print_csv(DISPUTE_COLUMNS, [dispute_row(item, profile) for item in items])
    note_truncation(truncated, args.limit, "disputes")
    return 0


def command_customer(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    identifier = args.customer.strip()

    if identifier.startswith("cus_"):
        customer = request(profile, "GET", f"customers/{urllib.parse.quote(identifier)}")
    elif "@" in identifier:
        matches, _ = list_objects(profile, "customers", {"email": identifier}, limit=5)
        if not matches:
            print(f"No customer matches email on profile {profile.name}.")
            return 0
        if len(matches) > 1:
            print(f"note: {len(matches)} customers share this email; showing the first.", file=sys.stderr)
        customer = matches[0]
    else:
        raise StripeError(
            f"Unrecognized customer reference {identifier!r}. Pass a cus_... id or an email address."
        )

    if args.json:
        print_json(customer)
        return 0

    address = customer.get("address") or {}
    print_kv(
        {
            "id": customer.get("id"),
            "name": customer.get("name"),
            "email": customer.get("email"),
            "created": compact_date(customer.get("created")),
            "currency": customer.get("currency"),
            "delinquent": customer.get("delinquent"),
            "country": address.get("country"),
            "description": truncate(customer.get("description"), 100),
            "profile": profile.name,
        },
        [
            "id", "name", "email", "created", "currency",
            "delinquent", "country", "description", "profile",
        ],
    )

    subscriptions, _ = list_objects(
        profile,
        "subscriptions",
        {"customer": customer.get("id"), "status": "all"},
        limit=args.subscription_limit,
    )
    if subscriptions:
        print()
        print_csv(SUBSCRIPTION_COLUMNS, [subscription_row(item, profile) for item in subscriptions])
    return 0


def command_report_types(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    items, truncated = list_objects(profile, "reporting/report_types", limit=args.limit)
    if args.json:
        print_json(items)
        return 0

    rows = [
        {
            "id": item.get("id"),
            "name": truncate(item.get("name"), 60),
            "data_available_start": compact_date(item.get("data_available_start")),
            "data_available_end": compact_date(item.get("data_available_end")),
            "profile": profile.name,
        }
        for item in items
    ]
    print_csv(REPORT_TYPE_COLUMNS, rows)
    note_truncation(truncated, args.limit, "report types")
    return 0


def download_report(profile: Profile, url: str, destination: Path) -> int:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != FILES_HOST:
        raise StripeError(f"Refusing to download report from unexpected host: {url}")
    if destination.exists():
        raise StripeError(f"Refusing to overwrite existing file: {destination}")

    req = urllib.request.Request(url, headers=profile.auth_headers(), method="GET")
    try:
        with open_url(req, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise StripeError(f"Stripe file download {exc.code}: {error_message(raw)}") from exc
    except urllib.error.URLError as exc:
        raise StripeError(f"Stripe file download failed: {exc.reason}") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return len(payload)


def command_report_run(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    start, end = window_bounds(args)

    form: dict[str, Any] = {
        "report_type": args.type,
        "parameters[interval_start]": start,
        "parameters[interval_end]": end,
    }
    if args.timezone:
        form["parameters[timezone]"] = args.timezone
    if args.currency:
        form["parameters[currency]"] = args.currency
    if args.column:
        form["parameters[columns][]"] = args.column

    run = request(profile, "POST", "reporting/report_runs", form=form)
    run_id = run.get("id")
    deadline = time.time() + args.timeout

    while run.get("status") == "pending" and time.time() < deadline:
        time.sleep(min(args.poll_interval, max(1, int(deadline - time.time()))))
        run = request(profile, "GET", f"reporting/report_runs/{urllib.parse.quote(str(run_id))}")

    if args.json:
        print_json(run)
        return 0 if run.get("status") == "succeeded" else 1

    result = run.get("result") or {}
    print_kv(
        {
            "run": run_id,
            "report_type": args.type,
            "status": run.get("status"),
            "window": f"{compact_date(start)} .. {compact_date(end)} UTC",
            "file": result.get("id"),
            "size": result.get("size"),
            "profile": profile.name,
        },
        ["run", "report_type", "status", "window", "file", "size", "profile"],
    )

    if run.get("status") == "pending":
        print(
            f"error: report run {run_id} still pending after {args.timeout}s; "
            f"retrieve it later with `report status {run_id}`.",
            file=sys.stderr,
        )
        return 1
    if run.get("status") != "succeeded":
        print(f"error: report run {run_id} finished with status {run.get('status')}.", file=sys.stderr)
        return 1

    if args.out:
        written = download_report(profile, str(result.get("url") or ""), Path(args.out).expanduser())
        print(f"saved\t{args.out} ({written} bytes)")
    else:
        print("note: pass --out PATH to download the CSV; the result URL requires the same API key.")
    return 0


def command_report_status(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    run = request(profile, "GET", f"reporting/report_runs/{urllib.parse.quote(args.run)}")
    if args.json:
        print_json(run)
        return 0

    result = run.get("result") or {}
    print_kv(
        {
            "run": run.get("id"),
            "report_type": run.get("report_type"),
            "status": run.get("status"),
            "created": compact_date(run.get("created")),
            "file": result.get("id"),
            "size": result.get("size"),
            "profile": profile.name,
        },
        ["run", "report_type", "status", "created", "file", "size", "profile"],
    )
    if run.get("status") == "succeeded" and args.out:
        written = download_report(profile, str(result.get("url") or ""), Path(args.out).expanduser())
        print(f"saved\t{args.out} ({written} bytes)")
    return 0 if run.get("status") == "succeeded" else 1


def add_env_option(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--env-file",
        default=default,
        help="Path to dotenv file. Defaults to the configured shared or isolated Stripe env.",
    )


def add_profile_option(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--profile",
        default=default,
        help="Stripe account name, from STRIPE_<FIELD>__<PROFILE> or STRIPE_<PROFILE>_<FIELD> env vars.",
    )


def add_all_profiles_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="Run the command across every configured Stripe profile.",
    )


def add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw Stripe JSON. Unredacted; may contain customer data.",
    )


def add_window_options(parser: argparse.ArgumentParser, default_days: int) -> None:
    parser.add_argument("--days", type=int, default=default_days, help="Look back this many days.")
    parser.add_argument("--start", help="Window start as YYYY-MM-DD UTC. Requires --end.")
    parser.add_argument("--end", help="Window end as YYYY-MM-DD UTC, exclusive. Requires --start.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stripe",
        description="Pull compact Stripe account, revenue, payout, and report context for review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              stripe profiles
              stripe balance --all-profiles
              stripe revenue --profile example --days 30
              stripe payouts --profile example --days 30 --limit 10
              stripe subscriptions --profile example --status active --limit 10
              stripe disputes --profile example --days 30
              stripe report types --profile example
              stripe report run --profile example --type balance.summary.1 \\
                --start 2026-07-01 --end 2026-08-01 --out ./balance.csv

            Every command reads Stripe. None of them refunds, cancels, or moves money.
            """
        ),
    )

    add_env_option(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List configured Stripe profiles.")
    add_env_option(profiles, suppress_defaults=True)
    profiles.set_defaults(handler=command_profiles)

    status = subparsers.add_parser("status", help="Show the account each profile reaches.")
    add_env_option(status, suppress_defaults=True)
    add_profile_option(status, suppress_defaults=True)
    add_all_profiles_option(status)
    add_json_option(status)
    status.set_defaults(handler=command_status)

    balance = subparsers.add_parser("balance", help="Show available and pending balance by currency.")
    add_env_option(balance, suppress_defaults=True)
    add_profile_option(balance, suppress_defaults=True)
    add_all_profiles_option(balance)
    add_json_option(balance)
    balance.set_defaults(handler=command_balance)

    revenue = subparsers.add_parser(
        "revenue", help="Roll up balance transactions into gross, fee, and net by currency and type."
    )
    add_env_option(revenue, suppress_defaults=True)
    add_profile_option(revenue, suppress_defaults=True)
    add_all_profiles_option(revenue)
    add_window_options(revenue, default_days=30)
    revenue.add_argument("--limit", type=int, default=1000, help="Maximum balance transactions to read.")
    add_json_option(revenue)
    revenue.set_defaults(handler=command_revenue)

    payouts = subparsers.add_parser("payouts", help="List recent payouts.")
    add_env_option(payouts, suppress_defaults=True)
    add_profile_option(payouts, suppress_defaults=True)
    payouts.add_argument("--days", type=int, default=30, help="Look back this many days.")
    payouts.add_argument("--status", help="Filter by payout status, e.g. paid, pending, failed.")
    payouts.add_argument("--limit", type=int, default=25, help="Maximum payouts to print.")
    add_json_option(payouts)
    payouts.set_defaults(handler=command_payouts)

    charges = subparsers.add_parser("charges", help="List recent charges.")
    add_env_option(charges, suppress_defaults=True)
    add_profile_option(charges, suppress_defaults=True)
    charges.add_argument("--days", type=int, default=7, help="Look back this many days.")
    charges.add_argument("--limit", type=int, default=25, help="Maximum charges to print.")
    add_json_option(charges)
    charges.set_defaults(handler=command_charges)

    subscriptions = subparsers.add_parser("subscriptions", help="List subscriptions by status.")
    add_env_option(subscriptions, suppress_defaults=True)
    add_profile_option(subscriptions, suppress_defaults=True)
    subscriptions.add_argument(
        "--status",
        default="active",
        help="Stripe subscription status filter, or 'all'. Defaults to active.",
    )
    subscriptions.add_argument("--limit", type=int, default=25, help="Maximum subscriptions to print.")
    add_json_option(subscriptions)
    subscriptions.set_defaults(handler=command_subscriptions)

    disputes = subparsers.add_parser("disputes", help="List recent disputes and chargebacks.")
    add_env_option(disputes, suppress_defaults=True)
    add_profile_option(disputes, suppress_defaults=True)
    disputes.add_argument("--days", type=int, default=30, help="Look back this many days.")
    disputes.add_argument("--limit", type=int, default=25, help="Maximum disputes to print.")
    add_json_option(disputes)
    disputes.set_defaults(handler=command_disputes)

    customer = subparsers.add_parser("customer", help="Show one customer and its subscriptions.")
    add_env_option(customer, suppress_defaults=True)
    add_profile_option(customer, suppress_defaults=True)
    customer.add_argument("customer", metavar="CUSTOMER_ID_OR_EMAIL")
    customer.add_argument(
        "--subscription-limit", type=int, default=10, help="Maximum subscriptions to print."
    )
    add_json_option(customer)
    customer.set_defaults(handler=command_customer)

    report = subparsers.add_parser("report", help="List, run, and retrieve Stripe reports.")
    report_commands = report.add_subparsers(dest="report_command", required=True)

    report_types = report_commands.add_parser("types", help="List report types this account can run.")
    add_env_option(report_types, suppress_defaults=True)
    add_profile_option(report_types, suppress_defaults=True)
    report_types.add_argument("--limit", type=int, default=50, help="Maximum report types to print.")
    add_json_option(report_types)
    report_types.set_defaults(handler=command_report_types)

    report_run = report_commands.add_parser("run", help="Create a report run and wait for its CSV.")
    add_env_option(report_run, suppress_defaults=True)
    add_profile_option(report_run, suppress_defaults=True)
    report_run.add_argument("--type", required=True, help="Report type id, e.g. balance.summary.1.")
    add_window_options(report_run, default_days=30)
    report_run.add_argument("--timezone", help="IANA timezone for the report. Defaults to UTC.")
    report_run.add_argument("--currency", help="Restrict the report to one currency.")
    report_run.add_argument("--column", action="append", help="Report column to include. Repeatable.")
    report_run.add_argument("--out", help="Write the CSV here. Refuses to overwrite an existing file.")
    report_run.add_argument("--timeout", type=int, default=180, help="Seconds to wait for completion.")
    report_run.add_argument("--poll-interval", type=int, default=5, help="Seconds between status polls.")
    add_json_option(report_run)
    report_run.set_defaults(handler=command_report_run)

    report_status = report_commands.add_parser("status", help="Retrieve an earlier report run.")
    add_env_option(report_status, suppress_defaults=True)
    add_profile_option(report_status, suppress_defaults=True)
    report_status.add_argument("run", metavar="REPORT_RUN_ID")
    report_status.add_argument("--out", help="Write the CSV here if the run succeeded.")
    add_json_option(report_status)
    report_status.set_defaults(handler=command_report_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    load_dotenv(resolve_env_file(getattr(args, "env_file", None)))

    try:
        handler: Callable[[argparse.Namespace], int] = args.handler
        return handler(args)
    except StripeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
