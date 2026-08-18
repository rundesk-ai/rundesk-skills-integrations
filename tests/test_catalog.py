"""The service integration catalog and every packaged command, entirely offline."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
ALLOWED = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Rundesk accepts a declared variable name only in this shape, and reserves the double
# underscore as the separator between a field and an account.
DECLARED_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
CATALOG_GUIDE = "https://github.com/rundesk-ai/rundesk-cli/blob/main/docs/catalogs.md"
AGENT_GUIDE_HEADINGS = tuple("""# AGENTS
## Purpose
## Before you work
## Repository layout
## Package and artifact contract
## Safety and approval gates
## Delegation
## Architecture and conventions
## Documentation duties
## Build, test, and run
## Pull requests and releases
## Definition of done""".splitlines())
README_HEADINGS = tuple("""# Rundesk Integration Skills
## Skills
## Install
## Requirements
## Repository layout
## Development
## Creating a skill catalog
## Contributing
## Releases
## License""".splitlines())
PR_HEADINGS = tuple("""## Summary
## Scope and compatibility
## Critical risk
## Validation
## Repository gates
## Release
## Manual user path
## Agent""".splitlines())
ISSUE_TEMPLATE_CONTRACTS = {
    "bug-report.md": (
        ("name: Bug report", "about: Report reproducible incorrect behavior",
         'title: "[Bug] "', 'labels: ""', 'assignees: ""'),
        ("## Problem", "## Reproduction", "## Expected behavior", "## Evidence",
         "## Environment", "## Scope and privacy"),
        "747da5c0682a73adc61c35407327fb174c648630e80278c275af4a4542da6caf",
    ),
    "change-proposal.md": (
        ("name: Change proposal",
         "about: Propose a skill, integration, command, or repository improvement",
         'title: "[Proposal] "', 'labels: ""', 'assignees: ""'),
        ("## Problem", "## Desired outcome", "## Users and value",
         "## Scope and compatibility", "## Alternatives", "## Validation"),
        "2fe6a1d651ce91af2c3d19e98eea150ca26f41ad9a1ed95a6466a692b73eb4d7",
    ),
}
AGENT_GUIDE_ANCHORS = {
    "runtime": ("Python 3.9", "standard library"),
    "offline boundary": ("offline test", "network"),
    "package isolation": ("Packages do not depend on sibling packages",),
    "secret redaction": ("commit or print credentials", "raw dotenv content"),
    "bounded reads": ("Bound every read", "truncation"),
    "mutation confirmation": ("Preview every mutation", "exact confirmation input"),
    "profile ambiguity": (
        "A named account never falls back to a plain value",
        "conflicting or incomplete forms are refused",
    ),
    "validation commands": (
        "python3 -m unittest discover -s tests -v",
        "python3 skills/cloudflare/scripts/cloudflare.d/test-cloudflare.py -q",
        "skills/cloudflare/scripts/cloudflare --help",
        '(cd /tmp && "$repository_root/skills/cloudflare/scripts/cloudflare" --help)',
    ),
    "privacy evidence": ("inspect the complete diff and commit-visible artifacts",),
    "diff check": ("git diff --check",),
    "exact head": ("exact pull request head commit",),
}
README_ANCHORS = (
    "rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations",
    "rundesk skills install https://github.com/rundesk-ai/rundesk-skills-integrations --confirm",
    "rundesk skills grant agent-name rundesk-skills-integrations/cloudflare",
    "rundesk skills configure rundesk-skills-integrations/jira",
    "rundesk skills profiles rundesk-skills-integrations/jira",
    ".github/ISSUE_TEMPLATE/bug-report.md",
    ".github/ISSUE_TEMPLATE/change-proposal.md",
    ".github/pull_request_template.md",
    "guarded edits to a transaction's category, merchant, notes, and tags",
    "category creation; transaction-rule creation and deletion; budget setting; and undo",
    "cannot change a transaction's amount, date, or account",
    "delete a transaction or category, or split a transaction",
    "python3 skills/cloudflare/scripts/cloudflare.d/test-cloudflare.py -q",
    '(cd /tmp && "$repository_root/skills/cloudflare/scripts/cloudflare" --help)',
)
PR_CHECKLIST_ANCHORS = (
    "Every mutation remains a preview until the owner approves the exact target and effect and "
    "supplies the package's exact confirmation input.",
    "Required GitHub checks pass for the exact head commit.",
    "`git diff --check`",
    "Reads remain bounded by default and report truncation explicitly.",
    "No package imports, executes, or depends on a sibling package.",
    "Runtime code remains Python 3.9+ and standard-library only, unless the owner approved a dependency.",
    "Tests remain offline and replace every network boundary with synthetic fixtures.",
    "Credential-free help, offline profiles, secret redaction, and configuration precedence remain intact.",
    "Ambiguous account or profile selection is refused, and credential forms never mix or fall back across accounts.",
    "The diff contains no credential, customer identifier, private-project language, owner-specific path, or unrelated artifact.",
)


class IntegrationCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    @staticmethod
    def markdown_headings(path):
        headings = []
        in_fence = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
            elif not in_fence and re.fullmatch(r"#{1,2} .+", line):
                headings.append(line)
        return tuple(headings)

    @staticmethod
    def shell_fences(text):
        return re.findall(r"(?ms)^```sh\n(.*?)^```$", text)

    def test_repository_guides_are_identical_and_structured(self):
        agents = ROOT / "AGENTS.md"
        claude = ROOT / "CLAUDE.md"
        self.assertTrue(claude.is_file())
        self.assertFalse(claude.is_symlink())
        self.assertEqual(agents.read_bytes(), claude.read_bytes())
        self.assertEqual(AGENT_GUIDE_HEADINGS, self.markdown_headings(agents))
        text = agents.read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        self.assertIn(CATALOG_GUIDE, text)
        for purpose, anchors in AGENT_GUIDE_ANCHORS.items():
            with self.subTest(contract=purpose):
                for anchor in anchors:
                    self.assertIn(" ".join(anchor.split()), normalized)
        for fence in self.shell_fences(text):
            self.assertNotRegex(fence, r"<[^>\n]+>")

    def test_repository_templates_follow_the_contract(self):
        pull_request = ROOT / ".github" / "pull_request_template.md"
        self.assertEqual(PR_HEADINGS, self.markdown_headings(pull_request))
        self.assertIn("🤖 by <Agent>", pull_request.read_text(encoding="utf-8"))
        pull_request_text = pull_request.read_text(encoding="utf-8")
        normalized_pull_request = " ".join(pull_request_text.split())
        for anchor in PR_CHECKLIST_ANCHORS:
            self.assertIn(" ".join(f"- [ ] {anchor}".split()), normalized_pull_request)
        issue_root = ROOT / ".github" / "ISSUE_TEMPLATE"
        self.assertEqual(
            set(ISSUE_TEMPLATE_CONTRACTS) | {"config.yml"},
            {path.name for path in issue_root.iterdir() if path.is_file()},
        )
        self.assertEqual(
            b"blank_issues_enabled: false\n",
            (issue_root / "config.yml").read_bytes(),
        )
        for filename, (frontmatter, headings, digest) in ISSUE_TEMPLATE_CONTRACTS.items():
            with self.subTest(template=filename):
                path = issue_root / filename
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                self.assertEqual(["", *frontmatter], text.split("---", 2)[1].splitlines())
                self.assertEqual(headings, self.markdown_headings(path))
                self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_readme_follows_the_catalog_contract(self):
        readme = ROOT / "README.md"
        self.assertEqual(README_HEADINGS, self.markdown_headings(readme))
        text = readme.read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        self.assertIn(CATALOG_GUIDE, text)
        self.assertNotIn("<agent>", text)
        for anchor in README_ANCHORS:
            self.assertIn(" ".join(anchor.split()), normalized)
        for fence in self.shell_fences(text):
            self.assertNotRegex(fence, r"<[^>\n]+>")

    def test_public_repository_docs_contain_no_private_material(self):
        forbidden = (
            "/Users/", "BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
        )
        paths = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", ROOT / "README.md"]
        paths.extend((ROOT / ".github").rglob("*.md"))
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_text(encoding="utf-8").lower()
                self.assertFalse(any(value.lower() in text for value in forbidden))

    def test_manifest_declares_every_complete_skill(self):
        self.assertEqual(1, self.manifest["schema"])
        self.assertEqual("rundesk-skills-integrations", self.manifest["name"])
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")
        declared = {entry["name"]: entry["path"] for entry in self.manifest["skills"]}
        self.assertEqual(
            {"cloudflare", "confluence", "coolify", "discord", "jira", "monarch", "sentry", "slack-fetch", "stripe"},
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

    def test_every_skill_declares_its_needs_for_rundesk(self):
        """`rundesk skills configure`, `profiles`, and `doctor` all read this one file."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                declaration = json.loads(
                    (ROOT / entry["path"] / "rundesk.json").read_text(encoding="utf-8")
                )
                self.assertEqual(["needs"], list(declaration))
                needs = declaration["needs"]
                self.assertIsInstance(needs, dict)
                self.assertTrue(needs, "a skill with no declared need cannot be configured")
                for name, reason in needs.items():
                    with self.subTest(variable=name):
                        self.assertRegex(name, DECLARED_NAME)
                        self.assertNotIn(
                            "__", name, "the double underscore is Rundesk's account separator"
                        )
                        skill_prefix = entry["name"].upper().replace("-", "_")
                        self.assertTrue(name.startswith(skill_prefix))
                        self.assertIsInstance(reason, str)
                        # The reason is what a person reads when told the value is missing,
                        # so it has to say where the value comes from, not restate the name.
                        self.assertGreater(len(reason), 40)
                        self.assertNotIn(name, reason)

    def test_declared_needs_match_the_required_fields_each_command_resolves(self):
        """A declaration the command does not read is a value Rundesk collects for nothing."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                declaration = json.loads(
                    (ROOT / entry["path"] / "rundesk.json").read_text(encoding="utf-8")
                )
                module = self.load_command(entry)
                self.assertEqual(
                    sorted(declaration["needs"]), sorted(module.REQUIRED_FIELDS)
                )
                for name in module.REQUIRED_FIELDS:
                    self.assertIn(name, module.PROFILE_FIELDS)

    def test_every_command_prefers_the_rundesk_account_suffix_over_the_legacy_form(self):
        """Both spellings resolve; the one Rundesk writes wins, and never leaks across accounts."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                skill = entry["name"].upper().replace("-", "_")
                for field in module.REQUIRED_FIELDS:
                    with self.subTest(field=field):
                        suffix = module.PROFILE_FIELDS[field]
                        env = {
                            f"{field}__ACME": "rundesk-value",
                            f"{skill}_ACME_{suffix}": "legacy-value",
                            field: "default-value",
                        }
                        with unittest.mock.patch.dict(os.environ, env, clear=True):
                            self.assertEqual(
                                "rundesk-value", module.profile_value("acme", field)
                            )
                        with unittest.mock.patch.dict(
                            os.environ, {k: v for k, v in env.items() if "__" not in k},
                            clear=True,
                        ):
                            self.assertEqual(
                                "legacy-value", module.profile_value("acme", field)
                            )
                        with unittest.mock.patch.dict(os.environ, {field: "default-value"},
                                                      clear=True):
                            self.assertEqual(
                                "default-value", module.profile_value("default", field)
                            )
                            self.assertEqual("", module.profile_value("acme", field))

    def test_every_command_discovers_a_rundesk_account_without_a_declaration(self):
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                env = {f"{field}__ACME_TWO": "value" for field in module.REQUIRED_FIELDS}
                with unittest.mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(["acme-two"], module.configured_profile_names())
                plain = {field: "value" for field in module.REQUIRED_FIELDS}
                with unittest.mock.patch.dict(os.environ, plain, clear=True):
                    self.assertEqual(["default"], module.configured_profile_names())

    def test_no_account_name_is_invented_from_a_double_underscore(self):
        """The legacy infix scan must not claim a key the account-suffix scan owns."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                skill = entry["name"].upper().replace("-", "_")
                for field, suffix in module.PROFILE_FIELDS.items():
                    with self.subTest(field=field):
                        # An account whose last word is itself a field suffix is the shape
                        # that a greedy infix pattern misreads.
                        with unittest.mock.patch.dict(
                            os.environ, {f"{field}__ACME_{suffix}": "value"}, clear=True
                        ):
                            self.assertEqual(
                                [module.profile_label(f"ACME_{suffix}")],
                                module.discovered_profile_names(),
                            )
                        with unittest.mock.patch.dict(
                            os.environ, {f"{skill}_ACME__{suffix}": "value"}, clear=True
                        ):
                            self.assertEqual([], module.discovered_profile_names())

    def test_no_plain_declared_name_is_read_as_an_account(self):
        """`STRIPE_API_KEY` is the default account's key, never an account named `api`."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                aliases = getattr(module, "PLAIN_ALIASES", {})
                names = set(module.PROFILE_FIELDS)
                names.update(getattr(module, "SHARED_ATLASSIAN_FIELDS", {}).values())
                for alias_group in aliases.values():
                    names.update(alias_group)
                for name in sorted(names):
                    with self.subTest(variable=name):
                        with unittest.mock.patch.dict(
                            os.environ, {name: "value"}, clear=True
                        ):
                            self.assertLessEqual(
                                set(module.discovered_profile_names()), {"default"}
                            )

    def test_a_legacy_account_beside_a_plain_value_stays_one_account(self):
        """Before Rundesk a plain value was every profile's fallback, so inventing a
        `default` account next to a legacy one would refuse every command as ambiguous."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                skill = entry["name"].upper().replace("-", "_")
                env = {field: "value" for field in module.REQUIRED_FIELDS}
                env.update({
                    f"{skill}_PROD_{module.PROFILE_FIELDS[field]}": "value"
                    for field in module.REQUIRED_FIELDS
                })
                with unittest.mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(["prod"], module.configured_profile_names())
                    self.assertEqual(
                        "prod", module.selected_profile_name(SimpleNamespace(profile=None))
                    )

    def test_the_default_word_in_the_legacy_infix_names_the_default_account(self):
        """`<SKILL>_DEFAULT_<FIELD>` resolves, so it must not be dropped from the listing."""
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                skill = entry["name"].upper().replace("-", "_")
                env = {
                    f"{skill}_DEFAULT_{module.PROFILE_FIELDS[field]}": "value"
                    for field in module.REQUIRED_FIELDS
                }
                with unittest.mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(["default"], module.discovered_profile_names())
                    for field in module.REQUIRED_FIELDS:
                        self.assertEqual("value", module.profile_value("default", field))

    def test_explicit_profiles_variable_overrides_discovery_everywhere(self):
        for entry in self.manifest["skills"]:
            with self.subTest(skill=entry["name"]):
                module = self.load_command(entry)
                skill = entry["name"].upper().replace("-", "_")
                env = {f"{skill}_PROFILES": "named"}
                env.update({f"{field}__ACME": "value" for field in module.REQUIRED_FIELDS})
                with unittest.mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(["named"], module.configured_profile_names())

    def test_every_launcher_and_script_stays_executable(self):
        """Rundesk reports a non-executable script as a fault."""
        for entry in self.manifest["skills"]:
            scripts = ROOT / entry["path"] / "scripts"
            for path in sorted(scripts.rglob("*")):
                if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts:
                    with self.subTest(script=path.relative_to(ROOT)):
                        self.assertTrue(os.access(path, os.X_OK))

    @staticmethod
    def load_command(entry):
        """Import one package's implementation module by path, without installing anything."""
        script = ROOT / entry["path"] / "scripts" / f"{entry['name']}.d" / f"{entry['name']}.py"
        spec = importlib.util.spec_from_file_location(f"{entry['name']}_command", script)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_readme_lists_exactly_the_declared_skills(self):
        """A catalog that ships a skill its README never mentions is a catalog nobody trusts."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"(?m)^- `([a-z0-9-]+)`", readme))
        declared = {entry["name"] for entry in self.manifest["skills"]}
        self.assertEqual(declared, listed, "README.md and manifest.json disagree")

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
            "/Users/", "BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
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
            "slack-fetch": [
                "SLACK_PROFILES=demo", "SLACK_DEMO_TOKEN=synthetic-token",
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
