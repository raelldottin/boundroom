"""The README's support matrix is what CI and the release gate run (SPEC-009 RA-ENT-008)."""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github/workflows"
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
# Required checks are matched by name, so these two must stay first and unrenamed.
REQUIRED = ["3.11", "3.12"]


def job(workflow: str, name: str) -> str:
    """One job's text, from its header to the next job's header."""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    after = text.split(f"\n  {name}:\n", 1)[1]
    following = re.search(r"^  [a-z][a-z0-9_-]*:\s*$", after, re.MULTILINE)
    return after[: following.start()] if following else after


def matrix(workflow: str, name: str) -> list[str]:
    after = job(workflow, name).split("python-version:", 1)[1]
    versions: list[str] = []
    for line in after.splitlines()[1:]:
        item = re.match(r'^\s+- "(\d+\.\d+)"$', line)
        if not item:
            break
        versions.append(item.group(1))
    return versions


def readme_row(component: str) -> list[str]:
    for line in README.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == component:
            return cells
    raise AssertionError(f"the README's support matrix has no {component} row")


class SupportMatrixTests(unittest.TestCase):
    def test_ci_and_the_release_gate_run_the_same_pythons(self) -> None:
        self.assertEqual(matrix("lint.yml", "lint"), matrix("release.yml", "gate"))

    def test_the_readme_names_exactly_the_pythons_ci_runs(self) -> None:
        tested = [version.strip() for version in readme_row("Python")[1].split(",")]
        self.assertEqual(matrix("lint.yml", "lint"), tested)

    def test_the_required_checks_keep_their_names(self) -> None:
        self.assertEqual(REQUIRED, matrix("lint.yml", "lint")[:2])

    def test_the_oldest_tested_python_is_the_oldest_allowed(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(f">={matrix('lint.yml', 'lint')[0]}", project["project"]["requires-python"])

    def test_macos_runs_in_ci_and_before_a_release(self) -> None:
        for workflow, name in (("lint.yml", "macos"), ("release.yml", "gate-macos")):
            with self.subTest(workflow=workflow):
                text = job(workflow, name)
                self.assertIn("name: make reusable-check (macOS)", text)
                self.assertIn("runs-on: macos-latest", text)
                self.assertIn("run: make reusable-check", text)
        self.assertIn("macOS", readme_row("Operating system")[1])

    def test_a_release_waits_for_every_gate(self) -> None:
        self.assertIn("needs: [gate, gate-macos]", (WORKFLOWS / "release.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
