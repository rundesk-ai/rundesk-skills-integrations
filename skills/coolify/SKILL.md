---
name: coolify
description: Inspect Coolify resources and preview guarded restarts or deploys with the bundled CLI. Use for servers, apps, services, databases, deployments, logs, self-hosted operations, or container status—even when Coolify is not named.
---

# Coolify

Run the bundled CLI at `$RUNDESK_SKILLS/coolify/scripts/coolify`. It loads credentials
itself; never inspect or print its credential file. Read `references/cli.md` only for
setup, output, API, or validation details.

Start with:

```sh
"$RUNDESK_SKILLS/coolify/scripts/coolify" profiles
"$RUNDESK_SKILLS/coolify/scripts/coolify" status --profile <profile>
```

## Reads

```sh
"$RUNDESK_SKILLS/coolify/scripts/coolify" servers --profile <profile> --limit 25
"$RUNDESK_SKILLS/coolify/scripts/coolify" applications --profile <profile> --limit 50
"$RUNDESK_SKILLS/coolify/scripts/coolify" application <uuid> --profile <profile>
"$RUNDESK_SKILLS/coolify/scripts/coolify" services --profile <profile> --limit 50
"$RUNDESK_SKILLS/coolify/scripts/coolify" databases --profile <profile> --limit 50
"$RUNDESK_SKILLS/coolify/scripts/coolify" projects --profile <profile>
"$RUNDESK_SKILLS/coolify/scripts/coolify" resources --profile <profile> --limit 50
"$RUNDESK_SKILLS/coolify/scripts/coolify" deployments --uuid <app_uuid> --limit 10
"$RUNDESK_SKILLS/coolify/scripts/coolify" logs application <uuid> --lines 100
"$RUNDESK_SKILLS/coolify/scripts/coolify" envs application <uuid>
```

Bound lists. Prefer CSV text over `--json`. `envs` redacts values unless
`--show-values` (never paste secrets into chat).

## Mutations (owner-approved only)

`start`, `stop`, `restart`, and `deploy` are **dry-run without `--confirm`**.

Never pass `--confirm` unless the owner approved that exact resource action in this
conversation. Show dry-run output first.

```sh
"$RUNDESK_SKILLS/coolify/scripts/coolify" restart application <uuid> --profile <profile>
"$RUNDESK_SKILLS/coolify/scripts/coolify" deploy --uuid <uuid> --profile <profile>
"$RUNDESK_SKILLS/coolify/scripts/coolify" stop service <uuid> --profile <profile>
```

## Gotchas

- **Base URL** is the Coolify host origin only (`https://coolify.example.com`). The CLI
  appends `/api/v1`.
- Tokens are **team-scoped**. Resources from other teams need another profile/token.
- UUIDs are the stable identifiers — names can collide.
- `deploy` can take `--uuid` and/or `--tag` (comma-separated). `--force` rebuilds without cache.
- Lifecycle actions are asynchronous; check `deployments` / resource status after confirm.
