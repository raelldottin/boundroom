"""The lane admission report: how many lanes a queue would run at once (SPEC-014 RA-LANE-003)."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "Tools/lane_admission_report.py"
_spec = importlib.util.spec_from_file_location("lane_admission_report", SCRIPT)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
# A dataclass looks its module up in sys.modules while the class is built.
sys.modules[_spec.name] = report
_spec.loader.exec_module(report)

EXAMPLE_QUEUE = REPO_ROOT / "automation/examples/example-slices.json"


def slice_record(slice_id: str, *paths: str, status: str = "queued", priority: int = 10, depends_on: tuple[str, ...] = ()):
    return {
        "slice_id": slice_id,
        "status": status,
        "priority": priority,
        "depends_on": list(depends_on),
        "allowed_paths": list(paths),
    }


def queue(*slices: dict[str, Any]) -> dict[str, Any]:
    return {"policy": {}, "slices": list(slices)}


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = report.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ReplayTest(unittest.TestCase):
    def test_a_dependency_chain_runs_one_lane_at_every_ceiling(self) -> None:
        chain = queue(
            slice_record("a", "src/a/"),
            slice_record("b", "src/b/", depends_on=("a",)),
            slice_record("c", "src/c/", depends_on=("b",)),
        )
        for ceiling in (1, 4, 8):
            with self.subTest(ceiling=ceiling):
                self.assertEqual(report.Replay(ceiling, (1, 1, 1), ()), report.replay(chain, ceiling))

    def test_disjoint_ready_slices_fill_the_ceiling_and_the_oldest_lane_finishes_first(self) -> None:
        disjoint = queue(slice_record("a", "src/a/"), slice_record("b", "src/b/"), slice_record("c", "src/c/"))
        expected = {1: (1, 1, 1), 2: (2, 2, 1), 3: (3, 2, 1), 8: (3, 2, 1)}
        for ceiling, lanes in expected.items():
            with self.subTest(ceiling=ceiling):
                self.assertEqual(report.Replay(ceiling, lanes, ()), report.replay(disjoint, ceiling))

    def test_a_held_slice_keeps_a_later_slice_from_jumping_it(self) -> None:
        held = queue(slice_record("a", "src/"), slice_record("b", "src/x/", "docs/"), slice_record("c", "docs/"))
        self.assertEqual(report.Replay(3, (1, 1, 1), ()), report.replay(held, 3))
        unheld = queue(slice_record("a", "src/"), slice_record("b", "src/x/"), slice_record("c", "docs/"))
        self.assertEqual(report.Replay(3, (2, 2, 1), ()), report.replay(unheld, 3))

    def test_done_slices_are_skipped_and_slices_that_never_become_eligible_are_named(self) -> None:
        mixed = queue(
            slice_record("old", "src/", status="done"),
            slice_record("next", "src/", depends_on=("old",)),
            slice_record("parked", "docs/", status="blocked"),
            slice_record("waiting", "web/", depends_on=("parked",)),
        )
        self.assertEqual(report.Replay(2, (1,), ("parked", "waiting")), report.replay(mixed, 2))

    def test_a_slice_marked_in_progress_is_a_lane_already_running(self) -> None:
        resumed = queue(
            slice_record("a", "src/", status="in_progress"),
            slice_record("b", "docs/", depends_on=("a",)),
            slice_record("c", "web/", depends_on=("a",)),
        )
        self.assertEqual(report.Replay(2, (1, 2, 1), ()), report.replay(resumed, 2))

    def test_replay_leaves_the_queue_unchanged(self) -> None:
        queue_data = queue(slice_record("a", "src/a/"), slice_record("b", "src/b/", depends_on=("a",)))
        before = copy.deepcopy(queue_data)
        report.replay(queue_data, 2)
        self.assertEqual(before, queue_data)


class MainTest(unittest.TestCase):
    def test_the_example_queue_report(self) -> None:
        code, out, err = run_main("--queue", str(EXAMPLE_QUEUE), "--max-ceiling", "3")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            "Queue: automation/examples/example-slices.json (3 slices: 1 done, 2 queued)\n"
            "\n"
            "| Ceiling | Steps | Most lanes at once | Lanes running at each step |\n"
            "| --- | --- | --- | --- |\n"
            "| 1 | 2 | 1 | 1 1 |\n"
            "| 2 | 2 | 1 | 1 1 |\n"
            "| 3 | 2 | 1 | 1 1 |\n"
            "\n"
            "Never admitted: none\n",
            out,
        )

    def test_the_default_reports_every_ceiling_the_schema_will_allow(self) -> None:
        code, out, _ = run_main()
        self.assertEqual(0, code)
        rows = [line for line in out.splitlines() if line.startswith("| ") and line[2].isdigit()]
        self.assertEqual([str(ceiling) for ceiling in range(1, 9)], [row.split(" | ")[0][2:] for row in rows])

    def test_a_queue_outside_the_repository_is_named_as_given_with_its_parallel_steps(self) -> None:
        queue_data = json.loads(EXAMPLE_QUEUE.read_text(encoding="utf-8"))
        first = queue_data["slices"][1]
        second = copy.deepcopy(first)
        second.update(slice_id="docs-only", depends_on=[], allowed_paths=["docs/other.md"])
        third = copy.deepcopy(first)
        third.update(slice_id="parked", status="deferred", entry_condition="after review", depends_on=[], allowed_paths=["x/"])
        queue_data["slices"] = [first | {"depends_on": []}, second, third]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slices.json"
            path.write_text(json.dumps(queue_data), encoding="utf-8")
            code, out, _ = run_main("--queue", str(path), "--max-ceiling", "2")
        self.assertEqual(0, code)
        self.assertIn(f"Queue: {path} (3 slices: 1 deferred, 2 queued)", out)
        self.assertIn("| 1 | 2 | 1 | 1 1 |\n| 2 | 2 | 2 | 2 1 |\n", out)
        self.assertIn("Never admitted: parked\n", out)

    def test_a_missing_or_invalid_queue_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.json"
            invalid.write_text("{not json", encoding="utf-8")
            unschematic = Path(directory) / "unschematic.json"
            unschematic.write_text(json.dumps({"slices": []}), encoding="utf-8")
            for path, message in (
                (Path(directory) / "missing.json", "queue file not found"),
                (invalid, "invalid JSON"),
                (unschematic, "Invalid queue file"),
                (Path(directory), "Is a directory"),
            ):
                with self.subTest(path=path.name):
                    code, out, err = run_main("--queue", str(path))
                    self.assertEqual((2, ""), (code, out))
                    self.assertIn(message, err)

    def test_a_ceiling_outside_the_schema_range_is_refused(self) -> None:
        for ceiling in ("0", "9"):
            with self.subTest(ceiling=ceiling), self.assertRaises(SystemExit) as raised:
                run_main("--max-ceiling", ceiling)
            self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
