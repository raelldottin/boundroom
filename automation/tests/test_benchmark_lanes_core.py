"""The lane experiment's bookkeeping: provenance, aggregation, comparison, and the round loop.

Only the unmanaged edges are doubled: the agent CLI (``FakeAgent``, at the adapter's runner),
ProgramBench's Docker eval (a recording ``RecordedEvalRunner``), and the judge's agent session
(a runner that writes the verdict). The clock, the environment and the checkout's commit are
handed in through ``run_cell``/``skip_cell``/``run_experiment``'s own keyword arguments, so
every timestamp, fingerprint and sha is asserted exactly. ``StopGate`` and ``Judge`` are the
real ones.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from automation.benchmark import judge as judge_module
from automation.benchmark import lanes as lanes_module
from automation.benchmark import tracing
from automation.benchmark.adapter import SupervisorAgentAdapter
from automation.benchmark.evalrunner import RecordedEvalRunner
from automation.benchmark.jspace import JSpaceUnavailable
from automation.benchmark.lanes import Cell
from automation.benchmark.scoring import EffectivenessReport, InstanceScore
from automation.benchmark.strategies import TREATMENT_INVALID, TREATMENT_VALID, build_strategy
from automation.supervisor import telemetry as telemetry_module
from automation.tests.test_benchmark_lanes import FIXTURE_ARTIFACT, REPO_ROOT, FakeAgent, TempDirCase

T0 = datetime(2026, 11, 26, 9, 30, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=75, microseconds=500000)

# A listed key the test process does not have, a non-ASCII value, and a key that is not listed.
ENVIRON = {
    "HERMES_REVISION": "fixture-rev",
    "HERMES_INFERENCE_MODEL": "modèle/κ-3",
    "UNLISTED_VARIABLE": "never recorded",
}
FINGERPRINT = {"HERMES_INFERENCE_MODEL": "modèle/κ-3", "HERMES_REVISION": "fixture-rev"}

UNTRACED = tracing.untraced().settings.report()

RUN_CELL_KEYS = [
    "lane",
    "instance_id",
    "repeat",
    "repo_automation_sha",
    "environment",
    "started_at",
    "finished_at",
    "returncode",
    "agent_sessions",
    "session_diagnoses",
    "provider_validity",
    "treatment_validity",
    "cleanroom",
    "strategy",
    "telemetry",
]

PASSING_EVAL = {"test_results": [{"name": "t1", "status": "passed", "branch": "main"}], "error_code": None}
FAILING_EVAL = {"test_results": [{"name": "t1", "status": "failed", "branch": "main"}], "error_code": None}
GOOD_VERDICT = {"score": 9, "good_enough": True, "reasons": ["handles every input the task names"]}


def json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def adapter_for(lane: str, agent: Optional[FakeAgent] = None) -> SupervisorAgentAdapter:
    return SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=agent or FakeAgent(), strategy=build_strategy(lane))


# Inside a Git hook (this file is synced to consumers, whose pre-push hook runs the suite), Git
# exports GIT_DIR, GIT_INDEX_FILE and friends. They would point the fixture's own git calls at the
# outer repository, so those calls run without them.
_GIT_ENV = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
# The code under test inherits the environment as it is; with GIT_DIR or GIT_WORK_TREE set, its
# HEAD lookup in a fixture checkout would answer for the outer repository instead.
_GIT_REDIRECTED = any(name in os.environ for name in ("GIT_DIR", "GIT_WORK_TREE"))
needs_own_git = unittest.skipIf(_GIT_REDIRECTED, "GIT_DIR or GIT_WORK_TREE redirects every git call")


def git(path: Path, *args: str) -> str:
    """Run git in ``path`` with no inherited GIT_* variables; return its output."""
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    command = ["git", *identity, *args]
    return subprocess.run(command, cwd=path, env=_GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


def git_checkout(path: Path) -> str:
    """A one-commit Git repository at ``path``; returns its HEAD."""
    git(path, "init", "--quiet")
    git(path, "commit", "--quiet", "--allow-empty", "-m", "fixture")
    return git(path, "rev-parse", "HEAD")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------


class RunCellProvenanceTests(TempDirCase):
    def test_a_cell_records_what_ran_it_and_when_its_session_started_and_ended(self) -> None:
        agent = FakeAgent()
        run_dir = self.directory()

        record = lanes_module.run_cell(
            Cell("A", 2, "inst-a"),
            run_dir,
            adapter_for("A", agent),
            REPO_ROOT,
            now=lambda: T1 if agent.sessions else T0,
            environ=ENVIRON,
            repo_sha="0123abc",
        )

        self.assertEqual(RUN_CELL_KEYS, list(record))
        self.assertEqual(
            ("A", "inst-a", 2, "0123abc", FINGERPRINT, T0.isoformat(), T1.isoformat()),
            (
                record["lane"],
                record["instance_id"],
                record["repeat"],
                record["repo_automation_sha"],
                record["environment"],
                record["started_at"],
                record["finished_at"],
            ),
        )
        self.assertEqual("2026-11-26T09:31:15.500000+00:00", record["finished_at"])
        self.assertEqual(
            (0, [], lanes_module.VALID, TREATMENT_VALID, None, "A", UNTRACED),
            (
                record["returncode"],
                record["agent_sessions"],
                record["provider_validity"],
                record["treatment_validity"],
                record["cleanroom"],
                record["strategy"]["lane"],
                record["telemetry"],
            ),
        )
        instance_dir = run_dir / "A" / "r2" / "inst-a"
        self.assertEqual(json_bytes(record), (instance_dir / lanes_module.PROVENANCE_FILENAME).read_bytes())
        self.assertTrue((instance_dir / "submission.tar.gz").is_file())

    def test_a_cell_carries_the_experiments_telemetry_settings_as_given(self) -> None:
        settings = {"mode": "otlp", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}
        record = lanes_module.run_cell(
            Cell("A", 1, "inst-a"),
            self.directory(),
            adapter_for("A"),
            REPO_ROOT,
            settings,
            now=lambda: T0,
            environ={},
            repo_sha="s",
        )
        self.assertEqual(settings, record["telemetry"])

    def test_a_cell_skipped_earlier_can_run_on_a_rerun_and_replaces_its_record(self) -> None:
        run_dir = self.directory()
        cell = Cell("A", 1, "inst-a")
        lanes_module.skip_cell(cell, run_dir, REPO_ROOT, "earlier", now=lambda: T0, environ={}, repo_sha="first")

        again = lanes_module.run_cell(cell, run_dir, adapter_for("A"), REPO_ROOT, now=lambda: T1, environ={}, repo_sha="second")

        self.assertEqual(("second", T1.isoformat(), 0), (again["repo_automation_sha"], again["started_at"], again["returncode"]))
        self.assertNotIn("skipped", again)
        self.assertEqual(json_bytes(again), (cell.directory(run_dir) / lanes_module.PROVENANCE_FILENAME).read_bytes())

    def test_each_sessions_diagnosis_is_judged_against_the_phase_it_ran(self) -> None:
        record = lanes_module.run_cell(
            Cell("C", 1, "inst-a"),
            self.directory(),
            adapter_for("C", ControlsAgent()),
            REPO_ROOT,
            now=lambda: T0,
            environ={},
            repo_sha="s",
        )

        # The phase's own record: without it a session is described, its "terminated" null.
        def ended(artifact_valid: Optional[bool]) -> dict:
            return {"returncode": 0, "at_seconds": None, "by_budget": False, "artifact_valid": artifact_valid}

        self.assertEqual(["1-research", "2-plan", "3-implement"], [d["session"] for d in record["session_diagnoses"]])
        self.assertEqual(
            [("research", ended(True)), ("plan", ended(True)), ("implement", ended(None))],
            [(d["phase"], d["terminated"]) for d in record["session_diagnoses"]],
        )


class ControlsAgent(FakeAgent):
    """The fake agent CLI, leaving each session's controls receipt, named for its phase, where the adapter collects them."""

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        returncode = super().__call__(command, workspace, env, timeout)
        session = self.sessions[-1]
        sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
        sessions.mkdir(parents=True, exist_ok=True)
        receipt = {"runner": "hermes", "slice_id": session["options"]["--slice-id"]}
        (sessions / f"{len(self.sessions)}-{session['phase']}.controls.json").write_text(json.dumps(receipt), encoding="utf-8")
        return returncode


class DefaultAdapterFactoryTests(unittest.TestCase):
    """The factory builds one supervisor adapter per lane. Nothing here may run the real agent command."""

    def test_each_lane_gets_an_adapter_of_its_own(self) -> None:
        factory = lanes_module.default_adapter_factory(REPO_ROOT, "agent --prompt {prompt}", timeout_seconds=42)

        adapters = {lane: factory(lane) for lane in ("A", "B", "C", "D")}

        for lane, adapter in adapters.items():
            with self.subTest(lane=lane):
                assert isinstance(adapter, SupervisorAgentAdapter), adapter
                self.assertEqual(lane, adapter.lane)


class SkipCellProvenanceTests(TempDirCase):
    def test_a_skipped_cell_is_recorded_as_unrun_at_one_instant(self) -> None:
        ticks = iter([T0, T1])
        run_dir = self.directory()

        record = lanes_module.skip_cell(
            Cell("E", 1, "inst-a"),
            run_dir,
            REPO_ROOT,
            "jspace_unavailable: répertoire absent",
            now=lambda: next(ticks),
            environ=ENVIRON,
            repo_sha="0123abc",
        )

        expected = {
            "lane": "E",
            "instance_id": "inst-a",
            "repeat": 1,
            "repo_automation_sha": "0123abc",
            "environment": FINGERPRINT,
            "started_at": T0.isoformat(),
            "finished_at": T0.isoformat(),
            "returncode": None,
            "skipped": True,
            "skip_reason": "jspace_unavailable: répertoire absent",
            "strategy": None,
            "telemetry": UNTRACED,
        }
        self.assertEqual(expected, record)
        self.assertEqual(list(expected), list(record))
        instance_dir = run_dir / "E" / "r1" / "inst-a"
        self.assertEqual(json_bytes(expected), (instance_dir / lanes_module.PROVENANCE_FILENAME).read_bytes())
        self.assertFalse((instance_dir / "submission.tar.gz").exists())

    def test_a_rerun_of_a_skipped_cell_replaces_its_record(self) -> None:
        run_dir = self.directory()
        cell = Cell("E", 1, "inst-a")
        lanes_module.skip_cell(cell, run_dir, REPO_ROOT, "first", now=lambda: T0, environ={}, repo_sha="s")

        again = lanes_module.skip_cell(cell, run_dir, REPO_ROOT, "second", now=lambda: T1, environ={}, repo_sha="s")

        self.assertEqual(("second", T1.isoformat()), (again["skip_reason"], again["started_at"]))
        self.assertEqual(json_bytes(again), (cell.directory(run_dir) / lanes_module.PROVENANCE_FILENAME).read_bytes())

    @needs_own_git
    def test_without_a_sha_the_cell_records_its_checkouts_head(self) -> None:
        checkout = self.directory()
        head = git_checkout(checkout)
        record = lanes_module.skip_cell(Cell("E", 1, "inst-a"), self.directory(), checkout, "r", now=lambda: T0, environ={})
        self.assertEqual(head, record["repo_automation_sha"])
        self.assertRegex(head, r"^[0-9a-f]{40}$")

    @needs_own_git
    def test_a_checkout_that_is_not_git_records_an_unknown_sha(self) -> None:
        checkout = self.directory()
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=checkout, env=_GIT_ENV, capture_output=True, text=True
        )
        if inside.returncode == 0:
            self.skipTest("the temporary directory is inside a Git work tree, so it has a HEAD")
        record = lanes_module.skip_cell(Cell("E", 1, "inst-a"), self.directory(), checkout, "r", now=lambda: T0, environ={})
        self.assertEqual("unknown", record["repo_automation_sha"])


class EnvironmentFingerprintTests(unittest.TestCase):
    def test_only_listed_variables_are_recorded_in_list_order(self) -> None:
        fingerprint = lanes_module._environment_fingerprint(ENVIRON)
        self.assertEqual(FINGERPRINT, fingerprint)
        self.assertEqual(["HERMES_INFERENCE_MODEL", "HERMES_REVISION"], list(fingerprint))

    def test_a_key_like_name_is_redacted_whatever_its_case(self) -> None:
        environ = {"NVIDIA_API_KEY": "sk-1", "hf_token": "hf-2", "db_password": "pw", "CLIENT_SECRET": "s", "MODEL": "m"}
        fingerprint = lanes_module._environment_fingerprint(
            environ, keys=("NVIDIA_API_KEY", "hf_token", "MODEL", "ABSENT", "db_password", "CLIENT_SECRET")
        )
        self.assertEqual(
            {
                "NVIDIA_API_KEY": "<redacted>",
                "hf_token": "<redacted>",
                "MODEL": "m",
                "db_password": "<redacted>",
                "CLIENT_SECRET": "<redacted>",
            },
            fingerprint,
        )
        self.assertEqual(["NVIDIA_API_KEY", "hf_token", "MODEL", "db_password", "CLIENT_SECRET"], list(fingerprint))


class ExperimentProvenanceTests(TempDirCase):
    def test_every_cell_gets_the_experiments_clock_environment_and_sha(self) -> None:
        def factory(lane: str) -> SupervisorAgentAdapter:
            if lane == "E":
                raise JSpaceUnavailable("jspace_unavailable: set JSPACE_ROOT")
            return adapter_for(lane)

        run_dir = self.directory()
        lanes_module.run_experiment(
            run_dir,
            ["inst-a"],
            factory,
            REPO_ROOT,
            lanes=["A", "E"],
            score=False,
            now=lambda: T0,
            environ=ENVIRON,
            repo_sha="sha-x",
        )

        for lane in ("A", "E"):
            with self.subTest(lane=lane):
                record = json.loads((run_dir / lane / "r1" / "inst-a" / "run.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    ("sha-x", FINGERPRINT, T0.isoformat(), T0.isoformat()),
                    (record["repo_automation_sha"], record["environment"], record["started_at"], record["finished_at"]),
                )

    @needs_own_git
    def test_without_a_sha_every_cell_records_the_head_the_experiment_started_from(self) -> None:
        checkout = self.directory()
        head = git_checkout(checkout)
        run_dir = self.directory()

        class CommittingAgent(FakeAgent):
            """Moves the checkout on during the first session, as a commit made mid-run would."""

            def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
                if not self.sessions:
                    git(checkout, "commit", "--quiet", "--allow-empty", "-m", "made while the experiment ran")
                return super().__call__(command, workspace, env, timeout)

        lanes_module.run_experiment(
            run_dir,
            ["inst-a"],
            lambda lane: adapter_for(lane, CommittingAgent()),
            checkout,
            lanes=["A", "B"],
            repeats=2,
            score=False,
            now=lambda: T0,
            environ={},
        )

        self.assertNotEqual(head, git(checkout, "rev-parse", "HEAD"))
        recorded = {
            path.relative_to(run_dir).as_posix(): json.loads(path.read_text(encoding="utf-8"))["repo_automation_sha"]
            for path in sorted(run_dir.glob("*/r*/inst-a/run.json"))
        }
        self.assertEqual(
            {
                "A/r1/inst-a/run.json": head,
                "A/r2/inst-a/run.json": head,
                "B/r1/inst-a/run.json": head,
                "B/r2/inst-a/run.json": head,
            },
            recorded,
        )

    def test_a_traced_experiment_names_the_same_commit_on_its_span_as_in_every_cell(self) -> None:
        spans_dir = self.directory()
        settings = tracing.benchmark_settings({"telemetry": "file", "telemetry_file_dir": str(spans_dir)}, {})
        handle = telemetry_module.configure(settings, repo_root=REPO_ROOT, run_id="exp-sha")
        self.assertTrue(handle.enabled, handle.messages)
        run_dir = self.directory()

        lanes_module.run_experiment(
            run_dir, ["inst-a"], adapter_for, REPO_ROOT, lanes=["A"], score=False, telemetry=handle, environ={}, repo_sha="sha-x"
        )
        self.assertTrue(handle.flush())

        spans = [json.loads(line) for line in (spans_dir / "exp-sha.spans.jsonl").read_text(encoding="utf-8").splitlines()]
        experiment = [span for span in spans if span["name"] == "boundroom.benchmark.experiment"]
        self.assertEqual(["sha-x"], [span["attributes"]["repo_sha"] for span in experiment])
        record = json.loads((run_dir / "A" / "r1" / "inst-a" / "run.json").read_text(encoding="utf-8"))
        self.assertEqual("sha-x", record["repo_automation_sha"])


# --------------------------------------------------------------------------------------
# Provider failures
# --------------------------------------------------------------------------------------


class LoggingAgent(FakeAgent):
    """The fake agent CLI, leaving per-session controls and logs where the adapter collects them."""

    def __init__(self, logs: Mapping[str, str]) -> None:
        super().__init__()
        self.logs = dict(logs)

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
        sessions.mkdir(parents=True, exist_ok=True)
        for stem, log in self.logs.items():
            (sessions / f"{stem}.controls.json").write_text('{"runner": "hermes"}', encoding="utf-8")
            (sessions / f"{stem}.log").write_text(log, encoding="utf-8")
        return super().__call__(command, workspace, env, timeout)


class ProviderFailureEvidenceTests(TempDirCase):
    """A refusal is read from the session's own log, and the line itself is carried as evidence.

    Each test runs its own cell in ``setUp``, not once in ``setUpClass``: the mutation harness
    picks a mutant's tests from per-test coverage, and work done for the class belongs to none.
    """

    LONG = "API failed after " + "x" * 300
    LOGS = {
        "s1-first-line": "Rate limited after 3 retries — HTTP 429\nretrying is pointless\n",
        "s2-later-line": "reading the repository...\n❌ API failed after 3 retries — Connection error.\ntrailing\n",
        "s3-long-line": LONG + "\n",
        "s4-both": "API failed after 1 retry\nRate limited after 3 retries\n",
        "s5-clean": "wrote compile.sh\n",
    }

    def setUp(self) -> None:
        super().setUp()
        self.record = lanes_module.run_cell(
            Cell("A", 1, "inst-a"),
            self.directory(),
            adapter_for("A", LoggingAgent(self.LOGS)),
            REPO_ROOT,
            now=lambda: T0,
            environ={},
            repo_sha="s",
        )
        self.failures = {session["session"]: session["provider_failure"] for session in self.record["agent_sessions"]}

    def test_a_marker_at_the_very_start_of_the_log_counts(self) -> None:
        self.assertEqual(
            {"kind": "rate_limit", "evidence": "Rate limited after 3 retries — HTTP 429"}, self.failures["s1-first-line"]
        )

    def test_the_evidence_is_the_marked_line_from_the_marker_on(self) -> None:
        self.assertEqual(
            {"kind": "unavailable", "evidence": "API failed after 3 retries — Connection error."}, self.failures["s2-later-line"]
        )

    def test_a_long_line_is_cut_to_two_hundred_characters(self) -> None:
        self.assertEqual({"kind": "unavailable", "evidence": self.LONG[:200]}, self.failures["s3-long-line"])
        self.assertEqual(200, len(self.failures["s3-long-line"]["evidence"]))

    def test_a_rate_limit_outranks_an_exhausted_transport_wherever_it_appears(self) -> None:
        self.assertEqual({"kind": "rate_limit", "evidence": "Rate limited after 3 retries"}, self.failures["s4-both"])

    def test_a_clean_log_is_no_failure_and_a_cell_never_served_is_unavailable(self) -> None:
        self.assertIsNone(self.failures["s5-clean"])
        self.assertEqual(lanes_module.PROVIDER_UNAVAILABLE, self.record["provider_validity"])


class ProviderValidityTableTests(unittest.TestCase):
    def test_validity_is_read_from_refusals_and_what_was_served(self) -> None:
        refused = {"provider_failure": {"kind": "rate_limit", "evidence": "Rate limited after 3 retries"}, "usage": None}
        served = {"provider_failure": None, "usage": {"model": "m", "api_calls": 2}}
        unnamed = {"provider_failure": {"kind": "unavailable", "evidence": "x"}, "usage": {"api_calls": 2}}
        table = [
            ([], lanes_module.VALID),
            ([served], lanes_module.VALID),
            ([refused], lanes_module.PROVIDER_UNAVAILABLE),
            ([unnamed], lanes_module.PROVIDER_UNAVAILABLE),
            ([served, refused], lanes_module.PROVIDER_DEGRADED),
        ]
        for sessions, validity in table:
            with self.subTest(sessions=sessions):
                self.assertEqual(validity, lanes_module.classify_provider_validity(sessions))


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------


class SummarizeLaneTests(TempDirCase):
    def test_a_lane_summary_reduces_every_cell_of_the_requested_repeats(self) -> None:
        run_dir = self.directory()
        lane_dir = run_dir / "D"
        write_json(
            lane_dir / "r1" / "inst-a" / "run.json",
            {
                "lane": "D",
                "returncode": 0,
                "provider_validity": lanes_module.PROVIDER_DEGRADED,
                "strategy": {
                    "seconds": 1.23456,
                    "agent_invocations": 5,
                    "phase_failures": 1,
                    "compaction": {
                        "research_raw_chars": 900,
                        "research_compression_ratio": 0.33333,
                        "plan_compression_ratio": None,
                    },
                    "jspace": None,
                    "phases": [
                        {"phase": "research", "returncode": 124, "artifact_valid": True},
                        {"phase": "plan", "returncode": 0, "artifact_valid": True},
                        {"phase": "plan_compact", "returncode": 0, "artifact_valid": False},
                        {"phase": "implement", "returncode": 124},
                    ],
                },
            },
        )
        write_json(
            lane_dir / "r1" / "inst-b" / "run.json",
            {
                "lane": "D",
                "returncode": 0,
                "strategy": {
                    "seconds": 2.0,
                    "agent_invocations": 3,
                    "phase_failures": 0,
                    "compaction": {"research_compression_ratio": 0.5},
                    "jspace": FIXTURE_ARTIFACT.provenance(),
                    "phases": [{"phase": "research", "returncode": 124}],
                },
            },
        )
        (lane_dir / "r1" / "inst-c").mkdir()  # no run.json: not a cell that ran
        (lane_dir / "r1" / "effectiveness-report.json").write_text("{}", encoding="utf-8")
        write_json(lane_dir / "r2" / "inst-a" / "run.json", {"lane": "D", "returncode": None, "skipped": True, "strategy": None})
        write_json(lane_dir / "r2" / "inst-b" / "run.json", {"lane": "D", "returncode": 1, "strategy": None})
        # Outside the requested repeats: a stale round from an earlier, longer run.
        write_json(
            lane_dir / "r3" / "inst-a" / "run.json",
            {"lane": "D", "returncode": 7, "provider_validity": lanes_module.PROVIDER_UNAVAILABLE, "strategy": None},
        )
        reports = [
            EffectivenessReport(
                (
                    InstanceScore("inst-a", 7, 1, branch_pass_fractions=(0.0, 0.5)),
                    InstanceScore("inst-b", 4, 4, unique_tests=4),
                )
            ),
            EffectivenessReport((InstanceScore("inst-a", 3, 2, unique_tests=3),)),
        ]

        summary = lanes_module.summarize_lane(run_dir, "D", 2, reports)

        expected = {
            "lane": "D",
            "repeats": 2,
            "attempts": 3,
            "skipped": 1,
            "provider_validity": lanes_module.PROVIDER_DEGRADED,
            "treatment_validity": TREATMENT_INVALID,
            "jspace": FIXTURE_ARTIFACT.provenance(),
            "primary": {"resolve_rate": 0.25, "near_resolve_rate": 0.25, "mean_pass_fraction": 0.619},
            "measurement": {
                "branch_macro_pass_fraction": 0.6458,
                "executions": 14,
                "unique_tests": 7,
                "censored_phases": ["implement", "research", "research"],
                "invalid_phases": ["plan_compact"],
            },
            "efficiency": {"wall_clock_seconds": 3.235, "agent_invocations": 8, "mean_seconds_per_attempt": 1.617},
            "process": {"phase_failures": 1, "nonzero_returncodes": 1, "mean_compression_ratio": 0.4167},
            "stability": {"resolve_rate_stdev": 0.3536, "mean_pass_fraction_stdev": 0.0673},
        }
        self.assertEqual(expected, summary)
        self.assertEqual(list(expected), list(summary))

    def test_the_rates_are_rounded_to_four_places(self) -> None:
        # One of three resolved, two of three near: thirds, which four and five places tell apart.
        report = EffectivenessReport((InstanceScore("i1", 10, 10), InstanceScore("i2", 100, 99), InstanceScore("i3", 10, 0)))

        summary = lanes_module.summarize_lane(self.directory(), "C", 1, [report])

        self.assertEqual({"resolve_rate": 0.3333, "near_resolve_rate": 0.6667, "mean_pass_fraction": 0.6633}, summary["primary"])

    def test_one_report_has_no_spread_and_no_reports_score_zero(self) -> None:
        run_dir = self.directory()
        one = lanes_module.summarize_lane(run_dir, "C", 1, [EffectivenessReport((InstanceScore("i", 3, 1),))])
        self.assertEqual({"resolve_rate_stdev": 0.0, "mean_pass_fraction_stdev": 0.0}, one["stability"])

        none = lanes_module.summarize_lane(run_dir, "C", 1, [])
        self.assertEqual({"resolve_rate": 0.0, "near_resolve_rate": 0.0, "mean_pass_fraction": 0.0}, none["primary"])
        self.assertEqual({"phase_failures": 0, "nonzero_returncodes": 0, "mean_compression_ratio": None}, none["process"])
        self.assertEqual(
            (0, 0, lanes_module.VALID, TREATMENT_VALID, None),
            (none["attempts"], none["skipped"], none["provider_validity"], none["treatment_validity"], none["jspace"]),
        )

    def test_a_cell_recorded_as_not_administered_invalidates_the_lane_even_with_valid_phases(self) -> None:
        run_dir = self.directory()
        write_json(
            run_dir / "C" / "r1" / "inst-a" / "run.json",
            {
                "lane": "C",
                "returncode": 0,
                "treatment_validity": TREATMENT_INVALID,
                "strategy": {
                    "seconds": 1.0,
                    "agent_invocations": 1,
                    "phase_failures": 0,
                    "compaction": {},
                    "phases": [{"phase": "research", "returncode": 0, "artifact_valid": True}],
                },
            },
        )
        summary = lanes_module.summarize_lane(run_dir, "C", 1, [])
        self.assertEqual(TREATMENT_INVALID, summary["treatment_validity"])
        self.assertEqual([], summary["measurement"]["invalid_phases"])


def lane_summary(lane: str, resolve: float, pass_fraction: float, macro: float, calls: int, wall: float, **extra: Any) -> dict:
    return {
        "lane": lane,
        "primary": {"resolve_rate": resolve, "near_resolve_rate": resolve / 2, "mean_pass_fraction": pass_fraction},
        "measurement": {"branch_macro_pass_fraction": macro},
        "efficiency": {"agent_invocations": calls, "wall_clock_seconds": wall},
        **extra,
    }


class LaneDeltaTests(unittest.TestCase):
    A = lane_summary("A", 0.12344, 0.5, 0.40001, 1, 100.1234)
    B = lane_summary("B", 0.23456, 0.61111, 0.5, 1, 120.5678)
    C = lane_summary("C", 0.2, 0.7, 0.55555, 3, 300.0)

    def test_each_adjacent_pair_is_subtracted_field_by_field(self) -> None:
        expected = [
            {
                "comparison": "B - A",
                "treatment": "bounded slice context (shipped harness)",
                "status": lanes_module.VALID,
                "resolve_rate": 0.1111,
                "near_resolve_rate": 0.0556,
                "mean_pass_fraction": 0.1111,
                "branch_macro_pass_fraction": 0.1,
                "agent_invocations": 0,
                "wall_clock_seconds": 20.444,
            },
            {
                "comparison": "C - B",
                "treatment": "Research -> Plan -> Implement",
                "status": lanes_module.VALID,
                "resolve_rate": -0.0346,
                "near_resolve_rate": -0.0173,
                "mean_pass_fraction": 0.0889,
                "branch_macro_pass_fraction": 0.0555,
                "agent_invocations": 2,
                "wall_clock_seconds": 179.432,
            },
        ]
        deltas = lanes_module.lane_deltas([self.A, self.B, self.C])
        self.assertEqual(expected, deltas)
        self.assertEqual(list(expected[0]), list(deltas[0]))

    def test_pairs_follow_the_lane_order_not_the_input_order(self) -> None:
        deltas = lanes_module.lane_deltas([self.C, self.B, self.A])
        self.assertEqual(["B - A", "C - B"], [delta["comparison"] for delta in deltas])
        self.assertEqual(["C - A"], [delta["comparison"] for delta in lanes_module.lane_deltas([self.C, self.A])])

    def test_a_refused_side_excludes_the_pair_before_an_unadministered_one_does(self) -> None:
        refused = {**self.B, "provider_validity": lanes_module.PROVIDER_DEGRADED, "treatment_validity": TREATMENT_INVALID}
        unadministered = {**self.C, "treatment_validity": TREATMENT_INVALID}
        deltas = lanes_module.lane_deltas([self.A, refused, unadministered])
        self.assertEqual(
            [
                {
                    "comparison": "B - A",
                    "treatment": "bounded slice context (shipped harness)",
                    "status": "unavailable",
                    "reason": "no provider-valid pair",
                },
                {
                    "comparison": "C - B",
                    "treatment": "Research -> Plan -> Implement",
                    "status": "unavailable",
                    "reason": "no provider-valid pair",
                },
            ],
            deltas,
        )
        only_unadministered = lanes_module.lane_deltas([self.B, unadministered])
        self.assertEqual(
            ["required phase artifact missing: the treatment was not administered"],
            [delta["reason"] for delta in only_unadministered],
        )

    def test_validity_left_unrecorded_counts_as_valid(self) -> None:
        explicit = {**self.A, "provider_validity": lanes_module.VALID, "treatment_validity": TREATMENT_VALID}
        self.assertEqual(lanes_module.lane_deltas([self.A, self.B]), lanes_module.lane_deltas([explicit, self.B]))
        self.assertEqual(lanes_module.VALID, lanes_module.lane_deltas([self.A, self.B])[0]["status"])


# --------------------------------------------------------------------------------------
# Comparison rendering and writing
# --------------------------------------------------------------------------------------


def full_comparison() -> dict:
    """Every section of the report filled in at least once."""

    def lane(name: str, **extra: Any) -> dict:
        base = {
            "lane": name,
            "skipped": 0,
            "provider_validity": lanes_module.VALID,
            "treatment_validity": TREATMENT_VALID,
            "jspace": None,
            "primary": {"resolve_rate": 0.25, "near_resolve_rate": 0.5, "mean_pass_fraction": 0.6789},
            "measurement": {
                "branch_macro_pass_fraction": 0.7012,
                "executions": 14,
                "censored_phases": [],
                "invalid_phases": [],
            },
            "efficiency": {"agent_invocations": 3, "wall_clock_seconds": 123.6},
            "stability": {"resolve_rate_stdev": 0.12345},
            "process": {"phase_failures": 0, "nonzero_returncodes": 0},
        }
        base.update(extra)
        return base

    lanes = [
        lane("A"),
        lane(
            "B",
            process={"phase_failures": 1, "nonzero_returncodes": 0},
            measurement={
                "branch_macro_pass_fraction": 0.5,
                "executions": 9,
                "censored_phases": ["research", "implement", "research"],
                "invalid_phases": [],
            },
        ),
        lane("C", provider_validity=lanes_module.PROVIDER_DEGRADED),
        lane(
            "D",
            treatment_validity=TREATMENT_INVALID,
            measurement={
                "branch_macro_pass_fraction": 0.1,
                "executions": 2,
                "censored_phases": [],
                "invalid_phases": ["plan", "research"],
            },
        ),
        lane("E", skipped=2, jspace=FIXTURE_ARTIFACT.provenance()),
    ]
    deltas = [
        {
            "comparison": "B - A",
            "treatment": "bounded slice context (shipped harness)",
            "status": "valid",
            "resolve_rate": 0.1,
            "near_resolve_rate": -0.05,
            "mean_pass_fraction": 0.0234,
            "branch_macro_pass_fraction": -0.2012,
            "agent_invocations": 0,
            "wall_clock_seconds": -12.6,
        },
        {
            "comparison": "C - B",
            "treatment": "Research -> Plan -> Implement",
            "status": "unavailable",
            "reason": "no provider-valid pair",
        },
    ]
    stop_gate = {
        "settings": {"min_repeats": 2, "pass_fraction": 0.95, "min_score": 7},
        "ceiling": 3,
        "repeats_run": 2,
        "stopped_by": "quality_reached",
        "rounds": [
            {"round": 1, "decision": "continue", "reason": "below_min_repeats"},
            {"round": 2, "decision": "stop", "reason": "quality_reached"},
        ],
    }
    return {"lanes": lanes, "deltas": deltas, "note": "Primary metric is ProgramBench correctness.", "stop_gate": stop_gate}


FULL_MARKDOWN = (
    "# Lane comparison\n"
    "\n"
    "## Primary outcome (ProgramBench)\n"
    "\n"
    "| lane | treatment | provider | administered | resolve | near | mean pass | macro pass | executions | censored | no artifact | stdev(resolve) | agent calls | wall s |\n"
    "|---|---|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|\n"
    "| A | one session, raw objective | valid | valid | 25.0% | 50.0% | 67.9% | 70.1% | 14 | - | - | 0.123 | 3 | 124 |\n"
    "| B | bounded slice context (shipped harness) | valid | valid | 25.0% | 50.0% | 67.9% | 50.0% | 9 | implement+research | - | 0.123 | 3 | 124 |\n"
    "| C | Research -> Plan -> Implement | provider_degraded | valid | 25.0% | 50.0% | 67.9% | 70.1% | 14 | - | - | 0.123 | 3 | 124 |\n"
    "| D | RPI + intentional compaction | valid | invalid | 25.0% | 50.0% | 67.9% | 10.0% | 2 | - | plan+research | 0.123 | 3 | 124 |\n"
    "| E | RPI + compaction + J-Space | valid | valid | 25.0% | 50.0% | 67.9% | 70.1% | 14 | - | - | 0.123 | 3 | 124 |\n"
    "\n"
    "`mean pass` is ProgramBench's own score, pooled over test branches, and stays the primary\n"
    "outcome. `macro pass` weights every branch equally over the same results: where the two\n"
    "disagree, the lane ordering depends on how often each branch happened to run rather than\n"
    "on the submission. `censored` names the phases killed at their ceiling - a censored lane\n"
    "reports its budget as much as its treatment. `no artifact` names the phases that owed\n"
    "the next one a typed artifact and produced none: those lanes ran something, but not the\n"
    "treatment their name claims, so `administered` reads invalid and they leave the deltas.\n"
    "\n"
    "## Provider validity\n"
    "\n"
    "The provider refused to serve part or all of these lanes. Their scores are kept as\n"
    "diagnostics and excluded from the deltas below: a rate limit is not a treatment.\n"
    "\n"
    "- C: provider_degraded - diagnostic score 67.9%\n"
    "\n"
    "## Treatment validity\n"
    "\n"
    "A required phase artifact never arrived in these lanes, so the phases after it had\n"
    "no handoff to work from and the lane stopped. Their scores are kept as diagnostics\n"
    "and excluded from the deltas below: this is an unrun treatment, not a weak one.\n"
    "\n"
    "- D: no artifact from plan+research - diagnostic score 67.9%\n"
    "\n"
    "## Marginal contribution of each treatment\n"
    "\n"
    "| comparison | treatment added | resolve | near | mean pass | macro pass | agent calls | wall s |\n"
    "|---|---|---:|---:|---:|---:|---:|---:|\n"
    "| B - A | bounded slice context (shipped harness) | +10.0% | -5.0% | +2.3% | -20.1% | +0 | -13 |\n"
    "\n"
    "Comparisons excluded:\n"
    "\n"
    "- C - B: unavailable - reason: no provider-valid pair\n"
    "\n"
    "## Process failures\n"
    "\n"
    "| lane | phase failures | nonzero exits |\n"
    "|---|---:|---:|\n"
    "| A | 0 | 0 |\n"
    "| B | 1 | 0 |\n"
    "| C | 0 | 0 |\n"
    "| D | 0 | 0 |\n"
    "| E | 0 | 0 |\n"
    "\n"
    "## Unrun cells\n"
    "\n"
    "A skipped cell was never administered, so its lane's rates are computed over fewer\n"
    "attempts. Read a skipped lane as unrun, not as a treatment that underperformed.\n"
    "\n"
    "| lane | skipped cells |\n"
    "|---|---:|\n"
    "| E | 2 |\n"
    "\n"
    "## J-Space artifact administered\n"
    "\n"
    "- lane E: `fixture/j-space` at `b2023124a1fa08278e3ee82aefa8ed9faeade995`, `j-space/SKILL.md` sha256 `0000000000000000000000000000000000000000000000000000000000000000`\n"
    "\n"
    "## Stop gate\n"
    "\n"
    "Ran 2 of at most 3 repeats; stopped by quality_reached. A round stops the run when every cell passes 95.0% of ProgramBench's tests and the judge scores it at least 7, from round 2 on.\n"
    "\n"
    "| round | decision | reason |\n"
    "|---:|---|---|\n"
    "| 1 | continue | below_min_repeats |\n"
    "| 2 | stop | quality_reached |\n"
    "\n"
    "Primary metric is ProgramBench correctness.\n"
)

MINIMAL_MARKDOWN = (
    "# Lane comparison\n"
    "\n"
    "## Primary outcome (ProgramBench)\n"
    "\n"
    "| lane | treatment | provider | administered | resolve | near | mean pass | macro pass | executions | censored | no artifact | stdev(resolve) | agent calls | wall s |\n"
    "|---|---|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|\n"
    "| A | one session, raw objective | valid | valid | 0.0% | 0.0% | 0.0% | 0.0% | 0 | - | - | 0.000 | 1 | 9 |\n"
    "\n"
    "`mean pass` is ProgramBench's own score, pooled over test branches, and stays the primary\n"
    "outcome. `macro pass` weights every branch equally over the same results: where the two\n"
    "disagree, the lane ordering depends on how often each branch happened to run rather than\n"
    "on the submission. `censored` names the phases killed at their ceiling - a censored lane\n"
    "reports its budget as much as its treatment. `no artifact` names the phases that owed\n"
    "the next one a typed artifact and produced none: those lanes ran something, but not the\n"
    "treatment their name claims, so `administered` reads invalid and they leave the deltas.\n"
    "\n"
    "## Marginal contribution of each treatment\n"
    "\n"
    "No comparison had a valid pair on both sides; this run measures nothing.\n"
    "\n"
    "Primary metric is ProgramBench correctness.\n"
)


class RenderComparisonTests(unittest.TestCase):
    maxDiff = None

    def test_every_section_renders_exactly(self) -> None:
        self.assertEqual(FULL_MARKDOWN, lanes_module.render_comparison_markdown(full_comparison()))

    def test_a_run_with_nothing_to_compare_says_it_measures_nothing(self) -> None:
        comparison = {
            "lanes": [
                {
                    "lane": "A",
                    "primary": {"resolve_rate": 0.0, "near_resolve_rate": 0.0, "mean_pass_fraction": 0.0},
                    "efficiency": {"agent_invocations": 1, "wall_clock_seconds": 9.4},
                    "stability": {"resolve_rate_stdev": 0.0},
                    "process": {"phase_failures": 0, "nonzero_returncodes": 0},
                }
            ],
            "deltas": [],
            "note": "Primary metric is ProgramBench correctness.",
        }
        self.assertEqual(MINIMAL_MARKDOWN, lanes_module.render_comparison_markdown(comparison))


class WriteComparisonTests(TempDirCase):
    def test_the_comparison_is_written_as_json_and_markdown_into_a_new_directory(self) -> None:
        comparison = full_comparison()
        comparison["note"] = "Mesure principale : la justesse ✓"
        run_dir = self.directory() / "not" / "yet" / "there"

        path = lanes_module.write_comparison(comparison, run_dir)

        self.assertEqual(run_dir / lanes_module.COMPARISON_MARKDOWN, path)
        self.assertEqual(json_bytes(comparison), (run_dir / lanes_module.COMPARISON_FILENAME).read_bytes())
        self.assertEqual(lanes_module.render_comparison_markdown(comparison), path.read_text(encoding="utf-8"))


class BuildComparisonTests(TempDirCase):
    def test_lanes_with_a_directory_are_summarized_and_lanes_without_are_left_out(self) -> None:
        run_dir = self.directory()
        write_json(run_dir / "A" / "r1" / "inst-a" / "run.json", {"lane": "A", "returncode": 0, "strategy": None})
        write_json(run_dir / "A" / "r1" / "inst-a" / "inst-a.eval.json", PASSING_EVAL)
        (run_dir / "B" / "r9").mkdir(parents=True)  # a lane directory, but no repeat in range

        comparison = lanes_module.build_comparison(run_dir, ["A", "B", "C"], 1)

        self.assertEqual(["A", "B"], [summary["lane"] for summary in comparison["lanes"]])
        self.assertEqual((1, 1.0), (comparison["lanes"][0]["repeats"], comparison["lanes"][0]["primary"]["mean_pass_fraction"]))
        self.assertEqual(0, comparison["lanes"][1]["repeats"])
        self.assertEqual(["B - A"], [delta["comparison"] for delta in comparison["deltas"]])
        self.assertEqual(["lanes", "deltas", "note"], list(comparison))
        self.assertTrue((run_dir / "A" / "r1" / "effectiveness-report.json").is_file())

    def test_a_gated_runs_round_records_are_summarized_unless_left_out(self) -> None:
        run_dir = self.directory()
        (run_dir / "A").mkdir()
        record = {
            "round": 1,
            "ceiling": 1,
            "decision": judge_module.STOP,
            "reason": "ceiling",
            "settings": {"min_repeats": 1, "pass_fraction": 0.95, "min_score": 7},
            "cells": [],
        }
        write_json(run_dir / judge_module.JUDGE_DIR / "r1" / judge_module.STOP_GATE_FILENAME, record)

        gated = lanes_module.build_comparison(run_dir, ["A"], 1)
        self.assertEqual(judge_module.summarize_rounds(run_dir, 1), gated["stop_gate"])
        self.assertEqual("ceiling", gated["stop_gate"]["stopped_by"])
        self.assertNotIn("stop_gate", lanes_module.build_comparison(run_dir, ["A"], 1, include_stop_gate=False))


class ScheduleTests(TempDirCase):
    def test_lane_order_rotates_per_instance_and_repeat(self) -> None:
        expected = [
            Cell(lane, repeat, instance)
            for repeat, instance, order in (
                (1, "i1", "ABC"),
                (1, "i2", "BCA"),
                (2, "i1", "BCA"),
                (2, "i2", "CAB"),
            )
            for lane in order
        ]
        cells = lanes_module.interleave(["i1", "i2"], ["A", "B", "C"], 2)
        self.assertEqual(expected, cells)
        self.assertEqual(12, len(set(cells)))
        self.assertEqual(Path("/runs/x/B/r2/i1"), Cell("B", 2, "i1").directory(Path("/runs/x")))

    def test_cell_dirs_lists_existing_rounds_in_range_lane_by_lane(self) -> None:
        run_dir = self.directory()
        for relative in ("A/r1", "A/r2", "A/r3", "B/r2"):
            (run_dir / relative).mkdir(parents=True)
        (run_dir / "B" / "r1").write_text("not a directory", encoding="utf-8")

        self.assertEqual([run_dir / "A/r1", run_dir / "A/r2", run_dir / "B/r2"], lanes_module.cell_dirs(run_dir, ["A", "B"], 2))
        self.assertEqual([run_dir / "A/r2", run_dir / "B/r2"], lanes_module.cell_dirs(run_dir, ["A", "B"], 2, first=2))


# --------------------------------------------------------------------------------------
# The round loop and the stop gate
# --------------------------------------------------------------------------------------


class RecordingEvalRunner:
    """ProgramBench's eval stand-in: writes the given results and records what it was asked to evaluate."""

    def __init__(self, run_dir: Path, results: dict) -> None:
        self.run_dir = Path(run_dir)
        self._inner = RecordedEvalRunner(lambda instance_id: results)
        self.calls: list[str] = []
        self.rounds_present: list[list[str]] = []

    def evaluate(self, cell_dir: Path) -> None:
        self.calls.append(Path(cell_dir).relative_to(self.run_dir).as_posix())
        self.rounds_present.append(sorted(p.relative_to(self.run_dir).as_posix() for p in self.run_dir.glob("*/r*")))
        self._inner.evaluate(cell_dir)


class JudgeSession:
    """The judge's agent session: writes the verdict a real judge would, and counts the sessions."""

    def __init__(self, verdict: dict) -> None:
        self.verdict = verdict
        self.calls = 0

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        self.calls += 1
        (Path(workspace) / judge_module.VERDICT_FILENAME).write_text(json.dumps(self.verdict), encoding="utf-8")
        return 0


class RunExperimentLoopTests(TempDirCase):
    def experiment(self, run_dir: Path, **kwargs: Any) -> Optional[dict]:
        kwargs.setdefault("lanes", ["A", "B"])
        return lanes_module.run_experiment(
            run_dir, ["inst-a"], adapter_for, REPO_ROOT, now=lambda: T0, environ={}, repo_sha="s", **kwargs
        )

    def gate(self, session: JudgeSession, min_repeats: int) -> judge_module.StopGate:
        return judge_module.StopGate(judge=judge_module.Judge(REPO_ROOT, runner=session), min_repeats=min_repeats)

    def test_by_default_one_round_runs_and_its_comparison_is_returned_and_written(self) -> None:
        run_dir = self.directory() / "nested" / "run"
        comparison = lanes_module.run_experiment(
            run_dir, ["inst-a"], adapter_for, REPO_ROOT, lanes=["A"], now=lambda: T0, environ={}, repo_sha="s"
        )
        self.assertEqual(json.loads((run_dir / lanes_module.COMPARISON_FILENAME).read_text(encoding="utf-8")), comparison)
        self.assertTrue((run_dir / lanes_module.COMPARISON_MARKDOWN).is_file())
        self.assertEqual(["r1"], sorted(path.name for path in (run_dir / "A").iterdir()))
        assert comparison is not None
        self.assertNotIn("stop_gate", comparison)

    def test_without_scoring_nothing_is_compared(self) -> None:
        run_dir = self.directory()
        self.assertIsNone(self.experiment(run_dir, lanes=["A"], score=False))
        self.assertFalse((run_dir / lanes_module.COMPARISON_FILENAME).exists())
        self.assertFalse((run_dir / lanes_module.COMPARISON_MARKDOWN).exists())

    def test_without_a_gate_every_round_runs_before_each_is_evaluated_once(self) -> None:
        run_dir = self.directory()
        evals = RecordingEvalRunner(run_dir, PASSING_EVAL)
        comparison = self.experiment(run_dir, repeats=2, eval_runner=evals)

        self.assertEqual(["A/r1", "A/r2", "B/r1", "B/r2"], sorted(evals.calls))
        self.assertEqual(["A/r1", "A/r2", "B/r1", "B/r2"], evals.rounds_present[0])
        assert comparison is not None
        self.assertEqual([2, 2], [summary["repeats"] for summary in comparison["lanes"]])

    def test_a_gate_that_never_passes_evaluates_each_round_once_and_stops_at_the_ceiling(self) -> None:
        run_dir = self.directory()
        evals = RecordingEvalRunner(run_dir, FAILING_EVAL)
        session = JudgeSession(GOOD_VERDICT)

        comparison = self.experiment(run_dir, repeats=3, eval_runner=evals, stop_gate=self.gate(session, 1))

        self.assertEqual(["A/r1", "B/r1", "A/r2", "B/r2", "A/r3", "B/r3"], evals.calls)
        self.assertEqual(0, session.calls)  # failing tests never reach the judge
        assert comparison is not None
        gate = comparison["stop_gate"]
        self.assertEqual((3, 3, "ceiling"), (gate["repeats_run"], gate["ceiling"], gate["stopped_by"]))
        self.assertEqual(["continue", "continue", "stop"], [entry["decision"] for entry in gate["rounds"]])

    def test_a_gate_that_passes_stops_the_run_and_only_its_rounds_are_compared(self) -> None:
        run_dir = self.directory()
        # A round left by an earlier, longer run in the same directory.
        write_json(run_dir / "A" / "r3" / "inst-a" / "run.json", {"lane": "A", "returncode": 9, "strategy": None})
        evals = RecordingEvalRunner(run_dir, PASSING_EVAL)
        session = JudgeSession(GOOD_VERDICT)

        comparison = self.experiment(run_dir, repeats=3, eval_runner=evals, stop_gate=self.gate(session, 2))

        self.assertEqual(["A/r1", "B/r1", "A/r2", "B/r2"], evals.calls)
        self.assertEqual(2, session.calls)
        self.assertFalse((run_dir / "A" / "r3" / "inst-a" / "submission.tar.gz").exists())
        self.assertFalse((run_dir / "B" / "r3").exists())
        assert comparison is not None
        self.assertEqual((2, "quality_reached"), (comparison["stop_gate"]["repeats_run"], comparison["stop_gate"]["stopped_by"]))
        self.assertEqual(
            [("A", 2, 2, 0), ("B", 2, 2, 0)],
            [
                (summary["lane"], summary["repeats"], summary["attempts"], summary["process"]["nonzero_returncodes"])
                for summary in comparison["lanes"]
            ],
        )

    def test_a_gate_without_eval_or_above_the_ceiling_is_refused_before_anything_runs(self) -> None:
        run_dir = self.directory() / "run"
        with self.assertRaisesRegex(ValueError, "the stop gate needs eval"):
            self.experiment(run_dir, stop_gate=self.gate(JudgeSession(GOOD_VERDICT), 1))
        with self.assertRaisesRegex(ValueError, r"min_repeats \(2\) is above the repeats ceiling \(1\)"):
            self.experiment(
                run_dir,
                repeats=1,
                eval_runner=RecordingEvalRunner(run_dir, FAILING_EVAL),
                stop_gate=self.gate(JudgeSession(GOOD_VERDICT), 2),
            )
        self.assertFalse(run_dir.exists())

    def test_a_gate_whose_minimum_is_the_ceiling_runs_that_one_round(self) -> None:
        run_dir = self.directory()
        evals = RecordingEvalRunner(run_dir, FAILING_EVAL)
        comparison = self.experiment(
            run_dir, lanes=["A"], repeats=1, eval_runner=evals, stop_gate=self.gate(JudgeSession(GOOD_VERDICT), 1)
        )
        assert comparison is not None
        self.assertEqual(["A/r1"], evals.calls)
        self.assertEqual((1, "ceiling"), (comparison["stop_gate"]["repeats_run"], comparison["stop_gate"]["stopped_by"]))


if __name__ == "__main__":
    unittest.main()
