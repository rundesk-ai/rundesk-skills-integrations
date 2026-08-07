---
name: discord
description: Use when the user asks to catch up on, find, or communicate in a Discord server, channel, thread, or DM, including an established Discord destination described without naming the service. It supplies bounded conversation reads and guarded messages, replies, DMs, reactions, threads, and attachments. Do not use for another chat service or a message with no Discord destination.
---

# Discord

Run `$RUNDESK_SKILLS/discord/scripts/discord`; it resolves the bot token itself, so never inspect or
print its source. Read `references/cli.md` only for setup, environment keys, output fields, API
behavior, or validation.

Start with:

```sh
"$RUNDESK_SKILLS/discord/scripts/discord" profiles
"$RUNDESK_SKILLS/discord/scripts/discord" status --profile <profile>
```

`status` reports the bot's identity and whether its writes are bounded to named servers,
channels, and users. Check it before offering to post anywhere.

## Reads

```sh
"$RUNDESK_SKILLS/discord/scripts/discord" guilds
"$RUNDESK_SKILLS/discord/scripts/discord" channels --guild <guild_id> --kind text
"$RUNDESK_SKILLS/discord/scripts/discord" threads --guild <guild_id>
"$RUNDESK_SKILLS/discord/scripts/discord" history <channel_id> --limit 25
"$RUNDESK_SKILLS/discord/scripts/discord" message <channel_id> <message_id> --full
"$RUNDESK_SKILLS/discord/scripts/discord" user <user_id>
```

`history` reads oldest first and clips each message at 300 characters; pass `--full` only
when the exact wording matters. Keep `--limit` small — a channel's backlog is unbounded
and each message costs context.

## Writes (owner-approved only)

`send`, `reply`, `dm`, `edit`, `delete`, `react`, and `thread` are **dry-run without
`--confirm`**. The dry run prints where the message would land and its first 200
characters.

Never pass `--confirm` unless the owner approved posting that text in that exact place in
this conversation. Show the dry run first.

```sh
"$RUNDESK_SKILLS/discord/scripts/discord" send <channel_id> --text "..."
"$RUNDESK_SKILLS/discord/scripts/discord" reply <channel_id> <message_id> --text "..."
"$RUNDESK_SKILLS/discord/scripts/discord" dm <user_id> --text "..."
"$RUNDESK_SKILLS/discord/scripts/discord" send <channel_id> --text "report" --file report.pdf
"$RUNDESK_SKILLS/discord/scripts/discord" thread <channel_id> --name "..." --message <message_id>
```

A room is read by everyone in it. Never post a credential, a private path, or anything
said in another conversation.

## Gotchas

- **Every id is a snowflake, not a name.** `#general` is refused. Turn on Developer Mode
  in Discord and use Copy ID, or find the id with `channels --guild <id>`.
- **A DM channel id is not a user id.** `dm` takes the *user* id and opens the channel
  itself.
- **2000 characters is a hard limit.** Longer text is refused rather than truncated. Pass
  `--split` to send it in parts at line breaks, or `--file` to attach it instead. Prefer
  attaching: a wall of split messages is worse to read than one file.
- **The bot only sees what it was invited to.** A 403 or 404 on a channel usually means
  missing access, not a wrong id — check `channels --guild <id>` first.
- **Reading message text needs the Message Content intent** enabled on the application, or
  `history` returns empty content for messages the bot was not mentioned in.
- **`edit` and `delete` only work on the bot's own messages** — deleting anyone else's
  needs Manage Messages, and is not something to do without an explicit request.
- Long text can come from a file with `--text-file`, or from stdin with `--text -`. Use
  that instead of pasting a large document into the command line.
