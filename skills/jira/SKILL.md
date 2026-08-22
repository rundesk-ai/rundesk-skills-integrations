---
name: jira
description: Use when the user asks to inspect Jira Cloud projects, issues, comments, or attachments, or to create, edit, comment on, attach a file to, or delete an issue. It supplies account-scoped Jira reads and guarded issue mutations with explicit confirmation. Do not use it for Jira transitions, bulk operations, or project and site administration.
---

# Jira

Run `$RUNDESK_SKILLS/jira/scripts/jira`; it resolves credentials itself, so never inspect or print
their source. Read `references/cli.md` only for setup, environment keys, output fields, API
behavior, or validation.

Start with `"$RUNDESK_SKILLS/jira/scripts/jira" profiles`. Use each profile's configured
project allowlist. When an issue prefix is unclear, run:

```sh
"$RUNDESK_SKILLS/jira/scripts/jira" identify '<text containing KEY-123>' --all-profiles
```

Use compact output by default:

```sh
"$RUNDESK_SKILLS/jira/scripts/jira" list --profile <profile> --project <key> --limit 10
"$RUNDESK_SKILLS/jira/scripts/jira" search --profile <profile> --jql '<bounded JQL>' --limit 10
"$RUNDESK_SKILLS/jira/scripts/jira" detail <KEY-123> --profile <profile> --full
"$RUNDESK_SKILLS/jira/scripts/jira" comments <KEY-123> --profile <profile>
"$RUNDESK_SKILLS/jira/scripts/jira" attachments <KEY-123> --profile <profile>
"$RUNDESK_SKILLS/jira/scripts/jira" create --profile <profile> --project <key> --issue-type <name> --summary '<text>'
"$RUNDESK_SKILLS/jira/scripts/jira" edit <KEY-123> --profile <profile> --summary '<text>'
"$RUNDESK_SKILLS/jira/scripts/jira" upload <KEY-123> --profile <profile> --file /path/to/file
"$RUNDESK_SKILLS/jira/scripts/jira" comment <KEY-123> --profile <profile> --body '<text>'
"$RUNDESK_SKILLS/jira/scripts/jira" delete <KEY-123> --profile <profile>
```

Keep JQL bounded to one project. Use `--json` only when raw structured data is required.
All mutations are dry-runs by default. Review the exact project, issue key, fields, comment body,
file, or delete target, then pass `--confirm` to perform the live operation. Create, edit, comment,
upload, and delete require the issue's project key to be in the profile's configured `JIRA_PROJECTS`
allowlist. Delete targets one issue and does not request deletion of subtasks. Comments remain
viewable through `comments` and `detail`; attachment metadata remains viewable through `attachments`.
The integration does not transition issues, perform bulk operations, or administer projects or sites.
