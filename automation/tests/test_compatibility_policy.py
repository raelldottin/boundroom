"""The compatibility policy stays linked and matches the deprecations the code declares (SPEC-009 RA-ENT-007)."""

from __future__ import annotations

import ast
import re
import tomllib
import unittest
from pathlib import Path

from automation.supervisor import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "docs/reference/compatibility.md"
SYNC = REPO_ROOT / "Tools/boundroom-sync.sh"
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(text)
    assert match, text
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def current_version() -> tuple[int, int, int]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return parse_version(project["project"]["version"])


def sync_deprecated_keys() -> dict[str, str]:
    """DEPRECATED_ENTRY_KEYS from the sync script, read without running it."""
    match = re.search(r"^DEPRECATED_ENTRY_KEYS = (\{.*\})$", SYNC.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "the sync script no longer declares DEPRECATED_ENTRY_KEYS on one line"
    return ast.literal_eval(match.group(1))


def deprecation_rows() -> dict[str, str]:
    """The policy's current-deprecations table: first cell to the "Stops working" cell."""
    text = POLICY.read_text(encoding="utf-8")
    section = text.split("## Current deprecations", 1)[1]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] not in ("What", "---"):
            rows[cells[0]] = cells[3]
    return rows


def row_for(fragment: str) -> str:
    matches = [stops for what, stops in deprecation_rows().items() if fragment in what]
    assert len(matches) == 1, f"expected one deprecation row naming {fragment!r}, found {len(matches)}"
    return matches[0]


class PolicyLinkTests(unittest.TestCase):
    def test_the_readme_and_the_upgrade_guide_link_the_policy(self) -> None:
        for page, link in (
            ("README.md", "docs/reference/compatibility.md"),
            ("docs/workflows/upgrading.md", "../reference/compatibility.md"),
        ):
            with self.subTest(page=page):
                self.assertIn(f"]({link}", (REPO_ROOT / page).read_text(encoding="utf-8"))


class DeprecationTableTests(unittest.TestCase):
    def test_legacy_variables_stop_when_the_registry_says(self) -> None:
        self.assertEqual(settings.LEGACY_ENV_REMOVAL, row_for("`REPO_AUTOMATION_*` variables an operator sets"))
        self.assertEqual(settings.LEGACY_ENV_REMOVAL, row_for("`OWLORY_CODEX_BIN`"))
        self.assertEqual(settings.LEGACY_ENV_REMOVAL, row_for("`OWLORY_SUPERVISOR_*`"))

    def test_each_deprecated_manifest_key_stops_when_the_sync_says(self) -> None:
        keys = sync_deprecated_keys()
        self.assertTrue(keys)
        for key, removal in keys.items():
            with self.subTest(key=key):
                self.assertEqual(removal, row_for(f"`{key}`"))

    def test_no_removal_lands_in_the_current_minor_line(self) -> None:
        """Step 3: removal waits for a later minor release than the one that warns."""
        major, minor, _ = current_version()
        for what, stops in deprecation_rows().items():
            if stops == "Not decided":
                continue
            with self.subTest(what=what):
                removal = parse_version(stops)
                self.assertGreater((removal[0], removal[1]), (major, minor))
                self.assertEqual(0, removal[2], "a removal happens only in a minor or major release")


if __name__ == "__main__":
    unittest.main()
