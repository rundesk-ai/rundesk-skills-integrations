# Codemap — rundesk-skills-integrations

Where each part lives. Counts are of artifacts, so they survive a rename and go wrong visibly when
the tree moves on without this page.

Every package is a directory under `skills/` holding a `SKILL.md`, the references it loads on
demand, and the one command it ships under `scripts/`. Nothing else in the repository is large.

## Packages (skills/ — 11, 15 reference files)

Each holds `SKILL.md` for routing and core procedure, and `references/` for detail loaded on demand.
`references/sources.md` is required in every touched package.

| Package | References | Command |
|---|---|---|
| `cloudflare` | 1 | yes |
| `confluence` | 1 | yes |
| `coolify` | 1 | yes |
| `discord` | 1 | yes |
| `grafana` | 1 | yes |
| `jira` | 3 | yes |
| `monarch` | 2 | yes |
| `posthog` | 1 | yes |
| `sentry` | 1 | yes |
| `slack-fetch` | 2 | yes |
| `stripe` | 1 | yes |

11 of 11 packages ship a command under `scripts/`. The rest are guidance only.

## Identity (root)

| File | What it is |
|---|---|
| `manifest.json` | schema, name, version (`0.13.0`), and description |
| `README.md` | the consumer contract: what the catalog is, how to install it, and every package |
| `ENVIRONMENTS.md` | the runtime, configuration, and credential contract every package obeys |
| `AGENTS.md`, `CLAUDE.md` | the repository guide, byte-identical by contract |
| `RELEASING.md` | the publication contract |

## Tests (tests/ — 1 suite)

The repository contract: the manifest and the tree agree, every package is complete and correctly
named, the README lists exactly what ships, and the guide pair stays byte-identical.

## Automation (.github/)

Issue templates, the pull-request template, and the workflow that runs the suite.

## Documentation (docs/)

`README.md`, `BRIEF.md`, and `CODEMAP.md` at the root.
