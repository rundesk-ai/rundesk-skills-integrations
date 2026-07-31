# Integration Environments

An integration has three environments with different ownership. Keeping them separate makes
catalog installation safe and makes a package portable between owners.

## 1. Runtime: isolated by package

The complete runtime lives inside `skills/<name>/`: one launcher, its support code, references,
and offline tests. Launchers resolve files relative to themselves and never depend on the agent's
working directory.

The current integrations use `/usr/bin/env python3` and only the standard library. They create no
virtual environment, download no package, and modify no machine interpreter. A catalog installer
validates and copies files; it never executes repository setup code.

Do not share Python environments between integrations. Shared environments create cross-skill
version conflicts and let one catalog update break another. If a future integration requires a
third-party dependency, wait for Rundesk's declarative per-skill runtime support or ship a
self-contained executable; never run `pip install` against the machine or an undocumented shared
environment.

## 2. Credentials and profiles: isolated by default

Each command reads configuration in this order:

1. Variables already present in the command process.
2. The path passed with `--env-file`.
3. The integration-specific variable, such as `CLOUDFLARE_ENV_FILE`.
4. The shared opt-in path in `RUNDESK_INTEGRATIONS_ENV`.
5. `${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/<skill>/env`.
6. The legacy `${XDG_CONFIG_HOME:-$HOME/.config}/<skill>/env`, when present.

The isolated file is the default because installing or removing one skill never changes another
skill's account access. The shared file is useful for one owner managing many integrations and is
always explicit; it is not copied into a catalog or Rundesk data.

Credential files must be owner-readable only:

```sh
mkdir -p "$HOME/.config/rundesk/integrations/cloudflare"
chmod 700 "$HOME/.config/rundesk" "$HOME/.config/rundesk/integrations" \
  "$HOME/.config/rundesk/integrations/cloudflare"
chmod 600 "$HOME/.config/rundesk/integrations/cloudflare/env"
```

Commands warn when an env file is readable by group or others. They never print secret values,
authorization headers, or raw dotenv contents.

## 3. Cache and state: isolated and disposable where possible

Use `${XDG_CACHE_HOME:-$HOME/.cache}/rundesk/integrations/<skill>/` for compiled helpers,
response caches, and other disposable data. Use
`${XDG_STATE_HOME:-$HOME/.local/state}/rundesk/integrations/<skill>/` only for non-secret
durable operational state.

Credentials belong in the configuration directory, not state. Neither credentials nor mutable
state belongs below `$RUNDESK_SKILLS`, because catalog updates atomically replace that tree.

## Building another integration

Use this package shape:

```text
skills/example/
├── SKILL.md
├── references/cli.md
└── scripts/
    ├── example
    └── example.d/
        ├── example.py
        └── test-example.py
```

The launcher invokes its support file through a path resolved from the launcher's own directory.
The command must provide credential-free `--help`, a credential/status check, bounded reads,
compact output, `--json` only on request, and dry-run mutations requiring `--confirm`. Tests use
synthetic fixtures and replace every network boundary; repository CI must work without credentials.

Add the complete package to `manifest.json` and bump its semantic version in the same pull request.
One-skill repositories use this exact same contract with a one-entry `skills` list.
