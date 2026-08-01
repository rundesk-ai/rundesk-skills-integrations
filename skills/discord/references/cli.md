---
name: discord
description: Discord servers, channels, threads, message history, and guarded send, reply, direct message, edit, delete, react, and thread creation.
category: providers
---

# discord

## Entry Point

Bundled command: `$RUNDESK_SKILLS/discord/scripts/discord`.

- `discord profiles`
- `discord status --profile example`
- `discord guilds --limit 25`
- `discord channels --guild GUILD_ID [--kind text|announcement|forum|public-thread|…]`
- `discord channel CHANNEL_ID`
- `discord threads --guild GUILD_ID`
- `discord history CHANNEL_ID --limit 25 [--before ID] [--after ID] [--full] [--newest-first]`
- `discord message CHANNEL_ID MESSAGE_ID [--full]`
- `discord user USER_ID`
- Dry-run post: `discord send CHANNEL_ID --text "..."`
- Confirm post: `discord send CHANNEL_ID --text "..." --confirm`
- `discord reply CHANNEL_ID MESSAGE_ID --text "..." [--confirm]`
- `discord dm USER_ID --text "..." [--confirm]`
- `discord edit CHANNEL_ID MESSAGE_ID --text "..." [--confirm]`
- `discord delete CHANNEL_ID MESSAGE_ID [--confirm]`
- `discord react CHANNEL_ID MESSAGE_ID --emoji "👀" [--confirm]`
- `discord thread CHANNEL_ID --name "..." [--message MESSAGE_ID] [--confirm]`

Reads never write to Discord. Every write is dry-run by default and requires `--confirm`
after owner approval for that exact message in that exact place.

## Output

Lists print CSV; single objects print `key<TAB>value`. `--json` returns the raw Discord
payload and is for structured work only — it is large.

`history` columns: `id, created_at, author, bot, reply_to, attachments, content`. Content
is clipped to 300 characters unless `--full`, and a message with no text but an embed shows
`[embed] <title>`. Rows are oldest first; `--newest-first` keeps Discord's own order.

Message text comes from `--text`, `--text-file PATH`, or `--text -` for stdin. Attach files
with repeated `--file PATH` (at most 5, 8 MB each without a boosted server); attachments go
on the first message when `--split` produces several.

## Validation

```sh
python3 "$RUNDESK_SKILLS/discord/scripts/discord.d/test-discord.py"
"$RUNDESK_SKILLS/discord/scripts/discord" profiles
"$RUNDESK_SKILLS/discord/scripts/discord" status --profile example
"$RUNDESK_SKILLS/discord/scripts/discord" guilds --limit 5
```

Never run a write `--confirm` as a smoke test; it posts where people are reading.

## Provider

Discord HTTP API v10 (`https://discord.com/api/v10/...`). Stdlib Python only — no gateway
connection, no websocket, no `discord.py`.

Authorization is `Bot <token>`, never `Bearer <token>`; Discord answers a Bearer-prefixed
bot token with 401 and no explanation. On HTTP 429 the command waits the `retry_after`
Discord returns, once, then reports the failure rather than looping.

### Setup

Create an application at <https://discord.com/developers/applications>, add a bot, copy its
token, and invite it to the server with the `bot` scope and the permissions the work needs:
View Channels, Read Message History, Send Messages, and — only if threads or reactions are
wanted — Create Public Threads and Add Reactions.

Enable **Message Content Intent** under Bot → Privileged Gateway Intents if the bot must read
message text it was not mentioned in.

```dotenv
DISCORD_PROFILES=example
DISCORD_DEFAULT_PROFILE=example
DISCORD_EXAMPLE_LABEL=Example bot
DISCORD_EXAMPLE_TOKEN=
DISCORD_EXAMPLE_ALLOW_GUILDS=
DISCORD_EXAMPLE_ALLOW_CHANNELS=
DISCORD_EXAMPLE_ALLOW_USERS=
```

A single bot needs no profile ceremony: `DISCORD_BOT_TOKEN` alone configures a profile
called `default`. `DISCORD_TOKEN` is accepted too, and is the same variable a Rundesk
Discord *channel* reads — an install that exports it hands this command the same bot
identity the agent already answers on.

Credential search: process env → `--env-file` → `DISCORD_ENV_FILE` →
`RUNDESK_INTEGRATIONS_ENV` → `~/.config/rundesk/integrations/discord/env` → legacy
`~/.config/discord/env`. Keep the file outside the catalog and mode `0600`.

### Write bounds

The three `ALLOW_` lists are comma-separated snowflakes and bound where writes may land.
Unprefixed `DISCORD_ALLOW_GUILDS`, `DISCORD_ALLOW_CHANNELS`, and `DISCORD_ALLOW_USERS`
apply to every profile that does not set its own.

| Configured | Effect |
|---|---|
| nothing | writes anywhere the bot has access |
| `ALLOW_CHANNELS` | writes only to those channel or thread ids |
| `ALLOW_GUILDS` | the channel is fetched and must belong to a listed server |
| `ALLOW_USERS` | `dm` only reaches those user ids |
| any bound, no `ALLOW_USERS` | `dm` is refused outright |

The check runs before the request, so a refusal costs nothing and posts nothing.

### Mutation boundary

| Command | Default | With `--confirm` |
|---|---|---|
| `send` / `reply` / `dm` | plan only, first 200 characters shown | posts the message |
| `edit` / `delete` | plan only | changes or removes the bot's own message |
| `react` / `thread` | plan only | adds the reaction or opens the thread |

Text over 2000 characters is refused unless `--split`, which breaks at line boundaries and
posts the parts in order. Nothing is ever silently truncated.
