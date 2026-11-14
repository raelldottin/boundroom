"""Benchmark telemetry: the same settings for every lane, and nothing the agent sees (SPEC-006 RA-TEL-005).

Every lane runs against the fake agent from ``test_benchmark_lanes``, never a model or a
provider. Covers the span tree the spec draws, the digests that join it to the cell records,
identical settings in every cell's run.json, byte-identical prompts, context files, commands,
environments and workspaces with telemetry on and off, ``environment: benchmark``, and off by
default unless this run's own command line asks.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest import mock

from automation.benchmark import lanes as lanes_module
from automation.benchmark import run as run_module
from automation.benchmark import tracing
from automation.benchmark.adapter import SupervisorAgentAdapter
from automation.benchmark.jspace import JSpaceUnavailable
from automation.benchmark.strategies import RpiStrategy, build_strategy
from automation.supervisor import telemetry
from automation.tests.test_benchmark_lanes import FIXTURE_ARTIFACT, TASK, FakeAgent

REPO_ROOT = Path(__file__).resolve().parents[2]
LANES = ("A", "B", "C", "D", "E")


def strategy_for(lane: str) -> Any:
    # Lane E's own factory resolves the canonical J-Space checkout; the fixture stands in for it,
    # as it does in test_benchmark_lanes.
    return RpiStrategy(compaction=True, jspace=FIXTURE_ARTIFACT) if lane == "E" else build_strategy(lane)


class RecordingAgent(FakeAgent):
    """The fake agent, also keeping every byte a session was handed, with temporary paths named.

    Each run gets fresh temporary directories, so their names are replaced with placeholders
    before two runs are compared; nothing else is.
    """

    def __init__(self) -> None:
        super().__init__()
        self.handed: list[dict[str, Any]] = []

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        argv = shlex.split(command)
        options = dict(zip(argv, argv[1:]))
        control = Path(options["--prompt-file"]).parent.parent
        sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).parent

        def named(text: str) -> str:
            return text.replace(str(workspace), "<workspace>").replace(str(control), "<control>").replace(str(sessions), "<out>")

        self.handed.append(
            {
                "prompt": named(Path(options["--prompt-file"]).read_text(encoding="utf-8")),
                "context": named(Path(options["--context-file"]).read_text(encoding="utf-8")),
                "command": named(command),
                "env": {name: named(value) for name, value in env.items()},
                "timeout": timeout,
                "workspace": tree(workspace),
            }
        )
        return super().__call__(command, workspace, env, timeout)


def tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` and its bytes. ``.git`` is the empty repository run_agent.sh needs."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def submission(tar_path: Path) -> dict[str, bytes]:
    """What ProgramBench grades: each member's name and contents, not its timestamp."""
    contents: dict[str, bytes] = {}
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            handle = tar.extractfile(member) if member.isfile() else None
            if handle is not None:
                contents[member.name] = handle.read()
    return contents


class TracedCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-benchmark-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.spans_dir = self.tmp / "spans"
        self.exported: list[Any] = []

    def traced(self, run_id: str = "exp-1") -> telemetry.Telemetry:
        """A file-mode handle that also keeps the exported spans, for their resource."""
        real = telemetry.json_lines_exporter
        exported = self.exported

        def keeping(path: Path, **options: Any) -> Any:
            exporter = real(path, **options)
            write = exporter.export

            def export(spans: Any) -> Any:
                exported.extend(spans)
                return write(spans)

            exporter.export = export
            return exporter

        settings = tracing.benchmark_settings({"telemetry": "file", "telemetry_file_dir": str(self.spans_dir)}, {})
        with mock.patch.object(telemetry, "json_lines_exporter", keeping):
            handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id=run_id)
        self.assertTrue(handle.enabled, handle.messages)
        return handle

    def spans(self, run_id: str = "exp-1") -> list[dict[str, Any]]:
        lines = (self.spans_dir / f"{run_id}.spans.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def experiment(self, handle: Optional[telemetry.Telemetry], lanes: tuple[str, ...], skip: tuple[str, ...] = ()) -> Path:
        def factory(lane: str) -> SupervisorAgentAdapter:
            if lane in skip:
                raise JSpaceUnavailable("jspace_unavailable: set JSPACE_ROOT to a checkout")
            return SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=FakeAgent(), strategy=strategy_for(lane), telemetry=handle)

        run_dir = self.tmp / "run"
        lanes_module.run_experiment(run_dir, ["inst-a"], factory, REPO_ROOT, lanes=lanes, repeats=1, telemetry=handle)
        if handle is not None:
            self.assertTrue(handle.flush())
        return run_dir


class SpanTreeTests(TracedCase):
    def test_the_experiment_is_one_tree_of_cells_phases_sessions_and_scores(self) -> None:
        run_dir = self.experiment(self.traced(), ("A", "C", "D"))
        records = self.spans()
        by_id = {record["span_id"]: record for record in records}

        def children(parent: dict[str, Any], name: str) -> list[dict[str, Any]]:
            found = [r for r in records if r["parent_span_id"] == parent["span_id"] and r["name"] == name]
            return sorted(found, key=lambda r: r["start_time_unix_nano"])

        (root,) = [r for r in records if r["parent_span_id"] is None]
        self.assertEqual("boundroom.benchmark.experiment", root["name"])
        self.assertEqual({root["trace_id"]}, {r["trace_id"] for r in records})
        self.assertEqual(["A", "C", "D"], json.loads(root["attributes"]["lanes"]))
        self.assertEqual((1, 1), (root["attributes"]["repeats"], root["attributes"]["instance_count"]))
        self.assertEqual(str(run_dir), root["attributes"]["run_dir"])
        self.assertEqual(40, len(root["attributes"]["repo_sha"]))
        # Every span is a cell, phase, session or score, and nothing hangs off anything else.
        allowed_parent = {
            "boundroom.benchmark.cell": "boundroom.benchmark.experiment",
            "boundroom.benchmark.score": "boundroom.benchmark.experiment",
            "boundroom.benchmark.phase": "boundroom.benchmark.cell",
            "boundroom.agent_session": "boundroom.benchmark.phase",
        }
        for record in records:
            if record is not root:
                self.assertEqual(allowed_parent[record["name"]], by_id[record["parent_span_id"]]["name"], record["name"])

        expected_phases = {
            "A": ["implement"],
            "C": ["research", "plan", "implement"],
            "D": ["research", "research_compact", "plan", "plan_compact", "implement"],
        }
        cells = {cell["attributes"]["lane"]: cell for cell in children(root, "boundroom.benchmark.cell")}
        self.assertEqual(set(expected_phases), set(cells))
        for lane, cell in cells.items():
            instance_dir = run_dir / lane / "r1" / "inst-a"
            attributes = cell["attributes"]
            with self.subTest(lane=lane):
                self.assertEqual(
                    (1, "inst-a", "valid", False),
                    (attributes["repeat"], attributes["instance"], attributes["provider_validity"], attributes["skipped"]),
                )
                self.assertEqual(sha256(instance_dir / "run.json"), attributes["run_json_sha256"])
                self.assertEqual(sha256(instance_dir / "submission.tar.gz"), attributes["submission_sha256"])
                phases = children(cell, "boundroom.benchmark.phase")
                self.assertEqual(expected_phases[lane], [phase["attributes"]["phase"] for phase in phases])
                for phase in phases:
                    (session,) = children(phase, "boundroom.agent_session")
                    self.assertEqual(0, session["attributes"]["exit_code"])
                    self.assertEqual(phase["attributes"]["budget_seconds"], session["attributes"]["budget_seconds"])
                    self.assertTrue(session["attributes"]["slice_id"].endswith(":" + phase["attributes"]["phase"]))
                    artifact = tracing_artifact(phase["attributes"]["phase"])
                    if artifact is None:
                        self.assertNotIn("artifact_sha256", phase["attributes"])
                    else:
                        # The digest joins the span to the artifact kept beside the submission.
                        self.assertEqual(sha256(instance_dir / "rpi" / artifact), phase["attributes"]["artifact_sha256"])

        scores = {score["attributes"]["lane"]: score for score in children(root, "boundroom.benchmark.score")}
        self.assertEqual(set(expected_phases), set(scores))
        for lane, score in scores.items():
            with self.subTest(score=lane):
                self.assertEqual(
                    sha256(run_dir / lane / "r1" / "effectiveness-report.json"), score["attributes"]["report_sha256"]
                )
                self.assertEqual("valid", score["attributes"]["treatment_validity"])

    def test_a_session_that_times_out_fails_its_span_with_a_reason_code(self) -> None:
        handle = self.traced()

        def factory(lane: str) -> SupervisorAgentAdapter:
            return SupervisorAgentAdapter(
                repo_root=REPO_ROOT, runner=FakeAgent(returncode=124), strategy=strategy_for(lane), telemetry=handle
            )

        lanes_module.run_experiment(self.tmp / "run", ["inst-a"], factory, REPO_ROOT, lanes=("A",), repeats=1, telemetry=handle)
        self.assertTrue(handle.flush())
        sessions = [record for record in self.spans() if record["name"] == "boundroom.agent_session"]
        self.assertTrue(sessions)
        self.assertEqual({("ERROR", "agent_timeout")}, {(s["status"], s["status_description"]) for s in sessions})

        lines = (self.spans_dir / "exp-1.metrics.jsonl").read_text(encoding="utf-8").splitlines()
        points = [json.loads(line) for line in lines]
        durations = [p for p in points if p["name"] == "boundroom.agent_session.duration"]
        self.assertEqual({("benchmark", "timeout")}, {(p["attributes"]["entry"], p["attributes"]["outcome"]) for p in durations})
        (runs,) = [p for p in points if p["name"] == "boundroom.runs"]
        self.assertEqual({"entry": "benchmark", "outcome": "ok"}, runs["attributes"])

    def test_benchmark_spans_carry_the_benchmark_environment(self) -> None:
        self.experiment(self.traced(), ("A",))
        self.assertTrue(self.exported)
        self.assertEqual({"benchmark"}, {span.resource.attributes["deployment.environment.name"] for span in self.exported})

    def test_a_skipped_cell_is_traced_as_skipped(self) -> None:
        self.experiment(self.traced(), ("D", "E"), skip=("E",))
        cells = {r["attributes"]["lane"]: r for r in self.spans() if r["name"] == "boundroom.benchmark.cell"}
        self.assertTrue(cells["E"]["attributes"]["skipped"])
        self.assertFalse([r for r in self.spans() if r["parent_span_id"] == cells["E"]["span_id"]])


def tracing_artifact(phase: str) -> Optional[str]:
    from automation.benchmark.strategies import PHASE_ARTIFACTS

    return PHASE_ARTIFACTS.get(phase)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LaneInvarianceTests(TracedCase):
    def test_every_cell_records_the_same_settings_in_its_run_json(self) -> None:
        handle = self.traced()
        run_dir = self.experiment(handle, LANES[:4], skip=())
        records = [json.loads((run_dir / lane / "r1" / "inst-a" / "run.json").read_text(encoding="utf-8")) for lane in LANES[:4]]
        (settings,) = {json.dumps(record["telemetry"], sort_keys=True) for record in records}
        (root,) = [r for r in self.spans() if r["parent_span_id"] is None]
        self.assertEqual(handle.settings.report(trace_id=root["trace_id"]), json.loads(settings))
        self.assertEqual(
            ("file", "benchmark", "off"),
            (records[0]["telemetry"]["mode"], records[0]["telemetry"]["environment"], records[0]["telemetry"]["runner"]),
        )

    def test_an_untraced_experiment_records_off_in_every_cell_including_a_skipped_one(self) -> None:
        run_dir = self.experiment(None, ("A", "E"), skip=("E",))
        records = [json.loads((run_dir / lane / "r1" / "inst-a" / "run.json").read_text(encoding="utf-8")) for lane in ("A", "E")]
        self.assertTrue(records[1]["skipped"])
        self.assertEqual(records[0]["telemetry"], records[1]["telemetry"])
        self.assertEqual(
            {"mode": "off", "trace_id": None, "environment": "benchmark"},
            {key: records[0]["telemetry"][key] for key in ("mode", "trace_id", "environment")},
        )
        self.assertFalse(self.spans_dir.exists())

    def test_the_agent_is_handed_the_same_bytes_with_telemetry_on_and_off(self) -> None:
        """Prompt, context file, command, environment and workspace, per session, and the submission."""

        def attempt(
            lane: str, handle: Optional[telemetry.Telemetry]
        ) -> tuple[list[dict[str, Any]], dict[str, bytes], dict[str, bytes], dict[str, bytes]]:
            agent = RecordingAgent()
            adapter = SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=agent, strategy=strategy_for(lane), telemetry=handle)
            out = Path(tempfile.mkdtemp(prefix=f"invariance-{lane}-", dir=self.tmp))
            result = adapter.produce_submission(TASK, out / "submission.tar.gz")
            phase_artifacts = tree(out / "rpi") if (out / "rpi").is_dir() else {}
            return agent.handed, tree(result.workspace), submission(out / "submission.tar.gz"), phase_artifacts

        handle = self.traced()
        for lane in LANES:
            with self.subTest(lane=lane):
                untraced = attempt(lane, None)
                traced = attempt(lane, handle)
                self.assertTrue(untraced[0], "the fake agent was never called")
                self.assertEqual(untraced, traced)
        self.assertTrue(handle.flush())
        # The traced runs really were traced: one session span per session the agent saw.
        sessions = [r for r in self.spans() if r["name"] == "boundroom.agent_session"]
        self.assertEqual(1 + 1 + 3 + 5 + 5, len(sessions))


class OffByDefaultTests(unittest.TestCase):
    def test_a_live_runs_settings_never_turn_benchmark_telemetry_on(self) -> None:
        live = {
            "BOUNDROOM_TELEMETRY": "file",
            "BOUNDROOM_TELEMETRY_FILE_DIR": "/var/tmp/live-spans",
            "BOUNDROOM_TELEMETRY_ENDPOINT": "https://collector.example.test",
            "BOUNDROOM_TELEMETRY_REQUIRED": "true",
        }
        args = run_module.build_parser().parse_args(["lanes", "--run-dir", "/var/tmp/run"])
        settings = tracing.benchmark_settings(vars(args), live)
        self.assertEqual(("off", "default", None), (settings.mode, settings.sources["mode"], settings.refusal))
        self.assertEqual("benchmark", settings.environment)

    def test_the_command_line_turns_it_on_and_the_kill_switch_still_wins(self) -> None:
        argv = ["lanes", "--run-dir", "/var/tmp/run", "--telemetry", "file", "--telemetry-file-dir", "/var/tmp/spans"]
        args = run_module.build_parser().parse_args(argv)
        on = tracing.benchmark_settings(vars(args), {})
        self.assertEqual(("file", "flag", "benchmark", "off"), (on.mode, on.sources["mode"], on.environment, on.runner))
        self.assertEqual("off", tracing.benchmark_settings(vars(args), {"BOUNDROOM_TELEMETRY": "off"}).mode)
        killed = run_module.build_parser().parse_args([*argv, "--no-telemetry"])
        self.assertEqual("off", tracing.benchmark_settings(vars(killed), {}).mode)

    def test_runner_telemetry_is_refused_before_any_cell_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lanes_module, "run_experiment") as experiment:
            argv = [
                "lanes",
                "--run-dir",
                tmp,
                "--telemetry",
                "file",
                "--telemetry-file-dir",
                tmp,
                "--telemetry-runner",
                "metadata",
            ]
            self.assertEqual(run_module.EX_CONFIG, run_module.main(argv))
        experiment.assert_not_called()

    def test_untraced_imports_no_sdk_and_writes_no_spans(self) -> None:
        code = """
import json, sys
from pathlib import Path
from automation.benchmark import lanes
from automation.benchmark.adapter import SupervisorAgentAdapter
from automation.benchmark.strategies import build_strategy
from automation.tests.test_benchmark_lanes import FakeAgent
repo, run_dir = Path(sys.argv[1]), Path(sys.argv[2])
factory = lambda lane: SupervisorAgentAdapter(repo_root=repo, runner=FakeAgent(), strategy=build_strategy(lane))
lanes.run_experiment(run_dir, ["inst-a"], factory, repo, lanes=["A", "C"], repeats=1)
loaded = [m for m in sys.modules if m.startswith(("logfire", "opentelemetry"))]
assert not loaded, loaded
print(json.loads((run_dir / "C/r1/inst-a/run.json").read_text())["telemetry"]["mode"])
"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            result = subprocess.run(
                [sys.executable, "-c", code, str(REPO_ROOT), str(run_dir)],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            )
            self.assertEqual("off", result.stdout.strip(), result.stderr)
            self.assertEqual([], list(run_dir.rglob("*.spans.jsonl")))


if __name__ == "__main__":
    unittest.main()
