#!/usr/bin/env python3
"""Offline tests for sentry."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parent
SCRIPT = MODULE_DIR / "sentry.py"


def load_module():
    spec = importlib.util.spec_from_file_location("sentry_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SentryModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example",
            token="token",
            org="example-org",
            base_url="https://example.sentry.io",
            projects=["example-api", "example-web"],
            label="Example Sentry",
        )

    def test_get_profile_maps_profiles_from_env(self) -> None:
        env = {
            "SENTRY_PROFILES": "example,example-two",
            "SENTRY_DEFAULT_PROFILE": "example",
            "SENTRY_EXAMPLE_LABEL": "Example Sentry",
            "SENTRY_EXAMPLE_BASE_URL": "https://example.sentry.io",
            "SENTRY_EXAMPLE_ORG": "example-org",
            "SENTRY_EXAMPLE_TOKEN": "secret",
            "SENTRY_EXAMPLE_PROJECTS": "example-api,example-web",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example", "example-two"])
            profile = self.module.get_profile("example")

        self.assertEqual(profile.label, "Example Sentry")
        self.assertEqual(profile.org, "example-org")
        self.assertEqual(profile.base_url, "https://example.sentry.io")
        self.assertEqual(profile.projects, ["example-api", "example-web"])

    def test_legacy_env_compatibility_supports_default_profile_and_auth_token(self) -> None:
        env = {
            "SENTRY_DEFAULT_PROFILE": "example-legacy",
            "SENTRY_EXAMPLE_LEGACY_ORG": "example-org",
            "SENTRY_EXAMPLE_LEGACY_BASE_URL": "https://us.sentry.io",
            "SENTRY_AUTH_TOKEN": "legacy-token",
            "SENTRY_EXAMPLE_LEGACY_PROJECTS": "example-api",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example-legacy"])
            profile = self.module.get_profile("example-legacy")

        self.assertEqual(profile.token, "legacy-token")
        self.assertEqual(profile.projects, ["example-api"])

    def test_single_profile_can_be_discovered_from_legacy_prefixed_keys(self) -> None:
        env = {
            "SENTRY_EXAMPLE_LEGACY_ORG": "example-org",
            "SENTRY_EXAMPLE_LEGACY_TOKEN": "legacy-token",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example-legacy"])
            args = SimpleNamespace(profile=None)
            self.assertEqual(self.module.selected_profile_name(args), "example-legacy")

    def test_profile_rejects_non_https_or_non_origin_base_urls(self) -> None:
        for base_url in (
            "http://example.sentry.io",
            "https://user:secret@example.sentry.io",
            "https://example.sentry.io/api/0",
            "https://example.sentry.io:invalid",
        ):
            env = {
                "SENTRY_EXAMPLE_BASE_URL": base_url,
                "SENTRY_EXAMPLE_ORG": "example-org",
                "SENTRY_EXAMPLE_TOKEN": "secret",
            }
            with self.subTest(base_url=base_url), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(self.module.SentryError):
                    self.module.get_profile("example")

    def test_load_dotenv_does_not_override_environment(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("SENTRY_TEST_VALUE=file\n")
            path = Path(handle.name)

        self.addCleanup(path.unlink)
        old_value = os.environ.get("SENTRY_TEST_VALUE")
        os.environ["SENTRY_TEST_VALUE"] = "env"
        try:
            self.module.load_dotenv(path)
            self.assertEqual(os.environ["SENTRY_TEST_VALUE"], "env")
        finally:
            if old_value is None:
                os.environ.pop("SENTRY_TEST_VALUE", None)
            else:
                os.environ["SENTRY_TEST_VALUE"] = old_value

    def test_dotenv_warns_when_group_or_others_can_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("SENTRY_TEST_VALUE=file\n", encoding="utf-8")
            path.chmod(0o604)
            error_output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stderr(error_output):
                self.module.load_dotenv(path)

        warning = error_output.getvalue()
        self.assertIn("WARNING", warning)
        self.assertIn("chmod 600", warning)
        self.assertIn(str(path), warning)

    def test_redirect_handler_removes_authorization_only_across_origins(self) -> None:
        handler = self.module.SameOriginRedirectHandler()
        original = self.module.urllib.request.Request(
            "https://example.sentry.io/api/0/projects/",
            headers={"Authorization": "Bearer secret"},
        )
        same_origin = handler.redirect_request(
            original, None, 302, "Found", {}, "https://example.sentry.io/api/0/organizations/"
        )
        cross_origin = handler.redirect_request(
            original, None, 302, "Found", {}, "https://other.sentry.io/api/0/organizations/"
        )
        plaintext = handler.redirect_request(
            original, None, 302, "Found", {}, "http://example.sentry.io/api/0/organizations/"
        )

        self.assertEqual(same_origin.get_header("Authorization"), "Bearer secret")
        self.assertIsNone(cross_origin.get_header("Authorization"))
        self.assertIsNone(plaintext.get_header("Authorization"))

    def test_request_retries_get_429_with_retry_after(self) -> None:
        calls = []

        def fake_urlopen(request, timeout=30):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    {"Retry-After": "3"},
                    self.error_body(b'{"detail":"rate limited"}'),
                )
            return self.response_body(b'{"ok": true}')

        with patch.object(self.module, "open_url", side_effect=fake_urlopen), patch.object(
            self.module.time, "sleep"
        ) as sleep:
            data, _ = self.module.request(self.profile, "GET", "projects/")

        self.assertEqual(data, {"ok": True})
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(3)

    def test_request_does_not_retry_non_get_429(self) -> None:
        calls = []

        def fake_urlopen(request, timeout=30):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "rate limited",
                {"Retry-After": "3"},
                self.error_body(b'{"detail":"rate limited"}'),
            )

        with patch.object(self.module, "open_url", side_effect=fake_urlopen), patch.object(
            self.module.time, "sleep"
        ) as sleep:
            with self.assertRaises(self.module.SentryError):
                self.module.request(self.profile, "PUT", "organizations/example-org/issues/123/", payload={"status": "resolved"})

        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_request_respects_zero_retries_for_get_429(self) -> None:
        calls = []

        def fake_urlopen(request, timeout=30):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "rate limited",
                {"Retry-After": "3"},
                self.error_body(b'{"detail":"rate limited"}'),
            )

        with patch.object(self.module, "open_url", side_effect=fake_urlopen), patch.object(
            self.module.time, "sleep"
        ) as sleep:
            with self.assertRaises(self.module.SentryError):
                self.module.request(self.profile, "GET", "projects/", retries=0)

        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_search_defaults_to_configured_projects(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_request(profile, method, path, params=None, payload=None, retries=2):
            calls.append((path, params or {}))
            if path == "organizations/example-org/projects/":
                return (
                    [
                        {"slug": "example-api", "id": "10"},
                        {"slug": "example-web", "id": "20"},
                    ],
                    {},
                )
            return ([], {})

        args = SimpleNamespace(
            query="is:unresolved",
            limit=5,
            sort="date",
            project=None,
            all_projects=False,
            json=False,
            all_profiles=False,
        )
        with patch.object(self.module, "selected_profiles", return_value=[self.profile]), patch.object(
            self.module, "request", side_effect=fake_request
        ), redirect_stdout(io.StringIO()):
            self.module.command_search(args)

        self.assertEqual(calls[-1][0], "organizations/example-org/issues/")
        self.assertEqual(calls[-1][1]["project"], ["10", "20"])
        self.assertEqual(calls[-1][1]["query"], "is:unresolved")

    def test_search_all_projects_skips_configured_project_filter(self) -> None:
        captured = {}

        def fake_request(profile, method, path, params=None, payload=None, retries=2):
            captured.update(params or {})
            return ([], {})

        args = SimpleNamespace(
            query="is:unresolved",
            limit=5,
            sort="date",
            project=None,
            all_projects=True,
            json=False,
            all_profiles=False,
        )
        with patch.object(self.module, "selected_profiles", return_value=[self.profile]), patch.object(
            self.module, "request", side_effect=fake_request
        ), redirect_stdout(io.StringIO()):
            self.module.command_search(args)

        self.assertNotIn("project", captured)

    def test_search_without_projects_requires_explicit_broad_scan(self) -> None:
        profile = self.module.Profile(
            name="empty",
            token="token",
            org="example-org",
            base_url="https://example.sentry.io",
            projects=[],
            label="Empty Sentry",
        )
        args = SimpleNamespace(project=None, all_projects=False)

        with self.assertRaises(self.module.SentryError):
            self.module.selected_project_slugs(args, profile)

    def test_all_profile_search_combines_profile_rows(self) -> None:
        env = {
            "SENTRY_PROFILES": "example,example-two",
            "SENTRY_EXAMPLE_ORG": "example-org",
            "SENTRY_EXAMPLE_TOKEN": "token",
            "SENTRY_EXAMPLE_PROJECTS": "example-api",
            "SENTRY_EXAMPLE_TWO_ORG": "example-two-org",
            "SENTRY_EXAMPLE_TWO_TOKEN": "token",
            "SENTRY_EXAMPLE_TWO_PROJECTS": "example-mobile",
        }

        def fake_request(profile, method, path, params=None, payload=None, retries=2):
            if path == f"organizations/{profile.org}/projects/":
                slug = profile.projects[0]
                return ([{"slug": slug, "id": f"{profile.name}-project"}], {})
            return ([self.issue(f"{profile.name}-1", slug=profile.projects[0])], {})

        args = SimpleNamespace(
            query="is:unresolved",
            limit=5,
            sort="date",
            project=None,
            all_projects=False,
            json=False,
            all_profiles=True,
        )
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_search(args)

        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(rows[0], self.module.ISSUE_LIST_COLUMNS)
        self.assertEqual(rows[1][-1], "example")
        self.assertEqual(rows[2][-1], "example-two")

    def test_resolve_short_id_requires_exact_single_match(self) -> None:
        def fake_search(profile, query, limit, sort="date", project_slugs=None, short_id_lookup=False):
            self.assertEqual(query, "example-1")
            self.assertTrue(short_id_lookup)
            return [self.issue("123", short_id="EXAMPLE-1")]

        with patch.object(self.module, "search_issues", side_effect=fake_search):
            self.assertEqual(self.module.resolve_issue_identifier(self.profile, "example-1"), "123")

    def test_resolve_short_id_rejects_multiple_matches(self) -> None:
        with patch.object(
            self.module,
            "search_issues",
            return_value=[self.issue("123", short_id="EXAMPLE-1"), self.issue("456", short_id="EXAMPLE-1")],
        ):
            with self.assertRaises(self.module.SentryError):
                self.module.resolve_issue_identifier(self.profile, "EXAMPLE-1")

    def test_resolve_short_id_rejects_fuzzy_sole_search_result(self) -> None:
        with patch.object(
            self.module,
            "search_issues",
            return_value=[self.issue("123", short_id="EXAMPLE-10")],
        ):
            with self.assertRaises(self.module.SentryError):
                self.module.resolve_issue_identifier(self.profile, "example-1")

    def test_issue_rows_are_csv_style_and_stable(self) -> None:
        output = io.StringIO()
        issue = self.issue("123", title='Quoted, "title"')
        with redirect_stdout(output):
            self.module.print_issue_rows([(issue, self.profile)])

        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(rows[0], self.module.ISSUE_LIST_COLUMNS)
        self.assertEqual(rows[1][0], "123")
        self.assertEqual(rows[1][1], "EXAMPLE-1")
        self.assertEqual(rows[1][2], 'Quoted, "title"')
        self.assertEqual(rows[1][-3], "2026-06-20 12:00")
        self.assertEqual(rows[1][-2], "2026-06-24 12:00")
        self.assertEqual(rows[1][-1], "example")

    def test_issue_line_prefers_short_id_and_only_previews_resolution(self) -> None:
        rendered = self.module.issue_line(self.issue("123"), self.profile)

        self.assertIn("detail: sentry detail EXAMPLE-1 --profile example", rendered)
        self.assertIn("inspect: sentry inspect EXAMPLE-1 --profile example", rendered)
        self.assertIn("resolve_preview: sentry resolve EXAMPLE-1 --profile example", rendered)
        self.assertNotIn("--confirm", rendered)
        self.assertIn("first=2026-06-20 12:00", rendered)
        self.assertIn("last=2026-06-24 12:00", rendered)

    def test_text_output_redacts_email_and_ip_values(self) -> None:
        issue = self.issue(
            "123",
            title="Failure for alex@example.com from 192.0.2.1 or 2001:db8::1",
        )
        event = self.event()
        event["title"] = "Failure for alex@example.com from 192.0.2.1 or 2001:db8::1"

        issue_output = self.module.issue_line(issue, self.profile)
        event_output = io.StringIO()
        with redirect_stdout(event_output):
            self.module.print_event(event)

        rendered = issue_output + event_output.getvalue()
        self.assertNotIn("alex@example.com", rendered)
        self.assertNotIn("192.0.2.1", rendered)
        self.assertNotIn("2001:db8::1", rendered)
        self.assertIn("[redacted-email]", rendered)
        self.assertIn("[redacted-ip]", rendered)

    def test_detail_renders_generic_external_issue_links(self) -> None:
        def fake_fetch_issue(profile, issue):
            return self.issue("123", integrationIssues=[{"serviceType": "example-tracker", "displayName": "APP-123"}])

        def fake_fetch_external(profile, issue_id):
            return ([{"id": "ext-1", "serviceType": "jira", "displayName": "APP-123", "webUrl": "https://example.atlassian.net/browse/APP-123"}], None)

        args = SimpleNamespace(issue="123", json=False)
        output = io.StringIO()
        with patch.object(self.module, "fetch_issue", side_effect=fake_fetch_issue), patch.object(
            self.module, "fetch_external_issues", side_effect=fake_fetch_external
        ), redirect_stdout(output):
            self.module.command_detail(args, self.profile)

        rendered = output.getvalue()
        self.assertIn("external_issues: count=2", rendered)
        self.assertIn("service=jira", rendered)
        self.assertIn("APP-123", rendered)
        self.assertNotIn("integrations/jira", rendered)

    def test_blank_external_issue_records_are_ignored(self) -> None:
        self.assertEqual(self.module.normalize_external_issue({}), {})

    def test_inspect_text_redacts_sensitive_user_values_and_prints_stack_summary(self) -> None:
        issue = self.issue("123")
        event = self.event(
            user={"email": "alex@example.com", "ip_address": "192.0.2.1"},
            entries=[
                {
                    "type": "exception",
                    "data": {
                        "values": [
                            {
                                "stacktrace": {
                                    "frames": [
                                        {"filename": "/srv/app/example.py", "function": "run", "lineno": 42, "inApp": True}
                                    ]
                                }
                            }
                        ]
                    },
                }
            ],
        )
        args = SimpleNamespace(issue="123", event_limit=1, json=False)
        output = io.StringIO()
        with patch.object(self.module, "fetch_issue", return_value=issue), patch.object(
            self.module, "fetch_external_issues", return_value=([], None)
        ), patch.object(self.module, "fetch_events", return_value=[event]), redirect_stdout(output):
            self.module.command_inspect(args, self.profile)

        rendered = output.getvalue()
        self.assertIn("user=present", rendered)
        self.assertIn("/srv/app/example.py:42 in run", rendered)
        self.assertNotIn("alex@example.com", rendered)
        self.assertNotIn("192.0.2.1", rendered)

    def test_resolve_without_confirm_is_dry_run(self) -> None:
        args = SimpleNamespace(issue="123", confirm=False, json=False)
        output = io.StringIO()
        with patch.object(self.module, "fetch_issue", return_value=self.issue("123")), patch.object(
            self.module, "request", side_effect=AssertionError("PUT should not be called")
        ), redirect_stdout(output):
            self.module.command_resolve(args, self.profile)

        self.assertIn("DRY-RUN Sentry resolve", output.getvalue())
        self.assertIn("Add --confirm", output.getvalue())

    def test_confirmed_resolve_sends_single_sentry_status_payload(self) -> None:
        calls: list[tuple[str, str, dict | None, int]] = []

        def fake_request(profile, method, path, params=None, payload=None, retries=2):
            calls.append((method, path, payload, retries))
            return (self.issue("123", status="resolved"), {})

        args = SimpleNamespace(issue="123", confirm=True, json=False)
        with patch.object(self.module, "fetch_issue", return_value=self.issue("123")), patch.object(
            self.module, "request", side_effect=fake_request
        ), redirect_stdout(io.StringIO()):
            self.module.command_resolve(args, self.profile)

        self.assertEqual(calls, [("PUT", "organizations/example-org/issues/123/", {"status": "resolved"}, 0)])

    def test_inspect_json_includes_raw_and_normalized_shapes(self) -> None:
        args = SimpleNamespace(issue="123", event_limit=1, json=True)
        output = io.StringIO()
        with patch.object(self.module, "fetch_issue", return_value=self.issue("123")), patch.object(
            self.module, "fetch_external_issues", return_value=([{"id": "ext-1", "displayName": "APP-123"}], None)
        ), patch.object(self.module, "fetch_events", return_value=[self.event()]), redirect_stdout(output):
            self.module.command_inspect(args, self.profile)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["issue"]["id"], "123")
        self.assertEqual(payload["externalIssues"][0]["id"], "ext-1")
        self.assertEqual(payload["normalized"]["issue"]["id"], "123")
        self.assertEqual(payload["normalized"]["events"][0]["id"], "event-1")

    def test_event_date_accepts_received_date_shape(self) -> None:
        event = {"dateReceived": "2026-06-24T12:34:56Z"}
        self.assertEqual(self.module.event_date(event), "2026-06-24 12:34")

    def issue(
        self,
        issue_id: str,
        short_id: str = "EXAMPLE-1",
        slug: str = "example-api",
        title: str = "Example failure",
        status: str = "unresolved",
        integrationIssues: list[dict] | None = None,
    ) -> dict:
        return {
            "id": issue_id,
            "shortId": short_id,
            "title": title,
            "culprit": "example.module in run",
            "permalink": "https://example.sentry.io/issues/123/",
            "level": "error",
            "priority": "medium",
            "status": status,
            "substatus": "ongoing",
            "count": "4",
            "userCount": 2,
            "firstSeen": "2026-06-20T12:00:00Z",
            "lastSeen": "2026-06-24T12:00:00Z",
            "project": {"id": "10", "slug": slug, "name": "Example API"},
            "metadata": {"type": "ValueError", "value": "Example value"},
            "integrationIssues": integrationIssues or [],
        }

    def event(self, user: dict | None = None, entries: list[dict] | None = None) -> dict:
        return {
            "eventID": "event-1",
            "dateCreated": "2026-06-24T12:34:56Z",
            "title": "Example failure",
            "release": "example-release",
            "platform": "python",
            "user": user,
            "tags": [
                {"key": "environment", "value": "production"},
                {"key": "release", "value": "example-release"},
            ],
            "entries": entries or [],
        }

    class error_body:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self) -> bytes:
            return self.body

        def close(self) -> None:
            return None

    class response_body:
        headers = {"Content-Type": "application/json"}

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self.body


if __name__ == "__main__":
    unittest.main()
