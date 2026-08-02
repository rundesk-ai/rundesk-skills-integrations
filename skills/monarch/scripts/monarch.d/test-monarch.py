#!/usr/bin/env python3
"""Offline tests for monarch.d/monarch.py. Every network boundary is replaced."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parent
SCRIPT = MODULE_DIR / "monarch.py"

# RFC 6238 Appendix B, SHA-1 column. The seed is the published ASCII string.
RFC6238_SEED = base64.b32encode(b"12345678901234567890").decode("ascii")
RFC6238_VECTORS = (
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
)

FIXTURE_TOKEN = "synthetic-session-token"
FIXTURE_PASSWORD = "synthetic-password"


def load_module():
    spec = importlib.util.spec_from_file_location("monarch_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self._status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self._status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHTTP:
    """Stands in for `open_url`, the module's single network boundary."""

    def __init__(self, exchanges):
        self.exchanges = list(exchanges)
        self.calls = []

    def __call__(self, req, timeout):
        payload = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls.append(
            SimpleNamespace(url=req.full_url, headers=dict(req.header_items()), payload=payload)
        )
        if not self.exchanges:
            raise AssertionError(f"unexpected request to {req.full_url}")
        status, body = self.exchanges.pop(0)
        raw = json.dumps(body)
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "error", {}, io.BytesIO(raw.encode("utf-8"))
            )
        return FakeResponse(status, raw)

    def header(self, index: int, name: str) -> str:
        lowered = {key.lower(): value for key, value in self.calls[index].headers.items()}
        return lowered.get(name.lower(), "")


@contextmanager
def isolated_home():
    """Point every credential, session, and state path at a throwaway directory."""
    with tempfile.TemporaryDirectory(prefix="monarch-test-") as temporary:
        root = Path(temporary)
        overrides = {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
        }
        with patch.dict(os.environ, overrides, clear=False):
            yield root


class MonarchTotpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_rfc6238_published_vectors(self) -> None:
        for moment, expected in RFC6238_VECTORS:
            with self.subTest(t=moment):
                self.assertEqual(
                    expected, self.module.totp(RFC6238_SEED, at=moment, digits=8)
                )

    def test_six_digit_code_is_the_vector_suffix(self) -> None:
        self.assertEqual("287082", self.module.totp(RFC6238_SEED, at=59))

    def test_secret_spacing_and_case_are_tolerated(self) -> None:
        spaced = " ".join(RFC6238_SEED[index : index + 4] for index in range(0, len(RFC6238_SEED), 4))
        self.assertEqual(
            self.module.totp(RFC6238_SEED, at=59), self.module.totp(spaced.lower(), at=59)
        )

    def test_unpadded_secret_is_accepted(self) -> None:
        self.assertEqual(6, len(self.module.totp("JBSWY3DPEHPK3PXP", at=59)))

    def test_invalid_base32_is_reported_as_a_setup_mistake(self) -> None:
        with self.assertRaises(self.module.MonarchError) as raised:
            self.module.totp("not base32 !!", at=59)
        self.assertIn("base32", str(raised.exception))

    def test_code_advances_with_the_thirty_second_step(self) -> None:
        self.assertEqual(self.module.totp(RFC6238_SEED, at=0), self.module.totp(RFC6238_SEED, at=29))
        self.assertNotEqual(
            self.module.totp(RFC6238_SEED, at=0), self.module.totp(RFC6238_SEED, at=30)
        )


class MonarchProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_get_profile_maps_env(self) -> None:
        env = {
            "MONARCH_PROFILES": "household,parents",
            "MONARCH_HOUSEHOLD_EMAIL": "agent@example.test",
            "MONARCH_HOUSEHOLD_PASSWORD": FIXTURE_PASSWORD,
            "MONARCH_HOUSEHOLD_MFA_SECRET": RFC6238_SEED,
            "MONARCH_HOUSEHOLD_LABEL": "Example Household",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["household", "parents"], self.module.configured_profile_names())
            profile = self.module.get_profile("household")
        self.assertEqual("agent@example.test", profile.email)
        self.assertEqual("Example Household", profile.label)
        self.assertTrue(profile.has_mfa)

    def test_profile_name_with_hyphen_maps_to_underscore_env(self) -> None:
        env = {
            "MONARCH_JOINT_ACCOUNT_EMAIL": "agent@example.test",
            "MONARCH_JOINT_ACCOUNT_PASSWORD": FIXTURE_PASSWORD,
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("joint-account")
        self.assertEqual("agent@example.test", profile.email)

    def test_missing_password_is_reported_by_variable_name(self) -> None:
        with patch.dict(os.environ, {"MONARCH_HOUSEHOLD_EMAIL": "agent@example.test"}, clear=True):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.get_profile("household")
        self.assertIn("MONARCH_HOUSEHOLD_PASSWORD", str(raised.exception))

    def test_ambiguous_profile_selection_is_refused(self) -> None:
        with patch.dict(os.environ, {"MONARCH_PROFILES": "one,two"}, clear=True):
            with self.assertRaises(self.module.MonarchError):
                self.module.selected_profile_name(SimpleNamespace(profile=None))

    def test_profiles_are_discovered_without_an_explicit_list(self) -> None:
        env = {
            "MONARCH_HOUSEHOLD_EMAIL": "agent@example.test",
            "MONARCH_PARENTS_PASSWORD": FIXTURE_PASSWORD,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["household", "parents"], self.module.discovered_profile_names())

    def test_masked_email_hides_the_local_part(self) -> None:
        self.assertEqual("a***@example.test", self.module.mask_email("agent@example.test"))
        self.assertEqual("***", self.module.mask_email("no-at-sign"))

    def test_profiles_command_prints_no_secret(self) -> None:
        env = {
            "MONARCH_PROFILES": "demo",
            "MONARCH_DEMO_EMAIL": "agent@example.test",
            "MONARCH_DEMO_PASSWORD": FIXTURE_PASSWORD,
        }
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(buf):
            code = self.module.command_profiles(SimpleNamespace())
        out = buf.getvalue()
        self.assertEqual(0, code)
        self.assertIn("demo", out)
        self.assertIn("a***@example.test", out)
        self.assertIn("mfa=no", out)
        self.assertNotIn(FIXTURE_PASSWORD, out)
        self.assertNotIn("agent@example.test", out)

    def test_profiles_command_reports_a_broken_profile_without_failing(self) -> None:
        with patch.dict(os.environ, {"MONARCH_PROFILES": "demo"}, clear=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.command_profiles(SimpleNamespace())
        self.assertEqual(0, code)
        self.assertIn("MONARCH_DEMO_EMAIL", buf.getvalue())

    def test_env_file_permission_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "env"
            env_file.write_text("MONARCH_PROFILES=demo\n", encoding="utf-8")
            env_file.chmod(0o644)
            err = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stderr(err):
                self.module.load_dotenv(env_file)
            self.assertIn("chmod 600", err.getvalue())

    def test_env_file_resolution_prefers_the_isolated_path(self) -> None:
        env = {"XDG_CONFIG_HOME": "/nonexistent-config"}
        with patch.dict(os.environ, env, clear=True):
            candidates = [str(path) for path in self.module.default_env_candidates()]
        self.assertEqual(
            "/nonexistent-config/rundesk/integrations/monarch/env", candidates[0]
        )
        self.assertEqual("/nonexistent-config/monarch/env", candidates[-1])


class MonarchFormattingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_account_mask_keeps_only_the_last_two_digits(self) -> None:
        self.assertEqual("····34", self.module.mask_account("1234"))
        self.assertEqual("····34", self.module.mask_account("**** **** **** 1234"))
        self.assertEqual("-", self.module.mask_account(None))

    def test_amounts_render_with_two_places(self) -> None:
        self.assertEqual("1234.50", self.module.format_amount(1234.5))
        self.assertEqual("-42.00", self.module.format_amount(-42))
        self.assertEqual("-", self.module.format_amount(None))

    def test_quantity_drops_trailing_zeros_without_scientific_notation(self) -> None:
        self.assertEqual("12.5", self.module.format_quantity("12.500"))
        self.assertEqual("100", self.module.format_quantity("100.00"))

    def test_compact_date_handles_dates_and_timestamps(self) -> None:
        self.assertEqual("2026-08-01", self.module.compact_date("2026-08-01"))
        self.assertEqual("2026-08-01 09:30", self.module.compact_date("2026-08-01T09:30:00Z"))

    def test_month_bounds_cover_the_whole_month(self) -> None:
        import datetime

        self.assertEqual(
            ("2026-02-01", "2026-02-28"),
            self.module.month_bounds(datetime.date(2026, 2, 1)),
        )

    def test_invalid_month_is_rejected(self) -> None:
        with self.assertRaises(self.module.MonarchError):
            self.module.parse_month("2026-13")

    def test_zero_days_is_rejected(self) -> None:
        with self.assertRaises(self.module.MonarchError):
            self.module.window_days(0)

    def test_truncation_note_goes_to_stderr(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.module.note_truncation(25, 137, "transactions")
        self.assertIn("showing 25 of 137 transactions", err.getvalue())

    def test_no_note_when_everything_was_shown(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.module.note_truncation(4, 4, "accounts")
        self.assertEqual("", err.getvalue())


class MonarchTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="household",
            email="agent@example.test",
            password=FIXTURE_PASSWORD,
            mfa_secret="",
            label="Example Household",
        )
        self.mfa_profile = self.module.Profile(
            name="household",
            email="agent@example.test",
            password=FIXTURE_PASSWORD,
            mfa_secret=RFC6238_SEED,
            label="Example Household",
        )

    def test_login_posts_the_rest_contract_and_returns_the_token(self) -> None:
        http = FakeHTTP([(200, {"token": FIXTURE_TOKEN, "tokenExpiration": None})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            token = self.module.login(self.profile, "device-0001")

        self.assertEqual(FIXTURE_TOKEN, token)
        self.assertEqual("https://api.monarch.com/auth/login/", http.calls[0].url)
        self.assertEqual(
            {
                "username": "agent@example.test",
                "password": FIXTURE_PASSWORD,
                "trusted_device": True,
                "supports_mfa": True,
            },
            http.calls[0].payload,
        )
        self.assertEqual("web", http.header(0, "Client-Platform"))
        self.assertEqual("device-0001", http.header(0, "device-uuid"))
        self.assertEqual("application/json", http.header(0, "Content-Type"))
        self.assertEqual("", http.header(0, "Authorization"))

    def test_login_sends_a_totp_when_a_secret_is_configured(self) -> None:
        http = FakeHTTP([(200, {"token": FIXTURE_TOKEN})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            self.module.login(self.mfa_profile, "device-0001")
        code = http.calls[0].payload["totp"]
        self.assertRegex(code, r"^\d{6}$")

    def test_mfa_challenge_without_a_secret_names_the_variable_to_set(self) -> None:
        http = FakeHTTP([(403, {"error_code": "MFA_REQUIRED"})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.login(self.profile, "device-0001")
        message = str(raised.exception)
        self.assertIn("MONARCH_HOUSEHOLD_MFA_SECRET", message)
        self.assertNotIn(FIXTURE_PASSWORD, message)

    def test_mfa_challenge_is_answered_once_with_a_generated_code(self) -> None:
        http = FakeHTTP(
            [(200, {"error_code": "MFA_REQUIRED"}), (200, {"token": FIXTURE_TOKEN})]
        )
        with isolated_home(), patch.object(self.module, "open_url", http):
            token = self.module.login(self.mfa_profile, "device-0001")
        self.assertEqual(FIXTURE_TOKEN, token)
        self.assertEqual(2, len(http.calls))
        self.assertRegex(http.calls[1].payload["totp"], r"^\d{6}$")

    def test_captcha_challenge_is_reported_as_itself(self) -> None:
        http = FakeHTTP([(403, {"error_code": "CAPTCHA_REQUIRED"})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.login(self.mfa_profile, "device-0001")
        self.assertIn("CAPTCHA", str(raised.exception))

    def test_short_lived_feature_token_is_refused(self) -> None:
        http = FakeHTTP([(200, {"token": "header.payload.signature"})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.login(self.profile, "device-0001")
        self.assertIn("short-lived", str(raised.exception))

    def test_bad_password_reports_the_api_detail_not_a_traceback(self) -> None:
        http = FakeHTTP([(400, {"detail": "Invalid email or password"})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.login(self.profile, "device-0001")
        self.assertIn("Invalid email or password", str(raised.exception))

    def test_session_is_cached_at_0600_and_reused_without_a_second_login(self) -> None:
        http = FakeHTTP(
            [
                (200, {"token": FIXTURE_TOKEN}),
                (200, {"data": {"accounts": []}}),
                (200, {"data": {"accounts": []}}),
            ]
        )
        with isolated_home(), patch.object(self.module, "open_url", http):
            self.module._TOKENS.clear()
            self.module.graphql(self.profile, "GetAccounts", self.module.QUERY_ACCOUNTS)
            cached = self.module.session_path(self.profile)
            self.assertEqual(0o600, cached.stat().st_mode & 0o777)
            self.assertEqual(FIXTURE_TOKEN, self.module.read_session(self.profile))

            # A fresh process would find the cached token and skip the login round trip.
            self.module._TOKENS.clear()
            self.module.graphql(self.profile, "GetAccounts", self.module.QUERY_ACCOUNTS)

        urls = [call.url for call in http.calls]
        self.assertEqual(1, urls.count("https://api.monarch.com/auth/login/"))
        self.assertEqual(2, urls.count("https://api.monarch.com/graphql"))

    def test_device_id_is_generated_once_and_persisted_at_0600(self) -> None:
        with isolated_home():
            first = self.module.device_uuid()
            second = self.module.device_uuid()
            stored = self.module.state_dir() / "device.json"
            self.assertEqual(first, second)
            self.assertEqual(0o600, stored.stat().st_mode & 0o777)

    def test_expired_session_forces_exactly_one_relogin_and_one_retry(self) -> None:
        http = FakeHTTP(
            [
                (401, {"detail": "Invalid token"}),
                (200, {"token": "second-synthetic-token"}),
                (200, {"data": {"accounts": [{"id": "1"}]}}),
            ]
        )
        with isolated_home(), patch.object(self.module, "open_url", http):
            self.module._TOKENS.clear()
            self.module.write_session(self.profile, FIXTURE_TOKEN, "device-0001")
            data = self.module.graphql(self.profile, "GetAccounts", self.module.QUERY_ACCOUNTS)

        self.assertEqual([{"id": "1"}], data["accounts"])
        urls = [call.url for call in http.calls]
        self.assertEqual(
            [
                "https://api.monarch.com/graphql",
                "https://api.monarch.com/auth/login/",
                "https://api.monarch.com/graphql",
            ],
            urls,
        )
        self.assertEqual("Token " + FIXTURE_TOKEN, http.header(0, "Authorization"))
        self.assertEqual("Token second-synthetic-token", http.header(2, "Authorization"))

    def test_a_persistent_401_gives_up_rather_than_looping(self) -> None:
        http = FakeHTTP(
            [
                (401, {"detail": "Invalid token"}),
                (200, {"token": FIXTURE_TOKEN}),
                (401, {"detail": "Invalid token"}),
            ]
        )
        with isolated_home(), patch.object(self.module, "open_url", http):
            self.module._TOKENS.clear()
            self.module.write_session(self.profile, "stale-token", "device-0001")
            with self.assertRaises(self.module.MonarchError):
                self.module.graphql(self.profile, "GetAccounts", self.module.QUERY_ACCOUNTS)
        self.assertEqual(3, len(http.calls))

    def test_graphql_errors_inside_a_200_body_are_raised(self) -> None:
        http = FakeHTTP(
            [
                (200, {"token": FIXTURE_TOKEN}),
                (200, {"errors": [{"message": "Cannot query field 'nope'"}], "data": None}),
            ]
        )
        with isolated_home(), patch.object(self.module, "open_url", http):
            self.module._TOKENS.clear()
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.graphql(self.profile, "GetAccounts", self.module.QUERY_ACCOUNTS)
        self.assertIn("Cannot query field 'nope'", str(raised.exception))

    def test_graphql_sends_the_operation_name_and_bearer_scheme(self) -> None:
        http = FakeHTTP([(200, {"token": FIXTURE_TOKEN}), (200, {"data": {"accounts": []}})])
        with isolated_home(), patch.object(self.module, "open_url", http):
            self.module._TOKENS.clear()
            self.module.graphql(
                self.profile, "GetAccounts", self.module.QUERY_ACCOUNTS, {"x": 1}
            )
        payload = http.calls[1].payload
        self.assertEqual("GetAccounts", payload["operationName"])
        self.assertEqual({"x": 1}, payload["variables"])
        self.assertTrue(http.header(1, "Authorization").startswith("Token "))

    def test_cross_origin_redirect_drops_the_authorization_header(self) -> None:
        handler = self.module.SameOriginRedirectHandler()
        request = self.module.urllib.request.Request(
            "https://api.monarch.com/graphql",
            data=b"{}",
            headers={"Authorization": "Token " + FIXTURE_TOKEN},
            method="POST",
        )
        redirected = handler.redirect_request(
            request, io.BytesIO(b""), 302, "Found", {}, "https://attacker.example.test/graphql"
        )
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_unreachable_host_becomes_a_monarch_error(self) -> None:
        def explode(req, timeout):
            raise urllib.error.URLError("name resolution failed")

        with isolated_home(), patch.object(self.module, "open_url", explode):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.login(self.profile, "device-0001")
        self.assertIn("name resolution failed", str(raised.exception))


class MonarchCommandTest(unittest.TestCase):
    ACCOUNTS = {
        "accounts": [
            {
                "id": "acct-1",
                "displayName": "Joint Checking",
                "mask": "4321",
                "isAsset": True,
                "displayBalance": 5210.4,
                "displayLastUpdatedAt": "2026-08-01T09:30:00Z",
                "type": {"name": "depository", "display": "Cash"},
                "subtype": {"name": "checking", "display": "Checking"},
                "institution": {"name": "Example Bank"},
            },
            {
                "id": "acct-2",
                "displayName": "Household Card",
                "mask": "**** **** **** 9876",
                "isAsset": False,
                "displayBalance": -812.15,
                "displayLastUpdatedAt": "2026-07-31T22:05:00Z",
                "type": {"name": "credit", "display": "Credit Cards"},
                "subtype": {"name": "credit_card", "display": "Credit Card"},
                "institution": {"name": "Example Card Co"},
            },
            {
                "id": "acct-3",
                "displayName": "Household Brokerage",
                "mask": "5555",
                "isAsset": True,
                "displayBalance": 91234.0,
                "displayLastUpdatedAt": "2026-08-01T06:00:00Z",
                "type": {"name": "brokerage", "display": "Investments"},
                "subtype": {"name": "brokerage", "display": "Brokerage"},
                "institution": {"name": "Example Brokerage"},
            },
        ]
    }

    CATEGORIES = {
        "categories": [
            {"id": "cat-1", "name": "Groceries", "group": {"id": "g-1", "name": "Food", "type": "expense"}},
            {"id": "cat-2", "name": "Restaurants", "group": {"id": "g-1", "name": "Food", "type": "expense"}},
            {"id": "cat-3", "name": "Paycheck", "group": {"id": "g-2", "name": "Income", "type": "income"}},
        ]
    }

    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="household",
            email="agent@example.test",
            password=FIXTURE_PASSWORD,
            mfa_secret="",
            label="Example Household",
        )

    def run_command(self, handler, args, responses):
        """Run one command with `graphql` replaced by a queue of synthetic payloads."""
        calls = []

        def fake_graphql(profile, operation, document, variables=None):
            calls.append(SimpleNamespace(operation=operation, document=document, variables=variables))
            if not responses:
                raise AssertionError(f"unexpected GraphQL call {operation}")
            payload = responses.pop(0)
            if isinstance(payload, Exception):
                raise payload
            return payload

        buf, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "graphql", side_effect=fake_graphql
        ):
            with redirect_stdout(buf), redirect_stderr(err):
                code = handler(args)
        return code, buf.getvalue(), err.getvalue(), calls

    def test_accounts_render_redacted_masks_and_balances(self) -> None:
        args = SimpleNamespace(profile="household", limit=50, json=False)
        code, out, err, calls = self.run_command(
            self.module.command_accounts, args, [self.ACCOUNTS]
        )
        self.assertEqual(0, code)
        self.assertEqual("GetAccounts", calls[0].operation)
        self.assertIn("Cash,Checking,Example Bank,Joint Checking,····21,5210.40,2026-08-01 09:30", out)
        self.assertIn("Credit Cards,Credit Card,Example Card Co,Household Card,····76,-812.15", out)
        self.assertNotIn("9876", out)
        self.assertNotIn("4321", out)
        self.assertEqual("", err)

    def test_accounts_limit_reports_what_was_hidden(self) -> None:
        args = SimpleNamespace(profile="household", limit=1, json=False)
        code, out, err, _ = self.run_command(
            self.module.command_accounts, args, [self.ACCOUNTS]
        )
        self.assertEqual(0, code)
        self.assertIn("showing 1 of 3 accounts", err)
        self.assertNotIn("showing 1 of 3", out)
        self.assertNotIn("Household Card", out)

    def test_accounts_json_is_the_raw_payload(self) -> None:
        args = SimpleNamespace(profile="household", limit=50, json=True)
        code, out, _, _ = self.run_command(
            self.module.command_accounts, args, [self.ACCOUNTS]
        )
        self.assertEqual(0, code)
        self.assertEqual(self.ACCOUNTS["accounts"], json.loads(out))

    def test_networth_reports_first_last_and_change(self) -> None:
        payload = {
            "aggregateSnapshots": [
                {"date": "2026-05-04", "balance": 100000.0, "assetsBalance": 120000.0, "liabilitiesBalance": -20000.0},
                {"date": "2026-08-02", "balance": 112500.5, "assetsBalance": 130000.5, "liabilitiesBalance": -17500.0},
            ]
        }
        args = SimpleNamespace(profile="household", days=90, json=False)
        code, out, err, calls = self.run_command(
            self.module.command_networth, args, [payload]
        )
        self.assertEqual(0, code)
        self.assertEqual("Common_GetAggregateSnapshots", calls[0].operation)
        self.assertIn("first,2026-05-04,120000.00,-20000.00,100000.00", out)
        self.assertIn("last,2026-08-02,130000.50,-17500.00,112500.50", out)
        self.assertIn("change,2026-05-04..2026-08-02,10000.50,2500.00,12500.50", out)
        self.assertEqual("", err)

    def test_networth_falls_back_when_the_split_fields_are_rejected(self) -> None:
        rejection = self.module.MonarchError(
            "Monarch Common_GetAggregateSnapshots returned errors for profile 'household': "
            "Cannot query field 'assetsBalance' on type 'Snapshot'."
        )
        fallback = {
            "aggregateSnapshots": [
                {"date": "2026-05-04", "balance": 100000.0},
                {"date": "2026-08-02", "balance": 112500.5},
            ]
        }
        args = SimpleNamespace(profile="household", days=90, json=False)
        code, out, err, calls = self.run_command(
            self.module.command_networth, args, [rejection, fallback]
        )
        self.assertEqual(0, code)
        self.assertEqual(
            ["Common_GetAggregateSnapshots", "GetAggregateSnapshots"],
            [call.operation for call in calls],
        )
        self.assertIn("asset/liability split", err)
        self.assertIn("first,2026-05-04,-,-,100000.00", out)

    def test_networth_does_not_swallow_an_unrelated_graphql_error(self) -> None:
        unrelated = self.module.MonarchError("Monarch returned errors: Rate limited.")
        args = SimpleNamespace(profile="household", days=90, json=False)
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "graphql", side_effect=unrelated
        ):
            with self.assertRaises(self.module.MonarchError):
                self.module.command_networth(args)

    def test_transactions_bound_the_window_and_the_row_count(self) -> None:
        payload = {
            "allTransactions": {
                "totalCount": 137,
                "results": [
                    {
                        "id": "txn-1",
                        "date": "2026-08-01",
                        "amount": -54.23,
                        "pending": False,
                        "category": {"id": "cat-1", "name": "Groceries"},
                        "merchant": {"id": "m-1", "name": "Example Market"},
                        "account": {"id": "acct-1", "displayName": "Joint Checking"},
                    }
                ],
            }
        }
        args = SimpleNamespace(
            profile="household", days=30, limit=25, account=None, category=None, json=False
        )
        code, out, err, calls = self.run_command(
            self.module.command_transactions, args, [payload]
        )
        self.assertEqual(0, code)
        self.assertEqual("GetTransactionsList", calls[0].operation)
        self.assertEqual(25, calls[0].variables["limit"])
        self.assertIn("startDate", calls[0].variables["filters"])
        self.assertIn("endDate", calls[0].variables["filters"])
        self.assertIn("2026-08-01,Example Market,Groceries,Joint Checking,-54.23,no", out)
        self.assertIn("showing 1 of 137 transactions", err)
        self.assertNotIn("showing 1 of 137", out)

    def test_transactions_resolve_an_account_name_to_an_id_filter(self) -> None:
        payload = {"allTransactions": {"totalCount": 0, "results": []}}
        args = SimpleNamespace(
            profile="household", days=30, limit=25, account="joint checking",
            category=None, json=False,
        )
        code, _, _, calls = self.run_command(
            self.module.command_transactions, args, [self.ACCOUNTS, payload]
        )
        self.assertEqual(0, code)
        self.assertEqual(["acct-1"], calls[1].variables["filters"]["accounts"])

    def test_ambiguous_account_name_exits_non_zero(self) -> None:
        args = SimpleNamespace(
            profile="household", days=30, limit=25, account="household",
            category=None, json=False,
        )
        code, _, err = self.run_main(args, self.module.command_transactions, [self.ACCOUNTS])
        self.assertEqual(1, code)
        self.assertIn("ambiguous", err)
        self.assertIn("Household Card", err)

    def test_unmatched_account_name_exits_non_zero(self) -> None:
        args = SimpleNamespace(
            profile="household", days=30, limit=25, account="Nowhere Bank",
            category=None, json=False,
        )
        code, _, err = self.run_main(args, self.module.command_transactions, [self.ACCOUNTS])
        self.assertEqual(1, code)
        self.assertIn("No account matches", err)

    def test_unmatched_category_name_exits_non_zero(self) -> None:
        args = SimpleNamespace(
            profile="household", days=30, limit=25, account=None,
            category="Yachts", json=False,
        )
        code, _, err = self.run_main(args, self.module.command_transactions, [self.CATEGORIES])
        self.assertEqual(1, code)
        self.assertIn("No category matches", err)

    def run_main(self, args, handler, responses):
        """Drive a handler through `main`'s error path so the exit code is the real one."""
        namespace = SimpleNamespace(**vars(args))
        namespace.handler = handler
        namespace.env_file = None
        calls = []

        def fake_graphql(profile, operation, document, variables=None):
            calls.append(operation)
            return responses.pop(0)

        buf, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "build_parser") as parser, patch.object(
            self.module, "get_profile", return_value=self.profile
        ), patch.object(self.module, "graphql", side_effect=fake_graphql), patch.object(
            self.module, "load_dotenv"
        ):
            parser.return_value.parse_args.return_value = namespace
            with redirect_stdout(buf), redirect_stderr(err):
                code = self.module.main([])
        return code, buf.getvalue(), err.getvalue()

    def test_categories_group_and_sort(self) -> None:
        args = SimpleNamespace(profile="household", limit=200, json=False)
        code, out, _, calls = self.run_command(
            self.module.command_categories, args, [self.CATEGORIES]
        )
        self.assertEqual(0, code)
        self.assertEqual("GetCategories", calls[0].operation)
        rows = [line for line in out.strip().splitlines()[1:]]
        self.assertEqual("Food,expense,Groceries,cat-1", rows[0])
        self.assertEqual("Income,income,Paycheck,cat-3", rows[2])

    def test_budgets_select_the_requested_month(self) -> None:
        payload = {
            "budgetData": {
                "monthlyAmountsByCategory": [
                    {
                        "category": {
                            "id": "cat-1",
                            "name": "Groceries",
                            "group": {"id": "g-1", "name": "Food", "type": "expense"},
                        },
                        "monthlyAmounts": [
                            {
                                "month": "2026-07-01",
                                "plannedCashFlowAmount": 700.0,
                                "actualAmount": 655.2,
                                "remainingAmount": 44.8,
                            },
                            {
                                "month": "2026-08-01",
                                "plannedCashFlowAmount": 750.0,
                                "actualAmount": 120.0,
                                "remainingAmount": 630.0,
                            },
                        ],
                    }
                ]
            }
        }
        args = SimpleNamespace(profile="household", month="2026-08", limit=100, json=False)
        code, out, err, calls = self.run_command(
            self.module.command_budgets, args, [payload]
        )
        self.assertEqual(0, code)
        self.assertEqual("Common_GetJointPlanningData", calls[0].operation)
        self.assertEqual("2026-08-01", calls[0].variables["startDate"])
        self.assertEqual("2026-08-31", calls[0].variables["endDate"])
        self.assertIn("month\t2026-08", err)
        self.assertIn("Food,Groceries,750.00,120.00,630.00", out)
        self.assertNotIn("655.20", out)

    def test_cashflow_reports_totals_then_the_largest_groups(self) -> None:
        payload = {
            "summary": [
                {"summary": {"sumIncome": 8200.0, "sumExpense": -6100.5, "savings": 2099.5, "savingsRate": 0.256}}
            ],
            "byCategoryGroup": [
                {
                    "groupBy": {"categoryGroup": {"id": "g-1", "name": "Food", "type": "expense"}},
                    "summary": {"sum": -900.25},
                },
                {
                    "groupBy": {"categoryGroup": {"id": "g-3", "name": "Housing", "type": "expense"}},
                    "summary": {"sum": -2400.0},
                },
            ],
        }
        args = SimpleNamespace(profile="household", days=30, limit=25, json=False)
        code, out, err, calls = self.run_command(
            self.module.command_cashflow, args, [payload]
        )
        self.assertEqual(0, code)
        self.assertEqual("Web_GetCashFlowPage", calls[0].operation)
        self.assertIn("total,income,-,8200.00", out)
        self.assertIn("total,savings,-,2099.50", out)
        rows = out.strip().splitlines()
        self.assertEqual("group,Housing,expense,-2400.00", rows[4])
        self.assertEqual("group,Food,expense,-900.25", rows[5])
        self.assertIn("window", err)

    def test_cashflow_group_limit_is_reported(self) -> None:
        payload = {
            "summary": [{"summary": {"sumIncome": 1.0, "sumExpense": -1.0, "savings": 0.0}}],
            "byCategoryGroup": [
                {"groupBy": {"categoryGroup": {"name": f"G{index}", "type": "expense"}},
                 "summary": {"sum": -float(index)}}
                for index in range(1, 4)
            ],
        }
        args = SimpleNamespace(profile="household", days=30, limit=1, json=False)
        code, _, err, _ = self.run_command(self.module.command_cashflow, args, [payload])
        self.assertEqual(0, code)
        self.assertIn("showing 1 of 3 category groups", err)

    def test_holdings_resolve_the_account_and_sort_by_value(self) -> None:
        payload = {
            "portfolio": {
                "aggregateHoldings": {
                    "edges": [
                        {
                            "node": {
                                "id": "h-1",
                                "quantity": 12.5,
                                "totalValue": 2500.0,
                                "security": {
                                    "id": "s-1", "name": "Example Index Fund",
                                    "ticker": "EXIDX", "currentPrice": 200.0,
                                },
                                "holdings": [],
                            }
                        },
                        {
                            "node": {
                                "id": "h-2",
                                "quantity": 100,
                                "totalValue": 8800.0,
                                "security": {
                                    "id": "s-2", "name": "Example Bond Fund",
                                    "ticker": "EXBND", "currentPrice": 88.0,
                                },
                                "holdings": [],
                            }
                        },
                    ]
                }
            }
        }
        args = SimpleNamespace(profile="household", account="Household Brokerage", limit=50, json=False)
        code, out, err, calls = self.run_command(
            self.module.command_holdings, args, [self.ACCOUNTS, payload]
        )
        self.assertEqual(0, code)
        self.assertEqual(["GetAccounts", "Web_GetHoldings"], [call.operation for call in calls])
        self.assertEqual(["acct-3"], calls[1].variables["input"]["accountIds"])
        rows = out.strip().splitlines()
        self.assertEqual("EXBND,Example Bond Fund,100,88.00,8800.00", rows[1])
        self.assertEqual("EXIDX,Example Index Fund,12.5,200.00,2500.00", rows[2])
        self.assertIn("Household Brokerage", err)

    def test_status_reports_auth_session_and_account_count(self) -> None:
        args = SimpleNamespace(profile="household", all_profiles=False, json=False)
        self.module._TOKEN_SOURCE["household"] = "fresh"
        code, out, _, _ = self.run_command(self.module.command_status, args, [self.ACCOUNTS])
        self.assertEqual(0, code)
        self.assertIn("auth\tok", out)
        self.assertIn("session\tfresh", out)
        self.assertIn("accounts\t3", out)
        self.assertIn("email\ta***@example.test", out)
        self.assertNotIn(FIXTURE_PASSWORD, out)

    def test_status_exits_non_zero_when_mfa_is_missing(self) -> None:
        failure = self.module.MonarchError(
            "Monarch requires multi-factor authentication for profile 'household'. "
            "Set MONARCH_HOUSEHOLD_MFA_SECRET to the base32 seed."
        )
        args = SimpleNamespace(profile="household", all_profiles=False, json=False)
        code, out, err, _ = self.run_command(self.module.command_status, args, [failure])
        self.assertEqual(1, code)
        self.assertIn("auth\tmfa-required", out)
        self.assertIn("MONARCH_HOUSEHOLD_MFA_SECRET", err)


class MonarchBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_no_graphql_document_is_a_mutation(self) -> None:
        documents = [
            value
            for name, value in vars(self.module).items()
            if name.startswith("QUERY_") and isinstance(value, str)
        ]
        self.assertEqual(8, len(documents))
        for document in documents:
            with self.subTest(document=document.strip().splitlines()[0]):
                self.assertNotIn("mutation", document)
                self.assertTrue(document.lstrip().startswith("query "))

    def test_no_subcommand_is_named_for_a_write(self) -> None:
        parser = self.module.build_parser()
        actions = [
            action for action in parser._actions if isinstance(action, self.module.argparse._SubParsersAction)
        ]
        names = sorted(actions[0].choices)
        self.assertEqual(
            [
                "accounts", "budgets", "cashflow", "categories", "holdings",
                "networth", "profiles", "status", "transactions",
            ],
            names,
        )

    def test_secrets_never_reach_the_rendered_output(self) -> None:
        env = {
            "MONARCH_PROFILES": "demo",
            "MONARCH_DEMO_EMAIL": "agent@example.test",
            "MONARCH_DEMO_PASSWORD": FIXTURE_PASSWORD,
            "MONARCH_DEMO_MFA_SECRET": RFC6238_SEED,
        }
        http = FakeHTTP([(200, {"token": FIXTURE_TOKEN}), (200, {"data": {"accounts": []}})])
        buf, err = io.StringIO(), io.StringIO()
        with isolated_home(), patch.dict(os.environ, env, clear=False), patch.object(
            self.module, "open_url", http
        ):
            self.module._TOKENS.clear()
            profile = self.module.get_profile("demo")
            with redirect_stdout(buf), redirect_stderr(err):
                self.module.command_profiles(SimpleNamespace())
                self.module.graphql(profile, "GetAccounts", self.module.QUERY_ACCOUNTS)

        captured = buf.getvalue() + err.getvalue()
        for secret in (FIXTURE_TOKEN, FIXTURE_PASSWORD, RFC6238_SEED):
            self.assertNotIn(secret, captured)

    def test_session_file_lives_in_the_config_tree_not_the_cache_tree(self) -> None:
        profile = self.module.Profile(
            name="household", email="agent@example.test", password=FIXTURE_PASSWORD,
            mfa_secret="", label="Example Household",
        )
        with isolated_home() as root:
            path = self.module.session_path(profile)
        self.assertTrue(str(path).startswith(str(root / "config")))
        self.assertIn("rundesk/integrations/monarch", str(path).replace(os.sep, "/"))
        self.assertTrue(path.name.endswith("session-household.json"))

    def test_main_help_exits_clean(self) -> None:
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.module.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("networth", buf.getvalue())

    def test_missing_credentials_exit_one_without_a_traceback(self) -> None:
        err = io.StringIO()
        with patch.dict(os.environ, {"MONARCH_PROFILES": "demo"}, clear=True), patch.object(
            self.module, "load_dotenv"
        ):
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = self.module.main(["accounts", "--profile", "demo"])
        self.assertEqual(1, code)
        self.assertIn("MONARCH_DEMO_EMAIL", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
