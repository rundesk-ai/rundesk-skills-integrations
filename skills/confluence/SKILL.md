---
name: confluence
description: Read Confluence Cloud spaces, page trees, search results, and bodies with the bundled CLI. Use for Confluence, Atlassian knowledge, internal docs, spaces, pages, runbooks, or specifications.
---

# Confluence

Run the bundled CLI at `$RUNDESK_SKILLS/confluence/scripts/confluence`. It loads
credentials itself; never inspect or print its credential file. Read `references/cli.md`
only for setup, output, API, or validation details.

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
scan. Use `--json` only when raw structured data is required. The integration is
read-only.
