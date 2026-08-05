---
name: coolify
description: Coolify servers, applications, services, databases, deployments, and guarded start/stop/restart/deploy.
category: providers
---

# coolify

## Entry Point

Bundled command: `$RUNDESK_SKILLS/coolify/scripts/coolify`.

- `coolify profiles`
- `coolify status --profile example`
- `coolify servers --profile example --limit 25`
- `coolify applications --profile example --limit 50`
- `coolify application APP_UUID --profile example`
- `coolify services|databases|projects|resources --profile example`
- `coolify deployments --uuid APP_UUID --limit 10`
- `coolify logs application APP_UUID --lines 100`
- `coolify envs application APP_UUID` (values redacted unless `--show-values`)
- Dry-run restart: `coolify restart application APP_UUID`
- Confirm restart: `coolify restart application APP_UUID --confirm`
- Dry-run deploy: `coolify deploy --uuid APP_UUID`
- Confirm deploy: `coolify deploy --uuid APP_UUID --confirm`

Reads never mutate Coolify. `start`, `stop`, `restart`, and `deploy` are dry-run by default and require `--confirm` after owner approval for that exact resource.

## Validation

```sh
python3 "$RUNDESK_SKILLS/coolify/scripts/coolify.d/test-coolify.py"
"$RUNDESK_SKILLS/coolify/scripts/coolify" profiles
"$RUNDESK_SKILLS/coolify/scripts/coolify" status --profile example
"$RUNDESK_SKILLS/coolify/scripts/coolify" applications --profile example --limit 5
```

Never run lifecycle `--confirm` as a smoke test.

## Provider

Coolify REST API v1 (`{BASE_URL}/api/v1/...`) with Bearer token auth. Stdlib Python only.

### Setup

Keep real tokens in the process environment or a local dotenv only. Never commit them, and keep the selected dotenv owner-only (`chmod 600`); the CLI warns when group or other permission bits are present.

Every base URL, in either spelling, must be the dashboard origin such as `https://coolify.example.com`, with no `/api/v1` path (a trailing `/api/v1` is stripped), no credentials, query, or fragment.

#### Rundesk-managed keys

`rundesk skills configure` writes these; Rundesk owns their storage and this command only reads them from the process environment. An account is a `__<ACCOUNT>` suffix on the plain variable name, and the plain name **is** the default account. A new account needs no declaration — the command finds it by scanning the environment.

Required, per `rundesk.json`:

```text
COOLIFY_BASE_URL    the origin of your Coolify instance, no /api/v1 path
COOLIFY_API_TOKEN   an API token from Keys & Tokens → API tokens
```

Optional in either spelling: `COOLIFY_LABEL`.

```dotenv
# the default account
COOLIFY_BASE_URL=https://coolify.example.com
COOLIFY_API_TOKEN=

# a second account named `example-two`
COOLIFY_BASE_URL__EXAMPLE_TWO=https://coolify.example.test
COOLIFY_API_TOKEN__EXAMPLE_TWO=
COOLIFY_LABEL__EXAMPLE_TWO=Example Two Coolify
```

A profile name maps to an account suffix by uppercasing it and replacing every non-alphanumeric run with `_`, so `--profile example-two` reads `__EXAMPLE_TWO`.

#### This command's own dotenv keys

These are the older spelling. Rundesk does not manage them; the command reads them by hand from the dotenv it resolves, so they are the way to keep an existing local dotenv working.

```dotenv
COOLIFY_PROFILES=example,example-two
COOLIFY_DEFAULT_PROFILE=example

COOLIFY_EXAMPLE_LABEL=Example Coolify
COOLIFY_EXAMPLE_BASE_URL=https://coolify.example.com
COOLIFY_EXAMPLE_TOKEN=

COOLIFY_EXAMPLE_TWO_LABEL=Example Two Coolify
COOLIFY_EXAMPLE_TWO_BASE_URL=https://coolify.example.test
COOLIFY_EXAMPLE_TWO_TOKEN=
```

The bare `COOLIFY_TOKEN` and `COOLIFY_URL` still work as aliases of `COOLIFY_API_TOKEN` and `COOLIFY_BASE_URL`, for the default account only.

#### Resolution order

For one field of one profile, highest precedence first:

1. `<PLAIN_NAME>__<PROFILE>` — the Rundesk-managed account key.
2. `COOLIFY_<PROFILE>_<FIELD>` — this command's own dotenv key.
3. the plain `<PLAIN_NAME>`, then its bare alias — **only when that profile is the default account**.

A profile is the default account when it is unnamed, named `default`, or equal to `COOLIFY_DEFAULT_PROFILE`. A named account never falls back to a plain value, so one instance's base URL can never be paired with another instance's token.

A missing key is reported by its Rundesk spelling (`COOLIFY_API_TOKEN__EXAMPLE_TWO`, or the plain `COOLIFY_API_TOKEN` for the default account). Values are never printed.

Credential search: process env → `--env-file` → `COOLIFY_ENV_FILE` →
`RUNDESK_INTEGRATIONS_ENV` → `~/.config/rundesk/integrations/coolify/env` → legacy
`~/.config/coolify/env`. Keep the file outside the catalog and mode `0600`.

Create tokens in Coolify: **Keys & Tokens → API tokens** (team-scoped).

### Mutation boundary

| Command | Default | With `--confirm` |
|---|---|---|
| `start` / `stop` / `restart` | plan only | lifecycle action |
| `deploy` | plan only | deploy by uuid/tag |

`envs` hides values unless `--show-values`. Never paste secret values into Discord or GitHub.
