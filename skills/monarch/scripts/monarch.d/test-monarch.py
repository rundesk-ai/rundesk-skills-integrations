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

# A fixed instant, so a journal batch id is the same on every run and on every machine.
FIXTURE_START = 1785000000.0

# The exact set the owner approved. A seventh entry here is a review, not a diff.
APPROVED_MUTATIONS = {
    "Web_TransactionDrawerUpdateTransaction",
    "Web_SetTransactionTags",
    "Web_CreateCategory",
    "Common_CreateTransactionRuleMutationV2",
    "Common_DeleteTransactionRule",
    "Common_UpdateBudgetItem",
}


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


def subparsers_of(module, parser) -> dict:
    """The subcommand table of one parser, keyed by name."""
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor
        if isinstance(action, module.argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def subcommands(module, parser) -> list:
    return sorted(subparsers_of(module, parser))


@contextmanager
def isolated_home():
    """Point every credential, session, and state path at a throwaway directory.

    `MONARCH_DEFAULT_PROFILE` is neutralised too: it decides which account reads the plain
    variable names, so an ambient value would change what a message names.
    """
    with tempfile.TemporaryDirectory(prefix="monarch-test-") as temporary:
        root = Path(temporary)
        overrides = {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "MONARCH_DEFAULT_PROFILE": "",
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
        self.assertIn("MONARCH_PASSWORD__HOUSEHOLD", str(raised.exception))
        self.assertNotIn(FIXTURE_PASSWORD, str(raised.exception))

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

    def test_rundesk_suffix_form_wins_over_the_legacy_form(self) -> None:
        env = {
            "MONARCH_EMAIL__HOUSEHOLD": "rundesk@example.test",
            "MONARCH_PASSWORD__HOUSEHOLD": FIXTURE_PASSWORD,
            "MONARCH_HOUSEHOLD_EMAIL": "legacy@example.test",
            "MONARCH_HOUSEHOLD_PASSWORD": "legacy-password",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["household"], self.module.discovered_profile_names())
            profile = self.module.get_profile("household")
        self.assertEqual("rundesk@example.test", profile.email)
        self.assertEqual(FIXTURE_PASSWORD, profile.password)

    def test_a_named_account_never_falls_back_to_the_plain_value(self) -> None:
        # The plain names belong to the default account. Reading them here would pair one
        # household's email with another household's password.
        env = {
            "MONARCH_EMAIL": "default@example.test",
            "MONARCH_PASSWORD": FIXTURE_PASSWORD,
            "MONARCH_EMAIL__PARENTS": "parents@example.test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual("", self.module.profile_value("parents", "MONARCH_PASSWORD"))
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.get_profile("parents")
        message = str(raised.exception)
        self.assertIn("MONARCH_PASSWORD__PARENTS", message)
        self.assertNotIn(FIXTURE_PASSWORD, message)

    def test_plain_names_alone_are_one_default_account(self) -> None:
        env = {
            "MONARCH_EMAIL": "agent@example.test",
            "MONARCH_PASSWORD": FIXTURE_PASSWORD,
            "MONARCH_LABEL": "Example Household",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["default"], self.module.configured_profile_names())
            profile = self.module.get_profile("default")
        self.assertEqual("agent@example.test", profile.email)
        self.assertEqual("Example Household", profile.label)
        self.assertFalse(profile.has_mfa)

    def test_a_partly_configured_default_account_is_listed_not_hidden(self) -> None:
        with patch.dict(os.environ, {"MONARCH_EMAIL": "agent@example.test"}, clear=True):
            self.assertEqual(["default"], self.module.configured_profile_names())
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.get_profile("default")
        self.assertIn("MONARCH_PASSWORD", str(raised.exception))
        self.assertNotIn("MONARCH_PASSWORD__", str(raised.exception))

    def test_a_lone_default_account_selects_without_a_profile_flag(self) -> None:
        env = {
            "MONARCH_EMAIL": "agent@example.test",
            "MONARCH_PASSWORD": FIXTURE_PASSWORD,
        }
        with patch.dict(os.environ, env, clear=True):
            name = self.module.selected_profile_name(SimpleNamespace(profile=None))
            self.assertEqual("default", name)
            profile = self.module.get_profile(name)
        with isolated_home():
            path = self.module.session_path(profile)
        self.assertEqual("session-default.json", path.name)

    def test_the_named_default_profile_reads_the_plain_values(self) -> None:
        env = {
            "MONARCH_DEFAULT_PROFILE": "household",
            "MONARCH_EMAIL": "agent@example.test",
            "MONARCH_PASSWORD": FIXTURE_PASSWORD,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["household"], self.module.configured_profile_names())
            profile = self.module.get_profile("household")
        self.assertEqual("agent@example.test", profile.email)

    def test_the_legacy_form_resolves_beside_a_rundesk_account(self) -> None:
        env = {
            "MONARCH_HOUSEHOLD_EMAIL": "legacy@example.test",
            "MONARCH_HOUSEHOLD_PASSWORD": FIXTURE_PASSWORD,
            "MONARCH_HOUSEHOLD_MFA_SECRET": RFC6238_SEED,
            "MONARCH_HOUSEHOLD_LABEL": "Example Household",
            "MONARCH_EMAIL__PARENTS": "parents@example.test",
            "MONARCH_PASSWORD__PARENTS": FIXTURE_PASSWORD,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                ["household", "parents"], self.module.configured_profile_names()
            )
            legacy = self.module.get_profile("household")
            rundesk = self.module.get_profile("parents")
        self.assertEqual("legacy@example.test", legacy.email)
        self.assertEqual("Example Household", legacy.label)
        self.assertTrue(legacy.has_mfa)
        self.assertEqual("parents@example.test", rundesk.email)

    def test_a_reserved_word_is_not_read_as_a_legacy_profile(self) -> None:
        env = {"MONARCH_ENV_LABEL": "Example Household"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual([], self.module.discovered_profile_names())

    def test_the_default_word_names_the_default_account_rather_than_vanishing(self) -> None:
        """`MONARCH_DEFAULT_EMAIL` resolves, so it must not be dropped from the listing."""
        env = {
            "MONARCH_DEFAULT_EMAIL": "agent@example.test",
            "MONARCH_DEFAULT_PASSWORD": FIXTURE_PASSWORD,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(["default"], self.module.discovered_profile_names())
            self.assertEqual("agent@example.test", self.module.get_profile("default").email)

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
        self.assertIn("MONARCH_EMAIL__DEMO", buf.getvalue())

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


class MonarchNameMatchingTest(unittest.TestCase):
    """`--account` receives names this tool printed, so display and matching must agree."""

    LONG = "Alex Q. Example Jr. - SEP IRA Brokerage Account"
    SIBLING = "Alex Q. Example Jr. - SEP IRA Rollover Account"

    def setUp(self) -> None:
        self.module = load_module()
        self.records = [
            {"id": "acct-1", "displayName": self.LONG},
            {"id": "acct-2", "displayName": "Joint Checking"},
        ]

    def match(self, wanted: str, records: list | None = None) -> dict:
        return self.module.match_one(
            self.records if records is None else records, "displayName", wanted, "Account"
        )

    def test_a_name_this_tool_shortened_still_resolves(self) -> None:
        shortened = self.module.truncate(self.LONG, 40)
        self.assertTrue(shortened.endswith(self.module.ELLIPSIS))
        self.assertNotIn(shortened, self.LONG)  # the ellipsis makes it no substring
        self.assertEqual("acct-1", self.match(shortened)["id"])

    def test_a_shortened_name_matching_two_records_is_refused(self) -> None:
        records = [
            {"id": "acct-1", "displayName": self.LONG},
            {"id": "acct-2", "displayName": self.SIBLING},
        ]
        shortened = self.module.truncate(self.LONG, 30)
        with self.assertRaises(self.module.MonarchError) as raised:
            self.match(shortened, records)
        self.assertIn("--json", str(raised.exception))

    def test_an_exact_name_wins_over_a_prefix(self) -> None:
        records = [
            {"id": "acct-1", "displayName": "Savings"},
            {"id": "acct-2", "displayName": "Savings Overflow"},
        ]
        self.assertEqual("acct-1", self.match("Savings", records)["id"])

    def test_substring_matching_is_unchanged(self) -> None:
        self.assertEqual("acct-2", self.match("joint check")["id"])

    def test_an_unmatched_name_still_raises(self) -> None:
        with self.assertRaises(self.module.MonarchError):
            self.match("Nowhere Bank")

    def test_a_bare_ellipsis_does_not_match_everything(self) -> None:
        with self.assertRaises(self.module.MonarchError):
            self.match(self.module.ELLIPSIS)

    def test_display_and_matching_share_one_ellipsis(self) -> None:
        """Two copies of this literal is how the round trip broke the first time."""
        self.assertTrue(self.module.truncate("x" * 50, 40).endswith(self.module.ELLIPSIS))


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
        self.assertIn("MONARCH_MFA_SECRET__HOUSEHOLD", message)
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

    def test_a_long_account_name_survives_the_accounts_to_holdings_round_trip(self) -> None:
        """The exact failure found live: `accounts` shortens a name `--account` then rejects."""
        long_name = "Alex Q. Example Jr. - SEP IRA Brokerage Account"
        accounts = {"accounts": [{
            "id": "acct-9", "displayName": long_name, "mask": "1234", "isAsset": True,
            "displayBalance": 1000.0, "displayLastUpdatedAt": "2026-08-01T09:30:00Z",
            "type": {"name": "brokerage", "display": "Investments"},
            "subtype": {"name": "ira", "display": "IRA"},
            "institution": {"name": "Example Brokerage"},
        }]}
        _, listed, _, _ = self.run_command(
            self.module.command_accounts,
            SimpleNamespace(profile="household", limit=50, json=False), [accounts],
        )
        printed = listed.strip().splitlines()[1].split(",")[3]
        self.assertNotEqual(long_name, printed)  # it was shortened for display

        empty = {"portfolio": {"aggregateHoldings": {"edges": []}}}
        code, _, err, calls = self.run_command(
            self.module.command_holdings,
            SimpleNamespace(profile="household", account=printed, limit=50, json=False),
            [accounts, empty],
        )
        self.assertEqual(0, code, err)
        self.assertEqual(["acct-9"], calls[1].variables["input"]["accountIds"])

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

    def test_every_query_document_is_still_only_a_query(self) -> None:
        documents = [
            value
            for name, value in vars(self.module).items()
            if name.startswith("QUERY_") and isinstance(value, str)
        ]
        self.assertEqual(12, len(documents))
        for document in documents:
            with self.subTest(document=document.strip().splitlines()[0]):
                self.assertNotIn("mutation", document)
                self.assertTrue(document.lstrip().startswith("query "))

    def test_every_mutation_document_in_the_module_is_on_the_allowlist(self) -> None:
        """The inverse of the guard this package carried while it was read-only.

        There is no longer `no mutation here`, so the guard that replaces it is that
        nothing can be added quietly: every mutation string in the module has to be a
        value in MUTATIONS, and MUTATIONS has to hold exactly what was approved.
        """
        documents = [
            value
            for name, value in vars(self.module).items()
            if name.startswith("MUTATION_") and isinstance(value, str)
        ]
        self.assertEqual(6, len(documents))
        allowed = set(self.module.MUTATIONS.values())
        for document in documents:
            with self.subTest(document=document.strip().splitlines()[0]):
                self.assertTrue(document.lstrip().startswith("mutation "))
                self.assertIn(document, allowed)

    def test_the_allowlist_holds_exactly_the_approved_operations(self) -> None:
        self.assertEqual(APPROVED_MUTATIONS, set(self.module.MUTATIONS))
        self.assertEqual(APPROVED_MUTATIONS, set(self.module.MUTATION_ROOTS))
        for operation, document in self.module.MUTATIONS.items():
            with self.subTest(operation=operation):
                self.assertIn(f"mutation {operation}(", document)
                self.assertIn(self.module.MUTATION_ROOTS[operation], document)

    def test_no_code_path_deletes_a_transaction_or_a_category_or_splits_one(self) -> None:
        """These four were considered and excluded; each is a fresh owner gate, not a diff."""
        source = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "deleteTransaction(",
            "Common_DeleteTransactionMutation",
            "deleteCategory",
            "Web_DeleteCategory",
            "updateTransactionSplit",
            "Common_SplitTransactionMutation",
            "deleteAllTransactionRules",
        )
        for fragment in forbidden:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

        for document in self.module.MUTATIONS.values():
            with self.subTest(document=document.strip().splitlines()[0]):
                for verb in ("deleteTransaction(", "deleteCategory", "splitTransaction"):
                    self.assertNotIn(verb, document)

    def test_edit_can_never_reach_an_amount_a_date_or_an_account(self) -> None:
        """`UpdateTransactionMutationInput` accepts them; this package has no name for them."""
        self.assertEqual(
            {"category": "category", "merchant": "name", "notes": "notes"},
            self.module.TRANSACTION_INPUT_FIELDS,
        )
        for forbidden in ("amount", "date", "accountId", "hideFromReports", "pending"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, self.module.TRANSACTION_INPUT_FIELDS.values())

    def test_subcommands_are_exactly_the_reads_plus_the_approved_writes(self) -> None:
        parser = self.module.build_parser()
        self.assertEqual(
            [
                "accounts", "budget", "budgets", "cashflow", "categories", "category",
                "edit", "holdings", "networth", "profiles", "rule", "rules", "status",
                "tag", "transactions", "undo",
            ],
            subcommands(self.module, parser),
        )
        nested = subparsers_of(self.module, parser)
        self.assertEqual(["create"], subcommands(self.module, nested["category"]))
        self.assertEqual(["create", "delete"], subcommands(self.module, nested["rule"]))
        self.assertEqual(["set"], subcommands(self.module, nested["budget"]))

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
        self.assertIn("MONARCH_EMAIL__DEMO", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class FakeHousehold:
    """A synthetic household that answers `graphql` and remembers what was written.

    It stands in for the module's one remaining boundary during a write, so a round trip
    — apply, read back, undo, read back again — is exercised end to end with no socket.
    """

    def __init__(self) -> None:
        self.transactions = {
            "txn-1": {
                "id": "txn-1", "date": "2026-08-01", "amount": -54.23, "pending": False,
                "notes": "", "category": {"id": "cat-1", "name": "Groceries"},
                "merchant": {"id": "m-1", "name": "Example Market"},
                "account": {"id": "acct-1", "displayName": "Joint Checking"},
                "tags": [],
            },
            "txn-2": {
                "id": "txn-2", "date": "2026-07-30", "amount": -18.0, "pending": False,
                "notes": "", "category": {"id": "cat-1", "name": "Groceries"},
                "merchant": {"id": "m-2", "name": "Example Deli"},
                "account": {"id": "acct-1", "displayName": "Joint Checking"},
                "tags": [],
            },
        }
        self.categories = [
            {"id": "cat-1", "name": "Groceries", "group": {"id": "g-1", "name": "Food", "type": "expense"}},
            {"id": "cat-2", "name": "Restaurants", "group": {"id": "g-1", "name": "Food", "type": "expense"}},
        ]
        self.groups = [{"id": "g-1", "name": "Food", "type": "expense"}]
        self.tags = [
            {"id": "tag-1", "name": "Reimbursable", "transactionCount": 3},
            {"id": "tag-2", "name": "Business", "transactionCount": 1},
        ]
        self.rules = []
        self.budgets = {("cat-1", "2026-08"): 700.0}

        self.calls = []
        self.refuse = {}       # operation -> the PayloadError message it comes back with
        self.frozen = set()    # transactions whose writes are accepted but do not land
        self.next_ids = iter(f"new-{index}" for index in range(1, 20))

    # -- transport ---------------------------------------------------------

    def __call__(self, profile, operation, document, variables=None):
        self.calls.append(
            SimpleNamespace(operation=operation, document=document, variables=variables or {})
        )
        try:
            handler = self.HANDLERS[operation]
        except KeyError:
            raise AssertionError(f"unexpected GraphQL operation {operation}")
        if operation in self.refuse:
            return self.refused(operation)
        return handler(self, variables or {})

    def refused(self, operation: str) -> dict:
        return {ROOTS[operation]: {"errors": {"message": self.refuse[operation],
                                              "code": "INVALID", "fieldErrors": []}}}

    @property
    def mutations(self) -> list:
        return [call for call in self.calls if call.operation in APPROVED_MUTATIONS]

    # -- reads -------------------------------------------------------------

    def get_transaction(self, variables: dict) -> dict:
        return {"getTransaction": self.transactions.get(str(variables.get("id")))}

    def get_categories(self, variables: dict) -> dict:
        return {"categories": self.categories}

    def get_groups(self, variables: dict) -> dict:
        return {"categoryGroups": self.groups}

    def get_tags(self, variables: dict) -> dict:
        return {"householdTransactionTags": self.tags}

    def get_rules(self, variables: dict) -> dict:
        return {"transactionRules": self.rules}

    def get_budgets(self, variables: dict) -> dict:
        month = str(variables.get("startDate"))[:7]
        entries = []
        for (category_id, key), amount in self.budgets.items():
            if key != month:
                continue
            category = next(one for one in self.categories if one["id"] == category_id)
            entries.append({
                "category": category,
                "monthlyAmounts": [{
                    "month": f"{key}-01", "plannedCashFlowAmount": amount,
                    "actualAmount": 0.0, "remainingAmount": amount,
                }],
            })
        return {"budgetData": {"monthlyAmountsByCategory": entries}}

    # -- writes ------------------------------------------------------------

    def update_transaction(self, variables: dict) -> dict:
        given = variables["input"]
        item = self.transactions[str(given["id"])]
        if str(given["id"]) not in self.frozen:
            if "category" in given:
                found = next(one for one in self.categories if one["id"] == given["category"])
                item["category"] = {"id": found["id"], "name": found["name"]}
            if "name" in given:
                item["merchant"] = {"id": "m-1", "name": given["name"]}
            if "notes" in given:
                item["notes"] = given["notes"]
        return {"updateTransaction": {"transaction": item, "errors": None}}

    def set_tags(self, variables: dict) -> dict:
        given = variables["input"]
        item = self.transactions[str(given["transactionId"])]
        by_id = {tag["id"]: tag for tag in self.tags}
        item["tags"] = [{"id": one, "name": by_id[one]["name"]} for one in given["tagIds"]]
        return {"setTransactionTags": {"transaction": item, "errors": None}}

    def create_category(self, variables: dict) -> dict:
        given = variables["input"]
        group = next(one for one in self.groups if one["id"] == given["group"])
        created = {"id": next(self.next_ids), "name": given["name"], "group": group}
        self.categories.append(created)
        return {"createCategory": {"category": created, "errors": None}}

    def create_rule(self, variables: dict) -> dict:
        given = variables["input"]
        category = next(
            (one for one in self.categories
             if one["id"] == given.get("setCategoryAction")), {"id": "", "name": "-"}
        )
        self.rules.append({
            "id": next(self.next_ids),
            "merchantNameCriteria": given.get("merchantNameCriteria") or [],
            "setCategoryAction": {"id": category["id"], "name": category["name"]},
            "recentApplicationCount": 0,
        })
        return {"createTransactionRuleV2": {"errors": None}}

    def delete_rule(self, variables: dict) -> dict:
        wanted = str(variables.get("id"))
        self.rules = [one for one in self.rules if str(one["id"]) != wanted]
        return {"deleteTransactionRule": {"deleted": True, "errors": None}}

    def set_budget(self, variables: dict) -> dict:
        given = variables["input"]
        key = (str(given["categoryId"]), str(given["startDate"])[:7])
        self.budgets[key] = float(given["amount"])
        return {"updateOrCreateBudgetItem": {"budgetItem": {"id": "budget-1",
                                                            "budgetAmount": given["amount"]}}}


ROOTS = {
    "Web_TransactionDrawerUpdateTransaction": "updateTransaction",
    "Web_SetTransactionTags": "setTransactionTags",
    "Web_CreateCategory": "createCategory",
    "Common_CreateTransactionRuleMutationV2": "createTransactionRuleV2",
    "Common_DeleteTransactionRule": "deleteTransactionRule",
    "Common_UpdateBudgetItem": "updateOrCreateBudgetItem",
}

FakeHousehold.HANDLERS = {
    "GetTransactionDrawer": FakeHousehold.get_transaction,
    "GetCategories": FakeHousehold.get_categories,
    "ManageGetCategoryGroups": FakeHousehold.get_groups,
    "GetHouseholdTransactionTags": FakeHousehold.get_tags,
    "GetTransactionRules": FakeHousehold.get_rules,
    "Common_GetJointPlanningData": FakeHousehold.get_budgets,
    "Web_TransactionDrawerUpdateTransaction": FakeHousehold.update_transaction,
    "Web_SetTransactionTags": FakeHousehold.set_tags,
    "Web_CreateCategory": FakeHousehold.create_category,
    "Common_CreateTransactionRuleMutationV2": FakeHousehold.create_rule,
    "Common_DeleteTransactionRule": FakeHousehold.delete_rule,
    "Common_UpdateBudgetItem": FakeHousehold.set_budget,
}


class WriteSurfaceTest(unittest.TestCase):
    """Shared rig: an isolated home for the journal, and a synthetic household."""

    def setUp(self) -> None:
        self.module = load_module()
        self.household = FakeHousehold()
        self.profile = self.module.Profile(
            name="household", email="agent@example.test", password=FIXTURE_PASSWORD,
            mfa_secret="", label="Example Household",
        )
        home = isolated_home()
        self.root = home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)

    def args(self, **overrides):
        namespace = SimpleNamespace(
            profile="household", confirm=False, max=self.module.DEFAULT_BULK_CAP,
            started=FIXTURE_START, json=False, limit=100,
        )
        for name, value in overrides.items():
            setattr(namespace, name, value)
        return namespace

    def run_write(self, handler, args):
        buf, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "graphql", side_effect=self.household
        ):
            with redirect_stdout(buf), redirect_stderr(err):
                code = handler(args)
        return code, buf.getvalue(), err.getvalue()

    def run_write_main(self, handler, args):
        """Through `main`, so a refusal's exit code is the real one rather than an exception."""
        namespace = SimpleNamespace(**vars(args))
        namespace.handler = handler
        namespace.env_file = None
        buf, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "build_parser") as parser, patch.object(
            self.module, "get_profile", return_value=self.profile
        ), patch.object(self.module, "graphql", side_effect=self.household), patch.object(
            self.module, "load_dotenv"
        ):
            parser.return_value.parse_args.return_value = namespace
            with redirect_stdout(buf), redirect_stderr(err):
                code = self.module.main([])
        return code, buf.getvalue(), err.getvalue()

    def with_defaults(self, defaults: dict, overrides: dict):
        merged = dict(defaults)
        merged.update(overrides)
        return self.args(**merged)

    def edit_args(self, *ids, **overrides):
        return self.with_defaults(
            {"transaction": list(ids), "category": None, "merchant": None, "note": None},
            overrides,
        )


class MonarchTransactionIdColumnTest(WriteSurfaceTest):
    """A write command can only name a transaction the read already named."""

    PAGE = {
        "allTransactions": {
            "totalCount": 1,
            "results": [{
                "id": "txn-1", "date": "2026-08-01", "amount": -54.23, "pending": False,
                "category": {"id": "cat-1", "name": "Groceries"},
                "merchant": {"id": "m-1", "name": "Example Market"},
                "account": {"id": "acct-1", "displayName": "Joint Checking"},
            }],
        }
    }

    def test_transactions_lead_with_the_id_a_write_needs(self) -> None:
        self.assertEqual("id", self.module.TRANSACTION_COLUMNS[0])

        args = SimpleNamespace(
            profile="household", days=30, limit=25, account=None, category=None, json=False
        )
        buf = io.StringIO()
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "graphql", return_value=self.PAGE
        ):
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                self.module.command_transactions(args)

        lines = buf.getvalue().strip().splitlines()
        self.assertEqual("id", lines[0].split(",")[0])
        self.assertEqual("txn-1", lines[1].split(",")[0])
        self.assertTrue(lines[1].startswith("txn-1,2026-08-01,Example Market,Groceries"))


class MonarchMutateTest(WriteSurfaceTest):
    def test_an_operation_off_the_allowlist_raises_before_any_request(self) -> None:
        with self.assertRaises(self.module.MonarchError) as raised:
            self.module.mutate(self.profile, "Web_DeleteCategory", {"id": "cat-1"})
        self.assertIn("allowlist", str(raised.exception))
        self.assertEqual([], self.household.calls)

    def test_a_field_error_inside_a_200_body_is_a_failure(self) -> None:
        self.household.refuse["Web_TransactionDrawerUpdateTransaction"] = "Category is required"
        with patch.object(self.module, "graphql", side_effect=self.household):
            with self.assertRaises(self.module.MonarchError) as raised:
                self.module.mutate(
                    self.profile,
                    "Web_TransactionDrawerUpdateTransaction",
                    {"input": {"id": "txn-1", "category": "cat-2"}},
                )
        self.assertIn("Category is required", str(raised.exception))

    def test_a_missing_payload_is_a_failure_not_a_silent_success(self) -> None:
        with patch.object(self.module, "graphql", return_value={}):
            with self.assertRaises(self.module.MonarchError):
                self.module.mutate(self.profile, "Web_SetTransactionTags", {"input": {}})

    def test_payload_errors_read_both_the_object_and_the_list_shape(self) -> None:
        as_object = {"errors": {"message": "nope", "code": "X", "fieldErrors": []}}
        as_list = {"errors": [{"message": None, "code": "X",
                               "fieldErrors": [{"field": "name", "messages": ["too long"]}]}]}
        self.assertIn("nope", self.module.payload_errors(as_object))
        self.assertIn("name: too long", self.module.payload_errors(as_list))
        self.assertEqual("", self.module.payload_errors({"errors": None}))


class MonarchJournalTest(WriteSurfaceTest):
    def change(self):
        return self.module.Change(
            operation="Web_TransactionDrawerUpdateTransaction", target="txn-1",
            field="category", before="cat-1", after="cat-2",
            shown_before="Groceries", shown_after="Restaurants", label="2026-08-01 Example Market",
        )

    def test_batch_id_comes_from_the_run_start_not_an_ambient_clock(self) -> None:
        first = self.module.batch_id(FIXTURE_START, 1)
        self.assertEqual(first, self.module.batch_id(FIXTURE_START, 1))
        self.assertRegex(first, r"^\d{8}T\d{6}Z-01$")
        self.assertNotEqual(first, self.module.batch_id(FIXTURE_START, 2))

    def test_a_second_batch_in_the_same_second_gets_the_next_counter(self) -> None:
        first = self.module.next_batch_id(FIXTURE_START)
        self.module.journal_batch(first, self.profile, [self.change()], FIXTURE_START)
        self.assertNotEqual(first, self.module.next_batch_id(FIXTURE_START))

    def test_the_journal_is_0600_inside_a_0700_directory(self) -> None:
        batch = self.module.journal_batch(
            self.module.next_batch_id(FIXTURE_START), self.profile, [self.change()], FIXTURE_START
        )
        path = self.module.journal_dir() / f"{batch}.json"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)

    def test_the_journal_lives_in_the_state_tree_and_names_no_secret(self) -> None:
        batch = self.module.journal_batch(
            self.module.next_batch_id(FIXTURE_START), self.profile, [self.change()], FIXTURE_START
        )
        path = self.module.journal_dir() / f"{batch}.json"
        self.assertTrue(str(path).startswith(str(self.root / "state")))
        raw = path.read_text(encoding="utf-8")
        for secret in (FIXTURE_PASSWORD, FIXTURE_TOKEN, "agent@example.test"):
            self.assertNotIn(secret, raw)
        self.assertIn("txn-1", raw)

    def test_a_change_survives_the_journal_round_trip(self) -> None:
        original = self.change()
        restored = self.module.Change.from_record(json.loads(json.dumps(original.as_record())))
        self.assertEqual(original.as_record(), restored.as_record())


class MonarchApplyChangesTest(WriteSurfaceTest):
    def changes(self, count: int = 1) -> list:
        return [
            self.module.Change(
                operation="Web_TransactionDrawerUpdateTransaction", target=f"txn-{index}",
                field="category", before="cat-1", after="cat-2",
                shown_before="Groceries", shown_after="Restaurants", label=f"target {index}",
            )
            for index in range(1, count + 1)
        ]

    def apply(self, changes, **overrides):
        settings = {"confirm": False, "cap": self.module.DEFAULT_BULK_CAP,
                    "started": FIXTURE_START, "action": "edit transactions"}
        settings.update(overrides)
        buf, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "graphql", side_effect=self.household):
            with redirect_stdout(buf), redirect_stderr(err):
                code = self.module.apply_changes(self.profile, changes, **settings)
        return code, buf.getvalue(), err.getvalue()

    def test_a_dry_run_previews_every_row_and_sends_no_mutation(self) -> None:
        code, out, _ = self.apply(self.changes(2))
        self.assertEqual(0, code)
        self.assertIn("mode\tdry-run", out)
        self.assertIn("txn-1,target 1,category,Groceries,Restaurants", out)
        self.assertIn("txn-2,target 2,category,Groceries,Restaurants", out)
        self.assertEqual([], self.household.mutations)
        self.assertEqual([], list(self.module.journal_dir().glob("*.json")))

    def test_a_batch_over_the_cap_is_refused_before_any_mutation(self) -> None:
        with self.assertRaises(self.module.MonarchError) as raised:
            self.apply(self.changes(3), cap=2, confirm=True)
        self.assertIn("bulk cap of 2", str(raised.exception))
        self.assertEqual([], self.household.mutations)

    def test_a_batch_over_the_cap_is_refused_in_a_dry_run_too(self) -> None:
        with self.assertRaises(self.module.MonarchError):
            self.apply(self.changes(3), cap=2)
        self.assertEqual([], self.household.mutations)

    def test_the_cap_can_be_raised_deliberately(self) -> None:
        code, _, err = self.apply(self.changes(2), cap=2, confirm=True)
        self.assertEqual(0, code, err)
        self.assertEqual(2, len(self.household.mutations))

    def test_a_confirmed_batch_sends_one_mutation_per_change_and_journals_it(self) -> None:
        code, out, err = self.apply(self.changes(2), confirm=True)
        self.assertEqual(0, code, err)
        self.assertIn("mode\tconfirmed", out)
        self.assertIn("applied\t2", out)

        sent = self.household.mutations
        self.assertEqual(
            ["Web_TransactionDrawerUpdateTransaction"] * 2, [call.operation for call in sent]
        )
        self.assertEqual({"input": {"id": "txn-1", "category": "cat-2"}}, sent[0].variables)

        batches = list(self.module.journal_dir().glob("*.json"))
        self.assertEqual(1, len(batches))
        self.assertEqual(2, len(json.loads(batches[0].read_text())["changes"]))

    def test_a_read_back_mismatch_stops_the_batch_and_leaves_the_journal(self) -> None:
        self.household.frozen.add("txn-2")
        code, out, err = self.apply(self.changes(2), confirm=True)
        self.assertEqual(1, code)
        self.assertIn("stopped after 2 of 2 changes", err)
        self.assertIn("read back as", err)
        self.assertNotIn("applied\t", out)

        record = json.loads(next(self.module.journal_dir().glob("*.json")).read_text())
        self.assertEqual(2, len(record["changes"]))
        self.assertIn("undo", err)

    def test_a_refused_mutation_stops_the_batch_and_journals_only_what_landed(self) -> None:
        changes = self.changes(2)
        original = self.household.update_transaction

        def refuse_the_second(variables):
            if variables["input"]["id"] == "txn-2":
                raise self.module.MonarchError("Monarch refused the change")
            return original(variables)

        self.household.HANDLERS = dict(self.household.HANDLERS)
        self.household.HANDLERS["Web_TransactionDrawerUpdateTransaction"] = (
            lambda _self, variables: refuse_the_second(variables)
        )
        code, _, err = self.apply(changes, confirm=True)
        self.assertEqual(1, code)
        self.assertIn("stopped after 1 of 2 changes", err)
        record = json.loads(next(self.module.journal_dir().glob("*.json")).read_text())
        self.assertEqual(["txn-1"], [one["target"] for one in record["changes"]])

    def test_an_empty_change_set_is_a_no_op_not_a_write(self) -> None:
        code, out, _ = self.apply([], confirm=True)
        self.assertEqual(0, code)
        self.assertIn("mode\tno-op", out)
        self.assertEqual([], self.household.mutations)


class MonarchEditTest(WriteSurfaceTest):
    def test_edit_without_a_field_flag_exits_non_zero(self) -> None:
        code, _, err = self.run_write_main(self.module.command_edit, self.edit_args("txn-1"))
        self.assertEqual(1, code)
        self.assertIn("--category", err)
        self.assertEqual([], self.household.mutations)

    def test_a_dry_run_shows_before_and_after_for_each_id_and_sends_nothing(self) -> None:
        args = self.edit_args("txn-1", "txn-2", category="Restaurants")
        code, out, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(0, code, err)
        self.assertIn("mode\tdry-run", out)
        self.assertIn("txn-1,2026-08-01 Example Market,category,Groceries,Restaurants", out)
        self.assertIn("txn-2,2026-07-30 Example Deli,category,Groceries,Restaurants", out)
        self.assertEqual([], self.household.mutations)
        self.assertEqual("Groceries", self.household.transactions["txn-1"]["category"]["name"])

    def test_confirm_sends_exactly_one_mutation_per_id_with_the_right_variables(self) -> None:
        args = self.edit_args("txn-1", "txn-2", category="Restaurants", confirm=True)
        code, out, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(0, code, err)
        self.assertIn("applied\t2", out)

        sent = self.household.mutations
        self.assertEqual(2, len(sent))
        self.assertEqual({"input": {"id": "txn-1", "category": "cat-2"}}, sent[0].variables)
        self.assertEqual({"input": {"id": "txn-2", "category": "cat-2"}}, sent[1].variables)
        self.assertEqual("Restaurants", self.household.transactions["txn-1"]["category"]["name"])

    def test_edit_never_sends_an_amount_a_date_or_an_account(self) -> None:
        args = self.edit_args(
            "txn-1", category="Restaurants", merchant="Example Grocer", note="checked",
            confirm=True,
        )
        code, _, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(0, code, err)

        keys = set()
        for call in self.household.mutations:
            keys.update(call.variables["input"])
        self.assertEqual({"id", "category", "name", "notes"}, keys)
        for forbidden in ("amount", "date", "accountId", "pending", "hideFromReports"):
            self.assertNotIn(forbidden, keys)

    def test_an_ambiguous_category_exits_non_zero_before_any_write(self) -> None:
        self.household.categories.append(
            {"id": "cat-3", "name": "Restaurants Abroad",
             "group": {"id": "g-1", "name": "Food", "type": "expense"}}
        )
        args = self.edit_args("txn-1", category="restaurant", confirm=True)
        code, _, err = self.run_write_main(self.module.command_edit, args)
        self.assertEqual(1, code)
        self.assertIn("ambiguous", err)
        self.assertEqual([], self.household.mutations)

    def test_an_unknown_transaction_id_is_named_rather_than_guessed(self) -> None:
        args = self.edit_args("txn-nope", category="Restaurants", confirm=True)
        code, _, err = self.run_write_main(self.module.command_edit, args)
        self.assertEqual(1, code)
        self.assertIn("txn-nope", err)
        self.assertEqual([], self.household.mutations)

    def test_a_field_already_holding_the_value_is_not_rewritten(self) -> None:
        args = self.edit_args("txn-1", category="Groceries", confirm=True)
        code, out, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(0, code, err)
        self.assertIn("mode\tno-op", out)
        self.assertEqual([], self.household.mutations)

    def test_a_shortened_category_name_round_trips_into_a_write(self) -> None:
        long_name = "Groceries, Household Supplies and Sundries for the Week"
        self.household.categories.append(
            {"id": "cat-9", "name": long_name,
             "group": {"id": "g-1", "name": "Food", "type": "expense"}}
        )
        shortened = self.module.truncate(long_name, 30)
        self.assertTrue(shortened.endswith(self.module.ELLIPSIS))

        args = self.edit_args("txn-1", category=shortened, confirm=True)
        code, _, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(0, code, err)
        self.assertEqual("cat-9", self.household.mutations[0].variables["input"]["category"])

    def test_ids_can_be_piped_in_from_a_reviewed_list(self) -> None:
        with patch.object(sys, "stdin", io.StringIO("txn-1\n\ntxn-2\n")):
            self.assertEqual(["txn-1", "txn-2"], self.module.target_ids(["-"]))

    def test_no_ids_at_all_is_refused(self) -> None:
        with self.assertRaises(self.module.MonarchError):
            self.module.target_ids([" "])

    def test_a_bulk_edit_over_the_cap_is_refused_before_a_single_read(self) -> None:
        for index in range(3, 8):
            self.household.transactions[f"txn-{index}"] = dict(
                self.household.transactions["txn-1"], id=f"txn-{index}"
            )
        args = self.edit_args(*[f"txn-{index}" for index in range(1, 8)],
                              category="Restaurants", max=3, confirm=True)
        code, _, err = self.run_write_main(self.module.command_edit, args)
        self.assertEqual(1, code)
        self.assertIn("bulk cap of 3", err)
        self.assertEqual([], self.household.mutations)
        # Refused on the count alone, so it never spent a read per target.
        self.assertEqual(
            [], [call for call in self.household.calls
                 if call.operation == "GetTransactionDrawer"]
        )

    def test_the_cap_is_refused_in_a_dry_run_too(self) -> None:
        args = self.edit_args("txn-1", "txn-2", category="Restaurants", max=1)
        code, _, err = self.run_write_main(self.module.command_edit, args)
        self.assertEqual(1, code)
        self.assertIn("bulk cap of 1", err)


class MonarchTagTest(WriteSurfaceTest):
    def tag_args(self, *ids, **overrides):
        return self.with_defaults(
            {"transaction": list(ids), "add": None, "remove": None}, overrides
        )

    def test_tag_without_add_or_remove_exits_non_zero(self) -> None:
        code, _, err = self.run_write_main(self.module.command_tag, self.tag_args("txn-1"))
        self.assertEqual(1, code)
        self.assertIn("--add", err)

    def test_adding_a_tag_sends_the_whole_replacement_set(self) -> None:
        self.household.transactions["txn-1"]["tags"] = [{"id": "tag-2", "name": "Business"}]
        args = self.tag_args("txn-1", add=["Reimbursable"], confirm=True)
        code, out, err = self.run_write(self.module.command_tag, args)
        self.assertEqual(0, code, err)
        self.assertEqual(
            {"input": {"transactionId": "txn-1", "tagIds": ["tag-2", "tag-1"]}},
            self.household.mutations[0].variables,
        )
        self.assertIn("Business,Business Reimbursable", out)

    def test_removing_a_tag_leaves_the_others(self) -> None:
        self.household.transactions["txn-1"]["tags"] = [
            {"id": "tag-1", "name": "Reimbursable"}, {"id": "tag-2", "name": "Business"},
        ]
        args = self.tag_args("txn-1", remove=["Business"], confirm=True)
        code, _, err = self.run_write(self.module.command_tag, args)
        self.assertEqual(0, code, err)
        self.assertEqual(
            ["tag-1"], self.household.mutations[0].variables["input"]["tagIds"]
        )

    def test_an_unknown_tag_is_refused_rather_than_created(self) -> None:
        args = self.tag_args("txn-1", add=["Nowhere"], confirm=True)
        code, _, err = self.run_write_main(self.module.command_tag, args)
        self.assertEqual(1, code)
        self.assertIn("No tag matches", err)
        self.assertEqual([], self.household.mutations)


class MonarchCategoryCreateTest(WriteSurfaceTest):
    def create_args(self, **overrides):
        return self.with_defaults({"name": "Pet Care", "group": "Food"}, overrides)

    def test_a_dry_run_names_the_group_and_sends_nothing(self) -> None:
        code, out, err = self.run_write(self.module.command_category_create, self.create_args())
        self.assertEqual(0, code, err)
        self.assertIn("mode\tdry-run", out)
        self.assertIn("Food / Pet Care", out)
        self.assertEqual([], self.household.mutations)

    def test_a_confirmed_create_sends_the_group_id_and_journals_the_new_id(self) -> None:
        code, out, err = self.run_write(
            self.module.command_category_create, self.create_args(confirm=True)
        )
        self.assertEqual(0, code, err)
        sent = self.household.mutations[0]
        self.assertEqual("Web_CreateCategory", sent.operation)
        self.assertEqual("g-1", sent.variables["input"]["group"])
        self.assertEqual("Pet Care", sent.variables["input"]["name"])

        record = json.loads(next(self.module.journal_dir().glob("*.json")).read_text())
        self.assertEqual("new-1", record["changes"][0]["target"])

    def test_a_created_category_is_journalled_as_not_reversible(self) -> None:
        """Undoing it would mean deleting a category, which reassigns every transaction."""
        code, _, err = self.run_write(
            self.module.command_category_create, self.create_args(confirm=True)
        )
        self.assertEqual(0, code, err)
        self.assertIn("cannot be undone by this package", err)
        record = json.loads(next(self.module.journal_dir().glob("*.json")).read_text())
        self.assertFalse(record["changes"][0]["reversible"])

    def test_a_duplicate_name_is_refused_before_any_write(self) -> None:
        code, _, err = self.run_write_main(
            self.module.command_category_create, self.create_args(name="Groceries", confirm=True)
        )
        self.assertEqual(1, code)
        self.assertIn("already exists", err)
        self.assertEqual([], self.household.mutations)


class MonarchRuleTest(WriteSurfaceTest):
    def existing_rule(self) -> dict:
        rule = {
            "id": "rule-1",
            "merchantNameCriteria": [{"operator": "contains", "value": "Example Market"}],
            "setCategoryAction": {"id": "cat-1", "name": "Groceries"},
            "recentApplicationCount": 4,
        }
        self.household.rules.append(rule)
        return rule

    def test_rules_lists_what_each_rule_matches_and_sets(self) -> None:
        self.existing_rule()
        code, out, err = self.run_write(self.module.command_rules, self.args())
        self.assertEqual(0, code, err)
        self.assertIn("rule-1,merchant contains Example Market,category Groceries,4", out)

    def test_a_confirmed_rule_create_sends_the_two_source_input_shape(self) -> None:
        args = self.args(merchant_contains="Example Deli", category="Restaurants", confirm=True)
        code, _, err = self.run_write(self.module.command_rule_create, args)
        self.assertEqual(0, code, err)
        sent = self.household.mutations[0]
        self.assertEqual("Common_CreateTransactionRuleMutationV2", sent.operation)
        self.assertEqual(
            {
                "merchantNameCriteria": [{"operator": "contains", "value": "Example Deli"}],
                "setCategoryAction": "cat-2",
                "applyToExistingTransactions": False,
            },
            sent.variables["input"],
        )

    def test_a_created_rule_is_identified_by_diffing_the_list_for_undo(self) -> None:
        args = self.args(merchant_contains="Example Deli", category="Restaurants", confirm=True)
        code, _, err = self.run_write(self.module.command_rule_create, args)
        self.assertEqual(0, code, err)
        record = json.loads(next(self.module.journal_dir().glob("*.json")).read_text())
        self.assertEqual("new-1", record["changes"][0]["target"])
        self.assertTrue(record["changes"][0]["reversible"])

    def test_an_empty_merchant_match_is_refused(self) -> None:
        args = self.args(merchant_contains="   ", category="Restaurants", confirm=True)
        code, _, err = self.run_write_main(self.module.command_rule_create, args)
        self.assertEqual(1, code)
        self.assertIn("match everything", err)
        self.assertEqual([], self.household.mutations)

    def test_rule_delete_sends_a_bare_id_not_an_input_object(self) -> None:
        self.existing_rule()
        code, _, err = self.run_write(
            self.module.command_rule_delete, self.args(rule="rule-1", confirm=True)
        )
        self.assertEqual(0, code, err)
        self.assertEqual({"id": "rule-1"}, self.household.mutations[0].variables)
        self.assertEqual([], self.household.rules)

    def test_deleting_a_rule_richer_than_rule_create_warns_it_cannot_be_rebuilt(self) -> None:
        rule = self.existing_rule()
        rule["amountCriteria"] = {"operator": "gt", "isExpense": True, "value": 100.0}
        code, _, err = self.run_write(
            self.module.command_rule_delete, self.args(rule="rule-1", confirm=True)
        )
        self.assertEqual(0, code, err)
        self.assertIn("cannot rebuild it", err)
        record = json.loads(next(self.module.journal_dir().glob("*.json")).read_text())
        self.assertFalse(record["changes"][0]["reversible"])

    def test_an_unknown_rule_id_exits_non_zero(self) -> None:
        code, _, err = self.run_write_main(
            self.module.command_rule_delete, self.args(rule="rule-nope", confirm=True)
        )
        self.assertEqual(1, code)
        self.assertIn("rule-nope", err)


class MonarchBudgetTest(WriteSurfaceTest):
    def budget_args(self, **overrides):
        return self.with_defaults(
            {"category": "Groceries", "month": "2026-08", "amount": "750"}, overrides
        )

    def test_a_dry_run_shows_the_previous_amount_and_sends_nothing(self) -> None:
        code, out, err = self.run_write(self.module.command_budget_set, self.budget_args())
        self.assertEqual(0, code, err)
        self.assertIn("700.00,750.00", out)
        self.assertEqual([], self.household.mutations)

    def test_a_confirmed_set_pins_the_month_and_never_applies_forward(self) -> None:
        code, _, err = self.run_write(self.module.command_budget_set, self.budget_args(confirm=True))
        self.assertEqual(0, code, err)
        sent = self.household.mutations[0]
        self.assertEqual("Common_UpdateBudgetItem", sent.operation)
        self.assertEqual(
            {
                "categoryId": "cat-1", "amount": 750.0, "timeframe": "month",
                "startDate": "2026-08-01", "applyToFuture": False,
            },
            sent.variables["input"],
        )
        self.assertEqual(750.0, self.household.budgets[("cat-1", "2026-08")])

    def test_an_unbudgeted_category_reads_as_zero_rather_than_missing(self) -> None:
        code, out, err = self.run_write(
            self.module.command_budget_set, self.budget_args(category="Restaurants")
        )
        self.assertEqual(0, code, err)
        self.assertIn("0.00,750.00", out)

    def test_an_amount_that_is_not_a_number_is_refused(self) -> None:
        code, _, err = self.run_write_main(
            self.module.command_budget_set, self.budget_args(amount="lots", confirm=True)
        )
        self.assertEqual(1, code)
        self.assertIn("Invalid --amount", err)
        self.assertEqual([], self.household.mutations)


class MonarchUndoTest(WriteSurfaceTest):
    def apply_one_edit(self, **overrides):
        args = self.edit_args("txn-1", category="Restaurants", confirm=True, **overrides)
        code, out, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(0, code, err)
        return next(
            line.split("\t")[1] for line in out.splitlines() if line.startswith("batch\t")
        )

    def undo_args(self, batch, **overrides):
        return self.with_defaults({"batch": batch, "list": False}, overrides)

    def test_a_full_round_trip_puts_every_value_back(self) -> None:
        batch = self.apply_one_edit()
        self.assertEqual("Restaurants", self.household.transactions["txn-1"]["category"]["name"])

        code, out, err = self.run_write(
            self.module.command_undo, self.undo_args(batch, confirm=True)
        )
        self.assertEqual(0, code, err)
        self.assertIn("state\tundone", out)
        self.assertEqual("Groceries", self.household.transactions["txn-1"]["category"]["name"])

    def test_undo_is_a_dry_run_until_confirm(self) -> None:
        batch = self.apply_one_edit()
        before = len(self.household.mutations)
        code, out, err = self.run_write(self.module.command_undo, self.undo_args(batch))
        self.assertEqual(0, code, err)
        self.assertIn("mode\tdry-run", out)
        self.assertIn("Restaurants,Groceries", out)
        self.assertEqual(before, len(self.household.mutations))
        self.assertEqual("Restaurants", self.household.transactions["txn-1"]["category"]["name"])

    def test_undoing_an_already_undone_batch_is_refused(self) -> None:
        batch = self.apply_one_edit()
        self.run_write(self.module.command_undo, self.undo_args(batch, confirm=True))
        code, _, err = self.run_write_main(
            self.module.command_undo, self.undo_args(batch, confirm=True)
        )
        self.assertEqual(1, code)
        self.assertIn("already undone", err)

    def test_a_target_changed_underneath_is_skipped_with_a_non_zero_exit(self) -> None:
        batch = self.apply_one_edit()
        # Somebody re-filed it in the Monarch app after this tool touched it.
        self.household.transactions["txn-1"]["category"] = {"id": "cat-1", "name": "Groceries"}

        code, _, err = self.run_write(self.module.command_undo, self.undo_args(batch, confirm=True))
        self.assertEqual(1, code)
        self.assertIn("changed underneath", err)
        self.assertIn("left standing", err)
        self.assertEqual("Groceries", self.household.transactions["txn-1"]["category"]["name"])

    def test_undo_reverses_only_the_part_of_a_batch_that_landed(self) -> None:
        self.household.frozen.add("txn-2")
        args = self.edit_args("txn-1", "txn-2", category="Restaurants", confirm=True)
        code, out, err = self.run_write(self.module.command_edit, args)
        self.assertEqual(1, code)
        self.assertIn("read back as", err)
        batch = next(line.split("\t")[1] for line in out.splitlines() if line.startswith("batch\t"))

        self.household.frozen.clear()
        code, _, err = self.run_write(self.module.command_undo, self.undo_args(batch, confirm=True))
        self.assertEqual(1, code)  # the frozen half never landed, so it is left alone
        self.assertIn("changed underneath", err)
        self.assertEqual("Groceries", self.household.transactions["txn-1"]["category"]["name"])

    def test_undoing_a_created_rule_deletes_it(self) -> None:
        args = self.args(merchant_contains="Example Deli", category="Restaurants", confirm=True)
        code, out, err = self.run_write(self.module.command_rule_create, args)
        self.assertEqual(0, code, err)
        self.assertEqual(1, len(self.household.rules))
        batch = next(line.split("\t")[1] for line in out.splitlines() if line.startswith("batch\t"))

        code, _, err = self.run_write(self.module.command_undo, self.undo_args(batch, confirm=True))
        self.assertEqual(0, code, err)
        self.assertEqual([], self.household.rules)

    def test_undoing_a_created_category_reports_it_rather_than_deleting_it(self) -> None:
        args = self.args(name="Pet Care", group="Food", confirm=True)
        code, out, err = self.run_write(self.module.command_category_create, args)
        self.assertEqual(0, code, err)
        batch = next(line.split("\t")[1] for line in out.splitlines() if line.startswith("batch\t"))

        before = list(self.household.categories)
        code, _, err = self.run_write(self.module.command_undo, self.undo_args(batch, confirm=True))
        self.assertEqual(1, code)
        self.assertIn("Pet Care", err)
        self.assertEqual(before, self.household.categories)
        self.assertEqual([], [call for call in self.household.mutations
                              if call.operation == "Common_DeleteTransactionRule"])

    def test_inverting_a_category_create_is_refused_rather_than_replayed(self) -> None:
        """Even a hand-edited journal cannot talk `undo` into deleting a category."""
        change = self.module.Change(
            operation="Web_CreateCategory", target="new-1", field="category",
            before=None, after="new-1", label="Pet Care",
        )
        with self.assertRaises(self.module.MonarchError) as raised:
            self.module.invert(change)
        self.assertIn("deleting a category", str(raised.exception))

    def test_a_reversed_batch_keeps_the_time_it_was_applied(self) -> None:
        batch = self.apply_one_edit()
        path = self.module.journal_dir() / f"{batch}.json"
        applied_at = json.loads(path.read_text())["when"]

        self.run_write(self.module.command_undo, self.undo_args(batch, confirm=True))
        record = json.loads(path.read_text())
        self.assertEqual("undone", record["state"])
        self.assertEqual(applied_at, record["when"])

    def test_undo_list_shows_batches_newest_first(self) -> None:
        self.apply_one_edit()
        code, out, err = self.run_write(
            self.module.command_undo, self.args(batch=None, list=True)
        )
        self.assertEqual(0, code, err)
        rows = out.strip().splitlines()
        self.assertEqual("batch,when,profile,changes,state", rows[0])
        self.assertIn("household,1,applied", rows[1])

    def test_an_unknown_batch_id_exits_non_zero(self) -> None:
        code, _, err = self.run_write_main(
            self.module.command_undo, self.undo_args("20000101T000000Z-01", confirm=True)
        )
        self.assertEqual(1, code)
        self.assertIn("undo --list", err)


if __name__ == "__main__":
    unittest.main()
