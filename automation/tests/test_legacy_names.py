"""Legacy variable names warn before v0.6.0 stops reading them (SPEC-009 RA-ENT-002)."""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from automation.supervisor import run_next, settings
from automation.tests.test_compatibility_policy import sync_deprecated_keys

REPO_ROOT = Path(__file__).resolve().parents[2]
UPGRADING = REPO_ROOT / "docs/workflows/upgrading.md"
# Pages that name the release the old names stop working in. Each one must keep saying it.
REMOVAL_PAGES = (
    "CHANGELOG.md",
    "automation/README.md",
    "docs/architecture/enterprise-readiness.md",
    "docs/reference/troubleshooting.md",
    "docs/workflows/boundroom.md",
    "docs/workflows/upgrading.md",
)
# A release right after "stop working in", "until" or "aliases in", or right before "refuses" or
# "stops reading". Other releases a page mentions, such as "the first release after v0.4.0", don't match.
VERSION = r"`?(v\d+\.\d+\.\d+)`?"
REMOVAL = re.compile(rf"(?:stops? working in|[Uu]ntil|aliases in) {VERSION}|{VERSION} (?:refuses|stops reading)")


# A line about the retired names. Another deprecation's removal release, on a line of its own, isn't checked.
OLD_NAMES = re.compile(r"REPO_AUTOMATION_|OWLORY_|allow_owlory_specific|Owlory|[Oo]ld (?:variable )?names?")


def removal_versions(text: str) -> list[str]:
    return [before or after for before, after in REMOVAL.findall(text)]


def legacy_removal_versions(text: str) -> list[str]:
    return [version for line in text.splitlines() if OLD_NAMES.search(line) for version in removal_versions(line)]


def operator_legacy_names() -> dict[str, str]:
    """Every legacy name an operator might set, mapped to its replacement."""
    return {
        name: setting.env
        for setting in settings.SETTINGS
        if setting.id not in settings.SUPERVISOR_EXPORTS and setting.env
        for name in setting.legacy_env
    }


class LegacyEnvWarningTests(unittest.TestCase):
    def test_there_are_legacy_names_to_warn_about(self) -> None:
        names = operator_legacy_names()
        self.assertIn("REPO_AUTOMATION_AGENT_RUNNER", names)
        self.assertEqual("BOUNDROOM_CODEX_BIN", names["OWLORY_CODEX_BIN"])

    def test_each_legacy_name_warns_and_names_its_replacement(self) -> None:
        for name, replacement in operator_legacy_names().items():
            with self.subTest(name=name):
                self.assertEqual(
                    [f"warning: {name} is deprecated and stops working in v0.6.0; use {replacement}."],
                    settings.legacy_env_warnings({name: "x"}),
                )

    def test_a_shadowed_legacy_name_says_it_is_ignored(self) -> None:
        warnings = settings.legacy_env_warnings({"REPO_AUTOMATION_CLAUDE_BIN": "old", "BOUNDROOM_CLAUDE_BIN": "new"})
        self.assertEqual(1, len(warnings))
        self.assertTrue(
            warnings[0].endswith("; BOUNDROOM_CLAUDE_BIN is also set, so REPO_AUTOMATION_CLAUDE_BIN is ignored."),
            warnings[0],
        )

    def test_the_supervisors_own_exports_raise_no_warning(self) -> None:
        exports = {
            name: "x" for setting in settings.SETTINGS if setting.id in settings.SUPERVISOR_EXPORTS for name in setting.env_names
        }
        self.assertIn("OWLORY_SUPERVISOR_SLICE_ID", exports)
        self.assertEqual([], settings.legacy_env_warnings(exports))

    def test_current_names_raise_no_warning(self) -> None:
        current = {setting.env: "x" for setting in settings.SETTINGS if setting.env}
        self.assertEqual([], settings.legacy_env_warnings(current))

    def test_run_agent_sh_names_the_same_removal_release(self) -> None:
        """Its variable, usage text and comments, and the sync script's retired key, all move together."""
        text = (REPO_ROOT / "automation/supervisor/run_agent.sh").read_text(encoding="utf-8")
        self.assertIn(f'legacy_removal="{settings.LEGACY_ENV_REMOVAL}"', text)
        self.assertEqual({settings.LEGACY_ENV_REMOVAL}, set(re.findall(r"v\d+\.\d+\.\d+", text)))
        self.assertEqual(settings.LEGACY_ENV_REMOVAL, sync_deprecated_keys()["allow_owlory_specific"])

    def test_run_next_prints_the_warnings_before_it_starts(self) -> None:
        """A run on an empty queue, in the environment it is given, warns first and then stops cleanly."""
        stderr, stdout = io.StringIO(), io.StringIO()
        example = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "slices.json"
            queue.write_text(json.dumps({**example, "slices": []}), encoding="utf-8")
            environ = {"REPO_AUTOMATION_HERMES_BIN": "hermes"}
            with redirect_stderr(stderr), redirect_stdout(stdout):
                code = run_next.main(["--dry-run", "--queue", str(queue)], repo_root=Path(directory), environ=environ)
        self.assertEqual(0, code)
        self.assertEqual(
            "warning: REPO_AUTOMATION_HERMES_BIN is deprecated and stops working in v0.6.0; use BOUNDROOM_HERMES_BIN.",
            stderr.getvalue().splitlines()[0],
        )
        self.assertEqual("stop: no eligible queued slice found.\n", stdout.getvalue())


class UpgradeGuideTests(unittest.TestCase):
    def test_the_upgrade_guide_names_each_legacy_name_and_its_replacement(self) -> None:
        text = UPGRADING.read_text(encoding="utf-8")
        rows = dict(re.findall(r"^\| `([A-Z0-9_]+)` \| `([A-Z0-9_]+)` \|", text, re.MULTILINE))
        every = {name: setting.env for setting in settings.SETTINGS if setting.env for name in setting.legacy_env}
        self.assertEqual(every, {name: rows.get(name) for name in every})

    def test_the_docs_name_the_same_removal_release(self) -> None:
        """Each page that tells a reader when the old names stop working moves with settings.py."""
        for page in REMOVAL_PAGES:
            with self.subTest(page=page):
                found = legacy_removal_versions((REPO_ROOT / page).read_text(encoding="utf-8"))
                self.assertTrue(found, f"{page} no longer says when the old names stop working")
                self.assertEqual({settings.LEGACY_ENV_REMOVAL}, set(found))

    def test_the_removal_pattern_skips_other_releases(self) -> None:
        self.assertEqual(["v9.9.0"], removal_versions("It stops working in v9.9.0. Releases up to v0.4.0 have none."))
        self.assertEqual(["v9.9.0"], removal_versions("| Old name | Replacement | Until v9.9.0 |"))
        self.assertEqual(["v9.9.0"], removal_versions("and v9.9.0 refuses the key."))
        self.assertEqual([], removal_versions("The first release after v0.4.0 carries the warnings, and v0.5.0 too."))
        other = "`Tools/repo-automation-sync.sh` stops working in v9.8.0.\n`OWLORY_CODEX_BIN` stops working in v9.9.0."
        self.assertEqual(["v9.9.0"], legacy_removal_versions(other))


if __name__ == "__main__":
    unittest.main()
