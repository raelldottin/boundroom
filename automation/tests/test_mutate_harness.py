"""The mutation harness measures what it claims (Tools/mutate, docs/reference/mutation-testing.md).

A toy project with known logic and a deliberately weak test suite is the fixture. Every
mutant's outcome is worked out by hand below, so a harness that reports anything else, such
as a survivor it should have killed or a kill it can't have seen, fails here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from Tools.mutate import apply_mutant, cli, report, runner
from Tools.mutate import Mutant, generate_mutants
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


def double(n):
    return n * 2


def triple(n):
    return n * 3
'''

TOY_CLI = '''\
"""Toy script that its test runs as a subprocess and never imports."""

import sys

if __name__ == "__main__":
    sys.exit(3)
'''

TOY_TOOL = '''\
"""Toy script that its test copies to a temporary directory and runs from there, as the
supervisor's own tests do with automation/supervisor."""

import sys

if __name__ == "__main__":
    sys.exit(7)
'''

TOY_TESTS = '''\
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from logic import countdown, double, floor_at_zero, is_adult, triple

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


STATE = []


def test_a_sets_state():
    STATE.append(1)


def test_b_needs_the_state_test_a_left():
    # Passes in the whole suite, in file order, and fails when run alone.
    assert STATE, "depends on test_a_sets_state running first"
    assert double(2) == 4


def test_c_flaky():
    # Passes its first two runs (the coverage run and the selection check), then always fails.
    counter = os.environ.get("TOY_FLAKY_FILE")
    if counter:
        handle = os.open(counter, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(handle, b"x")
        os.close(handle)
        assert os.path.getsize(counter) <= 2, "flaky: fails from its third run on"
    assert triple(2) == 6


def test_cli_exit_code():
    assert subprocess.run([sys.executable, str(ROOT / "cli.py")]).returncode == 3


def test_a_copy_of_the_tool_exits_7():
    with tempfile.TemporaryDirectory() as scratch:
        copied = Path(scratch, "tool.py")
        shutil.copy(ROOT / "tool.py", copied)
        assert subprocess.run([sys.executable, str(copied)]).returncode == 7


def test_an_unrelated_tool_py_elsewhere():
    # Same name and layout, other content: it must not be credited to tool.py.
    with tempfile.TemporaryDirectory() as scratch:
        other = Path(scratch, "tool.py")
        other.write_text((ROOT / "tool.py").read_text().replace("sys.exit(7)", "sys.exit(0)"))
        assert subprocess.run([sys.executable, str(other)]).returncode == 0
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
    # Only ever run from a copy in a temporary directory, yet the copy carries the mutant.
    ("tool.py", 6, "comparison", '__name__ != "__main__"'): "killed",
    ("tool.py", 7, "number_increment", "8"): "killed",
    ("tool.py", 7, "number_zero", "0"): "killed",
    # Only test_b executes line 26, and it fails when run alone, so its failure says nothing.
    ("logic.py", 26, "delete_statement", "pass"): "unreliable_selection",
    ("logic.py", 26, "return_none", "return None"): "unreliable_selection",
    ("logic.py", 26, "number_increment", "3"): "unreliable_selection",
    ("logic.py", 26, "number_zero", "0"): "unreliable_selection",
    # test_c_flaky fails under each mutant, and again when rerun without it, so no kill is trusted.
    ("logic.py", 30, "delete_statement", "pass"): "unreliable",
    ("logic.py", 30, "return_none", "return None"): "unreliable",
    ("logic.py", 30, "number_increment", "4"): "unreliable",
    ("logic.py", 30, "number_zero", "0"): "unreliable",
}

EXACT_ENTRY = {
    "module": "logic.py",
    "line": 10,
    "operator": "comparison",
    "original": "n < 0",
    "mutant": "n <= 0",
    "reason": "At n == 0 both branches return 0.",
}
# Near misses: right mutant on the wrong line, and the right line with other mutated text.
WRONG_TEXT = {**EXACT_ENTRY, "line": 6, "original": "age >= 18", "mutant": "age > 17", "reason": "near miss"}
WRONG_LINE = {**EXACT_ENTRY, "line": 7, "original": "age >= 18", "mutant": "age > 18", "reason": "wrong line"}

MIN_TIMEOUT = 5.0  # long enough for the unmutated recheck on a busy machine


PERSON = ("Toy Reviewer", "reviewer@example.com")
AGENT = ("boundroom-agent[bot]", "354874784+boundroom-agent[bot]@users.noreply.github.com")


def commit_file(path: Path, document: dict, author: tuple[str, str]) -> None:
    """Write ``document`` to ``path`` and commit it as ``author``, making the repository if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path.parent / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path.parent, check=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    name, email = author
    identity = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    env = {**os.environ, **identity}
    subprocess.run(["git", "add", path.name], cwd=path.parent, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "update"], cwd=path.parent, check=True, env=env)


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
            # Two nodes, two edits, though both give `x = not a`: duplicates are found by edit, not by program.
            "x = not not a": [(1, "remove_not", "not not a", "not a"), (1, "remove_not", "not a", "a")],
        }
        for source, expected in table.items():
            with self.subTest(source=source):
                self.assertEqual(sorted(expected), mutant_keys(source + "\n"))

    def test_the_same_edit_from_two_operators_is_one_mutant(self) -> None:
        from Tools.mutate.operators import dedupe

        def mutant(operator: str, replacement: str) -> Mutant:
            return Mutant(1, operator, "True", replacement, start=4, end=8, replacement=replacement, end_line=1, statement_line=1)

        first, second, other = mutant("boolean_constant", "False"), mutant("number_zero", "False"), mutant("x", "None")
        self.assertEqual([first, other], dedupe([first, second, other]))

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
        (cls.root / "tool.py").write_text(TOY_TOOL, encoding="utf-8")
        (cls.root / "tests" / "test_toy.py").write_text(TOY_TESTS, encoding="utf-8")
        (cls.root / "pyproject.toml").write_text(TOY_PYPROJECT, encoding="utf-8")
        cls.pids = base / "pids"
        cls.pids.mkdir()
        cls.work = base / "work"
        cls.work.mkdir()
        cls.reports = base / "quality"
        # The allowlist lives in a Git repository, added in a commit a person authored.
        cls.allowlist = base / "review" / "allowlist.json"
        commit_file(cls.allowlist, {"entries": [EXACT_ENTRY, WRONG_TEXT, WRONG_LINE]}, PERSON)
        cls.flaky = base / "flaky-count"
        cls.baseline = base / "baseline.json"
        # logic.py scores 0.75 today, so a baseline of 0.8 is a drop; cli.py holds at 1.0.
        cls.baseline.write_text(json.dumps({"modules": {"logic.py": {"score": 0.8}, "cli.py": {"score": 1.0}}}), encoding="utf-8")
        cls.baseline_before = cls.baseline.read_bytes()
        cls.before = snapshot(cls.root)
        os.environ["TOY_PID_DIR"] = str(cls.pids)
        os.environ["TOY_FLAKY_FILE"] = str(cls.flaky)
        try:
            cls.result = run_harness(cls.root, *cls.options(), "--check-baseline", "--update-baseline", "logic.py,cli.py,tool.py")
        finally:
            del os.environ["TOY_PID_DIR"]
            del os.environ["TOY_FLAKY_FILE"]
        cls.reports_by_module = {}
        for module, name in (("logic.py", "logic"), ("cli.py", "cli"), ("tool.py", "tool")):
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
        for module in ("logic.py", "cli.py", "tool.py"):
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
                (26, "delete_statement", "pass", "unreliable_selection"),
                (26, "return_none", "return None", "unreliable_selection"),
                (26, "number_increment", "3", "unreliable_selection"),
                (26, "number_zero", "0", "unreliable_selection"),
                (30, "delete_statement", "pass", "unreliable"),
                (30, "return_none", "return None", "unreliable"),
                (30, "number_increment", "4", "unreliable"),
                (30, "number_zero", "0", "unreliable"),
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
        keys = ("no_coverage", "allowlisted", "error", "unreliable_selection", "unreliable")
        summary.update({key: self.report("logic.py")[key] for key in keys})
        self.assertEqual(
            {
                "total": 29,
                "killed": 14,
                "timeout": 1,
                "survived": 2,
                "no_coverage": 3,
                "allowlisted": 1,
                "error": 0,
                "unreliable_selection": 4,
                "unreliable": 4,
            },
            summary,
        )
        # Unreliable mutants count against the score, so a flaky or order-dependent test lowers it.
        self.assertEqual(0.5357, self.report("logic.py")["score"])  # (14 + 1) / (29 - 1 - 0)
        self.assertEqual(1.0, self.report("cli.py")["score"])
        self.assertRegex(self.result.stdout, r"logic\.py\s+29\s+14\s+1\s+2\s+3\s+1\s+0\s+4\s+4\s+53\.6%")

    def test_a_kill_the_unmutated_tree_also_fails_is_not_trusted(self) -> None:
        line_30 = {key[2]: status for key, status in self.statuses().items() if key[:2] == ("logic.py", 30)}
        self.assertEqual({"unreliable"}, set(line_30.values()))

    def test_a_selection_that_fails_without_a_mutant_never_kills(self) -> None:
        line_26 = {key[2]: status for key, status in self.statuses().items() if key[:2] == ("logic.py", 26)}
        self.assertEqual({"unreliable_selection"}, set(line_26.values()))
        (selection,) = self.report("logic.py")["unreliable_selections"]
        self.assertEqual(["tests/test_toy.py::test_b_needs_the_state_test_a_left"], selection["tests"])
        self.assertEqual(1, selection["returncode"])
        self.assertIn("1 test selection fails without any mutant", self.result.stderr)

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

    def test_a_module_a_test_copies_and_runs_elsewhere_is_still_mapped(self) -> None:
        tool = {key: status for key, status in self.statuses().items() if key[0] == "tool.py"}
        copies = self.report("tool.py")["copies"]
        self.assertEqual({key: status for key, status in EXPECTED.items() if key[0] == "tool.py"}, tool, copies)
        self.assertEqual(1, copies["credited"], copies)
        # The unrelated tool.py elsewhere is seen, and refused for its content.
        self.assertEqual(["content differs"], [ignored["reason"] for ignored in copies["ignored"]], copies)
        selected = {mutant["tests"] for mutant in self.report("tool.py")["mutants"]}
        self.assertEqual({1}, selected)  # the copying test, and only it

    def test_a_script_its_test_runs_as_a_subprocess_is_still_mutated(self) -> None:
        cli = {key: status for key, status in self.statuses().items() if key[0] == "cli.py"}
        self.assertEqual({key: status for key, status in EXPECTED.items() if key[0] == "cli.py"}, cli)

    def test_the_source_tree_is_unchanged_and_no_copy_is_left(self) -> None:
        self.assertGreater(self.report("logic.py")["total"], 0)  # so a harness that did nothing can't pass
        self.assertEqual(self.before, snapshot(self.root))
        self.assertEqual([], list(self.work.iterdir()))

    def test_check_baseline_exits_1_when_a_score_drops(self) -> None:
        self.assertEqual(1, self.result.returncode, self.result.stdout + self.result.stderr)
        self.assertIn("logic.py: score 53.6% is below the baseline 80.0%", self.result.stdout)
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
        self.assertRegex(held.stdout, r"cli\.py\s+3\s+3\s+0\s+0\s+0\s+0\s+0\s+0\s+0\s+100\.0%")

    def test_an_update_that_fails_its_check_writes_no_baseline(self) -> None:
        # The main run passed --update-baseline with --check-baseline, and logic.py dropped.
        self.assertEqual(self.baseline_before, self.baseline.read_bytes())

    def test_an_update_records_what_was_measured(self) -> None:
        baseline = self.root.parent / "baseline-update.json"
        baseline.write_text(json.dumps({"modules": {"cli.py": {"score": 0.5}}}), encoding="utf-8")
        options = [*self.options()[:-1], str(baseline)]
        updated = run_harness(self.root, *options, "--check-baseline", "--update-baseline", "cli.py")
        self.assertEqual(0, updated.returncode, updated.stdout + updated.stderr)
        entry = json.loads(baseline.read_text(encoding="utf-8"))["modules"]["cli.py"]
        self.assertEqual(1.0, entry["score"])  # raised from 0.5
        data = TOY_CLI.encode("utf-8")
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), entry["module_blob"])
        self.assertEqual((None, False, {}), (entry["commit"], entry["dirty"], entry["trees"]))  # the toy isn't a Git checkout


class OutcomeTests(unittest.TestCase):
    def test_a_failure_is_a_kill_only_when_the_unmutated_tree_passes(self) -> None:
        table = {
            # (exit code, timed out, the same tests on the unmutated tree pass) -> status
            (0, False, None): "survived",
            (1, False, True): "killed",  # tests failed
            (2, False, True): "killed",  # the mutant broke import or collection: the suite caught it
            (3, False, True): "killed",
            (4, False, True): "killed",
            (5, False, True): "killed",
            (-9, False, True): "killed",
            (None, True, True): "timeout",
            (1, False, False): "unreliable",  # the unmutated tree fails too: a flaky test, not a kill
            (2, False, False): "unreliable",
            (None, True, False): "unreliable",
        }
        for (returncode, timed_out, unmutated), expected in table.items():
            with self.subTest(returncode=returncode, timed_out=timed_out, unmutated=unmutated):
                self.assertEqual(expected, report.outcome_status(returncode, timed_out, unmutated))

    def test_only_a_mutant_that_does_not_compile_is_left_out_of_the_score(self) -> None:
        counts = {"total": 10, "killed": 4, "timeout": 1, "survived": 1, "no_coverage": 1, "allowlisted": 1, "error": 1}
        counts.update({"unreliable_selection": 1, "unreliable": 0})
        self.assertEqual(0.625, report.score(counts))  # (4 + 1) / (10 - 1 allowlisted - 1 error)


class BaselineUpdateTests(unittest.TestCase):
    @staticmethod
    def measured(score: float) -> dict:
        counts = {key: 0 for key in ("total", *report.STATUSES)}
        return {"module": "m.py", "score": score, **counts, "commit": "c", "dirty": False, "trees": {}, "module_blob": "b"}

    def test_an_update_raises_a_score_and_never_lowers_one(self) -> None:
        baseline = {"modules": {"m.py": {"score": 0.8}}}
        self.assertEqual(0.8, report.updated_baseline(baseline, [self.measured(0.75)])["modules"]["m.py"]["score"])
        self.assertEqual(0.9, report.updated_baseline(baseline, [self.measured(0.9)])["modules"]["m.py"]["score"])
        self.assertEqual(0.6, report.updated_baseline({"modules": {}}, [self.measured(0.6)])["modules"]["m.py"]["score"])

    def test_lowering_a_score_names_the_module_and_records_the_reason(self) -> None:
        baseline = {"modules": {"m.py": {"score": 0.8}}}
        updated = report.updated_baseline(baseline, [self.measured(0.75)], lower={"m.py": "removed a flaky test"})
        self.assertEqual(0.75, updated["modules"]["m.py"]["score"])
        self.assertEqual(
            [{"module": "m.py", "from": 0.8, "to": 0.75, "reason": "removed a flaky test", "commit": "c"}],
            updated["lowered"],
        )

    def test_lower_baseline_needs_a_reason(self) -> None:
        with self.assertRaises(SystemExit), open(os.devnull, "w") as quiet:
            stderr, sys.stderr = sys.stderr, quiet
            try:
                cli.parse_args(["--lower-baseline", "m.py", "m.py"])
            finally:
                sys.stderr = stderr
        args = cli.parse_args(["--lower-baseline", "m.py", "--reason", "removed a flaky test", "m.py"])
        self.assertEqual({"m.py": "removed a flaky test"}, args.lower)


class AllowlistAuthorshipTests(unittest.TestCase):
    """An allowlist entry counts only when a person, not the agent, committed it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.project = base / "project"
        self.project.mkdir()
        (self.project / "mod.py").write_text("X = 1\n", encoding="utf-8")
        self.allowlist = base / "review" / "allowlist.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def refused(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return run_harness(self.project, "--allowlist", str(self.allowlist), *extra, "mod.py", timeout=60)

    def test_an_entry_the_agent_committed_is_refused(self) -> None:
        commit_file(self.allowlist, {"entries": [EXACT_ENTRY]}, AGENT)
        result = self.refused()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("added by boundroom-agent[bot]", result.stderr)
        self.assertNotIn("coverage run", result.stderr)

    def test_an_uncommitted_entry_is_refused(self) -> None:
        commit_file(self.allowlist, {"entries": []}, PERSON)
        self.allowlist.write_text(json.dumps({"entries": [EXACT_ENTRY]}), encoding="utf-8")
        result = self.refused()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("not committed", result.stderr)

    def test_an_allowlist_outside_git_with_entries_is_refused(self) -> None:
        self.allowlist.parent.mkdir(parents=True)
        self.allowlist.write_text(json.dumps({"entries": [EXACT_ENTRY]}), encoding="utf-8")
        result = self.refused()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("isn't in a Git repository", result.stderr)

    def test_a_free_text_reviewer_field_is_refused(self) -> None:
        with self.assertRaises(report.AllowlistError):
            report.validate_allowlist({"entries": [{**EXACT_ENTRY, "reviewed_by": "someone"}]})

    def test_a_lowered_score_the_agent_committed_is_refused(self) -> None:
        baseline = self.project.parent / "review" / "baseline.json"
        lowered = {"module": "mod.py", "from": 0.9, "to": 0.5, "reason": "r", "commit": "c"}
        commit_file(baseline, {"modules": {"mod.py": {"score": 0.5}}, "lowered": [lowered]}, AGENT)
        result = run_harness(self.project, "--baseline", str(baseline), "--check-baseline", "mod.py", timeout=60)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("lowered by boundroom-agent[bot]", result.stderr)

    def test_the_agent_is_recognised_under_any_of_its_names(self) -> None:
        self.assertTrue(report.is_agent("boundroom-agent[bot]", "354874784+boundroom-agent[bot]@users.noreply.github.com"))
        self.assertTrue(report.is_agent("repo-automation-agent[bot]", "x@example.com"))  # an earlier name
        self.assertTrue(report.is_agent("Someone", "354874784+renamed@users.noreply.github.com"))  # the bot's user id
        self.assertFalse(report.is_agent("Raell Dottin", "owner@example.com"))


class RepositoryLayoutTests(unittest.TestCase):
    def test_the_workflow_runs_only_when_a_person_starts_it(self) -> None:
        # A schedule needs the owner's approval as its own recorded action (AGENTS.md).
        workflow = (REPO_ROOT / ".github/workflows/mutation.yml").read_text(encoding="utf-8")
        triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", triggers)
        self.assertNotIn("schedule:", triggers)
        self.assertNotIn("cron", triggers)

    def test_only_the_baseline_and_the_allowlist_are_committed_under_quality(self) -> None:
        tracked = subprocess.run(["git", "ls-files", "quality"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(["quality/mutation-allowlist.json", "quality/mutation-baseline.json"], tracked.stdout.split())

    def test_the_default_report_directory_is_ignored_and_ci_uploads_it(self) -> None:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", f"{cli.DEFAULT_REPORT_DIR}/mutation-report-x.json"], cwd=REPO_ROOT, check=False
        )
        self.assertEqual(0, ignored.returncode)
        workflow = (REPO_ROOT / ".github/workflows/mutation.yml").read_text(encoding="utf-8")
        self.assertIn(f"path: {cli.DEFAULT_REPORT_DIR}/mutation-report-*.json", workflow)


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def test_it_records_the_python_trees_and_ignores_the_report_directory(self) -> None:
        commit, dirty, trees = runner.provenance(self.root, exclude=["quality"])
        self.assertEqual((self.git("rev-parse", "HEAD"), False), (commit, dirty))
        self.assertEqual({"pkg": self.git("rev-parse", "HEAD:pkg")}, trees)  # docs holds no Python
        (self.root / "quality").mkdir()
        (self.root / "quality" / "mutation-report-pkg.mod.json").write_text("{}", encoding="utf-8")
        self.assertFalse(runner.provenance(self.root, exclude=["quality"])[1])
        (self.root / "pkg" / "mod.py").write_text("X = 2\n", encoding="utf-8")
        self.assertTrue(runner.provenance(self.root, exclude=["quality"])[1])

    def test_an_update_from_a_dirty_tree_is_refused_before_measuring(self) -> None:
        (self.root / "pkg" / "mod.py").write_text("X = 2\n", encoding="utf-8")
        refused = run_harness(self.root, "--update-baseline", "pkg/mod.py", timeout=60)
        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("refusing to update the baseline from a working tree with changes", refused.stderr)
        self.assertNotIn("coverage run", refused.stderr)


if __name__ == "__main__":
    unittest.main()
