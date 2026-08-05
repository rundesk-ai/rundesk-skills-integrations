---
name: cloudflare
description: Cloudflare zones, registrar domains, domain checks, and guarded DNS mutations.
category: providers
---

# cloudflare

## Entry Point

- List profiles: `cloudflare profiles`
- Verify credentials: `cloudflare status --profile example`
- List accounts: `cloudflare accounts --profile example`
- List zones (domains you manage): `cloudflare zones --profile example --limit 25`
- Show one zone: `cloudflare zone example.com --profile example`
- List Registrar domains: `cloudflare domains --profile example`
- Check a domain: `cloudflare check example.com --profile example`
- Dry-run register: `cloudflare register new.example --profile example`
- Confirm register (owner-approved only): `... register new.example --profile example --confirm`
- List DNS: `cloudflare dns example.com --profile example --limit 50`
- Dry-run DNS create: `... dns-add example.com --type A --name www --content 1.2.3.4`
- Confirm DNS create: `... dns-add example.com --type A --name www --content 1.2.3.4 --confirm`
- Dry-run DNS update: `... dns-set example.com RECORD_ID --content 5.6.7.8`
- Confirm DNS update: `... dns-set example.com RECORD_ID --content 5.6.7.8 --confirm`
- Dry-run DNS delete: `... dns-rm example.com RECORD_ID`
- Confirm DNS delete: `... dns-rm example.com RECORD_ID --confirm`

Rundesk agents can use the installed `cloudflare` command the same way once the launcher is present.

Default list output is compact CSV-style rows. Use `--json` only when structured payloads are required.

Reads never mutate Cloudflare. `register`, `dns-add`, `dns-set`, and `dns-rm` are dry-run by default and perform a write only with `--confirm` after owner approval for that exact change.

## Validation

- Run `python3 "$RUNDESK_SKILLS/cloudflare/scripts/cloudflare.d/test-cloudflare.py"`.
- Tests are offline and use synthetic fixtures; they do not need Cloudflare credentials.
- Optional live read-only smoke tests after credentials exist:
  - `cloudflare profiles`
  - `cloudflare status --profile example`
  - `cloudflare zones --profile example --limit 5`
  - `cloudflare dns example.com --profile example --limit 5`

Never run `register --confirm` or DNS mutation `--confirm` as a smoke test.

## Provider

This integration talks to the Cloudflare API v4 (`https://api.cloudflare.com/client/v4`) with stdlib Python only. One profile maps to one Cloudflare credential set and optional default account id.

### Recommended Connection

Prefer an **API Token** (Bearer). Registrar-only actions sometimes still need **Global API Key** + email on accounts that have not granted registrar scopes to tokens — the profile can hold either auth mode.

Minimum useful token permissions:

```text
Account Settings: Read
Zone: Read
DNS: Read
DNS: Edit          # only if DNS mutations are authorized
Account: Read
```

Registrar list/register needs Registrar-related permissions (or Global API Key). Domain purchase also needs registrant contact fields when the API requires them.

### Setup

Keep real tokens in the owner-only secrets dotenv (mode `600`). Never commit them. Never put them under `~/.rundesk/data/scripts` or skills.

Required (the only key `rundesk.json` declares): `CLOUDFLARE_API_TOKEN`. Optional per account: `CLOUDFLARE_ACCOUNT_ID` (resolved from the API when exactly one account is visible), `CLOUDFLARE_LABEL`, and the `CLOUDFLARE_EMAIL` + `CLOUDFLARE_GLOBAL_KEY` pair for the Registrar calls a token cannot make.

Two spellings resolve. For one field of one account, highest precedence first:

1. `CLOUDFLARE_<FIELD>__<ACCOUNT>` — the Rundesk-managed form, written by `rundesk skills configure`. Rundesk finds accounts by scanning this suffix, so a new account needs no declaration.
2. `CLOUDFLARE_<PROFILE>_<FIELD>` — this repository's older form, in a dotenv this command reads by hand.
3. the plain `CLOUDFLARE_<FIELD>` — the **default account only**.

The plain name is the default account. A named account never falls back to a plain value, so one account's token is never paired with another account's id; a named account missing a key reports the Rundesk spelling, such as `CLOUDFLARE_API_TOKEN__EXAMPLE`. The default account is the profile named `default`, an empty profile, or whatever `CLOUDFLARE_DEFAULT_PROFILE` names.

Rundesk-managed keys:

```dotenv
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ACCOUNT_ID=

CLOUDFLARE_API_TOKEN__EXAMPLE=
CLOUDFLARE_ACCOUNT_ID__EXAMPLE=
CLOUDFLARE_LABEL__EXAMPLE=Example Cloudflare
```

Older per-profile keys, still read unchanged:

```dotenv
CLOUDFLARE_PROFILES=example
CLOUDFLARE_DEFAULT_PROFILE=example

CLOUDFLARE_EXAMPLE_LABEL=Example Cloudflare
CLOUDFLARE_EXAMPLE_TOKEN=
CLOUDFLARE_EXAMPLE_ACCOUNT_ID=

# Optional Global API Key auth (instead of or in addition to token for Registrar):
# CLOUDFLARE_EXAMPLE_EMAIL=owner@example.com
# CLOUDFLARE_EXAMPLE_GLOBAL_KEY=

# Optional registrant contact for register --confirm:
# CLOUDFLARE_EXAMPLE_CONTACT_FIRST_NAME=
# CLOUDFLARE_EXAMPLE_CONTACT_LAST_NAME=
# CLOUDFLARE_EXAMPLE_CONTACT_ORGANIZATION=
# CLOUDFLARE_EXAMPLE_CONTACT_ADDRESS=
# CLOUDFLARE_EXAMPLE_CONTACT_CITY=
# CLOUDFLARE_EXAMPLE_CONTACT_STATE=
# CLOUDFLARE_EXAMPLE_CONTACT_ZIP=
# CLOUDFLARE_EXAMPLE_CONTACT_COUNTRY=
# CLOUDFLARE_EXAMPLE_CONTACT_PHONE=
# CLOUDFLARE_EXAMPLE_CONTACT_EMAIL=
```

Legacy bare aliases, read for the default account only: `CF_API_TOKEN` for `CLOUDFLARE_API_TOKEN`, and `CLOUDFLARE_API_KEY` for `CLOUDFLARE_GLOBAL_KEY`.

The registrant contact keys resolve through both spellings too — `CLOUDFLARE_CONTACT_EMAIL__EXAMPLE`, `CLOUDFLARE_EXAMPLE_CONTACT_EMAIL`, or the plain `CLOUDFLARE_CONTACT_EMAIL` for the default account. `rundesk.json` does not declare them, because only a confirmed `register` reads them.

`api` and `contact` cannot be account names in the older `CLOUDFLARE_<PROFILE>_<FIELD>` form, because `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_CONTACT_EMAIL` are field names in their own right. Name such an account with `CLOUDFLARE_API_TOKEN__CONTACT`, or list it in `CLOUDFLARE_PROFILES`.

Credential search order: process env → `--env-file` → `CLOUDFLARE_ENV_FILE` →
`RUNDESK_INTEGRATIONS_ENV` → `~/.config/rundesk/integrations/cloudflare/env` → legacy
`~/.config/cloudflare/env`. Keep the file outside the catalog and mode `0600`.

### Mutation boundary

| Command | Default | With `--confirm` |
|---|---|---|
| `register` | Prints plan only | Purchases via Registrar API |
| `dns-add` | Prints plan only | Creates one DNS record |
| `dns-set` | Prints plan only | Patches one DNS record |
| `dns-rm` | Prints plan only | Deletes one DNS record |

Owner authorization for the exact domain/record is required in addition to `--confirm`. Agents must not confirm mutations unless the owner approved that exact action in the conversation.
