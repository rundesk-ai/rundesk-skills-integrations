---
name: confluence
description: Use when the user needs facts from an existing Confluence Cloud space, page, runbook, specification, or internal wiki, including when they name only the space or page. It supplies account-scoped search, page trees, and page bodies. Do not use to draft, edit, or publish documentation, or to read Jira issues.
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
```

Keep searches within configured spaces unless the user deliberately requests a broader
scan. Search or list first when the page is unknown; use `page --full` after identifying it or when
the user explicitly needs its body. Use `--json` only when raw structured data is required. The
integration is read-only.
