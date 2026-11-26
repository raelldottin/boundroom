"""Output-based tests for automation.benchmark.scoring.

Each test feeds eval data (or eval files on disk) in and compares the returned score,
report dict, summary text or written report file with a literal expected value.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from automation.benchmark import scoring
from automation.benchmark.scoring import EffectivenessReport, InstanceScore
from automation.tests.benchmark_fixtures import (
    DEFAULT_THRESHOLDS,
    ROUNDING_SUMMARY,
    THRESHOLD_SKIP_REASON,
    ev,
    rounding_fixture,
)


def report_of(fixture: dict[str, dict[str, Any]]) -> EffectivenessReport:
    return EffectivenessReport(tuple(scoring.score_eval_data(iid, data) for iid, data in fixture.items()))


def instance_dict(
    iid: str,
    total: int,
    passed: int,
    pass_fraction: float,
    macro: float,
    branch_count: int,
    unique: int,
    resolved: bool,
    near: bool,
    error_code: Optional[str],
) -> dict[str, Any]:
    return {
        "instance_id": iid,
        "total_tests": total,
        "passed_tests": passed,
        "pass_fraction": pass_fraction,
        "branch_macro_pass_fraction": macro,
        "branch_count": branch_count,
        "unique_tests": unique,
        "resolved": resolved,
        "near_resolved": near,
        "error_code": error_code,
    }


A_DICT = instance_dict("a", 3, 3, 1.0, 1.0, 1, 3, True, True, None)
B_DICT = instance_dict("b", 20, 19, 0.95, 0.95, 1, 20, False, True, None)
C_DICT = instance_dict("c", 7, 2, 0.2857, 0.3333, 2, 4, False, False, "compile_failed")

ROUNDING_DICT: dict[str, Any] = {
    "instance_count": 3,
    "resolved_count": 1,
    "near_resolved_count": 2,
    "resolve_rate": 0.3333,
    "near_resolve_rate": 0.6667,
    "mean_pass_fraction": 0.7452,
    "mean_branch_macro_pass_fraction": 0.7611,
    "executions": 30,
    "unique_tests": 27,
    "error_counts": {"compile_failed": 1},
    "instances": [A_DICT, B_DICT, C_DICT],
}

EMPTY_DICT: dict[str, Any] = {
    "instance_count": 0,
    "resolved_count": 0,
    "near_resolved_count": 0,
    "resolve_rate": 0.0,
    "near_resolve_rate": 0.0,
    "mean_pass_fraction": 0.0,
    "mean_branch_macro_pass_fraction": 0.0,
    "executions": 0,
    "unique_tests": 0,
    "error_counts": {},
    "instances": [],
}

RUN_DIR_DICT: dict[str, Any] = {
    "instance_count": 2,
    "resolved_count": 1,
    "near_resolved_count": 2,
    "resolve_rate": 0.5,
    "near_resolve_rate": 1.0,
    "mean_pass_fraction": 0.975,
    "mean_branch_macro_pass_fraction": 0.975,
    "executions": 23,
    "unique_tests": 23,
    "error_counts": {},
    "instances": [A_DICT, B_DICT],
}

ERROR_CODES = [("e1", "b"), ("e2", "a"), ("e3", "b"), ("e4", ""), ("e5", None)]

HEADER = "  instance                                            pass  resolved  error"


ERROR_COUNTS_SUMMARY = [
    "Harness Effectiveness (ProgramBench)",
    "  instances        : 5",
    "  resolved         : 0 (0.0%)",
    "  near-resolved    : 0 (0.0%)",
    "  mean pass frac.  : 50.0%",
    "  branch-macro     : 50.0%",
    "  executions       : 10 over 10 unique tests",
    "  errors           : a=1, b=2",
    "",
    HEADER,
    "  e1                                                50.0%        no  b",
    "  e2                                                50.0%        no  a",
    "  e3                                                50.0%        no  b",
    "  e4                                                50.0%        no  ",
    "  e5                                                50.0%        no  ",
]

EMPTY_SUMMARY = [
    "Harness Effectiveness (ProgramBench)",
    "  instances        : 0",
    "  resolved         : 0 (0.0%)",
    "  near-resolved    : 0 (0.0%)",
    "  mean pass frac.  : 0.0%",
    "  branch-macro     : 0.0%",
    "  executions       : 0 over 0 unique tests",
    "",
    HEADER,
]

RUN_DIR_SUMMARY = [
    "Harness Effectiveness (ProgramBench)",
    "  instances        : 2",
    "  resolved         : 1 (50.0%)",
    "  near-resolved    : 2 (100.0%)",
    "  mean pass frac.  : 97.5%",
    "  branch-macro     : 97.5%",
    "  executions       : 23 over 23 unique tests",
    "",
    HEADER,
    "  a                                                100.0%       yes  ",
    "  b                                                 95.0%      near  ",
]


def outcome(score: InstanceScore) -> tuple[int, int, float, float, bool, bool]:
    return (
        score.total_tests,
        score.passed_tests,
        score.pass_fraction,
        score.branch_macro_pass_fraction,
        score.resolved,
        score.near_resolved,
    )


class ThresholdTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("programbench") is not None, "thresholds come from programbench")
    def test_fallback_thresholds_are_programbench_defaults(self) -> None:
        self.assertEqual((scoring.RESOLVED_THRESHOLD, scoring.NEAR_RESOLVED_THRESHOLD), (1.0, 0.95))


@unittest.skipUnless(DEFAULT_THRESHOLDS, THRESHOLD_SKIP_REASON)
class InstanceScoreTests(unittest.TestCase):
    def test_classification_at_the_thresholds(self) -> None:
        # (label, data, (total, passed, pass_fraction, macro, resolved, near_resolved))
        rows = [
            ("tie at resolved", ev(20, 20), (20, 20, 1.0, 1.0, True, True)),
            ("tie at near", ev(19, 20), (20, 19, 0.95, 0.95, False, True)),
            ("just below near", ev(1899, 2000), (2000, 1899, 0.9495, 0.9495, False, False)),
            ("one passing of twenty", ev(1, 20), (20, 1, 0.05, 0.05, False, False)),
        ]
        for label, data, expected in rows:
            with self.subTest(label):
                self.assertEqual(outcome(scoring.score_eval_data("i", data)), expected)

    def test_zero_totals_score_nothing(self) -> None:
        rows = [
            ("empty list", {"test_results": []}),
            ("null", {"test_results": None}),
            ("missing key", {}),
        ]
        for label, data in rows:
            with self.subTest(label):
                score = scoring.score_eval_data("i", data)
                self.assertEqual(outcome(score), (0, 0, 0.0, 0.0, False, False))
                self.assertEqual(score.unique_tests, 0)
                self.assertEqual(score.branch_pass_fractions, ())

    def test_counts_passed_results_and_unique_names(self) -> None:
        data = {
            "test_results": [
                {"name": "t1", "status": "passed"},
                {"name": "t1", "status": "passed"},
                {"name": "t2", "status": "failed"},
                {"status": "failed"},
                {"name": "t3"},
            ]
        }
        score = scoring.score_eval_data("i", data)
        self.assertEqual(
            (score.total_tests, score.passed_tests, score.unique_tests, score.branch_pass_fractions),
            (5, 2, 4, (0.4,)),
        )

    def test_branch_grouping(self) -> None:
        disagree = ev(2, 3, branch="x")
        disagree["test_results"] += ev(0, 4, branch="y")["test_results"]
        interleaved = {
            "test_results": [
                {"name": "a", "status": "passed", "branch": "x"},
                {"name": "a", "status": "failed", "branch": "y"},
                {"name": "b", "status": "passed", "branch": "x"},
                {"name": "b", "status": "passed", "branch": "y"},
            ]
        }
        # (label, data, branch_pass_fractions, pass_fraction, branch macro)
        rows = [
            ("branches disagree", disagree, (2 / 3, 0.0), 2 / 7, 1 / 3),
            ("no branch", ev(3, 4), (0.75,), 0.75, 0.75),
            ("interleaved branches", interleaved, (1.0, 0.5), 0.75, 0.75),
        ]
        for label, data, fractions, pooled, macro in rows:
            with self.subTest(label):
                score = scoring.score_eval_data("i", data)
                self.assertEqual(score.branch_pass_fractions, fractions)
                self.assertAlmostEqual(score.pass_fraction, pooled, places=12)
                self.assertAlmostEqual(score.branch_macro_pass_fraction, macro, places=12)

    def test_error_code_is_carried(self) -> None:
        rows = [("missing", ev(1, 1), None), ("oom", ev(1, 1, "oom"), "oom"), ("empty", ev(1, 1, ""), "")]
        for label, data, expected in rows:
            with self.subTest(label):
                self.assertEqual(scoring.score_eval_data("i", data).error_code, expected)

    def test_score_keeps_its_instance_id(self) -> None:
        self.assertEqual(scoring.score_eval_data("abc__def", ev(1, 1)).instance_id, "abc__def")

    def test_macro_falls_back_to_pooled_without_branch_fractions(self) -> None:
        rows = [
            ("three of four", InstanceScore("i", total_tests=4, passed_tests=3), 0.75),
            ("none", InstanceScore("i", total_tests=0, passed_tests=0), 0.0),
            ("with fractions", InstanceScore("i", 4, 3, branch_pass_fractions=(1.0, 0.0)), 0.5),
        ]
        for label, score, expected in rows:
            with self.subTest(label):
                self.assertEqual(score.branch_macro_pass_fraction, expected)

    def test_defaults_report_no_branches_no_unique_tests_and_no_error(self) -> None:
        self.assertEqual(
            InstanceScore("i", total_tests=0, passed_tests=0).to_dict(),
            {
                "instance_id": "i",
                "total_tests": 0,
                "passed_tests": 0,
                "pass_fraction": 0.0,
                "branch_macro_pass_fraction": 0.0,
                "branch_count": 0,
                "unique_tests": 0,
                "resolved": False,
                "near_resolved": False,
                "error_code": None,
            },
        )

    def test_is_an_immutable_value(self) -> None:
        score = InstanceScore("i", total_tests=4, passed_tests=3)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            score.passed_tests = 4  # ty: ignore[invalid-assignment]
        self.assertEqual({score, InstanceScore("i", total_tests=4, passed_tests=3)}, {score})

    def test_to_dict(self) -> None:
        scores = report_of(rounding_fixture()).instances
        for score, expected in zip(scores, [A_DICT, B_DICT, C_DICT]):
            with self.subTest(expected["instance_id"]):
                self.assertEqual(score.to_dict(), expected)


@unittest.skipUnless(DEFAULT_THRESHOLDS, THRESHOLD_SKIP_REASON)
class EffectivenessReportTests(unittest.TestCase):
    def test_to_dict(self) -> None:
        rows = [
            ("rounding to 4 places", report_of(rounding_fixture()), ROUNDING_DICT),
            ("empty report", EffectivenessReport(()), EMPTY_DICT),
        ]
        for label, report, expected in rows:
            with self.subTest(label):
                self.assertEqual(report.to_dict(), expected)

    def test_is_an_immutable_value(self) -> None:
        report = EffectivenessReport((InstanceScore("i", total_tests=4, passed_tests=3),))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.instances = ()  # ty: ignore[invalid-assignment]
        self.assertEqual({report, EffectivenessReport((InstanceScore("i", total_tests=4, passed_tests=3),))}, {report})

    def test_error_counts_skip_empty_and_missing_codes(self) -> None:
        report = EffectivenessReport(tuple(scoring.score_eval_data(i, ev(1, 2, c)) for i, c in ERROR_CODES))
        self.assertEqual(report.error_counts, {"b": 2, "a": 1})
        self.assertEqual(report.to_dict()["error_counts"], {"b": 2, "a": 1})

    def test_summary_table(self) -> None:
        error_report = EffectivenessReport(tuple(scoring.score_eval_data(i, ev(1, 2, c)) for i, c in ERROR_CODES))
        run_dir_report = report_of({"a": rounding_fixture()["a"], "b": rounding_fixture()["b"]})
        rows = [
            ("rounding fixture, one error", report_of(rounding_fixture()), ROUNDING_SUMMARY),
            ("errors sorted by code", error_report, ERROR_COUNTS_SUMMARY),
            ("no errors line", run_dir_report, RUN_DIR_SUMMARY),
            ("empty report", EffectivenessReport(()), EMPTY_SUMMARY),
        ]
        for label, report, expected in rows:
            with self.subTest(label):
                self.assertEqual(report.summary_table().split("\n"), expected)


@unittest.skipUnless(DEFAULT_THRESHOLDS, THRESHOLD_SKIP_REASON)
class EvalFileTests(unittest.TestCase):
    def test_score_eval_file_instance_id(self) -> None:
        rows = [
            ("eval suffix", "foo.eval.json", "foo"),
            ("plain json falls back to stem", "foo.json", "foo"),
            ("dotted id keeps its dots", "org.repo.eval.json", "org.repo"),
            ("eval only in stem", "foo.eval", "foo"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for label, name, expected in rows:
                with self.subTest(label):
                    path = Path(tmp) / name
                    path.write_text(json.dumps(ev(1, 2, "oom")), encoding="utf-8")
                    score = scoring.score_eval_file(path)
                    self.assertEqual(score.instance_id, expected)
                    self.assertEqual((score.total_tests, score.passed_tests, score.error_code), (2, 1, "oom"))

    def test_score_run_dir_scores_matching_files_in_name_order(self) -> None:
        fixture = rounding_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            for iid in ("b", "a"):
                (rd / iid).mkdir()
                (rd / iid / f"{iid}.eval.json").write_text(json.dumps(fixture[iid]), encoding="utf-8")
            (rd / "noeval").mkdir()
            (rd / "stray.eval.json").write_text(json.dumps(ev(1, 1)), encoding="utf-8")
            (rd / "mismatch").mkdir()
            (rd / "mismatch" / "other.eval.json").write_text(json.dumps(ev(1, 1)), encoding="utf-8")
            self.assertEqual(scoring.score_run_dir(rd).to_dict(), RUN_DIR_DICT)

    def test_score_run_dir_orders_many_instances(self) -> None:
        names = ["m", "c", "z", "a", "q", "b", "y", "k"]
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            for iid in names:
                (rd / iid).mkdir()
                (rd / iid / f"{iid}.eval.json").write_text(json.dumps(ev(1, 1)), encoding="utf-8")
            ids = [score.instance_id for score in scoring.score_run_dir(rd).instances]
        self.assertEqual(ids, sorted(names))

    def test_score_run_dir_without_instance_dirs_is_empty(self) -> None:
        rows = [("no entries", []), ("only files", ["a.eval.json", "notes.txt"])]
        for label, files in rows:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                for name in files:
                    (Path(tmp) / name).write_text(json.dumps(ev(1, 1)), encoding="utf-8")
                self.assertEqual(scoring.score_run_dir(Path(tmp)).to_dict(), EMPTY_DICT)

    def test_write_report_text(self) -> None:
        rows = [
            ("rounding fixture", report_of(rounding_fixture()), ROUNDING_DICT),
            ("empty report", EffectivenessReport(()), EMPTY_DICT),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for label, report, expected in rows:
                with self.subTest(label):
                    path = Path(tmp) / "report.json"
                    scoring.write_report(report, path)
                    text = path.read_text(encoding="utf-8")
                    self.assertEqual(text, json.dumps(expected, indent=2) + "\n")
                    self.assertTrue(text.startswith('{\n  "instance_count": '))


if __name__ == "__main__":
    unittest.main()
