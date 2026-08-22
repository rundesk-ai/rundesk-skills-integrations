# Rundesk Integration Skills

Reusable Agent Skills that package guarded service CLIs with their operating guidance, credential
declarations, and offline tests. Every skill is an independently installable runtime.

## Skills

- `cloudflare` - zones, domains, registrar checks, and guarded DNS or domain changes.
- `confluence` - spaces, trees, search, and page content.
- `coolify` - servers, resources, deployments, logs, and guarded operational changes.
- `discord` - servers, channels, threads, history, and guarded messages, replies, and reactions.
- `grafana` - read-only Grafana Loki discovery, labels, bounded LogQL searches, filters, and error
  investigation through Grafana's authenticated data-source proxy.
- `jira` - projects, issues, viewable comments, attachment metadata/downloads, and guarded issue creation, editing, and one-file uploads.
- `monarch` - financial accounts, transactions, budgets, cash flow, and holdings; guarded edits to a
  transaction's category, merchant, notes, and tags; category creation; transaction-rule creation
  and deletion; budget setting; and undo. It cannot change a transaction's amount, date, or account,
  delete a transaction or category, or split a transaction.
- `posthog` - bounded product analytics reads for projects, event definitions, events, persons,
  session recording metadata, web analytics, saved insights, HogQL queries, and trend, traffic,
  audience, lead, and conversion presets. It has no capture, configuration, key-management, or
  mutation commands.
- `sentry` - projects, issue evidence, event inspection, and guarded resolution previews.
- `slack-fetch` - read-only channel and direct-message discovery, bounded message history, search,
  and complete thread reads through Slack's Web API.
- `stripe` - balances, revenue, payouts, subscriptions, disputes, and reports, with writes limited to
  creating a report artifact.

## Install

Rundesk CLI installs the complete catalog and keeps credentials, profiles, caches, and state outside
the package tree. Installation grants no skill automatically.

```sh
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations
rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations --confirm
rundesk skills grant agent-name rundesk-skills-integrations/cloudflare
```

The first install command previews the exact change; `--confirm` applies it. Skills use the verified
`<catalog>/<skill>` grant syntax. Updates and removal follow the same preview-first contract:

```sh
rundesk skills update rundesk-skills-integrations
rundesk skills update rundesk-skills-integrations --confirm
rundesk skills remove rundesk-skills-integrations
rundesk skills remove rundesk-skills-integrations --confirm
```

Configure one package's declared values and inspect its profiles without exposing secret values:

```sh
rundesk skills configure rundesk-skills-integrations/jira
rundesk skills profiles rundesk-skills-integrations/jira
rundesk skills doctor agent-name
```

To use a package without Rundesk, copy or symlink its complete `skills/<name>/` directory into the
skill directory supported by the agent runtime. Preserve `rundesk.json`, references, scripts, and
executable bits, and provide documented variables through your own secret manager.

## Requirements

- Python 3.9+ and the standard library. No package manager, virtual environment, or shared runtime is
  required.
- Credentials required by the chosen service, declared by variable name in its package-local
  `rundesk.json`. Never put secret values in the catalog, issue, logs, or command output.
- Explicit profile selection when more than one account is configured. A named account never falls
  back to plain default-account credentials.

Commands resolve process variables, explicit and package-specific env files, the opt-in
`RUNDESK_INTEGRATIONS_ENV`, isolated config, and supported legacy config in the exact order defined by
[ENVIRONMENTS.md](ENVIRONMENTS.md). That document also defines profile spellings, file permissions,
migration behavior, cache, and state.

## Repository layout

```text
.
├── .github/
│   ├── ISSUE_TEMPLATE/{bug-report.md,change-proposal.md}
│   └── pull_request_template.md
├── skills/
│   └── <name>/
│       ├── SKILL.md
│       ├── rundesk.json
│       ├── references/
│       │   ├── cli.md
│       │   └── <focused-reference>.md  optional
│       └── scripts/
│           ├── <name>
│           └── <name>.d/        implementation and offline tests
├── tests/test_catalog.py
├── AGENTS.md
├── CLAUDE.md
├── ENVIRONMENTS.md
├── RELEASING.md
└── manifest.json
```

Each package is an independent runtime, credential, profile, and removal boundary. Runtime files
never depend on a sibling package or a root-local library.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 skills/cloudflare/scripts/cloudflare.d/test-cloudflare.py -q
skills/cloudflare/scripts/cloudflare --help
repository_root="$(pwd)"
(cd /tmp && "$repository_root/skills/cloudflare/scripts/cloudflare" --help)
git diff --check
```

The root suite is the catalog gate and runs every package's offline suite. Tests use synthetic
fixtures and never contact live services. Read [AGENTS.md](AGENTS.md) before contributing for
approval, profile safety, privacy, validation, and documentation requirements.

## Creating a skill catalog

Use the organization-wide [skill catalog guide](https://github.com/rundesk-ai/rundesk-cli/blob/main/docs/catalogs.md)
for package structure, manifests, runtime isolation, credential declarations, public documentation,
testing, and release contracts. Extend an existing package when it already owns the service API or
command surface.

## Contributing

- Report reproducible incorrect behavior with the [bug report template](.github/ISSUE_TEMPLATE/bug-report.md).
- Propose a skill, integration, command, or repository improvement with the [change proposal template](.github/ISSUE_TEMPLATE/change-proposal.md).
- Prepare changes with the [pull request template](.github/pull_request_template.md) and provide
  evidence for the exact head commit.

Contributions must keep `README.md`, `manifest.json`, `skills/`, and catalog tests aligned and must
contain no credentials, personal data, private identifiers, or owner-specific paths.

## Releases

Follow [RELEASING.md](RELEASING.md) for semantic versioning, tags, and publication. Changes to
published catalog contents or runtime behavior require the version treatment it defines.
Process-only guide or template changes, including `AGENTS.md`, `CLAUDE.md`, and GitHub templates, do
not require a manifest version bump.

## License

This repository is licensed under the [MIT License](LICENSE).
