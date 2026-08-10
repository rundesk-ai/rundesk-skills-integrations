---
name: slack-fetch
description: Use when the user needs to list accessible Slack channels or DMs, fetch message history with timestamps, search messages, or read a complete thread without changing Slack state. It supplies profile-scoped, bounded reads with compact text or full JSON through Slack's read-only API methods. Do not use for broader Slack operations or to send, react, edit, delete, mark read, save, pin, join, or otherwise mutate Slack.
---

# Slack fetch

Run `$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch`; it resolves its configured user token without printing
or inspecting the credential source. Read `references/cli.md` for owner setup, scopes, output,
rate limits, local-session findings, or validation.

Start with `"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" profiles`. Select one profile explicitly when
more than one is configured. List conversations before fetching history when the channel ID is unknown:

```sh
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" channels --profile <profile> --limit 50
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" messages --profile <profile> --channel <channel-id> --limit 20
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" search --profile <profile> --query '<words>' --limit 10
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" thread --profile <profile> --permalink '<message-url>'
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" thread --profile <profile> --channel <channel-id> --ts <thread-ts>
```

Text output is compact and always includes message timestamps. Add `--json` for the full Slack
objects, including blocks, attachments, and other fields the API returns. Use Slack's search
modifiers such as `in:`, `from:`, `after:`, `before:`, and `is:thread` when the
request supplies those bounds. Search results name both `ts` and `thread_ts`; retrieve
`thread_ts` when present so a reply result opens its complete parent thread.

Treat a thread as complete only when the command reports `complete=yes`. If Slack rate-limits a
page or the safety cap is reached, report the thread as incomplete; do not present the partial
output as the whole discussion. Use `--json` only when structured output is required, and never
copy message bodies into logs, fixtures, public issues, or unrelated systems.

This integration has no mutation verbs and calls only `auth.test`, `conversations.list`,
`conversations.history`, `search.messages`, and `conversations.replies`. Never inspect Slack desktop caches, browser cookies, local storage,
passwords, session stores, or tokens to configure it. Never substitute browser or desktop UI
automation: viewing a conversation can change read state.
