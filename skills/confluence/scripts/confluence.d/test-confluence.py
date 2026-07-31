#!/usr/bin/env python3
"""Offline tests for confluence."""

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
SCRIPT = MODULE_DIR / "confluence.py"


def load_module():
    spec = importlib.util.spec_from_file_location("confluence_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ConfluenceModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example",
            base_url="https://example.atlassian.net",
            email="alex@example.com",
            token="token",
            spaces=["DOCS", "OPS"],
            label="Example Docs",
        )

    def test_profile_reuses_jira_credentials_and_maps_spaces(self) -> None:
        env = {
            "JIRA_PROFILES": "example,example-two",
            "JIRA_DEFAULT_PROFILE": "example",
            "JIRA_EXAMPLE_LABEL": "Example Atlassian",
            "JIRA_EXAMPLE_BASE_URL": "https://example.atlassian.net",
            "JIRA_EXAMPLE_EMAIL": "alex@example.com",
            "JIRA_EXAMPLE_API_TOKEN": "secret",
            "CONFLUENCE_EXAMPLE_SPACES": "DOCS,OPS",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example", "example-two"])
            profile = self.module.get_profile("example")

        self.assertEqual(profile.base_url, "https://example.atlassian.net")
        self.assertEqual(profile.label, "Example Atlassian")
        self.assertEqual(profile.spaces, ["DOCS", "OPS"])

    def test_confluence_specific_credentials_override_jira_credentials(self) -> None:
        env = {
            "CONFLUENCE_PROFILES": "example",
            "CONFLUENCE_EXAMPLE_LABEL": "Example Docs",
            "CONFLUENCE_EXAMPLE_BASE_URL": "https://docs.example.com",
            "CONFLUENCE_EXAMPLE_EMAIL": "docs@example.com",
            "CONFLUENCE_EXAMPLE_API_TOKEN": "docs-token",
            "CONFLUENCE_EXAMPLE_SPACES": "DOCS",
            "JIRA_EXAMPLE_BASE_URL": "https://jira.example.com",
            "JIRA_EXAMPLE_EMAIL": "jira@example.com",
            "JIRA_EXAMPLE_API_TOKEN": "jira-token",
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("example")

        self.assertEqual(profile.base_url, "https://docs.example.com")
        self.assertEqual(profile.email, "docs@example.com")
        self.assertEqual(profile.token, "docs-token")

    def test_profile_rejects_non_https_or_non_origin_base_urls(self) -> None:
        for base_url in (
            "http://docs.example.com",
            "https://user:secret@docs.example.com",
            "https://docs.example.com/wiki",
            "https://docs.example.com:invalid",
        ):
            env = {
                "CONFLUENCE_EXAMPLE_BASE_URL": base_url,
                "CONFLUENCE_EXAMPLE_EMAIL": "docs@example.com",
                "CONFLUENCE_EXAMPLE_API_TOKEN": "secret",
            }
            with self.subTest(base_url=base_url), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(self.module.ConfluenceError):
                    self.module.get_profile("example")

    def test_dotenv_warns_when_group_or_others_can_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("CONFLUENCE_TEST_VALUE=file\n", encoding="utf-8")
            path.chmod(0o640)
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
            "https://example.atlassian.net/wiki/api/v2/spaces",
            headers={"Authorization": "Basic secret"},
        )
        same_origin = handler.redirect_request(
            original, None, 302, "Found", {}, "https://example.atlassian.net/wiki/api/v2/pages"
        )
        cross_origin = handler.redirect_request(
            original, None, 302, "Found", {}, "https://docs.example.net/wiki/api/v2/pages"
        )
        plaintext = handler.redirect_request(
            original, None, 302, "Found", {}, "http://example.atlassian.net/wiki/api/v2/pages"
        )

        self.assertEqual(same_origin.get_header("Authorization"), "Basic secret")
        self.assertIsNone(cross_origin.get_header("Authorization"))
        self.assertIsNone(plaintext.get_header("Authorization"))

    def test_search_defaults_to_configured_spaces(self) -> None:
        captured = {}

        def fake_request(profile, path, params=None):
            captured.update(params or {})
            return {"results": []}

        args = SimpleNamespace(space=None, cql=None, query="source quality", all_spaces=False, limit=5, json=False)
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(io.StringIO()):
            self.module.command_search(args, self.profile)

        self.assertEqual(captured["cql"], 'type = page and space in ("DOCS", "OPS") and text ~ "source quality" order by lastmodified desc')
        self.assertEqual(captured["limit"], 5)

    def test_search_requires_space_bounds_without_all_spaces_or_cql(self) -> None:
        profile = self.module.Profile("empty", "https://example.atlassian.net", "a@example.com", "token", [], "Empty")
        args = SimpleNamespace(space=None, cql=None, query="anything", all_spaces=False, limit=5, json=False)
        with self.assertRaises(self.module.ConfluenceError):
            self.module.command_search(args, profile)

    def test_list_outputs_csv_rows_from_v2_space_pages(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "wiki/api/v2/spaces":
                return {"results": [{"id": "10", "key": "DOCS", "name": "Docs"}]}
            if path == "wiki/api/v2/spaces/10/pages":
                return {"results": [self.v2_page("100", "Home", parent_id="")], "_links": {}}
            raise AssertionError(path)

        args = SimpleNamespace(space="DOCS", limit=10, json=False)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_list(args, self.profile)

        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(rows[0], self.module.PAGE_LIST_COLUMNS)
        self.assertEqual(rows[1][0], "100")
        self.assertEqual(rows[1][1], "Home")
        self.assertEqual(rows[1][2], "DOCS")

    def test_tree_builds_hierarchy_from_v2_space_pages(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "wiki/api/v2/spaces":
                return {"results": [{"id": "10", "key": "DOCS"}]}
            if path == "wiki/api/v2/spaces/10/pages":
                return {
                    "results": [
                        self.v2_page("100", "Home", parent_id=""),
                        self.v2_page("101", "Child", parent_id="100"),
                    ],
                    "_links": {},
                }
            raise AssertionError(path)

        args = SimpleNamespace(space="DOCS", root=None, depth=3, max_pages=10, json=False)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_tree(args, self.profile)

        text = output.getvalue()
        self.assertIn("- id=100 | space=DOCS | type=page | title=Home", text)
        self.assertIn("  - id=101 | space=DOCS | type=page | title=Child", text)

    def test_tree_root_uses_descendants_endpoint(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "wiki/api/v2/spaces":
                return {"results": [{"id": "10", "key": "DOCS"}]}
            if path == "wiki/api/v2/pages/100":
                return self.v2_page("100", "Home", parent_id="", space_id="10")
            if path == "wiki/api/v2/pages/100/descendants":
                self.assertEqual(params["depth"], 2)
                return {"results": [self.v2_page("101", "Child", parent_id="100", depth=1, space_id="10")], "_links": {}}
            raise AssertionError(path)

        args = SimpleNamespace(space="DOCS", root="100", depth=2, max_pages=10, json=True)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_tree(args, self.profile)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["root"]["id"], "100")
        self.assertEqual(payload["descendants"][0]["id"], "101")

    def test_full_page_json_includes_body_children_attachments_comments_and_normalized_text(self) -> None:
        def fake_request(profile, path, params=None):
            if path == "wiki/rest/api/content/123":
                return self.v1_page("123", "DOCS", body="<p>Hello <strong>world</strong></p>")
            if path.endswith("/child/page"):
                return {"results": [{"id": "child-1"}], "_links": {}}
            if path.endswith("/child/attachment"):
                return {"results": [{"id": "attachment-1"}], "_links": {}}
            if path.endswith("/child/comment"):
                return {"results": [{"id": "comment-1"}], "_links": {}}
            raise AssertionError(path)

        args = SimpleNamespace(page_id="123", full=True, json=True, body_limit=4000)
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), redirect_stdout(output):
            self.module.command_page(args, self.profile)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["page"]["id"], "123")
        self.assertEqual(payload["children"], [{"id": "child-1"}])
        self.assertEqual(payload["attachments"], [{"id": "attachment-1"}])
        self.assertEqual(payload["comments"], [{"id": "comment-1"}])
        self.assertIn("Hello world", payload["normalized"]["body_text"])

    def test_fetch_paginated_uses_cursor_from_next_link(self) -> None:
        responses = [
            {"results": [{"id": "1"}], "_links": {"next": "/wiki/api/v2/spaces/10/pages?cursor=abc&limit=50"}},
            {"results": [{"id": "2"}], "_links": {}},
        ]
        cursors = []

        def fake_request(profile, path, params=None):
            cursors.append((params or {}).get("cursor"))
            return responses.pop(0)

        with patch.object(self.module, "request", side_effect=fake_request):
            values = self.module.fetch_paginated(
                self.profile,
                "wiki/api/v2/spaces/10/pages",
                params={"limit": 50},
                start_param=None,
            )

        self.assertEqual(values, [{"id": "1"}, {"id": "2"}])
        self.assertEqual(cursors, [None, "abc"])

    def test_link_header_next_extracts_next_url(self) -> None:
        header = '<https://example.atlassian.net/wiki/api/v2/spaces/10/pages?cursor=abc>; rel="next"'
        self.assertEqual(
            self.module.link_header_next(header),
            "https://example.atlassian.net/wiki/api/v2/spaces/10/pages?cursor=abc",
        )

    def test_request_maps_link_header_next_into_response_links(self) -> None:
        class FakeResponse:
            headers = {"Link": '<https://example.atlassian.net/wiki/api/v2/spaces/10/pages?cursor=abc>; rel="next"'}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"results":[{"id":"1"}]}'

        with patch.object(self.module, "open_url", return_value=FakeResponse()):
            data = self.module.request(self.profile, "wiki/api/v2/spaces/10/pages")

        self.assertEqual(data["_links"]["next"], "https://example.atlassian.net/wiki/api/v2/spaces/10/pages?cursor=abc")

    def test_html_to_text_keeps_readable_words(self) -> None:
        value = self.module.html_to_text("<h1>Title</h1><p>After <strong>awarding</strong>, not QS.</p>")
        self.assertIn("Title", value)
        self.assertIn("After awarding, not QS.", value)

    def test_page_line_uses_module_fetch_path(self) -> None:
        line = self.module.page_line(self.v1_page("123", "DOCS"), self.profile)
        self.assertIn("confluence page 123", line)

    @staticmethod
    def v1_page(page_id: str, space_key: str, body: str = ""):
        return {
            "id": page_id,
            "type": "page",
            "status": "current",
            "title": f"Page {page_id}",
            "space": {"key": space_key},
            "version": {"number": 3},
            "ancestors": [{"id": "1", "title": "Parent"}],
            "body": {"storage": {"value": body}},
            "_links": {"webui": f"/spaces/{space_key}/pages/{page_id}"},
        }

    @staticmethod
    def v2_page(page_id: str, title: str, parent_id: str = "", depth: int | None = None, space_id: str = "10"):
        page = {
            "id": page_id,
            "type": "page",
            "status": "current",
            "title": title,
            "spaceId": space_id,
            "parentId": parent_id,
            "version": {"number": 1},
            "_links": {"webui": f"/spaces/DOCS/pages/{page_id}"},
        }
        if depth is not None:
            page["depth"] = depth
        return page


if __name__ == "__main__":
    unittest.main()
