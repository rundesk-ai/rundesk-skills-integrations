---
name: monarch
description: Read a Monarch Money household with the bundled CLI — accounts, balances, net worth, transactions, categories, budget vs. actual, cashflow, and investment holdings. Use for net worth, spending, "what did we spend on X", budget questions, cashflow or savings rate, a transaction lookup, an account or investment balance, and any task mentioning Monarch, even when the household is not named.
---

# Monarch Money

Run the bundled CLI at `$RUNDESK_SKILLS/monarch/scripts/monarch`. It loads credentials itself;
never inspect or print its credential file.

Start with `"$RUNDESK_SKILLS/monarch/scripts/monarch" profiles`, then choose the profile whose
label matches the task. Never guess a profile when more than one is configured — Monarch profiles
are separate households, and reporting one household's finances as another's is worse than
returning nothing.

```sh
"$RUNDESK_SKILLS/monarch/scripts/monarch" profiles
"$RUNDESK_SKILLS/monarch/scripts/monarch" status --profile <profile>
"$RUNDESK_SKILLS/monarch/scripts/monarch" accounts --profile <profile>
"$RUNDESK_SKILLS/monarch/scripts/monarch" networth --profile <profile> --days 90
"$RUNDESK_SKILLS/monarch/scripts/monarch" transactions --profile <profile> --days 30 --limit 25
"$RUNDESK_SKILLS/monarch/scripts/monarch" categories --profile <profile>
"$RUNDESK_SKILLS/monarch/scripts/monarch" budgets --profile <profile> --month 2026-08
"$RUNDESK_SKILLS/monarch/scripts/monarch" cashflow --profile <profile> --days 30
"$RUNDESK_SKILLS/monarch/scripts/monarch" holdings --profile <profile> --account "<name>"
```

## Balances are as of each institution's last sync, not live

Every account carries its own `updated` column, and they diverge: a bank may have synced an hour
ago while a brokerage last synced yesterday. Read that column before quoting a figure, and say
when a number is stale. A total assembled from accounts with different sync times is an estimate,
not a statement balance.

## Bounded by default, and the caveat travels with the answer

`transactions` reads the last 30 days and prints 25 rows. `networth` looks back 90 days,
`cashflow` 30, and `budgets` covers the current month. When more records exist than were shown,
the command writes `showing N of M` to stderr. Carry that into the answer rather than presenting
a partial list as complete, and widen `--days` or `--limit` deliberately rather than habitually.

## Naming an account or a category

`--account` and `--category` match a configured name case-insensitively, preferring an exact
match and otherwise accepting a unique substring. An ambiguous or unmatched name **exits
non-zero** instead of quietly querying the whole household. Run `accounts` or `categories` first
to get the real name; a wrong account is worse than no answer.

## Reading each command

- `accounts` lists every account with its type, institution, balance, and sync time. Account
  numbers are redacted to their last two digits; the full mask is never printed.
- `networth` prints three rows — `first`, `last`, and `change` over the window. It is a daily
  snapshot series, so the `change` row is the movement between two dates, not a return.
- `transactions` is ordered by date. Negative amounts are money leaving the household.
- `budgets` compares `budgeted` against `actual` for one month, with `remaining` from Monarch
  itself. Mid-month, a large `remaining` is normal and is not an underspend.
- `cashflow` gives income, expense, and savings totals, then the largest category groups.
- `holdings` needs one investment account by name and prints ticker, quantity, price, and value.

## Boundaries

Every command reads. There is no command in this package that creates, updates, or deletes a
transaction, budget, goal, holding, or account, and adding one is a deliberate change, not a
convenience. This matters more here than in most integrations: Monarch issues no scoped or
read-only key, so the configured credentials are full account access and the read-only command
surface is the only boundary there is.

The data is a real household's finances. Text output is already redacted. `--json` is the raw
Monarch payload and carries merchant names, institution names, account identifiers, and exact
balances — use it only when a task genuinely needs a field the text output omits, and never paste
it into chat or a committed file.

## When the API changes under you

Monarch publishes no developer API. This package speaks the private GraphQL API the Monarch web
app uses, which carries no version guarantee. A schema change therefore surfaces as a GraphQL
error naming a field or type. That means **Monarch changed**, not that the household has no data
— say so, and do not report an empty or zeroed household. An HTTP 401 or 403 after a retry means
the stored credentials stopped working, again not missing data.

Read `references/cli.md` only for setup, environment keys, MFA, the transport contract, output
columns, or validation.
