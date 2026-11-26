"""The benchmark CLI (``python -m automation.benchmark``), tested by what it prints and writes.

No test here starts a model session or spends anything: every agent session is a local fake
script passed as ``--agent-cmd``, every ProgramBench call a fake passed as
``--programbench-cmd``, and every run names ``--instances`` except the one test of the default
smoke set, which still runs the fake agent. ``--cleanroom`` is used only where argument
validation refuses it before any work starts.

Exit codes (0, 2, 78), flags and the JSON records are the compatibility surface. The exact
summary, usage and ``stop:`` texts pinned here are not; they are characterization, pinned so a
refactor of ``main`` shows any change to what an operator reads.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import dataclass
from pathlib import Path

from automation.benchmark import run as benchmark_run
from automation.benchmark.adapter import SubmissionResult
from automation.benchmark.evalrunner import RecordedEvalRunner
from automation.benchmark.instances import DEFAULT_SMOKE_INSTANCES, TaskSpec
from automation.benchmark.strategies import ALL_LANES
from automation.tests.benchmark_fixtures import (
    DEFAULT_THRESHOLDS,
    RECORDER,
    ROUNDING_SUMMARY,
    THRESHOLD_SKIP_REASON,
    ev,
    rounding_fixture,
)

# Writes a buildable C project into its working directory (the workspace), optionally names a
# forwarded variable's value in a file, optionally sleeps and then writes one more file, and
# exits with argv[1].
FAKE_AGENT = """import os, sys, time
from pathlib import Path
Path("compile.sh").write_text("#!/bin/sh\\ncc -o executable main.c\\n")
Path("main.c").write_text("int main(void){return 0;}\\n")
forwarded = os.environ.get("U12_FORWARDED")
if forwarded:
    Path("env-" + forwarded + ".txt").write_text("")
delay = float(sys.argv[2])
if delay:
    time.sleep(delay)
    Path("late.txt").write_text("")
sys.exit(int(sys.argv[1]))
"""


# Stands in for `programbench eval <run_dir>`: every directory holding a submission gets a
# passing <iid>.eval.json, as the real one would for a passing submission.
PASSING_PROGRAMBENCH = """import json, sys
from pathlib import Path
out = Path({out!r})
calls = json.loads(out.read_text()) if out.exists() else []
calls.append(sys.argv[1:])
out.write_text(json.dumps(calls))
rd = Path(sys.argv[2])
for d in sorted(p for p in rd.rglob("*") if p.is_dir()):
    if (d / "submission.tar.gz").is_file():
        (d / (d.name + ".eval.json")).write_text(json.dumps({{"test_results": [{{"name": "t1", "status": "passed"}}]}}))
"""

USAGE = (
    "usage: python -m automation.benchmark [-h]\n"
    "                                      {run,eval,score,all,lanes,compare} ...\n"
    "python -m automation.benchmark: error: "
)
CLEANROOM_MESSAGE = (
    "--judge can't be used with --cleanroom yet: the lanes' tools run in ProgramBench's image, "
    "but the judge's would run on this machine while it reads model-written code"
)
NO_EVAL_MESSAGE = "--judge needs --eval: the gate reads ProgramBench's tests before it pays for a judge"
RUNNER_REFUSAL = (
    "stop: telemetry: the benchmark keeps runner telemetry off, because it would add variables and an "
    "exporter to the agent session being measured\n"
)


LANE_A_ROW_NO_EVAL = "| A | one session, raw objective | valid | valid | 0.0% | 0.0% | 0.0% | 0.0% | 0 | - | - | 0.000 | 1 | 0 |"


def lanes_args(*flags: str):
    return benchmark_run.build_parser().parse_args(["lanes", "--run-dir", "rd", *flags])


@dataclass
class Result:
    code: object
    out: str
    err: str


class ScratchCase(unittest.TestCase):
    """A scratch directory that also holds every temporary directory the code under test makes."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="test-benchmark-run-")).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        (self.scratch / "tmp").mkdir()
        # The adapter mkdtemps a workspace per session; TMPDIR alone is ignored once cached.
        self.enterContext(unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch / "tmp")))
        # argparse wraps its usage line to the terminal width, and from Python 3.14 colours it
        # when asked to.
        self.enterContext(unittest.mock.patch.dict(os.environ, {"COLUMNS": "80", "NO_COLOR": "1"}))
        for name in ("FORCE_COLOR", "PYTHON_COLORS"):
            os.environ.pop(name, None)
        # A developer's own telemetry or offline setting must not change what these tests see;
        # patch.dict restores them afterwards.
        for name in [n for n in os.environ if n.startswith("BOUNDROOM_TELEMETRY") or n == "BOUNDROOM_OFFLINE"]:
            del os.environ[name]

    def script(self, name: str, body: str) -> Path:
        path = self.scratch / name
        path.write_text(body, encoding="utf-8")
        return path


class FakeAdapter:
    """An agent that writes its task's instance id as the submission."""

    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
        Path(out_tar).write_text(task.instance_id, encoding="utf-8")
        return SubmissionResult(task.instance_id, Path(out_tar), Path(out_tar).parent, 0)


class ParserDefaultsTests(unittest.TestCase):
    def test_defaults_a_run_relies_on(self) -> None:
        args = lanes_args()
        expected = {
            "programbench_cmd": ["uvx", "programbench"],
            "timeout": 1800,
            "repeats": 1,
            "min_repeats": 2,
            "lanes": list(ALL_LANES),
            "docker_cpus": None,
            "workers": None,
            "agent_cmd": None,
            "agent_env": [],
            "cleanroom": False,
            "eval": False,
            "judge": False,
            "judge_pass_fraction": 0.95,
            "judge_min_score": 7,
            "judge_timeout": 600,
            "judge_agent_cmd": None,
            "instances": None,
            "all": False,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(value, getattr(args, name))

    def test_repo_root_defaults_to_this_checkout(self) -> None:
        self.assertEqual(Path(__file__).resolve().parents[2], lanes_args().repo_root)


class LanesUsageErrorTests(unittest.TestCase):
    MIN_REPEATS = "--min-repeats must be between 1 and --repeats ({repeats}), got {got}"

    def test_first_failing_rule_is_reported_else_none(self) -> None:
        cases = [
            ("no judge, cleanroom", ["--cleanroom"], None),
            ("no judge, min repeats above repeats", ["--min-repeats", "5"], None),
            ("judge with cleanroom", ["--judge", "--cleanroom", "--eval"], CLEANROOM_MESSAGE),
            ("judge with cleanroom, no eval", ["--judge", "--cleanroom"], CLEANROOM_MESSAGE),
            ("judge without eval", ["--judge"], NO_EVAL_MESSAGE),
            ("judge without eval, bad min repeats", ["--judge", "--min-repeats", "0"], NO_EVAL_MESSAGE),
            ("default min repeats above default repeats", ["--judge", "--eval"], self.MIN_REPEATS.format(repeats=1, got=2)),
            (
                "min repeats 0",
                ["--judge", "--eval", "--repeats", "2", "--min-repeats", "0"],
                self.MIN_REPEATS.format(repeats=2, got=0),
            ),
            ("min repeats 1", ["--judge", "--eval", "--repeats", "2", "--min-repeats", "1"], None),
            ("min repeats equal to repeats", ["--judge", "--eval", "--repeats", "3", "--min-repeats", "3"], None),
            (
                "min repeats one above repeats",
                ["--judge", "--eval", "--repeats", "3", "--min-repeats", "4"],
                self.MIN_REPEATS.format(repeats=3, got=4),
            ),
        ]
        for name, flags, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, benchmark_run.lanes_usage_error(lanes_args(*flags)))


class ResolveAgentEnvTests(unittest.TestCase):
    def test_nothing_named_forwards_nothing(self) -> None:
        for names in (None, []):
            with self.subTest(names=names):
                self.assertEqual({}, benchmark_run.resolve_agent_env(names))

    def test_named_variables_are_copied(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"U12_ONE": "1", "U12_TWO": "2", "U12_OTHER": "x"}):
            self.assertEqual({"U12_ONE": "1", "U12_TWO": "2"}, benchmark_run.resolve_agent_env(["U12_ONE", "U12_TWO"]))

    def test_a_refusal_names_the_position_of_the_offending_argument(self) -> None:
        cases = [
            ("not a name", ["U12_ONE", "bad name"], "--agent-env argument #2 is not a variable name"),
            ("not set", ["U12_ONE", "U12_UNSET_VARIABLE"], "--agent-env argument #2 names a variable that is not set"),
        ]
        with unittest.mock.patch.dict(os.environ, {"U12_ONE": "1"}):
            os.environ.pop("U12_UNSET_VARIABLE", None)
            for name, names, prefix in cases:
                with self.subTest(name):
                    with self.assertRaises(SystemExit) as raised:
                        benchmark_run.resolve_agent_env(names)
                    self.assertTrue(str(raised.exception.code).startswith(prefix), raised.exception.code)


class BuildStopGateTests(unittest.TestCase):
    def test_no_gate_without_judge(self) -> None:
        self.assertIsNone(benchmark_run.build_stop_gate(lanes_args("--eval", "--repeats", "3"), {}))

    def test_gate_settings_come_from_the_flags(self) -> None:
        cases = [
            ("defaults", [], {"min_repeats": 2, "pass_fraction": 0.95, "min_score": 7}),
            (
                "flags",
                ["--min-repeats", "3", "--judge-pass-fraction", "0.5", "--judge-min-score", "4"],
                {"min_repeats": 3, "pass_fraction": 0.5, "min_score": 4},
            ),
        ]
        for name, flags, expected in cases:
            with self.subTest(name):
                gate = benchmark_run.build_stop_gate(lanes_args("--judge", "--eval", "--repeats", "3", *flags), {})
                assert gate is not None
                self.assertEqual(expected, gate.settings)

    def test_a_judge_timeout_below_one_second_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "the judge's timeout must be at least 1 second, got 0"):
            benchmark_run.build_stop_gate(lanes_args("--judge", "--eval", "--judge-timeout", "0"), {})


class RunBenchmarkTests(ScratchCase):
    def test_a_missing_nested_run_dir_gets_one_submission_per_instance(self) -> None:
        run_dir = self.scratch / "a" / "b" / "rd"
        benchmark_run.run_benchmark(run_dir, ["i1", "i2"], FakeAdapter(), score=False)
        for iid in ("i1", "i2"):
            with self.subTest(iid):
                self.assertEqual(iid, (run_dir / iid / "submission.tar.gz").read_text(encoding="utf-8"))

    def test_evaluated_run_is_scored_and_the_report_written(self) -> None:
        run_dir = self.scratch / "rd"
        runner = RecordedEvalRunner.from_mapping({"i1": ev(1, 1), "i2": ev(0, 2)})
        report = benchmark_run.run_benchmark(run_dir, ["i1", "i2"], FakeAdapter(), eval_runner=runner)
        assert report is not None
        self.assertEqual((2, 1), (report.instance_count, report.resolved_count))
        written = json.loads((run_dir / benchmark_run.REPORT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(report.to_dict(), written)

    def test_without_an_eval_runner_nothing_is_scored(self) -> None:
        run_dir = self.scratch / "rd"
        report = benchmark_run.run_benchmark(run_dir, ["i1"], FakeAdapter())
        assert report is not None
        self.assertEqual(0, report.instance_count)
        self.assertTrue((run_dir / benchmark_run.REPORT_FILENAME).is_file())

    def test_no_instances_still_writes_an_empty_report_in_a_new_run_dir(self) -> None:
        run_dir = self.scratch / "new" / "rd"
        report = benchmark_run.run_benchmark(run_dir, [], FakeAdapter())
        assert report is not None
        self.assertEqual(0, report.instance_count)
        self.assertEqual([benchmark_run.REPORT_FILENAME], [p.name for p in run_dir.iterdir()])

    def test_score_false_returns_none_and_writes_no_report(self) -> None:
        run_dir = self.scratch / "rd"
        runner = RecordedEvalRunner.from_mapping({"i1": ev(1, 1)})
        self.assertIsNone(benchmark_run.run_benchmark(run_dir, ["i1"], FakeAdapter(), eval_runner=runner, score=False))
        self.assertFalse((run_dir / benchmark_run.REPORT_FILENAME).exists())
        self.assertTrue((run_dir / "i1" / "i1.eval.json").is_file())

    def test_produce_submissions_reuses_an_existing_instance_dir(self) -> None:
        run_dir = self.scratch / "rd"
        (run_dir / "i1").mkdir(parents=True)
        benchmark_run.produce_submissions(run_dir, ["i1"], FakeAdapter())
        self.assertEqual("i1", (run_dir / "i1" / "submission.tar.gz").read_text(encoding="utf-8"))


UNMANAGED_CLIS = ("claude", "codex", "hermes", "uvx", "programbench", "docker")


class MainCase(ScratchCase):
    def setUp(self) -> None:
        super().setUp()
        self.agent = self.script("fake_agent.py", FAKE_AGENT)
        # The real CLIs a regression could fall back to (a model session, ProgramBench, Docker)
        # are shadowed on PATH by stand-ins that only log; any call fails the test.
        guard = self.scratch / "guard"
        guard.mkdir()
        self.guard_log = self.scratch / "guard.log"
        for name in UNMANAGED_CLIS:
            stand_in = guard / name
            stand_in.write_text(f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(self.guard_log))}\nexit 97\n')
            stand_in.chmod(0o755)
        os.environ["PATH"] = f"{guard}{os.pathsep}{os.environ.get('PATH', '')}"
        self.addCleanup(self.assert_no_unmanaged_cli_ran)

    def assert_no_unmanaged_cli_ran(self) -> None:
        calls = self.guard_log.read_text() if self.guard_log.exists() else ""
        self.assertEqual("", calls, "a real agent, ProgramBench or Docker CLI was called")

    def agent_cmd(self, exit_code: int = 0, delay: float = 0) -> str:
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(self.agent))} {exit_code} {delay} --slice-id {{slice_id}}"

    def common(self, exit_code: int = 0, delay: float = 0, timeout: int = 60) -> list[str]:
        return ["--instances", "inst-a", "--timeout", str(timeout), "--agent-cmd", self.agent_cmd(exit_code, delay)]

    def main(self, *argv: str) -> Result:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code: object = benchmark_run.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return Result(code, out.getvalue(), err.getvalue())

    def recorder(self, name: str, code: int = 0) -> tuple[Path, Path]:
        out = self.scratch / f"{name}.json"
        return self.script(f"{name}.py", RECORDER.format(out=str(out), code=code)), out

    def passing_programbench(self, name: str = "pb") -> tuple[Path, Path]:
        out = self.scratch / f"{name}.json"
        return self.script(f"{name}.py", PASSING_PROGRAMBENCH.format(out=str(out))), out

    @staticmethod
    def files(instance_dir: Path) -> list[str]:
        return (instance_dir / "submission.files.txt").read_text(encoding="utf-8").splitlines()


class MainRunTests(MainCase):
    def test_run_writes_a_submission_per_instance_whatever_the_agent_exits(self) -> None:
        for exit_code in (0, 1):
            with self.subTest(agent_exit=exit_code):
                run_dir = self.scratch / f"run{exit_code}" / "nested" / "rd"
                result = self.main("run", "--run-dir", str(run_dir), *self.common(exit_code))
                self.assertEqual((0, f"Wrote submissions to {run_dir}\n", ""), (result.code, result.out, result.err))
                self.assertEqual(["inst-a"], sorted(p.name for p in run_dir.iterdir()))
                self.assertTrue((run_dir / "inst-a" / "submission.tar.gz").is_file())
                self.assertEqual(["compile.sh", "main.c"], self.files(run_dir / "inst-a"))

    def test_run_without_instances_uses_the_smoke_set(self) -> None:
        run_dir = self.scratch / "rd"
        result = self.main("run", "--run-dir", str(run_dir), "--timeout", "60", "--agent-cmd", self.agent_cmd())
        self.assertEqual(0, result.code)
        self.assertEqual(sorted(DEFAULT_SMOKE_INSTANCES), sorted(p.name for p in run_dir.iterdir()))

    def test_run_forwards_only_the_named_variables(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"U12_FORWARDED": "fwdvalue"}):
            for flags, expected in (
                ([], ["compile.sh", "main.c"]),
                (["--agent-env", "U12_FORWARDED"], ["compile.sh", "env-fwdvalue.txt", "main.c"]),
            ):
                with self.subTest(flags=flags):
                    run_dir = self.scratch / f"rd{len(flags)}"
                    self.assertEqual(0, self.main("run", "--run-dir", str(run_dir), *self.common(), *flags).code)
                    self.assertEqual(expected, self.files(run_dir / "inst-a"))

    def test_run_refuses_an_unset_forwarded_variable_before_any_work(self) -> None:
        self.enterContext(unittest.mock.patch.dict(os.environ))
        os.environ.pop("U12_UNSET_VARIABLE", None)
        run_dir = self.scratch / "rd"
        result = self.main("run", "--run-dir", str(run_dir), *self.common(), "--agent-env", "U12_UNSET_VARIABLE")
        self.assertTrue(str(result.code).startswith("--agent-env argument #1 names a variable that is not set"))
        self.assertFalse(run_dir.exists())

    def test_the_agent_timeout_is_the_flag(self) -> None:
        # The agent writes late.txt only if it outlives the 3-second timeout. Only its absence is
        # asserted: on a loaded machine the agent may be stopped before it writes anything.
        run_dir = self.scratch / "rd"
        result = self.main("run", "--run-dir", str(run_dir), *self.common(delay=30, timeout=3))
        self.assertEqual(0, result.code)
        self.assertNotIn("late.txt", self.files(run_dir / "inst-a"))


class MainEvalScoreAllTests(MainCase):
    def test_eval_hands_programbench_the_run_dir_and_flags(self) -> None:
        recorder, recorded = self.recorder("eval")
        run_dir = self.scratch / "rd"
        run_dir.mkdir()
        result = self.main(
            "eval", "--run-dir", str(run_dir), "--workers", "2", "--docker-cpus", "3",
            "--programbench-cmd", sys.executable, str(recorder),
        )  # fmt: skip
        self.assertEqual((0, "", ""), (result.code, result.out, result.err))
        self.assertEqual([["eval", str(run_dir), "--workers", "2", "--docker-cpus", "3"]], json.loads(recorded.read_text()))

    def test_a_failing_eval_raises(self) -> None:
        recorder, _ = self.recorder("eval_bad", code=3)
        run_dir = self.scratch / "rd"
        run_dir.mkdir()
        argv = ["eval", "--run-dir", str(run_dir), "--programbench-cmd", sys.executable, str(recorder)]
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            benchmark_run.main(argv)
        self.assertEqual(3, raised.exception.returncode)

    @unittest.skipUnless(DEFAULT_THRESHOLDS, THRESHOLD_SKIP_REASON)
    def test_score_prints_the_summary_and_writes_the_report(self) -> None:
        run_dir = self.scratch / "rd"
        for iid, data in rounding_fixture().items():
            (run_dir / iid).mkdir(parents=True)
            (run_dir / iid / f"{iid}.eval.json").write_text(json.dumps(data), encoding="utf-8")
        result = self.main("score", "--run-dir", str(run_dir))
        self.assertEqual((0, "\n".join(ROUNDING_SUMMARY) + "\n", ""), (result.code, result.out, result.err))
        report = json.loads((run_dir / "effectiveness-report.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (3, 1, 2, 0.3333, {"compile_failed": 1}),
            (
                report["instance_count"],
                report["resolved_count"],
                report["near_resolved_count"],
                report["resolve_rate"],
                report["error_counts"],
            ),
        )
        self.assertEqual(["a", "b", "c"], [row["instance_id"] for row in report["instances"]])

    def test_all_runs_evaluates_and_scores(self) -> None:
        programbench, recorded = self.passing_programbench()
        run_dir = self.scratch / "rd"
        result = self.main(
            "all", "--run-dir", str(run_dir), *self.common(), "--workers", "2", "--docker-cpus", "3",
            "--programbench-cmd", sys.executable, str(programbench),
        )  # fmt: skip
        self.assertEqual(0, result.code)
        self.assertEqual([["eval", str(run_dir), "--workers", "2", "--docker-cpus", "3"]], json.loads(recorded.read_text()))
        report = json.loads((run_dir / "effectiveness-report.json").read_text(encoding="utf-8"))
        self.assertEqual((1, 1), (report["instance_count"], report["resolved_count"]))
        self.assertEqual(["compile.sh", "main.c"], self.files(run_dir / "inst-a"))
        self.assertTrue(result.out.startswith("Harness Effectiveness (ProgramBench)\n  instances        : 1\n"), result.out)
        self.assertIn("  inst-a                                           100.0%       yes  \n", result.out)
        self.assertTrue(result.out.endswith("yes  \n"), result.out)


class MainLanesCompareTests(MainCase):
    def comparison(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "lane-comparison.json").read_text(encoding="utf-8"))

    def assert_lanes_output(self, result: Result, run_dir: Path) -> None:
        markdown = (run_dir / "lane-comparison.md").read_text(encoding="utf-8")
        self.assertEqual((0, ""), (result.code, result.err))
        self.assertEqual(markdown + "\n" + f"Wrote {run_dir / 'lane-comparison.md'}\n", result.out)

    def test_compare_on_an_empty_run_dir_measures_nothing(self) -> None:
        run_dir = self.scratch / "rd"
        run_dir.mkdir()
        result = self.main("compare", "--run-dir", str(run_dir))
        self.assert_lanes_output(result, run_dir)
        self.assertEqual(
            {
                "lanes": [],
                "deltas": [],
                "note": "Primary metric is ProgramBench correctness. A lane that saves context but loses resolve rate is worse.",
            },
            self.comparison(run_dir),
        )
        self.assertIn("No comparison had a valid pair on both sides; this run measures nothing.", result.out)

    def test_lanes_runs_each_lane_and_compare_rebuilds_the_same_lanes(self) -> None:
        run_dir = self.scratch / "rd"
        with unittest.mock.patch.dict(os.environ, {"U12_FORWARDED": "fwdvalue"}):
            result = self.main(
                "lanes", "--run-dir", str(run_dir), *self.common(), "--lanes", "A", "--repeats", "1",
                "--agent-env", "U12_FORWARDED",
            )  # fmt: skip
        self.assert_lanes_output(result, run_dir)
        self.assertIn(LANE_A_ROW_NO_EVAL + "\n", result.out)
        self.assertEqual(["A", "lane-comparison.json", "lane-comparison.md"], sorted(p.name for p in run_dir.iterdir()))
        self.assertEqual(["compile.sh", "env-fwdvalue.txt", "main.c"], self.files(run_dir / "A" / "r1" / "inst-a"))
        experiment = self.comparison(run_dir)
        self.assertEqual(["A"], [lane["lane"] for lane in experiment["lanes"]])
        self.assertNotIn("stop_gate", experiment)

        (run_dir / "lane-comparison.json").unlink()
        (run_dir / "lane-comparison.md").unlink()
        result = self.main("compare", "--run-dir", str(run_dir), "--lanes", "A", "--repeats", "1")
        self.assert_lanes_output(result, run_dir)
        self.assertEqual(experiment["lanes"], self.comparison(run_dir)["lanes"])

    def test_lanes_timeout_is_the_flag(self) -> None:
        run_dir = self.scratch / "rd"
        result = self.main("lanes", "--run-dir", str(run_dir), *self.common(delay=30, timeout=3), "--lanes", "A")
        self.assertEqual(0, result.code)
        self.assertNotIn("late.txt", self.files(run_dir / "A" / "r1" / "inst-a"))

    def test_lanes_with_eval_scores_every_cell(self) -> None:
        programbench, recorded = self.passing_programbench()
        run_dir = self.scratch / "rd"
        result = self.main(
            "lanes", "--run-dir", str(run_dir), *self.common(), "--lanes", "A", "--repeats", "1", "--eval",
            "--workers", "2", "--docker-cpus", "3", "--programbench-cmd", sys.executable, str(programbench),
        )  # fmt: skip
        self.assert_lanes_output(result, run_dir)
        self.assertEqual(
            [["eval", str(run_dir / "A" / "r1"), "--workers", "2", "--docker-cpus", "3"]], json.loads(recorded.read_text())
        )
        (lane,) = self.comparison(run_dir)["lanes"]
        self.assertEqual({"resolve_rate": 1.0, "near_resolve_rate": 1.0, "mean_pass_fraction": 1.0}, lane["primary"])

    def test_lanes_judge_gates_rounds_with_the_flags_settings(self) -> None:
        # min-repeats == repeats: round 1 is below the minimum and round 2 is the ceiling, so
        # the gate decides twice without paying for a judge session.
        programbench, _ = self.passing_programbench()
        run_dir = self.scratch / "rd"
        result = self.main(
            "lanes", "--run-dir", str(run_dir), *self.common(), "--lanes", "A", "--repeats", "2", "--eval",
            "--judge", "--judge-pass-fraction", "0.5", "--judge-min-score", "4",
            "--programbench-cmd", sys.executable, str(programbench),
        )  # fmt: skip
        self.assert_lanes_output(result, run_dir)
        gate = self.comparison(run_dir)["stop_gate"]
        self.assertEqual({"min_repeats": 2, "pass_fraction": 0.5, "min_score": 4}, gate["settings"])
        self.assertTrue((run_dir / "judge" / "r1").is_dir())
        self.assertTrue((run_dir / "judge" / "r2").is_dir())


class MainUsageErrorTests(MainCase):
    def test_lanes_usage_errors_exit_2_before_any_work(self) -> None:
        cases = [
            ("judge with cleanroom", ["--judge", "--cleanroom"], CLEANROOM_MESSAGE),
            ("judge without eval", ["--judge"], NO_EVAL_MESSAGE),
            ("default repeats", ["--judge", "--eval"], "--min-repeats must be between 1 and --repeats (1), got 2"),
            (
                "min repeats 0",
                ["--judge", "--eval", "--repeats", "2", "--min-repeats", "0"],
                "--min-repeats must be between 1 and --repeats (2), got 0",
            ),
            (
                "min repeats 3 of 2",
                ["--judge", "--eval", "--repeats", "2", "--min-repeats", "3"],
                "--min-repeats must be between 1 and --repeats (2), got 3",
            ),
            (
                "judge timeout 0",
                ["--judge", "--eval", "--repeats", "2", "--judge-timeout", "0"],
                "the judge's timeout must be at least 1 second, got 0",
            ),
            (
                "pass fraction 1.5",
                ["--judge", "--eval", "--repeats", "2", "--judge-pass-fraction", "1.5"],
                "pass_fraction must be between 0 and 1, got 1.5",
            ),
        ]
        for name, flags, message in cases:
            with self.subTest(name):
                run_dir = self.scratch / name.replace(" ", "_") / "rd"
                result = self.main("lanes", "--run-dir", str(run_dir), *self.common(), *flags)
                self.assertEqual((2, "", USAGE + message + "\n"), (result.code, result.out, result.err))
                self.assertFalse(run_dir.exists())

    def test_a_subcommand_is_required(self) -> None:
        result = self.main()
        self.assertEqual(
            (2, "", USAGE + "the following arguments are required: command\n"),
            (result.code, result.out, result.err),
        )

    def test_every_subcommand_requires_run_dir(self) -> None:
        # Fakes stand in for the agent and ProgramBench, so a missing --run-dir check would
        # still launch nothing real; the recorder shows ProgramBench was never called.
        programbench, programbench_calls = self.recorder("pb")
        fake_pb = ["--programbench-cmd", sys.executable, str(programbench)]
        rows = [
            ("run", self.common()),
            ("eval", fake_pb),
            ("score", []),
            ("all", [*self.common(), *fake_pb]),
            ("lanes", self.common()),
            ("compare", []),
        ]
        for command, flags in rows:
            with self.subTest(command):
                result = self.main(command, *flags)
                self.assertEqual(
                    (2, "", f"python -m automation.benchmark {command}: error: the following arguments are required: --run-dir"),
                    (result.code, result.out, result.err.splitlines()[-1]),
                )
        self.assertFalse(programbench_calls.exists())


class MainTelemetryTests(MainCase):
    def lanes(self, run_dir: Path, *flags: str) -> Result:
        return self.main("lanes", "--run-dir", str(run_dir), *self.common(), "--lanes", "A", "--repeats", "1", *flags)

    def test_telemetry_that_cannot_start_stops_the_experiment_with_exit_78(self) -> None:
        cases = [
            (
                "runner telemetry",
                ["--telemetry", "file", "--telemetry-file-dir", str(self.scratch / "spans"), "--telemetry-runner", "metadata"],
                RUNNER_REFUSAL,
            ),
            (
                "required file mode without a directory",
                ["--telemetry", "file", "--telemetry-required"],
                "telemetry: file mode needs file_dir; telemetry is off\n"
                "stop: telemetry: file mode needs file_dir; refusing to start because telemetry is required\n",
            ),
            (
                "required, refused by the exporter",
                ["--telemetry", "file", "--telemetry-required", "--telemetry-file-dir", "relative/spans"],
                "stop: telemetry: file_dir must be an absolute path, not relative/spans; "
                "refusing to start because telemetry is required\n",
            ),
        ]
        for name, flags, stderr in cases:
            with self.subTest(name):
                run_dir = self.scratch / name.replace(" ", "_") / "rd"
                result = self.lanes(run_dir, *flags)
                self.assertEqual((78, "", stderr), (result.code, result.out, result.err))
                self.assertFalse(run_dir.exists())

    def test_telemetry_that_falls_back_to_off_says_so_and_runs(self) -> None:
        run_dir = self.scratch / "rd"
        result = self.lanes(run_dir, "--telemetry", "file", "--telemetry-file-dir", "relative/spans")
        self.assertEqual(
            (0, "telemetry: file_dir must be an absolute path, not relative/spans; continuing with telemetry off\n"),
            (result.code, result.err),
        )
        self.assertTrue((run_dir / "lane-comparison.json").is_file())

    def test_traced_experiment_has_written_its_spans_when_main_returns(self) -> None:
        run_dir = self.scratch / "rd"
        spans_dir = self.scratch / "spans"
        result = self.lanes(run_dir, "--telemetry", "file", "--telemetry-file-dir", str(spans_dir))
        self.assertEqual((0, ""), (result.code, result.err))
        (spans_file,) = spans_dir.glob("*.spans.jsonl")
        records = [json.loads(line) for line in spans_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, sum(1 for record in records if record["parent_span_id"] is None), records)


if __name__ == "__main__":
    unittest.main()
