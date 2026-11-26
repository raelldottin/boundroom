"""Output-based tests for automation.benchmark.evalrunner.

The ProgramBench CLI is an unmanaged, out-of-process dependency, so it is replaced by a
fake Python script that records the arguments it was started with. The tests compare
those recorded arguments, and the files RecordedEvalRunner writes, with literal values.
The host's CPU count is an input, so ``os.cpu_count`` is stubbed.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from automation.benchmark import evalrunner
from automation.benchmark.evalrunner import ProgramBenchEvalRunner, RecordedEvalRunner
from automation.tests.benchmark_fixtures import RECORDER


def make_recorder(directory: Path, name: str, code: int = 0, shebang: bool = False) -> tuple[Path, Path]:
    """Write a fake CLI that appends its argv to ``<name>.json`` and exits with ``code``."""
    out = directory / f"{name}.json"
    script = directory / name
    body = RECORDER.format(out=str(out), code=code)
    if not shebang:
        script.write_text(body, encoding="utf-8")
        return script, out
    # An executable found on PATH: a /bin/sh wrapper, so an interpreter path with spaces or
    # beyond the kernel's shebang limit still runs.
    (directory / f"{name}.py").write_text(body, encoding="utf-8")
    script.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(directory / f"{name}.py"))} "$@"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, out


def recorded_calls(out: Path) -> list[list[str]]:
    return json.loads(out.read_text(encoding="utf-8"))


def stub_cpu_count(value: Any) -> Any:
    return unittest.mock.patch.object(evalrunner.os, "cpu_count", return_value=value)


class DefaultDockerCpusTests(unittest.TestCase):
    def test_never_exceeds_host_or_ten_and_never_below_one(self) -> None:
        rows = [(None, 1), (0, 1), (1, 1), (2, 2), (4, 4), (9, 9), (10, 10), (11, 10), (64, 10)]
        for cpus, expected in rows:
            with self.subTest(cpu_count=cpus), stub_cpu_count(cpus):
                self.assertEqual(evalrunner.default_docker_cpus(), expected)


class ProgramBenchEvalRunnerTests(unittest.TestCase):
    def test_argv_passed_to_the_cli(self) -> None:
        # (label, constructor kwargs, argv after "eval <run_dir>"), host has 4 CPUs.
        rows: list[tuple[str, dict[str, Any], list[str]]] = [
            ("defaults", {}, ["--docker-cpus", "4"]),
            ("workers=0", {"workers": 0}, ["--workers", "0", "--docker-cpus", "4"]),
            ("workers=7", {"workers": 7}, ["--workers", "7", "--docker-cpus", "4"]),
            ("docker_cpus=3", {"docker_cpus": 3}, ["--docker-cpus", "3"]),
            ("docker_cpus=0 falls back to the host default", {"docker_cpus": 0}, ["--docker-cpus", "4"]),
            ("extra_args last, in order", {"extra_args": ("--x", "y")}, ["--docker-cpus", "4", "--x", "y"]),
            (
                "everything",
                {"workers": 2, "docker_cpus": 8, "extra_args": ["--z", "--a", "1"]},
                ["--workers", "2", "--docker-cpus", "8", "--z", "--a", "1"],
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp, stub_cpu_count(4):
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            for n, (label, kwargs, tail) in enumerate(rows):
                with self.subTest(label):
                    script, out = make_recorder(root, f"rec{n}")
                    ProgramBenchEvalRunner((sys.executable, str(script)), **kwargs).evaluate(run_dir)
                    self.assertEqual(recorded_calls(out), [["eval", str(run_dir), *tail]])

    def test_extra_command_words_precede_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, stub_cpu_count(4):
            root = Path(tmp)
            script, out = make_recorder(root, "rec")
            ProgramBenchEvalRunner([sys.executable, str(script), "programbench"]).evaluate(root)
            self.assertEqual(recorded_calls(out), [["programbench", "eval", str(root), "--docker-cpus", "4"]])

    def test_non_zero_exit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, stub_cpu_count(4):
            root = Path(tmp)
            script, out = make_recorder(root, "bad", code=3)
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                ProgramBenchEvalRunner((sys.executable, str(script))).evaluate(root)
            self.assertEqual(caught.exception.returncode, 3)
            self.assertEqual(recorded_calls(out), [["eval", str(root), "--docker-cpus", "4"]])

    def test_default_command_is_uvx_programbench(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, stub_cpu_count(4):
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            _, out = make_recorder(bindir, "uvx", shebang=True)
            with unittest.mock.patch.dict(os.environ, {"PATH": str(bindir)}):
                ProgramBenchEvalRunner().evaluate(root)
            self.assertEqual(recorded_calls(out), [["programbench", "eval", str(root), "--docker-cpus", "4"]])


def make_run_dir(root: Path, submitted: list[str], unsubmitted: list[str], files: list[str]) -> Path:
    run_dir = root / "run"
    run_dir.mkdir()
    for iid in submitted:
        (run_dir / iid).mkdir()
        (run_dir / iid / "submission.tar.gz").write_bytes(b"")
    for iid in unsubmitted:
        (run_dir / iid).mkdir()
    for name in files:
        (run_dir / name).write_text("r", encoding="utf-8")
    return run_dir


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class RecordedEvalRunnerTests(unittest.TestCase):
    def test_writes_eval_json_only_for_submitted_instances(self) -> None:
        data = {"test_results": [{"name": "t", "status": "passed"}]}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = make_run_dir(Path(tmp), submitted=["x"], unsubmitted=["nosub"], files=["root.txt"])
            RecordedEvalRunner.from_mapping({"x": data}).evaluate(run_dir)
            self.assertEqual(tree(run_dir), ["nosub", "root.txt", "x", "x/submission.tar.gz", "x/x.eval.json"])
            self.assertEqual(
                (run_dir / "x" / "x.eval.json").read_text(encoding="utf-8"),
                '{\n  "test_results": [\n    {\n      "name": "t",\n      "status": "passed"\n    }\n  ]\n}\n',
            )

    def test_writes_each_submitted_instances_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = make_run_dir(Path(tmp), submitted=["c", "a", "e"], unsubmitted=["b", "d"], files=["a.eval.json", "z.txt"])
            RecordedEvalRunner(lambda iid: {"instance": iid}).evaluate(run_dir)
            written = {p.relative_to(run_dir).as_posix(): p.read_text(encoding="utf-8") for p in run_dir.glob("*/*.eval.json")}
        self.assertEqual(
            written,
            {f"{iid}/{iid}.eval.json": f'{{\n  "instance": "{iid}"\n}}\n' for iid in ("a", "c", "e")},
        )

    def test_missing_instance_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = make_run_dir(Path(tmp), submitted=["x"], unsubmitted=[], files=[])
            with self.assertRaises(KeyError) as caught:
                RecordedEvalRunner.from_mapping({"other": {}}).evaluate(run_dir)
            self.assertEqual(caught.exception.args, ("x",))
            self.assertEqual(tree(run_dir), ["x", "x/submission.tar.gz"])

    def test_run_dir_without_submissions_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = make_run_dir(Path(tmp), submitted=[], unsubmitted=["a"], files=["b.txt"])
            RecordedEvalRunner.from_mapping({}).evaluate(run_dir)
            self.assertEqual(tree(run_dir), ["a", "b.txt"])


if __name__ == "__main__":
    unittest.main()
