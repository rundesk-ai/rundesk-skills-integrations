---
name: jira
description: Reading Jira Cloud projects, issues, comments, and attachment metadata, plus guarded issue creation, editing, and one-file uploads.
category: providers
---

# jira

## Entry Point

- List configured profiles: `jira profiles`
- Verify credentials: `jira whoami --profile example`
- List visible projects: `jira projects --profile example`
- List configured-project issues: `jira list --profile example --limit 10`
- List one project: `jira list --profile example --project APP --limit 10`
- Run explicit JQL: `jira search --profile example --jql 'project = APP ORDER BY updated DESC' --limit 10`
- Fetch issue detail: `jira detail APP-252 --profile example --full`
- Fetch issue JSON with normalized fields: `jira detail APP-252 --profile example --full --json`
- Fetch comments: `jira comments APP-252 --profile example`
- Fetch attachment metadata: `jira attachments APP-252 --profile example`
- Dry-run one attachment download: `jira attachment --profile example --id EXAMPLE_ATTACHMENT_ID --output /tmp/example-attachment.png`
- Confirm one attachment download: `jira attachment --profile example --id EXAMPLE_ATTACHMENT_ID --output /tmp/example-attachment.png --confirm`
- Dry-run issue creation: `jira create --profile example --project APP --issue-type Task --summary "Example task"`
- Confirm issue creation: `jira create --profile example --project APP --issue-type Task --summary "Example task" --confirm`
- Dry-run issue edit: `jira edit APP-252 --profile example --summary "Updated title"`
- Confirm issue edit: `jira edit APP-252 --profile example --summary "Updated title" --confirm`
- View issue comments: `jira comments APP-252 --profile example`
- View issue comments in detail: `jira detail APP-252 --profile example`
- Dry-run one-file upload: `jira upload APP-252 --profile example --file /tmp/example.txt`
- Confirm one-file upload: `jira upload APP-252 --profile example --file /tmp/example.txt --confirm`
- Resolve issue keys from text: `jira identify 'Review APP-252' --all-profiles`

Default output is compact text for agent context. `list` and `search` print CSV-style rows. Use `--json` only for debugging, exports, or consumers that need raw plus normalized Jira fields.

## Validation

- Run `python3 "$RUNDESK_SKILLS/jira/scripts/jira.d/test-jira.py"`.
- Tests are offline and use synthetic fixtures; they do not need Jira credentials.
- Optional live read-only smoke tests:
  - `jira profiles`
  - `jira whoami --profile example`
  - `jira projects --profile example --limit 5`
  - `jira list --profile example --limit 3`
  - `jira detail APP-252 --profile example --full --json`
  - `jira attachments APP-252 --profile example`

Do not run `create --confirm`, `edit --confirm`, `upload --confirm`, or `attachment --output --confirm` as a smoke test
unless the owner confirms the exact profile and target/effect. Offline tests cover the write request
method, ADF description conversion, project allowlisting, multipart file upload, dry-runs, and confirmation paths.

## Provider

The integration reads Jira Cloud through the Atlassian REST API. It does not require browser login state or Atlassian CLI state. It supports multiple profiles through `.env` keys, where one profile maps to one Atlassian site/account credential and its known Jira project keys.

### Recommended Connection

Use an Atlassian account API token stored only in local `.env`.

Minimum Jira read access must allow:

```text
myself
project search
issue search
issue detail
issue comments
issue attachments
```

For OAuth-style apps, the broad classic read scope is `read:jira-work`. Granular scopes depend on the app model, but issue detail/comment/attachment reads map to Jira issue, comment, project, user/avatar, and attachment read scopes.

For create and edit, the Atlassian account also needs the Jira project permissions to create and edit
issues in the selected project. Jira Cloud's issue APIs use `POST /rest/api/3/issue` for creation and
`PUT /rest/api/3/issue/{issueIdOrKey}` for edits; descriptions are sent as Atlassian Document Format.
The account's existing API token remains the credential. OAuth apps additionally need the Jira write
scope documented by Atlassian.

For uploads, the account also needs Browse Projects and Create attachments for the issue's project.
The command sends one explicit local file as multipart form data and uses Jira's required
`X-Atlassian-Token: no-check` header. It never uploads a directory or recursively discovers files.

### Setup

`rundesk.json` declares what this skill needs: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`. `rundesk skills configure` prompts for each, `rundesk skills profiles` lists the accounts it finds, and `rundesk skills doctor` names any value still missing.

Every base URL must be an HTTPS origin such as `https://example.atlassian.net`: do not include credentials, a path, query, or fragment.

#### Rundesk-managed keys

Rundesk stores credentials itself and feeds them to the command as process environment variables. One account per suffix, separated by a double underscore; the plain name is the default account:

```dotenv
JIRA_BASE_URL=https://example.atlassian.net
JIRA_EMAIL=agent@example.com
JIRA_API_TOKEN=

JIRA_BASE_URL__EXAMPLE_TWO=https://example-two.atlassian.net
JIRA_EMAIL__EXAMPLE_TWO=agent@example.com
JIRA_API_TOKEN__EXAMPLE_TWO=
```

Accounts are found by scanning for `JIRA_<FIELD>__<ACCOUNT>`, so adding one needs no declaration. `JIRA_API_TOKEN__EXAMPLE_TWO` is the account `example-two`. Optional per-account keys are `JIRA_PROJECTS__<ACCOUNT>` and `JIRA_LABEL__<ACCOUNT>`.

A named account never falls back to a plain value. Without that rule one site's `JIRA_BASE_URL` would silently pair with another site's `JIRA_API_TOKEN`, so a partly configured account reports the key it is missing instead.

#### Dotenv keys this command reads itself

The older per-profile form still resolves, so an existing dotenv keeps working unchanged. It is a file this command reads by hand — Rundesk neither writes nor manages it:

```dotenv
JIRA_PROFILES=example,example-two
JIRA_DEFAULT_PROFILE=example

JIRA_EXAMPLE_LABEL=Example Jira
JIRA_EXAMPLE_BASE_URL=https://example.atlassian.net
JIRA_EXAMPLE_EMAIL=agent@example.com
JIRA_EXAMPLE_API_TOKEN=
JIRA_EXAMPLE_PROJECTS=APP,OPS

JIRA_EXAMPLE_TWO_LABEL=Example Two Jira
JIRA_EXAMPLE_TWO_BASE_URL=https://example-two.atlassian.net
JIRA_EXAMPLE_TWO_EMAIL=agent@example.com
JIRA_EXAMPLE_TWO_API_TOKEN=
JIRA_EXAMPLE_TWO_PROJECTS=ENG,HELP
```

`JIRA_PROFILES` and `JIRA_DEFAULT_PROFILE` stay an explicit override: when `JIRA_PROFILES` is absent the accounts are discovered from either spelling, and `JIRA_DEFAULT_PROFILE` names the account that owns the plain values.

Per field, for one account, the first value found wins: `JIRA_<FIELD>__<ACCOUNT>`, then `JIRA_<ACCOUNT>_<FIELD>`, then the plain `JIRA_<FIELD>` for the default account only.

Keep real tokens in the process environment or a local `.env` only. Never commit them. Restrict the selected dotenv file to the owner (`chmod 600 .env`); the CLI warns when group or other permission bits are present.

### Output Shape

`list` and `search` print one CSV row per issue:

```text
key,title,type,status,priority,assignee,updated,project,profile
APP-252,Example ticket title,Story,To Do,Medium,Alex Example,2026-06-23 12:34,APP,example
```

`detail --json` includes raw Jira issue data, paginated raw comments, and a `normalized` object with:

```text
key, id, profile, site, url, title, status, description, assignee, reporter,
creator, project, type, priority, updated, labels, components, fixVersions,
attachments, comments
```

Attachment bytes are never downloaded by `detail` or `attachments`; those commands list metadata only. `attachment --id ID --output PATH` is a dry-run by default. Add `--confirm` to download one attachment to an explicit local path. Existing paths and symlinks are rejected, and a confirmed download is published atomically without exposing a partial file or overwriting a racing target.

### Project Mapping Rules

- The account's project list (`JIRA_PROJECTS__<ACCOUNT>`, or `JIRA_<PROFILE>_PROJECTS`, or the plain `JIRA_PROJECTS` for the default account) is the source of truth for default issue searches.
- `list` is bounded to configured projects unless `--project` is provided.
- `search --jql` runs explicit JQL and should stay bounded by project in normal agent use.
- `identify --all-profiles` uses issue-key prefixes to try matching profiles first, then falls back to other configured profiles.
- Jira issue keys are not globally unique across sites, so output includes the profile.

### Safety Notes

- Tokens belong in local `.env`, never committed docs, examples, or chat logs.
- Provider base URLs must be HTTPS origins. Authorization is retained only for same-origin redirects and is stripped before a cross-origin redirect.
- Prefer service accounts for organization automation when available.
- Rotate any token that was pasted into chat or logs.
- Keep live checks small and bounded.
- Create and edit are guarded mutations: they print a dry-run and require `--confirm`.
- Create is bounded to the configured project allowlist and does not infer an issue type.
- Upload is bounded to one explicit regular file and the configured project allowlist.
- Comments remain read-only and viewable through `comments` and `detail`.
- The integration does not transition, comment on, or delete Jira issues.

### Official References

- [Jira issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
- [Jira issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
- [Jira comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/)
- [Jira attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/)
