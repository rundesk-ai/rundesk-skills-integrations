#!/usr/bin/env python3
"""Fetch Slack messages, threads, and hosted attachments through read-only Web API methods."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional


API_ORIGIN = "https://slack.com"
ALLOWED_METHODS = frozenset({
    "auth.test",
    "conversations.history",
    "conversations.list",
    "conversations.replies",
    "files.info",
    "search.messages",
})
DEFAULT_CONVERSATION_TYPES = ("public_channel", "private_channel", "mpim", "im")
CONVERSATION_TYPES = frozenset(DEFAULT_CONVERSATION_TYPES)
PROFILE_FIELDS = {
    "SLACK_FETCH_TOKEN": "TOKEN",
    "SLACK_FETCH_LABEL": "LABEL",
    "SLACK_FETCH_TYPES": "TYPES",
    "SLACK_FETCH_CHANNELS": "CHANNELS",
}
REQUIRED_FIELDS = ("SLACK_FETCH_TOKEN",)
PLAIN_ALIASES = {
    "SLACK_FETCH_TOKEN": ("SLACK_TOKEN", "SLACK_USER_TOKEN"),
    "SLACK_FETCH_LABEL": ("SLACK_LABEL",),
    "SLACK_FETCH_TYPES": ("SLACK_TYPES",),
    "SLACK_FETCH_CHANNELS": ("SLACK_CHANNELS",),
}
ACCOUNT_SUFFIX_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")
RESERVED_PROFILE_WORDS = frozenset({"DEFAULT", "ENV", "PROFILES"})
CHANNEL_RE = re.compile(r"[CDG][A-Z0-9]{8,}")
TS_RE = re.compile(r"\d{10,}\.\d{6}")
FILE_RE = re.compile(r"F[A-Z0-9]{8,}")
# Slack serves private file bytes from this origin only. The bearer token is sent here and
# to the API origin and nowhere else, so a metadata URL or redirect that leaves the set is
# refused rather than followed.
FILE_ORIGIN = "https://files.slack.com"
ALLOWED_FILE_ORIGINS = frozenset({FILE_ORIGIN})
# Only a Slack-hosted file has bytes this command may retrieve. An external, snippet, post,
# or tombstoned file either lives outside Slack or is not the uploaded artifact.
HOSTED_FILE_MODES = frozenset({"hosted"})
DEFAULT_MAX_ATTACHMENT_BYTES = 26214400
MAX_ATTACHMENT_BYTES = 104857600
DOWNLOAD_CHUNK_BYTES = 65536
# Named without the staged file's path: the caller owns the directory and can find it, and the
# path is private material that does not belong in an error line.
STAGED_RESIDUE_NOTE = (
    "A private temporary copy may also remain in the destination "
    "directory; remove it manually."
)
# Slack's own error code is exact but terse, and the scope that fixes it is not in the reply.
FILE_ERROR_GUIDANCE = {
    "missing_scope": (
        "Attachment retrieval needs the files:read scope on this profile's user token; "
        "message-only commands do not. Add files:read in the Slack app's OAuth & Permissions "
        "page, reinstall the app, and reconfigure the profile."
    ),
    "not_allowed_token_type": (
        "Attachment retrieval needs the user token this package already documents, "
        "not a bot token."
    ),
    "file_not_found": "The selected profile cannot see a file with that exact ID.",
    "file_deleted": "Slack reports this file as deleted; there are no bytes to retrieve.",
    "access_denied": "The selected profile is not allowed to read this file.",
}


class SlackError(RuntimeError):
    """A refusal or provider failure, carrying Slack's sanitized error code when it has one."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Profile:
    name: str
    token: str
    label: str
    conversation_types: tuple[str, ...] = DEFAULT_CONVERSATION_TYPES
    channels: tuple[str, ...] = ()


def default_env_candidates() -> list[Path]:
    candidates: list[Path] = []
    for key in ("SLACK_FETCH_ENV_FILE", "SLACK_ENV_FILE", "RUNDESK_INTEGRATIONS_ENV"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    candidates.append(xdg / "rundesk" / "integrations" / "slack-fetch" / "env")
    candidates.append(xdg / "rundesk" / "integrations" / "slack" / "env")
    candidates.append(xdg / "slack" / "env")
    return candidates


def resolve_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for path in default_env_candidates():
        if path.is_file():
            return path
    return default_env_candidates()[-1]


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
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_profile(profile: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", profile or "").strip("_").upper()


def profile_label(suffix: str) -> str:
    return suffix.lower().replace("_", "-")


def split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def is_default_profile(profile: str) -> bool:
    normalized = normalize_profile(profile)
    if not normalized or normalized == "DEFAULT":
        return True
    default = os.environ.get("SLACK_FETCH_DEFAULT_PROFILE", "")
    default = default or os.environ.get("SLACK_DEFAULT_PROFILE", "")
    return normalized == normalize_profile(default)


def profile_value(profile: str, field: str) -> str:
    normalized = normalize_profile(profile)
    candidates: list[str] = []
    if normalized:
        candidates.append(f"{field}__{normalized}")
        candidates.append(f"SLACK_FETCH_{normalized}_{PROFILE_FIELDS[field]}")
        candidates.append(f"SLACK_{normalized}_{PROFILE_FIELDS[field]}")
    if is_default_profile(profile):
        candidates.append(field)
        candidates.extend(PLAIN_ALIASES.get(field, ()))
    for name in candidates:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def discovered_profile_names() -> list[str]:
    suffixed: set[str] = set()
    infixed: set[str] = set()
    legacy_patterns = (
        re.compile(
            rf"^SLACK_FETCH_({ACCOUNT_SUFFIX_RE.pattern})_({'|'.join(PROFILE_FIELDS.values())})$"
        ),
        re.compile(
            rf"^SLACK_({ACCOUNT_SUFFIX_RE.pattern})_({'|'.join(PROFILE_FIELDS.values())})$"
        ),
    )
    plain_names = set(PROFILE_FIELDS)
    for aliases in PLAIN_ALIASES.values():
        plain_names.update(aliases)
    for key in os.environ:
        for field in PROFILE_FIELDS:
            prefix = f"{field}__"
            if key.startswith(prefix) and ACCOUNT_SUFFIX_RE.fullmatch(key[len(prefix):]):
                suffixed.add(profile_label(key[len(prefix):]))
        if key in plain_names:
            continue
        match = None
        for pattern in legacy_patterns:
            match = pattern.match(key)
            if match:
                break
        if not match:
            continue
        word = match.group(1)
        if word == "DEFAULT":
            infixed.add("default")
        elif word not in RESERVED_PROFILE_WORDS:
            infixed.add(profile_label(word))
    names = suffixed | infixed
    if not infixed and any(profile_value("", field) for field in REQUIRED_FIELDS):
        names.add(os.environ.get("SLACK_DEFAULT_PROFILE") or "default")
    return sorted(names)


def configured_profile_names() -> list[str]:
    names = split_csv(os.environ.get("SLACK_FETCH_PROFILES"))
    names = names or split_csv(os.environ.get("SLACK_PROFILES"))
    default = os.environ.get("SLACK_FETCH_DEFAULT_PROFILE", "")
    default = default or os.environ.get("SLACK_DEFAULT_PROFILE", "")
    if default and default not in names:
        names.insert(0, default)
    return names or discovered_profile_names()


def selected_profile_name(args: argparse.Namespace | SimpleNamespace) -> str:
    requested = getattr(args, "profile", None)
    if requested:
        return requested
    names = configured_profile_names()
    if len(names) == 1:
        return names[0]
    if not names:
        return "default"
    raise SlackError("Multiple Slack profiles are configured; choose --profile from: " + ", ".join(names))


def get_profile(name: str) -> Profile:
    token = profile_value(name, "SLACK_FETCH_TOKEN")
    if not token:
        suffix = "" if is_default_profile(name) else f"__{normalize_profile(name)}"
        raise SlackError(
            f"Missing SLACK_FETCH_TOKEN{suffix}. Run rundesk skills configure "
            "rundesk-skills-integrations/slack-fetch for this account."
        )
    label = profile_value(name, "SLACK_FETCH_LABEL") or name
    raw_types = profile_value(name, "SLACK_FETCH_TYPES")
    conversation_types = tuple(split_csv(raw_types)) if raw_types else DEFAULT_CONVERSATION_TYPES
    unsupported = sorted(set(conversation_types) - CONVERSATION_TYPES)
    if unsupported:
        raise SlackError("Unsupported Slack conversation types: " + ", ".join(unsupported))
    channels = tuple(value.upper() for value in split_csv(profile_value(name, "SLACK_FETCH_CHANNELS")))
    invalid_channels = sorted(value for value in channels if not CHANNEL_RE.fullmatch(value))
    if invalid_channels:
        raise SlackError("Slack channel discovery filter contains invalid conversation IDs.")
    return Profile(
        name=name,
        token=token,
        label=label,
        conversation_types=conversation_types,
        channels=channels,
    )


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (original.scheme, original.hostname, original.port) != (
            target.scheme,
            target.hostname,
            target.port,
        ):
            raise SlackError("Slack API refused an unexpected cross-origin redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def api_call(profile: Profile, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method not in ALLOWED_METHODS:
        raise SlackError(f"Slack API method is not allowed by this read-only command: {method}")
    encoded = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = urllib.request.Request(
        f"{API_ORIGIN}/api/{method}?{encoded}",
        headers={"Authorization": f"Bearer {profile.token}", "User-Agent": "rundesk-slack-fetch/1"},
        method="GET",
    )
    opener = urllib.request.build_opener(SameOriginRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry = exc.headers.get("Retry-After", "the provider interval")
            raise SlackError(f"Slack rate-limited this read; retry after {retry} seconds.") from None
        raise SlackError(f"Slack API HTTP error {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SlackError(f"Slack API read failed ({type(exc).__name__}).") from None
    if not isinstance(payload, dict):
        raise SlackError("Slack API returned an invalid response shape.")
    if not payload.get("ok"):
        code = str(payload.get("error") or "unknown_error")
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", code)[:80]
        raise SlackError(f"Slack API refused the read: {safe}.", code=safe)
    return payload


def compact_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_match(match: dict[str, Any]) -> dict[str, Any]:
    channel = match.get("channel") if isinstance(match.get("channel"), dict) else {}
    return {
        "channel_id": str(channel.get("id") or match.get("channel_id") or ""),
        "channel_name": str(channel.get("name") or ""),
        "ts": str(match.get("ts") or ""),
        "thread_ts": str(match.get("thread_ts") or match.get("ts") or ""),
        "user": str(match.get("user") or match.get("username") or ""),
        "text": str(match.get("text") or ""),
        "permalink": str(match.get("permalink") or ""),
    }


def conversation_type(conversation: dict[str, Any]) -> str:
    channel_id = str(conversation.get("id") or conversation.get("channel_id") or "")
    if conversation.get("is_im") or channel_id.startswith("D"):
        return "im"
    if conversation.get("is_mpim"):
        return "mpim"
    if conversation.get("is_private"):
        return "private_channel"
    return "public_channel"


def channel_matches_discovery_filter(profile: Profile, channel_id: str) -> bool:
    if not profile.channels:
        return True
    allowed = {value.upper() for value in profile.channels}
    return channel_id.upper() in allowed


def list_conversations(profile: Profile, limit: int) -> tuple[list[dict[str, Any]], bool]:
    conversations: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    provider_has_more = False
    while len(conversations) < limit:
        payload = api_call(
            profile,
            "conversations.list",
            {
                "types": ",".join(profile.conversation_types),
                "exclude_archived": "true",
                "limit": min(200, limit - len(conversations)),
                "cursor": cursor,
            },
        )
        page = payload.get("channels") if isinstance(payload.get("channels"), list) else []
        matching = [
            item for item in page
            if isinstance(item, dict)
            and channel_matches_discovery_filter(profile, str(item.get("id") or ""))
        ]
        remaining = limit - len(conversations)
        conversations.extend(matching[:remaining])
        metadata = payload.get("response_metadata")
        next_cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
        provider_has_more = bool(next_cursor or len(matching) > remaining)
        if not next_cursor or len(conversations) >= limit:
            break
        if next_cursor in seen_cursors:
            raise SlackError("Slack channel pagination repeated a cursor; refusing an incomplete loop.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return conversations, provider_has_more


def message_history(
    profile: Profile,
    channel: str,
    limit: int,
    oldest: str = "",
    latest: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    if not CHANNEL_RE.fullmatch(channel):
        raise SlackError("Channel must be a Slack conversation ID beginning with C, D, or G.")
    for label, value in (("oldest", oldest), ("latest", latest)):
        if value and not TS_RE.fullmatch(value):
            raise SlackError(f"--{label} must use Slack's seconds.microseconds timestamp format.")
    messages: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    provider_has_more = False
    while len(messages) < limit:
        payload = api_call(
            profile,
            "conversations.history",
            {
                "channel": channel,
                "limit": min(15, limit - len(messages)),
                "cursor": cursor,
                "oldest": oldest,
                "latest": latest,
                "inclusive": "true" if oldest or latest else "",
            },
        )
        page = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        valid_page = [item for item in page if isinstance(item, dict)]
        remaining = limit - len(messages)
        messages.extend(valid_page[:remaining])
        metadata = payload.get("response_metadata")
        next_cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
        provider_has_more = bool(next_cursor or payload.get("has_more") or len(valid_page) > remaining)
        if not next_cursor or len(messages) >= limit:
            break
        if next_cursor in seen_cursors:
            raise SlackError("Slack message pagination repeated a cursor; refusing an incomplete loop.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return messages[:limit], provider_has_more


def search_messages(profile: Profile, query: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    if not query.strip():
        raise SlackError("Search query cannot be empty.")
    if len(query) > 500 or any(ord(char) < 32 and char not in "\t" for char in query):
        raise SlackError("Search query must be at most 500 characters on one line.")
    results: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    page = 1
    provider_has_more = False
    while len(results) < limit:
        payload = api_call(
            profile,
            "search.messages",
            {"query": query, "count": min(100, limit - len(results)), "cursor": cursor, "page": page},
        )
        messages = payload.get("messages") if isinstance(payload.get("messages"), dict) else {}
        matches = messages.get("matches") if isinstance(messages.get("matches"), list) else []
        valid_matches = [item for item in matches if isinstance(item, dict)]
        remaining = limit - len(results)
        results.extend(valid_matches[:remaining])
        response_metadata = payload.get("response_metadata")
        cursor = str(response_metadata.get("next_cursor") or "") if isinstance(response_metadata, dict) else ""
        pagination = messages.get("pagination") if isinstance(messages.get("pagination"), dict) else {}
        page_count = int(pagination.get("page_count") or page)
        provider_has_more = bool(cursor or page < page_count or len(valid_matches) > remaining)
        if len(results) >= limit:
            break
        if cursor:
            if cursor in seen_cursors:
                raise SlackError("Slack search pagination repeated a cursor; refusing an incomplete loop.")
            seen_cursors.add(cursor)
            continue
        if page >= page_count:
            break
        page += 1
    return results[:limit], provider_has_more


def parse_permalink(value: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(".slack.com") or parsed.username or parsed.password:
        raise SlackError("Permalink must be an HTTPS Slack message URL.")
    match = re.fullmatch(r"/archives/([CDG][A-Z0-9]{8,})/p(\d{16,})/?", parsed.path)
    if not match:
        raise SlackError("Permalink must identify one Slack message under /archives/<channel>/p<timestamp>.")
    digits = match.group(2)
    return match.group(1), f"{digits[:-6]}.{digits[-6:]}"


def thread_messages(profile: Profile, channel: str, ts: str, max_messages: int) -> list[dict[str, Any]]:
    if not CHANNEL_RE.fullmatch(channel):
        raise SlackError("Channel must be a Slack conversation ID beginning with C, D, or G.")
    if not TS_RE.fullmatch(ts):
        raise SlackError("Thread timestamp must use Slack's seconds.microseconds format.")
    messages: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        payload = api_call(
            profile,
            "conversations.replies",
            {"channel": channel, "ts": ts, "limit": min(15, max_messages - len(messages)), "cursor": cursor},
        )
        page = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        messages.extend(item for item in page if isinstance(item, dict))
        metadata = payload.get("response_metadata")
        cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
        if not cursor:
            if payload.get("has_more"):
                raise SlackError("Thread is incomplete: Slack reported another page without a cursor.")
            return messages
        if cursor in seen_cursors:
            raise SlackError("Thread is incomplete: Slack repeated a pagination cursor.")
        seen_cursors.add(cursor)
        if len(messages) >= max_messages:
            raise SlackError(
                f"Thread is incomplete: reached --max-messages {max_messages} before Slack's final page."
            )


@dataclass(frozen=True)
class Attachment:
    """One validated Slack-hosted file. `download_url` is private and is never printed."""

    file_id: str
    name: str
    mimetype: str
    size: int
    download_url: str


def safe_label(value: Any, limit: int = 140) -> str:
    """Render a provider-supplied label for terminal output.

    A non-printing character or a `|` in a Slack-supplied name would otherwise break or forge
    a field in the pipe-delimited completion record a reader trusts. Every Unicode control,
    format, surrogate, private-use, and unassigned code point goes, which covers C0, DEL, C1,
    and the bidi and zero-width overrides that reorder or hide text in a terminal. Letters,
    marks, punctuation, and symbols in any script are kept as they are.
    """
    text = "".join(
        "/" if char == "|"
        else " " if unicodedata.category(char).startswith("C")
        else char
        for char in str(value or "")
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def request_origin(value: str) -> str:
    """Return the scheme://host[:port] of a URL, or an empty string when it has none."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return ""
    default_port = {"https": 443, "http": 80}.get(scheme)
    if port is None or port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def validate_download_url(value: str) -> str:
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise SlackError("Slack file metadata has no usable private download URL.")
    try:
        parsed = urllib.parse.urlsplit(value)
        credentials = parsed.username or parsed.password
    except ValueError:
        raise SlackError("Slack returned a malformed file download URL.") from None
    if credentials or request_origin(value) not in ALLOWED_FILE_ORIGINS:
        raise SlackError(f"Slack returned a file download URL outside {FILE_ORIGIN}.")
    return value


def file_info(profile: Profile, file_id: str) -> dict[str, Any]:
    if not FILE_RE.fullmatch(file_id):
        raise SlackError("File must be an exact Slack file ID beginning with F.")
    try:
        payload = api_call(profile, "files.info", {"file": file_id})
    except SlackError as exc:
        guidance = FILE_ERROR_GUIDANCE.get(exc.code)
        if guidance:
            raise SlackError(f"{exc} {guidance}", code=exc.code) from None
        raise
    info = payload.get("file")
    if not isinstance(info, dict):
        raise SlackError("Slack returned no file object for that ID.")
    return info


def resolve_attachment(profile: Profile, file_id: str, max_bytes: int) -> Attachment:
    """Validate that one exact file is visible, Slack-hosted, and within the size bound."""
    info = file_info(profile, file_id)
    if str(info.get("id") or "") != file_id:
        raise SlackError("Slack returned metadata for a different file ID.")
    access = str(info.get("file_access") or "")
    if access != "visible":
        raise SlackError(
            f"Slack reports this file as {safe_label(access, 40) or 'not visible'}; "
            "the selected profile cannot read it."
        )
    mode = str(info.get("mode") or "")
    if mode not in HOSTED_FILE_MODES or info.get("is_external") or info.get("external_type"):
        raise SlackError(
            f"Only Slack-hosted files can be retrieved; Slack reports mode={safe_label(mode, 40) or '-'}."
        )
    # The MIME type is part of the completion record a caller reads to decide what arrived,
    # so an absent, empty, or non-string one is malformed metadata rather than a blank field.
    raw_mimetype = info.get("mimetype")
    mimetype = safe_label(raw_mimetype, 120) if isinstance(raw_mimetype, str) else ""
    if not mimetype:
        raise SlackError("Slack file metadata has no usable MIME type.")
    size = info.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise SlackError("Slack file metadata has no usable byte size.")
    if size > max_bytes:
        raise SlackError(f"Slack file is {size} bytes, above the --max-bytes limit of {max_bytes}.")
    url = validate_download_url(str(info.get("url_private_download") or info.get("url_private") or ""))
    return Attachment(
        file_id=file_id,
        # The provider name is reported only; the destination path comes from --output, so a
        # hostile filename can never steer the write.
        name=safe_label(info.get("name") or info.get("title")),
        mimetype=mimetype,
        size=size,
        download_url=url,
    )


def check_destination(output: Path) -> None:
    if output.is_symlink() or output.exists():
        raise SlackError(f"Refusing to overwrite existing output path: {output}")
    if not output.parent.is_dir():
        raise SlackError(f"Destination directory does not exist: {output.parent}")


def response_length(response: Any) -> Optional[int]:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def remove_staged(staged: Path) -> bool:
    """Delete the staged file, reporting whether it is gone. Never raises, never reports a lie."""
    try:
        staged.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def save_attachment(profile: Profile, attachment: Attachment, output: Path, max_bytes: int) -> tuple[int, str]:
    """Stream one Slack-hosted file to `output`, returning its byte count and SHA-256 digest.

    The bytes are staged in a mode-0600 temporary file beside the destination and published
    with a link that fails rather than replaces, so a partial, oversized, or refused transfer
    never becomes the destination file. Staged-file removal is attempted on every path; a removal
    that itself fails is reported rather than suppressed, because private bytes outliving the
    command is a fact the caller has to act on.
    """
    request = urllib.request.Request(
        attachment.download_url,
        headers={"Authorization": f"Bearer {profile.token}", "User-Agent": "rundesk-slack-fetch/1"},
        method="GET",
    )
    opener = urllib.request.build_opener(SameOriginRedirectHandler())
    digest = hashlib.sha256()
    written = 0
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=str(output.parent))
    except OSError as exc:
        raise SlackError(
            f"Slack file could not be staged beside {output} ({type(exc).__name__})."
        ) from None
    staged = Path(name)
    try:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                try:
                    with opener.open(request, timeout=120) as response:
                        declared = response_length(response)
                        if declared is not None and declared > max_bytes:
                            raise SlackError(
                                f"Slack file download is {declared} bytes, above the --max-bytes "
                                f"limit of {max_bytes}."
                            )
                        while True:
                            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > max_bytes:
                                raise SlackError(
                                    f"Slack file download exceeded the --max-bytes limit of {max_bytes}."
                                )
                            digest.update(chunk)
                            handle.write(chunk)
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        retry = exc.headers.get("Retry-After", "the provider interval")
                        raise SlackError(
                            f"Slack rate-limited this download; retry after {retry} seconds."
                        ) from None
                    raise SlackError(f"Slack file download HTTP error {exc.code}.") from None
                # A body shorter than its Content-Length surfaces as http.client.IncompleteRead,
                # which is not an OSError, so a truncated transfer needs both families here.
                except (OSError, http.client.HTTPException) as exc:
                    raise SlackError(f"Slack file transfer failed ({type(exc).__name__}).") from None
                handle.flush()
                os.fsync(handle.fileno())
        # Opening, flushing, syncing, and closing the staged file each fail as OSError after
        # the transfer itself succeeded. None of them may escape as a traceback; the refusal
        # they become is cleaned up with every other refusal below.
        except OSError as exc:
            raise SlackError(
                f"Slack file could not be staged beside {output} ({type(exc).__name__})."
            ) from None
        if written != attachment.size:
            raise SlackError(
                f"Slack file download is incomplete: {written} of {attachment.size} bytes arrived."
            )
        try:
            os.link(staged, output)
        except FileExistsError:
            raise SlackError(f"Refusing to overwrite existing output path: {output}") from None
        except OSError as exc:
            raise SlackError(
                f"Slack file could not be written to {output} ({type(exc).__name__})."
            ) from None
    except SlackError as exc:
        # The refusal is the important fact, but private bytes outliving it is also one, so
        # the two are reported together rather than either being dropped.
        if not remove_staged(staged):
            raise SlackError(f"{exc} {STAGED_RESIDUE_NOTE}", code=exc.code) from None
        raise
    except BaseException:
        # Preserve the original interruption or defect, but make any private residue visible.
        if not remove_staged(staged):
            print(f"WARNING: {STAGED_RESIDUE_NOTE}", file=sys.stderr)
        raise
    if not remove_staged(staged):
        # The destination is already published and is never deleted to tidy up, because a
        # racing writer may own it by now. Say what is true and exit non-zero.
        raise SlackError(
            f"Slack attachment was written to {output}, but its temporary copy could not be "
            "removed from the destination directory; remove it manually."
        )
    return written, digest.hexdigest()


def print_profiles(as_json: bool) -> None:
    names = configured_profile_names()
    rows = [{"profile": name, "configured": bool(profile_value(name, "SLACK_FETCH_TOKEN"))} for name in names]
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No Slack profiles configured.")
        return
    for row in rows:
        print(f"{row['profile']} | configured={'yes' if row['configured'] else 'no'}")


def print_status(profile: Profile, as_json: bool) -> None:
    payload = api_call(profile, "auth.test", {})
    result = {
        "profile": profile.name,
        "label": profile.label,
        "configured": True,
        "authenticated": True,
        "team_id": str(payload.get("team_id") or ""),
        "user_id": str(payload.get("user_id") or ""),
        "types": list(profile.conversation_types),
        "channel_filter": list(profile.channels),
    }
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"Slack status | profile={profile.name} configured=yes authenticated=yes "
            f"team_id={result['team_id'] or '-'} user_id={result['user_id'] or '-'}"
        )


def print_channels(profile: Profile, args: argparse.Namespace) -> None:
    conversations, has_more = list_conversations(profile, args.limit)
    if has_more:
        print(
            f"WARNING: Slack channels were truncated at --limit {args.limit}; more results are available.",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps({
            "profile": profile.name,
            "count": len(conversations),
            "limit": args.limit,
            "has_more": has_more,
            "channels": conversations,
        }, indent=2))
        return
    print(
        f"Slack channels | profile={profile.name} count={len(conversations)} "
        f"limit={args.limit} more={'yes' if has_more else 'no'}"
    )
    for index, item in enumerate(conversations, 1):
        channel_id = str(item.get("id") or "-")
        name = str(item.get("name") or item.get("user") or "-")
        print(f"[{index}] id={channel_id} type={conversation_type(item)} name={name}")


def print_messages(profile: Profile, args: argparse.Namespace) -> None:
    messages, has_more = message_history(
        profile,
        args.channel,
        args.limit,
        oldest=args.oldest or "",
        latest=args.latest or "",
    )
    if has_more:
        print(
            f"WARNING: Slack messages were truncated at --limit {args.limit}; more results are available.",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps({
            "profile": profile.name,
            "channel": args.channel,
            "count": len(messages),
            "limit": args.limit,
            "has_more": has_more,
            "messages": messages,
        }, indent=2))
        return
    print(
        f"Slack messages | profile={profile.name} channel={args.channel} "
        f"count={len(messages)} limit={args.limit} more={'yes' if has_more else 'no'}"
    )
    for index, item in enumerate(messages, 1):
        timestamp = str(item.get("ts") or "-")
        author = str(item.get("user") or item.get("username") or "-")
        print(f"[{index}] ts={timestamp} user={author}\n    {compact_text(item.get('text'), 1000)}")


def print_search(profile: Profile, args: argparse.Namespace) -> None:
    results, has_more = search_messages(profile, args.query, args.limit)
    if has_more:
        print(
            f"WARNING: Slack search results were truncated at --limit {args.limit}; more results are available.",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps({
            "profile": profile.name,
            "count": len(results),
            "limit": args.limit,
            "has_more": has_more,
            "matches": results,
        }, indent=2))
        return
    print(
        f"Slack search | profile={profile.name} matches={len(results)} "
        f"limit={args.limit} more={'yes' if has_more else 'no'}"
    )
    for index, raw in enumerate(results, 1):
        item = normalize_match(raw)
        channel = item["channel_name"] or item["channel_id"] or "unknown"
        print(
            f"[{index}] channel={channel} ts={item['ts'] or '-'} thread_ts={item['thread_ts'] or '-'} "
            f"user={item['user'] or '-'}\n"
            f"    {compact_text(item['text'])}\n"
            f"    {item['permalink']}"
        )


def print_thread(profile: Profile, args: argparse.Namespace) -> None:
    if args.permalink:
        if args.channel or args.ts:
            raise SlackError("Use either --permalink or both --channel and --ts, not both forms.")
        channel, ts = parse_permalink(args.permalink)
    else:
        if not args.channel or not args.ts:
            raise SlackError("Thread requires --permalink or both --channel and --ts.")
        channel, ts = args.channel, args.ts
    messages = thread_messages(profile, channel, ts, args.max_messages)
    if args.json:
        print(json.dumps({
            "profile": profile.name,
            "channel": channel,
            "thread_ts": ts,
            "complete": True,
            "message_count": len(messages),
            "messages": messages,
        }, indent=2))
        return
    print(
        f"Slack thread | profile={profile.name} channel={channel} thread_ts={ts} "
        f"messages={len(messages)} complete=yes"
    )
    for index, item in enumerate(messages, 1):
        timestamp = str(item.get("ts") or "-")
        author = str(item.get("user") or item.get("username") or "-")
        print(f"[{index}] ts={timestamp} user={author}\n    {compact_text(item.get('text'), 2000)}")


def print_attachment(profile: Profile, args: argparse.Namespace) -> None:
    output = Path(args.output).expanduser()
    check_destination(output)
    attachment = resolve_attachment(profile, args.id, args.max_bytes)
    record = {
        "profile": profile.name,
        "file_id": attachment.file_id,
        "name": attachment.name,
        "mimetype": attachment.mimetype,
        "declared_bytes": attachment.size,
        "filename": output.name,
        "output": str(output),
    }
    if not args.confirm:
        record["downloaded"] = False
        if args.json:
            print(json.dumps(record, indent=2))
            return
        print(
            "DRY-RUN Slack attachment download | "
            + " | ".join([
                f"profile={profile.name}",
                f"id={attachment.file_id}",
                f"name={attachment.name or '-'}",
                f"mime={attachment.mimetype}",
                f"size={attachment.size}",
                f"filename={output.name}",
                f"output={output}",
                "confirm=pass --confirm to write the file",
            ])
        )
        return
    written, digest = save_attachment(profile, attachment, output, args.max_bytes)
    record.update({"downloaded": True, "bytes": written, "sha256": digest})
    if args.json:
        print(json.dumps(record, indent=2))
        return
    print(
        "Slack attachment downloaded | "
        + " | ".join([
            f"profile={profile.name}",
            f"id={attachment.file_id}",
            f"name={attachment.name or '-'}",
            f"mime={attachment.mimetype}",
            f"bytes={written}",
            f"sha256={digest}",
            f"filename={output.name}",
            f"output={output}",
        ])
    )


def byte_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--max-bytes must be a whole number of bytes.") from None
    if not 1 <= parsed <= MAX_ATTACHMENT_BYTES:
        raise argparse.ArgumentTypeError(
            f"--max-bytes must be between 1 and {MAX_ATTACHMENT_BYTES}."
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Slack message search, thread, and hosted-attachment retrieval."
    )
    parser.add_argument("--env-file", help="explicit owner-only dotenv path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="list configured Slack accounts without network access")
    profiles.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="verify token metadata with Slack auth.test")
    status.add_argument("--profile")
    status.add_argument("--json", action="store_true")

    channels = subparsers.add_parser("channels", help="list accessible channels and direct messages")
    channels.add_argument("--profile")
    channels.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="1..200")
    channels.add_argument("--json", action="store_true")

    messages = subparsers.add_parser("messages", help="list recent messages in one channel or DM")
    messages.add_argument("--profile")
    messages.add_argument("--channel", required=True)
    messages.add_argument("--limit", type=int, default=20, choices=range(1, 201), metavar="1..200")
    messages.add_argument("--oldest")
    messages.add_argument("--latest")
    messages.add_argument("--json", action="store_true")

    search = subparsers.add_parser("search", help="search messages with Slack search syntax")
    search.add_argument("--profile")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10, choices=range(1, 101), metavar="1..100")
    search.add_argument("--json", action="store_true")

    thread = subparsers.add_parser("thread", help="retrieve every available message in one thread")
    thread.add_argument("--profile")
    thread.add_argument("--permalink")
    thread.add_argument("--channel")
    thread.add_argument("--ts")
    thread.add_argument("--max-messages", type=int, default=1000, choices=range(1, 5001), metavar="1..5000")
    thread.add_argument("--json", action="store_true")

    attachment = subparsers.add_parser(
        "attachment",
        help="download one Slack-hosted file to an explicit local path; preview unless --confirm",
    )
    # Retrieval writes a local file with one account's credential, so the account is never
    # inferred here even when exactly one is configured.
    attachment.add_argument(
        "--profile", required=True, help="configured Slack account to retrieve the file with"
    )
    attachment.add_argument("--id", required=True, help="exact Slack file ID, beginning with F")
    attachment.add_argument(
        "--output", required=True, help="local file path to write; an existing path is never overwritten"
    )
    attachment.add_argument(
        "--max-bytes",
        type=byte_limit,
        default=DEFAULT_MAX_ATTACHMENT_BYTES,
        metavar=f"1..{MAX_ATTACHMENT_BYTES}",
    )
    attachment.add_argument(
        "--confirm", action="store_true", help="write the file after reviewing the preview"
    )
    attachment.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(resolve_env_file(args.env_file))
    try:
        if args.command == "profiles":
            print_profiles(args.json)
            return 0
        profile = get_profile(selected_profile_name(args))
        if args.command == "status":
            print_status(profile, args.json)
        elif args.command == "channels":
            print_channels(profile, args)
        elif args.command == "messages":
            print_messages(profile, args)
        elif args.command == "search":
            print_search(profile, args)
        elif args.command == "thread":
            print_thread(profile, args)
        elif args.command == "attachment":
            print_attachment(profile, args)
        return 0
    except SlackError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
