"""The reject demo (`make demo-reject`) still shows the supervisor rejecting an out-of-scope diff.

The demo's scenario is fixed, so this proves on every commit that evaluate_handoff.py rejects a
handoff that claims one file when the diff changes three, one of them outside allowed_paths.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from automation.examples import demo_reject
from automation.schemas.models import Handoff, SliceQueue
from automation.supervisor import policy

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "automation/examples/demo_reject.py"


class DemoRejectTests(unittest.TestCase):
    result: demo_reject.DemoResult

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            cls.result = demo_reject.run_demo(Path(workdir))

    def test_supervisor_rejects_the_out_of_scope_file(self) -> None:
        report = self.result.report
        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["reason"], "Historical diff contains files outside allowed_paths.")
        self.assertEqual(report["out_of_scope_files"], ["README.md"])
        self.assertEqual(report["changed_file_count"], 3)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.result.supervisor_exit, 1)
        self.assertTrue(demo_reject.rejected_as_expected(self.result))

    def test_handoff_understates_the_diff(self) -> None:
        self.assertEqual(self.result.handoff["files_touched"], ["docs/workflows/guide.md"])
        self.assertEqual(sorted(self.result.diff_files), ["README.md", "docs/workflows/faq.md", "docs/workflows/guide.md"])

    def test_inputs_are_valid_against_the_schemas(self) -> None:
        # A schema change must not turn the demo into a validation failure instead of a scope reject.
        handoff = policy.validate_document(self.result.handoff, Handoff)
        self.assertTrue(handoff.is_valid, handoff.errors)
        queue = policy.validate_document(demo_reject.QUEUE_DATA, SliceQueue)
        self.assertTrue(queue.is_valid, queue.errors)

    def test_render_shows_claim_diff_and_decision(self) -> None:
        text = demo_reject.render(self.result)
        self.assertIn("README.md  <- outside allowed_paths", text)
        self.assertIn("decision           reject", text)
        self.assertIn("exit code          1", text)
        self.assertIn("Rejected as expected", text)

    def test_command_exits_zero_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "TMPDIR": scratch}
            completed = subprocess.run(
                [sys.executable, str(DEMO)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("decision           reject", completed.stdout)
            self.assertEqual(list(Path(scratch).iterdir()), [], "the demo left its temporary directory behind")


if __name__ == "__main__":
    unittest.main()
