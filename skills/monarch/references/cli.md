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
- The household's transaction rules: `monarch rules --profile household`

Only `status` accepts `--all-profiles`. Every other command acts on one profile so that a
figure is never a silent blend of two households.

## The write surface

Every command below previews and sends nothing until `--confirm` is passed on the same
invocation. There is no separate confirmation step and no stored consent.

| Command | Required | Optional | What it sends |
| --- | --- | --- | --- |
| `edit TXN...` | at least one of `--category`, `--merchant`, `--note` | `--confirm`, `--max N` | one `updateTransaction` per changed field per transaction |
| `tag TXN...` | at least one of `--add NAME`, `--remove NAME` (both repeatable) | `--confirm`, `--max N` | one `setTransactionTags` per changed transaction |
| `category create` | `--name`, `--group` | `--confirm`, `--max N` | one `createCategory` |
| `rule create` | `--merchant-contains`, `--category` | `--confirm`, `--max N` | one `createTransactionRuleV2` |
| `rule delete RULE_ID` | — | `--confirm`, `--max N` | one `deleteTransactionRule` |
| `budget set` | `--category`, `--month YYYY-MM`, `--amount N` | `--confirm`, `--max N` | one `updateOrCreateBudgetItem` |
| `undo BATCH` | — | `--confirm`, `--max N` | the inverse of each journalled change, newest first |
| `undo --list` | — | `--limit N`, `--json` | nothing; reads the local journal only |

`TXN...` takes one or more transaction ids from the first column of `transactions`, or `-` to
read a reviewed list from stdin, one id per line. Duplicates collapse.

`--max` defaults to 50 and is checked twice: `edit` and `tag` refuse on the number of transaction
ids **before** resolving any of them, so an oversized batch costs one error message rather than
one read per target, and every write refuses again on the number of changes it actually built.
Both checks apply to a dry run as well as a confirmed one.

`--note ""` clears a note. `--amount 0` clears a budget. `tag` resolves names against existing
tags and creates none; an unknown tag name is an error.

### What the write surface cannot do

Enforced in code and asserted by the offline suite, not merely documented:

- **No amount, date, account, or pending state.** `UpdateTransactionMutationInput` accepts all
  four; this package maps exactly three flags to exactly three input fields
  (`--category`→`category`, `--merchant`→`name`, `--note`→`notes`) and has no path to a fourth.
- **No transaction deletion, and no splits.**
- **No category deletion.** Deleting a category in Monarch reassigns every transaction filed
  under it.
- **No rule update, and no bulk rule deletion.**
- **No implicit tag creation.**

Adding any of these means editing the `MUTATIONS` allowlist in `monarch.d/monarch.py`, which the
suite pins to the six approved operations. That is the reviewed change the repository's rules
require.

### The undo journal

| Path | Mode | Contents |
| --- | --- | --- |
| `${XDG_STATE_HOME:-$HOME/.local/state}/rundesk/integrations/monarch/journal/<batch>.json` | 0600 in a 0700 directory | batch id, profile name, timestamp, state, and one record per change |

A batch id is `YYYYMMDDTHHMMSSZ-NN`, derived from the run's start time plus a counter, so two
batches in the same second do not collide.

Each change record holds the operation, the target's id, the field, the previous and new values,
their display forms, whether it can be reversed, and any note. **It holds no balance, no account
number, no email, and no credential**; the values it does hold are category ids and names,
merchant names, note text, tag ids, rule ids, and budget amounts.

Retention is manual: nothing prunes the directory, and a batch stays until it is deleted by
hand. `undo --list` shows the newest first with `--limit` defaulting to 25.

Undo semantics:

- A batch already reversed is **refused**, not replayed — the journal records its own reversal
  by moving to `state: undone`.
- A target that changed since this tool wrote it is **reported and skipped**, and the command
  exits non-zero. The point is to restore what this tool did, not to clobber a later edit.
- A batch that stopped part way is undoable for the part that landed; the changes that never
  landed read as changed-underneath and are left alone.
- **A created category cannot be reversed**, because that would require deleting a category.
  `category create` warns before it runs, and `undo` names the category, leaves it, and exits
  non-zero. A created rule *is* reversed, by deleting it.

### The read-after-write contract

Every applied change is re-read from Monarch and compared with what was requested:

| Change | Read back with | Compared on |
| --- | --- | --- |
| category, merchant, note | `GetTransactionDrawer` | category id, merchant name, note text |
| tags | `GetTransactionDrawer` | the set of tag ids, order-insensitively |
| category created | `GetCategories` | the new id is present |
| rule created or deleted | `GetTransactionRules` | the id is present, or gone |
| budget amount | `Common_GetJointPlanningData` | the planned amount, as a decimal |

The journal is written **before** the read-back, so a change that landed but failed verification
is still undoable. On the first mismatch the batch stops, names the target, and exits non-zero;
it does not continue through the rest.

`createTransactionRuleV2` returns no id, so `rule create` lists the rules before and after and
takes the one that appeared. If zero or more than one appeared it says so and journals the
change as not reversible rather than guessing at an id.

## Credentials are full account access

**Monarch issues no API key, no scoped token, and no OAuth.** The only programmatic path is the
private GraphQL API the Monarch web app uses, authenticated with the same email and password a
person types into the website. Unlike the Stripe package in this catalog — which is configured
with a restricted, read-only key so an error is the worst outcome of a confused turn — these
variables can do anything the account holder can do.

Two consequences follow, and they are the reason this package is shaped the way it is:

1. The command surface is the **only** boundary. Nothing the credentials could do is prevented by
   the credentials themselves, so what this package refuses to construct is the whole of the
   protection. That is why the write surface is an explicit allowlist of six operations rather
   than a general mutation path, why every write previews first, and why adding a seventh
   operation is a reviewed change and never a convenience.
2. The environment file must be owner-readable only, and the values must never be echoed, logged,
   or committed. The undo journal is held to the same rule: it records ids, names, and amounts,
   and never a credential.

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

There are **two spellings, and both resolve.** The first is Rundesk-managed; the second is a
dotenv this command reads by hand.

| Field | Required | Purpose |
| --- | --- | --- |
| `MONARCH_EMAIL` | yes | Monarch account email for that household. |
| `MONARCH_PASSWORD` | yes | Monarch account password. |
| `MONARCH_MFA_SECRET` | when MFA is on | Base32 TOTP seed from Monarch's authenticator setup. |
| `MONARCH_LABEL` | optional | Human-readable household name in output. |

`MONARCH_EMAIL` and `MONARCH_PASSWORD` are the two names declared in this package's
`rundesk.json`. `MONARCH_MFA_SECRET` is deliberately not declared there: the command uses it only
when the account has an authenticator app enabled.

**1. Rundesk-managed — `MONARCH_<FIELD>__<ACCOUNT>`.** An account name is a *suffix* after a
double underscore, and the plain, unsuffixed field name is the **default account**. This is what
`rundesk skills configure` writes, and a new account needs no declaration anywhere — the command
finds it by scanning for the suffix.

```sh
# the default account
MONARCH_EMAIL=agent@example.test
MONARCH_PASSWORD=synthetic-password
MONARCH_LABEL=Example Household

# a second account named `parents`
MONARCH_EMAIL__PARENTS=agent@example.com
MONARCH_PASSWORD__PARENTS=synthetic-password
MONARCH_LABEL__PARENTS=Example Parents
```

**2. This package's own dotenv — `MONARCH_<PROFILE>_<FIELD>`.** The older infix form, kept so an
existing environment file keeps working. Nothing writes it for you; it is read from the process
environment or the dotenv paths above.

```sh
MONARCH_PROFILES=household,parents
MONARCH_DEFAULT_PROFILE=household

MONARCH_HOUSEHOLD_EMAIL=agent@example.test
MONARCH_HOUSEHOLD_PASSWORD=synthetic-password
MONARCH_HOUSEHOLD_MFA_SECRET=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
MONARCH_HOUSEHOLD_LABEL=Example Household

MONARCH_PARENTS_EMAIL=agent@example.com
MONARCH_PARENTS_PASSWORD=synthetic-password
MONARCH_PARENTS_LABEL=Example Parents
```

For one field of one profile the order is: `MONARCH_<FIELD>__<PROFILE>`, then
`MONARCH_<PROFILE>_<FIELD>`, then the plain `MONARCH_<FIELD>`.

**A named account never falls back to a plain value.** The plain names belong to the default
account only, so a partly configured `parents` is an error naming `MONARCH_PASSWORD__PARENTS`
rather than a login that quietly pairs one household's email with another household's password.
"Default account" means no `--profile`, `--profile default`, or the name in
`MONARCH_DEFAULT_PROFILE`.

A profile name is upper-cased and non-alphanumeric characters become underscores in both forms, so
profile `joint-account` reads `MONARCH_EMAIL__JOINT_ACCOUNT` or `MONARCH_JOINT_ACCOUNT_EMAIL`.

Two more keys, unchanged:

| Key | Required | Purpose |
| --- | --- | --- |
| `MONARCH_PROFILES` | optional | Comma-separated profile names, in listing order. |
| `MONARCH_DEFAULT_PROFILE` | optional | Profile used when `--profile` is absent. |

When no `MONARCH_PROFILES` list is set, profiles are discovered from the environment: every
`MONARCH_<FIELD>__<ACCOUNT>` suffix, every legacy `MONARCH_<NAME>_EMAIL`, `_PASSWORD`,
`_MFA_SECRET`, or `_LABEL`, plus one default account when any plain required name is set. A single
discovered account is selected without `--profile`; two or more require it.

### Multi-factor authentication

If the Monarch account has an authenticator app enabled, `MONARCH_MFA_SECRET__<PROFILE>` — or the
plain `MONARCH_MFA_SECRET` for the default account, or the legacy
`MONARCH_<PROFILE>_MFA_SECRET` — is required. Obtain the seed from Monarch's
**Settings → Security → two-factor authentication** setup screen: when it shows the QR code there
is also a text seed (a base32 string such as `GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ`). That seed, not a
six-digit code, is what goes in the variable. Spacing and case are ignored. If the account is
already enrolled and the seed was not recorded, re-run the enrolment to be shown a new one.

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

`<profile>` in the session file name is the profile name with anything outside `A-Za-z0-9_-`
replaced by a hyphen, so an account discovered from the plain variable names caches to
`session-default.json`.

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
`robcerda/monarch-mcp-server`, and `pulsemcp/mcp-servers`); none of them is imported or vendored,
and this package is pure standard library.

**Every operation here is attested by two or more of those sources, and none is single-sourced.**
Where sources conflicted, the majority shape was taken and the alternative discarded: the budget
mutation appears elsewhere as `SetBudgetAmount` keyed on a `budgetId` this package cannot obtain,
and `keithah/monarchmoney-ts` renders several operations as top-level arguments where two other
sources agree on an input object. Nothing in this package rests on that last source.

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
| `edit`, `tag`, `undo` (resolve and read back) | `GetTransactionDrawer` | `getTransaction(id: UUID!, redirectPosted: Boolean)` |
| `category create` (resolve `--group`) | `ManageGetCategoryGroups` | `categoryGroups` |
| `tag` (resolve a tag name) | `GetHouseholdTransactionTags` | `householdTransactionTags(search:, limit:)` |
| `rules`, `rule create`, `rule delete` | `GetTransactionRules` | `transactionRules` |

Every one of those is a `query`. The mutations are a closed set, held in the `MUTATIONS`
allowlist; `mutate()` refuses to send anything that is not a value in it, and the offline suite
asserts both that every mutation document in the module is on the allowlist and that the
allowlist holds exactly these six:

| Command | Operation | Root field | Input |
| --- | --- | --- | --- |
| `edit` | `Web_TransactionDrawerUpdateTransaction` | `updateTransaction(input: UpdateTransactionMutationInput!)` | `{id, category?, name?, notes?}` |
| `tag` | `Web_SetTransactionTags` | `setTransactionTags(input: SetTransactionTagsInput!)` | `{transactionId, tagIds}` |
| `category create` | `Web_CreateCategory` | `createCategory(input: CreateCategoryInput!)` | `{group, name, icon}` |
| `rule create` | `Common_CreateTransactionRuleMutationV2` | `createTransactionRuleV2(input: CreateTransactionRuleInput!)` | `{merchantNameCriteria, setCategoryAction, applyToExistingTransactions}` |
| `rule delete` | `Common_DeleteTransactionRule` | `deleteTransactionRule(id: ID!)` | a bare `id`, **not** an input object |
| `budget set` | `Common_UpdateBudgetItem` | `updateOrCreateBudgetItem(input: UpdateOrCreateBudgetItemMutationInput!)` | `{categoryId, amount, timeframe, startDate, applyToFuture}` |

Three things about that table are easy to get wrong and were confirmed against two independent
community sources each:

- `edit --category` sends **`category`**, not `categoryId`, and `--merchant` sends **`name`**,
  not `merchant`.
- `setTransactionTags` **replaces** the whole tag set. `--add`/`--remove` therefore read the
  current tags first and send the resulting set, rather than a delta.
- `deleteTransactionRule` takes a bare `$id: ID!`. Wrapping it in an input object is rejected.

`applyToExistingTransactions` and `applyToFuture` are both pinned to `false`. Either one set true
would turn a single-item command into a write over an unbounded number of records.

**Mutations report failure twice over.** Beyond the top-level `errors` every GraphQL response can
carry, each mutation payload has its own `errors` field shaped
`PayloadError { message, code, fieldErrors { field, messages } }`. A populated one inside an HTTP
200 with no top-level error is still a refused write, and `mutate()` raises on it. Some
`deleteTransactionRule` responses omit `deleted` on success, so only an explicit `deleted: false`
is treated as a failure.

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
| `transactions` | `id`, `date`, `merchant`, `category`, `account`, `amount`, `pending` |
| `categories` | `group`, `type`, `name`, `id` |
| `budgets` | `group`, `category`, `budgeted`, `actual`, `remaining` |
| `cashflow` | `scope` (`total`/`group`), `name`, `type`, `amount` |
| `holdings` | `ticker`, `name`, `quantity`, `price`, `value` |
| `rules` | `id`, `matches`, `sets`, `applied` |
| `undo --list` | `batch`, `when`, `profile`, `changes`, `state` |
| every write's preview | `target`, `what`, `field`, `before`, `after` |

`transactions` leads with `id` because a write command can only name a transaction the read
already named. It is the value `edit` and `tag` take as a positional argument.

A write prints `key<TAB>value` lines around its preview table: `action`, `profile`, `changes`,
then the table, then `mode` (`dry-run`, `confirmed`, or `no-op`). A confirmed run adds `batch`,
`applied`, and an `undo` line carrying the exact command that reverses it. A write that finds
every target already holding the requested value prints `mode<TAB>no-op` and sends nothing.

Redaction in text output: an account mask prints as its last two digits only (`····34`), and a
profile email prints with its local part masked (`a***@example.test`). No full account number
reaches stdout under any flag. `--json` prints the raw Monarch payload with no redaction at all.

Long text is shortened for display and ends in `...` — an account or institution name at 40
characters, a transaction's account at 30. **A shortened name round-trips:** `--account` and
`--category` resolve an exact match first, then treat a trailing `...` as this tool's own mark and
match what precedes it as a prefix, then fall back to a unique substring. A shortened form that
fits two records is refused, naming both, and `--json` carries the untruncated values.

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

A write exits `0` for a dry run, a confirmed run in which every change landed and read back, and
a no-op. It exits `1` for:

| Command | Non-zero when |
| --- | --- |
| `edit` | no `--category`, `--merchant`, or `--note`; an unknown or ambiguous category; an unknown transaction id; more targets than `--max`; a refused mutation; a read-back mismatch |
| `tag` | no `--add` or `--remove`; a tag name that matches no existing tag, or more than one; the `edit` failures above |
| `category create` | an unknown or ambiguous group; a name a category already uses |
| `rule create` | an unknown or ambiguous category; an empty `--merchant-contains` |
| `rule delete` | a rule id that is not in `rules` |
| `budget set` | an unknown or ambiguous category; a `--month` that is not `YYYY-MM`; an `--amount` that is not a number |
| `undo` | an unknown batch id; a batch already undone; any change skipped because its target changed underneath or because it was never reversible |

A partly-applied batch exits `1` and says on stderr how many changes landed and the exact `undo`
command that reverses them.

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
  - `monarch rules --profile household`

Every smoke test above reads. None of them can change a record.

To smoke-test the write path without changing anything, run any write **without** `--confirm`:
it resolves the targets, prints the before/after table, and sends no mutation.

## Provider notes

This integration is self-contained: its provider contract lives in this reference, not in a
separate shared folder.

- **Stability.** The API is unofficial and undocumented. Field names, operation names, and the
  login flow can change without notice, and a change surfaces as a GraphQL error naming a field
  rather than as missing data. Repair is confined to `login()`, `graphql()`, `mutate()`, and the
  `QUERY_*` and `MUTATION_*` documents at the top of `monarch.d/monarch.py`.
- **A schema change on the write path fails safe.** A renamed input field is refused by the
  server, so the mutation raises rather than writing something unintended, and the read-after-write
  catches a write that was accepted but did not take.
- Balances are as of each institution's last sync, not live; `accounts` reports each account's
  own `updated` time and they routinely differ.
- Repeated logins can trip Monarch's CAPTCHA rate limiting. The session cache exists to keep the
  login count at roughly one per token lifetime; do not delete it in a loop.
- `TransactionFilterInput` requires `startDate` and `endDate` together; a half-window is rejected
  by the API, so the command always sends both.
