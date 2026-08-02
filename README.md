# Rundesk Integration Skills

Reusable Agent Skills that package guarded service CLIs with their operating guidance and
offline tests. `manifest.json` is the authoritative list; the [Included skills](#included-skills)
section below names the same set, and the catalog suite fails when the two disagree.

```sh
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations --confirm
rundesk skills grant <agent> cloudflare
```

Installation makes every skill available and grants none automatically.
If a custom skill already uses any declared name, the complete catalog installation fails and
leaves that custom package unchanged.

```sh
rundesk skills catalogs
rundesk skills update rundesk-skills-integrations
rundesk skills remove rundesk-skills-integrations
```

Every update restores the repository's complete package files, including scripts and executable
permissions. Credentials, caches, and state remain outside those packages. Removal requires
`--yes` and is refused while any integration skill is granted.

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
- `discord` — servers, channels, threads, history, and guarded messages, replies, and reactions.
- `jira` — projects, issues, comments, and attachment metadata.
- `monarch` — Monarch Money accounts, net worth, transactions, budgets, cashflow, and holdings.
  Read-only: the package has no command that can change a financial record.
- `sentry` — projects, issue evidence, event inspection, and guarded resolution previews.
- `stripe` — balances, revenue, payouts, subscriptions, disputes, and reports. Read-only apart
  from creating a report artifact.
