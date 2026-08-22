---
name: confluence
description: Use when the user needs to read Confluence Cloud spaces, pages, page trees, comments, or attachments, or edit an existing page. It provides account-scoped reads and guarded page edits with explicit version confirmation. Do not use to create, delete, move, comment on, or attach files to Confluence content, or to read Jira issues.
---

# Confluence

Run `$RUNDESK_SKILLS/confluence/scripts/confluence`; it resolves credentials itself, so never
inspect or print their source. Read `references/cli.md` only for setup, environment keys, output
fields, API behavior, or validation.

Start with `"$RUNDESK_SKILLS/confluence/scripts/confluence" profiles`. Use each profile's
configured space allowlist; never infer an account or space from an organization name.

Use compact and bounded commands:

```sh
"$RUNDESK_SKILLS/confluence/scripts/confluence" spaces --profile <profile> --limit 10
"$RUNDESK_SKILLS/confluence/scripts/confluence" list --profile <profile> --space <space> --limit 10
"$RUNDESK_SKILLS/confluence/scripts/confluence" tree --profile <profile> --space <space> --depth 3 --max-pages 50
"$RUNDESK_SKILLS/confluence/scripts/confluence" search --profile <profile> --space <space> --query '<words>' --limit 10
"$RUNDESK_SKILLS/confluence/scripts/confluence" page <page-id> --profile <profile> --full
"$RUNDESK_SKILLS/confluence/scripts/confluence" edit <page-id> --profile <profile> --title "Updated title"
"$RUNDESK_SKILLS/confluence/scripts/confluence" edit <page-id> --profile <profile> --body-file page.xhtml --confirm --expected-version <version>
```

Keep searches within configured spaces unless the user deliberately requests a broader
scan. Search or list first when the page is unknown; use `page --full` after identifying it or when
the user explicitly needs its body. Use `--json` only when raw structured data is required. Page
edits preview the target, space, version, title, and body hash first. Execute only after reviewing
that preview with `--confirm --expected-version <version>`.

The integration can edit one existing page at a time. It preserves the current body or title when
the corresponding replacement is omitted, accepts Confluence storage XHTML from `--body` or one
explicit local `--body-file`, restricts edits to configured spaces, and refuses stale version
confirmations. It does not create, delete, move, comment on, or attach files to content.
