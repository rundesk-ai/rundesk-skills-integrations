# Rundesk Integration Skills

Reusable Agent Skills that package guarded service CLIs with their operating guidance and
offline tests. This catalog currently includes Cloudflare, Confluence, Coolify, Jira, and
Sentry.

```sh
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations --confirm
rundesk skills grant <agent> cloudflare
```

Installation makes every skill available and grants none automatically.

## Environment model

Every command is self-contained and uses Python's standard library, so installing this catalog
does not create a virtual environment or install a dependency. Credentials and profile routing
stay outside the catalog:

- isolated default: `${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/<skill>/env`;
- shared opt-in: set `RUNDESK_INTEGRATIONS_ENV` to one owner-only dotenv;
- explicit override: `<SKILL>_ENV_FILE` or the command's `--env-file` option.

Read [ENVIRONMENTS.md](ENVIRONMENTS.md) for precedence, permissions, migration, cache/state,
and the contract for building another integration. Maintainers use
[RELEASING.md](RELEASING.md).

## Included skills

- `cloudflare` — zones, domains, registrar checks, and guarded DNS/domain changes.
- `confluence` — spaces, trees, search, and page content.
- `coolify` — servers, resources, deployments, logs, and guarded operational changes.
- `jira` — projects, issues, comments, and attachment metadata.
- `sentry` — projects, issue evidence, event inspection, and guarded resolution previews.
