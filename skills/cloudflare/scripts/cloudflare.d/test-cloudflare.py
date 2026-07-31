#!/usr/bin/env python3
"""Offline tests for cloudflare."""

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
SCRIPT = MODULE_DIR / "cloudflare.py"


def load_module():
    spec = importlib.util.spec_from_file_location("cloudflare_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CloudflareModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.profile = self.module.Profile(
            name="example",
            token="token",
            email="",
            global_key="",
            account_id="acct-1",
            label="Example",
        )

    def test_get_profile_maps_token_profile(self) -> None:
        env = {
            "CLOUDFLARE_PROFILES": "example",
            "CLOUDFLARE_DEFAULT_PROFILE": "example",
            "CLOUDFLARE_EXAMPLE_LABEL": "Example CF",
            "CLOUDFLARE_EXAMPLE_TOKEN": "secret",
            "CLOUDFLARE_EXAMPLE_ACCOUNT_ID": "acct-1",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.module.configured_profile_names(), ["example"])
            profile = self.module.get_profile("example")
        self.assertEqual(profile.label, "Example CF")
        self.assertEqual(profile.token, "secret")
        self.assertEqual(profile.account_id, "acct-1")
        self.assertTrue(profile.has_bearer())

    def test_get_profile_supports_global_key(self) -> None:
        env = {
            "CLOUDFLARE_EXAMPLE_EMAIL": "owner@example.com",
            "CLOUDFLARE_EXAMPLE_GLOBAL_KEY": "global-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            profile = self.module.get_profile("example")
        self.assertTrue(profile.has_global_key())
        headers = profile.auth_headers()
        self.assertEqual(headers["X-Auth-Email"], "owner@example.com")
        self.assertEqual(headers["X-Auth-Key"], "global-secret")
        self.assertNotIn("Authorization", headers)

    def test_normalize_domain_rejects_junk(self) -> None:
        with self.assertRaises(self.module.CloudflareError):
            self.module.normalize_domain("not a domain")
        self.assertEqual(self.module.normalize_domain("Example.COM."), "example.com")

    def test_request_unwraps_success_envelope(self) -> None:
        payload = {"success": True, "result": [{"id": "z1", "name": "example.com"}]}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        with patch.object(self.module.urllib.request, "urlopen", return_value=FakeResponse()):
            data = self.module.request(self.profile, "GET", "zones")
        self.assertEqual(data["result"][0]["name"], "example.com")

    def test_register_is_dry_run_without_confirm(self) -> None:
        args = SimpleNamespace(
            profile="example",
            domain="new-brand.example",
            years=1,
            auto_renew=True,
            privacy=True,
            confirm=False,
            json=False,
        )
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "resolve_account_id", return_value="acct-1"
        ), patch.object(self.module, "find_zone", return_value=None), patch.object(
            self.module, "request"
        ) as mock_request:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_register(args)
        self.assertEqual(code, 0)
        self.assertIn("mode\tdry-run", buf.getvalue())
        mock_request.assert_not_called()

    def test_register_refuses_existing_zone(self) -> None:
        args = SimpleNamespace(
            profile="example",
            domain="example.com",
            years=1,
            auto_renew=True,
            privacy=True,
            confirm=False,
            json=False,
        )
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "resolve_account_id", return_value="acct-1"
        ), patch.object(
            self.module, "find_zone", return_value={"id": "z1", "name": "example.com"}
        ):
            with self.assertRaises(self.module.CloudflareError):
                self.module.cmd_register(args)

    def test_dns_add_dry_run_does_not_post(self) -> None:
        args = SimpleNamespace(
            profile="example",
            domain="example.com",
            type="A",
            name="www",
            content="1.2.3.4",
            ttl=1,
            proxied=True,
            priority=None,
            comment=None,
            confirm=False,
            json=False,
        )
        zone = {"id": "zone-1", "name": "example.com"}
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "require_zone", return_value=zone
        ), patch.object(self.module, "request") as mock_request:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_dns_add(args)
        self.assertEqual(code, 0)
        self.assertIn("mode\tdry-run", buf.getvalue())
        mock_request.assert_not_called()

    def test_dns_add_confirm_posts(self) -> None:
        args = SimpleNamespace(
            profile="example",
            domain="example.com",
            type="A",
            name="www",
            content="1.2.3.4",
            ttl=1,
            proxied=True,
            priority=None,
            comment=None,
            confirm=True,
            json=False,
        )
        zone = {"id": "zone-1", "name": "example.com"}
        created = {
            "success": True,
            "result": {
                "id": "rec-1",
                "type": "A",
                "name": "www.example.com",
                "content": "1.2.3.4",
                "ttl": 1,
                "proxied": True,
                "proxiable": True,
                "locked": False,
            },
        }
        with patch.object(self.module, "get_profile", return_value=self.profile), patch.object(
            self.module, "require_zone", return_value=zone
        ), patch.object(self.module, "request", return_value=created) as mock_request:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.module.cmd_dns_add(args)
        self.assertEqual(code, 0)
        self.assertIn("mode\tconfirmed", buf.getvalue())
        mock_request.assert_called_once()
        self.assertEqual(mock_request.call_args.args[1], "POST")

    def test_main_help_has_no_credentials(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.module.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("zones", buf.getvalue())
        self.assertNotIn("bearer", buf.getvalue().lower())
        self.assertNotIn("api_token=", buf.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
