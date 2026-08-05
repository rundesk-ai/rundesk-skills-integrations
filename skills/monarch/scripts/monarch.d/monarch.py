#!/usr/bin/env python3
"""
Read a Monarch Money household, and edit the parts of it the owner approved.

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
  monarch rules [--profile household] [--limit 100]
  monarch edit TXN... [--category NAME] [--merchant NAME] [--note TEXT]
                      [--confirm] [--max 50]
  monarch tag TXN... [--add NAME] [--remove NAME] [--confirm] [--max 50]
  monarch category create --name NAME --group GROUP [--confirm]
  monarch rule create --merchant-contains TEXT --category NAME [--confirm]
  monarch rule delete RULE_ID [--confirm]
  monarch budget set --category NAME --month 2026-08 --amount N [--confirm]
  monarch undo --list
  monarch undo BATCH [--confirm]

Inputs:
  Reads process env or an explicit/shared/isolated dotenv. Rundesk-managed accounts use
  MONARCH_<FIELD>__<PROFILE>, with the plain MONARCH_<FIELD> as the default account; the
  older MONARCH_<PROFILE>_<FIELD> keys still resolve. See references/cli.md for setup.
  Monarch issues no scoped or read-only key, so these values are full account access and
  must stay in an owner-only environment file.

Outputs:
  Writes compact text summaries to stdout. List output is CSV-style rows, account mask
  digits are redacted to the last two, and bounded reads report `showing N of M` on
  stderr. No raw JSON unless --json is provided.

Writes:
  Every write previews and sends nothing until an exact --confirm, is refused above a
  50-target cap unless --max raises it, is read back after it lands, and is journalled so
  `undo` can put it back. Only the operations in MUTATIONS can be sent. Nothing here
  changes an amount, a date, an account, or a pending state; nothing deletes or splits a
  transaction; nothing deletes a category.
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
# Aliased: `Change` has an attribute called `field`, and two meanings for one name in one
# class body is how a reader loses the thread.
from dataclasses import dataclass, field as dataclass_field
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
# `id` leads, because a write command can only name a transaction the read already named.
TRANSACTION_COLUMNS = ["id", "date", "merchant", "category", "account", "amount", "pending"]
CATEGORY_COLUMNS = ["group", "type", "name", "id"]
BUDGET_COLUMNS = ["group", "category", "budgeted", "actual", "remaining"]
CASHFLOW_COLUMNS = ["scope", "name", "type", "amount"]
HOLDING_COLUMNS = ["ticker", "name", "quantity", "price", "value"]
GROUP_COLUMNS = ["type", "name", "id"]
TAG_COLUMNS = ["name", "id", "transactions"]
RULE_COLUMNS = ["id", "matches", "sets", "applied"]
CHANGE_COLUMNS = ["target", "what", "field", "before", "after"]
BATCH_COLUMNS = ["batch", "when", "profile", "changes", "state"]

# A write over this many targets is refused unless --max raises it in that one run.
DEFAULT_BULK_CAP = 50

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


QUERY_TRANSACTION = """
query GetTransactionDrawer($id: UUID!, $redirectPosted: Boolean) {
  getTransaction(id: $id, redirectPosted: $redirectPosted) {
    id
    date
    amount
    pending
    notes
    category { id name }
    merchant { id name }
    account { id displayName }
    tags { id name }
  }
}
"""

QUERY_CATEGORY_GROUPS = """
query ManageGetCategoryGroups {
  categoryGroups {
    id
    name
    order
    type
  }
}
"""

QUERY_TAGS = """
query GetHouseholdTransactionTags($search: String, $limit: Int) {
  householdTransactionTags(search: $search, limit: $limit) {
    id
    name
    color
    order
    transactionCount
  }
}
"""

QUERY_RULES = """
query GetTransactionRules {
  transactionRules {
    id
    order
    merchantCriteriaUseOriginalStatement
    merchantCriteria { operator value }
    merchantNameCriteria { operator value }
    amountCriteria { operator isExpense value }
    categoryIds
    accountIds
    setCategoryAction { id name }
    setMerchantAction { id name }
    addTagsAction { id name }
    setHideFromReportsAction
    reviewStatusAction
    recentApplicationCount
    lastAppliedAt
  }
}
"""

# ---------------------------------------------------------------------------
# The write surface.
#
# `UpdateTransactionMutationInput` also accepts amount, date, accountId, and
# hideFromReports. This package sends none of them: `edit` assembles its input from
# exactly three flags, and there is no flag, branch, or fall-through that reaches a
# fourth. The offline suite asserts it, and asserts that no document here deletes a
# transaction or a category or splits one.
# ---------------------------------------------------------------------------

MUTATION_UPDATE_TRANSACTION = """
mutation Web_TransactionDrawerUpdateTransaction($input: UpdateTransactionMutationInput!) {
  updateTransaction(input: $input) {
    transaction {
      id
      notes
      category { id name }
      merchant { id name }
    }
    errors { message code fieldErrors { field messages } }
  }
}
"""

MUTATION_SET_TAGS = """
mutation Web_SetTransactionTags($input: SetTransactionTagsInput!) {
  setTransactionTags(input: $input) {
    transaction { id tags { id name } }
    errors { message code fieldErrors { field messages } }
  }
}
"""

MUTATION_CREATE_CATEGORY = """
mutation Web_CreateCategory($input: CreateCategoryInput!) {
  createCategory(input: $input) {
    category { id name group { id name type } }
    errors { message code fieldErrors { field messages } }
  }
}
"""

MUTATION_CREATE_RULE = """
mutation Common_CreateTransactionRuleMutationV2($input: CreateTransactionRuleInput!) {
  createTransactionRuleV2(input: $input) {
    errors { message code fieldErrors { field messages } }
  }
}
"""

MUTATION_DELETE_RULE = """
mutation Common_DeleteTransactionRule($id: ID!) {
  deleteTransactionRule(id: $id) {
    deleted
    errors { message code fieldErrors { field messages } }
  }
}
"""

MUTATION_SET_BUDGET = """
mutation Common_UpdateBudgetItem($input: UpdateOrCreateBudgetItemMutationInput!) {
  updateOrCreateBudgetItem(input: $input) {
    budgetItem { id budgetAmount }
  }
}
"""

# The allowlist. `mutate` sends nothing that is not a value here, so adding a verb that
# writes to a live household means editing this mapping — which is the reviewed change
# the repository's rules require, not an incidental one.
MUTATIONS = {
    "Web_TransactionDrawerUpdateTransaction": MUTATION_UPDATE_TRANSACTION,
    "Web_SetTransactionTags": MUTATION_SET_TAGS,
    "Web_CreateCategory": MUTATION_CREATE_CATEGORY,
    "Common_CreateTransactionRuleMutationV2": MUTATION_CREATE_RULE,
    "Common_DeleteTransactionRule": MUTATION_DELETE_RULE,
    "Common_UpdateBudgetItem": MUTATION_SET_BUDGET,
}

# Where each mutation's payload — and so its per-field errors — lives in the response.
MUTATION_ROOTS = {
    "Web_TransactionDrawerUpdateTransaction": "updateTransaction",
    "Web_SetTransactionTags": "setTransactionTags",
    "Web_CreateCategory": "createCategory",
    "Common_CreateTransactionRuleMutationV2": "createTransactionRuleV2",
    "Common_DeleteTransactionRule": "deleteTransactionRule",
    "Common_UpdateBudgetItem": "updateOrCreateBudgetItem",
}

# `edit --merchant` sets `name` and `--category` sets `category`; both community sources
# carry a comment warning that the obvious spellings are wrong.
TRANSACTION_INPUT_FIELDS = {"category": "category", "merchant": "name", "notes": "notes"}

# The default emoji both community sources send when no icon was chosen.
CATEGORY_ICON = "\U00002753"


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


# Each plain variable name Rundesk manages, paired with the per-profile suffix this
# repository has always used, so both spellings resolve to the same field. The keys are
# exactly the names declared in rundesk.json plus the optional ones a command only uses
# when present.
PROFILE_FIELDS = {
    "MONARCH_EMAIL": "EMAIL",
    "MONARCH_PASSWORD": "PASSWORD",
    "MONARCH_MFA_SECRET": "MFA_SECRET",
    "MONARCH_LABEL": "LABEL",
}
REQUIRED_FIELDS = ("MONARCH_EMAIL", "MONARCH_PASSWORD")
# A Rundesk account suffix: uppercase words joined by single underscores, because a
# double underscore is what separates the field name from the account name.
ACCOUNT_SUFFIX_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")
RESERVED_PROFILE_WORDS = frozenset({"DEFAULT", "ENV"})


def normalize_profile(profile: str) -> str:
    """A profile name as an environment-variable fragment: `joint-account` to `JOINT_ACCOUNT`."""
    return re.sub(r"[^A-Za-z0-9]+", "_", profile or "").strip("_").upper()


def profile_label(suffix: str) -> str:
    """The inverse of `normalize_profile`, so a discovered account reads as a profile name."""
    return suffix.lower().replace("_", "-")


def env_name(profile: str, suffix: str) -> str:
    return f"MONARCH_{normalize_profile(profile)}_{suffix}"


def is_default_profile(profile: str) -> bool:
    """Rundesk stores the default account under the plain, unsuffixed variable names."""
    normalized = normalize_profile(profile)
    if not normalized or normalized == "DEFAULT":
        return True
    return normalized == normalize_profile(os.environ.get("MONARCH_DEFAULT_PROFILE", ""))


def missing_name(profile: str, field: str) -> str:
    """The variable an owner must set, spelled the way Rundesk stores it."""
    return field if is_default_profile(profile) else f"{field}__{normalize_profile(profile)}"


def profile_value(profile: str, field: str) -> str:
    """Read one field for one profile.

    Rundesk's `<FIELD>__<PROFILE>` wins, then this repository's `MONARCH_<PROFILE>_<FIELD>`,
    then the plain `<FIELD>` — which belongs to the default account only, so a named
    account never pairs one household's email with another household's password.
    """
    normalized = normalize_profile(profile)
    if normalized:
        for name in (f"{field}__{normalized}", env_name(profile, PROFILE_FIELDS[field])):
            value = os.environ.get(name, "")
            if value:
                return value
    if not is_default_profile(profile):
        return ""
    return os.environ.get(field, "")


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
    """Accounts present in the environment, so adding one needs no declaration.

    Both spellings are scanned: Rundesk's `<FIELD>__<ACCOUNT>` suffix and this
    repository's `MONARCH_<PROFILE>_<FIELD>` infix.

    The plain names are one more account — the default one — listed even when only
    partly configured, so it carries its own error instead of vanishing. It is
    suppressed when the infix spelling is in use: there a plain value was a fallback
    shared by every profile, not an account of its own, and inventing one would make
    every command ambiguous for an owner whose dotenv predates Rundesk.
    """
    suffixed: set[str] = set()
    infixed: set[str] = set()
    legacy = re.compile(
        rf"^MONARCH_({ACCOUNT_SUFFIX_RE.pattern})_({'|'.join(PROFILE_FIELDS.values())})$"
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
    names = suffixed | infixed
    # The plain names are the default account. The infix spelling predates that idea and
    # treated a plain value as a fallback shared by every profile, so an environment
    # written that way gets no invented `default` account to make selection ambiguous.
    if not infixed and any(profile_value("", field) for field in REQUIRED_FIELDS):
        names.add(os.environ.get("MONARCH_DEFAULT_PROFILE") or "default")
    return sorted(names)


def get_profile(name: str) -> Profile:
    email = profile_value(name, "MONARCH_EMAIL").strip()
    password = profile_value(name, "MONARCH_PASSWORD")
    missing = [
        missing_name(name, field)
        for field, value in (("MONARCH_EMAIL", email), ("MONARCH_PASSWORD", password))
        if not value
    ]
    if missing:
        raise MonarchError(
            f"Missing Monarch config for profile {name!r}: {', '.join(missing)}. "
            "Run `rundesk skills configure`, add it to the secrets dotenv, or export it in "
            "the shell."
        )

    return Profile(
        name=name,
        email=email,
        password=password,
        mfa_secret=profile_value(name, "MONARCH_MFA_SECRET").strip(),
        label=profile_value(name, "MONARCH_LABEL") or name,
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


def parse_amount(value: Any) -> Decimal:
    """A budget amount must never fall back to zero silently; a typo is an error."""
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MonarchError(
            f"Invalid --amount {value!r}. Pass a number such as 750 or 750.00."
        ) from exc


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
            f"Set {missing_name(profile.name, 'MONARCH_MFA_SECRET')} to the base32 seed "
            "from Monarch's authenticator setup, then retry."
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


def mutate(profile: Profile, operation: str, variables: dict) -> dict:
    """Send one allowlisted mutation. Nothing outside `MUTATIONS` can be sent from here."""
    document = MUTATIONS.get(operation)
    if document is None:
        raise MonarchError(
            f"Refusing to send {operation!r}: it is not on this package's mutation allowlist. "
            f"The approved operations are {', '.join(sorted(MUTATIONS))}."
        )

    data = graphql(profile, operation, document, variables)
    payload = data.get(MUTATION_ROOTS[operation])
    if not isinstance(payload, dict):
        raise MonarchError(f"Monarch {operation} returned no payload for the change.")

    # GraphQL reports a refused write inside an HTTP 200 twice over: once at the top
    # level, which `graphql` already raised on, and once per field down here.
    refusal = payload_errors(payload)
    if refusal:
        raise MonarchError(f"Monarch refused the change ({operation}): {refusal}")
    return payload


def payload_errors(payload: dict) -> str:
    """Join a `PayloadError`, which arrives as an object from some operations and a list from others."""
    errors = payload.get("errors")
    entries = errors if isinstance(errors, list) else [errors] if isinstance(errors, dict) else []
    messages = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for field_error in entry.get("fieldErrors") or []:
            if isinstance(field_error, dict):
                said = "; ".join(text(one) for one in field_error.get("messages") or [])
                messages.append(f"{text(field_error.get('field'))}: {said}")
        if entry.get("message"):
            messages.append(truncate(entry.get("message"), 200))
    return "; ".join(messages)


@dataclass
class Change:
    """One field of one target moving from `before` to `after`, and how to say it out loud."""

    operation: str
    target: str
    field: str
    before: Any
    after: Any
    shown_before: str = "-"
    shown_after: str = "-"
    label: str = "-"
    reversible: bool = True
    note: str = ""
    payload: dict = dataclass_field(default_factory=dict)

    def row(self) -> dict:
        return {
            "target": self.target or "(new)",
            "what": self.label,
            "field": self.field,
            "before": self.shown_before,
            "after": self.shown_after,
        }

    def as_record(self) -> dict:
        return {
            "operation": self.operation,
            "target": self.target,
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "shown_before": self.shown_before,
            "shown_after": self.shown_after,
            "label": self.label,
            "reversible": self.reversible,
            "note": self.note,
            "payload": self.payload,
        }

    @classmethod
    def from_record(cls, record: dict) -> "Change":
        return cls(
            operation=text(record.get("operation"), ""),
            target=text(record.get("target"), ""),
            field=text(record.get("field"), ""),
            before=record.get("before"),
            after=record.get("after"),
            shown_before=text(record.get("shown_before")),
            shown_after=text(record.get("shown_after")),
            label=text(record.get("label")),
            reversible=bool(record.get("reversible")),
            note=text(record.get("note"), ""),
            payload=record.get("payload") or {},
        )


def journal_dir() -> Path:
    return state_dir() / "journal"


def batch_id(started: float, counter: int) -> str:
    """`20260802T143000Z-01`. Derived from the run's start, never from ambient clock reads."""
    moment = datetime.datetime.fromtimestamp(started, datetime.timezone.utc)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{counter:02d}"


def next_batch_id(started: float) -> str:
    directory = journal_dir()
    for counter in range(1, 100):
        candidate = batch_id(started, counter)
        if not (directory / f"{candidate}.json").exists():
            return candidate
    raise MonarchError("The undo journal already holds 99 batches for this second.")


def journal_batch(batch: str, profile: Profile, changes: list, started: float,
                  state: str = "applied") -> str:
    """Record what landed, at 0600 in a 0700 directory. Ids, names, and amounts — no secrets."""
    write_private_json(
        journal_dir() / f"{batch}.json",
        {
            "batch": batch,
            "profile": profile.name,
            "when": datetime.datetime.fromtimestamp(started, datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%SZ"),
            "state": state,
            "changes": [change.as_record() for change in changes],
        },
    )
    return batch


def read_batch(batch: str) -> dict:
    path = journal_dir() / f"{batch}.json"
    if not path.is_file():
        raise MonarchError(f"No undo batch {batch!r} in {journal_dir()}. Run `undo --list`.")
    record = read_json(path)
    if not record:
        raise MonarchError(f"Undo batch {batch!r} is unreadable.")
    return record


def list_batches() -> list[dict]:
    try:
        paths = sorted(journal_dir().glob("*.json"), reverse=True)
    except OSError:
        return []
    records = [read_json(path) for path in paths]
    return [record for record in records if record.get("batch")]


def same_value(current: Any, wanted: Any) -> bool:
    """Compare a read-back with what was written, tolerating tag order and number shape."""
    if isinstance(current, (list, tuple)) or isinstance(wanted, (list, tuple)):
        return (sorted(str(item) for item in current or [])
                == sorted(str(item) for item in wanted or []))
    if isinstance(current, (int, float, Decimal)) and isinstance(wanted, (int, float, Decimal)):
        return to_decimal(current) == to_decimal(wanted)
    return ("" if current is None else str(current)) == ("" if wanted is None else str(wanted))


def refuse_over_cap(count: int, cap: int, action: str, what: str) -> None:
    """A bulk mistake is refused whether or not it was confirmed, and as early as it is known."""
    if count > cap:
        raise MonarchError(
            f"Refusing to {action}: {count} {what} exceed the bulk cap of {cap}. "
            "Narrow the selection, or raise the cap for this one run with --max."
        )


def apply_changes(profile: Profile, changes: list, *, confirm: bool, cap: int,
                  started: float, action: str) -> int:
    """The one path every write takes: preview, cap, apply, journal, read back."""
    print(f"action\t{action}")
    print(f"profile\t{profile.name}")
    print(f"changes\t{len(changes)}")

    if not changes:
        print("mode\tno-op")
        print("next\tEvery target already holds the requested value; nothing to send.")
        return 0

    refuse_over_cap(len(changes), cap, action, "changes")
    print_csv(CHANGE_COLUMNS, [change.row() for change in changes])
    for change in changes:
        if not change.reversible:
            print(f"warning: {change.note}", file=sys.stderr)

    if not confirm:
        print("mode\tdry-run")
        print("next\tNothing was sent. Re-run with --confirm to apply exactly the rows above.")
        return 0

    batch = next_batch_id(started)
    print("mode\tconfirmed")
    print(f"batch\t{batch}")

    applied: list = []
    for change in changes:
        try:
            SENDERS[change.operation](profile, change)
        except MonarchError as exc:
            stop_batch(batch, profile, applied, started, len(changes),
                       f"{change.label} ({change.field}): {exc}")
            return 1

        applied.append(change)
        journal_batch(batch, profile, applied, started)

        current = VERIFIERS[change.operation](profile, change)
        if not same_value(current, change.after):
            stop_batch(batch, profile, applied, started, len(changes),
                       f"{change.label} ({change.field}) read back as {text(current)!r}, "
                       f"not {change.shown_after!r}")
            return 1

    print(f"applied\t{len(applied)}")
    print(f"undo\tmonarch undo {batch} --confirm")
    return 0


def stop_batch(batch: str, profile: Profile, applied: list, started: float,
               total: int, why: str) -> None:
    """Halt a part-done batch loudly, leaving the journal able to reverse what did land."""
    if applied:
        journal_batch(batch, profile, applied, started)
    print(f"error: stopped after {len(applied)} of {total} changes — {why}", file=sys.stderr)
    if applied:
        print(
            f"note: {len(applied)} change(s) did land and are journalled as {batch}; "
            f"reverse them with `monarch undo {batch} --confirm`.",
            file=sys.stderr,
        )


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
        print(
            "No Monarch profiles configured. Run `rundesk skills configure`, or set "
            "MONARCH_PROFILES and MONARCH_DEFAULT_PROFILE in the integration dotenv."
        )
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
        "id": text(item.get("id")),
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


def fetch_transaction(profile: Profile, transaction_id: str) -> dict:
    data = graphql(
        profile,
        "GetTransactionDrawer",
        QUERY_TRANSACTION,
        {"id": transaction_id, "redirectPosted": True},
    )
    item = data.get("getTransaction")
    if not isinstance(item, dict) or not item.get("id"):
        raise MonarchError(
            f"No transaction {transaction_id!r} in this household. Run `transactions` and "
            "copy an id from its first column."
        )
    return item


def fetch_category_groups(profile: Profile) -> list[dict]:
    data = graphql(profile, "ManageGetCategoryGroups", QUERY_CATEGORY_GROUPS)
    groups = data.get("categoryGroups")
    return [item for item in groups if isinstance(item, dict)] if isinstance(groups, list) else []


def fetch_tags(profile: Profile) -> list[dict]:
    data = graphql(profile, "GetHouseholdTransactionTags", QUERY_TAGS, {"search": "", "limit": 500})
    tags = data.get("householdTransactionTags")
    return [item for item in tags if isinstance(item, dict)] if isinstance(tags, list) else []


def fetch_rules(profile: Profile) -> list[dict]:
    data = graphql(profile, "GetTransactionRules", QUERY_RULES)
    rules = data.get("transactionRules")
    return [item for item in rules if isinstance(item, dict)] if isinstance(rules, list) else []


def budget_amount(profile: Profile, category_id: str, first: datetime.date) -> Decimal:
    """The planned amount for one category-month. An unbudgeted category reads as zero."""
    start, end = month_bounds(first)
    data = graphql(
        profile, "Common_GetJointPlanningData", QUERY_BUDGETS, {"startDate": start, "endDate": end}
    )
    entries = ((data.get("budgetData") or {}).get("monthlyAmountsByCategory")) or []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        if str((entry.get("category") or {}).get("id") or "") != category_id:
            continue
        for amount in entry.get("monthlyAmounts") or []:
            if isinstance(amount, dict) and str(amount.get("month") or "")[:7] == start[:7]:
                return to_decimal(amount.get("plannedCashFlowAmount"))
    return Decimal(0)


def transaction_field(item: dict, field: str) -> str:
    """The wire value of one editable field: a category id, a merchant name, or the note."""
    if field == "category":
        return str((item.get("category") or {}).get("id") or "")
    if field == "merchant":
        return str((item.get("merchant") or {}).get("name") or "")
    return str(item.get("notes") or "")


def shown_transaction_field(item: dict, field: str) -> str:
    if field == "category":
        return truncate((item.get("category") or {}).get("name"), 30)
    if field == "merchant":
        return truncate((item.get("merchant") or {}).get("name"), 40)
    return truncate(item.get("notes"), 40)


def transaction_label(item: dict) -> str:
    return f"{text(item.get('date'))} {truncate((item.get('merchant') or {}).get('name'), 30)}"


def tag_names(ids: list, names: dict) -> str:
    return " ".join(names.get(str(one), str(one)) for one in ids) if ids else "(none)"


def send_transaction_field(profile: Profile, change: Change) -> None:
    """Send exactly one field. The input is built by name lookup, so no fourth field exists."""
    key = TRANSACTION_INPUT_FIELDS[change.field]
    mutate(profile, change.operation, {"input": {"id": change.target, key: change.after}})


def verify_transaction_field(profile: Profile, change: Change) -> Any:
    return transaction_field(fetch_transaction(profile, change.target), change.field)


def send_tags(profile: Profile, change: Change) -> None:
    mutate(
        profile,
        change.operation,
        {"input": {"transactionId": change.target, "tagIds": list(change.after or [])}},
    )


def verify_tags(profile: Profile, change: Change) -> Any:
    item = fetch_transaction(profile, change.target)
    return [str(tag.get("id")) for tag in item.get("tags") or [] if isinstance(tag, dict)]


def send_create_category(profile: Profile, change: Change) -> None:
    payload = mutate(
        profile,
        change.operation,
        {"input": {"group": change.payload["group"], "name": change.payload["name"],
                   "icon": change.payload.get("icon", CATEGORY_ICON)}},
    )
    created = str((payload.get("category") or {}).get("id") or "")
    if not created:
        raise MonarchError("Monarch accepted the category but returned no id for it.")
    change.target = created
    change.after = created


def verify_create_category(profile: Profile, change: Change) -> Any:
    return next(
        (str(item.get("id")) for item in fetch_categories(profile)
         if str(item.get("id")) == change.target),
        "",
    )


def rule_input(payload: dict) -> dict:
    return {
        "merchantNameCriteria": [
            {"operator": "contains", "value": payload["merchantContains"]}
        ],
        "setCategoryAction": payload["categoryId"],
        "applyToExistingTransactions": False,
    }


def send_create_rule(profile: Profile, change: Change) -> None:
    """Monarch's rule mutation returns no id, so the new rule is found by diffing the list."""
    before = {str(rule.get("id")) for rule in fetch_rules(profile)}
    mutate(profile, change.operation, {"input": rule_input(change.payload)})
    appeared = [rule for rule in fetch_rules(profile) if str(rule.get("id")) not in before]

    if len(appeared) != 1:
        change.reversible = False
        change.note = (
            f"Monarch's rule mutation returns no id and {len(appeared)} new rules appeared, "
            "so this one cannot be identified for undo. Check `rules` and remove it by hand "
            "if it is unwanted."
        )
        change.target = ""
        change.after = None
        return
    change.target = str(appeared[0].get("id"))
    change.after = change.target


def verify_rule_present(profile: Profile, change: Change) -> Any:
    if not change.target:
        return ""
    return next(
        (str(rule.get("id")) for rule in fetch_rules(profile)
         if str(rule.get("id")) == change.target),
        "",
    )


def send_delete_rule(profile: Profile, change: Change) -> None:
    payload = mutate(profile, change.operation, {"id": change.target})
    # The payload can omit `deleted` on success, so only an explicit false is a failure.
    if payload.get("deleted") is False:
        raise MonarchError(f"Monarch declined to delete rule {change.target}.")


def send_budget(profile: Profile, change: Change) -> None:
    mutate(
        profile,
        change.operation,
        {
            "input": {
                "categoryId": change.payload["categoryId"],
                "amount": float(to_decimal(change.after)),
                "timeframe": "month",
                "startDate": change.payload["startDate"],
                # Pinned: applying forward would make one command a write over unbounded months.
                "applyToFuture": False,
            }
        },
    )


def verify_budget(profile: Profile, change: Change) -> Any:
    return budget_amount(
        profile,
        change.payload["categoryId"],
        parse_day(change.payload["startDate"], "month"),
    )


SENDERS: dict = {
    "Web_TransactionDrawerUpdateTransaction": send_transaction_field,
    "Web_SetTransactionTags": send_tags,
    "Web_CreateCategory": send_create_category,
    "Common_CreateTransactionRuleMutationV2": send_create_rule,
    "Common_DeleteTransactionRule": send_delete_rule,
    "Common_UpdateBudgetItem": send_budget,
}

VERIFIERS: dict = {
    "Web_TransactionDrawerUpdateTransaction": verify_transaction_field,
    "Web_SetTransactionTags": verify_tags,
    "Web_CreateCategory": verify_create_category,
    "Common_CreateTransactionRuleMutationV2": verify_rule_present,
    "Common_DeleteTransactionRule": verify_rule_present,
    "Common_UpdateBudgetItem": verify_budget,
}


def target_ids(given: list) -> list[str]:
    """Ids from the command line, or `-` to read a reviewed list from stdin, one per line."""
    collected: list[str] = []
    for value in given or []:
        if value == "-":
            collected.extend(line.strip() for line in sys.stdin.read().splitlines())
        else:
            collected.append(str(value).strip())

    unique: list[str] = []
    for one in collected:
        if one and one not in unique:
            unique.append(one)
    if not unique:
        raise MonarchError(
            "No transaction ids given. Pass ids from the `transactions` id column, or `-` to "
            "read a reviewed list from stdin."
        )
    return unique


def command_edit(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    wanted = {
        name: getattr(args, name)
        for name in ("category", "merchant", "note")
        if getattr(args, name) is not None
    }
    if not wanted:
        raise MonarchError(
            "edit changes nothing without --category, --merchant, or --note. It cannot change "
            "an amount, a date, an account, or a pending state at all."
        )

    category_id = category_name = ""
    if "category" in wanted:
        chosen = match_one(fetch_categories(profile), "name", args.category, "Category")
        category_id, category_name = str(chosen.get("id")), truncate(chosen.get("name"), 30)

    # Capped here as well as in `apply_changes`, so an oversized batch costs one message
    # rather than one read per target.
    wanted_ids = target_ids(args.transaction)
    refuse_over_cap(len(wanted_ids), args.max, "edit transactions", "transactions")

    changes = []
    for transaction_id in wanted_ids:
        item = fetch_transaction(profile, transaction_id)
        for name, field in (("category", "category"), ("merchant", "merchant"), ("note", "notes")):
            if name not in wanted:
                continue
            after = category_id if name == "category" else wanted[name]
            before = transaction_field(item, field)
            if same_value(before, after):
                continue
            changes.append(
                Change(
                    operation="Web_TransactionDrawerUpdateTransaction",
                    target=transaction_id,
                    field=field,
                    before=before,
                    after=after,
                    shown_before=shown_transaction_field(item, field),
                    shown_after=category_name if name == "category" else truncate(after, 40),
                    label=transaction_label(item),
                )
            )

    return apply_changes(
        profile, changes, confirm=args.confirm, cap=args.max, started=args.started,
        action="edit transactions",
    )


def command_tag(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    if not args.add and not args.remove:
        raise MonarchError("tag changes nothing without --add or --remove.")

    tags = fetch_tags(profile)
    names = {str(tag.get("id")): text(tag.get("name")) for tag in tags}
    adding = [str(match_one(tags, "name", one, "Tag").get("id")) for one in args.add or []]
    removing = {str(match_one(tags, "name", one, "Tag").get("id")) for one in args.remove or []}

    wanted_ids = target_ids(args.transaction)
    refuse_over_cap(len(wanted_ids), args.max, "set transaction tags", "transactions")

    changes = []
    for transaction_id in wanted_ids:
        item = fetch_transaction(profile, transaction_id)
        before = [str(tag.get("id")) for tag in item.get("tags") or [] if isinstance(tag, dict)]
        # `setTransactionTags` replaces the whole set, so the union is computed here rather
        # than hoped for from the API.
        after = [one for one in before if one not in removing]
        after += [one for one in adding if one not in after]
        if same_value(before, after):
            continue
        changes.append(
            Change(
                operation="Web_SetTransactionTags",
                target=transaction_id,
                field="tags",
                before=before,
                after=after,
                shown_before=tag_names(before, names),
                shown_after=tag_names(after, names),
                label=transaction_label(item),
            )
        )

    return apply_changes(
        profile, changes, confirm=args.confirm, cap=args.max, started=args.started,
        action="set transaction tags",
    )


def command_category_create(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    group = match_one(fetch_category_groups(profile), "name", args.group, "Category group")
    wanted = args.name.strip()
    if not wanted:
        raise MonarchError("--name cannot be empty.")
    if any(str(item.get("name") or "").casefold() == wanted.casefold()
           for item in fetch_categories(profile)):
        raise MonarchError(f"A category named {wanted!r} already exists in this household.")

    change = Change(
        operation="Web_CreateCategory",
        target="",
        field="category",
        before=None,
        after="",
        shown_before="(none)",
        shown_after=f"{truncate(group.get('name'), 30)} / {truncate(wanted, 40)}",
        label=wanted,
        reversible=False,
        note=(
            f"Creating category {wanted!r} cannot be undone by this package: removing a "
            "category reassigns every transaction filed under it, which is outside the "
            "approved write set. `undo` will name it and leave it standing."
        ),
        payload={"group": str(group.get("id")), "name": wanted},
    )
    return apply_changes(
        profile, [change], confirm=args.confirm, cap=args.max, started=args.started,
        action="create a category",
    )


def rule_summary(rule: dict) -> str:
    criteria = rule.get("merchantNameCriteria") or rule.get("merchantCriteria") or []
    matches = " or ".join(
        f"{text(one.get('operator'))} {text(one.get('value'))}"
        for one in criteria if isinstance(one, dict)
    )
    return truncate(f"merchant {matches}" if matches else "other criteria", 60)


def rule_action(rule: dict) -> str:
    parts = []
    category = rule.get("setCategoryAction") or {}
    if category:
        parts.append(f"category {text(category.get('name'))}")
    merchant = rule.get("setMerchantAction") or {}
    if merchant:
        parts.append(f"merchant {text(merchant.get('name'))}")
    for tag in rule.get("addTagsAction") or []:
        if isinstance(tag, dict):
            parts.append(f"tag {text(tag.get('name'))}")
    if rule.get("setHideFromReportsAction"):
        parts.append("hide from reports")
    return truncate(", ".join(parts) if parts else "-", 60)


def rule_spec(rule: dict) -> dict:
    """The rule as `rule create` would express it, or `{}` when it is richer than that."""
    criteria = rule.get("merchantNameCriteria") or []
    category = rule.get("setCategoryAction") or {}
    richer = (
        rule.get("merchantCriteria")
        or rule.get("amountCriteria")
        or rule.get("accountIds")
        or rule.get("setMerchantAction")
        or rule.get("addTagsAction")
        or rule.get("setHideFromReportsAction")
        or rule.get("reviewStatusAction")
    )
    if richer or len(criteria) != 1 or not category:
        return {}
    only = criteria[0]
    if not isinstance(only, dict) or str(only.get("operator") or "") != "contains":
        return {}
    return {
        "merchantContains": text(only.get("value"), ""),
        "categoryId": str(category.get("id") or ""),
        "categoryName": text(category.get("name")),
    }


def command_rules(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    rules = fetch_rules(profile)
    if args.json:
        print_json(rules)
        return 0

    rows = [
        {
            "id": text(rule.get("id")),
            "matches": rule_summary(rule),
            "sets": rule_action(rule),
            "applied": text(rule.get("recentApplicationCount"), "0"),
        }
        for rule in rules
    ]
    shown = rows[: args.limit]
    print_csv(RULE_COLUMNS, shown)
    note_truncation(len(shown), len(rows), "rules")
    return 0


def command_rule_create(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    chosen = match_one(fetch_categories(profile), "name", args.category, "Category")
    matching = args.merchant_contains.strip()
    if not matching:
        raise MonarchError("--merchant-contains cannot be empty; that would match everything.")

    change = Change(
        operation="Common_CreateTransactionRuleMutationV2",
        target="",
        field="rule",
        before=None,
        after=None,
        shown_before="(none)",
        shown_after=f"merchant contains {matching} -> {truncate(chosen.get('name'), 30)}",
        label="new rule",
        payload={
            "merchantContains": matching,
            "categoryId": str(chosen.get("id")),
            "categoryName": text(chosen.get("name")),
        },
    )
    return apply_changes(
        profile, [change], confirm=args.confirm, cap=args.max, started=args.started,
        action="create a transaction rule",
    )


def command_rule_delete(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    chosen = next((rule for rule in fetch_rules(profile) if str(rule.get("id")) == args.rule), None)
    if chosen is None:
        raise MonarchError(f"No transaction rule {args.rule!r}. Run `rules` for the current ids.")

    spec = rule_spec(chosen)
    change = Change(
        operation="Common_DeleteTransactionRule",
        target=str(chosen.get("id")),
        field="rule",
        before=str(chosen.get("id")),
        after=None,
        shown_before=f"{rule_summary(chosen)} -> {rule_action(chosen)}",
        shown_after="(deleted)",
        label=f"rule {text(chosen.get('id'))}",
        reversible=bool(spec),
        note=(
            ""
            if spec
            else "This rule uses criteria or actions `rule create` cannot express, so `undo` "
            "cannot rebuild it. Copy its definition out of `rules --json` before confirming."
        ),
        payload=spec,
    )
    return apply_changes(
        profile, [change], confirm=args.confirm, cap=args.max, started=args.started,
        action="delete a transaction rule",
    )


def command_budget_set(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    chosen = match_one(fetch_categories(profile), "name", args.category, "Category")
    first = parse_month(args.month)
    category_id = str(chosen.get("id"))
    before = budget_amount(profile, category_id, first)
    after = parse_amount(args.amount)

    changes = []
    if before != after:
        changes.append(
            Change(
                operation="Common_UpdateBudgetItem",
                target=f"{category_id}:{first.isoformat()[:7]}",
                field="budget",
                before=float(before),
                after=float(after),
                shown_before=format_amount(before),
                shown_after=format_amount(after),
                label=f"{truncate(chosen.get('name'), 30)} {first.isoformat()[:7]}",
                payload={"categoryId": category_id, "startDate": first.isoformat()},
            )
        )
    return apply_changes(
        profile, changes, confirm=args.confirm, cap=args.max, started=args.started,
        action="set a budget amount",
    )


def invert(change: Change) -> Change:
    """The change that puts a target back. A created rule is removed; a deleted one rebuilt."""
    if change.operation == "Web_CreateCategory":
        raise MonarchError(
            f"Creating category {change.label!r} has no inverse here: reversing it would mean "
            "deleting a category, which is outside this package's approved write set."
        )
    if change.operation == "Common_CreateTransactionRuleMutationV2":
        return Change(
            operation="Common_DeleteTransactionRule",
            target=change.target,
            field="rule",
            before=change.target,
            after=None,
            shown_before=change.shown_after,
            shown_after="(deleted)",
            label=change.label,
        )
    if change.operation == "Common_DeleteTransactionRule":
        return Change(
            operation="Common_CreateTransactionRuleMutationV2",
            target="",
            field="rule",
            before=None,
            after=None,
            shown_before="(deleted)",
            shown_after=change.shown_before,
            label=change.label,
            payload=change.payload,
        )
    return Change(
        operation=change.operation,
        target=change.target,
        field=change.field,
        before=change.after,
        after=change.before,
        shown_before=change.shown_after,
        shown_after=change.shown_before,
        label=change.label,
        payload=change.payload,
    )


def command_undo(args: argparse.Namespace) -> int:
    if args.list:
        return command_undo_list(args)
    if not args.batch:
        raise MonarchError("undo needs a batch id, or --list to see the ones on record.")

    profile = get_profile(selected_profile_name(args))
    record = read_batch(args.batch)
    if str(record.get("state")) == "undone":
        raise MonarchError(
            f"Batch {args.batch!r} was already undone on this machine. Reapplying it would "
            "overwrite whatever holds those values now."
        )
    changes = [Change.from_record(item) for item in record.get("changes") or []
               if isinstance(item, dict)]
    if not changes:
        raise MonarchError(f"Batch {args.batch!r} records no changes.")

    reversals, skipped = [], []
    for change in reversed(changes):
        if not change.reversible:
            skipped.append((change, change.note or "not reversible by this package"))
            continue
        current = VERIFIERS[change.operation](profile, change)
        if not same_value(current, change.after):
            # Restoring here would clobber an edit made later, in the app or by a person.
            skipped.append((change, f"changed underneath; it now reads {text(current)!r}"))
            continue
        reversals.append(invert(change))

    for change, why in skipped:
        print(f"warning: skipping {change.label} ({change.field}) — {why}", file=sys.stderr)

    code = apply_changes(
        profile, reversals, confirm=args.confirm, cap=args.max, started=args.started,
        action=f"undo batch {args.batch}",
    )
    if code == 0 and args.confirm and not skipped:
        # Rewritten rather than rebuilt, so the batch keeps the time it was applied.
        record["state"] = "undone"
        write_private_json(journal_dir() / f"{args.batch}.json", record)
        print("state\tundone")
    if skipped:
        print(
            f"error: {len(skipped)} of {len(changes)} change(s) were left standing.",
            file=sys.stderr,
        )
        return 1
    return code


def command_undo_list(args: argparse.Namespace) -> int:
    records = list_batches()
    if args.json:
        print_json(records)
        return 0

    rows = [
        {
            "batch": text(record.get("batch")),
            "when": text(record.get("when")),
            "profile": text(record.get("profile")),
            "changes": len(record.get("changes") or []),
            "state": text(record.get("state")),
        }
        for record in records
    ]
    shown = rows[: args.limit]
    print_csv(BATCH_COLUMNS, shown)
    note_truncation(len(shown), len(rows), "batches")
    return 0


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
        help="Monarch account name, from MONARCH_<FIELD>__<PROFILE> or "
        "MONARCH_<PROFILE>_<FIELD> env vars.",
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


def add_write_options(parser: argparse.ArgumentParser) -> None:
    """Every write takes the same two: an exact confirmation and the bulk cap."""
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply the change. Without it the command prints what it would do and sends nothing.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_BULK_CAP,
        help=f"Refuse a batch larger than this. Default {DEFAULT_BULK_CAP}.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monarch",
        description=(
            "Read a Monarch Money household, and edit categories, merchants, notes, tags, "
            "rules, and budget amounts (writes preview until --confirm)."
        ),
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
              monarch edit txn-1 --category Groceries
              monarch edit txn-1 --category Groceries --confirm
              monarch tag txn-1 --add Reimbursable --confirm
              monarch category create --name "Pet Care" --group Lifestyle --confirm
              monarch rules
              monarch rule create --merchant-contains "Example Market" --category Groceries
              monarch budget set --category Groceries --month 2026-08 --amount 750
              monarch undo --list
              monarch undo 20260802T143000Z-01 --confirm

            Every write is a preview until --confirm, is capped at 50 targets, and is
            journalled so `undo` can put it back. No command changes an amount, a date, an
            account, or a pending state; none deletes or splits a transaction; none deletes
            a category.
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

    edit = subparsers.add_parser(
        "edit", help="Set a transaction's category, merchant, or note (preview unless --confirm)."
    )
    add_env_option(edit, suppress_defaults=True)
    add_profile_option(edit, suppress_defaults=True)
    edit.add_argument("transaction", nargs="+", help="Transaction ids, or `-` to read from stdin.")
    edit.add_argument("--category", help="Category name to file these transactions under.")
    edit.add_argument("--merchant", help="Merchant name to set.")
    edit.add_argument("--note", help="Note to set. An empty string clears the note.")
    add_write_options(edit)
    edit.set_defaults(handler=command_edit)

    tag = subparsers.add_parser(
        "tag", help="Add or remove existing tags on transactions (preview unless --confirm)."
    )
    add_env_option(tag, suppress_defaults=True)
    add_profile_option(tag, suppress_defaults=True)
    tag.add_argument("transaction", nargs="+", help="Transaction ids, or `-` to read from stdin.")
    tag.add_argument("--add", action="append", help="Existing tag to add. Repeatable.")
    tag.add_argument("--remove", action="append", help="Existing tag to remove. Repeatable.")
    add_write_options(tag)
    tag.set_defaults(handler=command_tag)

    category = subparsers.add_parser("category", help="Category maintenance.")
    category_actions = category.add_subparsers(dest="category_command", required=True)
    category_create = category_actions.add_parser(
        "create", help="Create one category in a group (preview unless --confirm)."
    )
    add_env_option(category_create, suppress_defaults=True)
    add_profile_option(category_create, suppress_defaults=True)
    category_create.add_argument("--name", required=True, help="New category name.")
    category_create.add_argument("--group", required=True, help="Existing category group name.")
    add_write_options(category_create)
    category_create.set_defaults(handler=command_category_create)

    rules = subparsers.add_parser("rules", help="List the household's transaction rules.")
    add_env_option(rules, suppress_defaults=True)
    add_profile_option(rules, suppress_defaults=True)
    rules.add_argument("--limit", type=int, default=100, help="Maximum rules to print.")
    add_json_option(rules)
    rules.set_defaults(handler=command_rules)

    rule = subparsers.add_parser("rule", help="Transaction rule maintenance.")
    rule_actions = rule.add_subparsers(dest="rule_command", required=True)

    rule_create = rule_actions.add_parser(
        "create", help="Create a merchant-to-category rule (preview unless --confirm)."
    )
    add_env_option(rule_create, suppress_defaults=True)
    add_profile_option(rule_create, suppress_defaults=True)
    rule_create.add_argument(
        "--merchant-contains", required=True, help="Text the merchant name must contain."
    )
    rule_create.add_argument("--category", required=True, help="Category the rule files them under.")
    add_write_options(rule_create)
    rule_create.set_defaults(handler=command_rule_create)

    rule_delete = rule_actions.add_parser(
        "delete", help="Delete one transaction rule (preview unless --confirm)."
    )
    add_env_option(rule_delete, suppress_defaults=True)
    add_profile_option(rule_delete, suppress_defaults=True)
    rule_delete.add_argument("rule", help="Rule id from `rules`.")
    add_write_options(rule_delete)
    rule_delete.set_defaults(handler=command_rule_delete)

    budget = subparsers.add_parser("budget", help="Budget maintenance.")
    budget_actions = budget.add_subparsers(dest="budget_command", required=True)
    budget_set = budget_actions.add_parser(
        "set", help="Set one category's budget for one month (preview unless --confirm)."
    )
    add_env_option(budget_set, suppress_defaults=True)
    add_profile_option(budget_set, suppress_defaults=True)
    budget_set.add_argument("--category", required=True, help="Category name.")
    budget_set.add_argument("--month", required=True, help="Budget month as YYYY-MM.")
    budget_set.add_argument("--amount", required=True, help="Planned amount. Zero clears it.")
    add_write_options(budget_set)
    budget_set.set_defaults(handler=command_budget_set)

    undo = subparsers.add_parser(
        "undo", help="Reverse a journalled batch of writes (preview unless --confirm)."
    )
    add_env_option(undo, suppress_defaults=True)
    add_profile_option(undo, suppress_defaults=True)
    undo.add_argument("batch", nargs="?", help="Batch id from `undo --list`.")
    undo.add_argument("--list", action="store_true", help="List journalled batches, newest first.")
    undo.add_argument("--limit", type=int, default=25, help="Maximum batches to list.")
    add_json_option(undo)
    add_write_options(undo)
    undo.set_defaults(handler=command_undo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Stamped once, here, so a journal batch id comes from the run rather than from an
    # ambient clock read buried in the write path.
    if getattr(args, "started", None) is None:
        args.started = time.time()

    load_dotenv(resolve_env_file(getattr(args, "env_file", None)))

    try:
        handler: Callable[[argparse.Namespace], int] = args.handler
        return handler(args)
    except MonarchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
