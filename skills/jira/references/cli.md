---
name: jira
description: Use when an agent needs Jira Cloud project, issue, epic, board, sprint, backlog, comment, or attachment reads, or guarded issue creation, editing, commenting, epic/sprint assignment, deletion, or one-file uploads.
category: providers
---

# jira

## Entry Point

- List configured profiles: `jira profiles`
- Verify credentials: `jira whoami --profile example`
- List visible projects: `jira projects --profile example`
- Discover Jira Software boards: `jira boards --profile example --project APP`
- List board epics: `jira epics --profile example --board-id 42`
- List issues in an epic: `jira epic --epic APP-10 --profile example`
- List active or future sprints: `jira sprints --board-id 42 --state active --profile example`
- List the board backlog: `jira backlog --board-id 42 --profile example`
- List issues in a sprint: `jira sprint --sprint-id 7 --profile example`
- List configured-project issues: `jira list --profile example --limit 10`
- List one project: `jira list --profile example --project APP --limit 10`
- Run explicit JQL: `jira search --profile example --jql 'project = APP ORDER BY updated DESC' --limit 10`
- Fetch issue detail: `jira detail APP-252 --profile example --full`
- Fetch issue JSON with normalized fields: `jira detail APP-252 --profile example --full --json`
- Fetch comments: `jira comments APP-252 --profile example`
- Fetch attachment metadata: `jira attachments APP-252 --profile example`
- Dry-run one attachment download: `jira attachment --profile example --id EXAMPLE_ATTACHMENT_ID --output /tmp/example-attachment.png`
- Confirm one attachment download: `jira attachment --profile example --id EXAMPLE_ATTACHMENT_ID --output /tmp/example-attachment.png --confirm`
- Dry-run issue creation: `jira create --profile example --project APP --issue-type Task --summary "Example task" --description-file /tmp/example-task.md`
- Confirm issue creation: `jira create --profile example --project APP --issue-type Task --summary "Example task" --description-file /tmp/example-task.md --confirm`
- Read the description from standard input: `jira create --profile example --project APP --issue-type Task --summary "Example task" --description -`
- Create without the structure check: `jira create --profile example --project APP --issue-type Task --summary "Example task" --freeform`
- Dry-run issue creation with an epic: `jira create --profile example --project APP --issue-type Task --summary "Example task" --description-file /tmp/example-task.md --epic APP-10`
- Confirm issue creation with an epic: `jira create --profile example --project APP --issue-type Task --summary "Example task" --description-file /tmp/example-task.md --epic APP-10 --confirm`
- Dry-run issue edit: `jira edit APP-252 --profile example --summary "Updated title"`
- Confirm issue edit: `jira edit APP-252 --profile example --summary "Updated title" --confirm`
- Replace a description from a file: `jira edit APP-252 --profile example --description-file /tmp/example-task.md --confirm`
- Dry-run epic assignment: `jira assign-epic APP-252 --profile example --epic APP-10`
- Confirm epic assignment: `jira assign-epic APP-252 --profile example --epic APP-10 --confirm`
- Dry-run sprint assignment: `jira assign-sprint APP-252 --profile example --sprint-id 7`
- Confirm sprint assignment: `jira assign-sprint APP-252 --profile example --sprint-id 7 --confirm`
- View issue comments: `jira comments APP-252 --profile example`
- View issue comments in detail: `jira detail APP-252 --profile example`
- Dry-run one-file upload: `jira upload APP-252 --profile example --file /tmp/example.txt`
- Confirm one-file upload: `jira upload APP-252 --profile example --file /tmp/example.txt --confirm`
- Dry-run comment: `jira comment APP-252 --profile example --body "Progress update"`
- Confirm comment: `jira comment APP-252 --profile example --body "Progress update" --confirm`
- Comment from a file: `jira comment APP-252 --profile example --body-file /tmp/example-comment.md --confirm`
- Dry-run a comment with one native mention: `jira comment APP-252 --profile example --body "@[Alex Example] please review."`
- Confirm a comment with one native mention: `jira comment APP-252 --profile example --body "@[Alex Example] please review." --confirm`
- Dry-run issue deletion: `jira delete APP-252 --profile example`
- Confirm issue deletion: `jira delete APP-252 --profile example --confirm`
- Resolve issue keys from text: `jira identify 'Review APP-252' --all-profiles`

Default output is compact text for agent context. `list`, `search`, `backlog`, `sprint`, and `epic` print CSV-style rows. Use `--json` only for debugging, exports, or consumers that need raw plus normalized Jira fields.

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

Do not run `create --confirm`, `edit --confirm`, `upload --confirm`, `comment --confirm`, `delete --confirm`, or `attachment --output --confirm` as a smoke test
unless the owner confirms the exact profile and target/effect. Offline tests cover the write request
method, markdown-to-ADF rendering, ADF-to-markdown reading, ticket structure validation, description
and comment input paths, native mention resolution, refusal, and account-id redaction, project
allowlisting, multipart file upload, dry-runs, and confirmation paths.

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
`PUT /rest/api/3/issue/{issueIdOrKey}` for edits; descriptions are rendered from markdown into
Atlassian Document Format before they are sent.
The account's existing API token remains the credential. OAuth apps additionally need the Jira write
scope documented by Atlassian.

For uploads, the account also needs Browse Projects and Create attachments for the issue's project.
The command sends one explicit local file as multipart form data and uses Jira's required
`X-Atlassian-Token: no-check` header. It never uploads a directory or recursively discovers files.

For a native mention, the account also needs Jira's Browse users and groups permission, because the
command resolves the display name through `GET /rest/api/3/user/search` before it writes. That
bounded search is the only extra read a mention makes.

For comments, the account needs Browse projects and Add comments for the issue's project. For issue
deletion, it needs Browse projects and Delete issues. Jira refuses deletion when the issue has
subtasks unless a separate delete-subtasks option is supplied; this integration never supplies that
option.

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

`list`, `search`, `backlog`, `sprint`, and `epic` print one CSV row per issue:

```text
key,title,type,status,priority,assignee,updated,project,epic,sprint,profile
APP-252,Example ticket title,Story,To Do,Medium,Alex Example,2026-06-23 12:34,APP,APP-10 (Roadmap),7 (Sprint 7) [active],example
```

`detail --json` includes raw Jira issue data, paginated raw comments, and a `normalized` object with:

```text
key, id, profile, site, url, title, status, description, assignee, reporter,
creator, project, type, priority, updated, labels, components, fixVersions, epic, sprints,
attachments, comments
```

Attachment bytes are never downloaded by `detail` or `attachments`; those commands list metadata only. `attachment --id ID --output PATH` is a dry-run by default. Add `--confirm` to download one attachment to an explicit local path. Existing paths and symlinks are rejected, and a confirmed download is published atomically without exposing a partial file or overwriting a racing target.

### Description and Comment Input

`create --description`, `edit --description`, and `comment --body` accept the text three ways, and
exactly one of them per invocation:

```text
--description "<text>"        the literal text
--description-file <path>     read it from a regular file (a symlink is refused)
--description -               read it from standard input
```

Passing both the inline flag and its `-file` form is refused rather than resolved by precedence.
`--description -` refuses when standard input is a terminal, so an interactive call cannot hang.
A literal `-` cannot be sent as a description; use `--description-file` for that.
The file and standard-input forms read at most 262144 characters and refuse a longer body, or
one that is not UTF-8 text, naming the flag and the maximum rather than loading the file.

The text is markdown and is rendered into Atlassian Document Format: headings, bullet and numbered
lists with nesting, fenced code blocks with a language, block quotes, horizontal rules, and inline
strong, emphasis, code, strikethrough, and links. Anything else is left as literal text, never
dropped. Lists nest eight levels deep; a deeper item is kept as an item of the eighth rather than
nesting further. Inline code inside strong, emphasis, or strikethrough is sent as code alone,
because ADF combines the code mark with a link only. Three departures from ordinary markdown: a
single newline becomes a line break, `_` does not emphasize inside a word so identifiers survive,
and only `http`, `https`, and `mailto` link targets are created.

`detail` and `comments` render the stored document back to markdown, so structure written through
this command reads back as it was written. Text that would otherwise read as markup on a second
render — a paragraph beginning with `#` or `-`, literal `*` or backticks in prose, whitespace or
brackets inside inline code, a `]` in a link label, a fence inside a code block — comes back
escaped, padded, or fenced wider than the run it holds, so re-rendering it produces the same
document. A construct this catalog does not render — a table,
a panel, an expand — reads back as its text without that structure, and a mention or emoji reads
back as its display text.

### Native Mentions

`@[Display Name]` in a description or comment body creates one native Jira mention. A plain
`@Alex Example` stays the text it has always been, so nothing already written changes meaning.

```text
--body "@[Alex Example] please review this issue."
```

The command resolves the name through Jira's own user search and sends an Atlassian Document Format
`mention` node carrying the resolved account id and Jira's spelling of the display name. Jira
renders that node as a real mention and applies its normal mention notification behavior for the
account named; whether a given person is notified also depends on their own Jira notification
settings and their permission to see the issue.

Resolution is exact, and refuses rather than guesses:

| What Jira's search returns for the name | What the command does |
|---|---|
| one account whose display name matches exactly | sends the mention |
| no exact match | refuses, and reports how many nearby names the search returned |
| more than one exact match | refuses, and reports how many share that display name |
| a full 50-result page | refuses, because no match inside it can be proved unique |

Jira's search matches a *prefix* of a display name or email address, so it answers with neighbours
as well as the person named; only an exact display name selects an account. Matching ignores
capitalization and repeated spaces, so `@[alex example]` selects `Alex Example`.

One description or comment carries one mention; a second `@[...]` is refused. Resolution runs
before the dry-run prints, so a name Jira cannot resolve refuses at preview rather than after
`--confirm`, and it runs after the project allowlist check, so a project this profile may not write
to never triggers a user search. The preview names the profile, project, issue, and resolved display
name, and never prints the account id: `create` and `edit` replace it with `(redacted)` in their
`fields=` output. A confirmed `create`, `edit`, or `comment` reports the mention it sent as
`mention=@Display Name`.

Write `@\[Display Name]`, or wrap it in backticks, to keep those characters literal. A complete
link or image form keeps it literal too, and costs no user search:
`@[Alex Example](https://example.test/a)` stays the link it was before mentions existed,
`[@[Alex Example]](https://example.test/a)` keeps both its label text and its href, and
`![@[Alex Example]](https://example.test/i.png)` stays the text an image already is. ADF carries a
link on a text node's mark and a `mention` node holds no marks, so a mention is never created where
it would drop a link.

Completeness is what decides. A bracket group with no `(target)` after it is neither a link nor an
image, so `![@[Alex Example]]` still requests a mention. Nothing else about a link or an image
changes: a label's emphasis, code, escapes, and nested links render exactly as they did before.

A mention has to sit in a line of at most 20000 characters, the limit that keeps the markdown
writer's own scanning bounded. A longer line is still accepted as text, but `@[Display Name]` inside
one is refused rather than sent: written as text it names somebody Jira never notifies. Split the
paragraph so the mention sits in a shorter line, or escape it as `@\[Name]` to keep it literal. The
refusal names the syntax it found and nothing else from the line, and it happens before the user
search, so no oversized body reaches Jira.

`detail` and `comments` render a stored mention as its display text (`@Alex Example`), not as the
`@[...]` syntax, so a read-back that is re-sent sends plain text rather than mentioning anyone
again.

### Ticket Structure

`create` refuses a description that has no headings, or that is missing the sections its issue type
calls for, and the error names exactly which are absent. The check runs before the dry-run prints,
so an unstructured ticket never previews cleanly and then fails on `--confirm`.

```text
Story, Task   Objective, Background, Business value, Requirements, Acceptance criteria, Open questions
Bug           Problem, Steps to reproduce, Expected, Actual, Impact, Acceptance criteria
Spike         Question, Context, Timebox, Deliverable
```

Issue types are matched by a known word, so `Bug - Production` and `User Story` both resolve. A
type the catalog does not recognize must still carry at least one heading, but its sections are not
checked, and the fallback is reported on stderr. `--freeform` skips the section check and still
renders markdown. `edit` renders markdown but is not structure-checked, because an edit is often a
targeted correction. See [Writing a Jira ticket](ticket-format.md) for the templates, the accepted
section names, and the writing rules.

### Project Mapping Rules

- The account's project list (`JIRA_PROJECTS__<ACCOUNT>`, or `JIRA_<PROFILE>_PROJECTS`, or the plain `JIRA_PROJECTS` for the default account) is the source of truth for default issue searches.
- `list` is bounded to configured projects unless `--project` is provided.
- `search --jql` runs explicit JQL and should stay bounded by project in normal agent use.
- `identify --all-profiles` uses issue-key prefixes to try matching profiles first, then falls back to other configured profiles.
- Jira issue keys are not globally unique across sites, so output includes the profile.
- `boards --project APP` discovers a board id; use that id with `epics`, `sprints`, and `backlog`.
- `sprints --state active` and `--state future` filter sprint discovery by Jira's sprint state.
- `backlog` returns issues that Jira places in the selected board backlog; `sprint` returns issues
  assigned to one sprint. Use `list` or `search` when you need a project-wide view instead.
- `epics` lists board epics; `epic --epic APP-10` returns the issues Jira assigns to that epic.
- `create --epic` and `edit --epic` resolve the site's Epic Link or Parent field. Pass
  `--epic-field parent` or `--epic-field customfield_12345` when field discovery is ambiguous.

### Safety Notes

- Tokens belong in local `.env`, never committed docs, examples, or chat logs.
- Provider base URLs must be HTTPS origins. Authorization is retained only for same-origin redirects and is stripped before a cross-origin redirect.
- Prefer service accounts for organization automation when available.
- Rotate any token that was pasted into chat or logs.
- Keep live checks small and bounded.
- Create and edit are guarded mutations: they print a dry-run and require `--confirm`.
- Create is bounded to the configured project allowlist and does not infer an issue type.
- Create refuses an unstructured or missing description; `--freeform` sends it as written.
- Description and comment bodies read from a file or standard input are bounded to 262144
  characters; a longer or non-UTF-8 body is refused, never truncated or partly sent.
- Markdown link targets are limited to `http`, `https`, and `mailto`; any other target stays
  literal text, because a ticket is shared and clickable.
- Upload is bounded to one explicit regular file and the configured project allowlist.
- Comment creation and issue deletion are guarded mutations, each requiring `--confirm`.
- Epic and sprint assignment are guarded one-issue mutations, each requiring `--confirm`; they do
  not start, close, or otherwise manage sprint lifecycle.
- A native mention is created only by the explicit `@[Display Name]` syntax; an `@name` written in
  prose stays plain text and notifies nobody.
- A mention resolves to exactly one Jira account by exact display name. No match, several matches,
  or a search page too full to prove uniqueness each refuse before anything is written.
- A preview never prints a Jira account id; `create` and `edit` replace it with `(redacted)`.
- Comments remain viewable through `comments` and `detail`.
- Delete targets one issue and never requests deletion of subtasks.
- The integration does not transition issues, perform bulk operations, or administer projects/sites.

For board discovery, Agile endpoint behavior, pagination, field compatibility, and permission
troubleshooting, see [Agile workflows](agile.md). For issue templates, section names, and the
supported markdown, see [Writing a Jira ticket](ticket-format.md).

### Official References

- [Jira issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
- [Jira issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
- [Atlassian Document Format](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/)
- [ADF mention node](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/mention/)
- [Jira user search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/)
- [Jira comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/)
- [Jira attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/)
- [Jira Software boards](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/)
- [Jira Software epics](https://developer.atlassian.com/cloud/jira/software/rest/api-group-epic/)
- [Jira Software sprints](https://developer.atlassian.com/cloud/jira/software/rest/api-group-sprint/)
