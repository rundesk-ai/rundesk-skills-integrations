---
name: jira
description: Read Jira Cloud projects, issues, comments, and attachment metadata through the local Jira CLI. Use when a task mentions Jira, a ticket or issue key, backlog or project work, comments, or Jira attachments.
---

# Jira

Run the bundled CLI at `$RUNDESK_SKILLS/jira/scripts/jira`. It loads credentials itself;
never inspect or print its credential file. Read `references/cli.md` only for setup,
output, API, or validation details.

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
```

Keep JQL bounded to one project. Use `--json` only when raw structured data is required.
Jira is read-only. An attachment download writes only to an explicit new local path and
requires the owner's approval for that attachment and path before `--confirm`.
