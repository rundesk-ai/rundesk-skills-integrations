---
name: coolify
description: Coolify servers, applications, services, databases, deployments, and guarded start/stop/restart/deploy.
category: providers
---

# coolify

## Use When

Use this integration when an agent needs Coolify instance context: servers, applications, one-click services, databases, projects, deployments, logs, env key inventory, and guarded lifecycle actions (start/stop/restart/deploy) after explicit owner approval.

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

```dotenv
COOLIFY_PROFILES=example
COOLIFY_DEFAULT_PROFILE=example
COOLIFY_EXAMPLE_LABEL=Example Coolify
COOLIFY_EXAMPLE_BASE_URL=https://coolify.example.com
COOLIFY_EXAMPLE_TOKEN=
```

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
