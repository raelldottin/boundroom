"""The diagnostic transport, exercised the way the last probe should have been.

``test_benchmark_phase_durability`` passed against a Python stand-in that printed early,
and concluded that unbuffering had made a killed session readable. It had not: the real
``--oneshot`` path discards the turn's stdout entirely, so run 35788842735 archived 0-byte
logs again. The stand-in here therefore imitates what the pinned Hermes actually does on
each transport - ``--oneshot`` says nothing until the turn ends, ``chat --format
stream-json`` flushes one JSON object per event - and the assertions are about which of
those survives being killed at a ceiling.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import sqlite3
import string
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import automation.tests.fake_ps  # noqa: F401  (isolates run_agent.sh from the enclosing harness)
from automation.benchmark.adapter import AGENT_SESSIONS_DIR, _default_command_template, _subprocess_runner
from automation.benchmark.instances import TaskSpec, task_spec
from automation.tests.test_benchmark_effectiveness import StubWitness, _fake_prepare
from automation.benchmark.probe import (
    PROBE_FILENAME,
    STREAM_JSON_SUFFIX,
    artifact_mentions,
    main,
    make_probe_dirs,
    run_probe,
    summarize,
    timeline,
)
from automation.benchmark.strategies import (
    AGENT_TIMEOUT_RETURNCODE,
    PHASE_CEILING_SECONDS,
    PHASE_CENSORED,
    ExecutionContext,
    research_prompt,
    run_phase,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")
PHASE_BUDGET_SECONDS = 2
# Long enough that only the phase budget ends the session: the kill is what is under test.
HANG_UNTIL_KILLED = 600

# Both transports of the pinned CLI, chosen by the flags run_agent.sh passes. --oneshot
# buffers everything into a final answer it never reaches; chat --format stream-json
# flushes each event as it happens, which is the whole reason the probe exists.
FAKE_HERMES = string.Template('''#!/usr/bin/env python3
"""Stand in for the pinned Hermes CLI: one transport that speaks, one that does not."""
import json, sys, time

import os, pathlib

argv = sys.argv[1:]
stream = argv[0] == "chat" and "--format" in argv and "stream-json" in argv
# What the launcher put in the environment, before this process could change it. The real
# CLI freezes HERMES_YOLO_MODE at import, so only an export made before the exec counts.
pathlib.Path(sys.argv[0] + ".env.json").write_text(
    json.dumps({"HERMES_YOLO_MODE": os.environ.get("HERMES_YOLO_MODE")})
)

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

if stream:
    emit({"type": "system", "subtype": "init", "timestamp": 1000})
    emit({"type": "tool_use", "name": "file_write", "input": {"path": ".rpi/research.json"}, "timestamp": 2000})
    emit({"type": "tool_result", "name": "file_write", "is_error": False, "timestamp": 3000})
    emit({"type": "text", "text": "still reading", "timestamp": 4000})
else:
    # Exactly the --oneshot contract: nothing on stdout until the turn returns.
    pass
time.sleep(${hang})
sys.stdout.write("final answer never reached\\n")
''')


def _run_phase(tmp: Path, transport: str, *, hang: float = HANG_UNTIL_KILLED, budget: int = PHASE_BUDGET_SECONDS):
    """Run research against the stand-in, which sleeps ``hang`` seconds after it has spoken."""
    workspace = tmp / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)

    hermes = tmp / "fake-hermes"
    hermes.write_text(FAKE_HERMES.substitute(hang=hang), encoding="utf-8")
    hermes.chmod(0o755)

    sessions_dir = tmp / AGENT_SESSIONS_DIR
    env = dict(os.environ)
    env.update(
        {
            "BOUNDROOM_AGENT_RUNNER": "hermes",
            "BOUNDROOM_HERMES_BIN": str(hermes),
            "BOUNDROOM_HERMES_USAGE_DIR": str(sessions_dir),
            "BOUNDROOM_HERMES_TRANSPORT": transport,
            # run_agent.sh makes a throwaway HERMES_HOME under TMPDIR and leaves it there.
            "TMPDIR": str(tmp),
        }
    )
    ctx = ExecutionContext(
        repo_root=REPO_ROOT,
        task=TASK,
        workspace=workspace,
        control_dir=tmp / "control",
        command_template=_default_command_template(REPO_ROOT),
        env=env,
        timeout_seconds=budget,
        runner=_subprocess_runner,
    )
    result = run_phase(ctx, "research", research_prompt(TASK), {"objective": TASK.objective}, budget)
    return result, sessions_dir, hermes


def _probe(instance_id: str, out_dir: Path, **kwargs):
    """``run_probe`` with the workspace made beside ``out_dir``, inside the test's own temp dir.

    The probe never removes its workspace (a run's evidence outlives it), so a test that let it
    default to the system temporary directory would leave one behind on every run.
    """
    kwargs.setdefault("repo_root", REPO_ROOT)
    kwargs.setdefault("workspace_factory", functools.partial(make_probe_dirs, parent=Path(out_dir).parent))
    return run_probe(instance_id, out_dir, **kwargs)


class StreamTransportTests(unittest.TestCase):
    def test_a_killed_stream_session_keeps_every_event_it_flushed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result, sessions_dir, _hermes = _run_phase(Path(raw), "stream-json")

            self.assertEqual(AGENT_TIMEOUT_RETURNCODE, result.returncode)
            self.assertEqual(PHASE_CENSORED, result.state)

            streams = sorted(sessions_dir.glob(f"*{STREAM_JSON_SUFFIX}"))
            self.assertEqual(1, len(streams), f"expected one event stream, found {streams}")
            events = [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                ["system", "tool_use", "tool_result", "text"],
                [event["type"] for event in events],
                "the killed session lost events it had already flushed",
            )

            # The four questions the probe exists to answer, off the archived stream alone.
            entries = timeline(events)
            self.assertEqual([0.0, 1.0, 2.0], [entry["at_seconds"] for entry in entries])
            mentions = artifact_mentions(events)
            self.assertEqual(1, len(mentions), "the write attempt is not visible in the stream")
            self.assertEqual("tool_use", mentions[0]["type"])

    def test_the_oneshot_transport_still_leaves_nothing_when_killed(self) -> None:
        """Not a regression - the reason the probe had to exist."""
        with tempfile.TemporaryDirectory() as raw:
            result, sessions_dir, _hermes = _run_phase(Path(raw), "oneshot")

            self.assertEqual(AGENT_TIMEOUT_RETURNCODE, result.returncode)
            self.assertEqual([], sorted(sessions_dir.glob(f"*{STREAM_JSON_SUFFIX}")))
            logs = sorted(sessions_dir.glob("*.log"))
            self.assertEqual(1, len(logs))
            self.assertEqual("", logs[0].read_text(encoding="utf-8"))


class TransportContractTests(unittest.TestCase):
    def test_the_stream_transport_records_itself_and_claims_no_usage(self) -> None:
        """A probe must not look like a cell that was served and spent nothing."""
        with tempfile.TemporaryDirectory() as raw:
            _result, sessions_dir, _hermes = _run_phase(Path(raw), "stream-json", hang=0)

            controls = sorted(sessions_dir.glob("*.controls.json"))
            self.assertEqual(1, len(controls))
            recorded = json.loads(controls[0].read_text(encoding="utf-8"))
            self.assertEqual("stream-json", recorded["transport"])
            self.assertEqual([], sorted(sessions_dir.glob("*.usage.json")))
            # Nothing here refuses a pinned tool by configuration any more, so the receipt
            # claims no refusal; what the session was actually given is read off the JSONL.
            self.assertEqual([], recorded["refused_tools"])

    def test_the_stream_transport_is_launched_without_the_approval_difference(self) -> None:
        """`hermes chat -q` denies execute_code unattended, and freezes the override at
        import - so an export after the exec would measure a session working around a
        refusal the diagnostic did not mean to administer."""
        with tempfile.TemporaryDirectory() as raw:
            _result, _sessions_dir, hermes = _run_phase(Path(raw), "stream-json", hang=0)

            observed = json.loads(Path(str(hermes) + ".env.json").read_text(encoding="utf-8"))
            self.assertEqual("1", observed["HERMES_YOLO_MODE"])

    def test_the_default_transport_is_left_to_set_its_own_approval_mode(self) -> None:
        """--oneshot sets it inside Hermes. Exporting it here too would change the posture
        every A-E lane has been measured under, for no diagnostic gain."""
        with tempfile.TemporaryDirectory() as raw:
            _result, _sessions_dir, hermes = _run_phase(Path(raw), "oneshot", hang=0)

            observed = json.loads(Path(str(hermes) + ".env.json").read_text(encoding="utf-8"))
            self.assertIsNone(observed["HERMES_YOLO_MODE"])

    def test_the_default_transport_is_unchanged_and_still_reports_usage(self) -> None:
        """Killed at the budget, as a --oneshot session that never answers is."""
        with tempfile.TemporaryDirectory() as raw:
            _result, sessions_dir, _hermes = _run_phase(Path(raw), "oneshot")

            controls = json.loads(sorted(sessions_dir.glob("*.controls.json"))[0].read_text(encoding="utf-8"))
            self.assertEqual("oneshot", controls["transport"])
            self.assertEqual(
                ["terminal", "file", "code_execution", "todo"],
                controls["requested_toolsets"],
                "the transport switch must not disturb the pinned posture",
            )
            # Not observed refusing anything is a different claim from observed allowing
            # everything, which is why effective_tools stays null on both transports.
            self.assertEqual([], controls["refused_tools"])
            self.assertIsNone(controls["effective_tools"])

    def test_an_unknown_transport_is_refused(self) -> None:
        script = REPO_ROOT / "automation" / "supervisor" / "run_agent.sh"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "prompt.md").write_text("hello\n", encoding="utf-8")
            (tmp / "context.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(tmp)], check=True)
            # Present and runnable, so the refusal below is about the transport and not
            # about a missing CLI.
            hermes = tmp / "fake-hermes"
            hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hermes.chmod(0o755)
            env = dict(os.environ)
            env.update(
                {
                    "BOUNDROOM_AGENT_RUNNER": "hermes",
                    "BOUNDROOM_HERMES_BIN": str(hermes),
                    "BOUNDROOM_HERMES_TRANSPORT": "tail -f",
                    "TMPDIR": str(tmp),
                }
            )
            done = subprocess.run(
                [
                    str(script),
                    "--repo-root",
                    str(tmp),
                    "--prompt-file",
                    str(tmp / "prompt.md"),
                    "--context-file",
                    str(tmp / "context.json"),
                    "--handoff-file",
                    str(tmp / "handoff.json"),
                    "--slice-id",
                    "probe",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(64, done.returncode, done.stderr)
            self.assertIn("BOUNDROOM_HERMES_TRANSPORT", done.stderr)


class ProbeRecordTests(unittest.TestCase):
    """The record has to stand on its own: the runner it came from will be gone."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _runner(self, checkpoint: dict, events: list[dict]):
        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            artifact = workspace / ".rpi" / "research.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(checkpoint), encoding="utf-8")
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            stem = sessions / "20260101T000000Z-1"
            stem.with_suffix(".controls.json").write_text(
                json.dumps({"runner": "hermes", "transport": env["BOUNDROOM_HERMES_TRANSPORT"]}),
                encoding="utf-8",
            )
            (sessions / f"20260101T000000Z-1{STREAM_JSON_SUFFIX}").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )
            return AGENT_TIMEOUT_RETURNCODE

        return runner

    def test_the_probe_keeps_the_artifact_the_stream_and_the_verdict(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        checkpoint = {
            "task": "rebuild cmatrix",
            "relevant_files": [],
            "findings": [],
            "constraints": [],
            "unknowns": [],
            "risks": [],
            "evidence": [],
        }
        events = [
            {"type": "system", "subtype": "init", "timestamp": 5000},
            {"type": "tool_use", "name": "file_write", "input": {"path": ".rpi/research.json"}, "timestamp": 9000},
        ]
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record = _probe(
                self.INSTANCE,
                out,
                budget_seconds=30,
                cleanroom=False,
                runner=self._runner(checkpoint, events),
            )

            self.assertEqual("stream-json", record["transport"])
            self.assertEqual(PHASE_CENSORED, record["phase_result"]["state"])
            self.assertTrue(record["artifact"]["present"])
            self.assertEqual([], record["artifact"]["schema_errors"])
            self.assertEqual(2, record["stream"]["events"])
            self.assertEqual([0.0, 4.0], [entry["at_seconds"] for entry in record["stream"]["timeline"]])
            self.assertEqual(1, len(record["stream"]["artifact_mentions"]))
            # The prompt is the lane's, not a restatement of it.
            self.assertEqual(len(research_prompt(task_spec(self.INSTANCE))), record["prompt_chars"])
            self.assertTrue((out / PROBE_FILENAME).is_file())
            self.assertTrue((out / "rpi" / "research.json").is_file())

    def test_artifact_mentions_are_attributed_to_the_event_that_named_them(self) -> None:
        """timeline() drops text events; zipping it against the unfiltered list misaligns both.

        Every entry after the first text delta was attributed to the wrong event, so the probe
        reported the wrong time and the wrong tool for the write attempt it exists to find.
        """
        events = [
            {"type": "system", "subtype": "init", "timestamp": 0},
            {"type": "text", "timestamp": 1000},
            {"type": "tool_use", "name": "shell", "input": {"command": "ls"}, "timestamp": 2000},
            {"type": "tool_use", "name": "file_write", "input": {"path": ".rpi/research.json"}, "timestamp": 3000},
        ]
        mentions = artifact_mentions(events)
        self.assertEqual(1, len(mentions))
        self.assertEqual("file_write", mentions[0]["name"])
        self.assertEqual(3.0, mentions[0]["at_seconds"])

    def test_a_truncated_last_line_does_not_lose_the_stream(self) -> None:
        """A session killed mid-write leaves a partial line; the rest is still evidence."""
        from automation.benchmark.probe import _events

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "s.stream.jsonl"
            path.write_text('{"type": "system", "timestamp": 1}\n{"type": "tool_u', encoding="utf-8")
            self.assertEqual([{"type": "system", "timestamp": 1}], _events(path))


class ProbeEnvironmentIsolationTests(unittest.TestCase):
    """The probe launches an agent against an untrusted repository, through shell=True.

    It inherited the launcher environment wholesale - the boundary
    ``audited_inherited_environment()`` exists to hold on the adapter path, absent one file
    over. What a lane refuses to hand an agent, a diagnostic does not get to hand it either.
    """

    INSTANCE = "abishekvashok__cmatrix.5c082c6"
    SENTINEL = "PROBE_LEAK_SENTINEL_NOT_A_REAL_SECRET"

    def _capturing_runner(self, captured: dict):
        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            captured["env"] = dict(env)
            artifact = workspace / ".rpi" / "research.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            return 0

        return runner

    def _probe_environment(self, probe_env=None) -> dict:

        captured: dict = {}
        with tempfile.TemporaryDirectory() as raw:
            _probe(
                self.INSTANCE,
                Path(raw) / "probe",
                budget_seconds=30,
                cleanroom=False,
                runner=self._capturing_runner(captured),
                env=probe_env,
            )
        return captured["env"]

    def test_a_parent_only_variable_does_not_reach_the_probe_session(self) -> None:
        with unittest.mock.patch.dict(os.environ, {self.SENTINEL: "sentinel-value"}):
            environment = self._probe_environment()
        # Never assertNotIn: it would render the whole environment into the failure report.
        self.assertFalse(self.SENTINEL in environment, f"{self.SENTINEL} reached the probe session")

    def test_an_explicitly_selected_variable_reaches_the_probe_session(self) -> None:
        environment = self._probe_environment({self.SENTINEL: "sentinel-value"})
        self.assertEqual("sentinel-value", environment.get(self.SENTINEL))

    def test_the_probe_still_carries_what_the_runner_reads(self) -> None:
        environment = self._probe_environment()
        self.assertIn("PATH", environment)
        self.assertEqual("stream-json", environment["BOUNDROOM_HERMES_TRANSPORT"])

    def test_the_cli_selects_launcher_variables_by_name(self) -> None:
        from automation.benchmark import probe as probe_module

        args = probe_module.build_parser().parse_args(
            ["--instance", self.INSTANCE, "--out-dir", "out", "--agent-env", self.SENTINEL]
        )
        self.assertEqual([self.SENTINEL], args.agent_env)


class ProbeArtifactLinkTests(unittest.TestCase):
    """`.rpi` is agent-created. Copying out of it is an evidence collector's job, not a
    dereference of whatever the agent decided to point at."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _probe_over_a_linked_artifact(self, out: Path, target: Path):

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            rpi = workspace / ".rpi"
            rpi.mkdir(parents=True, exist_ok=True)
            (rpi / "research.json").write_text("{}", encoding="utf-8")
            (rpi / "host-file.json").symlink_to(target)
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        return _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)

    def test_a_symlinked_artifact_is_not_copied_and_its_target_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside" / "host-sentinel.txt"
            host.parent.mkdir(parents=True)
            host.write_text("host sentinel, not agent evidence\n", encoding="utf-8")
            out = Path(raw) / "probe"

            self._probe_over_a_linked_artifact(out, host)

            copied = out / "rpi" / "host-file.json"
            self.assertFalse(copied.exists(), "a symlinked artifact was dereferenced into the run directory")
            self.assertFalse(copied.is_symlink(), "a symlinked artifact was reproduced in the run directory")
            self.assertEqual("host sentinel, not agent evidence\n", host.read_text(encoding="utf-8"))
            # The real artifact beside it is still collected: skipping is not refusing.
            self.assertTrue((out / "rpi" / "research.json").is_file())

    def test_the_skipped_link_is_recorded_rather_than_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside" / "host-sentinel.txt"
            host.parent.mkdir(parents=True)
            host.write_text("host sentinel\n", encoding="utf-8")
            out = Path(raw) / "probe"

            record = self._probe_over_a_linked_artifact(out, host)

            # A silent omission reads as "the agent wrote nothing". This one reads as a
            # finding about the session, which is the only thing a probe produces.
            self.assertEqual(["host-file.json"], record["rpi"]["skipped_links"])
            self.assertEqual(["research.json"], record["rpi"]["copied"])

    def test_a_symlinked_rpi_directory_is_not_followed_at_all(self) -> None:
        """The guard one level up.

        ``Path.is_dir()`` follows links, so gating the copy on it and then checking each
        entry leaves the whole container untrusted: an agent that links `.rpi` itself gets
        every entry of the target copied, each one a real file with is_symlink() False.
        Strictly more reach than the single-entry case, and invisible to a test that builds
        `.rpi` as a real directory.
        """

        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside"
            host.mkdir()
            (host / "id_rsa").write_text("host sentinel, not agent evidence\n", encoding="utf-8")
            out = Path(raw) / "probe"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                (workspace / ".rpi").symlink_to(host, target_is_directory=True)
                Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)

            self.assertFalse((out / "rpi" / "id_rsa").exists(), "a linked artifact container was followed")
            self.assertFalse((out / "rpi").exists(), "a linked artifact container produced a run directory")
            self.assertEqual("host sentinel, not agent evidence\n", (host / "id_rsa").read_text(encoding="utf-8"))
            # Recorded, for the same reason an entry-level skip is: silence reads as "the
            # agent wrote nothing", which is the question the probe exists to answer.
            self.assertTrue(record["rpi"]["container_skipped"])

    def test_a_subdirectory_is_skipped_rather_than_destroying_the_collected_evidence(self) -> None:
        """One predicate covers this and the link case.

        read_bytes() on a directory raised mid-loop and the BaseException handler then
        removed the destination - so a session could delete its own collected evidence by
        leaving a directory in `.rpi`, which is the failure skip-rather-than-refuse exists
        to prevent.
        """

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                rpi = workspace / ".rpi"
                (rpi / "nested").mkdir(parents=True)
                (rpi / "research.json").write_text("{}", encoding="utf-8")
                Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)

            self.assertTrue((out / "rpi" / "research.json").is_file(), "the real artifact was lost with the directory")
            self.assertFalse((out / "rpi" / "nested").exists())
            self.assertEqual(["nested"], record["rpi"]["skipped_non_files"])

    def test_a_run_with_no_links_records_an_empty_skip_list(self) -> None:

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            rpi = workspace / ".rpi"
            rpi.mkdir(parents=True, exist_ok=True)
            (rpi / "research.json").write_text("{}", encoding="utf-8")
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record = _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)
            self.assertEqual([], record["rpi"]["skipped_links"])
            self.assertEqual([], record["rpi"]["skipped_non_files"])
            self.assertFalse(record["rpi"]["container_skipped"])


class ProbeOutputOwnershipTests(unittest.TestCase):
    """The probe claims what it writes. It used to wipe a directory it had never created."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _runner(self, calls: list, squats: Optional[Callable[[], None]] = None):
        """A phase that produces the artifact, and optionally claims an output mid-run.

        ``squats`` is the race the pre-flight refusal cannot see: a path that did not exist
        when the probe checked, and does by the time the probe writes.
        """

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            calls.append(workspace)
            artifact = workspace / ".rpi" / "research.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            if squats is not None:
                squats()
            return 0

        return runner

    def _run(self, out_dir: Path, calls: Optional[list] = None, squats: Optional[Callable[[], None]] = None):

        return _probe(
            self.INSTANCE,
            out_dir,
            budget_seconds=30,
            cleanroom=False,
            runner=self._runner(calls if calls is not None else [], squats),
        )

    def test_a_pre_existing_artifact_directory_is_refused_and_left_intact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            nested = out / "rpi" / "earlier-run"
            nested.mkdir(parents=True)
            (nested / "phase-1.json").write_text("first attempt\n", encoding="utf-8")
            sentinel = out / "rpi" / "owner-sentinel.txt"
            sentinel.write_text("first attempt\n", encoding="utf-8")

            calls: list = []
            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, calls)

            self.assertIn("refusing to overwrite", str(refusal.exception))
            # Refused before launch: a probe that runs the phase first has already spent the
            # budget the refusal exists to save.
            self.assertEqual([], calls)
            # Byte-for-byte and structurally unchanged: unlink() used to raise IsADirectoryError
            # on the subdirectory, after it had already destroyed the file beside it.
            self.assertEqual("first attempt\n", sentinel.read_text(encoding="utf-8"))
            self.assertEqual("first attempt\n", (nested / "phase-1.json").read_text(encoding="utf-8"))

    def test_a_pre_existing_probe_record_is_refused_not_overwritten(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            out.mkdir(parents=True)
            record = out / PROBE_FILENAME
            record.write_text("first attempt\n", encoding="utf-8")
            calls: list = []
            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, calls)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("first attempt\n", record.read_text(encoding="utf-8"))
            self.assertEqual([], calls)

    def test_an_artifact_directory_that_appears_mid_run_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def squat() -> None:
                (out / "rpi").mkdir(parents=True)
                (out / "rpi" / "other-run.json").write_text("not mine\n", encoding="utf-8")

            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, squats=squat)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("not mine\n", (out / "rpi" / "other-run.json").read_text(encoding="utf-8"))

    def test_a_probe_record_that_appears_mid_run_is_still_refused(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def squat() -> None:
                (out / PROBE_FILENAME).write_text("not mine\n", encoding="utf-8")

            with self.assertRaises(FileExistsError) as refusal:
                self._run(out, squats=squat)
            self.assertIn("refusing to overwrite", str(refusal.exception))
            self.assertEqual("not mine\n", (out / PROBE_FILENAME).read_text(encoding="utf-8"))

    def test_a_probe_into_an_empty_run_directory_writes_both(self) -> None:
        from automation.benchmark.probe import PROBE_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record = self._run(out)
            self.assertEqual(json.dumps(record, indent=2) + "\n", (out / PROBE_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue((out / "rpi" / "research.json").is_file())


class ProbeHardLinkTests(unittest.TestCase):
    """The third way host content reaches the run directory.

    A hard link is not a symlink and ``is_file()`` is True for it, so both existing
    per-entry guards pass it straight through and the host's bytes are written out and
    listed under ``copied`` as though the session had produced them. It grants no read
    authority the agent lacks - HOME is already inherited - so the harm is to the evidence,
    not to the host: the record asserts a clean collection of something it did not collect.
    """

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def test_a_hardlinked_artifact_is_not_laundered_into_the_collected_evidence(self) -> None:

        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw) / "outside" / "id_rsa"
            host.parent.mkdir(parents=True)
            host.write_text("host sentinel, not agent evidence\n", encoding="utf-8")
            out = Path(raw) / "probe"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                rpi = workspace / ".rpi"
                rpi.mkdir(parents=True, exist_ok=True)
                (rpi / "research.json").write_text("{}", encoding="utf-8")
                os.link(host, rpi / "id_rsa")
                Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)

            self.assertFalse(
                (out / "rpi" / "id_rsa").exists(),
                "host content reached the run directory through a hard link",
            )
            self.assertEqual(["id_rsa"], record["rpi"]["skipped_links"])
            # Skipping is not refusing: the session's own artifact beside it still arrives.
            self.assertEqual(["research.json"], record["rpi"]["copied"])
            self.assertEqual("host sentinel, not agent evidence\n", host.read_text(encoding="utf-8"))


class ProbeSummaryRenderingTests(unittest.TestCase):
    """Agent-chosen names are printed to an operator. ``probe.json`` is safe because
    ``json.dumps`` escapes; the printed summary joins the names raw, so a name carrying a
    newline writes whole lines of its own into the operator's answer."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    FORGED = "artifact      present (9999 chars, schema valid)"

    def test_a_filename_cannot_forge_a_summary_line(self) -> None:
        from automation.benchmark.probe import summarize

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            # Trailing newline too: the attacker owns the whole name, so the line the
            # summary was going to append lands on a line of its own rather than as a tail.
            name = f"decoy\n{self.FORGED}\n"

            def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
                (workspace / ".rpi" / name).mkdir(parents=True)
                Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            record = _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)
            summary = summarize(record)

            # The run this describes wrote no artifact at all.
            self.assertFalse(record["artifact"]["present"])
            self.assertFalse(
                self.FORGED in summary.splitlines(),
                "an agent-chosen filename wrote a whole line into the operator summary",
            )
            # Escaped for printing, kept verbatim in the record: the real name is evidence.
            self.assertEqual([name], record["rpi"]["skipped_non_files"])


class ProbeCleanroomInheritanceTests(unittest.TestCase):
    """The diagnostic has to run where a cell runs.

    Before this, the probe built a bare `git init` workspace on the local filesystem, named
    it after the real instance id, and reached Hermes' local backend with no ProgramBench
    executable, no bundled docs and no network-none container. Its timings would have
    described the pre-cleanroom environment that PR #55 and #56 retired, and a 600-second
    answer about the wrong sandbox is worse than no answer.
    """

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _run(self, out: Path, calls: list, watched: Optional[list] = None):
        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            calls.append({"workspace": workspace, "env": dict(env)})
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / f"turn{STREAM_JSON_SUFFIX}").write_text(
                json.dumps({"type": "system", "subtype": "init", "timestamp": 1000})
                + "\n"
                + json.dumps(
                    {"type": "result", "exit_code": 0, "duration_ms": 612000, "tokens": {"total": 4096}, "session_id": "s-1"}
                )
                + "\n",
                encoding="utf-8",
            )
            return 0

        proofs: list = []
        witness = StubWitness()

        def witness_factory(*args: object) -> StubWitness:
            if watched is not None:
                watched.append(args)
            return witness

        record = _probe(
            self.INSTANCE,
            out,
            budget_seconds=30,
            runner=runner,
            prepare=_fake_prepare,
            prove_sandbox=lambda *a, **k: proofs.append(len(calls)),
            witness_factory=witness_factory,
        )
        return record, proofs

    def test_the_probe_session_runs_in_the_cell_s_own_cleanroom(self) -> None:
        from automation.benchmark.adapter import SANDBOX_IMAGE_ENV
        from automation.benchmark.cleanroom import image_for

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            calls: list = []
            watched: list = []
            record, proofs = self._run(out, calls, watched)

            self.assertEqual(1, len(calls))
            self.assertEqual(
                [((out / AGENT_SESSIONS_DIR).resolve(), calls[0]["workspace"], image_for(self.INSTANCE), "sha256:d1ge57")],
                watched,
            )
            # Named by the image the caller materialized, so run_agent.sh takes its sandbox
            # branch instead of Hermes' local backend.
            self.assertEqual(image_for(self.INSTANCE), calls[0]["env"][SANDBOX_IMAGE_ENV])
            self.assertEqual("stream-json", calls[0]["env"]["BOUNDROOM_HERMES_TRANSPORT"])
            # De-identified, like a cell's: the agent's shell prompt shows this directory.
            workspace = calls[0]["workspace"].name
            self.assertIn(task_spec(self.INSTANCE).public_id, workspace)
            self.assertNotIn(self.INSTANCE, workspace)
            # Proven before the budget is spent, not after.
            self.assertEqual([0], proofs, "the sandbox proof did not run before the model turn")

    def test_the_receipts_are_kept_beside_the_record(self) -> None:
        from automation.benchmark.adapter import CLEANROOM_RECEIPT_FILENAME

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record, _proofs = self._run(out, [])

            written = (out / CLEANROOM_RECEIPT_FILENAME).read_text(encoding="utf-8")
            receipt = json.loads(written)
            self.assertEqual(json.dumps(receipt, indent=2) + "\n", written)
            self.assertEqual("none", receipt["network_mode"])
            self.assertEqual(receipt, record["cleanroom"])
            self.assertTrue(record["sandbox"]["observation"]["observed"])
            self.assertEqual([], record["sandbox"]["violations"])
            self.assertIn("NetworkMode=none", summarize(record))

    def test_a_sandbox_violation_is_recorded_rather_than_raised(self) -> None:
        """A cell refuses, because a violated sandbox makes its score not a ProgramBench
        result. A probe has no score, and its timeline is the whole deliverable - throwing
        that away over a finding about the environment would lose the finding too."""
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"

            def runner(_command: str, _workspace: Path, env, _timeout: int) -> int:
                Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
                return 0

            witness = StubWitness(violations=("container ran with NetworkMode=bridge",))
            record = _probe(
                self.INSTANCE,
                out,
                budget_seconds=30,
                runner=runner,
                prepare=_fake_prepare,
                prove_sandbox=lambda *a, **k: None,
                witness_factory=lambda *a, **k: witness,
            )

            self.assertEqual(["container ran with NetworkMode=bridge"], record["sandbox"]["violations"])
            self.assertIn("NetworkMode=bridge", summarize(record))

    def test_the_synthetic_path_says_it_is_not_a_cell_s_environment(self) -> None:
        """--no-cleanroom exists to exercise this module without Docker. Its summary must not
        read like a measurement of the environment a lane runs in."""

        def runner(_command: str, _workspace: Path, env, _timeout: int) -> int:
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            record = _probe(
                self.INSTANCE,
                Path(raw) / "probe",
                budget_seconds=30,
                runner=runner,
                cleanroom=False,
            )

            self.assertIsNone(record["cleanroom"])
            self.assertEqual([], record["sandbox"]["violations"])
            self.assertIn("NO CLEANROOM", summarize(record))


class TerminalResultTests(unittest.TestCase):
    """What PR #54 was for, kept without its transport claim: stream-json carries the turn's
    duration and token counts, and names no model. The job-level preflight is what compares
    served against asked."""

    def test_the_final_result_event_is_the_only_accounting_this_transport_gives(self) -> None:
        from automation.benchmark.probe import terminal_result

        self.assertIsNone(terminal_result([{"type": "text", "text": "hi"}]))
        final = terminal_result(
            [
                {"type": "result", "exit_code": 1, "duration_ms": 1},
                {
                    "type": "result",
                    "exit_code": 0,
                    "duration_ms": 612000,
                    "tokens": {"total": 4096},
                    "session_id": "s-1",
                    "model": "",
                },
            ]
        )
        assert final is not None
        # The last one wins, and nothing model-shaped is carried forward.
        self.assertEqual(
            {"exit_code": 0, "duration_ms": 612000, "tokens": {"total": 4096}, "session_id": "s-1"},
            final,
        )
        self.assertNotIn("model", final)


class FirstTurnAccountingTests(unittest.TestCase):
    """The accounting slice: one completed provider turn, then read what it spent.

    The stream-json probe ruled out tool and sandbox latency but cannot say whether the model
    reasoned silently or the provider was slow, because the emitter drops reasoning events by
    design. `reasoning_tokens` is what separates the two, and neither transport gives both
    halves: --oneshot writes a usage report but honours no turn cap, and the capped chat path
    writes no report. So the cap goes on the chat path and the accounting is read from Hermes'
    own session ledger, which every path writes through.
    """

    INSTANCE = "abishekvashok__cmatrix.5c082c6"
    # Shape taken from hermes_cli/oneshot.py's _USAGE_KEYS, not invented.
    REPORT = {
        "input_tokens": 5852,
        "output_tokens": 55,
        "reasoning_tokens": 35,
        "total_tokens": 5942,
        "api_calls": 2,
        "model": "z-ai/glm-5.3",
        "provider": "nvidia",
        "completed": False,
        "partial": True,
        "interrupted": False,
        "turn_exit_reason": "max_turns",
    }
    SESSION_ID = "sess-1f485258"
    ROW = (SESSION_ID, "z-ai/glm-5.3", "nvidia", 2, 5852, 55, 0, 0, 35, 0.001)

    PINNED = {"HERMES_INFERENCE_MODEL": "z-ai/glm-5.3", "HERMES_INFERENCE_PROVIDER": "nvidia"}

    def _run(self, out: Path, *, transport: str, max_turns=None, session: str = "ledger", rows=None, env=None):
        """Run the probe against a fake session that leaves what that transport leaves.

        `session`: "ledger" writes a stream plus a controls receipt and a ledger row, as the
        capped chat path does; "report" writes a --usage-file report, as --oneshot does;
        "nothing" leaves neither.
        """
        from automation.benchmark.probe import CONTROLS_SUFFIX, STREAM_JSON_SUFFIX, USAGE_SUFFIX

        captured: dict = {}

        def runner(_command: str, _workspace: Path, env, _timeout: int) -> int:
            captured.update(env)
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            if session == "report":
                (sessions / f"turn{USAGE_SUFFIX}").write_text(json.dumps(self.REPORT), encoding="utf-8")
            elif session == "ledger":
                home = sessions / "hermes-home"
                (sessions / f"turn{STREAM_JSON_SUFFIX}").write_text(
                    json.dumps({"type": "system", "subtype": "init", "session_id": self.SESSION_ID}) + "\n",
                    encoding="utf-8",
                )
                (sessions / f"turn{CONTROLS_SUFFIX}").write_text(
                    json.dumps({"hermes_home": str(home), "max_turns": 1}), encoding="utf-8"
                )
                _write_ledger(home, list(rows) if rows is not None else [self.ROW])
            return 0

        record = _probe(
            self.INSTANCE,
            out,
            budget_seconds=1800,
            runner=runner,
            cleanroom=False,
            transport=transport,
            max_turns=max_turns,
            env=env,
        )
        return record, captured

    def test_the_turn_cap_goes_to_the_transport_that_consumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record, env = self._run(Path(raw) / "probe", transport="stream-json", max_turns=1)

            self.assertEqual("stream-json", env["BOUNDROOM_HERMES_TRANSPORT"])
            self.assertEqual("1", env["BOUNDROOM_HERMES_MAX_TURNS"])
            self.assertEqual(1, record["max_turns"])

    def test_a_cap_on_the_uncappable_transport_is_refused_not_recorded(self) -> None:
        """At the pinned revision --oneshot builds its AIAgent without max_iterations, so
        sys.maxsize stands; the flag, agent.max_turns and HERMES_MAX_ITERATIONS are all read
        on the chat path it bypasses. Recording the cap anyway would label a session that ran
        to the wall clock as one turn."""

        def unreachable(*_args: object) -> int:
            raise AssertionError("a refused cap must not reach a session")

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError) as refusal:
                _probe(
                    self.INSTANCE,
                    Path(raw) / "probe",
                    runner=unreachable,
                    cleanroom=False,
                    transport="oneshot",
                    max_turns=1,
                )

            self.assertIn("not honoured on the 'oneshot' transport", str(refusal.exception))

    def test_an_uncapped_probe_says_nothing_about_turns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record, env = self._run(Path(raw) / "probe", transport="oneshot", session="report")

            self.assertNotIn("BOUNDROOM_HERMES_MAX_TURNS", env)
            self.assertIsNone(record["max_turns"])

    def test_the_capped_transport_reads_its_accounting_from_the_session_ledger(self) -> None:
        """The chat path writes no usage report, so the numbers come from the row Hermes wrote
        for that exact session id - the one the stream names."""
        with tempfile.TemporaryDirectory() as raw:
            record, _env = self._run(Path(raw) / "probe", transport="stream-json", max_turns=1)

            self.assertEqual("state.db:session_model_usage", record["usage_source"])
            self.assertEqual(self.SESSION_ID, record["usage"]["session_id"])
            self.assertEqual(35, record["usage"]["reasoning_tokens"])
            self.assertEqual(2, record["usage"]["api_calls"])
            self.assertEqual("z-ai/glm-5.3", record["usage"]["model"])
            self.assertEqual("nvidia", record["usage"]["provider"])
            rendered = summarize(record)
            self.assertIn("reasoning 35", rendered)
            self.assertIn("state.db:session_model_usage", rendered)
            self.assertIn("per api call", rendered)

    def test_two_routes_in_the_ledger_are_summed_and_said_to_be_a_blend(self) -> None:
        """A fallback mid-session leaves two rows. Summing them and naming the busiest route is
        the only reading available, so the record has to say the counts are not one model's."""
        with tempfile.TemporaryDirectory() as raw:
            record, _env = self._run(
                Path(raw) / "probe",
                transport="stream-json",
                max_turns=1,
                rows=[
                    (self.SESSION_ID, "z-ai/glm-5.3", "nvidia", 3, 100, 10, 0, 0, 30, 0.001),
                    (self.SESSION_ID, "fallback/model", "openrouter", 1, 50, 5, 0, 0, 5, 0.002),
                ],
            )

            self.assertEqual(2, record["usage"]["routes"])
            self.assertEqual(4, record["usage"]["api_calls"])
            self.assertEqual(35, record["usage"]["reasoning_tokens"])
            self.assertEqual("z-ai/glm-5.3", record["usage"]["model"])
            self.assertIn("2 ROUTES summed", summarize(record))

    NEMOTRON_ROW = (SESSION_ID, "nvidia/nemotron-3-ultra-550b-a55b", "nvidia", 1, 5661, 54, 0, 0, 0, 0.001)

    def test_a_ledger_row_naming_another_model_fails_attribution(self) -> None:
        """Run 36822804022, reproduced: the session asked for z-ai/glm-5.3 on the stream-json
        transport and was billed nvidia/nemotron-3-ultra-550b-a55b. The receipt is still
        written - the finding is in it - but nothing may report it as a measurement of the
        pinned model."""
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            record, _env = self._run(out, transport="stream-json", max_turns=1, rows=[self.NEMOTRON_ROW], env=self.PINNED)

            self.assertEqual("z-ai/glm-5.3", record["attribution"]["asked_model"])
            self.assertIn("ledger says 'nvidia/nemotron-3-ultra-550b-a55b'", record["attribution"]["error"])
            self.assertIn("WRONG MODEL", summarize(record))
            # On disk too: the verdict is evidence, not just a return value.
            written = json.loads((out / "probe.json").read_text(encoding="utf-8"))
            self.assertEqual(record["attribution"], written["attribution"])

    def test_a_fallback_route_fails_attribution_even_when_the_busiest_route_is_the_pinned_model(self) -> None:
        """The busiest route names the model, so a blend can agree with the request and still be
        two models' counters summed. The route count is what catches it."""
        with tempfile.TemporaryDirectory() as raw:
            record, _env = self._run(
                Path(raw) / "probe",
                transport="stream-json",
                max_turns=1,
                rows=[
                    (self.SESSION_ID, "z-ai/glm-5.3", "nvidia", 3, 100, 10, 0, 0, 30, 0.001),
                    (self.SESSION_ID, "nvidia/nemotron-3-ultra-550b-a55b", "nvidia", 1, 50, 5, 0, 0, 5, 0.002),
                ],
                env=self.PINNED,
            )

            self.assertEqual("z-ai/glm-5.3", record["usage"]["model"])  # the request is satisfied on its face
            self.assertIn("2 model routes", record["attribution"]["error"])

    def test_the_pinned_model_on_one_route_passes_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record, env = self._run(Path(raw) / "probe", transport="stream-json", max_turns=1, env=self.PINNED)

            self.assertIsNone(record["attribution"]["error"])
            self.assertEqual(
                ("z-ai/glm-5.3", "nvidia"), (record["attribution"]["asked_model"], record["attribution"]["asked_provider"])
            )
            self.assertNotIn("WRONG MODEL", summarize(record))
            # The wrapper is handed the request the ledger is then held to.
            self.assertEqual("z-ai/glm-5.3", env["HERMES_INFERENCE_MODEL"])

    def test_a_probe_that_asked_for_no_model_compares_nothing(self) -> None:
        """Attribution is a comparison, and there is nothing to compare an unstated request to.
        A route count is still a route count, so that half of the check stays on."""
        with tempfile.TemporaryDirectory() as raw:
            record, _env = self._run(
                Path(raw) / "probe",
                transport="stream-json",
                max_turns=1,
                rows=[self.NEMOTRON_ROW],
                env={"HERMES_INFERENCE_MODEL": "", "HERMES_INFERENCE_PROVIDER": ""},
            )

            self.assertIsNone(record["attribution"]["error"])
            self.assertIsNone(record["attribution"]["asked_model"])
            self.assertIsNone(record["attribution"]["asked_provider"])

    def test_a_session_that_never_called_the_api_leaves_no_row_and_says_so(self) -> None:
        from automation.benchmark.probe import ledger_usage

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            self.assertIsNone(ledger_usage(home, self.SESSION_ID))  # no database at all
            _write_ledger(home, [])
            self.assertIsNone(ledger_usage(home, self.SESSION_ID))  # a database, no row

    def test_the_reasoning_tokens_and_the_wall_clock_are_recorded_together(self) -> None:
        """A token count without elapsed time cannot distinguish a long hidden trace from a
        slow start to a short one - the ratio is the whole finding."""
        with tempfile.TemporaryDirectory() as raw:
            record, _env = self._run(Path(raw) / "probe", transport="oneshot", session="report")

            self.assertEqual(self.REPORT, record["usage"])
            self.assertEqual("usage.json", record["usage_source"])
            self.assertIsInstance(record["elapsed_seconds"], float)
            rendered = summarize(record)
            self.assertIn("reasoning 35", rendered)
            self.assertIn("exit_reason=max_turns", rendered)
            self.assertIn("per api call", rendered)

    def test_no_accounting_at_all_is_said_out_loud(self) -> None:
        """Silence here would read as a turn that cost nothing, which is the one reading the
        accounting slice exists to prevent."""
        with tempfile.TemporaryDirectory() as raw:
            record, _env = self._run(Path(raw) / "probe", transport="oneshot", session="nothing")

            self.assertIsNone(record["usage"])
            self.assertIsNone(record["usage_source"])
            self.assertIn("NO USAGE REPORT", summarize(record))

    def test_a_nonsense_transport_or_cap_is_refused_before_anything_runs(self) -> None:

        def unreachable(*_args: object) -> int:
            raise AssertionError("refused arguments must not reach a session")

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            with self.assertRaises(ValueError):
                _probe(self.INSTANCE, out, runner=unreachable, cleanroom=False, transport="grpc")
            with self.assertRaises(ValueError):
                _probe(
                    self.INSTANCE,
                    out,
                    runner=unreachable,
                    cleanroom=False,
                    transport="stream-json",
                    max_turns=0,
                )


class PlanProbeTests(unittest.TestCase):
    """Plan, probed on the research a lane would hand it, asking the lane's own question."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"
    RESEARCH = {
        "task": "rebuild cmatrix",
        "relevant_files": ["README.md"],
        "findings": ["curses"],
        "constraints": [],
        "unknowns": [],
        "risks": [],
        "evidence": [],
    }

    def _research_file(self, tmp: Path, data: object) -> Path:
        path = tmp / "research.json"
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        return path

    def test_the_plan_probe_seeds_research_and_sends_the_lanes_own_prompt(self) -> None:
        from automation.benchmark.strategies import plan_prompt

        seen: dict[str, object] = {}

        def runner(command: str, workspace: Path, env, _timeout: int) -> int:
            seen["seeded"] = json.loads((workspace / ".rpi" / "research.json").read_text(encoding="utf-8"))
            seen["prompt"] = next(
                Path(part).read_text(encoding="utf-8") for part in command.split() if part.endswith("prompt.md")
            )
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "20260101T000000Z-7.controls.json").write_text(
                json.dumps({"transport": "stream-json", "slice_id": "task:plan"}), encoding="utf-8"
            )
            (sessions / f"20260101T000000Z-7{STREAM_JSON_SUFFIX}").write_text("", encoding="utf-8")
            return AGENT_TIMEOUT_RETURNCODE

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            research = self._research_file(tmp, self.RESEARCH)
            before = research.read_bytes()
            record = _probe(
                self.INSTANCE,
                tmp / "probe",
                cleanroom=False,
                runner=runner,
                phase="plan",
                research_artifact=research,
            )
            self.assertEqual(before, research.read_bytes(), "the research input was modified")

        self.assertEqual(self.RESEARCH, seen["seeded"])
        self.assertEqual(plan_prompt(task_spec(self.INSTANCE)), seen["prompt"])
        self.assertIn("## Checkpoint protocol", str(seen["prompt"]))
        self.assertIn("non-empty `goal`", str(seen["prompt"]))
        self.assertEqual("plan", record["phase"])
        self.assertEqual("plan.json", record["artifact"]["filename"])
        self.assertFalse(record["artifact"]["present"])
        self.assertEqual(64, len(record["research_input"]["sha256"]))
        self.assertEqual(PHASE_CEILING_SECONDS["plan"], record["budget_seconds"])
        [diagnosis] = record["diagnosis"]
        self.assertEqual(("plan", 7, "evidence_unavailable"), (diagnosis["phase"], diagnosis["pid"], diagnosis["verdict"]))
        self.assertIn("event stream: empty - not even system/init was flushed", diagnosis["missing_evidence"])
        self.assertIn("missing       event stream: empty - not even system/init was flushed", summarize(record).splitlines())

    def test_research_keeps_its_observed_captures_and_plan_is_seeded_with_them(self) -> None:
        """Run 38877265488's plan spent its last turns regenerating captures research had saved."""

        def research_runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            observed = workspace / ".rpi" / "observed"
            observed.mkdir(parents=True)
            (workspace / ".rpi" / "research.json").write_text(json.dumps(self.RESEARCH), encoding="utf-8")
            (observed / "usage_h.txt").write_text("Usage: cmatrix", encoding="utf-8")
            (observed / "deeper").mkdir()
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        seen: dict[str, object] = {}

        def plan_runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            seen["observed"] = sorted(p.name for p in (workspace / ".rpi" / "observed").iterdir())
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            research_out = Path(raw) / "probe-research"
            research = _probe(self.INSTANCE, research_out, budget_seconds=30, runner=research_runner, cleanroom=False)
            # One level only, under the same guards: a directory inside observed/ is skipped and named.
            self.assertIn("observed/usage_h.txt", research["rpi"]["copied"])
            self.assertEqual(["observed/deeper"], research["rpi"]["skipped_non_files"])
            self.assertTrue((research_out / "rpi" / "observed" / "usage_h.txt").is_file())

            plan = _probe(
                self.INSTANCE,
                Path(raw) / "probe",
                budget_seconds=30,
                runner=plan_runner,
                cleanroom=False,
                phase="plan",
                research_artifact=research_out / "rpi" / "research.json",
            )

        self.assertEqual(["usage_h.txt"], seen["observed"])
        self.assertEqual(["usage_h.txt"], plan["research_input"]["observed"])

    def test_an_exit_0_is_completed_only_when_the_owed_artifact_is_valid(self) -> None:
        """run_phase leaves artifact_valid unset; the probe must judge it before the diagnosis does."""
        from automation.benchmark.strategies import PLAN_SPEC

        cases = {
            "absent": (None, "treatment_invalid", "evidence_unavailable"),
            "invalid": ("{not json", "treatment_invalid", "evidence_unavailable"),
            "valid": (json.dumps(PLAN_SPEC.template()), "completed", "completed"),
        }
        for name, (plan_text, state, verdict) in cases.items():

            def runner(_command: str, workspace: Path, env, _timeout: int, plan_text=plan_text) -> int:
                if plan_text is not None:
                    (workspace / ".rpi" / "plan.json").write_text(plan_text, encoding="utf-8")
                sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
                sessions.mkdir(parents=True, exist_ok=True)
                (sessions / "20260101T000000Z-8.controls.json").write_text(
                    json.dumps({"transport": "stream-json", "slice_id": "task:plan"}), encoding="utf-8"
                )
                return 0

            with self.subTest(plan=name), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                record = _probe(
                    self.INSTANCE,
                    tmp / "probe",
                    cleanroom=False,
                    runner=runner,
                    phase="plan",
                    research_artifact=self._research_file(tmp, self.RESEARCH),
                )
                self.assertEqual(name == "valid", record["phase_result"]["artifact_valid"])
                self.assertEqual(state, record["phase_result"]["state"])
                self.assertEqual(record["artifact"]["schema_errors"], record["phase_result"]["artifact_errors"])
                self.assertEqual(verdict, record["diagnosis"][0]["verdict"])

    def test_research_a_lane_would_stop_on_is_refused_before_any_session(self) -> None:

        def unreachable(*_args) -> int:
            raise AssertionError("a session was launched")

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for bad in ({"task": "only"}, "not an object"):
                with self.subTest(research=bad), self.assertRaises(ValueError):
                    _probe(
                        self.INSTANCE,
                        tmp / "probe",
                        cleanroom=False,
                        runner=unreachable,
                        phase="plan",
                        research_artifact=self._research_file(tmp, bad),
                    )

    def test_plan_without_research_and_research_with_it_are_both_refused(self) -> None:

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            with self.assertRaises(ValueError):
                _probe(self.INSTANCE, tmp / "a", cleanroom=False, phase="plan")
            with self.assertRaises(ValueError):
                _probe(
                    self.INSTANCE,
                    tmp / "b",
                    cleanroom=False,
                    research_artifact=self._research_file(tmp, self.RESEARCH),
                )


class ProbeWorkspaceTests(unittest.TestCase):
    """Where the session runs: a fresh, de-identified directory, and a git repo only off the cleanroom."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def test_a_synthetic_workspace_is_a_git_repo_named_by_the_public_id(self) -> None:
        task = task_spec(self.INSTANCE)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            workspace, control = make_probe_dirs(task, False, parent=tmp)

            self.assertEqual((tmp, tmp), (workspace.parent, control.parent))
            self.assertTrue(workspace.name.startswith(f"pb-probe-{task.public_id}-"), workspace.name)
            self.assertTrue(control.name.startswith(f"pb-probe-control-{task.public_id}-"), control.name)
            self.assertNotIn(self.INSTANCE, workspace.name)
            self.assertEqual([".git"], [entry.name for entry in workspace.iterdir()])
            self.assertEqual([], list(control.iterdir()))

    def test_a_cleanroom_workspace_is_left_for_the_cleanroom_to_fill(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace, control = make_probe_dirs(task_spec(self.INSTANCE), True, parent=Path(raw))
            self.assertEqual([], list(workspace.iterdir()))
            self.assertNotEqual(workspace, control)

    def test_a_synthetic_workspace_git_cannot_initialise_stops_the_probe(self) -> None:
        """run_agent.sh refuses a non-git repo root, so a failed init must not reach a session."""
        calls: list[str] = []

        def runner(command: str, _workspace: Path, _env, _timeout: int) -> int:
            calls.append(command)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            blocker = Path(raw) / "a-file"
            blocker.write_text("", encoding="utf-8")
            out = Path(raw) / "out"
            with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(blocker / "repo")}):
                with self.assertRaises(subprocess.CalledProcessError):
                    _probe(self.INSTANCE, out, budget_seconds=30, runner=runner, cleanroom=False)
            self.assertEqual([], calls)
            self.assertFalse((out / PROBE_FILENAME).exists())

    def test_the_session_runs_in_the_workspace_the_factory_made(self) -> None:
        seen: list[Path] = []

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            seen.append(workspace)
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            run_probe(
                self.INSTANCE,
                tmp / "probe",
                repo_root=REPO_ROOT,
                budget_seconds=30,
                runner=runner,
                cleanroom=False,
                workspace_factory=functools.partial(make_probe_dirs, parent=tmp),
            )
            [workspace] = seen
            self.assertEqual(tmp, workspace.parent)
            self.assertTrue((workspace / ".git").is_dir())


class TimelineTests(unittest.TestCase):
    """The pure reading of an event stream: what happened, when, after how much prose."""

    def test_tool_activity_is_timed_from_the_first_event_and_counts_the_prose_before_it(self) -> None:
        events = [
            {"type": "system", "subtype": "init", "timestamp": 1000},
            {"type": "text", "text": "thinking", "timestamp": 1100},
            {"type": "text", "text": "aloud", "timestamp": 1200},
            {"type": "tool_use", "name": "shell", "timestamp": 2349},
            {"type": "tool_result", "name": "shell", "is_error": False, "timestamp": 2400},
        ]
        self.assertEqual(
            [
                {"at_seconds": 0.0, "type": "system", "text_deltas_before": 0, "subtype": "init"},
                {"at_seconds": 1.3, "type": "tool_use", "text_deltas_before": 2, "name": "shell"},
                {"at_seconds": 1.4, "type": "tool_result", "text_deltas_before": 2, "name": "shell", "is_error": False},
            ],
            timeline(events),
        )

    def test_an_event_without_a_timestamp_counts_from_zero(self) -> None:
        """Pins today's behaviour: a missing timestamp counts as 0 and so sets the start to 0.

        With epoch-millisecond timestamps that would make every offset the epoch time; the
        streams the probe has archived carry a timestamp on every event.
        """
        # 1050 ms sits on a rounding edge on purpose: 1.05 rounds to 1.1 (the double is just
        # above 1.05), while a start moved by one millisecond gives 1.049, which rounds to 1.0.
        events = [{"type": "system"}, {"type": "tool_use", "tool": "file_write", "timestamp": 1050}]
        self.assertEqual([(0.0, None), (1.1, "file_write")], [(e["at_seconds"], e.get("tool")) for e in timeline(events)])

    def test_no_events_is_no_timeline(self) -> None:
        self.assertEqual([], timeline([]))
        self.assertEqual([], artifact_mentions([]))

    def test_the_session_id_is_the_first_one_the_stream_names(self) -> None:
        from automation.benchmark.probe import stream_session_id

        self.assertEqual(
            "abc", stream_session_id([{"type": "system", "session_id": ""}, {"session_id": "abc"}, {"session_id": "z"}])
        )
        self.assertIsNone(stream_session_id([{"type": "system"}]))


# Columns as hermes_state_common.py declares them for session_model_usage.
LEDGER_COLUMNS = (
    "session_id",
    "model",
    "billing_provider",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
)


def _write_ledger(hermes_home: Path, rows: list[tuple]) -> None:
    """Hermes' session_model_usage table, in a real sqlite file, holding ``rows``."""
    from automation.benchmark.probe import LEDGER_TABLE, SESSION_DB_FILENAME

    columns = LEDGER_COLUMNS
    hermes_home.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(hermes_home / SESSION_DB_FILENAME)) as conn:
        conn.execute(f"CREATE TABLE {LEDGER_TABLE} ({', '.join(columns)})")
        conn.executemany(f"INSERT INTO {LEDGER_TABLE} VALUES ({', '.join('?' * len(columns))})", rows)
        conn.commit()


class LedgerTests(unittest.TestCase):
    """Hermes' own usage table, read from a real sqlite file."""

    SESSION_ID = "sess-1"

    def test_routes_are_summed_and_named_after_the_busiest_one(self) -> None:
        from automation.benchmark.probe import ledger_usage

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            # The busiest route is not the first row, and a NULL counter reads as nothing spent.
            _write_ledger(
                home,
                [
                    (self.SESSION_ID, "fallback/model", "openrouter", None, 50, 5, None, None, 5, None),
                    (self.SESSION_ID, "z-ai/glm-5.3", "nvidia", 1, 100, 10, 7, None, 30, 0.25),
                    ("another-session", "other/model", "other", 9, 9, 9, 9, 9, 9, 9.0),
                ],
            )
            self.assertEqual(
                {
                    "input_tokens": 150,
                    "output_tokens": 15,
                    "cache_read_tokens": 7,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 35,
                    "api_calls": 1,
                    "estimated_cost_usd": 0.25,
                    "model": "z-ai/glm-5.3",
                    "provider": "nvidia",
                    "session_id": self.SESSION_ID,
                    "routes": 2,
                },
                ledger_usage(home, self.SESSION_ID),
            )

    def test_a_database_without_the_ledger_table_is_reported_not_raised(self) -> None:
        from automation.benchmark.probe import SESSION_DB_FILENAME, ledger_usage

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            with contextlib.closing(sqlite3.connect(home / SESSION_DB_FILENAME)) as conn:
                conn.execute("CREATE TABLE something_else (x)")
                conn.commit()
            self.assertEqual(
                {"error": "OperationalError: no such table: session_model_usage"},
                ledger_usage(home, self.SESSION_ID),
            )

    def test_session_accounting_needs_both_the_stream_s_session_and_the_receipt_s_home(self) -> None:
        from automation.benchmark.probe import CONTROLS_SUFFIX, session_accounting

        with tempfile.TemporaryDirectory() as raw:
            sessions = Path(raw) / "sessions"
            sessions.mkdir()
            self.assertEqual((None, None), session_accounting(sessions))

            (sessions / f"turn{STREAM_JSON_SUFFIX}").write_text(
                json.dumps({"type": "system", "session_id": self.SESSION_ID}) + "\n", encoding="utf-8"
            )
            self.assertEqual((self.SESSION_ID, None), session_accounting(sessions))

            receipt = sessions / f"turn{CONTROLS_SUFFIX}"
            receipt.write_text("{not json", encoding="utf-8")
            self.assertEqual((self.SESSION_ID, None), session_accounting(sessions))

            receipt.write_text(json.dumps({"hermes_home": ""}), encoding="utf-8")
            self.assertEqual((self.SESSION_ID, None), session_accounting(sessions))

            home = Path(raw) / "home"
            _write_ledger(home, [(self.SESSION_ID, "m", "p", 1, 1, 1, 0, 0, 0, 0.0)])
            receipt.write_text(json.dumps({"hermes_home": str(home)}), encoding="utf-8")
            session_id, usage = session_accounting(sessions)
            assert usage is not None
            self.assertEqual((self.SESSION_ID, "m", 1), (session_id, usage["model"], usage["routes"]))


class AttributionTests(unittest.TestCase):
    """What was asked for against what the ledger billed."""

    def test_no_accounting_compares_nothing(self) -> None:
        from automation.benchmark.probe import attribution_error

        self.assertIsNone(attribution_error(None, "m", "p"))
        self.assertIsNone(attribution_error({}, "m", "p"))
        self.assertIsNone(attribution_error({"error": "OperationalError: locked", "model": "x"}, "m", "p"))

    def test_each_mismatch_is_named(self) -> None:
        from automation.benchmark.probe import attribution_error

        usage = {"model": "m", "provider": "nvidia", "routes": 1}
        self.assertIsNone(attribution_error(usage, "m", "nvidia"))
        self.assertEqual("asked for model 'other', ledger says 'm'", attribution_error(usage, "other", "nvidia"))
        self.assertEqual("asked for provider 'openrouter', ledger says 'nvidia'", attribution_error(usage, "m", "openrouter"))
        self.assertEqual(
            "2 model routes in one session, so the counts are a blend",
            attribution_error({**usage, "routes": 2}, "m", "nvidia"),
        )
        # A --usage-file report carries no route count, and is not a blend for lacking one.
        self.assertIsNone(attribution_error({"model": "m", "provider": "nvidia"}, "m", ""))


def _record(**overrides: object) -> dict:
    """A probe record as run_probe writes it, with nothing in it, for the summary's sake."""
    record: dict = {
        "instance_id": "owner__proj.abc1234",
        "phase": "research",
        "transport": "stream-json",
        "max_turns": None,
        "prompt_chars": 100,
        "budget_seconds": 600,
        "phase_result": {"returncode": 0, "state": "completed", "seconds": 12.0},
        "stream": {"events": 0, "text_deltas": 0, "file": None, "timeline": [], "artifact_mentions": [], "terminal_result": None},
        "artifact": {"filename": "research.json", "present": False, "chars": 0, "schema_errors": ["absent"]},
        "cleanroom": None,
        "usage": None,
        "usage_source": None,
        "elapsed_seconds": None,
        "attribution": {"asked_model": None, "asked_provider": None, "error": None},
        "sandbox": {"observation": None, "violations": []},
        "rpi": {"copied": [], "skipped_links": [], "skipped_non_files": [], "container_skipped": False},
        "diagnosis": [],
    }
    record.update(overrides)
    return record


def _entry(seconds: float, name: str) -> dict:
    return {"at_seconds": seconds, "type": "tool_use", "text_deltas_before": 0, "name": name}


class SummaryTests(unittest.TestCase):
    """The operator's answer, line by line."""

    def assertBlock(self, block: list[str], summary: str) -> None:
        lines = summary.splitlines()
        for start in range(len(lines) - len(block) + 1):
            if lines[start : start + len(block)] == block:
                return
        self.fail(f"{block!r} is not a run of lines in:\n{summary}")

    def test_an_empty_record_says_what_is_missing(self) -> None:
        self.assertEqual(
            [
                "instance      owner__proj.abc1234",
                "phase         research via stream-json, 100 prompt chars",
                "ended         rc 0 (completed) after 12s of 600s",
                "stream        0 events (0 text deltas) in no stream file",
                "artifact      ABSENT (0 chars, schema ['absent'])",
                "sandbox       NO CLEANROOM: synthetic repo, local backend - not a cell's environment",
                "accounting    NO USAGE REPORT: no report and no ledger row, so no token counts to read",
                "write attempt none",
            ],
            summarize(_record()).splitlines(),
        )

    def test_a_turn_cap_is_named_in_the_header(self) -> None:
        self.assertIn("phase         research via stream-json, max 1 turn(s), 100 prompt chars", summarize(_record(max_turns=1)))

    def test_a_usage_report_prints_its_tokens_its_ending_and_its_time(self) -> None:
        usage = {
            "input_tokens": 1,
            "output_tokens": 2,
            "reasoning_tokens": 3,
            "api_calls": 0,
            "model": "m",
            "provider": "p",
            "completed": False,
        }
        self.assertBlock(
            [
                "accounting    in 1 / out 2 / reasoning 3 tokens over 0 api calls",
                "              m on p, per usage.json",
                "              completed=False partial=None interrupted=None exit_reason=None",
                "              12s wall, 12s per api call",
            ],
            summarize(_record(usage=usage, usage_source="usage.json", elapsed_seconds=12.0)),
        )

    def test_a_ledger_row_has_no_ending_to_print_and_one_route_is_not_a_blend(self) -> None:
        usage = {
            "input_tokens": 1,
            "output_tokens": 2,
            "reasoning_tokens": 3,
            "api_calls": 4,
            "model": "m",
            "provider": "p",
            "routes": 1,
        }
        self.assertBlock(
            [
                "accounting    in 1 / out 2 / reasoning 3 tokens over 4 api calls",
                "              m on p, per state.db:session_model_usage",
                "write attempt none",
            ],
            summarize(_record(usage=usage, usage_source="state.db:session_model_usage")),
        )

    def test_skipped_entries_are_printed_escaped(self) -> None:
        rpi = {"copied": [], "skipped_links": ["a\nb", "c"], "skipped_non_files": ["d"], "container_skipped": True}
        self.assertBlock(
            [
                "skipped links .rpi itself is a symlink, so nothing under it was collected",
                "skipped links 'a\\nb', 'c' (links in .rpi, not followed)",
                "skipped dirs  'd' (not regular files in .rpi)",
            ],
            summarize(_record(rpi=rpi)),
        )

    def test_at_most_ten_write_attempts_are_listed_and_the_fallback_stays_quiet(self) -> None:
        mentions = [_entry(float(i), f"tool{i}") for i in range(11)]
        stream = {**_record()["stream"], "timeline": mentions, "artifact_mentions": mentions}
        summary = summarize(_record(stream=stream))
        self.assertIn("write attempt 11 events name research.json", summary.splitlines())
        self.assertEqual(
            [f"  at {float(i):>7.1f}s  tool_use tool{i}" for i in range(10)],
            [line for line in summary.splitlines() if line.startswith("  at ")],
        )
        self.assertNotIn("first tool activity:", summary)

    def test_without_a_write_attempt_the_first_ten_tool_events_are_listed(self) -> None:
        entries = [_entry(float(i), f"tool{i}") for i in range(11)]
        summary = summarize(_record(stream={**_record()["stream"], "timeline": entries}))
        self.assertBlock(["write attempt none", "first tool activity:"], summary)
        self.assertEqual(
            [f"  at {float(i):>7.1f}s  tool_use tool{i}" for i in range(10)],
            [line for line in summary.splitlines() if line.startswith("  at ")],
        )

    def test_without_any_tool_activity_there_is_no_fallback_heading(self) -> None:
        self.assertNotIn("first tool activity:", summarize(_record()))


class ProbeRunEdgeTests(unittest.TestCase):
    """run_probe's refusals and what it records, with a stand-in session and real directories."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"
    RESEARCH = {
        "task": "rebuild cmatrix",
        "relevant_files": ["README.md"],
        "findings": ["curses"],
        "constraints": [],
        "unknowns": [],
        "risks": [],
        "evidence": [],
    }

    @staticmethod
    def _quiet(_command: str, _workspace: Path, env, _timeout: int) -> int:
        Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
        return 0

    def _research(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "research.json"
        path.write_text(json.dumps(self.RESEARCH), encoding="utf-8")
        return path

    def test_an_unknown_phase_is_refused_before_the_run_directory_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            with self.assertRaises(ValueError) as refusal:
                _probe(self.INSTANCE, out, cleanroom=False, runner=self._quiet, phase="implement")
            self.assertIn("unknown phase 'implement'", str(refusal.exception))
            self.assertFalse(out.exists())

    def test_research_that_is_not_json_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            research = Path(raw) / "research.json"
            research.write_text("{truncated", encoding="utf-8")
            with self.assertRaises(ValueError) as refusal:
                _probe(
                    self.INSTANCE,
                    Path(raw) / "probe",
                    cleanroom=False,
                    runner=self._quiet,
                    phase="plan",
                    research_artifact=research,
                )
            self.assertEqual(f"{research} is not JSON", str(refusal.exception))

    def test_only_regular_observed_captures_are_seeded_into_plan(self) -> None:
        seen: dict = {}

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            observed = workspace / ".rpi" / "observed"
            seen["observed"] = {p.name: p.read_text(encoding="utf-8") for p in observed.iterdir()}
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            research = self._research(tmp / "run")
            observed = tmp / "run" / "observed"
            (observed / "deeper").mkdir(parents=True)
            (observed / "usage_h.txt").write_text("Usage: cmatrix", encoding="utf-8")
            (observed / "version.txt").write_text("cmatrix 2.0", encoding="utf-8")
            (tmp / "host.txt").write_text("host, not research's\n", encoding="utf-8")
            (observed / "linked.txt").symlink_to(tmp / "host.txt")

            record = _probe(
                self.INSTANCE, tmp / "probe", cleanroom=False, runner=runner, phase="plan", research_artifact=research
            )

        self.assertEqual({"usage_h.txt": "Usage: cmatrix", "version.txt": "cmatrix 2.0"}, seen["observed"])
        self.assertEqual(["usage_h.txt", "version.txt"], record["research_input"]["observed"])

    def test_a_linked_observed_directory_is_not_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            research = self._research(tmp / "run")
            elsewhere = tmp / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "id_rsa").write_text("host\n", encoding="utf-8")
            (tmp / "run" / "observed").symlink_to(elsewhere, target_is_directory=True)

            record = _probe(
                self.INSTANCE, tmp / "probe", cleanroom=False, runner=self._quiet, phase="plan", research_artifact=research
            )

        self.assertEqual([], record["research_input"]["observed"])

    def test_plan_is_seeded_even_where_the_cleanroom_already_made_the_artifact_directory(self) -> None:
        def prepare_with_rpi(instance_id: str, workspace: Path, repository: str):
            receipt = _fake_prepare(instance_id, workspace, repository)
            (workspace / ".rpi").mkdir()
            return receipt

        seen: dict = {}

        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            seen["research"] = json.loads((workspace / ".rpi" / "research.json").read_text(encoding="utf-8"))
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _probe(
                self.INSTANCE,
                tmp / "probe",
                runner=runner,
                phase="plan",
                research_artifact=self._research(tmp / "run"),
                prepare=prepare_with_rpi,
                prove_sandbox=lambda *a, **k: None,
                witness_factory=lambda *a, **k: StubWitness(),
            )
        self.assertEqual(self.RESEARCH, seen["research"])

    def test_a_run_directory_may_be_nested_or_already_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            nested = tmp / "runs" / "today" / "probe"
            existing = tmp / "existing"
            existing.mkdir()
            for out in (nested, existing):
                with self.subTest(out=out.name):
                    record = _probe(
                        self.INSTANCE,
                        out,
                        cleanroom=False,
                        runner=self._quiet,
                        workspace_factory=functools.partial(make_probe_dirs, parent=tmp),
                    )
                    self.assertTrue((out / "probe.json").is_file())
                    self.assertEqual(PHASE_CEILING_SECONDS["research"], record["budget_seconds"])

    def test_the_stream_counts_its_text_deltas(self) -> None:
        events = [
            {"type": "system", "subtype": "init", "timestamp": 0},
            {"type": "text", "text": "a", "timestamp": 1},
            {"type": "tool_use", "name": "shell", "timestamp": 2},
            {"type": "text", "text": "b", "timestamp": 3},
            {"type": "text", "text": "c", "timestamp": 4},
        ]

        def runner(_command: str, _workspace: Path, env, _timeout: int) -> int:
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / f"turn{STREAM_JSON_SUFFIX}").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            return 0

        with tempfile.TemporaryDirectory() as raw:
            record = _probe(self.INSTANCE, Path(raw) / "probe", cleanroom=False, runner=runner)
        self.assertEqual((5, 3), (record["stream"]["events"], record["stream"]["text_deltas"]))

    def test_an_unreadable_stream_reads_as_no_events(self) -> None:
        def runner(_command: str, _workspace: Path, env, _timeout: int) -> int:
            (Path(env["BOUNDROOM_HERMES_USAGE_DIR"]) / f"turn{STREAM_JSON_SUFFIX}").mkdir(parents=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            record = _probe(self.INSTANCE, Path(raw) / "probe", cleanroom=False, runner=runner)
        self.assertEqual(
            (f"turn{STREAM_JSON_SUFFIX}", 0, []),
            (record["stream"]["file"], record["stream"]["events"], record["stream"]["timeline"]),
        )

    def test_the_wall_clock_is_recorded_to_a_tenth_of_a_second(self) -> None:
        def runner(command: str, workspace: Path, env, timeout: int) -> int:
            time.sleep(0.06)
            return self._quiet(command, workspace, env, timeout)

        with tempfile.TemporaryDirectory() as raw:
            record = _probe(self.INSTANCE, Path(raw) / "probe", cleanroom=False, runner=runner)
        elapsed = record["elapsed_seconds"]
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertEqual(round(elapsed, 1), elapsed)

    @unittest.skipIf(os.geteuid() == 0, "root reads a file without its read bit")
    def test_an_artifact_that_cannot_be_read_stops_the_collection_and_leaves_no_partial_copy(self) -> None:
        def runner(_command: str, workspace: Path, env, _timeout: int) -> int:
            rpi = workspace / ".rpi"
            rpi.mkdir()
            (rpi / "research.json").write_text("{}", encoding="utf-8")
            (rpi / "secret").write_text("x", encoding="utf-8")
            (rpi / "secret").chmod(0)
            Path(env["BOUNDROOM_HERMES_USAGE_DIR"]).mkdir(parents=True, exist_ok=True)
            return 0

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "probe"
            with self.assertRaises(PermissionError):
                _probe(self.INSTANCE, out, cleanroom=False, runner=runner)
            self.assertFalse((out / "rpi").exists())
            self.assertFalse((out / "probe.json").exists())


class ProbeMainTests(unittest.TestCase):
    """The CLI: its exit code is the verdict on attribution, and the record is already on disk."""

    INSTANCE = "abishekvashok__cmatrix.5c082c6"

    def _main(self, tmp: Path, served: str, *extra: str) -> tuple[int, str, dict]:
        seen: dict = {}

        def runner(command: str, _workspace: Path, env, timeout: int) -> int:
            seen.update(env=dict(env), timeout=timeout, command=command)
            sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "turn.usage.json").write_text(json.dumps({"model": served, "api_calls": 1}), encoding="utf-8")
            return 0

        argv = [
            "--instance",
            self.INSTANCE,
            "--out-dir",
            str(tmp / "probe"),
            "--no-cleanroom",
            "--agent-env",
            "HERMES_INFERENCE_MODEL",
            "--budget",
            "30",
            *extra,
        ]
        printed = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {"HERMES_INFERENCE_MODEL": "z-ai/glm-5.3"}),
            contextlib.redirect_stdout(printed),
        ):
            code = main(argv, runner=runner, workspace_factory=functools.partial(make_probe_dirs, parent=tmp))
        return code, printed.getvalue(), seen

    def test_the_model_that_was_asked_for_exits_0_and_prints_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            code, printed, seen = self._main(Path(raw), "z-ai/glm-5.3", "--max-turns", "1")
            record = json.loads((Path(raw) / "probe" / "probe.json").read_text(encoding="utf-8"))
        self.assertEqual(0, code)
        self.assertEqual(f"instance      {self.INSTANCE}", printed.splitlines()[0])
        self.assertEqual("z-ai/glm-5.3", seen["env"]["HERMES_INFERENCE_MODEL"])
        self.assertEqual("1", seen["env"]["BOUNDROOM_HERMES_MAX_TURNS"])
        self.assertEqual((30, 1, None), (record["budget_seconds"], record["max_turns"], record["attribution"]["error"]))

    def test_a_model_nobody_asked_for_exits_1_with_the_record_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            code, printed, _seen = self._main(Path(raw), "nvidia/nemotron", "--transport", "oneshot")
            record = json.loads((Path(raw) / "probe" / "probe.json").read_text(encoding="utf-8"))
        self.assertEqual(1, code)
        self.assertIn("WRONG MODEL", printed)
        self.assertEqual("oneshot", record["transport"])
        self.assertIn("ledger says 'nvidia/nemotron'", record["attribution"]["error"])

    def test_the_plan_phase_takes_its_research_from_the_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            research = Path(raw) / "research.json"
            research.write_text(json.dumps(ProbeRunEdgeTests.RESEARCH), encoding="utf-8")
            code, _printed, _seen = self._main(Path(raw), "z-ai/glm-5.3", "--phase", "plan", "--research-artifact", str(research))
            record = json.loads((Path(raw) / "probe" / "probe.json").read_text(encoding="utf-8"))
        self.assertEqual(0, code)
        self.assertEqual(("plan", str(research)), (record["phase"], record["research_input"]["path"]))

    def test_bad_arguments_are_a_usage_error(self) -> None:
        cases = {
            "unknown phase": ["--instance", self.INSTANCE, "--out-dir", "o", "--phase", "implement"],
            "no instance": ["--out-dir", "o"],
            "no run directory": ["--instance", self.INSTANCE],
        }
        for name, argv in cases.items():
            with self.subTest(name), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
                main(argv)
            self.assertEqual(2, stop.exception.code)

    def test_the_harness_root_defaults_to_this_checkout(self) -> None:
        from automation.benchmark.probe import build_parser

        self.assertEqual(REPO_ROOT, build_parser().parse_args(["--instance", self.INSTANCE, "--out-dir", "o"]).repo_root)


if __name__ == "__main__":
    unittest.main()
