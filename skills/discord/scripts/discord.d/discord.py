#!/usr/bin/env python3
"""
Discord messaging and history for rundesk agents.

Usage:
  discord profiles
  discord status [--profile example]
  discord guilds [--profile example] [--limit 25]
  discord channels --guild GUILD_ID [--profile example] [--limit 100]
  discord channel CHANNEL_ID [--profile example]
  discord threads --guild GUILD_ID [--profile example] [--limit 50]
  discord history CHANNEL_ID [--limit 25] [--before ID] [--after ID] [--full]
  discord message CHANNEL_ID MESSAGE_ID [--profile example] [--full]
  discord user USER_ID [--profile example]
  discord send CHANNEL_ID --text "..." [--file PATH] [--split] [--confirm]
  discord reply CHANNEL_ID MESSAGE_ID --text "..." [--confirm]
  discord dm USER_ID --text "..." [--confirm]
  discord edit CHANNEL_ID MESSAGE_ID --text "..." [--confirm]
  discord delete CHANNEL_ID MESSAGE_ID [--confirm]
  discord react CHANNEL_ID MESSAGE_ID --emoji "👀" [--confirm]
  discord thread CHANNEL_ID --name "..." [--message MESSAGE_ID] [--confirm]

Inputs:
  Reads a dotenv outside the Rundesk skill library. Configure DISCORD_PROFILES and
  DISCORD_<PROFILE>_TOKEN, or a single DISCORD_BOT_TOKEN. Optional per-profile
  ALLOW_GUILDS / ALLOW_CHANNELS / ALLOW_USERS lists bound where writes may land.
  Secrets stay in the local dotenv only.

Outputs:
  Compact text / CSV for agent context. --json for structured payloads.
  Mutations are dry-run by default and require --confirm for the exact action.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


API_BASE = "https://discord.com/api/v10"

# Discord's own limits, and the ones a caller trips over first.
MESSAGE_LIMIT = 2000
HISTORY_MAX = 100
FILES_MAX = 5
FILE_BYTES_MAX = 8 * 1024 * 1024
PREVIEW_CHARS = 300

SNOWFLAKE = re.compile(r"^\d{17,20}$")

CHANNEL_KINDS = {
    0: "text", 1: "dm", 2: "voice", 3: "group-dm", 4: "category", 5: "announcement",
    10: "news-thread", 11: "public-thread", 12: "private-thread", 13: "stage",
    15: "forum", 16: "media",
}

GUILD_COLUMNS = ["id", "name", "owner", "profile"]
CHANNEL_COLUMNS = ["id", "name", "kind", "parent_id", "topic", "profile"]
MESSAGE_COLUMNS = ["id", "created_at", "author", "bot", "reply_to", "attachments", "content"]


def default_env_candidates() -> list[Path]:
    """Explicit shared configuration, then isolated and legacy per-tool files."""
    candidates: list[Path] = []
    for key in ("DISCORD_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "discord" / "env")
    candidates.append(xdg / "discord" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


class DiscordError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    token: str
    label: str
    allow_guilds: tuple[str, ...] = ()
    allow_channels: tuple[str, ...] = ()
    allow_users: tuple[str, ...] = ()

    @property
    def bounded(self) -> bool:
        return bool(self.allow_guilds or self.allow_channels or self.allow_users)

    def auth_headers(self) -> dict[str, str]:
        if not self.token:
            raise DiscordError(
                f"Profile {self.name!r} missing {env_name(self.name, 'TOKEN')}."
            )
        # A bot credential is `Bot <token>`, never `Bearer <token>`: Discord answers a
        # Bearer-prefixed bot token with 401 and no hint about the prefix.
        return {
            "Authorization": f"Bot {self.token}",
            "Accept": "application/json",
            "User-Agent": "DiscordBot (https://rundesk.ai, 1.0) rundesk-discord/1.0",
        }


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        print(
            f"WARNING: dotenv file {path} is accessible by group or others (mode {mode:04o}); "
            "restrict it with chmod 600.",
            file=sys.stderr,
        )
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = value


def env_name(profile: str, suffix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile).strip("_").upper()
    return f"DISCORD_{normalized}_{suffix}"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("DISCORD_PROFILES"))
    default = os.environ.get("DISCORD_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def discovered_profile_names() -> list[str]:
    names: set[str] = set()
    pattern = re.compile(
        r"^DISCORD_([A-Z0-9_]+)_(TOKEN|LABEL|ALLOW_GUILDS|ALLOW_CHANNELS|ALLOW_USERS)$"
    )
    for key in os.environ:
        match = pattern.match(key)
        if not match:
            continue
        raw = match.group(1)
        if raw in {"DEFAULT", "BOT", "API", "ENV"}:
            continue
        names.add(raw.lower().replace("_", "-"))
    if os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN"):
        names.add(os.environ.get("DISCORD_DEFAULT_PROFILE", "default") or "default")
    return sorted(names)


def allow_list(name: str, suffix: str) -> tuple[str, ...]:
    """A per-profile allowed list, falling back to the unprefixed one."""
    raw = os.environ.get(env_name(name, suffix)) or os.environ.get(f"DISCORD_{suffix}") or ""
    return tuple(split_csv(raw))


def get_profile(name: str) -> Profile:
    token = (
        os.environ.get(env_name(name, "TOKEN"))
        # The bare names exist so one bot needs no profile ceremony. DISCORD_TOKEN is
        # also what a Rundesk Discord *channel* reads, so an install that exports it
        # for the gateway hands the same identity to this command.
        or os.environ.get("DISCORD_BOT_TOKEN")
        or os.environ.get("DISCORD_TOKEN")
        or ""
    )
    if not token:
        raise DiscordError(
            f"Missing Discord config: {env_name(name, 'TOKEN')} (or DISCORD_BOT_TOKEN). "
            "Add it to the integration dotenv."
        )
    return Profile(
        name=name,
        token=token,
        label=os.environ.get(env_name(name, "LABEL"), name),
        allow_guilds=allow_list(name, "ALLOW_GUILDS"),
        allow_channels=allow_list(name, "ALLOW_CHANNELS"),
        allow_users=allow_list(name, "ALLOW_USERS"),
    )


def selected_profile_name(args: argparse.Namespace) -> str:
    if getattr(args, "profile", None):
        return args.profile
    default = os.environ.get("DISCORD_DEFAULT_PROFILE", "")
    if default:
        return default
    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if not names:
        raise DiscordError(
            "No Discord profiles configured. Set DISCORD_BOT_TOKEN, or DISCORD_PROFILES "
            "and DISCORD_<PROFILE>_TOKEN."
        )
    raise DiscordError(
        f"Multiple Discord profiles configured; pass --profile. Available: {', '.join(names)}"
    )


def snowflake(value: str, what: str) -> str:
    """A Discord id, or a refusal naming what was wanted.

    Checked before the request because an id that is really a channel *name* returns a
    404 that reads like a permission problem.
    """
    said = str(value).strip().lstrip("#<@!>").rstrip(">")
    if not SNOWFLAKE.match(said):
        raise DiscordError(
            f"{what} must be a Discord id (17-20 digits), not {value!r}. "
            "Turn on Developer Mode in Discord and use Copy ID."
        )
    return said


def text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    value = str(value).replace("\r", " ").replace("\n", " ").strip()
    return value if value else fallback


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def print_csv(columns: list[str], rows: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: text(row.get(column)) for column in columns})


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def encode_multipart(payload: dict[str, Any], files: list[Path]) -> tuple[bytes, str]:
    """`payload_json` plus one part per file, the shape Discord wants for attachments."""
    boundary = "----rundesk" + str(len(files)) + "boundary" + str(len(json.dumps(payload)))
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="payload_json"\r\n'
    body += b"Content-Type: application/json\r\n\r\n"
    body += json.dumps(payload).encode("utf-8") + b"\r\n"
    for index, path in enumerate(files):
        guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="files[{index}]"; '
            f'filename="{path.name}"\r\n'
        ).encode()
        body += f"Content-Type: {guessed}\r\n\r\n".encode()
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def request(
    profile: Profile,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    files: list[Path] | None = None,
    _retried: bool = False,
) -> Any:
    url = API_BASE + "/" + path.lstrip("/")
    if params:
        clean = {key: value for key, value in params.items() if value not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
    headers = profile.auth_headers()
    body = None
    if files:
        body, content_type = encode_multipart(payload or {}, files)
        headers["Content-Type"] = content_type
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw[:2000]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"message": raw[:500]}
        # Discord rate-limits per route and says how long to wait. One bounded wait is
        # worth it; a second means the caller is looping and should be told, not slept.
        if exc.code == 429 and not _retried:
            wait = min(float(data.get("retry_after") or 1), 5.0)
            time.sleep(wait)
            return request(profile, method, path, params, payload, files, _retried=True)
        detail = data.get("message") or raw[:300] or exc.reason
        code = data.get("code")
        raise DiscordError(
            f"Discord API {exc.code}{f'/{code}' if code else ''} profile={profile.name}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DiscordError(
            f"Discord API request failed profile={profile.name}: {exc.reason}"
        ) from exc


def guild_of(profile: Profile, channel_id: str) -> str:
    found = request(profile, "GET", f"channels/{channel_id}")
    return text(found.get("guild_id") if isinstance(found, dict) else "", "")


def allow_channel(profile: Profile, channel_id: str) -> None:
    """Refuse a write outside the places this profile was given, before it happens."""
    if channel_id in profile.allow_channels:
        return
    if profile.allow_channels and not profile.allow_guilds:
        raise DiscordError(
            f"Channel {channel_id} is not in {env_name(profile.name, 'ALLOW_CHANNELS')}; "
            "refusing to write there."
        )
    if profile.allow_guilds:
        guild = guild_of(profile, channel_id)
        if guild not in profile.allow_guilds:
            raise DiscordError(
                f"Channel {channel_id} belongs to guild {guild or 'unknown'}, which is not in "
                f"{env_name(profile.name, 'ALLOW_GUILDS')}; refusing to write there."
            )


def allow_user(profile: Profile, user_id: str) -> None:
    if user_id in profile.allow_users:
        return
    if profile.allow_users:
        raise DiscordError(
            f"User {user_id} is not in {env_name(profile.name, 'ALLOW_USERS')}; "
            "refusing to send a direct message."
        )
    if profile.bounded:
        raise DiscordError(
            f"This profile is bounded to named guilds or channels, so a direct message needs "
            f"{env_name(profile.name, 'ALLOW_USERS')}; refusing to message {user_id}."
        )


def message_body(args: argparse.Namespace) -> str:
    """The text to send: an argument, a file, or standard input for `--text -`."""
    said = getattr(args, "text", None)
    from_file = getattr(args, "text_file", None)
    if from_file:
        if said:
            raise DiscordError("Pass --text or --text-file, not both.")
        said = Path(from_file).expanduser().read_text(encoding="utf-8")
    elif said == "-":
        said = sys.stdin.read()
    if not said or not said.strip():
        raise DiscordError("Nothing to send: pass --text, --text-file, or --text - for stdin.")
    return said


def chunks(said: str, split: bool) -> list[str]:
    """One message, or the several Discord's 2000-character limit forces.

    Never silently truncated and never silently split: an answer cut in half without the
    caller knowing is worse than an error naming the length.
    """
    if len(said) <= MESSAGE_LIMIT:
        return [said]
    if not split:
        raise DiscordError(
            f"Message is {len(said)} characters; Discord's limit is {MESSAGE_LIMIT}. "
            "Shorten it, attach it with --file, or pass --split to send it in parts."
        )
    parts: list[str] = []
    rest = said
    while len(rest) > MESSAGE_LIMIT:
        window = rest[:MESSAGE_LIMIT]
        cut = window.rfind("\n")
        if cut < MESSAGE_LIMIT // 2:
            cut = MESSAGE_LIMIT
        parts.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


def attachments(args: argparse.Namespace) -> list[Path]:
    given = getattr(args, "file", None) or []
    if len(given) > FILES_MAX:
        raise DiscordError(f"At most {FILES_MAX} attachments per message.")
    ready: list[Path] = []
    for one in given:
        path = Path(one).expanduser()
        if not path.is_file():
            raise DiscordError(f"No such file: {path}")
        size = path.stat().st_size
        if size > FILE_BYTES_MAX:
            raise DiscordError(
                f"{path.name} is {size} bytes; Discord refuses more than {FILE_BYTES_MAX} "
                "without a boosted server."
            )
        ready.append(path)
    return ready


def author_of(item: dict[str, Any]) -> tuple[str, Any]:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    who = author.get("global_name") or author.get("username") or author.get("id")
    return text(who), author.get("bot", False)


def message_row(item: dict[str, Any], full: bool) -> dict[str, Any]:
    who, is_bot = author_of(item)
    reference = item.get("message_reference")
    content = text(item.get("content"), "")
    embeds = item.get("embeds") or []
    if not content and embeds:
        content = "[embed] " + text(
            (embeds[0] or {}).get("title") or (embeds[0] or {}).get("description"), ""
        )
    return {
        "id": item.get("id"),
        "created_at": item.get("timestamp"),
        "author": who,
        "bot": is_bot,
        "reply_to": (reference or {}).get("message_id") if isinstance(reference, dict) else None,
        "attachments": len(item.get("attachments") or []),
        "content": content if full else clip(content, PREVIEW_CHARS),
    }


def cmd_profiles(_args: argparse.Namespace) -> int:
    names = configured_profile_names()
    if not names:
        print("No Discord profiles configured.")
        return 0
    default = os.environ.get("DISCORD_DEFAULT_PROFILE", "")
    for name in names:
        marker = " (default)" if name == default or (not default and len(names) == 1) else ""
        try:
            profile = get_profile(name)
            bounds = "unbounded" if not profile.bounded else ",".join(
                part for part in (
                    f"guilds={len(profile.allow_guilds)}" if profile.allow_guilds else "",
                    f"channels={len(profile.allow_channels)}" if profile.allow_channels else "",
                    f"users={len(profile.allow_users)}" if profile.allow_users else "",
                ) if part
            )
            print(f"{name}{marker}\t{profile.label}\ttoken=set\twrites={bounds}")
        except DiscordError as exc:
            print(f"{name}{marker}\tERROR\t{exc}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    me = request(profile, "GET", "users/@me") or {}
    guilds = request(profile, "GET", "users/@me/guilds", params={"limit": 200}) or []
    print(f"profile\t{profile.name}")
    print(f"label\t{profile.label}")
    print(f"bot\t{text(me.get('username'))}")
    print(f"bot_id\t{text(me.get('id'))}")
    print(f"guilds\t{len(guilds) if isinstance(guilds, list) else 0}")
    print(f"allow_guilds\t{','.join(profile.allow_guilds) or '-'}")
    print(f"allow_channels\t{','.join(profile.allow_channels) or '-'}")
    print(f"allow_users\t{','.join(profile.allow_users) or '-'}")
    print(f"writes\t{'bounded' if profile.bounded else 'anywhere the bot can post'}")
    print("api\tok")
    return 0


def cmd_guilds(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", "users/@me/guilds", params={"limit": 200}) or []
    rows = [item for item in data if isinstance(item, dict)][: args.limit]
    if args.json:
        print_json(rows)
        return 0
    print_csv(GUILD_COLUMNS, [
        {"id": r.get("id"), "name": r.get("name"), "owner": r.get("owner"),
         "profile": profile.name}
        for r in rows
    ])
    return 0


def channel_row(item: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "kind": CHANNEL_KINDS.get(item.get("type"), text(item.get("type"))),
        "parent_id": item.get("parent_id"),
        "topic": clip(text(item.get("topic"), ""), 80),
        "profile": profile.name,
    }


def cmd_channels(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    guild = snowflake(args.guild, "--guild")
    data = request(profile, "GET", f"guilds/{guild}/channels") or []
    rows = [item for item in data if isinstance(item, dict)]
    if args.kind:
        wanted = {number for number, name in CHANNEL_KINDS.items() if name == args.kind}
        rows = [item for item in rows if item.get("type") in wanted]
    rows = rows[: args.limit]
    if args.json:
        print_json(rows)
        return 0
    print_csv(CHANNEL_COLUMNS, [channel_row(r, profile) for r in rows])
    return 0


def cmd_channel(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", f"channels/{snowflake(args.channel, 'CHANNEL_ID')}") or {}
    if args.json:
        print_json(data)
        return 0
    print_csv(CHANNEL_COLUMNS + ["guild_id"], [
        dict(channel_row(data, profile), guild_id=data.get("guild_id"))
    ])
    return 0


def cmd_threads(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    guild = snowflake(args.guild, "--guild")
    data = request(profile, "GET", f"guilds/{guild}/threads/active") or {}
    rows = [item for item in (data.get("threads") or []) if isinstance(item, dict)][: args.limit]
    if args.json:
        print_json(rows)
        return 0
    print_csv(CHANNEL_COLUMNS, [channel_row(r, profile) for r in rows])
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    params = {
        "limit": args.limit,
        "before": snowflake(args.before, "--before") if args.before else None,
        "after": snowflake(args.after, "--after") if args.after else None,
    }
    data = request(profile, "GET", f"channels/{channel}/messages", params=params) or []
    rows = [item for item in data if isinstance(item, dict)]
    # Discord answers newest first. Oldest first is how a conversation reads, and how a
    # summary of one comes out right.
    if not args.newest_first:
        rows = list(reversed(rows))
    if args.json:
        print_json(rows)
        return 0
    print_csv(MESSAGE_COLUMNS, [message_row(r, args.full) for r in rows])
    return 0


def cmd_message(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    message = snowflake(args.message, "MESSAGE_ID")
    data = request(profile, "GET", f"channels/{channel}/messages/{message}") or {}
    if args.json:
        print_json(data)
        return 0
    row = message_row(data, args.full)
    for key in MESSAGE_COLUMNS:
        print(f"{key}\t{text(row.get(key))}")
    for one in data.get("attachments") or []:
        print(f"attachment\t{text(one.get('filename'))}\t{text(one.get('size'))}")
    return 0


def cmd_user(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    data = request(profile, "GET", f"users/{snowflake(args.user, 'USER_ID')}") or {}
    if args.json:
        print_json(data)
        return 0
    for key in ("id", "username", "global_name", "bot"):
        print(f"{key}\t{text(data.get(key))}")
    return 0


def mutation(
    args: argparse.Namespace,
    profile: Profile,
    *,
    action: str,
    where: str,
    preview: dict[str, Any] | None = None,
    run=None,
) -> int:
    """Say exactly what would happen; do it only when this exact action was confirmed."""
    print(f"action\t{action}")
    print(f"where\t{where}")
    print(f"profile\t{profile.name}")
    for key, value in (preview or {}).items():
        print(f"{key}\t{value}")
    if not args.confirm:
        print("mode\tdry-run")
        print(
            f"next\tOwner approval required. Re-run with --confirm only after the owner "
            f"approves {action} in {where}."
        )
        return 0
    result = run()
    print("mode\tconfirmed")
    if getattr(args, "json", False):
        print_json(result)
        return 0
    for item in result if isinstance(result, list) else [result]:
        if isinstance(item, dict) and item.get("id"):
            print(f"message_id\t{item['id']}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    allow_channel(profile, channel)
    said = message_body(args)
    parts = chunks(said, args.split)
    files = attachments(args)

    def run() -> list[Any]:
        sent = []
        for index, part in enumerate(parts):
            sent.append(request(
                profile, "POST", f"channels/{channel}/messages",
                payload={"content": part},
                files=files if index == 0 else None,
            ))
        return sent

    return mutation(
        args, profile, action="send a message", where=f"channel {channel}",
        preview={
            "characters": len(said),
            "messages": len(parts),
            "attachments": len(files),
            "preview": clip(text(parts[0]), 200),
        },
        run=run,
    )


def cmd_reply(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    message = snowflake(args.message, "MESSAGE_ID")
    allow_channel(profile, channel)
    said = message_body(args)
    parts = chunks(said, args.split)
    files = attachments(args)
    payload = {
        "content": parts[0],
        # fail_if_not_exists off: a deleted parent must cost the quote, not the answer.
        "message_reference": {"message_id": message, "fail_if_not_exists": False},
    }

    def run() -> list[Any]:
        sent = [request(profile, "POST", f"channels/{channel}/messages",
                        payload=payload, files=files)]
        for part in parts[1:]:
            sent.append(request(profile, "POST", f"channels/{channel}/messages",
                                payload={"content": part}))
        return sent

    return mutation(
        args, profile, action=f"reply to message {message}", where=f"channel {channel}",
        preview={"characters": len(said), "messages": len(parts),
                 "attachments": len(files), "preview": clip(text(parts[0]), 200)},
        run=run,
    )


def cmd_dm(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    user = snowflake(args.user, "USER_ID")
    allow_user(profile, user)
    said = message_body(args)
    parts = chunks(said, args.split)
    files = attachments(args)

    def run() -> list[Any]:
        # A DM channel id is not the user id, and only exists once it has been opened.
        opened = request(profile, "POST", "users/@me/channels", payload={"recipient_id": user})
        channel = (opened or {}).get("id")
        if not channel:
            raise DiscordError(f"Could not open a direct message channel with {user}.")
        sent = []
        for index, part in enumerate(parts):
            sent.append(request(
                profile, "POST", f"channels/{channel}/messages",
                payload={"content": part}, files=files if index == 0 else None,
            ))
        return sent

    return mutation(
        args, profile, action="send a direct message", where=f"user {user}",
        preview={"characters": len(said), "messages": len(parts),
                 "attachments": len(files), "preview": clip(text(parts[0]), 200)},
        run=run,
    )


def cmd_edit(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    message = snowflake(args.message, "MESSAGE_ID")
    allow_channel(profile, channel)
    said = message_body(args)
    if len(said) > MESSAGE_LIMIT:
        raise DiscordError(
            f"Edited message is {len(said)} characters; Discord's limit is {MESSAGE_LIMIT}."
        )
    return mutation(
        args, profile, action=f"edit message {message}", where=f"channel {channel}",
        preview={"characters": len(said), "preview": clip(text(said), 200)},
        run=lambda: request(profile, "PATCH", f"channels/{channel}/messages/{message}",
                            payload={"content": said}),
    )


def cmd_delete(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    message = snowflake(args.message, "MESSAGE_ID")
    allow_channel(profile, channel)
    return mutation(
        args, profile, action=f"delete message {message}", where=f"channel {channel}",
        run=lambda: request(profile, "DELETE", f"channels/{channel}/messages/{message}"),
    )


def cmd_react(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    message = snowflake(args.message, "MESSAGE_ID")
    allow_channel(profile, channel)
    # A unicode emoji is percent-encoded; a custom one is `name:id` and must keep its colon.
    emoji = urllib.parse.quote(args.emoji, safe=":")
    return mutation(
        args, profile, action=f"react {args.emoji} to message {message}",
        where=f"channel {channel}",
        run=lambda: request(
            profile, "PUT", f"channels/{channel}/messages/{message}/reactions/{emoji}/@me",
        ),
    )


def cmd_thread(args: argparse.Namespace) -> int:
    profile = get_profile(selected_profile_name(args))
    channel = snowflake(args.channel, "CHANNEL_ID")
    allow_channel(profile, channel)
    name = args.name.strip()
    if not name or len(name) > 100:
        raise DiscordError("--name must be 1-100 characters.")
    if args.message:
        message = snowflake(args.message, "--message")
        path = f"channels/{channel}/messages/{message}/threads"
        payload: dict[str, Any] = {"name": name}
        action = f"open a thread on message {message}"
    else:
        path = f"channels/{channel}/threads"
        payload = {"name": name, "type": 11}
        action = "open a public thread"
    return mutation(
        args, profile, action=action, where=f"channel {channel}",
        preview={"name": name},
        run=lambda: request(profile, "POST", path, payload=payload),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discord",
        description="Discord messages, history and channels (writes dry-run without --confirm).",
    )
    parser.add_argument("--env-file", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(one: argparse.ArgumentParser, *, limit: int | None = None) -> None:
        one.add_argument("--profile", default=None)
        one.add_argument("--json", action="store_true")
        if limit is not None:
            one.add_argument("--limit", type=int, default=limit)

    def add_writing(one: argparse.ArgumentParser, *, splittable: bool = True) -> None:
        one.add_argument("--text", default=None, help="Message text, or - to read stdin")
        one.add_argument("--text-file", default=None, help="Read the message from a file")
        one.add_argument("--confirm", action="store_true")
        if splittable:
            one.add_argument("--file", action="append", default=[], metavar="PATH",
                             help=f"Attach a file (at most {FILES_MAX})")
            one.add_argument("--split", action="store_true",
                             help=f"Send in parts when longer than {MESSAGE_LIMIT} characters")

    one = sub.add_parser("profiles", help="List configured profiles and their write bounds")
    one.set_defaults(func=cmd_profiles)

    one = sub.add_parser("status", help="Bot identity, guild count, write bounds (read-only)")
    add_common(one)
    one.set_defaults(func=cmd_status)

    one = sub.add_parser("guilds", help="Servers this bot is in")
    add_common(one, limit=25)
    one.set_defaults(func=cmd_guilds)

    one = sub.add_parser("channels", help="Channels in one server")
    add_common(one, limit=100)
    one.add_argument("--guild", required=True)
    one.add_argument("--kind", default=None, choices=sorted(set(CHANNEL_KINDS.values())))
    one.set_defaults(func=cmd_channels)

    one = sub.add_parser("channel", help="Show one channel")
    add_common(one)
    one.add_argument("channel")
    one.set_defaults(func=cmd_channel)

    one = sub.add_parser("threads", help="Active threads in one server")
    add_common(one, limit=50)
    one.add_argument("--guild", required=True)
    one.set_defaults(func=cmd_threads)

    one = sub.add_parser("history", help="Recent messages in a channel or thread")
    add_common(one, limit=25)
    one.add_argument("channel")
    one.add_argument("--before", default=None, help="Messages older than this message id")
    one.add_argument("--after", default=None, help="Messages newer than this message id")
    one.add_argument("--full", action="store_true", help="Do not clip message content")
    one.add_argument("--newest-first", action="store_true", help="Discord's own order")
    one.set_defaults(func=cmd_history)

    one = sub.add_parser("message", help="Show one message")
    add_common(one)
    one.add_argument("channel")
    one.add_argument("message")
    one.add_argument("--full", action="store_true")
    one.set_defaults(func=cmd_message)

    one = sub.add_parser("user", help="Show one user")
    add_common(one)
    one.add_argument("user")
    one.set_defaults(func=cmd_user)

    one = sub.add_parser("send", help="Post a message (dry-run unless --confirm)")
    add_common(one)
    one.add_argument("channel")
    add_writing(one)
    one.set_defaults(func=cmd_send)

    one = sub.add_parser("reply", help="Reply to a message (dry-run unless --confirm)")
    add_common(one)
    one.add_argument("channel")
    one.add_argument("message")
    add_writing(one)
    one.set_defaults(func=cmd_reply)

    one = sub.add_parser("dm", help="Direct message a user (dry-run unless --confirm)")
    add_common(one)
    one.add_argument("user")
    add_writing(one)
    one.set_defaults(func=cmd_dm)

    one = sub.add_parser("edit", help="Edit one of the bot's own messages")
    add_common(one)
    one.add_argument("channel")
    one.add_argument("message")
    add_writing(one, splittable=False)
    one.set_defaults(func=cmd_edit)

    one = sub.add_parser("delete", help="Delete a message (dry-run unless --confirm)")
    add_common(one)
    one.add_argument("channel")
    one.add_argument("message")
    one.add_argument("--confirm", action="store_true")
    one.set_defaults(func=cmd_delete)

    one = sub.add_parser("react", help="Add a reaction (dry-run unless --confirm)")
    add_common(one)
    one.add_argument("channel")
    one.add_argument("message")
    one.add_argument("--emoji", required=True, help="A unicode emoji, or name:id for a custom one")
    one.add_argument("--confirm", action="store_true")
    one.set_defaults(func=cmd_react)

    one = sub.add_parser("thread", help="Open a thread (dry-run unless --confirm)")
    add_common(one)
    one.add_argument("channel")
    one.add_argument("--name", required=True)
    one.add_argument("--message", default=None, help="Open the thread on this message")
    one.add_argument("--confirm", action="store_true")
    one.set_defaults(func=cmd_thread)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(resolve_env_file(getattr(args, "env_file", None)))
    try:
        limit = getattr(args, "limit", None)
        if limit is not None and (limit < 1 or limit > 200):
            raise DiscordError("--limit must be between 1 and 200")
        if args.command == "history" and limit is not None and limit > HISTORY_MAX:
            raise DiscordError(f"history --limit must be between 1 and {HISTORY_MAX}")
        return int(args.func(args))
    except DiscordError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"discord: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
