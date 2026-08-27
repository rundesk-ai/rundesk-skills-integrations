#!/usr/bin/env python3
"""Offline tests for the read-only Slack integration."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
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
                        }
                    ],
                    "pagination": {"page": 1, "page_count": 2},
                },
            }

        with patch.object(self.module, "api_call", side_effect=fake_call):
            results, has_more = self.module.search_messages(self.profile, "synthetic in:#example", 1)

        self.assertEqual(1, len(results))
        self.assertTrue(has_more)
        self.assertEqual("1700000000.000000", results[0]["thread_ts"])
        self.assertEqual("search.messages", calls[0][0])
        self.assertEqual(1, calls[0][1]["count"])

    def test_exact_legacy_profile_shape_is_discovered_and_parsed(self) -> None:
        env = {
            "SLACK_EXAMPLECO_LABEL": "Example Workspace",
            "SLACK_EXAMPLECO_TOKEN": "synthetic-token",
            "SLACK_EXAMPLECO_TYPES": "public_channel,private_channel,mpim,im",
            "SLACK_EXAMPLECO_CHANNELS": "",
        }

        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["exampleco"], self.module.configured_profile_names())
            profile = self.module.get_profile("exampleco")

        self.assertEqual("Example Workspace", profile.label)
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

        warning = io.StringIO()
        with patch.object(self.module, "list_conversations", return_value=(channels[:1], True)), \
                redirect_stdout(io.StringIO()), redirect_stderr(warning):
            self.module.print_channels(
                self.profile,
                self.module.SimpleNamespace(limit=1, json=False),
            )
        self.assertIn("truncated", warning.getvalue())

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
        text_error = io.StringIO()
        json_error = io.StringIO()
        args = self.module.SimpleNamespace(
            channel="C00000000", limit=2, oldest=None, latest=None, json=False
        )
        with patch.object(self.module, "message_history", return_value=(messages, True)), \
                redirect_stdout(text_output), redirect_stderr(text_error):
            self.module.print_messages(self.profile, args)
        args.json = True
        with patch.object(self.module, "message_history", return_value=(messages, True)), \
                redirect_stdout(json_output), redirect_stderr(json_error):
            self.module.print_messages(self.profile, args)

        self.assertIn("ts=1700000000.000000", text_output.getvalue())
        self.assertIn("First message", text_output.getvalue())
        self.assertIn("truncated", text_error.getvalue())
        self.assertIn("truncated", json_error.getvalue())
        payload = self.module.json.loads(json_output.getvalue())
        self.assertTrue(payload["has_more"])
        self.assertEqual([{"type": "section"}], payload["messages"][0]["blocks"])

    def test_search_discloses_truncation_in_text_and_json(self) -> None:
        results = [{
            "channel": {"id": "C00000000", "name": "example"},
            "ts": "1700000001.000001",
            "user": "U00000000",
            "text": "Synthetic result",
            "permalink": "https://example.slack.com/archives/C00000000/p1700000001000001",
            "metadata": {"preserved": True},
        }]
        args = self.module.SimpleNamespace(query="synthetic", limit=1, json=False)
        text_output = io.StringIO()
        text_error = io.StringIO()
        with patch.object(self.module, "search_messages", return_value=(results, True)), \
                redirect_stdout(text_output), redirect_stderr(text_error):
            self.module.print_search(self.profile, args)

        self.assertIn("more=yes", text_output.getvalue())
        self.assertIn("truncated", text_error.getvalue())

        args.json = True
        json_output = io.StringIO()
        json_error = io.StringIO()
        with patch.object(self.module, "search_messages", return_value=(results, True)), \
                redirect_stdout(json_output), redirect_stderr(json_error):
            self.module.print_search(self.profile, args)

        payload = self.module.json.loads(json_output.getvalue())
        self.assertTrue(payload["has_more"])
        self.assertTrue(payload["matches"][0]["metadata"]["preserved"])
        self.assertIn("truncated", json_error.getvalue())

    def test_channel_setting_filters_discovery_but_not_direct_reads(self) -> None:
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
        history = {
            "ok": True,
            "messages": [{"ts": "1700000000.000000", "text": "Direct read"}],
            "response_metadata": {"next_cursor": ""},
        }
        with patch.object(self.module, "api_call", return_value=history) as call:
            messages, _ = self.module.message_history(profile, "C00000001", 10)
        self.assertEqual("Direct read", messages[0]["text"])
        self.assertEqual("conversations.history", call.call_args.args[1])

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
                "files.info",
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


class ForbiddenOpener:
    """Any HTTP boundary the test did not stage is a defect, not a network call."""

    def open(self, request, timeout):  # noqa: A003 - urllib opener protocol
        raise AssertionError(f"unexpected HTTP request to {request.full_url}")


class StagedResponse:
    def __init__(self, body: bytes, headers=None) -> None:
        self.body = body
        self.headers = dict(headers or {})
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class RecordingOpener:
    """One staged download response, keeping the request so headers can be inspected."""

    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):  # noqa: A003 - urllib opener protocol
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


class SlackAttachmentTest(unittest.TestCase):
    FILE_ID = "F00000000"
    BODY = b"synthetic attachment bytes\n" * 4
    URL = "https://files.slack.com/files-pri/T00000000-F00000000/download/example.docx"

    def setUp(self) -> None:
        self.module = load_module()
        temporary = tempfile.TemporaryDirectory(prefix="slack-fetch-attachment-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination_dir = self.root / "downloads"
        self.destination_dir.mkdir()
        self.output = self.destination_dir / "example.docx"

    def file_payload(self, **overrides) -> dict:
        info = {
            "id": self.FILE_ID,
            "name": "example.docx",
            "title": "Example",
            "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "mode": "hosted",
            "file_access": "visible",
            "size": len(self.BODY),
            "url_private_download": self.URL,
        }
        info.update(overrides)
        return {"ok": True, "file": {key: value for key, value in info.items() if value is not None}}

    def environment(self) -> dict:
        return {
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            # The account-suffixed spelling: `attachment` requires an explicit profile, and a
            # named account never falls back to a plain default-account token.
            "SLACK_FETCH_TOKEN__EXAMPLE": "synthetic-token",
        }

    def run_cli(self, argv, api=None, opener=None):
        """Run the command with every HTTP boundary staged, so nothing can reach a network."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.environment(), clear=True))
            stack.enter_context(patch.object(
                self.module.urllib.request,
                "build_opener",
                return_value=opener if opener is not None else ForbiddenOpener(),
            ))
            if api is not None:
                stack.enter_context(patch.object(self.module, "api_call", side_effect=api))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            code = self.module.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def metadata_only(self, payload=None, calls=None):
        """An `api_call` stand-in that answers files.info and records what was asked."""
        answer = self.file_payload() if payload is None else payload

        def call(profile, method, params):
            if calls is not None:
                calls.append((method, params))
            if method != "files.info":
                raise AssertionError(f"unexpected method {method}")
            return answer

        return call

    def leftovers(self) -> list:
        return sorted(path.name for path in self.destination_dir.iterdir())

    def assertNoSecrets(self, *texts: str) -> None:
        for text in texts:
            self.assertNotIn("synthetic-token", text)
            self.assertNotIn("Bearer", text)
            self.assertNotIn("files-pri", text)
            self.assertNotIn(self.URL, text)

    def test_preview_reports_metadata_and_writes_nothing(self) -> None:
        calls = []
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
            api=self.metadata_only(calls=calls),
        )

        self.assertEqual(0, code)
        self.assertEqual([("files.info", {"file": self.FILE_ID})], calls)
        self.assertIn("DRY-RUN Slack attachment download", out)
        self.assertIn(f"id={self.FILE_ID}", out)
        self.assertIn("name=example.docx", out)
        self.assertIn("mime=application/vnd.openxmlformats", out)
        self.assertIn(f"size={len(self.BODY)}", out)
        self.assertIn("confirm=pass --confirm to write the file", out)
        self.assertFalse(self.output.exists())
        self.assertEqual([], self.leftovers())
        self.assertNoSecrets(out, err)

    def test_confirm_writes_the_exact_bytes_and_reports_a_compact_record(self) -> None:
        opener = RecordingOpener(StagedResponse(self.BODY, {"Content-Length": str(len(self.BODY))}))
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm"],
            api=self.metadata_only(),
            opener=opener,
        )

        digest = hashlib.sha256(self.BODY).hexdigest()
        self.assertEqual(0, code)
        self.assertEqual(self.BODY, self.output.read_bytes())
        self.assertEqual(["example.docx"], self.leftovers())
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        self.assertIn("Slack attachment downloaded", out)
        self.assertIn(f"bytes={len(self.BODY)}", out)
        self.assertIn(f"sha256={digest}", out)
        self.assertIn(f"output={self.output}", out)
        self.assertNoSecrets(out, err)

        request = opener.requests[0]
        self.assertEqual("files.slack.com", urllib.parse.urlsplit(request.full_url).hostname)
        self.assertEqual("Bearer synthetic-token", request.get_header("Authorization"))
        self.assertEqual("GET", request.get_method())
        self.assertEqual([120], opener.timeouts)

    def test_confirm_json_record_carries_only_non_secret_metadata(self) -> None:
        opener = RecordingOpener(StagedResponse(self.BODY))
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm", "--json"],
            api=self.metadata_only(),
            opener=opener,
        )

        payload = self.module.json.loads(out)
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "profile", "file_id", "name", "mimetype", "declared_bytes",
                "filename", "output", "downloaded", "bytes", "sha256",
            },
            set(payload),
        )
        self.assertEqual(self.FILE_ID, payload["file_id"])
        self.assertEqual("example.docx", payload["filename"])
        self.assertEqual(len(self.BODY), payload["bytes"])
        self.assertEqual(hashlib.sha256(self.BODY).hexdigest(), payload["sha256"])
        self.assertTrue(payload["downloaded"])
        self.assertNoSecrets(out, err)

    def test_missing_files_read_scope_reports_the_exact_setup_step(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": false, "error": "missing_scope", "needed": "files:read"}'

        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
            opener=RecordingOpener(Response()),
        )

        self.assertEqual(1, code)
        self.assertIn("missing_scope", err)
        self.assertIn("files:read", err)
        self.assertIn("OAuth & Permissions", err)
        self.assertFalse(self.output.exists())
        self.assertNoSecrets(out, err)

    def test_slack_refusals_are_translated_without_echoing_the_response(self) -> None:
        cases = {
            "file_not_found": "cannot see a file with that exact ID",
            "file_deleted": "reports this file as deleted",
            "access_denied": "not allowed to read this file",
        }
        for code_name, expected in cases.items():
            with self.subTest(error=code_name):
                def call(profile, method, params, code_name=code_name):
                    raise self.module.SlackError(
                        f"Slack API refused the read: {code_name}.", code=code_name
                    )

                exit_code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
                    api=call,
                )
                self.assertEqual(1, exit_code)
                self.assertIn(code_name, err)
                self.assertIn(expected, err)
                self.assertNoSecrets(out, err)

    def test_malformed_or_mismatched_metadata_is_refused(self) -> None:
        cases = {
            "no file object": ({"ok": True}, "no file object"),
            "file is not an object": ({"ok": True, "file": "example"}, "no file object"),
            "different id": (self.file_payload(id="F99999999"), "different file ID"),
            "missing size": (self.file_payload(size=None), "no usable byte size"),
            "zero size": (self.file_payload(size=0), "no usable byte size"),
            "text size": (self.file_payload(size="12"), "no usable byte size"),
            "no url": (
                self.file_payload(url_private_download=None),
                "no usable private download URL",
            ),
        }
        for label, (payload, expected) in cases.items():
            with self.subTest(case=label):
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
                    api=self.metadata_only(payload),
                )
                self.assertEqual(1, code)
                self.assertIn(expected, err)
                self.assertFalse(self.output.exists())
                self.assertEqual([], self.leftovers())
                self.assertNoSecrets(out, err)

    def test_only_a_visible_slack_hosted_file_is_retrieved(self) -> None:
        cases = {
            "limited access": (self.file_payload(file_access="check_file_info"), "cannot read it"),
            "external mode": (self.file_payload(mode="external"), "Only Slack-hosted files"),
            "external flag": (self.file_payload(is_external=True), "Only Slack-hosted files"),
            "external type": (self.file_payload(external_type="gdrive"), "Only Slack-hosted files"),
            "snippet": (self.file_payload(mode="snippet"), "Only Slack-hosted files"),
            "tombstoned": (self.file_payload(mode="tombstoned"), "Only Slack-hosted files"),
        }
        for label, (payload, expected) in cases.items():
            with self.subTest(case=label):
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
                    api=self.metadata_only(payload),
                )
                self.assertEqual(1, code)
                self.assertIn(expected, err)
                self.assertFalse(self.output.exists())
                self.assertNoSecrets(out, err)

        # Visibility is required by this command's metadata contract.
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
            api=self.metadata_only(self.file_payload(file_access=None)),
        )
        self.assertEqual(1, code)
        self.assertIn("not visible", err)
        self.assertFalse(self.output.exists())
        self.assertNoSecrets(out, err)

    def test_download_url_must_be_the_approved_slack_file_origin(self) -> None:
        cases = (
            "http://files.slack.com/files-pri/T0-F0/download/example.docx",
            "https://files.slack.com.example.test/files-pri/T0-F0/download/example.docx",
            "https://example.test/files-pri/T0-F0/download/example.docx",
            "https://files.slack.com:8443/files-pri/T0-F0/download/example.docx",
            "https://user:secret@files.slack.com/files-pri/T0-F0/download/example.docx",
            "https://slack.com/files-pri/T0-F0/download/example.docx",
            "//files.slack.com/files-pri/T0-F0/download/example.docx",
        )
        for url in cases:
            with self.subTest(url=url):
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm"],
                    api=self.metadata_only(self.file_payload(url_private_download=url)),
                )
                self.assertEqual(1, code)
                self.assertIn("https://files.slack.com", err)
                self.assertFalse(self.output.exists())
                self.assertEqual([], self.leftovers())
                self.assertNotIn("secret", err)
                self.assertNoSecrets(out, err)

    def test_file_id_must_be_exact(self) -> None:
        for value in ("F", "F0000", "C00000000", "f00000000", "F00000000 ", "*"):
            with self.subTest(file_id=value):
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", value, "--output", str(self.output)],
                    api=self.metadata_only(),
                )
                self.assertEqual(1, code)
                self.assertIn("exact Slack file ID", err)
                self.assertNoSecrets(out, err)

    def test_a_file_above_the_byte_bound_is_refused_before_any_download(self) -> None:
        code, out, err = self.run_cli(
            [
                "attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output),
                "--max-bytes", "10", "--confirm",
            ],
            api=self.metadata_only(),
        )

        self.assertEqual(1, code)
        self.assertIn("above the --max-bytes limit of 10", err)
        self.assertFalse(self.output.exists())
        self.assertEqual([], self.leftovers())
        self.assertNoSecrets(out, err)

    def test_a_download_larger_than_its_metadata_is_refused_and_leaves_nothing(self) -> None:
        cases = {
            "declared content length": (
                StagedResponse(self.BODY, {"Content-Length": str(len(self.BODY) + 1000)}),
                "above the --max-bytes limit",
            ),
            "streamed overrun": (
                StagedResponse(self.BODY + b"overrun"),
                "exceeded the --max-bytes limit",
            ),
        }
        for label, (response, expected) in cases.items():
            with self.subTest(case=label):
                code, out, err = self.run_cli(
                    [
                        "attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output),
                        "--max-bytes", str(len(self.BODY)), "--confirm",
                    ],
                    api=self.metadata_only(),
                    opener=RecordingOpener(response),
                )
                self.assertEqual(1, code)
                self.assertIn(expected, err)
                self.assertFalse(self.output.exists())
                self.assertEqual([], self.leftovers())
                self.assertNoSecrets(out, err)

    def test_a_partial_transfer_is_never_published_as_the_destination(self) -> None:
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm"],
            api=self.metadata_only(),
            opener=RecordingOpener(StagedResponse(self.BODY[:10])),
        )

        self.assertEqual(1, code)
        self.assertIn(f"incomplete: 10 of {len(self.BODY)} bytes", err)
        self.assertFalse(self.output.exists())
        self.assertEqual([], self.leftovers())
        self.assertNoSecrets(out, err)

    def test_an_existing_destination_is_never_overwritten(self) -> None:
        self.output.write_bytes(b"existing")
        link = self.destination_dir / "link.docx"
        link.symlink_to(self.destination_dir / "missing.docx")
        cases = {
            "existing file": (self.output, "Refusing to overwrite"),
            "dangling symlink": (link, "Refusing to overwrite"),
            "missing directory": (
                self.destination_dir / "absent" / "example.docx",
                "Destination directory does not exist",
            ),
        }
        for label, (destination, expected) in cases.items():
            with self.subTest(case=label):
                # `api_call` is left unstaged: the destination is checked before Slack is asked.
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(destination), "--confirm"],
                )
                self.assertEqual(1, code)
                self.assertIn(expected, err)
                self.assertNoSecrets(out, err)
        self.assertEqual(b"existing", self.output.read_bytes())
        self.assertEqual(["example.docx", "link.docx"], self.leftovers())

    def test_a_race_that_creates_the_destination_first_refuses_rather_than_replaces(self) -> None:
        winner = b"written by somebody else"

        class RacingResponse(StagedResponse):
            def read(inner, size=-1):
                self.output.write_bytes(winner)
                return super(RacingResponse, inner).read(size)

        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm"],
            api=self.metadata_only(),
            opener=RecordingOpener(RacingResponse(self.BODY)),
        )

        self.assertEqual(1, code)
        self.assertIn("Refusing to overwrite", err)
        self.assertEqual(winner, self.output.read_bytes())
        self.assertEqual(["example.docx"], self.leftovers())
        self.assertNoSecrets(out, err)

    def test_download_http_failures_are_reported_without_the_url_or_token(self) -> None:
        cases = {
            "forbidden": (
                self.module.urllib.error.HTTPError(self.URL, 403, "Forbidden", {}, None),
                "HTTP error 403",
            ),
            "rate limited": (
                self.module.urllib.error.HTTPError(
                    self.URL, 429, "Too Many Requests", {"Retry-After": "9"}, None
                ),
                "retry after 9 seconds",
            ),
            "transport": (
                self.module.urllib.error.URLError("connection reset"),
                "transfer failed (URLError)",
            ),
            # A body shorter than its Content-Length is not an OSError, so it needs its own path.
            "truncated body": (
                self.module.http.client.IncompleteRead(b"partial"),
                "transfer failed (IncompleteRead)",
            ),
            "unsafe redirect": (
                self.module.SlackError("Slack API refused an unexpected cross-origin redirect."),
                "cross-origin redirect",
            ),
        }
        for label, (error, expected) in cases.items():
            with self.subTest(case=label):
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm"],
                    api=self.metadata_only(),
                    opener=RecordingOpener(error=error),
                )
                self.assertEqual(1, code)
                self.assertIn(expected, err)
                self.assertFalse(self.output.exists())
                self.assertEqual([], self.leftovers())
                self.assertNoSecrets(out, err)

    def test_the_download_uses_the_same_origin_redirect_guard(self) -> None:
        handler = self.module.SameOriginRedirectHandler()
        request = self.module.urllib.request.Request(
            self.URL, headers={"Authorization": "Bearer synthetic-token"}
        )

        redirected = handler.redirect_request(
            request, None, 302, "Found", {},
            "https://files.slack.com/files-pri/T00000000-F00000000/download/example.docx?t=1",
        )
        self.assertEqual("Bearer synthetic-token", redirected.get_header("Authorization"))

        for target in (
            "https://example.test/collect",
            "https://files.slack.com.example.test/collect",
            "http://files.slack.com/downgrade",
        ):
            with self.subTest(target=target), self.assertRaises(self.module.SlackError):
                handler.redirect_request(request, None, 302, "Found", {}, target)

        captured = {}

        def build_opener(*handlers):
            captured["handlers"] = handlers
            return RecordingOpener(StagedResponse(self.BODY))

        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(self.module, "api_call", side_effect=self.metadata_only()), \
                patch.object(self.module.urllib.request, "build_opener", build_opener), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, self.module.main(
                ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output), "--confirm"]
            ))
        self.assertTrue(any(
            isinstance(handler, self.module.SameOriginRedirectHandler)
            for handler in captured["handlers"]
        ))

    def test_max_bytes_is_bounded_by_the_parser(self) -> None:
        for value in ("0", "-1", "abc", str(self.module.MAX_ATTACHMENT_BYTES + 1)):
            with self.subTest(value=value), patch.dict(os.environ, self.environment(), clear=True):
                with redirect_stderr(io.StringIO()) as error:
                    with self.assertRaises(SystemExit) as raised:
                        self.module.main([
                            "attachment", "--profile", "example", "--id", self.FILE_ID,
                            "--output", str(self.output), "--max-bytes", value,
                        ])
                self.assertEqual(2, raised.exception.code)
                self.assertIn("--max-bytes", error.getvalue())

    def test_message_commands_stay_usable_on_a_profile_without_files_read(self) -> None:
        def call(profile, method, params):
            if method == "files.info":
                raise self.module.SlackError(
                    "Slack API refused the read: missing_scope.", code="missing_scope"
                )
            return {
                "ok": True,
                "messages": [{"ts": "1700000000.000000", "user": "U00000000", "text": "Readable"}],
                "response_metadata": {"next_cursor": ""},
            }

        code, out, err = self.run_cli(
            ["messages", "--channel", "C00000000", "--limit", "1"], api=call
        )
        self.assertEqual(0, code)
        self.assertIn("Readable", out)
        self.assertEqual("", err)

        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)], api=call
        )
        self.assertEqual(1, code)
        self.assertIn("files:read", err)
        self.assertNoSecrets(out, err)

    def test_provider_supplied_names_cannot_break_or_forge_the_output_record(self) -> None:
        payload = self.file_payload(
            name="report\n\r\twith\x00control.docx | bytes=0 | sha256=forged",
            mimetype="text/plain\n",
        )
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
            api=self.metadata_only(payload),
        )

        self.assertEqual(0, code)
        self.assertEqual(1, len(out.strip().splitlines()))
        self.assertIn("name=report with control.docx / bytes=0 / sha256=forged", out)
        self.assertIn("mime=text/plain", out)
        # The forged fields are inert: the real record still has exactly its own separators.
        self.assertEqual(8, out.count(" | "))
        self.assertNoSecrets(out, err)

    def test_non_printing_unicode_is_removed_and_real_scripts_are_kept(self) -> None:
        hostile = {
            "C1 control": "\u0085",
            "bidi override": "\u202e",
            "bidi isolate": "\u2066",
            "zero width space": "\u200b",
            "zero width joiner": "\u200d",
            "soft hyphen": "\u00ad",
            "line separator": "\u2028",
            "paragraph separator": "\u2029",
            "private use": "\ue000",
        }
        for label, char in hostile.items():
            with self.subTest(case=label):
                code, out, _ = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID,
                     "--output", str(self.output)],
                    api=self.metadata_only(self.file_payload(name=f"a{char}b.docx")),
                )
                self.assertEqual(0, code)
                self.assertNotIn(char, out)
                # The record stays one line with its own separators, whatever arrived.
                self.assertEqual(1, len(out.strip().splitlines()))
                self.assertEqual(8, out.count(" | "))

        # Ordinary Unicode is content, not a control, and survives untouched.
        code, out, _ = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
            api=self.metadata_only(self.file_payload(name="réport-日本語-café.docx")),
        )
        self.assertEqual(0, code)
        self.assertIn("name=réport-日本語-café.docx", out)

    def test_attachment_requires_an_explicit_profile_before_any_other_work(self) -> None:
        loaded = []
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.environment(), clear=True))
            # Any of these being reached would mean the refusal came too late.
            stack.enter_context(patch.object(
                self.module, "load_dotenv", side_effect=lambda path: loaded.append(path)
            ))
            stack.enter_context(patch.object(
                self.module.urllib.request, "build_opener", return_value=ForbiddenOpener()
            ))
            stack.enter_context(redirect_stdout(io.StringIO()))
            error = stack.enter_context(redirect_stderr(io.StringIO()))
            with self.assertRaises(SystemExit) as raised:
                self.module.main(
                    ["attachment", "--id", self.FILE_ID, "--output", str(self.output)]
                )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("--profile", error.getvalue())
        self.assertEqual([], loaded, "no credential source may be read")
        self.assertFalse(self.output.exists())
        self.assertEqual([], self.leftovers())
        self.assertNoSecrets(error.getvalue())

    def test_only_attachment_requires_the_profile_flag(self) -> None:
        parser = self.module.build_parser()
        # Every message-only command still resolves a single configured account by itself.
        for argv in (
            ["profiles"],
            ["status"],
            ["channels"],
            ["messages", "--channel", "C00000000"],
            ["search", "--query", "synthetic"],
            ["thread", "--channel", "C00000000", "--ts", "1700000000.000000"],
        ):
            with self.subTest(command=argv[0]):
                self.assertIsNone(getattr(parser.parse_args(argv), "profile", None))
        with self.subTest(command="attachment"), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["attachment", "--id", self.FILE_ID, "--output", str(self.output)]
                )

    def test_a_malformed_mime_type_is_refused_before_anything_is_staged(self) -> None:
        cases = {
            "absent": self.file_payload(mimetype=None),
            "empty": self.file_payload(mimetype=""),
            "whitespace only": self.file_payload(mimetype="   "),
            "control only": self.file_payload(mimetype="\x00\x01"),
            "number": self.file_payload(mimetype=123),
            "object": self.file_payload(mimetype={"type": "application/pdf"}),
            "list": self.file_payload(mimetype=["application/pdf"]),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                code, out, err = self.run_cli(
                    ["attachment", "--profile", "example", "--id", self.FILE_ID,
                     "--output", str(self.output), "--confirm"],
                    api=self.metadata_only(payload),
                )
                self.assertEqual(1, code)
                self.assertIn("no usable MIME type", err)
                self.assertFalse(self.output.exists())
                self.assertEqual([], self.leftovers())
                self.assertNoSecrets(out, err)

    def test_a_staged_write_failure_is_bounded_and_leaves_nothing(self) -> None:
        """A full disk or failing device after the transfer must refuse, not traceback."""
        module_os = self.module.os
        real_fdopen = module_os.fdopen

        class FlushFailure:
            """Every byte is accepted and the flush then fails."""

            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()
                return False

            def write(self, chunk):
                return self.handle.write(chunk)

            def flush(self):
                raise OSError(28, "No space left on device")

            def fileno(self):
                return self.handle.fileno()

        class CloseFailure(FlushFailure):
            """The buffered close ending the `with` fails, as a full disk can."""

            def flush(self):
                self.handle.flush()

            def __exit__(self, *args):
                self.handle.close()
                raise OSError(5, "Input/output error")

        def failing_handle(factory):
            return lambda descriptor, mode: factory(real_fdopen(descriptor, mode))

        cases = {
            "fsync": lambda stack: stack.enter_context(patch.object(
                module_os, "fsync", side_effect=OSError(5, "Input/output error")
            )),
            "flush": lambda stack: stack.enter_context(patch.object(
                module_os, "fdopen", failing_handle(FlushFailure)
            )),
            "close": lambda stack: stack.enter_context(patch.object(
                module_os, "fdopen", failing_handle(CloseFailure)
            )),
        }
        for label, arrange in cases.items():
            with self.subTest(case=label):
                with ExitStack() as stack:
                    arrange(stack)
                    code, out, err = self.run_cli(
                        ["attachment", "--profile", "example", "--id", self.FILE_ID,
                         "--output", str(self.output), "--confirm"],
                        api=self.metadata_only(),
                        opener=RecordingOpener(StagedResponse(self.BODY)),
                    )

                self.assertEqual(1, code)
                self.assertIn("could not be staged beside", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(1, len(err.strip().splitlines()))
                self.assertTrue(err.startswith("ERROR: "))
                self.assertFalse(self.output.exists())
                self.assertEqual([], self.leftovers())
                self.assertNoSecrets(out, err)

    def staged_residue(self) -> list:
        """Whatever the staged temporary file left behind, found by its own prefix."""
        return [name for name in self.leftovers() if name.startswith(f".{self.output.name}.")]

    def test_a_failed_cleanup_after_publication_is_reported_not_suppressed(self) -> None:
        """The attachment is written, so the caller is told that and that a copy may remain."""
        with patch.object(
            self.module.Path, "unlink", side_effect=OSError(13, "Permission denied")
        ):
            code, out, err = self.run_cli(
                ["attachment", "--profile", "example", "--id", self.FILE_ID,
                 "--output", str(self.output), "--confirm"],
                api=self.metadata_only(),
                opener=RecordingOpener(StagedResponse(self.BODY)),
            )

        self.assertEqual(1, code)
        self.assertIn(f"was written to {self.output}", err)
        self.assertIn("temporary copy could not be removed", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(1, len(err.strip().splitlines()))
        self.assertTrue(err.startswith("ERROR: "))
        # The destination really is published, and the record of it is not claimed as success.
        self.assertEqual(self.BODY, self.output.read_bytes())
        self.assertNotIn("Slack attachment downloaded", out)
        residue = self.staged_residue()
        self.assertEqual(1, len(residue), "the injected failure must leave the staged file")
        self.assertNotIn(residue[0], err, "the staged path is private and stays out of output")
        self.assertNoSecrets(out, err)

    def test_a_failed_cleanup_after_a_refusal_reports_both_facts(self) -> None:
        """The refusal keeps its own wording and gains the residue fact; neither is dropped."""
        with patch.object(
            self.module.Path, "unlink", side_effect=OSError(13, "Permission denied")
        ):
            code, out, err = self.run_cli(
                ["attachment", "--profile", "example", "--id", self.FILE_ID,
                 "--output", str(self.output), "--confirm"],
                api=self.metadata_only(),
                opener=RecordingOpener(StagedResponse(self.BODY[:10])),
            )

        self.assertEqual(1, code)
        self.assertIn(f"incomplete: 10 of {len(self.BODY)} bytes", err)
        self.assertIn("may also remain in the destination directory", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(1, len(err.strip().splitlines()))
        self.assertTrue(err.startswith("ERROR: "))
        # Nothing was published, and the partial bytes are named without naming their path.
        self.assertFalse(self.output.exists())
        residue = self.staged_residue()
        self.assertEqual(1, len(residue), "the injected failure must leave the staged file")
        self.assertNotIn(residue[0], err)
        self.assertNoSecrets(out, err)

    def test_cleanup_that_succeeds_is_silent_on_both_paths(self) -> None:
        """The residual-state report is rare: normal runs still say nothing about staging."""
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID,
             "--output", str(self.output), "--confirm"],
            api=self.metadata_only(),
            opener=RecordingOpener(StagedResponse(self.BODY)),
        )
        self.assertEqual(0, code)
        self.assertEqual([], self.staged_residue())
        self.assertNotIn("temporary", out + err)

        self.output.unlink()
        code, out, err = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID,
             "--output", str(self.output), "--confirm"],
            api=self.metadata_only(),
            opener=RecordingOpener(StagedResponse(self.BODY[:10])),
        )
        self.assertEqual(1, code)
        self.assertEqual([], self.staged_residue())
        self.assertNotIn("may also remain", err)

    def test_interruption_with_failed_cleanup_warns_and_preserves_the_interrupt(self) -> None:
        """An interrupt stays an interrupt while private residue is still disclosed."""
        class InterruptedResponse(StagedResponse):
            def read(inner, size=-1):
                if inner.offset:
                    raise KeyboardInterrupt
                return super(InterruptedResponse, inner).read(10)

        attachment = self.module.Attachment(
            file_id=self.FILE_ID,
            name="example.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=len(self.BODY),
            download_url=self.URL,
        )
        error = io.StringIO()
        with patch.object(
            self.module.urllib.request,
            "build_opener",
            return_value=RecordingOpener(InterruptedResponse(self.BODY)),
        ), patch.object(
            self.module.Path, "unlink", side_effect=OSError(13, "Permission denied")
        ), redirect_stderr(error):
            with self.assertRaises(KeyboardInterrupt):
                self.module.save_attachment(
                    self.module.Profile("example", "synthetic-token", "Example"),
                    attachment,
                    self.output,
                    self.module.DEFAULT_MAX_ATTACHMENT_BYTES,
                )

        message = error.getvalue()
        self.assertEqual(1, len(message.strip().splitlines()))
        self.assertTrue(message.startswith("WARNING: "))
        self.assertIn("private temporary copy may also remain", message)
        self.assertFalse(self.output.exists())
        residue = self.staged_residue()
        self.assertEqual(1, len(residue), "the injected failure must leave the staged file")
        self.assertNotIn(residue[0], message)
        self.assertNoSecrets(message)

    def test_a_long_provider_name_is_bounded(self) -> None:
        code, out, _ = self.run_cli(
            ["attachment", "--profile", "example", "--id", self.FILE_ID, "--output", str(self.output)],
            api=self.metadata_only(self.file_payload(name="a" * 500)),
        )
        self.assertEqual(0, code)
        self.assertIn("a" * 139 + "…", out)
        self.assertNotIn("a" * 141, out)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root ignores directory permissions, so staging cannot be made to fail this way",
    )
    def test_a_destination_that_cannot_be_staged_is_refused_cleanly(self) -> None:
        locked = self.destination_dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)

        code, out, err = self.run_cli(
            [
                "attachment", "--profile", "example", "--id", self.FILE_ID,
                "--output", str(locked / "example.docx"), "--confirm",
            ],
            api=self.metadata_only(),
            opener=RecordingOpener(StagedResponse(self.BODY)),
        )

        self.assertEqual(1, code)
        self.assertIn("could not be staged beside", err)
        self.assertEqual([], sorted(path.name for path in locked.iterdir()))
        self.assertNoSecrets(out, err)


if __name__ == "__main__":
    unittest.main()
