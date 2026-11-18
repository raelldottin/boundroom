"""CodeQL analyzes the Python code and the workflows, and reports without blocking (SPEC-009 RA-ENT-005)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from automation.tests.test_contributing import required_checks

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEQL = REPO_ROOT / ".github/workflows/codeql.yml"


class CodeqlWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = CODEQL.read_text(encoding="utf-8")

    def test_it_runs_on_pull_requests_main_and_weekly(self) -> None:
        triggers = self.text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        for trigger in ("pull_request:", "push:", "schedule:"):
            self.assertIn(trigger, triggers)
        self.assertIn("- main", triggers)

    def test_it_analyzes_python_and_the_workflows(self) -> None:
        self.assertIn("language: [python, actions]", self.text)
        self.assertIn("build-mode: none", self.text)
        self.assertIn("category: /language:${{ matrix.language }}", self.text)

    def test_init_and_analyze_are_the_same_pinned_release(self) -> None:
        pins = set(re.findall(r"uses: github/codeql-action/(?:init|analyze)@([0-9a-f]{40}) # (v\S+)", self.text))
        self.assertEqual(1, len(pins), pins)

    def test_only_the_analyze_job_can_write_security_events(self) -> None:
        top = self.text.split("\njobs:\n", 1)[0]
        self.assertIn("permissions:\n  contents: read\n", top)
        self.assertNotIn("security-events", top)
        self.assertEqual(1, self.text.count("security-events: write"))
        self.assertNotIn("contents: write", self.text)

    def test_it_is_not_a_required_check(self) -> None:
        """Making it one is the owner's decision; AGENTS.md lists the required checks."""
        self.assertFalse([name for name in required_checks() if name.startswith("CodeQL")])


if __name__ == "__main__":
    unittest.main()
