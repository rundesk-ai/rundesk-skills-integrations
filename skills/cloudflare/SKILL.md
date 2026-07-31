---
name: cloudflare
description: Cloudflare domains, zones, registrar checks, and guarded DNS changes through the local cloudflare CLI. Use when a task mentions Cloudflare, DNS records, domain ownership, domain availability, registering a domain, nameservers, or zone config — even if nobody says "Cloudflare".
---

# Cloudflare

Run the bundled CLI at `$RUNDESK_SKILLS/cloudflare/scripts/cloudflare`. It loads
credentials itself; never inspect or print its credential file. Read `references/cli.md`
only for setup, output, API, or validation details.

Start with:

```sh
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" profiles
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" status --profile <profile>
```

## Reads (safe defaults)

```sh
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" zones --profile <profile> --limit 25
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" zone example.com --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" domains --profile <profile> --limit 25
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" check example.com --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" available example.com other.com --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" search "coffee shop" --limit 10 --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" dns example.com --profile <profile> --limit 50
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" dns example.com --type A --name www --profile <profile>
```

**Availability:** use `available` (real-time registry via `POST .../registrar/domain-check`).
`search` is keyword discovery only (cached) — confirm a pick with `available` before any
register dry-run. `registrable=false` + `reason=domain_unavailable` means taken.

Bound lists. Prefer CSV text over `--json` unless structured data is required.

## Mutations (owner-approved only)

`register`, `dns-add`, `dns-set`, and `dns-rm` are **dry-run without `--confirm`**.

Never pass `--confirm` unless Tim approved that exact domain/record change in this
conversation. Show the dry-run output first when proposing a write.

```sh
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" register new.example --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" dns-add example.com --type A --name www --content 1.2.3.4 --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" dns-set example.com RECORD_ID --content 5.6.7.8 --profile <profile>
"$RUNDESK_SKILLS/cloudflare/scripts/cloudflare" dns-rm example.com RECORD_ID --profile <profile>
```

## Gotchas

- **Zones vs Registrar:** `zones` = domains on the Cloudflare account (DNS/proxy).
  `domains` = Cloudflare Registrar registrations. A zone can exist without Registrar
  (DNS-only) and the reverse can appear during transfers.
- **`check`** answers both: on-account zone and registrar record when permissions allow.
- **Auth:** prefer API Token (`CLOUDFLARE_<PROFILE>_TOKEN`). Some Registrar calls still need
  Global API Key + email on the same profile.
- **Account id:** set `CLOUDFLARE_<PROFILE>_ACCOUNT_ID` when more than one account is visible.
- **Register contact:** purchase may need `CLOUDFLARE_<PROFILE>_CONTACT_*` fields; dry-run
  reports which contact fields are present without printing values.
- Never paste tokens, global keys, or full WHOIS contact blocks into Discord or GitHub.
