#!/usr/bin/env python3
"""Offline tests for the read-only Grafana Loki integration."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("grafana_command", HERE / "grafana.py")
grafana = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = grafana
SPEC.loader.exec_module(grafana)


class GrafanaCommand(unittest.TestCase):
    def run_main(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, env or {}, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = grafana.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def profile(self) -> grafana.Profile:
        return grafana.Profile("example", "https://grafana.example.test", "secret-token",
                               "loki-main", "Example")

    def test_profiles_needs_no_credentials_or_network(self):
        with unittest.mock.patch.object(grafana, "api_get") as called:
            code, out, err = self.run_main("profiles")
        self.assertEqual(0, code)
        self.assertIn("No Grafana profiles configured", out)
        self.assertEqual("", err)
        called.assert_not_called()

    def test_rundesk_account_values_win_and_named_accounts_never_fall_back(self):
        env = {
            "GRAFANA_BASE_URL__PROD": "https://prod.example.test",
            "GRAFANA_PROD_BASE_URL": "https://legacy.example.test",
            "GRAFANA_BASE_URL": "https://default.example.test",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual("https://prod.example.test",
                             grafana.profile_value("prod", "GRAFANA_BASE_URL"))
            self.assertEqual("", grafana.profile_value("other", "GRAFANA_BASE_URL"))

    def test_non_https_or_path_bearing_origins_are_refused(self):
        for value in ("http://grafana.example.test", "https://user@example.test",
                      "https://grafana.example.test/explore", "https://example.test?x=1"):
            with self.subTest(value=value), self.assertRaises(grafana.GrafanaError):
                grafana.validate_origin(value)

    def test_api_allows_only_read_routes_and_refuses_cross_origin_redirects(self):
        self.assertTrue(grafana.allowed_api_path("/api/datasources"))
        self.assertTrue(grafana.allowed_api_path(
            "/api/datasources/proxy/uid/logs/loki/api/v1/query_range"))
        self.assertFalse(grafana.allowed_api_path("/api/dashboards"))
        self.assertFalse(grafana.allowed_api_path("/api/datasources-delete"))
        handler = grafana.SameOriginRedirectHandler()
        request = SimpleNamespace(full_url="https://grafana.example.test/api/datasources")
        with self.assertRaises(grafana.GrafanaError):
            handler.redirect_request(request, None, 302, "", {}, "https://evil.example.test/")

    def test_datasource_discovery_keeps_only_loki_and_reports_truncation(self):
        payload = [
            {"uid": "a", "name": "Logs A", "type": "loki"},
            {"uid": "metrics", "name": "Metrics", "type": "prometheus"},
            {"uid": "b", "name": "Logs B", "type": "loki"},
        ]
        with unittest.mock.patch.object(grafana, "api_get", return_value=payload):
            rows, more = grafana.datasource_rows(self.profile(), 1)
        self.assertEqual(["a"], [row["uid"] for row in rows])
        self.assertTrue(more)

    def test_time_ranges_are_bounded_and_explicit_ranges_need_both_ends(self):
        now = dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc)
        args = SimpleNamespace(since="1h", start=None, end=None)
        start, end = grafana.time_bounds(args, now=now)
        self.assertEqual(3600 * 1_000_000_000, end - start)
        with self.assertRaises(grafana.GrafanaError):
            grafana.time_bounds(SimpleNamespace(since="31d", start=None, end=None), now=now)
        with self.assertRaises(grafana.GrafanaError):
            grafana.time_bounds(SimpleNamespace(since=None, start="2026-08-15T00:00:00Z",
                                                end=None), now=now)
        explicit = SimpleNamespace(since=None, start="2026-08-15T00:00:00Z",
                                   end="2026-08-15T01:00:00Z")
        start, end = grafana.time_bounds(explicit, now=now)
        self.assertEqual(3600 * 1_000_000_000, end - start)

    def test_filters_are_quoted_as_data_and_error_search_is_explicit(self):
        query = grafana.filtered_query(
            '{service_name="api"}', ['timeout" |~ ".*'], ["health"], ["status=5.."], True
        )
        self.assertIn('|= "timeout\\\" |~ \\".*"', query)
        self.assertIn('!= "health"', query)
        self.assertIn('|~ "(?i)(error|exception|fatal|panic|failed)"', query)
        self.assertIn('|~ "status=5.."', query)
        with self.assertRaises(grafana.GrafanaError):
            grafana.filtered_query('{job="api"} |= "all"', [], [], [])
        with self.assertRaises(grafana.GrafanaError):
            grafana.filtered_query("{}", [], [], [])
        for selector in ('{job=~".*"}', '{job!="impossible"}', '{job=""}'):
            with self.subTest(selector=selector), self.assertRaises(grafana.GrafanaError):
                grafana.filtered_query(selector, [], [], [])

    def test_raw_queries_also_require_an_exact_nonempty_label_match(self):
        self.assertEqual('{service="api"}', grafana.selector_from_query(
            ' {service="api"} | json | status >= 500'))
        for query in ("{}", '{job=~".*"}', 'sum(rate({job="api"}[5m]))'):
            with self.subTest(query=query), self.assertRaises(grafana.GrafanaError):
                grafana.require_bounded_selector(grafana.selector_from_query(query))

    def test_datasource_must_be_a_visible_loki_source(self):
        with unittest.mock.patch.object(grafana, "api_get", return_value=[
                {"uid": "logs", "type": "loki"}, {"uid": "metrics", "type": "prometheus"}]):
            grafana.verify_loki_datasource(self.profile(), "logs")
            with self.assertRaises(grafana.GrafanaError):
                grafana.verify_loki_datasource(self.profile(), "metrics")

    def test_log_reads_use_the_grafana_proxy_with_bounds(self):
        args = SimpleNamespace(datasource="loki-main", limit=25, since="5m", start=None, end=None,
                               direction="backward", json=False)
        data = {"result": [], "resultType": "streams"}
        replies = [[{"uid": "loki-main", "type": "loki"}],
                   {"status": "success", "data": data}]
        with unittest.mock.patch.object(grafana, "api_get", side_effect=replies) as called:
            with contextlib.redirect_stdout(io.StringIO()):
                grafana.print_logs(self.profile(), args, '{job="api"}')
        path = called.call_args_list[1].args[1]
        params = called.call_args_list[1].args[2]
        self.assertEqual("/api/datasources/proxy/uid/loki-main/loki/api/v1/query_range", path)
        self.assertEqual('{job="api"}', params["query"])
        self.assertEqual(25, params["limit"])
        self.assertLess(params["start"], params["end"])

    def test_text_output_redacts_common_secret_shapes(self):
        line = "Authorization: Bearer abc123 password=hunter2 api_key=key-123 ordinary=value"
        redacted = grafana.compact_line(line)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("key-123", redacted)
        self.assertIn("ordinary=value", redacted)
        self.assertEqual(3, redacted.count("[REDACTED]"))

    def test_streams_are_flattened_newest_first(self):
        rows = grafana.flattened_streams({"result": [
            {"stream": {"service": "api"}, "values": [["2", "later"], ["1", "earlier"]]}
        ]})
        self.assertEqual(["later", "earlier"], [row["line"] for row in rows])
        with self.assertRaises(grafana.GrafanaError):
            grafana.flattened_streams({"result": [
                {"stream": {}, "values": [["not-a-time", "broken"]]}
            ]})

    def test_help_is_credential_free(self):
        with self.assertRaises(SystemExit) as ended:
            with contextlib.redirect_stdout(io.StringIO()):
                grafana.parser().parse_args(["--help"])
        self.assertEqual(0, ended.exception.code)

    def test_missing_credentials_fail_without_printing_values(self):
        code, out, err = self.run_main("datasources")
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("Missing GRAFANA_BASE_URL", err)

    def test_raw_json_is_emitted_only_when_requested(self):
        args = SimpleNamespace(datasource="loki-main", limit=2, since="1m", start=None, end=None,
                               direction="backward", json=True)
        data = {"resultType": "streams", "result": [
            {"stream": {"job": "api"}, "values": [["1", "raw secret"]]}
        ]}
        replies = [[{"uid": "loki-main", "type": "loki"}],
                   {"status": "success", "data": data}]
        with unittest.mock.patch.object(grafana, "api_get", side_effect=replies):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                grafana.print_logs(self.profile(), args, '{job="api"}')
        payload = json.loads(out.getvalue())
        self.assertEqual("raw secret", payload["data"]["result"][0]["values"][0][1])


if __name__ == "__main__":
    unittest.main()
