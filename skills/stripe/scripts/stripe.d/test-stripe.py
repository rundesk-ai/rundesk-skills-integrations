#!/usr/bin/env python3
"""Offline tests for stripe.d/stripe.py."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parent
SCRIPT = MODULE_DIR / "stripe.py"


def load_module():
    spec = importlib.util.spec_from_file_location("stripe_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StripeProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_get_profile_maps_env(self) -> None:
        env = {
            "STRIPE_PROFILES": "example,other",
            "STRIPE_EXAMPLE_KEY": "rk_live_synthetic",
            "STRIPE_EXAMPLE_LABEL": "Example Inc",
            "STRIPE_EXAMPLE_ACCOUNT": "acct_synthetic1",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["example", "other"], self.module.configured_profile_names())
            profile = self.module.get_profile("example")
        self.assertEqual("rk_live_synthetic", profile.key)
        self.assertEqual("Example Inc", profile.label)
        self.assertEqual("acct_synthetic1", profile.account)
        self.assertEqual("live", profile.mode)
        self.assertEqual("restricted", profile.key_kind)

    def test_profile_name_with_hyphen_maps_to_underscore_env(self) -> None:
        env = {"STRIPE_PROFILES": "platform-sub", "STRIPE_PLATFORM_SUB_KEY": "rk_test_synthetic"}
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("platform-sub")
        self.assertEqual("rk_test_synthetic", profile.key)
        self.assertEqual("test", profile.mode)

    def test_connected_account_sets_stripe_account_header(self) -> None:
        profile = self.module.Profile(
            name="sub", key="rk_live_synthetic", account="acct_synthetic1",
            label="Sub", api_version="",
        )
        headers = profile.auth_headers()
        self.assertEqual("acct_synthetic1", headers["Stripe-Account"])
        self.assertNotIn("Stripe-Version", headers)

    def test_own_account_profile_omits_stripe_account_header(self) -> None:
        profile = self.module.Profile(
            name="own", key="rk_live_synthetic", account="", label="Own", api_version="2026-01-01",
        )
        headers = profile.auth_headers()
        self.assertNotIn("Stripe-Account", headers)
        self.assertEqual("2026-01-01", headers["Stripe-Version"])

    def test_publishable_key_is_rejected(self) -> None:
        with patch.dict(os.environ, {"STRIPE_EXAMPLE_KEY": "pk_live_synthetic"}, clear=True):
            with self.assertRaises(self.module.StripeError):
                self.module.get_profile("example")

    def test_missing_key_is_reported_by_variable_name(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.module.StripeError) as raised:
                self.module.get_profile("example")
        self.assertIn("STRIPE_EXAMPLE_KEY", str(raised.exception))

    def test_invalid_connected_account_is_rejected(self) -> None:
        env = {"STRIPE_EXAMPLE_KEY": "rk_live_synthetic", "STRIPE_EXAMPLE_ACCOUNT": "not-an-account"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(self.module.StripeError):
                self.module.get_profile("example")

    def test_discovery_ignores_conventional_single_account_variables(self) -> None:
        env = {"STRIPE_API_KEY": "rk_live_synthetic", "STRIPE_ACME_KEY": "rk_live_synthetic"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["acme"], self.module.discovered_profile_names())

    def test_ambiguous_profile_selection_is_refused(self) -> None:
        env = {"STRIPE_PROFILES": "one,two"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(self.module.StripeError):
                self.module.selected_profile_name(SimpleNamespace(profile=None))

    def test_env_file_permission_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "env"
            env_file.write_text("STRIPE_PROFILES=example\n", encoding="utf-8")
            env_file.chmod(0o644)
            err = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stderr(err):
                self.module.load_dotenv(env_file)
            self.assertIn("chmod 600", err.getvalue())


class StripeFormattingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_two_decimal_currency_converts_from_minor_units(self) -> None:
        self.assertEqual("12.34", self.module.format_amount(1234, "usd"))

    def test_zero_decimal_currency_is_not_divided(self) -> None:
        self.assertEqual("1234", self.module.format_amount(1234, "jpy"))

    def test_three_decimal_currency_uses_three_places(self) -> None:
        self.assertEqual("1.234", self.module.format_amount(1234, "kwd"))

    def test_negative_amount_keeps_sign(self) -> None:
        self.assertEqual("-12.34", self.module.format_amount(-1234, "usd"))

    def test_compact_date_renders_unix_timestamp_as_utc(self) -> None:
        self.assertEqual("2026-07-01 00:00", self.module.compact_date(1782864000))

    def test_parse_day_rejects_non_iso_dates(self) -> None:
        with self.assertRaises(self.module.StripeError):
            self.module.parse_day("07/01/2026", "start")

    def test_text_redacts_email_and_ip(self) -> None:
        rendered = self.module.text("buyer@example.test from 203.0.113.7")
        self.assertIn("[redacted-email]", rendered)
        self.assertIn("[redacted-ip]", rendered)
        self.assertNotIn("buyer@example.test", rendered)

    def test_window_bounds_requires_both_endpoints(self) -> None:
        args = SimpleNamespace(start="2026-07-01", end=None, days=30)
        with self.assertRaises(self.module.StripeError):
            self.module.window_bounds(args)

    def test_window_bounds_rejects_reversed_range(self) -> None:
        args = SimpleNamespace(start="2026-08-01", end="2026-07-01", days=30)
        with self.assertRaises(self.module.StripeError):
            self.module.window_bounds(args)


class StripeCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example", key="rk_live_synthetic", account="", label="Example", api_version="",
        )

    def run_command(self, handler, args, request_results):
        calls = []

        def fake_request(profile, method, path, params=None, form=None, retries=2):
            calls.append(SimpleNamespace(method=method, path=path, params=params, form=form))
            return request_results.pop(0)

        buf, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "request", side_effect=fake_request
        ):
            with redirect_stdout(buf), redirect_stderr(err):
                code = handler(args)
        return code, buf.getvalue(), err.getvalue(), calls

    def test_balance_reports_available_and_pending_per_currency(self) -> None:
        payload = {
            "available": [{"currency": "usd", "amount": 250000}, {"currency": "jpy", "amount": 5000}],
            "pending": [{"currency": "usd", "amount": 12500}],
        }
        args = SimpleNamespace(profile="example", all_profiles=False, json=False)
        code, out, _, _ = self.run_command(self.module.command_balance, args, [payload])
        self.assertEqual(0, code)
        self.assertIn("usd,2500.00,125.00", out)
        self.assertIn("jpy,5000,-", out)

    def test_revenue_groups_by_currency_and_type_with_totals(self) -> None:
        payload = {
            "has_more": False,
            "data": [
                {"currency": "usd", "type": "charge", "amount": 10000, "fee": 320, "net": 9680},
                {"currency": "usd", "type": "charge", "amount": 5000, "fee": 175, "net": 4825},
                {"currency": "usd", "type": "refund", "amount": -2000, "fee": 0, "net": -2000},
            ],
        }
        args = SimpleNamespace(
            profile="example", all_profiles=False, json=False,
            days=30, start=None, end=None, limit=1000,
        )
        code, out, _, _ = self.run_command(self.module.command_revenue, args, [payload])
        self.assertEqual(0, code)
        self.assertIn("usd,charge,2,150.00,4.95,145.05", out)
        self.assertIn("usd,refund,1,-20.00,0.00,-20.00", out)
        self.assertIn("usd,TOTAL,3,130.00,4.95,125.05", out)

    def test_payouts_bound_window_and_limit(self) -> None:
        payload = {
            "has_more": False,
            "data": [
                {
                    "id": "po_synthetic1", "amount": 480000, "currency": "usd", "status": "paid",
                    "arrival_date": 1782864000, "created": 1782432000, "method": "standard",
                    "type": "bank_account", "description": "STRIPE PAYOUT",
                }
            ],
        }
        args = SimpleNamespace(
            profile="example", days=30, status=None, limit=25, json=False,
        )
        code, out, _, calls = self.run_command(self.module.command_payouts, args, [payload])
        self.assertEqual(0, code)
        self.assertIn("po_synthetic1,4800.00,usd,paid", out)
        self.assertIn("created[gte]", calls[0].params)
        self.assertEqual(25, calls[0].params["limit"])

    def test_subscriptions_read_price_from_first_item(self) -> None:
        payload = {
            "has_more": False,
            "data": [
                {
                    "id": "sub_synthetic1", "customer": "cus_synthetic1", "status": "active",
                    "current_period_end": 1782864000, "cancel_at_period_end": False,
                    "items": {
                        "data": [
                            {
                                "price": {
                                    "currency": "usd", "unit_amount": 4900,
                                    "recurring": {"interval": "month", "interval_count": 1},
                                }
                            }
                        ]
                    },
                }
            ],
        }
        args = SimpleNamespace(profile="example", status="active", limit=25, json=False)
        code, out, _, calls = self.run_command(self.module.command_subscriptions, args, [payload])
        self.assertEqual(0, code)
        self.assertIn("sub_synthetic1,cus_synthetic1,active,49.00,usd,month", out)
        self.assertEqual("active", calls[0].params["status"])

    def test_truncation_is_reported_rather_than_silent(self) -> None:
        payload = {
            "has_more": True,
            "data": [{"id": f"ch_{index}", "currency": "usd", "amount": 100} for index in range(2)],
        }
        args = SimpleNamespace(profile="example", days=7, limit=2, json=False)
        code, _, err, _ = self.run_command(self.module.command_charges, args, [payload])
        self.assertEqual(0, code)
        self.assertIn("more charges exist", err)

    def test_customer_lookup_by_email_uses_list_filter(self) -> None:
        customers = {"has_more": False, "data": [{"id": "cus_synthetic1", "name": "Example Buyer"}]}
        subscriptions = {"has_more": False, "data": []}
        args = SimpleNamespace(
            profile="example", customer="buyer@example.test", subscription_limit=10, json=False,
        )
        code, out, _, calls = self.run_command(
            self.module.command_customer, args, [customers, subscriptions]
        )
        self.assertEqual(0, code)
        self.assertEqual("buyer@example.test", calls[0].params["email"])
        self.assertIn("cus_synthetic1", out)

    def test_customer_reference_must_be_id_or_email(self) -> None:
        args = SimpleNamespace(profile="example", customer="Example Buyer", subscription_limit=10, json=False)
        with patch.object(self.module, "get_profile", return_value=self.profile):
            with self.assertRaises(self.module.StripeError):
                self.module.command_customer(args)

    def test_report_run_posts_bracketed_interval_parameters(self) -> None:
        created = {"id": "frr_synthetic1", "status": "succeeded", "result": {"id": "file_synthetic1", "size": 42}}
        args = SimpleNamespace(
            profile="example", type="balance.summary.1", days=30,
            start="2026-07-01", end="2026-08-01", timezone=None, currency=None,
            column=None, out=None, timeout=180, poll_interval=5, json=False,
        )
        code, out, _, calls = self.run_command(self.module.command_report_run, args, [created])
        self.assertEqual(0, code)
        self.assertEqual("POST", calls[0].method)
        self.assertEqual("reporting/report_runs", calls[0].path)
        self.assertEqual("balance.summary.1", calls[0].form["report_type"])
        self.assertEqual(1782864000, calls[0].form["parameters[interval_start]"])
        self.assertEqual(1785542400, calls[0].form["parameters[interval_end]"])
        self.assertIn("frr_synthetic1", out)

    def test_report_run_polls_until_terminal_status(self) -> None:
        pending = {"id": "frr_synthetic1", "status": "pending", "result": None}
        done = {"id": "frr_synthetic1", "status": "succeeded", "result": {"id": "file_synthetic1"}}
        args = SimpleNamespace(
            profile="example", type="balance.summary.1", days=30,
            start=None, end=None, timezone=None, currency=None, column=None,
            out=None, timeout=180, poll_interval=0, json=False,
        )
        with patch.object(self.module.time, "sleep"):
            code, out, _, calls = self.run_command(
                self.module.command_report_run, args, [pending, done]
            )
        self.assertEqual(0, code)
        self.assertEqual("GET", calls[1].method)
        self.assertEqual("reporting/report_runs/frr_synthetic1", calls[1].path)
        self.assertIn("succeeded", out)

    def test_failed_report_run_exits_non_zero(self) -> None:
        failed = {"id": "frr_synthetic1", "status": "failed", "result": None}
        args = SimpleNamespace(
            profile="example", type="balance.summary.1", days=30,
            start=None, end=None, timezone=None, currency=None, column=None,
            out=None, timeout=180, poll_interval=0, json=False,
        )
        code, _, err, _ = self.run_command(self.module.command_report_run, args, [failed])
        self.assertEqual(1, code)
        self.assertIn("failed", err)


class StripeTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example", key="rk_live_synthetic", account="", label="Example", api_version="",
        )

    def test_error_message_extracts_stripe_error_object(self) -> None:
        raw = '{"error": {"message": "Invalid API Key provided", "type": "invalid_request_error"}}'
        self.assertIn("Invalid API Key provided", self.module.error_message(raw))

    def test_download_refuses_unexpected_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.csv"
            with self.assertRaises(self.module.StripeError):
                self.module.download_report(
                    self.profile, "https://attacker.example.test/v1/files/f/contents", destination
                )

    def test_download_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.csv"
            destination.write_text("existing", encoding="utf-8")
            with self.assertRaises(self.module.StripeError):
                self.module.download_report(
                    self.profile, "https://files.stripe.com/v1/files/f/contents", destination
                )
            self.assertEqual("existing", destination.read_text(encoding="utf-8"))

    def test_no_command_writes_to_stripe_except_report_runs(self) -> None:
        parser = self.module.build_parser()
        writing = set()
        for name, value in vars(self.module).items():
            if name.startswith("command_") and callable(value):
                source = value.__code__.co_consts
                if any(const == "POST" for const in source if isinstance(const, str)):
                    writing.add(name)
        self.assertEqual({"command_report_run"}, writing)
        self.assertIsNotNone(parser)

    def test_main_help_exits_clean(self) -> None:
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.module.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("revenue", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
