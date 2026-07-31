---
name: jira
description: Reading Jira Cloud projects, issues, comments, and attachment metadata.
category: providers
---

# jira

## Use When

Use this integration when an agent needs Jira Cloud project, issue, comment, attachment, or issue-key resolution context across configured Atlassian profiles. The integration is read-only against Jira; the only write it performs is an explicit local attachment download to a user-provided output path.

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

Do not run `attachment --output --confirm` as a smoke test unless the owner confirms the exact profile, attachment id, and output path.

## Provider

This integration is self-contained: its provider contract lives here, in this README, not in a separate file or a shared folder.

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

### Setup

Keep real tokens in local `.env` only. Never commit them. Restrict the selected dotenv file to the owner (`chmod 600 .env`); the CLI warns when group or other permission bits are present.

Every `JIRA_<PROFILE>_BASE_URL` must be an HTTPS origin such as `https://example.atlassian.net`: do not include credentials, a path, query, or fragment.

Recommended local keys:

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

- `JIRA_<PROFILE>_PROJECTS` is the source of truth for default issue searches.
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
- The integration does not create, edit, transition, comment on, upload to, or delete Jira issues.

### Official References

- [Jira issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
- [Jira issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
- [Jira comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/)
- [Jira attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/)
