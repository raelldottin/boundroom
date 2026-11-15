"""Legacy variable names warn before v0.5.0 stops reading them (SPEC-009 RA-ENT-002)."""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from automation.supervisor import run_next, settings

REPO_ROOT = Path(__file__).resolve().parents[2]
UPGRADING = REPO_ROOT / "docs/workflows/upgrading.md"


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
                    [f"warning: {name} is deprecated and stops working in v0.5.0; use {replacement}."],
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

    def test_run_next_prints_the_warnings_before_it_starts(self) -> None:
        stderr = io.StringIO()
        environ = {"REPO_AUTOMATION_HERMES_BIN": "hermes"}
        with (
            mock.patch.dict(run_next.os.environ, environ, clear=True),
            mock.patch.object(run_next, "parse_args", return_value=mock.Mock(dry_run=True)),
            mock.patch.object(run_next, "_run", return_value=0) as run,
            redirect_stderr(stderr),
        ):
            self.assertEqual(0, run_next.main())
        run.assert_called_once()
        self.assertIn(
            "warning: REPO_AUTOMATION_HERMES_BIN is deprecated and stops working in v0.5.0; use BOUNDROOM_HERMES_BIN.",
            stderr.getvalue(),
        )


class UpgradeGuideTests(unittest.TestCase):
    def test_the_upgrade_guide_names_each_legacy_name_and_its_replacement(self) -> None:
        text = UPGRADING.read_text(encoding="utf-8")
        rows = dict(re.findall(r"^\| `([A-Z0-9_]+)` \| `([A-Z0-9_]+)` \|", text, re.MULTILINE))
        every = {name: setting.env for setting in settings.SETTINGS if setting.env for name in setting.legacy_env}
        self.assertEqual(every, {name: rows.get(name) for name in every})


if __name__ == "__main__":
    unittest.main()
