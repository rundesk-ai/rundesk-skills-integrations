#!/usr/bin/env python3
"""Offline tests for coolify.d/coolify.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parent
SCRIPT = MODULE_DIR / "coolify.py"


def load_module():
    spec = importlib.util.spec_from_file_location("coolify_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CoolifyModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example",
            token="secret-token",
            base_url="https://coolify.example.com",
            label="Example",
        )

    def test_get_profile_maps_env(self) -> None:
        env = {
            "COOLIFY_PROFILES": "example",
            "COOLIFY_DEFAULT_PROFILE": "example",
            "COOLIFY_EXAMPLE_LABEL": "Example Coolify",
            "COOLIFY_EXAMPLE_TOKEN": "tok",
            "COOLIFY_EXAMPLE_BASE_URL": "https://coolify.example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example"])
            profile = self.module.get_profile("example")
        self.assertEqual(profile.token, "tok")
        self.assertEqual(profile.base_url, "https://coolify.example.com")
        self.assertEqual(profile.label, "Example Coolify")

    def test_rundesk_suffix_keys_win_over_legacy_keys_for_one_account(self) -> None:
        env = {
            "COOLIFY_API_TOKEN__EXAMPLE": "rundesk-token",
            "COOLIFY_BASE_URL__EXAMPLE": "https://rundesk.example.test",
            "COOLIFY_EXAMPLE_TOKEN": "legacy-token",
            "COOLIFY_EXAMPLE_BASE_URL": "https://legacy.example.test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example"])
            profile = self.module.get_profile("example")
        self.assertEqual(profile.token, "rundesk-token")
        self.assertEqual(profile.base_url, "https://rundesk.example.test")

    def test_named_account_never_falls_back_to_plain_values(self) -> None:
        env = {
            "COOLIFY_API_TOKEN": "default-token",
            "COOLIFY_BASE_URL": "https://default.example.test",
            "COOLIFY_BASE_URL__EXAMPLE": "https://example.example.test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.profile_value("example", "COOLIFY_API_TOKEN"), "")
            with self.assertRaises(self.module.CoolifyError) as raised:
                self.module.get_profile("example")
        message = str(raised.exception)
        self.assertIn("COOLIFY_API_TOKEN__EXAMPLE", message)
        self.assertNotIn("default-token", message)

    def test_plain_names_alone_give_one_default_account(self) -> None:
        env = {
            "COOLIFY_API_TOKEN": "plain-token",
            "COOLIFY_BASE_URL": "https://coolify.example.test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["default"])
            name = self.module.selected_profile_name(SimpleNamespace(profile=None))
            profile = self.module.get_profile(name)
        self.assertEqual(name, "default")
        self.assertEqual(profile.token, "plain-token")
        self.assertEqual(profile.base_url, "https://coolify.example.test")

    def test_legacy_bare_aliases_still_configure_the_default_account(self) -> None:
        env = {
            "COOLIFY_TOKEN": "bare-token",
            "COOLIFY_URL": "https://coolify.example.test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["default"])
            profile = self.module.get_profile("default")
        self.assertEqual(profile.token, "bare-token")
        self.assertEqual(profile.base_url, "https://coolify.example.test")

    def test_legacy_infix_keys_still_resolve_and_are_discovered(self) -> None:
        env = {
            "COOLIFY_EXAMPLE_TWO_LABEL": "Example Two",
            "COOLIFY_EXAMPLE_TWO_TOKEN": "legacy-token",
            "COOLIFY_EXAMPLE_TWO_BASE_URL": "https://example-two.example.test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example-two"])
            profile = self.module.get_profile("example-two")
        self.assertEqual(profile.token, "legacy-token")
        self.assertEqual(profile.base_url, "https://example-two.example.test")
        self.assertEqual(profile.label, "Example Two")

    def test_plain_token_is_not_discovered_as_an_account_named_api(self) -> None:
        env = {"COOLIFY_API_TOKEN": "plain-token"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.discovered_profile_names(), ["default"])

    def test_missing_default_config_names_the_plain_keys(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.module.CoolifyError) as raised:
                self.module.get_profile("default")
        message = str(raised.exception)
        self.assertIn("COOLIFY_API_TOKEN", message)
        self.assertIn("COOLIFY_BASE_URL", message)
        self.assertNotIn("__", message)

    def test_validate_base_url_strips_api_v1(self) -> None:
        self.assertEqual(
            self.module.validate_base_url("https://coolify.example.com/api/v1"),
            "https://coolify.example.com",
        )

    def test_validate_base_url_rejects_credentials(self) -> None:
        with self.assertRaises(self.module.CoolifyError):
            self.module.validate_base_url("https://user:pass@coolify.example.com")

    def test_restart_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            profile="example",
            kind="application",
            uuid="app-1",
            confirm=False,
            json=False,
        )
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "request"
        ) as mock_request:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_restart(args)
        self.assertEqual(code, 0)
        self.assertIn("mode\tdry-run", buf.getvalue())
        mock_request.assert_not_called()

    def test_restart_confirm_posts(self) -> None:
        args = SimpleNamespace(
            profile="example",
            kind="application",
            uuid="app-1",
            confirm=True,
            json=False,
        )
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "request", return_value={"message": "Restart request queued."}
        ) as mock_request:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_restart(args)
        self.assertEqual(code, 0)
        self.assertIn("mode\tconfirmed", buf.getvalue())
        mock_request.assert_called_once()
        self.assertEqual(mock_request.call_args.args[1], "POST")
        self.assertEqual(mock_request.call_args.args[2], "applications/app-1/restart")

    def test_deploy_requires_uuid_or_tag(self) -> None:
        args = SimpleNamespace(
            profile="example",
            uuid=None,
            tag=None,
            force=False,
            confirm=False,
            json=False,
        )
        with patch.object(self.module, "get_profile", return_value=self.profile):
            with self.assertRaises(self.module.CoolifyError):
                self.module.cmd_deploy(args)

    def test_envs_redact_values_by_default(self) -> None:
        args = SimpleNamespace(
            profile="example",
            kind="application",
            uuid="app-1",
            show_values=False,
            json=False,
        )
        payload = [{"uuid": "e1", "key": "DB_PASSWORD", "value": "super-secret"}]
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "request", return_value=payload
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_envs(args)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("DB_PASSWORD", out)
        self.assertIn("<set>", out)
        self.assertNotIn("super-secret", out)

    def test_applications_list_csv(self) -> None:
        args = SimpleNamespace(profile="example", limit=10, json=False)
        payload = [
            {
                "uuid": "app-1",
                "name": "web",
                "status": "running",
                "fqdn": "https://example.com",
                "git_repository": "org/repo",
                "git_branch": "main",
                "server": {"name": "prod"},
            }
        ]
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "request", return_value=payload
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_applications(args)
        self.assertEqual(code, 0)
        self.assertIn("app-1", buf.getvalue())
        self.assertIn("web", buf.getvalue())

    def test_main_help_exits_clean(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.module.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("applications", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
