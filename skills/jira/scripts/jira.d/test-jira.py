#!/usr/bin/env python3
"""Offline tests for jira."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import subprocess
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

    def walk_nodes(self, node):
        """Yield every node in an ADF document, depth first."""
        if isinstance(node, dict):
            yield node
            for child in node.get("content", []):
                yield from self.walk_nodes(child)

    def collect_text(self, document) -> str:
        """Everything a reader can see, including values carried in attrs."""
        seen = []
        for node in self.walk_nodes(document):
            if node.get("type") == "text":
                seen.append(node["text"])
            for mark in node.get("marks", []):
                if mark.get("type") == "link":
                    seen.append(mark["attrs"]["href"])
        return "".join(seen)

    def test_markdown_to_adf_separates_blocks_on_blank_lines(self) -> None:
        self.assertEqual(
            self.module.markdown_to_adf("First line\n\nThird line"),
            {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "First line"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "Third line"}]},
                ],
            },
        )

    def test_markdown_to_adf_keeps_single_newlines_as_hard_breaks(self) -> None:
        self.assertEqual(
            self.module.markdown_to_adf("First\nSecond")["content"],
            [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "First"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "Second"},
                    ],
                }
            ],
        )

    def test_markdown_to_adf_renders_heading_levels(self) -> None:
        content = self.module.markdown_to_adf("# One\n\n###### Six")["content"]
        self.assertEqual([node["type"] for node in content], ["heading", "heading"])
        self.assertEqual([node["attrs"]["level"] for node in content], [1, 6])

    def test_markdown_to_adf_leaves_a_hash_without_a_space_as_prose(self) -> None:
        for source in ("#1234 is the ticket", "####### seven hashes"):
            self.assertEqual(
                self.module.markdown_to_adf(source)["content"][0]["type"], "paragraph", source
            )

    def test_markdown_to_adf_renders_a_bullet_list(self) -> None:
        self.assertEqual(
            self.module.markdown_to_adf("- First\n- Second")["content"],
            [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "First"}]}
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]}
                            ],
                        },
                    ],
                }
            ],
        )

    def test_markdown_to_adf_nests_a_list_inside_its_parent_item(self) -> None:
        content = self.module.markdown_to_adf("- Parent\n  - Child")["content"]
        parent_item = content[0]["content"][0]
        self.assertEqual([node["type"] for node in parent_item["content"]], ["paragraph", "bulletList"])
        nested = parent_item["content"][1]["content"][0]["content"][0]
        self.assertEqual(nested["content"][0]["text"], "Child")

    def test_markdown_to_adf_records_an_ordered_list_start_only_when_it_is_not_one(self) -> None:
        self.assertNotIn("attrs", self.module.markdown_to_adf("1. One\n2. Two")["content"][0])
        self.assertEqual(
            self.module.markdown_to_adf("3. Three\n4. Four")["content"][0]["attrs"], {"order": 3}
        )

    def test_markdown_to_adf_starts_a_new_list_when_the_marker_kind_changes(self) -> None:
        self.assertEqual(
            [node["type"] for node in self.module.markdown_to_adf("- Bullet\n1. Ordered")["content"]],
            ["bulletList", "orderedList"],
        )

    def test_markdown_to_adf_takes_fenced_code_literally(self) -> None:
        source = "```sql\n# not a heading\n- not a list\n**not bold**\n```"
        self.assertEqual(
            self.module.markdown_to_adf(source)["content"],
            [
                {
                    "type": "codeBlock",
                    "attrs": {"language": "sql"},
                    # codeBlock content takes text nodes without marks.
                    "content": [
                        {"type": "text", "text": "# not a heading\n- not a list\n**not bold**"}
                    ],
                }
            ],
        )

    def test_markdown_to_adf_omits_the_language_when_the_fence_names_none(self) -> None:
        self.assertNotIn("attrs", self.module.markdown_to_adf("```\nplain\n```")["content"][0])

    def test_markdown_to_adf_keeps_the_body_of_an_unterminated_fence(self) -> None:
        node = self.module.markdown_to_adf("```py\nstill mine")["content"][0]
        self.assertEqual(node["type"], "codeBlock")
        self.assertEqual(node["content"][0]["text"], "still mine")

    def test_markdown_to_adf_renders_a_rule_and_a_blockquote(self) -> None:
        content = self.module.markdown_to_adf("> Quoted\n\n---")["content"]
        self.assertEqual(
            content,
            [
                {
                    "type": "blockquote",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Quoted"}]}
                    ],
                },
                {"type": "rule"},
            ],
        )

    def test_markdown_to_adf_reads_spaced_dashes_as_a_rule_not_a_list(self) -> None:
        self.assertEqual(self.module.markdown_to_adf("- - -")["content"], [{"type": "rule"}])

    def test_markdown_to_adf_demotes_a_heading_inside_a_blockquote(self) -> None:
        # blockquote content permits paragraph, lists, and codeBlock, but not heading.
        quoted = self.module.markdown_to_adf("> # Not a heading here")["content"][0]
        self.assertEqual([node["type"] for node in quoted["content"]], ["paragraph"])

    def test_markdown_to_adf_applies_inline_marks(self) -> None:
        content = self.module.markdown_to_adf(
            "A **strong** and *soft* and `literal` and ~~struck~~ and [label](https://example.test/a)."
        )["content"][0]["content"]
        marked = {
            node["text"]: [mark["type"] for mark in node.get("marks", [])] for node in content
        }
        self.assertEqual(marked["strong"], ["strong"])
        self.assertEqual(marked["soft"], ["em"])
        self.assertEqual(marked["literal"], ["code"])
        self.assertEqual(marked["struck"], ["strike"])
        self.assertEqual(marked["label"], ["link"])
        link = [node for node in content if node["text"] == "label"][0]
        self.assertEqual(link["marks"][0]["attrs"], {"href": "https://example.test/a"})

    def test_markdown_to_adf_opens_strong_and_emphasis_together(self) -> None:
        self.assertEqual(
            self.module.markdown_to_adf("***both***")["content"][0]["content"],
            [{"type": "text", "text": "both", "marks": [{"type": "strong"}, {"type": "em"}]}],
        )

    def test_markdown_to_adf_suppresses_marks_inside_a_code_span(self) -> None:
        self.assertEqual(
            self.module.markdown_to_adf("`**not bold**`")["content"][0]["content"],
            [{"type": "text", "text": "**not bold**", "marks": [{"type": "code"}]}],
        )

    def test_markdown_to_adf_leaves_underscores_inside_a_word_alone(self) -> None:
        """An identifier must survive; JIRA_API_TOKEN is not emphasis."""
        content = self.module.markdown_to_adf("Read JIRA_API_TOKEN from file_name.py")["content"][0]
        self.assertEqual(content["content"], [{"type": "text", "text": "Read JIRA_API_TOKEN from file_name.py"}])
        emphasized = self.module.markdown_to_adf("a _word_ here")["content"][0]["content"]
        self.assertEqual([node for node in emphasized if node["text"] == "word"][0]["marks"], [{"type": "em"}])

    def test_markdown_to_adf_refuses_a_link_target_it_cannot_vouch_for(self) -> None:
        """A ticket is shared and clickable, so only a trusted scheme becomes a link."""
        for source in ("[x](javascript:alert(1))", "[x](data:text/html,hi)", "[x](/relative)"):
            document = json.dumps(self.module.markdown_to_adf(source))
            self.assertNotIn('"link"', document, source)
            self.assertIn("x", document, source)

    def test_markdown_to_adf_degrades_unmatched_delimiters_to_text(self) -> None:
        for source in ("**open", "[label](", "~~struck", "`code", "*"):
            content = self.module.markdown_to_adf(source)["content"][0]["content"]
            self.assertEqual("".join(node["text"] for node in content), source, source)
            self.assertNotIn("marks", content[0], source)

    def test_markdown_to_adf_never_drops_visible_words(self) -> None:
        source = (
            "# Objective\n\nShip **it** with `care`.\n\n"
            "## Requirements\n\n- alpha\n  - beta\n\n1. gamma\n\n"
            "```py\ndelta = 1\n```\n\n> epsilon\n\n---\n\n[zeta](https://example.test/z)\n"
        )
        rendered = self.collect_text(self.module.markdown_to_adf(source))
        for word in re.findall(r"[A-Za-z0-9]+", source):
            if word in {"py", "1"}:  # a fence language and a list marker are syntax, not prose
                continue
            self.assertIn(word, rendered, word)

    def test_markdown_to_adf_never_emits_an_empty_text_node(self) -> None:
        """ADF rejects an empty text node, so no input may produce one."""
        for source in ("", "\n\n", "   ", "#", "- ", "``", "****", "> "):
            for node in self.walk_nodes(self.module.markdown_to_adf(source)):
                if node.get("type") == "text":
                    self.assertNotEqual(node["text"], "", source)

    def test_markdown_to_adf_always_returns_a_document_with_content(self) -> None:
        for source in ("", "\n\n\n", "    "):
            document = self.module.markdown_to_adf(source)
            self.assertEqual(document["version"], 1)
            self.assertEqual(document["content"], [{"type": "paragraph"}], source)

    def test_markdown_to_adf_bounds_deep_nesting_and_long_delimiter_runs(self) -> None:
        deep = self.module.markdown_to_adf(">" * 200 + " bottom")
        self.assertIn("bottom", self.collect_text(deep))
        run = self.module.markdown_to_adf("prose " + "*" * 5000)
        self.assertIn("prose", self.collect_text(run))

    def test_markdown_to_adf_keeps_a_code_span_inside_emphasis_adf_compatible(self) -> None:
        """ADF allows `code` beside `link` only; strong, em, or strike with it is invalid."""
        for source, outer in (
            ("**bold `literal` here**", "strong"),
            ("*soft `literal` here*", "em"),
            ("~~struck `literal` here~~", "strike"),
            ("***both `literal` here***", "strong"),
        ):
            content = self.module.markdown_to_adf(source)["content"][0]["content"]
            code = [node for node in content if node["text"] == "literal"]
            self.assertEqual(len(code), 1, source)
            self.assertEqual(code[0]["marks"], [{"type": "code"}], source)
            surrounding = [
                mark["type"] for node in content if node["text"] != "literal"
                for mark in node.get("marks", [])
            ]
            self.assertIn(outer, surrounding, source)

    def test_markdown_to_adf_keeps_link_on_a_code_span(self) -> None:
        """`link` is the one mark ADF lets `code` keep, and the label is still a link."""
        content = self.module.markdown_to_adf("[`literal`](https://example.test/a)")["content"][0]["content"]
        self.assertEqual(
            content,
            [{
                "type": "text", "text": "literal",
                "marks": [{"type": "code"}, {"type": "link", "attrs": {"href": "https://example.test/a"}}],
            }],
        )

    def test_create_posts_no_code_mark_combined_with_emphasis(self) -> None:
        """The payload Jira receives is what must be valid, not only the renderer's output."""
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update(kwargs)
            return {"id": "10001", "key": "APP-253"}

        for source in (
            "**bold `literal`**", "*soft `literal`*", "~~struck `literal`~~", "***both `literal`***",
        ):
            args = self.create_args(freeform=True, description=source, confirm=True, json=True)
            with patch.object(self.module, "request", side_effect=fake_request), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.module.command_create(args, self.profile)
            posted = captured["body"]["fields"]["description"]
            for node in self.walk_nodes(posted):
                types = [mark["type"] for mark in node.get("marks", [])]
                if "code" in types:
                    self.assertEqual(types, ["code"], source)
            self.assertIn("literal", self.collect_text(posted), source)

    def test_comment_posts_no_code_mark_combined_with_emphasis(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update(kwargs)
            return {"id": "20001"}

        for source in (
            "**bold `literal`**", "*soft `literal`*", "~~struck `literal`~~", "***both `literal`***",
        ):
            args = SimpleNamespace(
                issue_key="APP-252", body=source, body_file=None, confirm=True, json=True,
            )
            with patch.object(self.module, "request", side_effect=fake_request), \
                    redirect_stdout(io.StringIO()):
                self.module.command_comment(args, self.profile)
            posted = captured["body"]["body"]
            for node in self.walk_nodes(posted):
                types = [mark["type"] for mark in node.get("marks", [])]
                if "code" in types:
                    self.assertEqual(types, ["code"], source)
            self.assertIn("literal", self.collect_text(posted), source)

    def stored_paragraph(self, text: str) -> dict:
        """One paragraph exactly as Jira's own editor would store the literal text."""
        return {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }

    def test_adf_to_text_escapes_prose_that_would_read_back_as_a_block(self) -> None:
        """A paragraph stored as `# note` must not return as a heading on the next render."""
        for literal in (
            "# not a heading", "###### not a heading", "- not a bullet", "* not a bullet",
            "+ not a bullet", "1. not ordered", "1) not ordered", "> not a quote",
            "```", "~~~", "---", "***", "___", "* * *", "- - -",
        ):
            document = self.stored_paragraph(literal)
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, literal)

    def test_adf_to_text_escapes_a_block_marker_a_list_item_carries(self) -> None:
        """The escape has to survive the `- ` a list item prefixes onto it."""
        document = {
            "type": "doc", "version": 1,
            "content": [{"type": "bulletList", "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "# not a heading"}]},
            ]}]}],
        }
        rendered = self.module.adf_to_text(document)
        self.assertEqual(self.module.markdown_to_adf(rendered), document)

    def test_adf_to_text_escapes_a_link_label_that_closes_early(self) -> None:
        """`[a]b](url)` loses the link entirely, so the label's brackets are escaped."""
        for label in ("a]b", "a[b", "[bracketed]"):
            document = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{
                    "type": "text", "text": label,
                    "marks": [{"type": "link", "attrs": {"href": "https://example.test/a"}}],
                }]}],
            }
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, label)

    def test_adf_to_text_escapes_unmarked_inline_markdown(self) -> None:
        """Literal Jira-editor text must not gain marks when its readback is reused."""
        for literal in (
            "*literal*", "**literal**", "~~literal~~", "foo`bar`baz",
            "[label](https://example.test/a)", r"already\*escaped\*", "a[b]c",
        ):
            document = self.stored_paragraph(literal)
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, literal)

    def test_adf_to_text_preserves_literal_delimiters_inside_marks(self) -> None:
        """An escaped content delimiter must not be selected as the mark's closing token."""
        for mark, literal in (
            ("strong", "bold * and **"),
            ("em", " soft * and [bracket]"),
            ("strike", "gone ~ and ~~"),
        ):
            document = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{
                    "type": "text", "text": literal, "marks": [{"type": mark}],
                }]}],
            }
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, mark)

    def test_adf_to_text_preserves_code_span_edge_whitespace(self) -> None:
        """The code-span pad is one space; meaningful edge whitespace must remain."""
        for literal in (" a ", " a", "a ", "  ", "\ta", "a\t", "` a `"):
            document = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{
                    "type": "text", "text": literal, "marks": [{"type": "code"}],
                }]}],
            }
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, repr(literal))

    def test_adf_to_text_preserves_special_text_in_a_code_link(self) -> None:
        """Escapes for the link label must not become characters inside inline code."""
        for literal in ("a]b", "a[b", r"a\b", r"a]b\c[", "`a]b`"):
            document = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{
                    "type": "text", "text": literal,
                    "marks": [
                        {"type": "code"},
                        {"type": "link", "attrs": {"href": "https://example.test/a"}},
                    ],
                }]}],
            }
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, literal)

    def test_adf_to_text_fences_inline_code_past_the_backticks_it_holds(self) -> None:
        """A single backtick closes the span early and drops the rest of the text."""
        for literal in ("a`b", "a``b", "`x`", "``", "plain"):
            document = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": literal, "marks": [{"type": "code"}]},
                ]}],
            }
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, literal)

    def test_adf_to_text_fences_a_code_block_that_contains_a_fence(self) -> None:
        """A three-backtick fence around a body holding one spills the code into prose."""
        for body in ("```\ninner\n```", "a\n````\nb", "x = 1"):
            document = {
                "type": "doc", "version": 1,
                "content": [{
                    "type": "codeBlock", "attrs": {"language": "py"},
                    "content": [{"type": "text", "text": body}],
                }],
            }
            rendered = self.module.adf_to_text(document)
            self.assertEqual(self.module.markdown_to_adf(rendered), document, body)

    def test_adf_to_text_round_trips_a_description_holding_its_own_delimiters(self) -> None:
        """One document carrying every hazard at once, written and read as this tool does."""
        source = (
            "## Objective\n\nUse ``a`b`` and [label\\]here](https://example.test/a).\n\n"
            "````\n```\nnested fence\n```\n````\n\n- \\# literal hash\n- plain\n"
        )
        document = self.module.markdown_to_adf(source)
        rendered = self.module.adf_to_text(document)
        self.assertEqual(self.module.markdown_to_adf(rendered), document)

    def test_adf_to_text_round_trips_a_structured_description(self) -> None:
        """A ticket this tool writes must read back as the markdown that produced it."""
        source = (
            "## Objective\n\nShip **it** with `care`.\n\n"
            "## Requirements\n\n- alpha\n  - beta\n- gamma\n\n"
            "3. three\n4. four\n\n```py\nx = 1\n```\n\n"
            "> note\n> more\n\n---\n\nSee [spec](https://example.test/s)."
        )
        self.assertEqual(self.module.adf_to_text(self.module.markdown_to_adf(source)), source)

    def test_adf_to_text_keeps_a_rule_and_a_link_target(self) -> None:
        rendered = self.module.adf_to_text(
            self.module.markdown_to_adf("---\n\n[label](https://example.test/a)")
        )
        self.assertIn("---", rendered)
        self.assertIn("https://example.test/a", rendered)

    def test_adf_to_text_marks_headings_and_list_items(self) -> None:
        rendered = self.module.adf_to_text(self.module.markdown_to_adf("# Title\n\n- one\n- two"))
        self.assertEqual(rendered, "# Title\n\n- one\n- two")

    def test_truncate_block_keeps_newlines_that_truncate_collapses(self) -> None:
        """`detail` prints block text, so its truncation must not flatten the structure."""
        self.assertEqual(self.module.truncate_block("a\nb"), "a\nb")
        self.assertEqual(self.module.truncate("a\nb"), "a b")
        self.assertEqual(self.module.truncate_block(None), "-")
        self.assertEqual(self.module.truncate_block("abcdefghij", 8), "abcde...")

    def test_markdown_to_adf_bounds_nested_link_recursion(self) -> None:
        """A link label recurses, so nesting must be bounded or `create` dies on a stack overflow."""
        source = "t"
        for _ in range(1000):
            source = "[" + source + "](https://a.test)"
        self.assertIn("t", self.collect_text(self.module.markdown_to_adf(source)))

    def test_markdown_to_adf_leaves_an_image_as_literal_text(self) -> None:
        """Images are not supported, so the whole construct stays text rather than becoming a link."""
        document = json.dumps(self.module.markdown_to_adf("![alt](https://example.test/i.png)"))
        self.assertNotIn('"link"', document)
        self.assertIn("alt", document)

    def test_markdown_to_adf_merges_sub_lists_under_uneven_indent(self) -> None:
        item = self.module.markdown_to_adf("- A\n    - B\n  - C\n- D")["content"][0]["content"][0]
        self.assertEqual([node["type"] for node in item["content"]], ["paragraph", "bulletList"])

    def test_adf_to_text_does_not_invent_spaces_around_marks(self) -> None:
        """`foo`bar`baz` must not read back as `foo `bar` baz`."""
        for source in ("foo`bar`baz", "word**bold**word", "a[link](https://example.test/x)b"):
            self.assertEqual(self.module.adf_to_text(self.module.markdown_to_adf(source)), source)

    def test_adf_to_text_surfaces_a_mention_or_emoji_carried_in_attrs(self) -> None:
        """A mention holds its text in attrs; dropping it loses who was named."""
        body = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Hey "},
                    {"type": "mention", "attrs": {"id": "a", "text": "@Jane Example"}},
                    {"type": "text", "text": ", see "},
                    {"type": "emoji", "attrs": {"shortName": ":thumbsup:"}},
                ]}
            ],
        }
        rendered = self.module.adf_to_text(body)
        self.assertIn("@Jane Example", rendered)
        self.assertIn(":thumbsup:", rendered)

    def test_adf_to_text_surfaces_a_block_node_whose_text_is_only_in_attrs(self) -> None:
        """A status lozenge or media title must not vanish from a ticket read-back."""
        body = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Before"}]},
                {"type": "mediaSingle", "attrs": {"title": "diagram.png"}},
                {"type": "status", "attrs": {"text": "IN PROGRESS"}},
            ],
        }
        rendered = self.module.adf_to_text(body)
        self.assertIn("diagram.png", rendered)
        self.assertIn("IN PROGRESS", rendered)

    def test_clip_is_shared_by_both_truncations(self) -> None:
        self.assertEqual(self.module.clip("abcdefghij", 8), "abcde...")
        self.assertEqual(self.module.clip("abc", 8), "abc")
        self.assertEqual(self.module.clip("abcdefghij", 3), "abc")

    def test_every_template_shares_one_acceptance_criteria_alias_tuple(self) -> None:
        """Two copies would let a Story and a Bug drift apart silently."""
        story = dict(self.module.STORY_SECTIONS)["Acceptance criteria"]
        bug = dict(self.module.BUG_SECTIONS)["Acceptance criteria"]
        self.assertIs(story, bug)

    def test_create_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            project="APP",
            issue_type="Task", freeform=True,
            summary="Create this",
            description="Details",
            confirm=False,
            json=False,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stderr(io.StringIO()), redirect_stdout(output):
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
            issue_type="Task", freeform=True,
            summary="Create this",
            description="Details",
            confirm=True,
            json=True,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=fake_request), \
                redirect_stderr(io.StringIO()), redirect_stdout(output):
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

    STORY_DESCRIPTION = (
        "## Objective\n\nRevoke a session.\n\n"
        "## Background\n\nSupport waits for the TTL.\n\n"
        "## Business value\n\nCuts the exposure window.\n\n"
        "## Requirements\n\n- Add the endpoint\n\n"
        "## Acceptance criteria\n\n- A revoked session returns 401\n\n"
        "## Open questions\n\n- None\n"
    )

    def create_args(self, **overrides):
        args = {
            "project": "APP", "issue_type": "Story", "summary": "Create this",
            "description": self.STORY_DESCRIPTION, "description_file": None,
            "freeform": False, "confirm": False, "json": False,
        }
        args.update(overrides)
        return SimpleNamespace(**args)

    def run_create(self, args) -> str:
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stderr(io.StringIO()), redirect_stdout(output):
            self.module.command_create(args, self.profile)
        return output.getvalue()

    def test_ticket_template_matches_decorated_and_custom_issue_type_names(self) -> None:
        for issue_type, expected in (
            ("Story", "story"), ("User Story", "story"), ("Task", "task"), ("Sub-task", "task"),
            ("Bug", "bug"), ("Defect", "bug"), ("Bug - Production", "bug"),
            ("Spike", "spike"), ("Research", "spike"), ("Epic", "story"),
        ):
            self.assertEqual(self.module.ticket_template_for(issue_type)[0], expected, issue_type)

    def test_ticket_template_is_empty_for_an_issue_type_we_do_not_know(self) -> None:
        template, sections = self.module.ticket_template_for("Kundenanfrage")
        self.assertEqual(template, "")
        self.assertEqual(sections, ())

    def test_create_accepts_a_story_with_every_required_section(self) -> None:
        self.assertIn("DRY-RUN Jira issue create", self.run_create(self.create_args()))

    def test_create_accepts_alias_section_headings(self) -> None:
        description = (
            "## Goal\n\nx\n\n## Context\n\nx\n\n## Why\n\nx\n\n"
            "## Scope\n\nx\n\n## Done when\n\nx\n\n## Questions\n\nx\n"
        )
        self.assertIn("DRY-RUN", self.run_create(self.create_args(description=description)))

    def test_create_refuses_a_description_with_no_headings(self) -> None:
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(description="Just do the thing, it is obvious."))
        message = str(error.exception)
        self.assertIn("no headings", message)
        self.assertIn("--freeform", message)
        self.assertIn("ticket-format.md", message)

    def test_create_refuses_a_story_missing_required_sections(self) -> None:
        description = "## Objective\n\nRevoke a session.\n\n## Background\n\nContext.\n"
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(description=description))
        message = str(error.exception)
        self.assertIn("missing Business value, Requirements, Acceptance criteria, Open questions", message)
        self.assertIn("Found: Objective, Background", message)

    def test_create_refuses_a_description_that_was_never_given(self) -> None:
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(description=None))
        self.assertIn("no description", str(error.exception))
        self.assertIn("DRY-RUN", self.run_create(self.create_args(description=None, freeform=True)))

    def test_create_requires_the_reproduction_sections_of_a_bug(self) -> None:
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(issue_type="Bug"))
        self.assertIn("Steps to reproduce", str(error.exception))

    def test_create_requires_the_timebox_and_deliverable_of_a_spike(self) -> None:
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(issue_type="Spike"))
        self.assertIn("Timebox", str(error.exception))
        self.assertIn("Deliverable", str(error.exception))

    def test_create_holds_an_unknown_issue_type_to_the_heading_floor(self) -> None:
        """A site's own type still may not ship a wall of text."""
        with self.assertRaises(self.module.JiraError):
            self.run_create(self.create_args(issue_type="Kundenanfrage", description="wall of text"))
        self.assertIn(
            "DRY-RUN",
            self.run_create(self.create_args(issue_type="Kundenanfrage", description="## Anything\n\nx")),
        )

    def test_create_refuses_a_heading_that_only_mentions_a_section_name(self) -> None:
        description = self.STORY_DESCRIPTION.replace(
            "## Acceptance criteria", "## Not the acceptance criteria"
        )
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(description=description))
        self.assertIn("Acceptance criteria", str(error.exception))

    def test_create_ignores_a_section_heading_buried_in_a_list(self) -> None:
        """A section is a top-level heading, not a line inside a bullet."""
        description = self.STORY_DESCRIPTION.replace("## Open questions", "- ## Open questions")
        with self.assertRaises(self.module.JiraError) as error:
            self.run_create(self.create_args(description=description))
        self.assertIn("Open questions", str(error.exception))

    def test_freeform_skips_the_check_but_still_renders_markdown(self) -> None:
        captured = {}

        def fake_request(profile, path, **kwargs):
            captured.update(kwargs)
            return {"id": "10001", "key": "APP-253"}

        args = self.create_args(description="# Heading", freeform=True, confirm=True, json=True)
        with patch.object(self.module, "request", side_effect=fake_request), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.module.command_create(args, self.profile)
        heading = captured["body"]["fields"]["description"]["content"][0]
        self.assertEqual(heading["type"], "heading")

    def test_the_structure_check_runs_before_the_dry_run_is_printed(self) -> None:
        """A clean preview followed by a refusal on --confirm is the wrong feedback order."""
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stderr(io.StringIO()), redirect_stdout(output):
            with self.assertRaises(self.module.JiraError):
                self.module.command_create(self.create_args(description="wall of text"), self.profile)
        self.assertNotIn("DRY-RUN", output.getvalue())

    def test_edit_renders_markdown_without_a_structure_check(self) -> None:
        """An edit is often a targeted correction, so it is not held to a template."""
        args = SimpleNamespace(
            issue_key="APP-252", summary=None, description="# Only a heading",
            description_file=None, clear_description=False, confirm=False, json=False,
        )
        output = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stdout(output):
            self.module.command_edit(args, self.profile)
        self.assertIn('"type": "heading"', output.getvalue())

    def test_freeform_says_on_stderr_that_the_check_was_skipped(self) -> None:
        errors = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stderr(errors), redirect_stdout(io.StringIO()):
            self.module.command_create(
                self.create_args(description="wall of text", freeform=True), self.profile
            )
        self.assertIn("structure check skipped", errors.getvalue())

    def test_an_unknown_issue_type_reports_its_fallback_on_stderr(self) -> None:
        """A skipped check has to be visible, and the references promise this line."""
        errors = io.StringIO()
        with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stderr(errors), redirect_stdout(io.StringIO()):
            self.module.command_create(
                self.create_args(issue_type="Kundenanfrage", description="## Anything\n\nx"),
                self.profile,
            )
        self.assertIn("Kundenanfrage", errors.getvalue())
        self.assertIn("not one this catalog knows", errors.getvalue())

    def test_comment_reads_the_body_from_stdin(self) -> None:
        args = SimpleNamespace(
            issue_key="APP-252", body="-", body_file=None, confirm=False, json=False
        )
        stdin = io.StringIO("Piped **comment**.")
        output = io.StringIO()
        with patch.object(self.module.sys, "stdin", stdin), \
                patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                redirect_stdout(output):
            self.module.command_comment(args, self.profile)
        self.assertIn("Piped **comment**.", output.getvalue())

    def test_edit_refuses_a_description_file_together_with_clear_description(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "d.md"
            page.write_text("# Rewritten", encoding="utf-8")
            args = SimpleNamespace(
                issue_key="APP-252", summary=None, description=None,
                description_file=str(page), clear_description=True, confirm=False, json=False,
            )
            with patch.object(self.module, "request", side_effect=AssertionError("live write")):
                with self.assertRaises(self.module.JiraError) as error:
                    self.module.command_edit(args, self.profile)
        self.assertIn("--clear-description", str(error.exception))

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
            {"body": self.module.markdown_to_adf("Progress update")},
        )
        self.assertEqual(json.loads(output.getvalue())["comment_id"], "20001")

    def test_create_reads_the_description_from_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "description.md"
            page.write_text("## Objective\n\nShip it.\n", encoding="utf-8")
            output = self.run_create(self.create_args(
                issue_type="Task", freeform=True, description=None, description_file=str(page),
            ))
        self.assertIn('"level": 2', output)
        self.assertIn("Objective", output)

    def test_create_reads_the_description_from_stdin(self) -> None:
        stdin = io.StringIO("## Objective\n\nPiped.")
        with patch.object(self.module.sys, "stdin", stdin):
            output = self.run_create(self.create_args(
                issue_type="Task", freeform=True, description="-", description_file=None,
            ))
        self.assertIn("Piped.", output)

    def test_create_refuses_both_description_and_description_file(self) -> None:
        args = SimpleNamespace(
            project="APP", issue_type="Task", summary="Create this",
            description="inline", description_file="/nonexistent.md", confirm=False, json=False,
        )
        with patch.object(self.module, "request", side_effect=AssertionError("live write")):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.command_create(args, self.profile)
        self.assertIn("not both", str(error.exception))

    def test_description_file_refuses_a_missing_path_or_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / "link.md"
            (Path(folder) / "real.md").write_text("x", encoding="utf-8")
            link.symlink_to(Path(folder) / "real.md")
            for candidate in (str(Path(folder) / "missing.md"), str(link)):
                args = SimpleNamespace(description=None, description_file=candidate)
                with self.assertRaises(self.module.JiraError) as error:
                    self.module.argument_text(args, "description")
                self.assertIn("regular file", str(error.exception), candidate)

    def test_description_file_is_refused_past_the_body_maximum(self) -> None:
        """An oversized file is refused with a JiraError, never read whole into memory."""
        limit = self.module.MAX_BODY_CHARS
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "huge.md"
            page.write_text("x" * (limit + 1), encoding="utf-8")
            args = SimpleNamespace(description=None, description_file=str(page))
            with self.assertRaises(self.module.JiraError) as error:
                self.module.argument_text(args, "description")
            self.assertIn(str(limit), str(error.exception))
            self.assertIn("character maximum", str(error.exception))

            page.write_text("y" * limit, encoding="utf-8")
            self.assertEqual(len(self.module.argument_text(args, "description")), limit)

    def test_stdin_body_is_refused_past_the_body_maximum_after_one_bounded_read(self) -> None:
        """The stream position proves the refusal read one character past the maximum."""
        limit = self.module.MAX_BODY_CHARS
        stdin = io.StringIO("z" * (limit + 4096))
        args = SimpleNamespace(body="-", body_file=None)
        with patch.object(self.module.sys, "stdin", stdin):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.argument_text(args, "body")
        self.assertIn("--body - standard input", str(error.exception))
        self.assertEqual(stdin.tell(), limit + 1)

        stdin = io.StringIO("z" * limit)
        with patch.object(self.module.sys, "stdin", stdin):
            self.assertEqual(len(self.module.argument_text(args, "body")), limit)

    def test_description_file_that_is_not_utf8_is_refused_without_a_traceback(self) -> None:
        """A pasted binary must surface as a redacted refusal, not a UnicodeDecodeError."""
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "binary.dat"
            page.write_bytes(b"\xff\xfe\x00\x01")
            args = SimpleNamespace(description=None, description_file=str(page))
            with self.assertRaises(self.module.JiraError) as error:
                self.module.argument_text(args, "description")
        message = str(error.exception)
        self.assertIn("not UTF-8 text", message)
        self.assertNotIn("codec", message)

    def test_launcher_refuses_non_utf8_stdin_without_a_preview_or_traceback(self) -> None:
        """The real process stream may use surrogateescape, so test bytes at the launcher."""
        launcher = MODULE_DIR.parent / "jira"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C",
            "LANG": "C",
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "alex@example.com",
            "JIRA_API_TOKEN": "synthetic-token",
            "JIRA_PROJECTS": "APP",
        }
        result = subprocess.run(
            [str(launcher), "comment", "APP-252", "--body", "-"],
            input=b"\xff", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        message = result.stderr.decode("utf-8")
        self.assertIn("standard input is not UTF-8 text", message)
        self.assertNotIn("Traceback", message)

    def test_markdown_to_adf_bounds_nested_list_depth(self) -> None:
        """A thousand indent steps must flatten onto the deepest list, not overflow the stack."""
        source = "\n".join("  " * level + "- item %d" % level for level in range(1000))
        document = self.module.markdown_to_adf(source)

        def list_depth(node: dict, depth: int = 0) -> int:
            here = depth + (1 if node.get("type") in ("bulletList", "orderedList") else 0)
            return max([here] + [
                list_depth(child, here) for child in node.get("content") or []
                if isinstance(child, dict)
            ])

        self.assertEqual(list_depth(document), self.module.MAX_LIST_DEPTH)
        rendered = self.collect_text(document)
        for level in (0, 7, 8, 999):
            self.assertIn("item %d" % level, rendered, level)

    def test_stdin_is_refused_when_nothing_is_piped_in(self) -> None:
        args = SimpleNamespace(description="-", description_file=None)
        with patch.object(self.module.sys, "stdin", SimpleNamespace(isatty=lambda: True)):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.argument_text(args, "description")
        self.assertIn("nothing is piped in", str(error.exception))

    def test_comment_reads_the_body_from_a_file_and_previews_the_resolved_text(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "body.md"
            page.write_text("Progress **update**.", encoding="utf-8")
            args = SimpleNamespace(
                issue_key="APP-252", body=None, body_file=str(page), confirm=False, json=False,
            )
            output = io.StringIO()
            with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                    redirect_stdout(output):
                self.module.command_comment(args, self.profile)
        # The preview must show the text that will be sent, never the flag value.
        self.assertIn("Progress **update**.", output.getvalue())

    def test_comment_refuses_when_no_body_source_is_given(self) -> None:
        args = SimpleNamespace(issue_key="APP-252", body=None, body_file=None, confirm=False, json=False)
        with patch.object(self.module, "request", side_effect=AssertionError("live write")):
            with self.assertRaises(self.module.JiraError) as error:
                self.module.command_comment(args, self.profile)
        for flag in ("--body", "--body-file", "--body -"):
            self.assertIn(flag, str(error.exception))

    def test_edit_accepts_a_description_file_as_its_only_field(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "d.md"
            page.write_text("# Rewritten", encoding="utf-8")
            args = SimpleNamespace(
                issue_key="APP-252", summary=None, description=None, description_file=str(page),
                clear_description=False, confirm=False, json=False,
            )
            output = io.StringIO()
            with patch.object(self.module, "request", side_effect=AssertionError("live write")), \
                    redirect_stdout(output):
                self.module.command_edit(args, self.profile)
        self.assertIn("Rewritten", output.getvalue())

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

        args = SimpleNamespace(project="APP", issue_type="Task", freeform=True, summary="Create this", description=None, epic="APP-10", epic_field=None, confirm=True, json=True)
        with patch.object(self.module, "request", side_effect=fake_request), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
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
