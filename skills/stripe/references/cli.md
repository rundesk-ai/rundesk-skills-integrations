# Stripe CLI reference

The integration is Stripe-only. It talks to the Stripe REST API directly over HTTPS and needs
no Stripe CLI installation, browser login, or dashboard session.

## Entry point

- List configured profiles: `stripe profiles`
- Show the account a profile reaches: `stripe status --profile example`
- Show balances across every account: `stripe balance --all-profiles`
- Roll up 30 days of money movement: `stripe revenue --profile example --days 30`
- Roll up an exact window: `stripe revenue --profile example --start 2026-07-01 --end 2026-08-01`
- List recent payouts: `stripe payouts --profile example --days 30 --limit 10`
- List failed payouts only: `stripe payouts --profile example --days 90 --status failed`
- List recent charges: `stripe charges --profile example --days 7 --limit 25`
- List active subscriptions: `stripe subscriptions --profile example --status active --limit 25`
- List every subscription state: `stripe subscriptions --profile example --status all --limit 50`
- List open disputes: `stripe disputes --profile example --days 30`
- Look up one customer: `stripe customer cus_example --profile example`
- Look up by email: `stripe customer buyer@example.test --profile example`
- List runnable report types: `stripe report types --profile example`
- Run a report and save it: `stripe report run --profile example --type balance.summary.1 --start 2026-07-01 --end 2026-08-01 --out ./balance.csv`
- Retrieve an earlier run: `stripe report status frr_example --profile example --out ./balance.csv`

`status`, `balance`, and `revenue` accept `--all-profiles` to sweep every configured account
in one call. The remaining commands act on one profile so that a report is never a silent
blend of two businesses.

## Configuration

Credentials load from the first of these that exists: variables already in the process, the
path given to `--env-file`, `STRIPE_ENV_FILE`, `RUNDESK_INTEGRATIONS_ENV`, then
`${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/stripe/env`, then the legacy
`${XDG_CONFIG_HOME:-$HOME/.config}/stripe/env`.

```sh
mkdir -p "$HOME/.config/rundesk/integrations/stripe"
chmod 700 "$HOME/.config/rundesk" "$HOME/.config/rundesk/integrations" \
  "$HOME/.config/rundesk/integrations/stripe"
chmod 600 "$HOME/.config/rundesk/integrations/stripe/env"
```

The command warns on stderr when the file is readable by group or others, and never prints a
key, an authorization header, or raw dotenv contents.

### Environment keys

Every field has two accepted spellings. For one profile, the command reads them in this order
and takes the first non-empty value:

1. `<FIELD>__<PROFILE>` — Rundesk's form, written by `rundesk skills configure`. Rundesk owns
   these variables and finds accounts by scanning for the suffix, so a new account needs no
   declaration anywhere.
2. `STRIPE_<PROFILE>_<FIELD>` — this repository's older form, which this command reads by hand
   from the dotenv. Kept so an existing file keeps working; nothing manages it for you.
3. the plain `<FIELD>` — the **default account** only.

A named account never falls back to a plain value. That refusal is the point: `STRIPE_ACCOUNT`
belongs to the default account, and silently lending it to `acme` would send one business's key
with another business's connected-account id.

The default account is the profile named `default`, the one named in `STRIPE_DEFAULT_PROFILE`,
or the implicit account when no profile is named at all.

| Field | Required | Rundesk-managed key | Older dotenv key | Purpose |
| --- | --- | --- | --- | --- |
| `STRIPE_API_KEY` | yes | `STRIPE_API_KEY__<PROFILE>` | `STRIPE_<PROFILE>_KEY` | Restricted API key (`rk_...`) for that account. |
| `STRIPE_ACCOUNT` | optional | `STRIPE_ACCOUNT__<PROFILE>` | `STRIPE_<PROFILE>_ACCOUNT` | `acct_...` id sent as the `Stripe-Account` header. |
| `STRIPE_API_VERSION` | optional | `STRIPE_API_VERSION__<PROFILE>` | `STRIPE_<PROFILE>_API_VERSION` | Pins `Stripe-Version`; defaults to the account's version. |
| `STRIPE_LABEL` | optional | `STRIPE_LABEL__<PROFILE>` | `STRIPE_<PROFILE>_LABEL` | Human-readable account name in output. |

`STRIPE_API_KEY` is the only required name; it is what `rundesk.json` declares. Two further
keys route rather than authenticate, and are read only from the dotenv:

| Key | Required | Purpose |
| --- | --- | --- |
| `STRIPE_PROFILES` | optional | Comma-separated profile names, in listing order. Overrides discovery. |
| `STRIPE_DEFAULT_PROFILE` | optional | Profile used when `--profile` is absent. |

Neither is needed for a Rundesk-managed setup: with no `STRIPE_PROFILES`, `profiles` lists
every account it finds in either spelling, plus the default account when a plain name is set.

A profile name is upper-cased and non-alphanumeric characters become underscores, so profile
`platform-sub` reads `STRIPE_API_KEY__PLATFORM_SUB` or `STRIPE_PLATFORM_SUB_KEY`.

```sh
# Rundesk's form: one default account and two named ones.
STRIPE_API_KEY=rk_live_synthetic
STRIPE_LABEL=Example Inc

STRIPE_API_KEY__WIDGETS=rk_live_synthetic
STRIPE_LABEL__WIDGETS=Widgets Ltd

STRIPE_API_KEY__PLATFORM_SUB=rk_live_synthetic
STRIPE_ACCOUNT__PLATFORM_SUB=acct_synthetic
STRIPE_LABEL__PLATFORM_SUB=Connected merchant
```

```sh
# This repository's older form, still read from the dotenv.
STRIPE_PROFILES=acme,widgets,platform-sub
STRIPE_DEFAULT_PROFILE=acme

STRIPE_ACME_KEY=rk_live_synthetic
STRIPE_ACME_LABEL=Acme Inc

STRIPE_WIDGETS_KEY=rk_live_synthetic
STRIPE_WIDGETS_LABEL=Widgets Ltd

STRIPE_PLATFORM_SUB_KEY=rk_live_synthetic
STRIPE_PLATFORM_SUB_ACCOUNT=acct_synthetic
STRIPE_PLATFORM_SUB_LABEL=Connected merchant
```

An older single-account dotenv may spell the default account's key `STRIPE_SECRET_KEY`; that
name still resolves, for the default account only. Because the plain names exist, `api`,
`secret`, `default`, and `env` are reserved and cannot be used as profile names.

### Two ways to hold several accounts

**Separate businesses** each get their own restricted key and no `_ACCOUNT` value. Each key is
independent, so revoking one leaves the others working.

**Connect connected accounts** share one platform key and set `_ACCOUNT` to the `acct_...` id.
The command sends `Stripe-Account`, and Stripe answers as that connected account. The platform
key needs the corresponding Connect read permissions. Stripe does not support OAuth for this
routing, which is why the package uses keys rather than an OAuth flow.

### Recommended key scopes

Create a restricted key in the Stripe dashboard with **read** on the resources you intend to
use and nothing else:

```text
Balance                 read
Balance transactions    read
Charges                 read
Customers               read
Disputes                read
Payouts                 read
Subscriptions           read
Reporting               read and write   (only for `report run`)
```

`report run` creates a report run object, so the Reporting resource is the one place a
write scope is needed. Omit it entirely if the account only needs listing and rollups; every
other command works with read scopes alone. A missing scope surfaces as a Stripe `403` with
its message intact, which is deliberately distinguishable from an empty result.

## Output contract

List commands print CSV-style rows with a header, one row per object, and a trailing `profile`
column so combined output stays attributable. `revenue` adds a `TOTAL` row per currency.
Detail commands print tab-separated `key<TAB>value` lines.

Monetary columns are converted from Stripe's minor units using the currency's exponent: zero
decimal places for BIF, CLP, DJF, GNF, JPY, KMF, KRW, MGA, PYG, RWF, UGX, VND, VUV, XAF, XOF
and XPF; three for BHD, JOD, KWD, OMR and TND; two otherwise. Timestamps render as UTC
`YYYY-MM-DD HH:MM`.

Text output replaces email addresses with `[redacted-email]` and IP literals with
`[redacted-ip]`. `--json` prints the raw Stripe payload with no redaction and amounts still in
minor units.

Windows come from `--days N` or an exact `--start`/`--end` pair of `YYYY-MM-DD` UTC dates,
where `--end` is exclusive. Passing only one of the pair is an error rather than a silent
half-window.

Bounded reads page through Stripe until `--limit` is reached. When Stripe reports more records
beyond that point, the command writes a `note:` line to stderr; stdout stays clean for piping.
Errors go to stderr and exit non-zero. A report run that is still pending, or that failed,
exits non-zero.

## Validation

- Run `python3 "$RUNDESK_SKILLS/stripe/scripts/stripe.d/test-stripe.py"`.
- Tests are offline, use synthetic fixtures, replace every network boundary, and need no
  Stripe credentials.
- Optional live read-only smoke tests:
  - `stripe profiles`
  - `stripe status --profile example`
  - `stripe balance --profile example`
  - `stripe payouts --profile example --days 30 --limit 3`
  - `stripe report types --profile example`

`report run` is safe to smoke test but consumes report-generation quota on the account; prefer
`report types` for a connectivity check.

## Provider notes

This integration is self-contained: its provider contract lives in this reference, not in a
separate shared folder.

- Base URL is `https://api.stripe.com/v1`; report CSVs download from `files.stripe.com` and the
  command refuses any other host.
- Authorization headers are dropped on cross-origin redirects.
- Stripe list endpoints cap `limit` at 100 per page; the command pages transparently.
- `429` and transient `5xx` responses retry with `Retry-After` when Stripe supplies it.
- Report types and their `data_available_start` / `data_available_end` ranges vary by account
  and by which Stripe products are enabled, so read `report types` rather than assuming a type
  id exists.
