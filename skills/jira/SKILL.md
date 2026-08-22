---
name: jira
description: Use when the user needs facts from an existing Jira Cloud issue, project, backlog, comment, or attachment, or asks to create, edit, or attach a file to a Jira issue. It supplies account-scoped searches, issue detail, and guarded issue writes. Do not use to plan work generally or to transition, comment on, or delete Jira issues.
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
```

Keep JQL bounded to one project. Use `--json` only when raw structured data is required.
Create and edit are dry-runs by default. Review the exact project, issue type, issue key, and
fields, then pass `--confirm` to perform the live write. Create refuses projects outside the
profile's configured `JIRA_PROJECTS` allowlist. The write surface is limited to issue creation,
summary/description replacement, and one-file attachment upload; it does not transition, comment
on, or delete issues.
Comments remain viewable through `comments` and `detail`. Attachment metadata remains viewable
through `attachments`, and one file can be uploaded with `upload`. Uploads and attachment downloads
write only after approval of the exact issue/file or attachment/path and `--confirm`.
