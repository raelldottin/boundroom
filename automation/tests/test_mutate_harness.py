"""The mutation harness measures what it claims (Tools/mutate, docs/reference/mutation-testing.md).

A toy project with known logic and a deliberately weak test suite is the fixture. Every
mutant's outcome is worked out by hand below, so a harness that reports anything else, such
as a survivor it should have killed or a kill it can't have seen, fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from Tools.mutate import apply_mutant, generate_mutants
from Tools.mutate.report import baseline_failures

REPO_ROOT = Path(__file__).resolve().parents[2]

TOY_LOGIC = '''\
"""Toy module with known logic for the mutation harness's own tests."""


def is_adult(age):
    # The weak suite never checks 18 itself.
    return age >= 18


def floor_at_zero(n):
    if n < 0:  # `n <= 0` returns the same: an equivalent mutant
        return 0
    return n


def countdown(n):
    while n > 0:
        n -= 1
    return n


def untested(flag):
    return not flag
'''

TOY_CLI = '''\
"""Toy script that its test runs as a subprocess and never imports."""

import sys

if __name__ == "__main__":
    sys.exit(3)
'''

TOY_TESTS = '''\
import os
import subprocess
import sys
import uuid
from pathlib import Path

from logic import countdown, floor_at_zero, is_adult

ROOT = Path(__file__).resolve().parents[1]


def record_processes():
    """Leave a sleeping child behind, and write both pids down, so the caller can see they die."""
    directory = os.environ.get("TOY_PID_DIR")
    if not directory:
        return
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(directory, uuid.uuid4().hex).write_text(f"{os.getpid()} {sleeper.pid}")


def test_is_adult():
    assert is_adult(30) is True
    assert is_adult(5) is False


def test_floor_at_zero():
    assert floor_at_zero(-3) == 0
    assert floor_at_zero(4) == 4
    assert floor_at_zero(0.5) == 0.5


def test_countdown():
    record_processes()
    assert countdown(3) == 0


def test_cli_exit_code():
    assert subprocess.run([sys.executable, str(ROOT / "cli.py")]).returncode == 3
'''

TOY_PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
"""

# Every mutant of the toy, worked out by hand: (module, line, operator, mutated text) -> status.
EXPECTED = {
    ("logic.py", 6, "delete_statement", "pass"): "killed",
    ("logic.py", 6, "return_none", "return None"): "killed",
    ("logic.py", 6, "comparison", "age > 18"): "survived",  # 18 itself is never tested
    ("logic.py", 6, "number_increment", "19"): "survived",
    ("logic.py", 6, "number_zero", "0"): "killed",
    ("logic.py", 10, "comparison", "n <= 0"): "allowlisted",  # equivalent, and allowlisted exactly
    ("logic.py", 10, "number_increment", "1"): "killed",  # floor_at_zero(0.5)
    ("logic.py", 11, "delete_statement", "pass"): "killed",
    ("logic.py", 11, "return_none", "return None"): "killed",
    ("logic.py", 11, "number_increment", "1"): "killed",
    ("logic.py", 12, "delete_statement", "pass"): "killed",
    ("logic.py", 12, "return_none", "return None"): "killed",
    ("logic.py", 16, "comparison", "n >= 0"): "killed",
    ("logic.py", 16, "number_increment", "1"): "killed",
    ("logic.py", 17, "number_increment", "2"): "killed",
    ("logic.py", 17, "number_zero", "0"): "timeout",  # `n -= 0` never ends
    ("logic.py", 18, "delete_statement", "pass"): "killed",
    ("logic.py", 18, "return_none", "return None"): "killed",
    ("logic.py", 22, "delete_statement", "pass"): "no_coverage",
    ("logic.py", 22, "return_none", "return None"): "no_coverage",
    ("logic.py", 22, "remove_not", "flag"): "no_coverage",
    ("cli.py", 5, "comparison", '__name__ != "__main__"'): "killed",  # only ever run as a script
    ("cli.py", 6, "number_increment", "4"): "killed",
    ("cli.py", 6, "number_zero", "0"): "killed",
}

EXACT_ENTRY = {
    "module": "logic.py",
    "line": 10,
    "operator": "comparison",
    "original": "n < 0",
    "mutant": "n <= 0",
    "reason": "At n == 0 both branches return 0.",
    "reviewed_by": "toy-reviewer",
}
# Near misses: right mutant on the wrong line, and the right line with other mutated text.
WRONG_TEXT = {**EXACT_ENTRY, "line": 6, "original": "age >= 18", "mutant": "age > 17", "reason": "near miss"}
WRONG_LINE = {**EXACT_ENTRY, "line": 7, "original": "age >= 18", "mutant": "age > 18", "reason": "wrong line"}

MIN_TIMEOUT = 2.0


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_harness(root: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COVERAGE_", "COV_CORE_"))}
    return subprocess.run(
        [sys.executable, "-m", "Tools.mutate", "--root", str(root), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def mutant_keys(source: str) -> list[tuple[int, str, str, str]]:
    return sorted((m.line, m.operator, m.original, m.mutant) for m in generate_mutants(source))


class OperatorTests(unittest.TestCase):
    def test_a_small_function_yields_exactly_the_expected_mutants(self) -> None:
        source = textwrap.dedent('''\
            """Docstring 1 < 2 is not code."""
            from typing import TYPE_CHECKING, Literal

            __all__ = ("pick",) if True else ()

            if TYPE_CHECKING:
                LIMIT = 5


            def pick(items, limit: Literal[3] = 3) -> Literal[True]:
                """Pick 2 of them."""
                for item in items:
                    if item is None or item in limit:
                        continue
                    if not item:
                        break
                    _fail("bad", item)
                logger.info("done %d", 7)
                print(1 > 0)
                return swap(items, limit)
            ''')
        expected = [
            (10, "number_increment", "3", "4"),
            (10, "number_zero", "3", "0"),
            (13, "boolean_operator", "item is None or item in limit", "item is None and item in limit"),
            (13, "comparison", "item in limit", "item not in limit"),
            (13, "comparison", "item is None", "item is not None"),
            (14, "loop_control", "continue", "pass"),
            (15, "remove_not", "not item", "item"),
            (16, "loop_control", "break", "pass"),
            (17, "delete_statement", '_fail("bad", item)', "pass"),
            (20, "delete_statement", "return swap(items, limit)", "pass"),
            (20, "return_none", "return swap(items, limit)", "return None"),
            (20, "swap_arguments", "swap(items, limit)", "swap(limit, items)"),
        ]
        self.assertEqual(sorted(expected), mutant_keys(source))

    def test_each_operator_goes_both_ways_where_it_has_a_pair(self) -> None:
        table = {
            "x = a < b": [(1, "comparison", "a < b", "a <= b")],
            "x = a <= b": [(1, "comparison", "a <= b", "a < b")],
            "x = a > b": [(1, "comparison", "a > b", "a >= b")],
            "x = a >= b": [(1, "comparison", "a >= b", "a > b")],
            "x = a == b": [(1, "comparison", "a == b", "a != b")],
            "x = a != b": [(1, "comparison", "a != b", "a == b")],
            "x = a in b": [(1, "comparison", "a in b", "a not in b")],
            "x = a not in b": [(1, "comparison", "a not in b", "a in b")],
            "x = a is b": [(1, "comparison", "a is b", "a is not b")],
            "x = a is not b": [(1, "comparison", "a is not b", "a is b")],
            "x = a and b": [(1, "boolean_operator", "a and b", "a or b")],
            "x = a or b or c": [(1, "boolean_operator", "a or b or c", "a and b and c")],
            "x = not a": [(1, "remove_not", "not a", "a")],
            "x = True": [(1, "boolean_constant", "True", "False")],
            "x = False": [(1, "boolean_constant", "False", "True")],
            "x = 0": [(1, "number_increment", "0", "1")],
            "x = 7": [(1, "number_increment", "7", "8"), (1, "number_zero", "7", "0")],
            "x = 1000": [],
            "x = 2.5": [],
            "raise E": [(1, "delete_statement", "raise E", "pass")],
            "fail_now()": [(1, "delete_statement", "fail_now()", "pass")],
            "check()": [],
            "f(a, b)": [(1, "swap_arguments", "f(a, b)", "f(b, a)")],
            "f(a, a)": [],
            "f(a, 1)": [(1, "number_increment", "1", "2"), (1, "number_zero", "1", "0")],
            "log.debug('%d', a < b)": [],
            "x: Literal[1] = y": [],
            "x = f'{a < b}'": [],
            "log.append(a < b)": [(1, "comparison", "a < b", "a <= b")],  # a list called log isn't logging
            "x = not not a": [(1, "remove_not", "not not a", "not a")],  # either `not` gives the same program
        }
        for source, expected in table.items():
            with self.subTest(source=source):
                self.assertEqual(sorted(expected), mutant_keys(source + "\n"))

    def test_code_that_runs_at_import_is_marked_so(self) -> None:
        source = "X = 1\n\n\nclass C:\n    Y = 2\n\n    def f(self, a=3):\n        return 4\n"
        marks = {m.original: m.import_time for m in generate_mutants(source) if m.operator == "number_increment"}
        self.assertEqual({"1": True, "2": True, "3": True, "4": False}, marks)

    def test_a_mutant_changes_only_its_node_and_keeps_comments_and_formatting(self) -> None:
        source = textwrap.dedent("""\
            # A header comment   with   odd spacing.
            def f(a,  b):  # keep   this
                return (a
                        <  b)  # and this
            """)
        (mutant,) = [m for m in generate_mutants(source) if m.operator == "comparison"]
        self.assertEqual(source.replace("<  b", "<=  b"), apply_mutant(source, mutant))
        (deleted,) = [m for m in generate_mutants(source) if m.operator == "delete_statement"]
        self.assertEqual(
            "# A header comment   with   odd spacing.\ndef f(a,  b):  # keep   this\n    pass  # and this\n",
            apply_mutant(source, deleted),
        )


class ToyProjectTests(unittest.TestCase):
    """One harness run over the toy, checked from several sides."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.root = base / "toy"
        (cls.root / "tests").mkdir(parents=True)
        (cls.root / "logic.py").write_text(TOY_LOGIC, encoding="utf-8")
        (cls.root / "cli.py").write_text(TOY_CLI, encoding="utf-8")
        (cls.root / "tests" / "test_toy.py").write_text(TOY_TESTS, encoding="utf-8")
        (cls.root / "pyproject.toml").write_text(TOY_PYPROJECT, encoding="utf-8")
        cls.pids = base / "pids"
        cls.pids.mkdir()
        cls.work = base / "work"
        cls.work.mkdir()
        cls.reports = base / "quality"
        cls.allowlist = base / "allowlist.json"
        cls.allowlist.write_text(json.dumps({"entries": [EXACT_ENTRY, WRONG_TEXT, WRONG_LINE]}), encoding="utf-8")
        cls.baseline = base / "baseline.json"
        # logic.py scores 0.75 today, so a baseline of 0.8 is a drop; cli.py holds at 1.0.
        cls.baseline.write_text(json.dumps({"modules": {"logic.py": {"score": 0.8}, "cli.py": {"score": 1.0}}}), encoding="utf-8")
        cls.before = snapshot(cls.root)
        os.environ["TOY_PID_DIR"] = str(cls.pids)
        try:
            cls.result = run_harness(cls.root, *cls.options(), "--check-baseline", "logic.py,cli.py")
        finally:
            del os.environ["TOY_PID_DIR"]
        cls.reports_by_module = {}
        for module, name in (("logic.py", "logic"), ("cli.py", "cli")):
            path = cls.reports / f"mutation-report-{name}.json"
            if path.exists():
                cls.reports_by_module[module] = json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def options(cls) -> list[str]:
        return [
            "--jobs",
            "4",
            "--min-timeout",
            str(MIN_TIMEOUT),
            "--workdir",
            str(cls.work),
            "--report-dir",
            str(cls.reports),
            "--allowlist",
            str(cls.allowlist),
            "--baseline",
            str(cls.baseline),
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def report(self, module: str) -> dict:
        self.assertIn(module, self.reports_by_module, self.result.stdout + self.result.stderr)
        return self.reports_by_module[module]

    def statuses(self) -> dict[tuple[str, int, str, str], str]:
        found = {}
        for module in ("logic.py", "cli.py"):
            for mutant in self.report(module)["mutants"]:
                found[(module, mutant["line"], mutant["operator"], mutant["mutant"])] = mutant["status"]
        return found

    def test_every_mutant_has_the_status_worked_out_by_hand(self) -> None:
        self.assertEqual(EXPECTED, self.statuses())

    def test_it_reports_exactly_the_known_survivors(self) -> None:
        survivors = {(s["line"], s["operator"], s["mutant"], s["status"]) for s in self.report("logic.py")["survivors"]}
        self.assertEqual(
            {
                (6, "comparison", "age > 18", "survived"),
                (6, "number_increment", "19", "survived"),
                (22, "delete_statement", "pass", "no_coverage"),
                (22, "return_none", "return None", "no_coverage"),
                (22, "remove_not", "flag", "no_coverage"),
            },
            survivors,
        )
        (survivor,) = [s for s in self.report("logic.py")["survivors"] if s["mutant"] == "age > 18"]
        self.assertEqual("logic.py:6", survivor["location"])
        self.assertIn("-    return age >= 18\n+    return age > 18\n", survivor["diff"])
        self.assertEqual([], self.report("cli.py")["survivors"])

    def test_it_kills_the_mutants_the_weak_suite_does_catch(self) -> None:
        killed = {key for key, status in self.statuses().items() if status == "killed"}
        self.assertEqual({key for key, status in EXPECTED.items() if status == "killed"}, killed)

    def test_a_line_no_test_executes_is_no_coverage(self) -> None:
        line_22 = {key[2]: status for key, status in self.statuses().items() if key[:2] == ("logic.py", 22)}
        self.assertEqual({"delete_statement": "no_coverage", "return_none": "no_coverage", "remove_not": "no_coverage"}, line_22)

    def test_an_equivalent_mutant_is_excluded_only_by_an_exact_allowlist_entry(self) -> None:
        statuses = self.statuses()
        self.assertEqual("allowlisted", statuses[("logic.py", 10, "comparison", "n <= 0")])
        # A near-miss entry on line 6 doesn't hide its survivor.
        self.assertEqual("survived", statuses[("logic.py", 6, "comparison", "age > 18")])

    def test_a_stale_allowlist_entry_is_reported(self) -> None:
        self.assertEqual([WRONG_TEXT, WRONG_LINE], self.report("logic.py")["stale_allowlist"])
        self.assertIn("stale allowlist entry", self.result.stdout)

    def test_the_score_is_killed_and_timeout_over_the_measured_mutants(self) -> None:
        summary = {key: self.report("logic.py")[key] for key in ("total", "killed", "timeout", "survived")}
        summary.update({key: self.report("logic.py")[key] for key in ("no_coverage", "allowlisted", "error")})
        self.assertEqual(
            {"total": 21, "killed": 14, "timeout": 1, "survived": 2, "no_coverage": 3, "allowlisted": 1, "error": 0}, summary
        )
        self.assertEqual(0.75, self.report("logic.py")["score"])  # (14 + 1) / (21 - 1 - 0)
        self.assertEqual(1.0, self.report("cli.py")["score"])
        self.assertRegex(self.result.stdout, r"logic\.py\s+21\s+14\s+1\s+2\s+3\s+1\s+0\s+75\.0%")

    def test_an_endless_loop_is_a_timeout_within_the_bound_and_leaves_no_process(self) -> None:
        (looping,) = [m for m in self.report("logic.py")["mutants"] if m["line"] == 17 and m["operator"] == "number_zero"]
        self.assertEqual("timeout", looping["status"])
        self.assertEqual(MIN_TIMEOUT, looping["timeout"])
        self.assertLess(looping["duration"], MIN_TIMEOUT + 5)
        pids = [int(pid) for path in self.pids.iterdir() for pid in path.read_text().split()]
        self.assertGreater(len(pids), 2)  # the coverage run and each mutant run of test_countdown
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual([], [pid for pid in pids if alive(pid)])

    def test_a_script_its_test_runs_as_a_subprocess_is_still_mutated(self) -> None:
        cli = {key: status for key, status in self.statuses().items() if key[0] == "cli.py"}
        self.assertEqual({key: status for key, status in EXPECTED.items() if key[0] == "cli.py"}, cli)

    def test_the_source_tree_is_unchanged_and_no_copy_is_left(self) -> None:
        self.assertGreater(self.report("logic.py")["total"], 0)  # so a harness that did nothing can't pass
        self.assertEqual(self.before, snapshot(self.root))
        self.assertEqual([], list(self.work.iterdir()))

    def test_check_baseline_exits_1_when_a_score_drops(self) -> None:
        self.assertEqual(1, self.result.returncode, self.result.stdout + self.result.stderr)
        self.assertIn("logic.py: score 75.0% is below the baseline 80.0%", self.result.stdout)
        self.assertNotIn("cli.py: score", self.result.stdout)

    def test_check_baseline_fails_a_module_with_a_baseline_that_could_not_be_scored(self) -> None:
        unscored = {"module": "logic.py", "score": None}
        self.assertEqual(
            ["logic.py: no score (nothing measured) but the baseline is 80.0%"],
            baseline_failures([unscored], {"modules": {"logic.py": {"score": 0.8}}}),
        )
        self.assertEqual([], baseline_failures([unscored], {"modules": {}}))

    def test_check_baseline_passes_when_no_score_drops(self) -> None:
        held = run_harness(self.root, *self.options(), "--check-baseline", "cli.py")
        self.assertEqual(0, held.returncode, held.stdout + held.stderr)
        self.assertRegex(held.stdout, r"cli\.py\s+3\s+3\s+0\s+0\s+0\s+0\s+0\s+100\.0%")


if __name__ == "__main__":
    unittest.main()
