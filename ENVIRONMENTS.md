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

1. Variables already present in the command process. Rundesk manages credentials itself and feeds
   them to the command here, so a Rundesk-configured skill never needs a file on disk.
2. The path passed with `--env-file`.
3. The integration-specific variable, such as `CLOUDFLARE_ENV_FILE`.
4. The shared opt-in path in `RUNDESK_INTEGRATIONS_ENV`.
5. `${XDG_CONFIG_HOME:-$HOME/.config}/rundesk/integrations/<skill>/env`.
6. The legacy `${XDG_CONFIG_HOME:-$HOME/.config}/<skill>/env`, when present.

The isolated file is the default because installing or removing one skill never changes another
skill's account access. The shared file is useful for one owner managing many integrations and is
always explicit; it is not copied into a catalog or Rundesk data.

### What a skill declares

Every package carries a `rundesk.json` beside its `SKILL.md` with exactly one key:

```json
{"needs": {"JIRA_API_TOKEN": "an Atlassian API token from id.atlassian.com, under Security > Create and manage API tokens"}}
```

Each entry maps a plain, unprofiled variable name to why it is needed and where the value comes
from, in the order an owner should be asked for them. That declaration drives
`rundesk skills configure`, `rundesk skills profiles`, and `rundesk skills doctor`, whose report of
a missing value is the reason text a person reads. Declare only what is genuinely required: a value
a command uses when present, such as an optional self-hosted base URL or a label, is not a need.
Names must match `^[A-Z][A-Z0-9_]*$` and must not contain a double underscore, because the double
underscore is the account separator below.

### Two spellings for one account

Both forms resolve, and each command prefers them in this order for one field of one account:

1. `<DECLARED_NAME>__<ACCOUNT>` — **Rundesk-managed.** The double underscore separates the field
   from the account, so `JIRA_API_TOKEN__ACME` is the `acme` account's token and nothing else.
   Rundesk finds accounts by scanning stored names for this shape, so adding one is declared
   nowhere.
2. `<SKILL>_<ACCOUNT>_<FIELD>` — **a dotenv this repository's commands read by hand,** the original
   form, kept so no existing owner's file breaks. Rundesk cannot parse it: `JIRA_ACME_API_TOKEN` is
   equally the `acme` account of `JIRA_API_TOKEN` and the `acme-api` account of `JIRA_TOKEN`.
3. the plain `<DECLARED_NAME>` — the **default** account only.

A named account never falls back to a plain value. Without that rule one site's plain base URL
silently pairs with another site's account token, so a partly configured account reports the key it
is missing instead.

`<SKILL>_PROFILES` and `<SKILL>_DEFAULT_PROFILE` remain an explicit override: they name the accounts
and which one owns the plain values. When `<SKILL>_PROFILES` is absent, each command discovers
accounts from either spelling present in the environment:

- one account per `<DECLARED_NAME>__<ACCOUNT>` found, whatever the field;
- one account per `<SKILL>_<ACCOUNT>_<FIELD>` found, where `<SKILL>_DEFAULT_<FIELD>` is the infix
  spelling of the default account rather than an account named `default`;
- the default account itself when a plain value is set — listed even when only partly configured, so
  it carries its own error instead of vanishing.

That last one is **suppressed when the infix spelling is in use.** In that older world a plain value
was a fallback shared by every profile, not an account of its own, so inventing a `default` beside
`<SKILL>_PROD_<FIELD>` would make every command refuse as ambiguous for an owner who changed
nothing.

### Upgrading an existing dotenv

Nothing to do, with one exception: if `<SKILL>_PROFILES` names an account and that account's
credential lives in a plain variable, set `<SKILL>_DEFAULT_PROFILE` to the same name. That is what
tells the command those plain values belong to that account; otherwise a named account correctly
refuses to read them.

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
├── rundesk.json
├── references/cli.md
└── scripts/
    ├── example
    └── example.d/
        ├── example.py
        └── test-example.py
```

The launcher invokes its support file through a path resolved from the launcher's own directory, and
every launcher and script stays executable — Rundesk reports a non-executable script as a fault.
The command must provide credential-free `--help`, a credential/status check, bounded reads,
compact output, `--json` only on request, and dry-run mutations requiring `--confirm`. `rundesk.json`
declares its required variables, and the command resolves each through both spellings above. Tests
use synthetic fixtures and replace every network boundary; repository CI must work without
credentials.

Add the complete package to `manifest.json` and bump its semantic version in the same pull request.
One-skill repositories use this exact same contract with a one-entry `skills` list.
