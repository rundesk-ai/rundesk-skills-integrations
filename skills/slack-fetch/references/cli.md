# Slack fetch CLI

## Owner setup

This command runs locally but does not reuse a Slack desktop or browser session. Slack documents
its desktop cache as resettable app data, not as an integration API. Extracting a token, cookie,
IndexedDB record, or other session material would depend on private implementation details, bypass
the owner's app authorization choices, and risk exposing credentials. Browser UI automation is
also outside this read-only contract because opening a conversation can advance Slack read state.

Have a Slack workspace owner approve or create an app for this read-only use, install it to the
workspace, and issue a **user OAuth token** with only the scopes needed for the conversations Cole
may read:

```text
search:read
channels:read
groups:read
im:read
mpim:read
channels:history
groups:history
im:history
mpim:history
```

`search.messages` requires a user token. `conversations.list` uses the matching `:read` scopes;
`conversations.history` and `conversations.replies` use the matching history scope for each
conversation type. A token can read only content the Slack user and workspace policy allow. Omit
read and history scopes for conversation types that must remain inaccessible. Do not add
`chat:write`, `reactions:write`, `channels:manage`, or another mutation scope.

The command also accepts the established local profile shape
`SLACK_<PROFILE>_TOKEN`, `SLACK_<PROFILE>_LABEL`, `SLACK_<PROFILE>_TYPES`, and
`SLACK_<PROFILE>_CHANNELS`. `TYPES` is a comma-separated subset of
`public_channel,private_channel,mpim,im`; blank means all four. `CHANNELS` is an optional
comma-separated allowlist of channel IDs; blank means every conversation the token can
read. Rundesk-managed configuration uses the canonical `SLACK_FETCH_*` names instead.

Configure the token at the owner's terminal, never in chat or a repository:

```sh
rundesk skills configure rundesk-skills-integrations/slack-fetch
rundesk skills configure rundesk-skills-integrations/slack-fetch --profile <name>
```

New values reach the next agent turn. A named profile is a complete separate Slack identity and
never falls back to the default token.

## Commands

```sh
slack-fetch profiles
slack-fetch status --profile <profile>
slack-fetch channels --profile <profile> --limit 50
slack-fetch messages --profile <profile> --channel C00000000 --limit 20
slack-fetch messages --profile <profile> --channel D00000000 --oldest 1700000000.000000 --limit 20
slack-fetch search --profile <profile> --query 'release in:#example after:2026-01-01' --limit 10
slack-fetch thread --profile <profile> --permalink 'https://example.slack.com/archives/C00000000/p1700000000000000'
slack-fetch thread --profile <profile> --channel C00000000 --ts 1700000000.000000
```

`channels` returns at most `--limit` accessible public channels, private channels, group DMs, and
DMs, narrowed by the profile's optional `TYPES` and `CHANNELS` settings. `messages` returns bounded
recent history for one channel ID, with optional Slack-format `--oldest` and `--latest` timestamps.

`search` returns at most `--limit` matches (default 10, maximum 100). Each result includes the
channel, message timestamp, parent `thread_ts` when applicable, permalink, author identifier, and
bounded text. Use the parent timestamp for `thread` when the match is a reply.

`thread` follows Slack cursors until the API says there are no more replies. It prints
`complete=yes` only then. `--max-messages` defaults to 1,000 and may be raised to 5,000; reaching
the cap exits non-zero and labels the output incomplete. Slack may apply a low rate limit and a
15-message page size to some app installations. The command does not wait or retry silently: a
rate-limited page exits non-zero with the provider's non-secret retry interval, so the result must
not be called complete.

Default text output is compact for agent context and includes each message timestamp, author ID,
and bounded text. `--json` preserves the complete channel or message objects returned by Slack and
is only for an explicitly requested structured consumer. Never redirect live output to routine
logs or use it as a test fixture.

## Boundaries and limitations

- There are no send, reply, react, edit, delete, save, pin, mark-read, membership, or administration
  commands. The HTTP allowlist contains only `auth.test`, `conversations.list`,
  `conversations.history`, `search.messages`, and `conversations.replies`.
- Slack retention, Enterprise policies, search exclusions, token scopes, and the user's existing
  access determine what can be found. The command does not bypass them.
- Search relevance is Slack's result ordering. Narrow with quoted phrases and `in:`, `from:`,
  `after:`, `before:`, `on:`, or `is:thread` modifiers before increasing `--limit`.
- `search.messages` is a supported legacy Web API method; Slack recommends its newer real-time
  search surface for apps that qualify for that product, but the legacy method remains the
  documented user-token method with the narrow `search:read` scope.
- A long thread can be interrupted by Slack's rate limit. Partial output is explicitly incomplete
  and must not be summarized as a complete thread.
- The command resolves no display names and downloads no files. IDs and message text are returned
  exactly as the read endpoints provide them, with text bounded in compact output.
- The local Slack app and an authenticated Chrome session are not prerequisites and are never
  inspected. macOS Full Disk Access, Keychain access, Accessibility, and Screen Recording are not
  requested or bypassed.

## Validation

```sh
python3 "$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch.d/test-slack-fetch.py" -q
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" --help
"$RUNDESK_SKILLS/slack-fetch/scripts/slack-fetch" profiles
```

The offline suite uses synthetic API responses and patches every HTTP boundary. It checks the local
profile shape, channel and DM listing, bounded timestamped message history, full JSON, searches,
reply-to-root selection, complete pagination, cap refusal, rate-limit errors, URL encoding,
credential redaction, and the endpoint allowlist without real workspaces or messages.

Optional live checks should remain metadata-only until the owner approves content access:

```sh
slack-fetch status --profile <profile>
```

## Sources

- [Slack search help](https://slack.com/help/articles/202528808-How-to-search-in-Slack) documents
  search modifiers and the browser/desktop search surface.
- [Slack `search.messages`](https://api.slack.com/methods/search.messages) documents the
  credentialed message-search method.
- [Slack `conversations.list`](https://api.slack.com/methods/conversations.list) documents
  conversation types, read scopes, and cursor pagination.
- [Slack `conversations.history`](https://api.slack.com/methods/conversations.history) documents
  bounded channel and DM history with Slack timestamps.
- [Slack `conversations.replies`](https://api.slack.com/methods/conversations.replies) documents
  cursor pagination, history scopes by conversation type, and current rate-limit constraints.
- [Slack deep linking](https://docs.slack.dev/interactivity/deep-linking/) documents supported
  desktop URI targets; it does not define a local message-history or desktop-cache API.
- [Slack system requirements](https://slack.com/help/articles/115002037526-System-requirements-for-using-Slack.)
  documents supported macOS and browser clients.
