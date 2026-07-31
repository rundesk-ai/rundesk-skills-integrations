# AGENTS

This repository publishes Rundesk's reusable service integration skills.

- Every package is complete under `skills/<name>/`; no runtime file may live outside its skill.
- Commands use Python's standard library and install nothing into the machine or Rundesk.
- Reads are bounded by default. Mutations are previews until an exact `--confirm` request.
- Never include credentials, customer routing, private project names, local state, or owner paths.
- Follow `ENVIRONMENTS.md`: isolated configuration by default, shared dotenv only by opt-in.
- Credential-free `--help` and offline synthetic tests are required for every command.
- `manifest.json` is the catalog name, schema, version, and complete skill list.
- A version change updates `manifest.json`; release tags use that version prefixed with `v`.
