"""The reject demo (`make demo-reject`) still shows the supervisor rejecting an out-of-scope diff.

The demo's scenario is fixed, so this proves on every commit that evaluate_handoff.py rejects a
handoff that claims one file when the diff changes three, one of them outside allowed_paths.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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
        # The demo must not claim the supervisor compares the handoff's claims with the diff.
        self.assertIn("doesn't compare files_touched", text)

    def test_command_exits_zero_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "TMPDIR": scratch}
            completed = subprocess.run(
                [sys.executable, str(DEMO)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("decision           reject", completed.stdout)
            self.assertEqual(list(Path(scratch).iterdir()), [], "the demo left its temporary directory behind")


def _copy_supervisor_source(destination: Path) -> Path:
    """A checkout-shaped copy of this repository's supervisor, for a test to make incompatible."""
    for relative in demo_reject.SUPERVISOR_DIRS:
        shutil.copytree(REPO_ROOT / relative, destination / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = demo_reject.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class DemoFailureTests(unittest.TestCase):
    """Exit 1 means the supervisor decided something else; every failure of the demo itself is 2."""

    def test_an_unexpected_exception_is_a_demo_failure_with_its_traceback(self) -> None:
        for target in ("run_demo", "render"):
            with self.subTest(failing=target), tempfile.TemporaryDirectory() as scratch:
                with (
                    unittest.mock.patch.dict(os.environ, {"TMPDIR": scratch}),
                    unittest.mock.patch.object(tempfile, "tempdir", None),
                    unittest.mock.patch.object(demo_reject, target, side_effect=ValueError("boom")),
                ):
                    code, stdout, stderr = _run_main([])
                self.assertEqual(2, code)
                self.assertIn("Traceback", stderr)
                self.assertIn("ValueError: boom", stderr)
                self.assertNotIn("UNEXPECTED", stdout)
                self.assertEqual([], list(Path(scratch).iterdir()), "the demo left its temporary directory behind")


class SupervisorContractTests(unittest.TestCase):
    """A consumer's copy of the demo is a template and stays put while its synced supervisor moves.

    The demo checks the supervisor it is about to run, and says it is out of date instead of
    reporting the supervisor's answer to a question that supervisor no longer accepts.
    """

    def _assert_stale(self, source_root: Path) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(demo_reject.StaleDemoError) as raised:
                demo_reject.run_demo(Path(workdir), source_root)
        self.assertIn(demo_reject.STALE_DEMO_MESSAGE, str(raised.exception))
        with unittest.mock.patch.object(demo_reject, "SOURCE_ROOT", source_root):
            code, stdout, stderr = _run_main([])
        self.assertEqual(2, code)
        self.assertIn("this demo is older than your synced supervisor; copy the current one from the release", stderr)
        self.assertNotIn("UNEXPECTED", stdout)

    def test_a_handoff_schema_the_demo_no_longer_satisfies_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = _copy_supervisor_source(Path(tmp) / "src")
            schema_path = source / "automation/schemas/handoff.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["required"].append("attestation")
            schema["properties"]["attestation"] = {"type": "string", "minLength": 1}
            schema["properties"]["status"]["enum"] = ["completed", "blocked", "failed"]
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            self._assert_stale(source)

    def test_a_queue_schema_the_demo_no_longer_satisfies_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = _copy_supervisor_source(Path(tmp) / "src")
            schema_path = source / "automation/schemas/slice.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            del schema["$defs"]["SliceRecord"]["properties"]["required_proof_level"]
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            self._assert_stale(source)

    def test_an_evaluate_handoff_without_the_flags_the_demo_passes_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = _copy_supervisor_source(Path(tmp) / "src")
            (source / demo_reject.EVALUATE_HANDOFF).write_text(
                "import sys\nprint('usage: evaluate_handoff.py --slice-id ID --handoff PATH --base-sha SHA --head-sha SHA')\n",
                encoding="utf-8",
            )
            self._assert_stale(source)

    def test_this_checkouts_supervisor_matches_the_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            demo_reject.copy_supervisor(REPO_ROOT, repo)
            self.assertEqual([], demo_reject.supervisor_contract_problems(repo))


if __name__ == "__main__":
    unittest.main()
