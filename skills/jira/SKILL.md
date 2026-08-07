---
name: jira
description: Use when the user needs facts from an existing Jira Cloud issue, project, backlog, comment, or attachment, including when they provide only an issue or project key. It supplies account-scoped searches and issue detail. Do not use to plan work generally or to create, edit, transition, or comment on Jira issues.
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
```

Keep JQL bounded to one project. Use `--json` only when raw structured data is required.
Jira is read-only. An attachment download writes only to an explicit new local path and
requires the owner's approval for that attachment and path before `--confirm`.
