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

        self.assertIn("JIRA_MISSING_BASE_URL", str(error.exception))
        self.assertIn("JIRA_MISSING_EMAIL", str(error.exception))
        self.assertIn("JIRA_MISSING_API_TOKEN", str(error.exception))

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
