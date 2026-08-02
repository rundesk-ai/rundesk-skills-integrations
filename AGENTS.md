# AGENTS

Rules for every agent working in this repository. These rules are law; where they conflict with your
general habits, this file wins.

This repository publishes **Rundesk's reusable service integration skills** — one guarded CLI per
service, packaged with its operating guidance and offline tests. `README.md` is what a person reads,
`ENVIRONMENTS.md` is the credential and state contract, `RELEASING.md` is how a version ships. This
file defines how you build here.

## Before you work

1. **Read `README.md`, `ENVIRONMENTS.md`, and the `SKILL.md` of every package you are touching.**
   Read a file before editing it.
2. **Load the skill that governs the artifact you are about to write.** Each one is law for that
   artifact, the same as this file:

   | Writing or changing | Follow |
   |---|---|
   | any `SKILL.md` | `writing-skills` |
   | any `.py` under `skills/` | `python-patterns` |
   | any `test-*.py` or `tests/test_catalog.py` | `python-testing` |
   | a new integration package | `building-integration-clis`, then the three above |
   | a pull request | `writing-github-pull-requests` |
   | a version bump, tag, or release | `RELEASING.md`, then `publishing-github-releases` |

   An agent that does not hold one of these skills still follows the rule; say in your report which
   ones you could not load, because silence reads as compliance.
3. **Check whether an existing package already owns the service** before adding one. Extend it
   rather than shipping a second integration for the same API.
4. When the owner raises a concern, investigate before contradicting — evidence, not a hunch.

## Hard gates — require explicit approval

- **A new mutation command.** Every package here is bounded by what it cannot do. Adding a verb that
  writes to a live service is the owner's call, not a convenience, and doubly so where the service
  issues no scoped or read-only credential.
- **A dependency.** The standard library is not a preference here, it is the contract — a catalog
  installer copies files and executes no setup code, so a package that needs `pip` cannot be
  installed at all.
- **Deletions.** Do not delete a package, a command, or a file outside the task's immediate scope.
- **Commits.** Do not commit or push unless told to.
- **This file.** Never modify `AGENTS.md` without approval.

## Never

- **Never let the catalog's public surface drift.** Adding, removing, or renaming a skill changes
  `manifest.json`, `README.md`, and the catalog suite **in the same commit**. A README naming five
  skills for a catalog of eight is how a reader learns the repository cannot be trusted, and it
  hides in a diff that only adds files. `tests/test_catalog.py` enforces this, so the rule survives
  an agent who forgets it.
- **Never put a runtime file outside its package.** Everything a command needs lives under
  `skills/<name>/`; nothing is shared between integrations, so removing one can never break another.
- **Never let a test reach the network.** Every network boundary is replaced with a synthetic
  fixture. CI has no credentials and must never need any.
- **Never commit a credential, a customer name, a private project name, a real account identifier,
  or an absolute owner path.** Examples use `example.test`. Reference a secret by variable name; the
  value stays in the environment.
- **Never print a token, password, authorization header, cookie, or raw dotenv content** to stdout,
  stderr, or a log — including inside an error message.
- **Never let a command report success it did not earn.** Work that did not happen writes to stderr
  and exits non-zero.
- **Never widen a read silently.** A truncated list says so on stderr, so a partial answer is never
  presented as a complete one.

## The package contract

```text
skills/<name>/
├── SKILL.md              when to reach for it, the safest defaults, the boundaries
├── references/cli.md     setup, credentials, output contract, validation — read on demand
└── scripts/
    ├── <name>            a launcher that resolves paths from its own location
    └── <name>.d/
        ├── <name>.py     the implementation
        └── test-<name>.py  the offline suite
```

- Credential-free `--help` exits 0.
- `profiles` lists configured accounts and makes no network call. The catalog suite runs it for
  every package against a synthetic dotenv.
- Reads are bounded by default and take `--limit`. Compact text for agent context; `--json` only
  when asked.
- Mutations are previews until an exact `--confirm`. Overwrites, broad deletes, and ambiguous
  account selection are refused.
- Configuration resolves in the order `ENVIRONMENTS.md` sets out. Credentials, caches, and state
  live outside this repository, because a catalog update replaces the package tree atomically.

## Tech stack

- **Runtime:** Python 3.9+ — the floor CI pins, because it is the oldest a fresh macOS ships. Start
  every module with `from __future__ import annotations`, or builtin generic annotations fail there.
- **Dependencies:** the standard library, and nothing else. See the hard gate above.
- **Tests:** `unittest`, offline, run directly.

## Build, test & run

```sh
python3 -m unittest discover -s tests -v                    # the gate
python3 skills/<name>/scripts/<name>.d/test-<name>.py -q    # one package on its own
skills/<name>/scripts/<name> --help                         # exits 0 with no credentials present
```

The catalog suite runs each package's own suite, so a package added to `manifest.json` is in the
gate the day it lands. Run the bundled command once from a directory outside the source tree to
prove the launcher resolves its own files.

CI runs the same command on Python 3.9 and 3.13, on Linux and macOS, with no credentials.

## Documentation duties

Keep the documentation true in the same task that changes reality.

- A skill added, removed, or renamed → `manifest.json`, `README.md`, and the expected-skill
  assertions in `tests/test_catalog.py`.
- A change to credential precedence, cache, or state → `ENVIRONMENTS.md`.
- A change to the release process → `RELEASING.md`.
- Setup, environment keys, output contracts, and validation belong in the package's
  `references/cli.md`, never in `SKILL.md`. `SKILL.md` carries triggers, defaults, boundaries, and
  the gotchas an agent could not infer.

## Definition of done

1. `python3 -m unittest discover -s tests -v` passes, and CI is green on every matrix cell.
2. Every rule here held — no dependency, no network in a test, no credential or owner path
   committed.
3. `README.md` and `manifest.json` agree with what the repository actually ships.
4. The governing skills in **Before you work** were followed, or your report names the ones you
   could not load.
