---
name: stripe
description: Review Stripe accounts and pull revenue, payout, subscription, dispute, and report data with the bundled CLI. Use for balances, payouts, MRR or revenue questions, chargebacks, failed payments, customer billing lookups, month-end or reconciliation reporting, and any task mentioning Stripe, even when the account is not named.
---

# Stripe

Run the bundled CLI at `$RUNDESK_SKILLS/stripe/scripts/stripe`. It loads credentials itself;
never inspect or print its credential file.

Start with `"$RUNDESK_SKILLS/stripe/scripts/stripe" profiles`, then choose the profile whose
label and account match the task. Never guess a profile when more than one is configured —
Stripe profiles are separate businesses, and reporting one account's numbers as another's is
worse than returning nothing.

```sh
"$RUNDESK_SKILLS/stripe/scripts/stripe" profiles
"$RUNDESK_SKILLS/stripe/scripts/stripe" balance --all-profiles
"$RUNDESK_SKILLS/stripe/scripts/stripe" revenue --profile <profile> --days 30
"$RUNDESK_SKILLS/stripe/scripts/stripe" payouts --profile <profile> --days 30 --limit 10
"$RUNDESK_SKILLS/stripe/scripts/stripe" subscriptions --profile <profile> --status active --limit 25
"$RUNDESK_SKILLS/stripe/scripts/stripe" disputes --profile <profile> --days 30
"$RUNDESK_SKILLS/stripe/scripts/stripe" customer <cus_id|email> --profile <profile>
```

## Check the mode before quoting any number

`profiles` prints `mode=live`, `mode=test`, or `mode=unknown`, derived from the key prefix.
A test-mode profile returns fully-formed but fictional balances, charges, and payouts. Say
which mode produced a figure whenever it is not live, and never present test data as revenue.

## Money is in minor units, and not every currency has cents

The CLI already converts amounts for text output, so read its columns rather than dividing
raw values yourself. This matters when using `--json`: those amounts are still in Stripe's
smallest unit, and JPY, KRW, VND, CLP and the other zero-decimal currencies have no cents at
all — dividing them by 100 understates the figure a hundredfold. BHD, JOD, KWD, OMR and TND
use three decimal places.

## What each read actually means

- `balance` is money Stripe currently holds, split into `available` and `pending`. It is not
  what has landed in the bank; `payouts` is the transfer to the bank account.
- `revenue` rolls up **balance transactions** — Stripe's record of money movement, grouped by
  currency and type with a `TOTAL` row per currency. It is not invoiced revenue and not
  accrual-recognized revenue. For accounting-grade figures use `report run`.
- `subscriptions` defaults to `--status active`. Canceled, past-due, and trialing
  subscriptions are invisible until you pass the status you want or `--status all`.
- `charges` covers a short recent window by default; widen it with `--days` deliberately
  rather than habitually.

## Reports

`report types` lists what the account can actually run, with each type's available data range;
a run outside that range fails. `report run` creates the report, waits for it, and writes the
CSV only when given `--out`:

```sh
"$RUNDESK_SKILLS/stripe/scripts/stripe" report types --profile <profile>
"$RUNDESK_SKILLS/stripe/scripts/stripe" report run --profile <profile> \
  --type balance.summary.1 --start 2026-07-01 --end 2026-08-01 --out ./balance.csv
```

`--end` is exclusive, so a full month is the first of one month to the first of the next.
Report runs can outlive the wait; if one is still pending, retrieve it later with
`report status <frr_id> --out <path>` rather than starting a second run.

## Boundaries

Every command reads Stripe. There is no refund, cancel, payout, or customer-write command in
this package, and adding one is a deliberate change, not a convenience. The single POST is
`report run`, which creates a report artifact and cannot alter a balance, customer,
subscription, or payment.

Configure the profile's key as a **restricted key with read-only scopes**, so an error is the
worst outcome of a confused turn. A `403` therefore means a missing scope on the key, not
missing data in the account — say so instead of reporting the resource as absent.

Text output redacts email and IP values. `--json` is raw Stripe payloads containing customer
names, emails, addresses, and card metadata; use it only when a task genuinely needs a field
the text output omits, and never paste it into chat or a committed file.

Reads are bounded by `--limit`. When more records exist than were shown, the command says so
on stderr — carry that caveat into the answer rather than presenting a partial list as
complete.

Read `references/cli.md` only for setup, environment keys, connected-account routing, output
contracts, or validation.
