# Jira Software Agile workflows

Use this guide for board, epic, sprint, and backlog work. The CLI keeps these reads bounded and
does not change sprint lifecycle. Every command accepts `--limit`; use `--json` when a downstream
agent needs ids or the raw Jira response.

## Discover the working context

Start with a project key and discover the Jira Software board that owns its backlog and sprints:

```sh
jira boards --profile example --project APP --limit 10
jira epics --profile example --board-id 42 --limit 25
jira sprints --profile example --board-id 42 --state active --limit 10
jira sprints --profile example --board-id 42 --state future --limit 25
```

Use the returned board id for later reads. A board is the context for backlog and sprint views;
the project allowlist still controls issue mutations. If several boards are returned, select the
one whose name and type match the user's intended board and ask before making a write when the
choice is ambiguous.

## Fetch issues by epic, backlog, or sprint

```sh
# All issues Jira assigns to an epic.
jira epic --profile example --epic APP-10 --limit 50

# Issues currently in the board backlog.
jira backlog --profile example --board-id 42 --limit 50

# Issues assigned to one sprint, including active, future, or closed sprints.
jira sprint --profile example --sprint-id 7 --limit 50
```

Use `sprints --state active` for the current sprint and `sprints --state future` for planned
sprints. Backlog is a board view, not a sprint named `backlog`; do not infer backlog membership
from an empty or missing `sprint` field in a project-wide issue search. When a combined view is
needed, fetch the backlog and the selected sprint separately and preserve the source label.

Issue rows include `epic` and `sprint` columns. Sprint labels include the Jira sprint id/name and,
when Jira returns it, the sprint state. `detail --json --full` also exposes normalized `epic` and
`sprints` references while preserving the raw issue fields. Jira may expose epic membership as the
legacy Epic Link custom field or as the newer Parent field; the CLI normalizes both for reads.

## Create or edit under an epic

Preview the exact issue fields first:

```sh
jira create --profile example --project APP --issue-type Task \
  --summary "Example task" --epic APP-10
jira edit APP-252 --profile example --epic APP-10
```

`--epic` detects the site's Epic Link or Parent field from Jira field metadata. If the site has a
custom configuration that cannot be detected, provide the exact field id:

```sh
jira create --profile example --project APP --issue-type Task \
  --summary "Example task" --epic APP-10 --epic-field customfield_10014
jira edit APP-252 --profile example --epic APP-10 --epic-field parent
```

Review the dry-run, then repeat the same command with `--confirm`. The issue project must be in
the selected profile's `JIRA_PROJECTS` allowlist. The CLI never guesses an epic key and does not
silently move an issue between projects.

## Assign an existing issue

```sh
jira assign-epic APP-252 --profile example --epic APP-10
jira assign-sprint APP-252 --profile example --sprint-id 7
```

Both commands are one-issue dry-runs. Add `--confirm` only after checking the issue, epic, sprint,
and profile. Sprint assignment adds the issue to the selected sprint; it does not start, close,
rename, rank, or otherwise manage the sprint. Epic assignment uses Jira Software's epic issue
association. These commands do not provide bulk assignment or an option to move an issue back to
the backlog; to remove sprint membership, use a separately approved Jira workflow outside this
skill rather than guessing at a lifecycle operation.

## Fetching and pagination

The CLI follows Jira's `startAt`, `maxResults`, `total`, and `isLast` response fields until the
requested limit is reached or Jira reports the collection is complete. It caps each request at a
provider-safe page size and never fetches an unbounded collection by default. For large boards or
epics, use a smaller limit for triage, then page through deliberate bounded calls or use `--json`
for a consumer that can retain ids and source context.

The useful identifiers are:

| Need | Command | Identifier |
| --- | --- | --- |
| Board context | `boards --project APP` | `board.id` |
| Epic catalog | `epics --board-id 42` | `epic.key` and `epic.id` |
| Epic issues | `epic --epic APP-10` | issue keys |
| Sprint catalog | `sprints --board-id 42` | `sprint.id` |
| Sprint issues | `sprint --sprint-id 7` | issue keys |
| Board backlog | `backlog --board-id 42` | issue keys |

Use `--json` when passing an id to a later command; do not parse human-readable labels when the
structured response is available.

## Permissions and troubleshooting

The account must be able to browse the project and see the selected board. Jira Software Agile
reads may additionally require the app's Jira Software read scopes, including board, epic, and
sprint read access. Epic and sprint assignment also requires the corresponding Jira Software write
scope and the project's issue-edit permission. A 403 can therefore mean either a missing OAuth
scope or a missing Jira project/board permission.

Common checks:

```sh
jira whoami --profile example
jira projects --profile example --limit 10
jira boards --profile example --project APP --limit 10
```

If `boards` returns no matching board, verify the project key, board visibility, and whether the
site uses a company-managed or team-managed project. If an epic is visible but `epic` returns no
issues, verify that the issue is assigned to that epic in Jira and that the selected profile can
browse those issues. If `--epic` fails during create/edit, inspect the field metadata error and
retry with the exact `--epic-field` id documented by the Jira project.

## Official API guides

- [Boards](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/)
- [Epics](https://developer.atlassian.com/cloud/jira/software/rest/api-group-epic/)
- [Sprints](https://developer.atlassian.com/cloud/jira/software/rest/api-group-sprint/)
- [Issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
- [Issue create and edit](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
