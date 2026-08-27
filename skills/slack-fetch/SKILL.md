---
name: slack-fetch
description: Use when the user needs to list accessible Slack channels or DMs, fetch message history with timestamps, search messages, read a complete thread, or retrieve one Slack-hosted attachment by its exact file ID, without changing Slack state. It supplies profile-scoped, bounded reads with compact text or full JSON through Slack's read-only API methods, plus one guarded local file write that previews before it writes. Do not use for broader Slack operations or to send, react, edit, delete, mark read, save, pin, join, upload, share a file, make a file public, or otherwise mutate Slack.
---

# Slack fetch

Run `$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch`; it resolves its configured user token without printing
or inspecting the credential source. Read `references/cli.md` for owner setup, scopes, output,
rate limits, local-session findings, or validation, and `references/sources.md` before changing or
challenging a claim about Slack's own methods, scopes, pagination, or limits.

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

## Attachments

Read a file's bytes only when the request needs them and the message already names one file. Take
the exact `id` from that file object in `--json` output, and name the destination path yourself:

```sh
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" attachment --profile <profile> --id <file-id> --output <path>
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" attachment --profile <profile> --id <file-id> --output <path> --confirm
```

Name the profile every time: this is the one command that will not infer the account. The first
form previews the file Slack would send and writes nothing. Report that preview and take
the owner's decision before adding `--confirm`, which is the only form that writes. Retrieval needs
the optional `files:read` scope; a profile without it refuses this command and keeps every message
command working. There is no file listing or search: retrieval takes one exact file ID.

The command refuses rather than guessing — an existing destination, a file stored outside Slack, a
file the profile cannot see, a size above the bound, or a transfer that did not arrive whole. Report
the refusal as it stands; do not retry it against a different path to make it succeed. Treat a
downloaded file as the private material it was in Slack, and report the completion record's byte
count and SHA-256 rather than the file's contents when the request only asks whether it arrived.

This integration has no mutation verbs and calls only `auth.test`, `conversations.list`,
`conversations.history`, `search.messages`, `conversations.replies`, and `files.info`. Never inspect Slack desktop caches, browser cookies, local storage,
passwords, session stores, or tokens to configure it. Never substitute browser or desktop UI
automation: viewing a conversation can change read state.
