#!/usr/bin/env python3
"""Offline tests for the read-only Slack integration."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent / "slack-fetch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("slack_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SlackModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile("example", "synthetic-token", "Example")

    def test_search_is_bounded_and_reply_points_to_parent_thread(self) -> None:
        calls = []

        def fake_call(profile, method, params):
            calls.append((method, params))
            return {
                "ok": True,
                "messages": {
                    "matches": [
                        {
                            "channel": {"id": "C00000000", "name": "example"},
                            "ts": "1700000001.000001",
                            "thread_ts": "1700000000.000000",
                            "user": "U00000000",
                            "text": "Synthetic result one",
                            "permalink": "https://example.slack.com/archives/C00000000/p1700000001000001",
                        },
                        {
                            "channel": {"id": "C00000000", "name": "example"},
                            "ts": "1700000002.000002",
                            "user": "U00000001",
                            "text": "Synthetic result two",
                            "permalink": "https://example.slack.com/archives/C00000000/p1700000002000002",
                        },
                    ]
                },
            }

        with patch.object(self.module, "api_call", side_effect=fake_call):
            results = self.module.search_messages(self.profile, "synthetic in:#example", 1)

        self.assertEqual(1, len(results))
        self.assertEqual("1700000000.000000", results[0]["thread_ts"])
        self.assertEqual("search.messages", calls[0][0])
        self.assertEqual(1, calls[0][1]["count"])

    def test_exact_legacy_profile_shape_is_discovered_and_parsed(self) -> None:
        env = {
            "SLACK_TUCKMGMTINC_LABEL": "Tuck Mgmt Inc",
            "SLACK_TUCKMGMTINC_TOKEN": "synthetic-token",
            "SLACK_TUCKMGMTINC_TYPES": "public_channel,private_channel,mpim,im",
            "SLACK_TUCKMGMTINC_CHANNELS": "",
        }

        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["tuckmgmtinc"], self.module.configured_profile_names())
            profile = self.module.get_profile("tuckmgmtinc")

        self.assertEqual("Tuck Mgmt Inc", profile.label)
        self.assertEqual("synthetic-token", profile.token)
        self.assertEqual(
            {"public_channel", "private_channel", "mpim", "im"},
            set(profile.conversation_types),
        )
        self.assertEqual((), profile.channels)

    def test_profile_refuses_unknown_types_and_invalid_channel_ids(self) -> None:
        cases = (
            {"SLACK_EXAMPLE_TOKEN": "synthetic-token", "SLACK_EXAMPLE_TYPES": "public_channel,all"},
            {"SLACK_EXAMPLE_TOKEN": "synthetic-token", "SLACK_EXAMPLE_CHANNELS": "general"},
        )
        for env in cases:
            with self.subTest(env=sorted(env)), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(self.module.SlackError):
                    self.module.get_profile("example")

    def test_channels_include_public_private_mpim_and_dm_with_full_json(self) -> None:
        page = {
            "ok": True,
            "channels": [
                {"id": "C00000000", "name": "general", "topic": {"value": "Full detail"}},
                {"id": "G00000000", "name": "private", "is_private": True},
                {"id": "G00000001", "name": "group-dm", "is_mpim": True},
                {"id": "D00000000", "user": "U00000000", "is_im": True},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        with patch.object(self.module, "api_call", return_value=page) as call:
            channels, has_more = self.module.list_conversations(self.profile, 10)

        self.assertEqual(4, len(channels))
        self.assertFalse(has_more)
        self.assertEqual(
            "public_channel,private_channel,mpim,im",
            call.call_args.args[2]["types"],
        )
        output = io.StringIO()
        args = self.module.SimpleNamespace(limit=10, json=True)
        with patch.object(self.module, "list_conversations", return_value=(channels, False)), \
                redirect_stdout(output):
            self.module.print_channels(self.profile, args)
        payload = self.module.json.loads(output.getvalue())
        self.assertEqual("Full detail", payload["channels"][0]["topic"]["value"])

    def test_message_history_is_bounded_and_outputs_timestamps(self) -> None:
        page = {
            "ok": True,
            "messages": [
                {
                    "ts": "1700000000.000000",
                    "user": "U00000000",
                    "text": "First message",
                    "blocks": [{"type": "section"}],
                },
                {"ts": "1700000001.000001", "user": "U00000001", "text": "Second message"},
            ],
            "has_more": True,
            "response_metadata": {"next_cursor": "next"},
        }
        with patch.object(self.module, "api_call", return_value=page) as call:
            messages, has_more = self.module.message_history(
                self.profile, "C00000000", 2
            )

        self.assertEqual(2, len(messages))
        self.assertTrue(has_more)
        self.assertEqual("conversations.history", call.call_args.args[1])
        self.assertEqual(2, call.call_args.args[2]["limit"])

        text_output = io.StringIO()
        json_output = io.StringIO()
        args = self.module.SimpleNamespace(
            channel="C00000000", limit=2, oldest=None, latest=None, json=False
        )
        with patch.object(self.module, "message_history", return_value=(messages, True)), \
                redirect_stdout(text_output):
            self.module.print_messages(self.profile, args)
        args.json = True
        with patch.object(self.module, "message_history", return_value=(messages, True)), \
                redirect_stdout(json_output):
            self.module.print_messages(self.profile, args)

        self.assertIn("ts=1700000000.000000", text_output.getvalue())
        self.assertIn("First message", text_output.getvalue())
        payload = self.module.json.loads(json_output.getvalue())
        self.assertEqual([{"type": "section"}], payload["messages"][0]["blocks"])

    def test_channel_allowlist_filters_listing_and_refuses_history(self) -> None:
        profile = self.module.Profile(
            "example",
            "synthetic-token",
            "Example",
            channels=("C00000000",),
        )
        page = {
            "ok": True,
            "channels": [
                {"id": "C00000000", "name": "allowed"},
                {"id": "C00000001", "name": "blocked"},
            ],
            "response_metadata": {"next_cursor": ""},
        }
        with patch.object(self.module, "api_call", return_value=page):
            channels, _ = self.module.list_conversations(profile, 10)
        self.assertEqual(["C00000000"], [item["id"] for item in channels])
        with self.assertRaises(self.module.SlackError) as raised:
            self.module.message_history(profile, "C00000001", 10)
        self.assertIn("allowlist", str(raised.exception))

    def test_search_encodes_query_without_putting_it_in_headers(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true, "messages": {"matches": []}}'

        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["auth"] = request.get_header("Authorization")
                captured["method"] = request.get_method()
                captured["timeout"] = timeout
                return Response()

        with patch.object(self.module.urllib.request, "build_opener", return_value=Opener()):
            self.module.api_call(self.profile, "search.messages", {"query": "alpha beta", "count": 3})

        parsed = urllib.parse.urlsplit(captured["url"])
        self.assertEqual(["alpha beta"], urllib.parse.parse_qs(parsed.query)["query"])
        self.assertEqual("Bearer synthetic-token", captured["auth"])
        self.assertEqual("GET", captured["method"])
        self.assertEqual(30, captured["timeout"])
        self.assertEqual("slack.com", parsed.hostname)
        self.assertNotIn("synthetic-token", captured["url"])

    def test_cross_origin_redirect_is_refused_before_forwarding_authorization(self) -> None:
        request = self.module.urllib.request.Request(
            "https://slack.com/api/auth.test",
            headers={"Authorization": "Bearer synthetic-token"},
        )
        handler = self.module.SameOriginRedirectHandler()

        with self.assertRaises(self.module.SlackError) as raised:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.test/collect",
            )

        self.assertIn("cross-origin", str(raised.exception))

    def test_rate_limit_reports_only_the_retry_interval(self) -> None:
        error = self.module.urllib.error.HTTPError(
            "https://slack.com/api/auth.test",
            429,
            "Too Many Requests",
            {"Retry-After": "7"},
            None,
        )

        with patch.object(
            self.module.urllib.request,
            "build_opener",
        ) as build_opener:
            build_opener.return_value.open.side_effect = error
            with self.assertRaises(self.module.SlackError) as raised:
                self.module.api_call(self.profile, "auth.test", {})

        message = str(raised.exception)
        self.assertIn("retry after 7 seconds", message)
        self.assertNotIn("synthetic-token", message)

    def test_thread_follows_every_cursor_and_reports_complete(self) -> None:
        pages = [
            {"ok": True, "messages": [{"ts": "1700000000.000000", "text": "Root"}],
             "response_metadata": {"next_cursor": "next"}},
            {"ok": True, "messages": [{"ts": "1700000001.000001", "text": "Reply"}],
             "response_metadata": {"next_cursor": ""}},
        ]
        with patch.object(self.module, "api_call", side_effect=pages) as call:
            messages = self.module.thread_messages(
                self.profile, "C00000000", "1700000000.000000", 100
            )

        self.assertEqual(2, len(messages))
        self.assertEqual("next", call.call_args_list[1].args[2]["cursor"])

    def test_thread_refuses_to_call_a_capped_partial_result_complete(self) -> None:
        page = {
            "ok": True,
            "messages": [{"ts": "1700000000.000000", "text": "Root"}],
            "response_metadata": {"next_cursor": "next"},
        }
        with patch.object(self.module, "api_call", return_value=page):
            with self.assertRaises(self.module.SlackError) as raised:
                self.module.thread_messages(
                    self.profile, "C00000000", "1700000000.000000", 1
                )
        self.assertIn("incomplete", str(raised.exception))

    def test_thread_refuses_has_more_without_a_cursor(self) -> None:
        page = {
            "ok": True,
            "messages": [{"ts": "1700000000.000000", "text": "Root"}],
            "has_more": True,
            "response_metadata": {"next_cursor": ""},
        }
        with patch.object(self.module, "api_call", return_value=page):
            with self.assertRaises(self.module.SlackError) as raised:
                self.module.thread_messages(
                    self.profile, "C00000000", "1700000000.000000", 100
                )
        self.assertIn("incomplete", str(raised.exception))

    def test_permalink_parser_accepts_only_slack_message_urls(self) -> None:
        channel, ts = self.module.parse_permalink(
            "https://example.slack.com/archives/C00000000/p1700000000000000"
        )
        self.assertEqual("C00000000", channel)
        self.assertEqual("1700000000.000000", ts)
        for value in (
            "http://example.slack.com/archives/C00000000/p1700000000000000",
            "https://example.test/archives/C00000000/p1700000000000000",
            "https://example.slack.com/not-a-message",
        ):
            with self.subTest(value=value), self.assertRaises(self.module.SlackError):
                self.module.parse_permalink(value)

    def test_only_read_methods_are_allowed(self) -> None:
        self.assertEqual(
            {
                "auth.test",
                "conversations.history",
                "conversations.list",
                "conversations.replies",
                "search.messages",
            },
            self.module.ALLOWED_METHODS,
        )
        with self.assertRaises(self.module.SlackError):
            self.module.api_call(self.profile, "chat.postMessage", {})

    def test_provider_error_never_echoes_token_or_response_content(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": false, "error": "missing_scope", "detail": "private body"}'

        class Opener:
            def open(self, request, timeout):
                return Response()

        with patch.object(self.module.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(self.module.SlackError) as raised:
                self.module.api_call(self.profile, "auth.test", {})

        message = str(raised.exception)
        self.assertIn("missing_scope", message)
        self.assertNotIn("synthetic-token", message)
        self.assertNotIn("private body", message)

    def test_profiles_is_offline_and_reads_isolated_dotenv(self) -> None:
        with self.subTest("legacy profile spelling"):
            env = {
                "SLACK_PROFILES": "example",
                "SLACK_EXAMPLE_TOKEN": "synthetic-token",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(["example"], self.module.configured_profile_names())
                self.assertEqual("synthetic-token", self.module.get_profile("example").token)

    def test_cli_error_output_does_not_echo_configured_token(self) -> None:
        error = io.StringIO()
        env = {"SLACK_FETCH_TOKEN": "synthetic-token"}
        with patch.dict(os.environ, env, clear=True), redirect_stderr(error):
            code = self.module.main(["thread", "--channel", "invalid", "--ts", "invalid"])
        self.assertEqual(1, code)
        self.assertNotIn("synthetic-token", error.getvalue())

    def test_help_and_profiles_need_no_live_credential(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            self.assertEqual(0, self.module.main(["profiles"]))
        self.assertIn("No Slack profiles", output.getvalue())


if __name__ == "__main__":
    unittest.main()
