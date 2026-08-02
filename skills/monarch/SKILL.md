---
name: monarch
description: Read a Monarch Money household with the bundled CLI — accounts, balances, net worth, transactions, categories, budget vs. actual, cashflow, investment holdings — and edit a transaction's category, merchant, or note, set tags, create a category, manage transaction rules, and set a budget amount, each previewing until --confirm and reversible with undo. Use for net worth, spending, "what did we spend on X", budget questions, cashflow or savings rate, a transaction lookup, an account or investment balance, recategorizing or tagging transactions, cleaning up miscategorized spending, and any task mentioning Monarch, even when the household is not named.
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
"$RUNDESK_SKILLS/monarch/scripts/monarch" rules --profile <profile>
```

Writes take the same shape, and the first run of each is a preview:

```sh
"$RUNDESK_SKILLS/monarch/scripts/monarch" edit <txn-id>... --category "<name>"
"$RUNDESK_SKILLS/monarch/scripts/monarch" edit <txn-id> --merchant "<name>" --note "<text>"
"$RUNDESK_SKILLS/monarch/scripts/monarch" tag <txn-id>... --add "<tag>" --remove "<tag>"
"$RUNDESK_SKILLS/monarch/scripts/monarch" category create --name "<name>" --group "<group>"
"$RUNDESK_SKILLS/monarch/scripts/monarch" rule create --merchant-contains "<text>" --category "<name>"
"$RUNDESK_SKILLS/monarch/scripts/monarch" rule delete <rule-id>
"$RUNDESK_SKILLS/monarch/scripts/monarch" budget set --category "<name>" --month 2026-08 --amount 750
"$RUNDESK_SKILLS/monarch/scripts/monarch" undo --list
"$RUNDESK_SKILLS/monarch/scripts/monarch" undo <batch-id> --confirm
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

`--account`, `--category`, `--group`, and a tag name match a configured name case-insensitively,
preferring an exact match, then a name this tool shortened for display, then a unique substring.
An ambiguous or unmatched name **exits non-zero** instead of quietly querying the whole
household — and on a write it does so before anything is sent. Run `accounts`, `categories`, or
`rules` first; a wrong account is worse than no answer, and a wrong category on a write is worse
than both.

Long names are shortened in text output and end in `...`. Pass one back exactly as printed —
the ellipsis is understood. If two accounts share that shortened form the command says so and
stops, and `--json` carries the full names.

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
- `rules` lists the household's transaction rules: what each matches and what it sets. A rule is
  the preventive half of a cleanup — one rule files future transactions without an agent in the
  loop, which is cheaper and more reliable than recategorizing the same merchant every month.

## Every write is a preview until you confirm it

`edit`, `tag`, `category create`, `rule create`, `rule delete`, `budget set`, and `undo` all
print exactly what they would change and **send nothing** until `--confirm` is passed on the
same command. Read the `before` and `after` columns before confirming; the preview is the
review, and there is no second prompt.

Three more things hold on every write:

- **A bulk cap of 50 targets.** More than that is refused outright. `--max N` raises it for that
  one run, so a large batch is always a deliberate act.
- **A read-after-write.** Each change is re-read and compared. On the first mismatch the batch
  stops rather than pressing on, and says which target failed.
- **An undo journal.** Monarch itself has no undo, so this package keeps one: each change's
  previous value is recorded before it changes, at
  `${XDG_STATE_HOME:-$HOME/.local/state}/rundesk/integrations/monarch/journal/`. `undo --list`
  shows the batches; `undo <batch> --confirm` puts them back. A batch that was interrupted is
  still undoable for the part that landed.

Name a transaction by the `id` in the first column of `transactions`. Several ids can be given
at once, and `-` reads a reviewed list from stdin, one per line.

`undo` will not overwrite a later edit: if a target has changed since this tool touched it, that
change is reported, skipped, and the command exits non-zero. It also refuses to replay a batch
it has already reversed.

## Boundaries

The write surface is small on purpose, and the owner approved exactly this set on 2026-08-02:
a transaction's category, merchant, and notes; tags; creating a category; transaction rules;
budget amounts; and undo.

**No command in this package can:**

- change a transaction's amount, date, account, or pending state — there is no flag that reaches
  them and no code path that constructs them;
- delete a transaction, or split one;
- delete a category — deleting one in Monarch reassigns every transaction filed under it, which
  is a bulk change wearing a single-item costume;
- create a tag implicitly — `tag` resolves names against tags that already exist and refuses one
  it cannot find.

Adding any of those is a deliberate, reviewed change requiring the owner's approval, not a
convenience. This matters more here than in most integrations: Monarch issues no scoped or
read-only key, so the configured credentials are full account access, and the command surface is
the only boundary there is.

One asymmetry to know before you use it: **`category create` cannot be undone by this package**,
because undoing it would mean deleting a category. The command says so before it runs, and
`undo` names the category and leaves it standing rather than removing it. Everything else in the
approved set reverses cleanly.

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
columns, each write command's flags and exit codes, the journal's location and retention, or
validation.
