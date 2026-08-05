#!/usr/bin/env python3
"""Offline tests for discord.d/discord.py."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parent
SCRIPT = MODULE_DIR / "discord.py"


def load_module():
    spec = importlib.util.spec_from_file_location("discord_integration", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHANNEL = "123456789012345678"
MESSAGE = "223456789012345678"
USER = "323456789012345678"
GUILD = "423456789012345678"


def sending(**overrides) -> SimpleNamespace:
    args = dict(
        profile="example", json=False, confirm=False, text="hello", text_file=None,
        file=[], split=False, channel=CHANNEL, message=MESSAGE, user=USER,
    )
    args.update(overrides)
    return SimpleNamespace(**args)


class DiscordModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(name="example", token="secret-token", label="Example")

    def run_command(self, func, args) -> tuple[int, str]:
        buf = io.StringIO()
        with patch.object(self.module, "get_profile", return_value=self.profile):
            with redirect_stdout(buf):
                code = func(args)
        return code, buf.getvalue()

    def test_get_profile_maps_env(self) -> None:
        env = {
            "DISCORD_PROFILES": "example",
            "DISCORD_DEFAULT_PROFILE": "example",
            "DISCORD_EXAMPLE_LABEL": "Example bot",
            "DISCORD_EXAMPLE_TOKEN": "tok",
            "DISCORD_EXAMPLE_ALLOW_CHANNELS": f"{CHANNEL}, 999888777666555444",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["example"], self.module.configured_profile_names())
            profile = self.module.get_profile("example")
        self.assertEqual("tok", profile.token)
        self.assertEqual("Example bot", profile.label)
        self.assertEqual((CHANNEL, "999888777666555444"), profile.allow_channels)
        self.assertTrue(profile.bounded)

    def test_bare_bot_token_makes_a_default_profile(self) -> None:
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "tok"}, clear=True):
            self.assertEqual(["default"], self.module.configured_profile_names())
            self.assertEqual("tok", self.module.get_profile("default").token)

    def test_missing_token_names_the_variable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.module.DiscordError) as raised:
                self.module.get_profile("example")
        message = str(raised.exception)
        self.assertIn("DISCORD_BOT_TOKEN__EXAMPLE", message)
        self.assertIn("rundesk skills configure", message)

    def test_rundesk_suffix_wins_over_the_legacy_form(self) -> None:
        env = {
            "DISCORD_BOT_TOKEN__EXAMPLE": "rundesk-tok",
            "DISCORD_EXAMPLE_TOKEN": "legacy-tok",
            "DISCORD_LABEL__EXAMPLE": "Rundesk bot",
            "DISCORD_EXAMPLE_LABEL": "Legacy bot",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["example"], self.module.configured_profile_names())
            profile = self.module.get_profile("example")
        self.assertEqual("rundesk-tok", profile.token)
        self.assertEqual("Rundesk bot", profile.label)

    def test_legacy_profile_form_still_resolves(self) -> None:
        env = {
            "DISCORD_EXAMPLE_TOKEN": "legacy-tok",
            "DISCORD_EXAMPLE_ALLOW_GUILDS": GUILD,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["example"], self.module.configured_profile_names())
            profile = self.module.get_profile("example")
        self.assertEqual("legacy-tok", profile.token)
        self.assertEqual((GUILD,), profile.allow_guilds)

    def test_a_named_account_does_not_read_the_plain_token(self) -> None:
        env = {"DISCORD_BOT_TOKEN": "default-tok", "DISCORD_TOKEN": "gateway-tok"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(self.module.DiscordError) as raised:
                self.module.get_profile("example")
            self.assertEqual("default-tok", self.module.get_profile("default").token)
        self.assertIn("DISCORD_BOT_TOKEN__EXAMPLE", str(raised.exception))

    def test_the_plain_token_alias_configures_the_default_account(self) -> None:
        with patch.dict(os.environ, {"DISCORD_TOKEN": "gateway-tok"}, clear=True):
            self.assertEqual(["default"], self.module.configured_profile_names())
            self.assertEqual("gateway-tok", self.module.get_profile("default").token)

    def test_plain_names_alone_give_one_default_account(self) -> None:
        env = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_LABEL": "House bot"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["default"], self.module.configured_profile_names())
            self.assertEqual("House bot", self.module.get_profile("default").label)

    def test_a_named_default_account_reads_the_plain_names(self) -> None:
        env = {"DISCORD_DEFAULT_PROFILE": "example", "DISCORD_BOT_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["example"], self.module.configured_profile_names())
            self.assertEqual("tok", self.module.get_profile("example").token)

    def test_a_named_account_inherits_the_plain_allow_lists(self) -> None:
        """A guardrail must never narrow: adding an account cannot unbound it."""
        env = {
            "DISCORD_BOT_TOKEN__EXAMPLE": "tok",
            "DISCORD_ALLOW_GUILDS": GUILD,
            "DISCORD_ALLOW_CHANNELS": CHANNEL,
            "DISCORD_ALLOW_USERS": USER,
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("example")
        self.assertEqual((GUILD,), profile.allow_guilds)
        self.assertEqual((CHANNEL,), profile.allow_channels)
        self.assertEqual((USER,), profile.allow_users)
        self.assertTrue(profile.bounded)

    def test_a_named_allow_list_overrides_the_plain_one(self) -> None:
        env = {
            "DISCORD_BOT_TOKEN__EXAMPLE": "tok",
            "DISCORD_ALLOW_CHANNELS": "999888777666555444",
            "DISCORD_ALLOW_CHANNELS__EXAMPLE": CHANNEL,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual((CHANNEL,), self.module.get_profile("example").allow_channels)

    def test_authorization_header_is_a_bot_credential(self) -> None:
        self.assertEqual("Bot secret-token", self.profile.auth_headers()["Authorization"])

    def test_snowflake_rejects_a_channel_name(self) -> None:
        with self.assertRaises(self.module.DiscordError):
            self.module.snowflake("#rundesk-cli", "CHANNEL_ID")
        self.assertEqual(CHANNEL, self.module.snowflake(f"<#{CHANNEL}>", "CHANNEL_ID"))

    def test_send_is_dry_run_without_confirm(self) -> None:
        with patch.object(self.module, "request") as called:
            code, out = self.run_command(self.module.cmd_send, sending())
        self.assertEqual(0, code)
        self.assertIn("mode\tdry-run", out)
        self.assertIn("characters\t5", out)
        called.assert_not_called()

    def test_send_confirm_posts_to_the_channel(self) -> None:
        with patch.object(self.module, "request", return_value={"id": "9"}) as called:
            code, out = self.run_command(self.module.cmd_send, sending(confirm=True))
        self.assertEqual(0, code)
        self.assertIn("mode\tconfirmed", out)
        self.assertIn("message_id\t9", out)
        self.assertEqual("POST", called.call_args.args[1])
        self.assertEqual(f"channels/{CHANNEL}/messages", called.call_args.args[2])
        self.assertEqual({"content": "hello"}, called.call_args.kwargs["payload"])

    def test_over_long_message_is_refused_until_split(self) -> None:
        long = "x" * (self.module.MESSAGE_LIMIT + 10)
        with self.assertRaises(self.module.DiscordError) as raised:
            self.module.chunks(long, split=False)
        self.assertIn(str(len(long)), str(raised.exception))
        parts = self.module.chunks(long, split=True)
        self.assertEqual(2, len(parts))
        self.assertTrue(all(len(part) <= self.module.MESSAGE_LIMIT for part in parts))
        self.assertEqual(long, "".join(parts))

    def test_split_prefers_a_line_break(self) -> None:
        head = "a" * (self.module.MESSAGE_LIMIT - 5)
        parts = self.module.chunks(head + "\n" + "b" * 50, split=True)
        self.assertEqual(head, parts[0])
        self.assertEqual("b" * 50, parts[1])

    def test_allow_channels_refuses_an_unlisted_channel(self) -> None:
        bounded = self.module.Profile(
            name="example", token="tok", label="Example",
            allow_channels=("999888777666555444",),
        )
        with patch.object(self.module, "get_profile", return_value=bounded):
            with self.assertRaises(self.module.DiscordError) as raised:
                self.module.cmd_send(sending(confirm=True))
        self.assertIn("DISCORD_ALLOW_CHANNELS__EXAMPLE", str(raised.exception))

    def test_allow_guilds_checks_the_channel_it_was_given(self) -> None:
        bounded = self.module.Profile(
            name="example", token="tok", label="Example", allow_guilds=(GUILD,),
        )
        with patch.object(self.module, "request", return_value={"guild_id": "1" * 18}):
            with self.assertRaises(self.module.DiscordError) as raised:
                self.module.allow_channel(bounded, CHANNEL)
        self.assertIn("DISCORD_ALLOW_GUILDS__EXAMPLE", str(raised.exception))
        with patch.object(self.module, "request", return_value={"guild_id": GUILD}):
            self.module.allow_channel(bounded, CHANNEL)

    def test_a_bounded_profile_will_not_direct_message_without_allow_users(self) -> None:
        bounded = self.module.Profile(
            name="example", token="tok", label="Example", allow_guilds=(GUILD,),
        )
        with self.assertRaises(self.module.DiscordError):
            self.module.allow_user(bounded, USER)
        allowed = self.module.Profile(
            name="example", token="tok", label="Example", allow_users=(USER,),
        )
        self.module.allow_user(allowed, USER)

    def test_dm_opens_a_channel_before_posting(self) -> None:
        answers = [{"id": "555"}, {"id": "9"}]
        with patch.object(self.module, "request", side_effect=answers) as called:
            code, out = self.run_command(self.module.cmd_dm, sending(confirm=True))
        self.assertEqual(0, code)
        self.assertIn("message_id\t9", out)
        self.assertEqual("users/@me/channels", called.call_args_list[0].args[2])
        self.assertEqual("channels/555/messages", called.call_args_list[1].args[2])

    def test_reply_quotes_without_failing_on_a_deleted_parent(self) -> None:
        with patch.object(self.module, "request", return_value={"id": "9"}) as called:
            self.run_command(self.module.cmd_reply, sending(confirm=True))
        reference = called.call_args.kwargs["payload"]["message_reference"]
        self.assertEqual(MESSAGE, reference["message_id"])
        self.assertFalse(reference["fail_if_not_exists"])

    def test_history_reads_oldest_first_and_clips_content(self) -> None:
        payload = [
            {"id": "2", "timestamp": "2026-01-02T00:00:00+00:00", "content": "y" * 500,
             "author": {"username": "ada", "bot": False}, "attachments": []},
            {"id": "1", "timestamp": "2026-01-01T00:00:00+00:00", "content": "first",
             "author": {"username": "ada", "bot": False}, "attachments": []},
        ]
        args = SimpleNamespace(profile="example", json=False, channel=CHANNEL, limit=25,
                               before=None, after=None, full=False, newest_first=False)
        with patch.object(self.module, "request", return_value=payload):
            code, out = self.run_command(self.module.cmd_history, args)
        self.assertEqual(0, code)
        lines = out.strip().splitlines()
        self.assertTrue(lines[1].startswith("1,"))
        self.assertNotIn("y" * 400, out)
        self.assertIn("…", out)

    def test_history_shows_an_embed_when_there_is_no_text(self) -> None:
        row = self.module.message_row(
            {"id": "1", "content": "", "embeds": [{"title": "Deploy finished"}],
             "author": {"username": "ci"}, "attachments": []},
            full=True,
        )
        self.assertIn("Deploy finished", row["content"])

    def test_react_encodes_a_unicode_emoji_and_keeps_a_custom_one(self) -> None:
        args = sending(confirm=True, emoji="👀")
        with patch.object(self.module, "request", return_value=None) as called:
            self.run_command(self.module.cmd_react, args)
        self.assertIn("%F0%9F%91%80", called.call_args.args[2])
        with patch.object(self.module, "request", return_value=None) as called:
            self.run_command(self.module.cmd_react, sending(confirm=True, emoji="shipit:12345"))
        self.assertIn("shipit:12345", called.call_args.args[2])

    def test_delete_is_dry_run_without_confirm(self) -> None:
        with patch.object(self.module, "request") as called:
            code, out = self.run_command(self.module.cmd_delete, sending())
        self.assertEqual(0, code)
        self.assertIn("mode\tdry-run", out)
        called.assert_not_called()

    def test_thread_on_a_message_uses_the_message_route(self) -> None:
        args = sending(confirm=True, name="release v1")
        with patch.object(self.module, "request", return_value={"id": "77"}) as called:
            self.run_command(self.module.cmd_thread, args)
        self.assertEqual(f"channels/{CHANNEL}/messages/{MESSAGE}/threads", called.call_args.args[2])
        args = sending(confirm=True, name="release v1", message=None)
        with patch.object(self.module, "request", return_value={"id": "77"}) as called:
            self.run_command(self.module.cmd_thread, args)
        self.assertEqual(f"channels/{CHANNEL}/threads", called.call_args.args[2])
        self.assertEqual(11, called.call_args.kwargs["payload"]["type"])

    def test_missing_attachment_is_named(self) -> None:
        with self.assertRaises(self.module.DiscordError) as raised:
            self.module.attachments(SimpleNamespace(file=["/nowhere/absent.txt"]))
        self.assertIn("absent.txt", str(raised.exception))

    def test_multipart_carries_payload_and_file(self) -> None:
        with io.open(MODULE_DIR / "discord.py", "rb"):
            pass
        body, content_type = self.module.encode_multipart(
            {"content": "here"}, [MODULE_DIR / "discord.py"]
        )
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="payload_json"', body)
        self.assertIn(b'filename="discord.py"', body)

    def test_rate_limit_waits_once_then_reports(self) -> None:
        module = self.module

        class Refused(module.urllib.error.HTTPError):
            def __init__(self) -> None:
                super().__init__("https://discord.test", 429, "Too Many Requests", {}, None)

            def read(self):  # noqa: D401 - the error body Discord sends
                return b'{"message": "You are being rate limited.", "retry_after": 0.01}'

        with patch.object(module.urllib.request, "urlopen", side_effect=Refused()), \
                patch.object(module.time, "sleep") as slept:
            with self.assertRaises(module.DiscordError) as raised:
                module.request(self.profile, "GET", "users/@me")
        self.assertEqual(1, slept.call_count)
        self.assertIn("429", str(raised.exception))

    def test_status_never_prints_the_token(self) -> None:
        args = SimpleNamespace(profile="example", json=False)
        with patch.object(self.module, "request", side_effect=[{"username": "winston", "id": "5"},
                                                               [{"id": GUILD}]]):
            code, out = self.run_command(self.module.cmd_status, args)
        self.assertEqual(0, code)
        self.assertNotIn("secret-token", out)
        self.assertIn("bot\twinston", out)
        self.assertIn("writes\tanywhere the bot can post", out)

    def test_main_help_exits_clean(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.module.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("history", buf.getvalue())

    def test_history_limit_is_bounded(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = self.module.main(
                ["--env-file", str(MODULE_DIR / "no-such-env"), "history", CHANNEL,
                 "--limit", "500"]
            )
        self.assertEqual(1, code)
        self.assertIn("between 1 and", err.getvalue())


if __name__ == "__main__":
    unittest.main()
