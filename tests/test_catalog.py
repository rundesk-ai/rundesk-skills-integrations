"""The service integration catalog and every packaged command, entirely offline."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ALLOWED = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class IntegrationCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_declares_every_complete_skill(self):
        self.assertEqual(1, self.manifest["schema"])
        self.assertEqual("rundesk-skills-integrations", self.manifest["name"])
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")
        declared = {entry["name"]: entry["path"] for entry in self.manifest["skills"]}
        self.assertEqual(
            {"cloudflare", "confluence", "coolify", "discord", "jira", "monarch", "sentry", "stripe"},
            set(declared),
        )
        self.assertEqual(
            sorted(declared),
            sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir()),
        )
        for name, relative in declared.items():
            with self.subTest(skill=name):
                self.assertRegex(name, ALLOWED)
                package = ROOT / relative
                self.assertEqual(name, package.name)
                page = (package / "SKILL.md").read_text(encoding="utf-8")
                self.assertRegex(page, rf"(?m)^name: {re.escape(name)}$")
                frontmatter = page.split("---", 2)[1]
                keys = [line.split(":", 1)[0] for line in frontmatter.splitlines()
                        if line and not line.startswith(" ")]
                self.assertEqual(["name", "description"], keys)
                description = re.search(
                    r"(?m)^description: (.+)$", frontmatter
                ).group(1)
                self.assertLessEqual(len(description), 1024)
                self.assertIn("Use ", description)
                self.assertLess(len(page.splitlines()), 500)
                self.assertFalse((package / "README.md").exists())
                self.assertFalse((package / "CHANGELOG.md").exists())
                self.assertTrue((package / "scripts" / name).is_file())

    def test_every_launcher_has_credential_free_help(self):
        clean = {key: value for key, value in os.environ.items()
                 if not key.endswith(("_TOKEN", "_API_TOKEN", "_GLOBAL_KEY"))}
        for entry in self.manifest["skills"]:
            command = ROOT / entry["path"] / "scripts" / entry["name"]
            with self.subTest(skill=entry["name"]):
                completed = subprocess.run(
                    [str(command), "--help"], capture_output=True, text=True,
                    check=False, env=clean,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("usage:", (completed.stdout + completed.stderr).lower())

    def test_every_package_offline_suite_passes(self):
        for entry in self.manifest["skills"]:
            support = ROOT / entry["path"] / "scripts" / f"{entry['name']}.d"
            tests = list(support.glob("test-*.py"))
            with self.subTest(skill=entry["name"]):
                self.assertEqual(1, len(tests))
                completed = subprocess.run(
                    [sys.executable, str(tests[0]), "-q"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_repository_contains_no_private_routing_owner_paths_or_credentials(self):
        forbidden = (
            "/Users/", "rocketquote", "invelo",
            "BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
        )
        for path in (ROOT / "skills").rglob("*"):
            if (path.is_file() and ".git" not in path.parts
                    and "__pycache__" not in path.parts and path.suffix != ".pyc"):
                with self.subTest(path=path.relative_to(ROOT)):
                    raw = path.read_text(encoding="utf-8", errors="ignore")
                    text = raw.lower()
                    self.assertFalse(any(value.lower() in text for value in forbidden))
                    self.assertNotRegex(
                        raw,
                        r"\b(?:after|unless)\s+(?!the owner\b)[A-Z][a-z]+\s+approve",
                    )
                    self.assertNotIn("## use when", text)

    def test_environment_contract_is_documented_and_implemented(self):
        documented = (ROOT / "ENVIRONMENTS.md").read_text(encoding="utf-8")
        self.assertIn("RUNDESK_INTEGRATIONS_ENV", documented)
        for entry in self.manifest["skills"]:
            implementation = next(
                (ROOT / entry["path"] / "scripts" / f"{entry['name']}.d").glob("*.py")
            ).parent / f"{entry['name']}.py"
            text = implementation.read_text(encoding="utf-8")
            self.assertIn("RUNDESK_INTEGRATIONS_ENV", text)
            self.assertIn("rundesk", text)
            self.assertIn("integrations", text)

    def test_each_isolated_default_env_is_discovered_without_shell_exports(self):
        variables = {
            "cloudflare": [
                "CLOUDFLARE_PROFILES=demo", "CLOUDFLARE_DEMO_TOKEN=synthetic-token",
            ],
            "confluence": [
                "CONFLUENCE_PROFILES=demo", "CONFLUENCE_DEMO_BASE_URL=https://example.atlassian.net",
                "CONFLUENCE_DEMO_EMAIL=agent@example.test", "CONFLUENCE_DEMO_API_TOKEN=synthetic-token",
            ],
            "coolify": [
                "COOLIFY_PROFILES=demo", "COOLIFY_DEMO_BASE_URL=https://coolify.example.test",
                "COOLIFY_DEMO_TOKEN=synthetic-token",
            ],
            "discord": [
                "DISCORD_PROFILES=demo", "DISCORD_DEMO_TOKEN=synthetic-token",
            ],
            "jira": [
                "JIRA_PROFILES=demo", "JIRA_DEMO_BASE_URL=https://example.atlassian.net",
                "JIRA_DEMO_EMAIL=agent@example.test", "JIRA_DEMO_API_TOKEN=synthetic-token",
            ],
            "monarch": [
                "MONARCH_PROFILES=demo", "MONARCH_DEMO_EMAIL=agent@example.test",
                "MONARCH_DEMO_PASSWORD=synthetic-password",
            ],
            "sentry": [
                "SENTRY_PROFILES=demo", "SENTRY_DEMO_TOKEN=synthetic-token",
                "SENTRY_DEMO_ORG=example", "SENTRY_DEMO_BASE_URL=https://sentry.example.test",
            ],
            "stripe": [
                "STRIPE_PROFILES=demo", "STRIPE_DEMO_KEY=rk_test_synthetic",
                "STRIPE_DEMO_LABEL=Example",
            ],
        }
        with tempfile.TemporaryDirectory(prefix="rundesk-integration-env-") as temporary:
            root = Path(temporary)
            clean = {
                key: value for key, value in os.environ.items()
                if not key.startswith(tuple(name.upper() for name in variables))
                and key != "RUNDESK_INTEGRATIONS_ENV"
            }
            clean.update({"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")})
            for name, lines in variables.items():
                env_file = root / "config" / "rundesk" / "integrations" / name / "env"
                env_file.parent.mkdir(parents=True)
                env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                env_file.chmod(0o600)
                command = ROOT / "skills" / name / "scripts" / name
                completed = subprocess.run(
                    [str(command), "profiles"], capture_output=True, text=True,
                    check=False, env=clean,
                )
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("demo", completed.stdout)


if __name__ == "__main__":
    unittest.main()
