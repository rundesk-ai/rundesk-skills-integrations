#!/usr/bin/env python3
"""Offline tests for jira."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parent
SCRIPT = MODULE_DIR / "jira.py"


def load_module():
    spec = importlib.util.spec_from_file_location("jira_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JiraModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example",
            base_url="https://example.atlassian.net",
            email="alex@example.com",
            token="token",
            projects=["APP", "OPS"],
            label="Example Jira",
        )

    def test_get_profile_maps_site_and_project_keys_from_env(self) -> None:
        env = {
            "JIRA_PROFILES": "example,example-two",
            "JIRA_DEFAULT_PROFILE": "example",
            "JIRA_EXAMPLE_LABEL": "Example Jira",
            "JIRA_EXAMPLE_BASE_URL": "https://example.atlassian.net",
            "JIRA_EXAMPLE_EMAIL": "alex@example.com",
            "JIRA_EXAMPLE_API_TOKEN": "secret",
            "JIRA_EXAMPLE_PROJECTS": "APP,OPS",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example", "example-two"])
            profile = self.module.get_profile("example")

        self.assertEqual(profile.base_url, "https://example.atlassian.net")
        self.assertEqual(profile.label, "Example Jira")
        self.assertEqual(profile.projects, ["APP", "OPS"])

    def test_missing_profile_config_reports_required_keys(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.get_profile("missing")

        message = str(error.exception)
        self.assertIn("JIRA_BASE_URL__MISSING", message)
        self.assertIn("JIRA_EMAIL__MISSING", message)
        self.assertIn("JIRA_API_TOKEN__MISSING", message)

    def test_missing_default_account_config_reports_the_plain_names(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.get_profile("default")

        message = str(error.exception)
        self.assertIn("JIRA_BASE_URL", message)
        self.assertNotIn("__", message)

    def test_rundesk_account_suffix_wins_over_the_legacy_profile_infix(self) -> None:
        """Rundesk manages `<FIELD>__<ACCOUNT>`; it must outrank this repository's own form."""
        env = {
            "JIRA_BASE_URL__EXAMPLE": "https://rundesk.atlassian.net",
            "JIRA_EMAIL__EXAMPLE": "managed@example.com",
            "JIRA_API_TOKEN__EXAMPLE": "managed-token",
            "JIRA_PROJECTS__EXAMPLE": "APP",
            "JIRA_EXAMPLE_BASE_URL": "https://legacy.atlassian.net",
            "JIRA_EXAMPLE_EMAIL": "legacy@example.com",
            "JIRA_EXAMPLE_API_TOKEN": "legacy-token",
            "JIRA_EXAMPLE_PROJECTS": "OPS",
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("example")

        self.assertEqual(profile.base_url, "https://rundesk.atlassian.net")
        self.assertEqual(profile.email, "managed@example.com")
        self.assertEqual(profile.token, "managed-token")
        self.assertEqual(profile.projects, ["APP"])

    def test_legacy_profile_infix_still_resolves_when_no_rundesk_account_exists(self) -> None:
        env = {
            "JIRA_EXAMPLE_TWO_BASE_URL": "https://legacy.atlassian.net",
            "JIRA_EXAMPLE_TWO_EMAIL": "legacy@example.com",
            "JIRA_EXAMPLE_TWO_API_TOKEN": "legacy-token",
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("example-two")

        self.assertEqual(profile.base_url, "https://legacy.atlassian.net")
        self.assertEqual(profile.token, "legacy-token")

    def test_named_account_never_falls_back_to_the_default_account_value(self) -> None:
        """Pairing one site's URL with another site's token is the failure this prevents."""
        env = {
            "JIRA_BASE_URL": "https://default.atlassian.net",
            "JIRA_EMAIL": "default@example.com",
            "JIRA_API_TOKEN": "default-token",
            "JIRA_BASE_URL__EXAMPLE": "https://example.atlassian.net",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.get_profile("example")

        self.assertIn("JIRA_EMAIL__EXAMPLE", str(error.exception))
        self.assertIn("JIRA_API_TOKEN__EXAMPLE", str(error.exception))
        self.assertNotIn("default-token", str(error.exception))

    def test_plain_names_alone_configure_one_default_account(self) -> None:
        env = {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "alex@example.com",
            "JIRA_API_TOKEN": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["default"], self.module.configured_profile_names())
            profile = self.module.get_profile("default")

        self.assertEqual(profile.base_url, "https://example.atlassian.net")
        self.assertEqual(profile.token, "secret")

    def test_accounts_are_discovered_from_both_spellings_without_a_declaration(self) -> None:
        env = {
            "JIRA_API_TOKEN__ACME": "acme-token",
            "JIRA_BASE_URL__ACME_TWO": "https://acme-two.atlassian.net",
            "JIRA_LEGACY_API_TOKEN": "legacy-token",
            "JIRA_ENV_FILE": "/dev/null",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                ["acme", "acme-two", "legacy"], self.module.configured_profile_names()
            )

    def test_explicit_profiles_variable_overrides_discovery(self) -> None:
        env = {
            "JIRA_PROFILES": "example",
            "JIRA_API_TOKEN__ACME": "acme-token",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["example"], self.module.configured_profile_names())

    def test_default_profile_variable_names_the_account_holding_the_plain_values(self) -> None:
        env = {
            "JIRA_DEFAULT_PROFILE": "example",
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "alex@example.com",
            "JIRA_API_TOKEN": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("example")
            with self.assertRaises(self.module.JiraError):
                self.module.get_profile("other")

        self.assertEqual(profile.token, "secret")

    def test_profile_rejects_non_https_or_non_origin_base_urls(self) -> None:
        for base_url in (
            "http://example.atlassian.net",
            "https://user:secret@example.atlassian.net",
            "https://example.atlassian.net/wiki",
            "https://example.atlassian.net:invalid",
        ):
            env = {
                "JIRA_EXAMPLE_BASE_URL": base_url,
                "JIRA_EXAMPLE_EMAIL": "alex@example.com",
                "JIRA_EXAMPLE_API_TOKEN": "secret",
            }
            with self.subTest(base_url=base_url), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(self.module.JiraError):
                    self.module.get_profile("example")

    def test_dotenv_warns_when_group_or_others_can_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("JIRA_TEST_VALUE=file\n", encoding="utf-8")
            path.chmod(0o644)
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
            "https://example.atlassian.net/rest/api/3/myself",
            headers={"Authorization": "Basic secret"},
        )
        same_origin = handler.redirect_request(
            original, None, 302, "Found", {}, "https://example.atlassian.net/rest/api/3/users"
        )
        cross_origin = handler.redirect_request(
            original, None, 302, "Found", {}, "https://files.example.net/download"
        )
        plaintext = handler.redirect_request(
            original, None, 302, "Found", {}, "http://example.atlassian.net/rest/api/3/users"
        )

        self.assertEqual(same_origin.get_header("Authorization"), "Basic secret")
        self.assertIsNone(cross_origin.get_header("Authorization"))
        self.assertIsNone(plaintext.get_header("Authorization"))

    def test_default_list_query_is_bounded_to_configured_projects(self) -> None:
        captured = {}

        def fake_request(profile, path, params=None):
            captured.update(params or {})
            return {"issues": []}

        args = SimpleNamespace(project=None, jql=None, limit=10, json=False)
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(io.StringIO()):
            self.module.command_list(args, self.profile)

        self.assertEqual(captured["jql"], "project in (APP, OPS) ORDER BY updated DESC")
        self.assertEqual(captured["maxResults"], 10)

    def test_search_uses_explicit_jql(self) -> None:
        captured = {}

        def fake_request(profile, path, params=None):
            captured.update(params or {})
            return {"issues": []}

        args = SimpleNamespace(project=None, jql="project = APP ORDER BY updated DESC", limit=5, json=False)
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(io.StringIO()):
            self.module.command_list(args, self.profile)

        self.assertEqual(captured["jql"], "project = APP ORDER BY updated DESC")
        self.assertEqual(captured["maxResults"], 5)

    def test_request_sends_json_body_with_requested_method(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"key":"APP-253"}'

        def fake_open_url(request, timeout):
            captured["method"] = request.method
            captured["data"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            captured["timeout"] = timeout
            return Response()

        with patch.object(self.module, "open_url", side_effect=fake_open_url):
            response = self.module.request(
                self.profile,
                "rest/api/3/issue",
                method="POST",
                body={"fields": {"summary": "Created"}},
                retries=0,
            )

        self.assertEqual(response, {"key": "APP-253"})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["data"]), {"fields": {"summary": "Created"}})
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(captured["timeout"], 30)

    def test_request_sends_raw_body_and_extra_headers(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"[]"

        def fake_open_url(request, timeout):
            captured["method"] = request.method
            captured["data"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            captured["token_header"] = request.get_header("X-atlassian-token")
            return Response()

        with patch.object(self.module, "open_url", side_effect=fake_open_url):
            response = self.module.request(
                self.profile,
                "rest/api/3/issue/APP-252/attachments",
                method="POST",
                raw_body=b"multipart",
                extra_headers={
                    "Content-Type": "multipart/form-data; boundary=test",
                    "X-Atlassian-Token": "no-check",
                },
                retries=0,
            )

        self.assertEqual(response, [])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["data"], b"multipart")
        self.assertEqual(captured["content_type"], "multipart/form-data; boundary=test")
        self.assertEqual(captured["token_header"], "no-check")

    def test_text_to_adf_preserves_lines(self) -> None:
        self.assertEqual(
            self.module.text_to_adf("First line\n\nThird line"),
            {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "First line"}]},
                    {"type": "paragraph"},
                    {"type": "paragraph", "content": [{"type": "text", "text": "Third line"}]},
                ],
            },
        )

    def test_create_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            project="APP",
            issue_type="Task",
            summary="Create this",
            description="Details",
            confirm=False,
            json=False,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), redirect_stdout(output):
            self.module.command_create(args, self.profile)

        self.assertIn("DRY-RUN Jira issue create", output.getvalue())
        self.assertIn('"summary": "Create this"', output.getvalue())

    def test_create_posts_only_to_an_allowed_project(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"profile": profile.name, "path": path, **kwargs})
            return {"id": "10001", "key": "APP-253"}

        args = SimpleNamespace(
            project="APP",
            issue_type="Task",
            summary="Create this",
            description="Details",
            confirm=True,
            json=True,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_create(args, self.profile)

        self.assertEqual(captured["path"], "rest/api/3/issue")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["retries"], 0)
        self.assertEqual(captured["body"]["fields"]["project"], {"key": "APP"})
        self.assertEqual(json.loads(output.getvalue())["issue_key"], "APP-253")

    def test_create_refuses_a_project_outside_the_allowlist(self) -> None:
        args = SimpleNamespace(
            project="OTHER",
            issue_type="Task",
            summary="Create this",
            description=None,
            confirm=True,
            json=False,
        )
        with patch.object(self.module, "request", side_effect=AssertionError("live write")):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.command_create(args, self.profile)

        self.assertIn("outside configured project allowlist", str(error.exception))

    def test_edit_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            issue_key="APP-252",
            summary="Updated title",
            description=None,
            clear_description=False,
            confirm=False,
            json=False,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), redirect_stdout(output):
            self.module.command_edit(args, self.profile)

        self.assertIn("DRY-RUN Jira issue edit", output.getvalue())
        self.assertIn('"summary": "Updated title"', output.getvalue())

    def test_edit_puts_only_requested_fields(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"profile": profile.name, "path": path, **kwargs})
            return None

        args = SimpleNamespace(
            issue_key="APP-252",
            summary=None,
            description=None,
            clear_description=True,
            confirm=True,
            json=True,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_edit(args, self.profile)

        self.assertEqual(captured["path"], "rest/api/3/issue/APP-252")
        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(captured["retries"], 0)
        self.assertEqual(captured["body"], {"fields": {"description": None}})
        self.assertEqual(json.loads(output.getvalue())["edited_fields"], ["description"])

    def test_edit_requires_a_field(self) -> None:
        args = SimpleNamespace(
            issue_key="APP-252",
            summary=None,
            description=None,
            clear_description=False,
            confirm=False,
            json=False,
        )
        with self.assertRaises(self.module.JiraError) as error:
            self.module.command_edit(args, self.profile)

        self.assertIn("Pass --summary", str(error.exception))

    def test_upload_is_dry_run_without_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "example.txt"
            file_path.write_bytes(b"example")
            args = SimpleNamespace(
                issue_key="APP-252",
                file=str(file_path),
                confirm=False,
                json=False,
            )
            output = io.StringIO()
            with patch.object(self.module, "request", side_effect=AssertionError("live upload")), redirect_stdout(output):
                self.module.command_upload(args, self.profile)

        self.assertIn("DRY-RUN Jira attachment upload", output.getvalue())
        self.assertIn("bytes=7", output.getvalue())

    def test_upload_posts_one_multipart_file(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"profile": profile.name, "path": path, **kwargs})
            return [{"id": "10001", "filename": "example.txt", "size": 7}]

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "example.txt"
            file_path.write_bytes(b"example")
            args = SimpleNamespace(
                issue_key="APP-252",
                file=str(file_path),
                confirm=True,
                json=True,
            )
            output = io.StringIO()
            with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
                self.module.command_upload(args, self.profile)

        self.assertEqual(captured["path"], "rest/api/3/issue/APP-252/attachments")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["retries"], 0)
        self.assertEqual(captured["extra_headers"]["X-Atlassian-Token"], "no-check")
        self.assertIn("multipart/form-data; boundary=", captured["extra_headers"]["Content-Type"])
        self.assertIn(b'name="file"; filename="example.txt"', captured["raw_body"])
        self.assertIn(b"example", captured["raw_body"])
        self.assertEqual(json.loads(output.getvalue())["attachments"][0]["id"], "10001")

    def test_upload_refuses_a_project_outside_the_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "example.txt"
            file_path.write_bytes(b"example")
            args = SimpleNamespace(
                issue_key="OTHER-252",
                file=str(file_path),
                confirm=True,
                json=False,
            )
            with patch.object(self.module, "request", side_effect=AssertionError("live upload")):
                with self.assertRaises(self.module.JiraError) as error:
                    self.module.command_upload(args, self.profile)

        self.assertIn("outside configured project allowlist", str(error.exception))

    def test_comment_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            issue_key="APP-252",
            body="Progress update",
            confirm=False,
            json=False,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live comment")), redirect_stdout(output):
            self.module.command_comment(args, self.profile)

        self.assertIn("DRY-RUN Jira comment add", output.getvalue())
        self.assertIn("Progress update", output.getvalue())

    def test_comment_posts_adf_body(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"profile": profile.name, "path": path, **kwargs})
            return {"id": "20001"}

        args = SimpleNamespace(
            issue_key="APP-252",
            body="Progress update",
            confirm=True,
            json=True,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_comment(args, self.profile)

        self.assertEqual(captured["path"], "rest/api/3/issue/APP-252/comment")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["retries"], 0)
        self.assertEqual(
            captured["body"],
            {"body": self.module.text_to_adf("Progress update")},
        )
        self.assertEqual(json.loads(output.getvalue())["comment_id"], "20001")

    def test_delete_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            issue_key="APP-252",
            confirm=False,
            json=False,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live delete")), redirect_stdout(output):
            self.module.command_delete(args, self.profile)

        self.assertIn("DRY-RUN Jira issue delete", output.getvalue())
        self.assertIn("permanently delete", output.getvalue())

    def test_delete_uses_exact_issue_key_and_confirmation(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"profile": profile.name, "path": path, **kwargs})
            return None

        args = SimpleNamespace(
            issue_key="APP-252",
            confirm=True,
            json=True,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_delete(args, self.profile)

        self.assertEqual(captured["path"], "rest/api/3/issue/APP-252")
        self.assertEqual(captured["method"], "DELETE")
        self.assertEqual(captured["retries"], 0)
        self.assertEqual(json.loads(output.getvalue())["deleted"], True)

    def test_list_output_is_csv_style_rows_with_module_detail_path(self) -> None:
        issue = self.issue(
            "APP-252",
            "APP",
            summary='Fix "quoted", comma title',
            assignee={"displayName": "Alex Example"},
            updated="2026-06-23T12:34:56.000-0400",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.module.print_issue_list([issue], self.profile)

        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(rows[0], self.module.ISSUE_LIST_COLUMNS)
        self.assertEqual(rows[1][0], "APP-252")
        self.assertEqual(rows[1][1], 'Fix "quoted", comma title')
        self.assertEqual(rows[1][5], "Alex Example")
        self.assertIn("jira detail APP-252", self.module.issue_line(issue, self.profile))

    def test_adf_to_text_joins_inline_paragraph_text(self) -> None:
        body = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": "world."}]}
            ],
        }
        self.assertEqual(self.module.adf_to_text(body), "Hello world.")

    def test_detail_json_includes_normalized_issue_comments_and_attachments(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "rest/api/3/issue/APP-252":
                self.assertIn("description", params["fields"])
                return self.issue(
                    "APP-252",
                    "APP",
                    description=self.adf("Build the integration."),
                    attachment=[
                        {
                            "id": "10000",
                            "filename": "example.png",
                            "size": 2048,
                            "mimeType": "image/png",
                            "author": {"displayName": "Alex Example", "accountId": "acct-1"},
                            "created": "2026-06-23T12:34:56.000-0400",
                            "content": "https://example.atlassian.net/rest/api/3/attachment/content/10000",
                        }
                    ],
                )
            if path.endswith("/comment"):
                start = int((params or {}).get("startAt") or 0)
                if start == 0:
                    return {"startAt": 0, "maxResults": 100, "total": 101, "comments": [self.comment("1", "First comment.")]}
                return {"startAt": 100, "maxResults": 100, "total": 101, "comments": [self.comment("2", "Second comment.")]}
            raise AssertionError(path)

        args = SimpleNamespace(
            issue_key="APP-252",
            full=False,
            json=True,
            comment_limit=10,
            attachment_limit=10,
            description_limit=2000,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_detail(args, self.profile)

        payload = json.loads(output.getvalue())
        normalized = payload["normalized"]
        self.assertNotIn("comments", payload)
        self.assertEqual(normalized["key"], "APP-252")
        self.assertEqual(normalized["title"], "Summary for APP-252")
        self.assertEqual(normalized["status"], "To Do")
        self.assertEqual(normalized["description"], "Build the integration.")
        self.assertEqual(normalized["attachments"][0]["id"], "10000")
        self.assertEqual([comment["id"] for comment in normalized["comments"]], ["1", "2"])

    def test_full_detail_json_includes_raw_comments_changelog_and_worklogs(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "rest/api/3/issue/APP-252":
                self.assertEqual(params["fields"], "*all")
                return self.issue("APP-252", "APP", description=self.adf("Full detail."))
            if path.endswith("/comment"):
                return {"startAt": 0, "maxResults": 100, "total": 1, "comments": [self.comment("1", "Full comment.")]}
            if path.endswith("/changelog"):
                return {"startAt": 0, "maxResults": 100, "total": 1, "values": [{"id": "change-1"}]}
            if path.endswith("/worklog"):
                return {"startAt": 0, "maxResults": 100, "total": 1, "worklogs": [{"id": "work-1"}]}
            raise AssertionError(path)

        args = SimpleNamespace(
            issue_key="APP-252",
            full=True,
            json=True,
            comment_limit=10,
            attachment_limit=10,
            description_limit=2000,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_detail(args, self.profile)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["comments"][0]["id"], "1")
        self.assertEqual(payload["normalized"]["comments"][0]["body"], "Full comment.")
        self.assertEqual(payload["changelog"], [{"id": "change-1"}])
        self.assertEqual(payload["worklogs"], [{"id": "work-1"}])

    def test_detail_text_respects_attachment_limit(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "rest/api/3/issue/APP-252":
                return self.issue(
                    "APP-252",
                    "APP",
                    attachment=[
                        {"id": "10000", "filename": "shown.pdf", "size": 2048, "mimeType": "application/pdf"},
                        {"id": "10001", "filename": "hidden.pdf", "size": 2048, "mimeType": "application/pdf"},
                    ],
                )
            if path.endswith("/comment"):
                return {"startAt": 0, "maxResults": 100, "total": 0, "comments": []}
            raise AssertionError(path)

        args = SimpleNamespace(
            issue_key="APP-252",
            full=False,
            json=False,
            comment_limit=10,
            attachment_limit=1,
            description_limit=2000,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_detail(args, self.profile)

        text_output = output.getvalue()
        self.assertIn("attachments: count=2", text_output)
        self.assertIn("shown.pdf", text_output)
        self.assertNotIn("hidden.pdf", text_output)

    def test_comments_command_outputs_paginated_json(self) -> None:
        def fake_request(profile, path, params=None):
            return {"startAt": 0, "maxResults": 100, "total": 1, "comments": [self.comment("1", "Ready.")]}

        args = SimpleNamespace(issue_key="APP-1", limit=10, body_limit=1200, json=True)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_comments(args, self.profile)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["normalized"][0]["body"], "Ready.")

    def test_attachments_command_lists_metadata_without_downloading_bytes(self) -> None:
        def fake_request(profile, path, params=None):
            return self.issue(
                "APP-1",
                "APP",
                attachment=[
                    {
                        "id": "10000",
                        "filename": "wireframe.pdf",
                        "size": 4096,
                        "mimeType": "application/pdf",
                        "author": {"displayName": "Blair Example"},
                    }
                ],
            )

        args = SimpleNamespace(issue_key="APP-1", limit=10, json=False)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), patch.object(
            self.module, "request_bytes", side_effect=AssertionError("downloaded bytes")
        ), redirect_stdout(output):
            self.module.command_attachments(args, self.profile)

        self.assertIn("Jira attachments", output.getvalue())
        self.assertIn("id=10000", output.getvalue())
        self.assertIn("wireframe.pdf", output.getvalue())

    def test_attachment_download_is_dry_run_without_confirm(self) -> None:
        def fake_request(profile, path, params=None):
            return {"id": "10000", "filename": "example.bin"}

        args = SimpleNamespace(id="10000", output="/tmp/example.bin", confirm=False)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), patch.object(
            self.module, "request_bytes", side_effect=AssertionError("downloaded bytes")
        ), redirect_stdout(output):
            self.module.command_attachment(args, self.profile)

        self.assertIn("DRY-RUN Jira attachment download", output.getvalue())

    def test_attachment_download_requires_confirm_and_writes_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "example.bin"

            def fake_request(profile, path, params=None):
                self.assertEqual(path, "rest/api/3/attachment/10000")
                return {"id": "10000", "filename": "example.bin"}

            def fake_request_bytes(profile, path, params=None):
                self.assertEqual(path, "rest/api/3/attachment/content/10000")
                self.assertIsNone(params)
                return b"example-bytes"

            args = SimpleNamespace(id="10000", output=str(output_path), confirm=True)
            with patch.object(self.module, "request", side_effect=fake_request), patch.object(
                self.module, "request_bytes", side_effect=fake_request_bytes
            ), redirect_stdout(io.StringIO()):
                self.module.command_attachment(args, self.profile)

            self.assertEqual(output_path.read_bytes(), b"example-bytes")

    def test_attachment_download_refuses_existing_output_before_requesting_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "example.bin"
            output_path.write_bytes(b"existing")

            def fake_request(profile, path, params=None):
                return {"id": "10000", "filename": "example.bin"}

            args = SimpleNamespace(id="10000", output=str(output_path), confirm=True)
            with patch.object(self.module, "request", side_effect=fake_request), patch.object(
                self.module, "request_bytes", side_effect=AssertionError("downloaded bytes")
            ):
                with self.assertRaises(self.module.JiraError):
                    self.module.command_attachment(args, self.profile)

            self.assertEqual(output_path.read_bytes(), b"existing")

    def test_attachment_download_refuses_dangling_symlink_before_requesting_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "example.bin"
            output_path.symlink_to(Path(temp_dir) / "missing.bin")
            args = SimpleNamespace(id="10000", output=str(output_path), confirm=True)

            with patch.object(self.module, "request", return_value={"id": "10000"}), patch.object(
                self.module, "request_bytes", side_effect=AssertionError("downloaded bytes")
            ):
                with self.assertRaises(self.module.JiraError):
                    self.module.command_attachment(args, self.profile)

            self.assertTrue(output_path.is_symlink())

    def test_attachment_publish_does_not_overwrite_racing_target_or_leave_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "example.bin"

            def racing_link(source, target):
                Path(target).write_bytes(b"racing-writer")
                raise FileExistsError(target)

            args = SimpleNamespace(id="10000", output=str(output_path), confirm=True)
            with patch.object(self.module, "request", return_value={"id": "10000"}), patch.object(
                self.module, "request_bytes", return_value=b"downloaded"
            ), patch.object(self.module.os, "link", side_effect=racing_link):
                with self.assertRaises(self.module.JiraError):
                    self.module.command_attachment(args, self.profile)

            self.assertEqual(output_path.read_bytes(), b"racing-writer")
            self.assertEqual([path.name for path in Path(temp_dir).iterdir()], ["example.bin"])

    def test_fetch_paginated_stops_on_empty_final_page(self) -> None:
        responses = [
            {"startAt": 0, "maxResults": 100, "values": [{"id": "1"}]},
            {"startAt": 100, "maxResults": 100, "values": []},
        ]

        def fake_request(profile, path, params=None):
            return responses.pop(0)

        with patch.object(self.module, "request", side_effect=fake_request):
            values = self.module.fetch_paginated(self.profile, "rest/api/3/example", "values")

        self.assertEqual(values, [{"id": "1"}])

    def test_issue_normalization_includes_epic_and_sprint_context(self) -> None:
        issue = self.issue(
            "APP-252",
            "APP",
            epic={"id": "100", "key": "APP-10", "name": "Roadmap"},
            sprint={"id": 7, "name": "Sprint 7", "state": "active"},
        )

        normalized = self.module.normalized_issue(issue, self.profile)

        self.assertEqual(normalized["epic"], {"key": "APP-10", "id": "100", "name": "Roadmap"})
        self.assertEqual(normalized["sprints"][0]["id"], "7")
        self.assertEqual(normalized["sprints"][0]["state"], "active")
        self.assertIn("epic=APP-10 (Roadmap)", self.module.issue_line(issue, self.profile))
        self.assertIn("sprint=7 (Sprint 7) [active]", self.module.issue_line(issue, self.profile))

    def test_boards_filter_by_project_and_bound_results(self) -> None:
        captured = {}

        def fake_request(profile, path, params=None):
            captured.update({"path": path, "params": params})
            return {"startAt": 0, "maxResults": 2, "total": 2, "values": [
                {"id": 42, "name": "App board", "type": "scrum"},
                {"id": 43, "name": "Other board", "type": "kanban"},
            ]}

        args = SimpleNamespace(project="APP", limit=2, json=True)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_boards(args, self.profile)

        self.assertEqual(captured["path"], "rest/agile/1.0/board")
        self.assertEqual(captured["params"]["projectKeyOrId"], "APP")
        self.assertEqual(len(json.loads(output.getvalue())["boards"]), 2)

    def test_sprints_pass_state_filter_to_board_endpoint(self) -> None:
        captured = {}

        def fake_request(profile, path, params=None):
            captured.update({"path": path, "params": params})
            return {"startAt": 0, "maxResults": 2, "total": 1, "values": [{"id": 7, "name": "Sprint 7", "state": "active"}]}

        args = SimpleNamespace(board_id="42", state=["active", "future"], limit=2, json=True)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_sprints(args, self.profile)

        self.assertEqual(captured["path"], "rest/agile/1.0/board/42/sprint")
        self.assertEqual(captured["params"]["state"], "active,future")
        self.assertEqual(json.loads(output.getvalue())["sprints"][0]["id"], 7)

    def test_backlog_and_epic_commands_fetch_issue_collections(self) -> None:
        paths = []

        def fake_request(profile, path, params=None):
            paths.append(path)
            return {"startAt": 0, "maxResults": 2, "total": 1, "issues": [self.issue("APP-252", "APP")]}

        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_backlog(SimpleNamespace(board_id="42", limit=2, json=False), self.profile)
            self.module.command_epic_issues(SimpleNamespace(epic="APP-10", limit=2, json=False), self.profile)

        self.assertEqual(paths, ["rest/agile/1.0/board/42/backlog", "rest/agile/1.0/epic/APP-10/issue"])
        self.assertIn("Jira backlog | board=42", output.getvalue())
        self.assertIn("Jira epic | epic=APP-10", output.getvalue())

    def test_assign_epic_is_dry_run_without_confirmation(self) -> None:
        args = SimpleNamespace(issue_key="APP-252", epic="APP-10", confirm=False, json=False)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), redirect_stdout(output):
            self.module.command_assign_epic(args, self.profile)

        self.assertIn("DRY-RUN Jira epic assignment", output.getvalue())

    def test_assign_epic_posts_one_issue(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"path": path, **kwargs})
            return None

        args = SimpleNamespace(issue_key="APP-252", epic="APP-10", confirm=True, json=True)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_assign_epic(args, self.profile)

        self.assertEqual(captured["path"], "rest/agile/1.0/epic/APP-10/issue")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], {"issues": ["APP-252"]})
        self.assertEqual(json.loads(output.getvalue())["epic"], "APP-10")

    def test_assign_sprint_posts_numeric_sprint_id(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"path": path, **kwargs})
            return None

        args = SimpleNamespace(issue_key="APP-252", sprint_id="7", confirm=True, json=True)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_assign_sprint(args, self.profile)

        self.assertEqual(captured["path"], "rest/agile/1.0/sprint/7/issue")
        self.assertEqual(captured["body"], {"issues": ["APP-252"]})
        self.assertEqual(json.loads(output.getvalue())["sprint_id"], 7)

    def test_create_detects_legacy_epic_link_field(self) -> None:
        captured = {}

        def fake_request(profile, path, params=None, **kwargs):
            if path == "rest/api/3/field":
                return [{
                    "id": "customfield_10014",
                    "name": "Epic Link",
                    "schema": {"custom": "com.pyxis.greenhopper.jira:gh-epic-link"},
                }]
            captured.update({"path": path, **kwargs})
            return {"id": "10001", "key": "APP-253"}

        args = SimpleNamespace(project="APP", issue_type="Task", summary="Create this", description=None, epic="APP-10", epic_field=None, confirm=True, json=True)
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(io.StringIO()):
            self.module.command_create(args, self.profile)

        self.assertEqual(captured["body"]["fields"]["customfield_10014"], "APP-10")

    def test_edit_accepts_parent_epic_field_override(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update({"path": path, **kwargs})
            return None

        args = SimpleNamespace(issue_key="APP-252", summary=None, description=None, clear_description=False, epic="APP-10", epic_field="parent", confirm=True, json=True)
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(io.StringIO()):
            self.module.command_edit(args, self.profile)

        self.assertEqual(captured["body"], {"fields": {"parent": {"key": "APP-10"}}})

    def test_identify_routes_by_project_prefix_then_falls_back_after_404(self) -> None:
        env = {
            "JIRA_PROFILES": "alpha,beta",
            "JIRA_ALPHA_BASE_URL": "https://alpha.example.com",
            "JIRA_ALPHA_EMAIL": "alpha@example.com",
            "JIRA_ALPHA_API_TOKEN": "token",
            "JIRA_ALPHA_PROJECTS": "ALPHA",
            "JIRA_BETA_BASE_URL": "https://beta.example.com",
            "JIRA_BETA_EMAIL": "beta@example.com",
            "JIRA_BETA_API_TOKEN": "token",
            "JIRA_BETA_PROJECTS": "BETA",
        }
        attempts = []

        def fake_request(profile, path, params=None):
            key = path.rsplit("/", 1)[-1]
            attempts.append((profile.name, key))
            if profile.name == "beta" and key == "BETA-9":
                return self.issue("BETA-9", "BETA")
            raise self.module.JiraError("Jira API 404 profile=%s: not found" % profile.name)

        args = SimpleNamespace(text="BETA-9 NOPE-1", all_profiles=True)
        with patch.dict(os.environ, env, clear=True), patch.object(self.module, "request", side_effect=fake_request):
            output = io.StringIO()
            with redirect_stdout(output):
                self.module.command_identify(args)

        self.assertEqual(attempts[0], ("beta", "BETA-9"))
        self.assertIn("NOPE-1 | not found", output.getvalue())

    @staticmethod
    def adf(text: str):
        return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    def comment(self, comment_id: str, body: str):
        return {
            "id": comment_id,
            "author": {"displayName": "Casey Example", "accountId": "acct-comment"},
            "created": "2026-06-23T12:34:56.000-0400",
            "updated": "2026-06-23T12:35:56.000-0400",
            "body": self.adf(body),
            "self": f"https://example.atlassian.net/rest/api/3/issue/10000/comment/{comment_id}",
        }

    @staticmethod
    def issue(key: str, project: str, **field_overrides):
        fields = {
            "summary": f"Summary for {key}",
            "project": {"key": project, "name": f"{project} Project"},
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "Alex Example", "accountId": "acct-assignee"},
            "updated": "2026-06-23T00:00:00.000-0400",
            "creator": {"displayName": "Creator Example"},
            "reporter": {"displayName": "Reporter Example"},
            "labels": ["example"],
            "components": [{"name": "App"}],
            "fixVersions": [{"name": "v1"}],
        }
        fields.update(field_overrides)
        return {"key": key, "id": "10000", "fields": fields}


if __name__ == "__main__":
    unittest.main()
