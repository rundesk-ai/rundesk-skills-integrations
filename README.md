# Rundesk Integration Skills

Reusable Agent Skills that package guarded service CLIs with their operating guidance and
offline tests. `manifest.json` is the authoritative list; the [Included skills](#included-skills)
section below names the same set, and the catalog suite fails when the two disagree.

```sh
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations --confirm
rundesk skills grant <agent> rundesk-skills-integrations/cloudflare
```

Installation previews until `--confirm`. It makes every skill available and grants none
automatically; a skill is addressed `<catalog>/<skill>`. If a custom skill already uses any declared
name, the complete catalog installation fails and leaves that custom package unchanged.

```sh
rundesk skills catalogs
rundesk skills update rundesk-skills-integrations
rundesk skills remove rundesk-skills-integrations --confirm
```

Every update restores the repository's complete package files, including scripts and executable
permissions. Credentials, caches, and state remain outside those packages. Removal requires
`--confirm` and is refused while any integration skill is granted.

## Credentials

Each package declares what it needs in its own `rundesk.json` — a variable name for each required
value, with why it is needed and where to get one:

```sh
rundesk skills configure          # prompt for each declared value, in the order declared
rundesk skills profiles           # list the accounts found for each skill
rundesk skills doctor             # report which declared value is missing
```

Rundesk stores those values and feeds them to a command as process environment variables. One
account per suffix, separated by a double underscore, and the plain name is the default account:
`JIRA_API_TOKEN` is the default site, `JIRA_API_TOKEN__ACME` is the `acme` site. Accounts are found
by scanning, so adding one is declared nowhere, and a named account never falls back to a plain
value.

## Environment model

Every command is self-contained and uses Python's standard library, so installing this catalog
does not create a virtual environment or install a dependency. Credentials and profile routing
stay outside the catalog:

- Rundesk-managed: the values above, already in the command's environment;
- isolated default: `${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/<skill>/env`;
- shared opt-in: set `RUNDESK_INTEGRATIONS_ENV` to one owner-only dotenv;
- explicit override: `<SKILL>_ENV_FILE` or the command's `--env-file` option.

A dotenv may use either spelling: Rundesk's `<FIELD>__<ACCOUNT>` or this repository's original
`<SKILL>_<ACCOUNT>_<FIELD>`, which still resolves so no existing file breaks.

Read [ENVIRONMENTS.md](ENVIRONMENTS.md) for precedence, permissions, migration, cache/state,
and the contract for building another integration. Maintainers use
[RELEASING.md](RELEASING.md).

## Included skills

- `cloudflare` — zones, domains, registrar checks, and guarded DNS/domain changes.
- `confluence` — spaces, trees, search, and page content.
- `coolify` — servers, resources, deployments, logs, and guarded operational changes.
- `discord` — servers, channels, threads, history, and guarded messages, replies, and reactions.
- `jira` — projects, issues, comments, and attachment metadata.
- `monarch` — Monarch Money accounts, net worth, transactions, budgets, cashflow, and holdings,
  plus guarded edits to a transaction's category, merchant, notes, and tags, category creation,
  transaction rules, and budget amounts. Every edit is a preview until an exact `--confirm`, is
  capped in bulk, and is reversible through the package's own undo journal. No command can change an
  amount, a date, or an account, split a transaction, or delete a transaction or a category.
- `sentry` — projects, issue evidence, event inspection, and guarded resolution previews.
- `stripe` — balances, revenue, payouts, subscriptions, disputes, and reports. Read-only apart
  from creating a report artifact.
