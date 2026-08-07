---
name: sentry
description: Use when the user needs to triage a production error, exception, stack trace, event, error ID, or unresolved issue in their Sentry organization. It supplies issue and event evidence plus guarded resolution previews. Do not use for general application debugging, observability design, Sentry SDK configuration, or bulk issue changes.
---

# Sentry

Run `$RUNDESK_SKILLS/sentry/scripts/sentry`; it resolves credentials itself, so never inspect or
print their source. Read `references/cli.md` only for setup, environment keys, output fields, API
behavior, or validation.

Start with `"$RUNDESK_SKILLS/sentry/scripts/sentry" profiles`, then choose the profile
whose configured projects match the task. Never guess a profile when more than one is
configured.

Keep searches bounded:

```sh
"$RUNDESK_SKILLS/sentry/scripts/sentry" projects --profile <profile> --limit 10
"$RUNDESK_SKILLS/sentry/scripts/sentry" list --profile <profile> --days 7 --limit 10
"$RUNDESK_SKILLS/sentry/scripts/sentry" search --profile <profile> --project <slug> --query 'is:unresolved environment:production lastSeen:-1d' --limit 20
"$RUNDESK_SKILLS/sentry/scripts/sentry" detail <issue> --profile <profile>
"$RUNDESK_SKILLS/sentry/scripts/sentry" inspect <issue> --profile <profile> --event-limit 1
```

Use the 24-hour production query for recent production-error requests; keep the seven-day
`list` default for general triage. Sentry issue search uses implicit AND and does not
support an `OR` operator; use `key:[value-one,value-two]` or run separate queries.

Prefer canonical short IDs such as `EXAMPLE-1` in findings and follow-up commands. Report
title, short ID, status, first and last seen, event count, and affected users; add
environment and release from `inspect` when relevant. State explicitly when no issues
match.

Prefer `inspect` for compact event evidence. Its text output redacts email and IP values
and summarizes stack locations instead of printing raw stack traces. Raw JSON may contain
request bodies, cookies, user data, and full stack traces; use it only when explicitly
needed and never paste sensitive values into chat or committed files.

`resolve` is a dry-run without `--confirm`. Never confirm resolution unless the owner
approves that exact profile and issue.
