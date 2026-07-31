# Sentry CLI reference

## Use When

Use this integration when an agent needs Sentry issue context across configured Sentry profiles: profile discovery, project listing, bounded issue listing/search, issue detail, linked external issue references, recent event evidence, and guarded one-issue Sentry resolution.

The integration is Sentry-only. It may show external issue links that Sentry returns, including Jira-looking links, but it does not call Jira, transition Jira tickets, or depend on another integration.

## Entry Point

- List configured profiles: `sentry profiles`
- List visible projects: `sentry projects --profile example --limit 10`
- List projects across profiles: `sentry projects --all-profiles --limit 10`
- List recent unresolved issues: `sentry list --profile example --days 7 --limit 10`
- List configured-project issues across profiles: `sentry list --all-profiles --days 7 --limit 10`
- List recent production errors: `sentry search --profile example --project example-api --query "is:unresolved environment:production lastSeen:-1d" --limit 20`
- Search issues: `sentry search --profile example --query "is:unresolved" --limit 10`
- Search one project: `sentry search --profile example --project example-api --query "is:unresolved" --limit 10`
- Fetch issue detail: `sentry detail EXAMPLE-1 --profile example`
- Fetch raw detail with normalized fields: `sentry detail EXAMPLE-1 --profile example --json`
- Fetch recent events: `sentry events EXAMPLE-1 --profile example --limit 3`
- Inspect issue detail plus recent full event evidence: `sentry inspect EXAMPLE-1 --profile example --event-limit 1`
- Dry-run resolution: `sentry resolve EXAMPLE-1 --profile example`
- Confirm one Sentry resolution: `sentry resolve EXAMPLE-1 --profile example --confirm`

Default `list` and `search` output is compact CSV-style rows. Use `--json` only for debugging, exports, or consumers that need raw Sentry payloads.

Read commands never mutate Sentry. `resolve` is dry-run by default and performs a Sentry update only when `--confirm` is present.

Sentry issue search joins space-separated terms with implicit AND and does not support an
`OR` operator. Use an in-list such as `environment:[production,staging]` when alternatives
share one key, or run separate queries for unrelated alternatives.

## Validation

- Run `python3 "$RUNDESK_SKILLS/sentry/scripts/sentry.d/test-sentry.py"`.
- Tests are offline and use synthetic fixtures; they do not need Sentry credentials.
- Optional live read-only smoke tests:
  - `sentry profiles`
  - `sentry projects --profile example --limit 5`
  - `sentry search --profile example --query "is:unresolved" --limit 2`
  - `sentry detail EXAMPLE-1 --profile example`
  - `sentry inspect EXAMPLE-1 --profile example --event-limit 1`

Do not run `resolve --confirm` as a smoke test unless the owner approves the exact profile and issue.

## Provider

This integration is self-contained: its detailed provider contract lives in this reference, not in a separate shared folder.

The integration reads Sentry directly through the Sentry REST API. It does not require browser login state or Sentry CLI state. It supports multiple profiles through `.env` keys, where one profile maps to one Sentry organization, base URL, token, and default project slug list.

### Recommended Connection

Use a Sentry auth token stored only in local `.env`.

Minimum read access must allow:

```text
project listing
organization issue search
issue detail
issue events
external issue link reads
```

Relevant Sentry token scopes include `project:read` and `event:read`. Resolution requires `event:write` or `event:admin`.

### Setup

Keep real tokens in local `.env` only. Never commit them. Restrict the selected dotenv file to the owner (`chmod 600 .env`); the CLI warns when group or other permission bits are present.

Every `SENTRY_<PROFILE>_BASE_URL` must be an HTTPS origin such as `https://example.sentry.io`: do not include credentials, a path, query, or fragment.

Recommended local keys:

```dotenv
SENTRY_PROFILES=example,example-two
SENTRY_DEFAULT_PROFILE=example

SENTRY_EXAMPLE_LABEL=Example Sentry
SENTRY_EXAMPLE_BASE_URL=https://example.sentry.io
SENTRY_EXAMPLE_ORG=example-org
SENTRY_EXAMPLE_TOKEN=
SENTRY_EXAMPLE_PROJECTS=example-api,example-web

SENTRY_EXAMPLE_TWO_LABEL=Example Two Sentry
SENTRY_EXAMPLE_TWO_BASE_URL=https://example-two.sentry.io
SENTRY_EXAMPLE_TWO_ORG=example-two-org
SENTRY_EXAMPLE_TWO_TOKEN=
SENTRY_EXAMPLE_TWO_PROJECTS=example-mobile
```

Compatibility keys still work for existing local profiles. Keep any real profile name in local `.env`; committed docs use synthetic names only.

```dotenv
SENTRY_DEFAULT_PROFILE=example-legacy
SENTRY_EXAMPLE_LEGACY_BASE_URL=https://us.sentry.io
SENTRY_EXAMPLE_LEGACY_ORG=example-legacy-org
SENTRY_EXAMPLE_LEGACY_TOKEN=
SENTRY_EXAMPLE_LEGACY_PROJECTS=example-legacy-api
SENTRY_AUTH_TOKEN=
```

`SENTRY_AUTH_TOKEN` is a fallback token only. Prefer `SENTRY_<PROFILE>_TOKEN` for multi-profile use.

### Output Shape

`list` and `search` print one CSV row per issue:

```text
id,short_id,title,project,level,priority,status,events,users,first_seen,last_seen,profile
1234567890,EXAMPLE-1,Example failure,example-api,error,medium,unresolved,4,2,2026-06-20 12:00,2026-06-24 12:00,example
```

`detail --json` includes raw issue data, raw external issue link data, and a `normalized` object with issue id, short id, profile, org, project, title, status, counts, dates, permalink, assignee summary, and generic external issue references.

Human-readable issue and event output redacts email and IP values wherever they appear.
`inspect` fetches issue detail, generic linked external issue references, and recent full
event evidence. Text output summarizes event title, date, environment, release, whether a
user object exists, and up to three stack frame locations; it does not print request
bodies, cookies, raw user values, or full stack traces. Use `inspect --json` only when raw
Sentry payloads are explicitly needed.

Issue detail hints use canonical short IDs and show a dry-run `resolve_preview`; they never
include `--confirm`. A header-only list/search result means no issues matched, and the
agent should state that explicitly in its response.

### Project And Organization Rules

- One profile maps to one Sentry organization.
- `SENTRY_PROFILES` controls multi-org discovery.
- `SENTRY_<PROFILE>_PROJECTS` is the source of truth for default issue list/search bounds.
- `list` and `search` use configured projects by default; pass `--project` one or more times to choose explicit project slugs.
- Pass `--all-projects` only for a deliberate broad org search.
- Pass `--all-profiles` only for a deliberate broad multi-org scan.
- Numeric issue IDs are accepted exactly. Sentry short IDs such as `EXAMPLE-1` are resolved through organization issue search with short-id lookup and must match exactly one result case-insensitively; a sole fuzzy search result is rejected.

### Write Boundaries

Write support is limited to one command: `resolve`.

Rules for `resolve`:

1. Dry-run by default.
2. Require `--confirm` for every real Sentry update.
3. Update only one Sentry issue to `status=resolved`.
4. Never bulk-resolve.
5. Never call or mutate Jira, even when Sentry shows a linked Jira issue.
6. Use `--json` only when the raw update response is needed.

Provider base URLs must be HTTPS origins. Authorization is retained only for same-origin redirects and is stripped before a cross-origin redirect.

Do not add assignment, mute, delete, merge, discard, bookmark, public sharing, or bulk mutation support unless explicitly approved later with the same confirmation discipline.

### Official References

- [List an organization's issues](https://docs.sentry.io/api/events/list-an-organizations-issues/)
- [Retrieve an issue](https://docs.sentry.io/api/events/retrieve-an-issue/)
- [List an issue's events](https://docs.sentry.io/api/events/list-an-issues-events/)
- [Update an issue](https://docs.sentry.io/api/events/update-an-issue/)
- [List your projects](https://docs.sentry.io/api/projects/list-your-projects/)
- [Retrieve custom integration issue links for a Sentry issue](https://docs.sentry.io/api/integration/retrieve-custom-integration-issue-links-for-the-given-sentry-issue/)
