# Monarch CLI reference

The integration is Monarch Money-only. It talks to Monarch's API directly over HTTPS and needs
no Monarch CLI installation, browser session, or third-party Python package.

## Entry point

- List configured profiles: `monarch profiles`
- Check a profile logs in and see the household size: `monarch status --profile household`
- Check every configured household: `monarch status --all-profiles`
- List accounts with balances and sync times: `monarch accounts --profile household`
- Net worth over the last quarter: `monarch networth --profile household --days 90`
- Recent transactions: `monarch transactions --profile household --days 30 --limit 25`
- One account's transactions: `monarch transactions --profile household --account "Joint Checking"`
- One category's transactions: `monarch transactions --profile household --category Groceries`
- List categories and their groups: `monarch categories --profile household`
- Budget against actual for a month: `monarch budgets --profile household --month 2026-08`
- Income, expense, and savings: `monarch cashflow --profile household --days 30`
- One investment account's holdings: `monarch holdings --profile household --account "Brokerage"`

Only `status` accepts `--all-profiles`. Every other command acts on one profile so that a
figure is never a silent blend of two households.

## Credentials are full account access

**Monarch issues no API key, no scoped token, and no OAuth.** The only programmatic path is the
private GraphQL API the Monarch web app uses, authenticated with the same email and password a
person types into the website. Unlike the Stripe package in this catalog — which is configured
with a restricted, read-only key so an error is the worst outcome of a confused turn — these
variables can do anything the account holder can do.

Two consequences follow, and they are the reason this package is shaped the way it is:

1. The read-only command surface is the **only** boundary. There is no mutation command here and
   no `--confirm` path, because there is nothing to confirm. Adding a write command removes the
   boundary entirely, so it must be a deliberate, reviewed change and never a convenience.
2. The environment file must be owner-readable only, and the values must never be echoed, logged,
   or committed.

## Configuration

Credentials load from the first of these that exists: variables already in the process, the path
given to `--env-file`, `MONARCH_ENV_FILE`, `RUNDESK_INTEGRATIONS_ENV`, then
`${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/monarch/env`, then the legacy
`${XDG_CONFIG_HOME:-$HOME/.config}/monarch/env`.

```sh
mkdir -p "$HOME/.config/rundesk/integrations/monarch"
chmod 700 "$HOME/.config/rundesk" "$HOME/.config/rundesk/integrations" \
  "$HOME/.config/rundesk/integrations/monarch"
chmod 600 "$HOME/.config/rundesk/integrations/monarch/env"
```

The command warns on stderr when the file is readable by group or others, and never prints a
password, MFA seed, session token, authorization header, or raw dotenv contents.

### Environment keys

| Key | Required | Purpose |
| --- | --- | --- |
| `MONARCH_PROFILES` | recommended | Comma-separated profile names, in listing order. |
| `MONARCH_DEFAULT_PROFILE` | optional | Profile used when `--profile` is absent. |
| `MONARCH_<PROFILE>_EMAIL` | yes | Monarch account email for that household. |
| `MONARCH_<PROFILE>_PASSWORD` | yes | Monarch account password. |
| `MONARCH_<PROFILE>_MFA_SECRET` | when MFA is on | Base32 TOTP seed from Monarch's authenticator setup. |
| `MONARCH_<PROFILE>_LABEL` | optional | Human-readable household name in output. |

A profile name is upper-cased and non-alphanumeric characters become underscores, so profile
`joint-account` reads `MONARCH_JOINT_ACCOUNT_EMAIL`.

```sh
MONARCH_PROFILES=household,parents
MONARCH_DEFAULT_PROFILE=household

MONARCH_HOUSEHOLD_EMAIL=agent@example.test
MONARCH_HOUSEHOLD_PASSWORD=synthetic-password
MONARCH_HOUSEHOLD_MFA_SECRET=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
MONARCH_HOUSEHOLD_LABEL=Example Household

MONARCH_PARENTS_EMAIL=agent@example.test
MONARCH_PARENTS_PASSWORD=synthetic-password
MONARCH_PARENTS_LABEL=Example Parents
```

When no `MONARCH_PROFILES` list is set, profiles are discovered from any `MONARCH_<NAME>_EMAIL`,
`_PASSWORD`, `_MFA_SECRET`, or `_LABEL` variables present.

### Multi-factor authentication

If the Monarch account has an authenticator app enabled, `MONARCH_<PROFILE>_MFA_SECRET` is
required. Obtain the seed from Monarch's **Settings → Security → two-factor authentication**
setup screen: when it shows the QR code there is also a text seed (a base32 string such as
`GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ`). That seed, not a six-digit code, is what goes in the
variable. Spacing and case are ignored. If the account is already enrolled and the seed was not
recorded, re-run the enrolment to be shown a new one.

The command computes the code itself with RFC 6238 TOTP from the standard library — SHA-1,
30-second step, six digits — and never prompts. An agent has no terminal, so a missing seed is a
hard error naming the variable to set rather than a hanging prompt.

Two related cases:

- **A Google-login-only account has no password.** Set one in Monarch first; this API path
  authenticates with email and password.
- **A `CAPTCHA_REQUIRED` response** means Monarch rate-limited the login, not that the password
  is wrong. Wait rather than retrying in a loop.

### Session and device cache

| Path | Mode | Contents |
| --- | --- | --- |
| `${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/monarch/session-<profile>.json` | 0600 | `{"token", "device", "saved"}` |
| `${XDG_STATE_HOME:-$HOME/.local/state}/rundesk/integrations/monarch/device.json` | 0600 | `{"device"}` |

The session token is a bearer credential, so it lives in the configuration tree, not the cache
tree that `ENVIRONMENTS.md` reserves for disposable data. The device id is durable, non-secret
operational state: a changing device id re-triggers MFA on every call, which is why it is
generated once and kept.

Delete the session file to force a fresh login. A cached token is reused until Monarch rejects
it; on an HTTP 401 or 403 the command discards it, logs in once more, and retries the request
exactly once.

## Transport contract

Recorded here so a future break is repairable without re-deriving it. **Monarch publishes no
developer API, so none of this is guaranteed and it may change without notice.** The contract
was established by reading the current source of the community clients
(`bradleyseanf/monarchmoneycommunity`, `thedavidweng/monarchmoney-cli`, `eshaffer321/monarch-go`,
`keithah/monarchmoney-ts`); none of them is imported or vendored, and this package is pure
standard library.

- Base host is `https://api.monarch.com`. The older `api.monarchmoney.com` is retired and is
  what produces the widely-reported 404 and 525 login failures.
- **Login** — `POST https://api.monarch.com/auth/login/`, JSON body
  `{"username": <email>, "password": …, "trusted_device": true, "supports_mfa": true, "totp": …}`
  where `totp` is present only when an MFA seed is configured.
- **Login headers** — `Accept: application/json`, `Content-Type: application/json`,
  `Client-Platform: web`, a `User-Agent`, `Origin: https://app.monarch.com`,
  `Referer: https://app.monarch.com/`, and `device-uuid: <stable uuid4>`.
- **MFA challenge** — HTTP 401 or 403, or an `error_code` of `MFA_REQUIRED` or
  `EMAIL_OTP_REQUIRED` inside an HTTP 200 body. It is answered by re-posting the same
  `/auth/login/` URL with `totp` filled in; there is no separate MFA endpoint in use.
- **Token** — read from `token` at the top level of the login response. A JWT-shaped value is a
  short-lived feature token rather than a session token and is refused.
- **Queries** — `POST https://api.monarch.com/graphql` with
  `{"operationName", "query", "variables"}`, `Authorization: Token <token>`, and the same
  `device-uuid`. Authorization headers are dropped on cross-origin redirects.
- **Errors** — GraphQL reports failure inside an HTTP 200 body under `errors`, so a 200 is not
  success on its own. The command raises with the joined messages.

### Named GraphQL operations

| Command | Operation | Root field |
| --- | --- | --- |
| `accounts`, `status`, and name resolution | `GetAccounts` | `accounts` |
| `networth` | `Common_GetAggregateSnapshots` | `aggregateSnapshots(filters: AggregateSnapshotFilters)` |
| `networth` fallback | `GetAggregateSnapshots` | same, without the asset/liability split |
| `transactions` | `GetTransactionsList` | `allTransactions(filters: TransactionFilterInput)` |
| `categories` | `GetCategories` | `categories` |
| `budgets` | `Common_GetJointPlanningData` | `budgetData(startMonth:, endMonth:)` |
| `cashflow` | `Web_GetCashFlowPage` | `aggregates(filters:, fillEmptyValues:)` and `aggregates(filters:, groupBy: ["categoryGroup"])` |
| `holdings` | `Web_GetHoldings` | `portfolio(input: PortfolioInput)` |

Every one is a `query`. There is no `mutation` document in this package, and the offline suite
asserts that.

`aggregateSnapshots` is requested with `assetsBalance` and `liabilitiesBalance` first. Those two
fields are attested by only one community source, so if the server rejects them by name the
command retries once with the two-source-attested `date`/`balance` shape, notes on stderr that
the split is unavailable, and prints `-` in those columns.

## Output contract

List commands print CSV-style rows with a header. `status` prints tab-separated `key<TAB>value`
lines.

| Command | Columns |
| --- | --- |
| `profiles` | `profile`, `label`, masked `email`, `mfa` |
| `status` | `profile`, `label`, `email`, `auth`, `session`, `accounts` |
| `accounts` | `type`, `subtype`, `institution`, `name`, `mask`, `balance`, `updated` |
| `networth` | `point` (`first`/`last`/`change`), `date`, `assets`, `liabilities`, `net` |
| `transactions` | `date`, `merchant`, `category`, `account`, `amount`, `pending` |
| `categories` | `group`, `type`, `name`, `id` |
| `budgets` | `group`, `category`, `budgeted`, `actual`, `remaining` |
| `cashflow` | `scope` (`total`/`group`), `name`, `type`, `amount` |
| `holdings` | `ticker`, `name`, `quantity`, `price`, `value` |

Redaction in text output: an account mask prints as its last two digits only (`····34`), and a
profile email prints with its local part masked (`a***@example.test`). No full account number
reaches stdout under any flag. `--json` prints the raw Monarch payload with no redaction at all.

Amounts are already in major units and print with two decimal places. **Monarch's API exposes no
per-account currency field**, so there is no currency column; figures are in the household's own
display currency. Dates render as `YYYY-MM-DD`, and sync timestamps as `YYYY-MM-DD HH:MM`.

Windows come from `--days N` and end today. `budgets` takes `--month YYYY-MM` and defaults to the
current month. Bounded reads stop at `--limit`; when Monarch reports more records than were
shown, the command writes `note: showing N of M …` to stderr and stdout stays clean for piping.

Exit codes: `0` on success, `1` on any error. Errors go to stderr as a single `error:` line, not
a traceback. Specifically non-zero: missing or rejected credentials (naming the profile and the
missing variable), an MFA challenge with no seed configured, an ambiguous or unmatched
`--account` or `--category`, and a GraphQL error. `status` exits non-zero when any selected
profile fails to authenticate.

## Validation

- Run `python3 "$RUNDESK_SKILLS/monarch/scripts/monarch.d/test-monarch.py"`.
- Tests are offline, use synthetic fixtures, replace every network boundary, and need no Monarch
  credentials. The TOTP implementation is asserted against the published RFC 6238 test vectors.
- Credential-free help: `monarch --help`.
- Optional live read-only smoke tests, cheapest first:
  - `monarch profiles`  (no network at all)
  - `monarch status --profile household`
  - `monarch accounts --profile household`
  - `monarch transactions --profile household --days 7 --limit 5`

Every smoke test above reads. None of them can change a record.

## Provider notes

This integration is self-contained: its provider contract lives in this reference, not in a
separate shared folder.

- **Stability.** The API is unofficial and undocumented. Field names, operation names, and the
  login flow can change without notice, and a change surfaces as a GraphQL error naming a field
  rather than as missing data. Repair is confined to `login()`, `graphql()`, and the `QUERY_*`
  documents at the top of `monarch.d/monarch.py`.
- Balances are as of each institution's last sync, not live; `accounts` reports each account's
  own `updated` time and they routinely differ.
- Repeated logins can trip Monarch's CAPTCHA rate limiting. The session cache exists to keep the
  login count at roughly one per token lifetime; do not delete it in a loop.
- `TransactionFilterInput` requires `startDate` and `endDate` together; a half-window is rejected
  by the API, so the command always sends both.
