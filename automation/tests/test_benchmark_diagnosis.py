"""The session diagnosis's shell, against what the pinned Hermes really leaves on disk.

Each case builds the files one session would leave - controls receipt, log, usage report,
stream, witness receipt, activity snapshot - plus a ``state.db`` with the three tables the
diagnosis reads, at the columns pinned Hermes ``b6a87db`` declares them with. What the evidence
can decide is the pure core's job and is tested in ``test_benchmark_diagnosis_core``; here are
the reads, the snapshot the adapter takes before a budget kill, and whole sessions end to end.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from automation.benchmark import diagnosis
from automation.benchmark.adapter import AGENT_SESSIONS_DIR, SubmissionResult
from automation.benchmark.instances import TaskSpec
from automation.benchmark.strategies import PhaseResult, StrategyResult

# 2026-10-12T12:56:44Z, the launch second of run 38248084765's plan session.
LAUNCH = 1791809804.0
STEM = "20261012T125644Z-42025"
RESEARCH_STEM = "20261012T122043Z-3852"

_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
    ended_at REAL, end_reason TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    api_call_count INTEGER DEFAULT 0, last_activity_at REAL, last_activity_description TEXT,
    last_activity_provenance TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
    finish_reason TEXT);
CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    first_seen REAL, last_seen REAL);
"""


def phase(returncode: int, seconds: float, artifact_valid: Optional[bool], name: str = "plan") -> dict[str, Any]:
    return PhaseResult(
        phase=name, returncode=returncode, seconds=seconds, prompt_chars=1, artifact_valid=artifact_valid
    ).to_dict()


class Session:
    """One session's leftovers, written the way run_agent.sh and Hermes write them."""

    def __init__(self, root: Path, stem: str = STEM, phase_name: str = "plan", transport: str = "oneshot") -> None:
        self.dir = root / AGENT_SESSIONS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stem = stem
        launched = diagnosis.launch_time(stem)[0]
        assert launched is not None, f"fixture stem {stem!r} carries no launch time"
        self.launch: float = launched
        self.home = root / f"hermes-home-{stem}"
        self.home.mkdir()
        self.controls = {
            "runner": "hermes",
            "transport": transport,
            "slice_id": f"task-1a2b:{phase_name}",
            "hermes_home": str(self.home),
        }
        self.file(".controls.json", json.dumps(self.controls))
        self.file(".log", "")

    def file(self, suffix: str, text: str) -> None:
        (self.dir / f"{self.stem}{suffix}").write_text(text, encoding="utf-8")

    def ledger(
        self,
        *,
        row: bool = True,
        calls: int = 0,
        messages: tuple[tuple[str, float, bool], ...] = (),
        activity: Optional[tuple[float, str]] = None,
    ) -> None:
        """``messages`` are (role, seconds after launch, carries tool_calls); ``activity`` is
        (seconds after launch, the persisted activity description)."""
        at, description = activity or (3, None)
        with contextlib.closing(sqlite3.connect(self.home / "state.db")) as conn, conn:
            conn.executescript(_SCHEMA)
            if row:
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, api_call_count, last_activity_at,"
                    " last_activity_description, last_activity_provenance) VALUES (?,?,?,?,?,?,?)",
                    ("s1", "cli", self.launch + 3, calls, self.launch + at, description, "unknown" if description else None),
                )
            if calls:
                conn.execute(
                    "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                    ("s1", "z-ai/glm-5.3", "nvidia", calls, 900, 40, self.launch + 60, self.launch + 60 * calls),
                )
            for role, at, has_calls in messages:
                conn.execute(
                    "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)",
                    ("s1", role, '[{"name": "terminal"}]' if has_calls else None, self.launch + at),
                )

    def witness(self, *created_at: str) -> None:
        containers = [{"task_id": f"t{i}", "container_id": f"c{i}", "created_at": at} for i, at in enumerate(created_at)]
        self.file(".sandbox.json", json.dumps({"observed": True, "containers": containers}))


def old_store(path: Path) -> None:
    """A ``state.db`` from before ``sessions.last_activity_description`` existed."""
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT, started_at REAL, ended_at REAL, end_reason TEXT,"
            " api_call_count INTEGER, message_count INTEGER, tool_call_count INTEGER, last_activity_at REAL)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER, role TEXT, timestamp REAL, tool_name TEXT, finish_reason TEXT, tool_calls TEXT)"
        )
        conn.execute(
            "CREATE TABLE session_model_usage (model TEXT, billing_provider TEXT, api_call_count INTEGER,"
            " reasoning_tokens INTEGER, output_tokens INTEGER, first_seen REAL, last_seen REAL)"
        )
        conn.execute("INSERT INTO sessions VALUES ('s1', ?, NULL, NULL, 0, 1, 0, ?)", (LAUNCH + 3, LAUNCH + 3))


class GatherEvidenceTests(unittest.TestCase):
    """Every read the diagnosis makes, into one ``SessionEvidence``."""

    def test_every_source_a_session_left_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), transport="stream-json")
            session.ledger(calls=1, messages=(("assistant", 60, True),))
            session.file(".log", "turn\n")
            session.file(".usage.json", json.dumps({"model": "z-ai/glm-5.3", "api_calls": 1}))
            # A bad line between two good ones is skipped, not the end; JSON that is not an
            # event is skipped; a killed session's half-written last line is dropped.
            session.file(
                ".stream.jsonl",
                '{"type": "system"}\nnot json\n{"type": "text"}\n[1, 2]\n3\n{"type": "te',
            )
            session.witness("2026-10-12T12:57:30Z")
            session.file(".activity.json", json.dumps({"captured_at": LAUNCH + 1080, "row": None}))
            store = {"credential_pool": {"nvidia": [{"source": "env:NVIDIA_API_KEY", "last_status": None}]}}
            (session.home / diagnosis.AUTH_STORE_FILENAME).write_text(json.dumps(store), encoding="utf-8")
            evidence = diagnosis.gather_evidence(session.dir, STEM)
            ledger = diagnosis.read_ledger(str(session.home))
        self.assertTrue(ledger["available"])
        self.assertEqual(
            diagnosis.SessionEvidence(
                stem=STEM,
                controls_present=True,
                transport="stream-json",
                slice_id="task-1a2b:plan",
                usage_report_present=True,
                log_bytes=5,
                events=[{"type": "system"}, {"type": "text"}],
                ledger=ledger,
                sandbox_receipt={
                    "observed": True,
                    "containers": [{"task_id": "t0", "container_id": "c0", "created_at": "2026-10-12T12:57:30Z"}],
                },
                snapshot={"captured_at": LAUNCH + 1080, "row": None},
                credential_pool=[
                    {"provider": "nvidia", "source": "env:NVIDIA_API_KEY", "last_status": None, "last_error_code": None}
                ],
            ),
            evidence,
        )

    def test_a_session_that_left_nothing_but_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = diagnosis.gather_evidence(Path(tmp), STEM)
        self.assertEqual(
            diagnosis.SessionEvidence(
                stem=STEM,
                controls_present=False,
                transport=None,
                slice_id="",
                usage_report_present=False,
                log_bytes=None,
                events=None,
                ledger={"available": False, "reason": "controls receipt names no hermes_home"},
                sandbox_receipt=None,
                snapshot=None,
                credential_pool=None,
            ),
            evidence,
        )

    def test_an_empty_file_is_not_an_absent_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.file(".stream.jsonl", "")
            empty = diagnosis.gather_evidence(session.dir, STEM)
            (session.dir / f"{STEM}.log").unlink()
            session.file(".stream.jsonl", "not json\n")
            absent = diagnosis.gather_evidence(session.dir, STEM)
        self.assertEqual(((0, []), (None, [])), ((empty.log_bytes, empty.events), (absent.log_bytes, absent.events)))

    def test_a_stream_that_was_never_written_is_not_an_empty_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.file(".stream.jsonl", "")
            empty = diagnosis.gather_evidence(session.dir, STEM).events
            (session.dir / f"{STEM}.stream.jsonl").unlink()
            absent = diagnosis.gather_evidence(session.dir, STEM).events
        self.assertEqual(([], None), (empty, absent))

    def test_a_controls_receipt_without_a_slice_id_reads_as_an_empty_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.file(".controls.json", json.dumps({"slice_id": None}))
            evidence = diagnosis.gather_evidence(session.dir, STEM)
        self.assertEqual(("", True, None), (evidence.slice_id, evidence.controls_present, evidence.transport))

    def test_a_symlinked_receipt_is_followed(self) -> None:
        """``_load_json`` follows symlinks; ``judge.py`` imports it, so that is pinned, not changed."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            target = elsewhere / "controls.json"
            target.write_text(json.dumps(session.controls), encoding="utf-8")
            link = session.dir / f"{STEM}.controls.json"
            link.unlink()
            link.symlink_to(target)
            loaded = diagnosis._load_json(link)
            evidence = diagnosis.gather_evidence(session.dir, STEM)
        self.assertEqual(session.controls, loaded)
        self.assertEqual((True, "oneshot", "task-1a2b:plan"), (evidence.controls_present, evidence.transport, evidence.slice_id))

    def test_unreadable_json_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{", encoding="utf-8")
            self.assertEqual((None, None), (diagnosis._load_json(path), diagnosis._load_json(Path(tmp) / "absent.json")))


class MalformedFileTests(unittest.TestCase):
    """A receipt of the wrong shape fails the diagnosis loudly, as it always has."""

    def test_controls_that_are_not_an_object_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.file(".controls.json", "[1]")
            with self.assertRaisesRegex(AttributeError, "'list' object has no attribute 'get'"):
                diagnosis.diagnose_session(session.dir, STEM)
            with self.assertRaisesRegex(AttributeError, "'list' object has no attribute 'get'"):
                diagnosis.diagnose_sessions(session.dir, [])

    def test_a_snapshot_that_is_not_an_object_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.file(".activity.json", "[1]")
            with self.assertRaisesRegex(AttributeError, "'list' object has no attribute 'get'"):
                diagnosis.diagnose_session(session.dir, STEM)


class ReadLedgerTests(unittest.TestCase):
    def test_each_reason_the_ledger_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            gone = Path(tmp) / "gone"
            corrupt = Path(tmp) / "corrupt"
            for directory in (home, corrupt):
                directory.mkdir()
            (corrupt / "state.db").write_bytes(b"not a database, not even close" * 100)
            cases = [
                ("no home named", None, "controls receipt names no hermes_home"),
                ("an empty home named", "", "controls receipt names no hermes_home"),
                ("a home that is gone", str(gone), f"{gone} is gone, so its state.db cannot be read"),
                ("no store in the home", str(home), f"{home} exists but Hermes never created state.db in it"),
                ("a store that is not a database", str(corrupt), "DatabaseError: file is not a database"),
            ]
            for name, hermes_home, reason in cases:
                with self.subTest(name):
                    self.assertEqual({"available": False, "reason": reason}, diagnosis.read_ledger(hermes_home))

    def test_the_three_tables_timestamps_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(
                calls=2, messages=(("assistant", 30, True), ("tool", 40, False)), activity=(50, "receiving stream response")
            )
            with contextlib.closing(sqlite3.connect(session.home / "state.db")) as conn, conn:
                for at, calls in ((41, ""), (42, "[]"), (43, "null")):
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp, finish_reason)"
                        " VALUES ('s1', 'assistant', 'SECRET-PROSE', ?, 'terminal', ?, 'stop')",
                        (calls, LAUNCH + at),
                    )
            ledger = diagnosis.read_ledger(str(session.home))
        no_calls = {"role": "assistant", "tool_name": "terminal", "finish_reason": "stop", "has_tool_calls": 0}
        self.assertEqual(
            {
                "available": True,
                "sessions": [
                    {
                        "id": "s1",
                        "started_at": LAUNCH + 3,
                        "ended_at": None,
                        "end_reason": None,
                        "api_call_count": 2,
                        "message_count": 0,
                        "tool_call_count": 0,
                        "last_activity_at": LAUNCH + 50,
                        "last_activity_description": "receiving stream response",
                        "last_activity_provenance": "unknown",
                    }
                ],
                "messages": [
                    {
                        "role": "assistant",
                        "timestamp": LAUNCH + 30,
                        "tool_name": None,
                        "finish_reason": None,
                        "has_tool_calls": 1,
                    },
                    {"role": "tool", "timestamp": LAUNCH + 40, "tool_name": None, "finish_reason": None, "has_tool_calls": 0},
                    {**no_calls, "timestamp": LAUNCH + 41},
                    {**no_calls, "timestamp": LAUNCH + 42},
                    {**no_calls, "timestamp": LAUNCH + 43},
                ],
                "usage": [
                    {
                        "model": "z-ai/glm-5.3",
                        "billing_provider": "nvidia",
                        "api_call_count": 2,
                        "reasoning_tokens": 900,
                        "output_tokens": 40,
                        "first_seen": LAUNCH + 60,
                        "last_seen": LAUNCH + 120,
                    }
                ],
                "has_activity_description": True,
            },
            ledger,
        )

    def test_a_store_older_than_the_activity_column_is_read_without_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_store(Path(tmp) / "state.db")
            ledger = diagnosis.read_ledger(tmp)
        self.assertEqual(
            {
                "available": True,
                "sessions": [
                    {
                        "id": "s1",
                        "started_at": LAUNCH + 3,
                        "ended_at": None,
                        "end_reason": None,
                        "api_call_count": 0,
                        "message_count": 1,
                        "tool_call_count": 0,
                        "last_activity_at": LAUNCH + 3,
                    }
                ],
                "messages": [],
                "usage": [],
                "has_activity_description": False,
            },
            ledger,
        )


def pool_entry(source: str, requests: int, status: Optional[str] = None, code: Optional[int] = None, **extra: Any) -> dict:
    """One ``credential_pool`` row in the shape pinned Hermes (b6a87db) writes to auth.json."""
    return {
        "id": source[-6:],
        "label": source.split(":", 1)[-1],
        "auth_type": "api_key",
        "priority": 0,
        "source": source,
        "access_token": None,
        "request_count": requests,
        "last_status": status,
        "last_error_code": code,
        **extra,
    }


class CredentialPoolTests(unittest.TestCase):
    """A session the second key served must not look like one the first key served."""

    def _pool(self, store: Any) -> Optional[list[dict[str, Any]]]:
        with tempfile.TemporaryDirectory() as tmp:
            if store is not None:
                (Path(tmp) / diagnosis.AUTH_STORE_FILENAME).write_text(json.dumps(store), encoding="utf-8")
            return diagnosis.read_credential_pool(tmp)

    def test_a_rotation_shows_the_throttled_key_and_the_one_that_took_over(self) -> None:
        store = {
            "credential_pool": {
                "openrouter": [pool_entry("env:OPENROUTER_API_KEY", 1)],
                "nvidia": [
                    pool_entry("env:NVIDIA_API_KEY", 3, "exhausted", 429),
                    "not an entry",
                    pool_entry("env:NVIDIA_API_KEY_2", 5),
                ],
                "broken": {"source": "env:NOT_A_LIST"},
            }
        }
        self.assertEqual(
            [
                {"provider": "nvidia", "source": "env:NVIDIA_API_KEY", "last_status": "exhausted", "last_error_code": 429},
                {"provider": "nvidia", "source": "env:NVIDIA_API_KEY_2", "last_status": None, "last_error_code": None},
                {"provider": "openrouter", "source": "env:OPENROUTER_API_KEY", "last_status": None, "last_error_code": None},
            ],
            self._pool(store),
        )

    def test_nothing_but_names_and_counters_is_copied(self) -> None:
        """Hermes can label a row from a token's claims; neither the label nor a token leaves."""
        entry = pool_entry(
            "env:NVIDIA_API_KEY", 1, label="someone@example.com", access_token="nvapi-SECRET", refresh_token="r-SECRET"
        )
        recorded = json.dumps(self._pool({"credential_pool": {"nvidia": [entry]}}))
        for value in ("SECRET", "someone@example.com"):
            self.assertNotIn(value, recorded)

    def test_no_store_or_no_pool_is_none_not_an_empty_pool(self) -> None:
        for name, store in (
            ("no store", None),
            ("no pool", {"providers": {}}),
            ("a store that is a list", [1]),
            ("a pool that is a list", {"credential_pool": []}),
        ):
            with self.subTest(name):
                self.assertIsNone(self._pool(store))
        self.assertEqual((None, None), (diagnosis.read_credential_pool(None), diagnosis.read_credential_pool("")))


class ActivitySnapshotTests(unittest.TestCase):
    """The read the adapter takes just before SIGTERM, which Hermes answers by clearing the label."""

    def snapshot(self, session: Session) -> dict[str, Any]:
        return json.loads((session.dir / f"{session.stem}.activity.json").read_text(encoding="utf-8"))

    def test_the_session_launched_since_is_snapshotted_beside_its_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True, activity=(1025.0, "receiving stream response"))
            before = time.time()
            diagnosis.snapshot_activity(session.dir, frozenset())
            after = time.time()
            snapshot = self.snapshot(session)
        captured = snapshot.pop("captured_at")
        self.assertTrue(before <= captured <= after, (before, captured, after))
        self.assertEqual(
            {
                "row": {
                    "last_activity_at": LAUNCH + 1025,
                    "last_activity_description": "receiving stream response",
                    "last_activity_provenance": "unknown",
                }
            },
            snapshot,
        )

    def test_sessions_announced_before_launch_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True)
            diagnosis.snapshot_activity(session.dir, frozenset({STEM}))
            self.assertFalse((session.dir / f"{STEM}.activity.json").exists())

    def test_two_new_sessions_are_not_told_apart_so_neither_is_snapshotted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = [Session(Path(tmp), stem=RESEARCH_STEM), Session(Path(tmp))]
            for session in sessions:
                session.ledger(row=True)
            diagnosis.snapshot_activity(sessions[0].dir, frozenset())
            self.assertEqual([], sorted(path.name for path in sessions[0].dir.glob("*.activity.json")))

    def test_each_failure_is_written_down_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = [("no home named", None), ("no store in the home", "missing"), ("a store that is not a database", "corrupt")]
            for index, (name, kind) in enumerate(cases):
                with self.subTest(name):
                    session = Session(Path(tmp) / str(index))
                    if kind is None:
                        session.file(".controls.json", json.dumps({"transport": "oneshot"}))
                    elif kind == "corrupt":
                        (session.home / "state.db").write_bytes(b"not a database, not even close" * 100)
                    diagnosis.snapshot_activity(session.dir, frozenset())
                    snapshot = self.snapshot(session)
                    error = {
                        None: "FileNotFoundError: controls receipt names no HERMES_HOME",
                        "missing": f"FileNotFoundError: {session.home / 'state.db'} does not exist",
                        "corrupt": "DatabaseError: file is not a database",
                    }[kind]
                    self.assertEqual(["captured_at", "error"], sorted(snapshot))
                    self.assertEqual(error, snapshot["error"])

    def test_an_empty_sessions_table_is_a_null_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=False)
            diagnosis.snapshot_activity(session.dir, frozenset())
            snapshot = self.snapshot(session)
        self.assertEqual({"row": None}, {key: value for key, value in snapshot.items() if key != "captured_at"})

    def test_a_snapshot_that_cannot_be_written_never_raises_into_the_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True)
            (session.dir / f"{STEM}.activity.json").mkdir()
            self.assertIsNone(diagnosis.snapshot_activity(session.dir, frozenset()))
            self.assertTrue((session.dir / f"{STEM}.activity.json").is_dir())

    def test_the_budget_kill_snapshots_before_it_signals(self) -> None:
        """The real kill path, with a child that clears its label on SIGTERM the way Hermes does.

        The child's handler blanks ``last_activity_description`` and then drops a marker, and the
        test's ``killpg`` waits for that marker after delivering SIGTERM. So a snapshot taken after
        the signal reliably reads ``''``, and only one taken before it keeps the label.

        Under load a one-second budget can expire before the child has started at all, so the
        shell writes the controls receipt before it execs the child, the child installs its
        handler before anything else and then announces itself, and the test's ``Popen`` (the
        child-process boundary) returns only once that announcement exists. The budget starts
        after the child is ready, and the real ``snapshot_activity`` runs unwrapped. Nothing is
        left to a race.
        """
        from automation.benchmark import adapter

        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            database = home / "state.db"
            with contextlib.closing(sqlite3.connect(database)) as conn, conn:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, last_activity_at, last_activity_description)"
                    " VALUES ('s1', 'cli', 1, 2, 'stream retry 1/5 after ReadTimeout')"
                )
            cleared = Path(tmp) / "cleared"
            pid_file = Path(tmp) / "child.pid"
            controls = sessions / f"{STEM}.controls.json"

            def reap() -> None:
                """Never leave the child's group running, whichever assertion fails."""
                with contextlib.suppress(OSError, ValueError):
                    os.killpg(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)

            child = Path(tmp) / "child.py"
            child.write_text(
                "import signal\n"
                "def clear(*_):\n"
                "    import sqlite3, sys\n"
                f"    with sqlite3.connect({str(database)!r}) as conn:\n"
                "        conn.execute(\"UPDATE sessions SET last_activity_description = '',"
                " last_activity_provenance = 'unknown'\")\n"
                f"    open({str(cleared)!r}, 'w').close()\n"
                "    sys.exit(143)\n"
                "signal.signal(signal.SIGTERM, clear)\n"
                "import os, time\n"
                f"open({str(pid_file) + '.tmp'!r}, 'w').write(str(os.getpid()))\n"
                f"os.replace({str(pid_file) + '.tmp'!r}, {str(pid_file)!r})\n"
                "time.sleep(120)\n",
                encoding="utf-8",
            )
            receipt = json.dumps({"hermes_home": str(home)})
            command = (
                f"printf '%s' {shlex.quote(receipt)} > {shlex.quote(str(controls))}"
                f" && exec {shlex.quote(sys.executable)} {shlex.quote(str(child))}"
            )

            def await_child(timeout: float = 120) -> None:
                deadline = time.monotonic() + timeout
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

            real_popen = adapter.subprocess.Popen

            class PopenOnceTheChildIsReady(real_popen):
                """Start this test's child, then return only once it is ready; pass anything else through."""

                def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
                    super().__init__(args, *rest, **kwargs)
                    if args == command:
                        await_child()

            real_killpg = os.killpg

            def killpg_then_await_clear(pgid: int, signum: int) -> None:
                real_killpg(pgid, signum)
                deadline = time.monotonic() + 120
                while signum == signal.SIGTERM and not cleared.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

            env = {"PATH": "/usr/bin:/bin", "BOUNDROOM_HERMES_USAGE_DIR": str(sessions)}
            try:
                with (
                    mock.patch.object(adapter.subprocess, "Popen", PopenOnceTheChildIsReady),
                    mock.patch.object(adapter.os, "killpg", killpg_then_await_clear),
                ):
                    returncode = adapter._subprocess_runner(command, Path(tmp), env, 1)
                snapshot = json.loads((sessions / f"{STEM}.activity.json").read_text(encoding="utf-8"))
                handler_ran = cleared.exists()
                with contextlib.closing(sqlite3.connect(database)) as conn:
                    (after_exit,) = conn.execute("SELECT last_activity_description FROM sessions").fetchone()
            finally:
                reap()
        self.assertEqual(124, returncode)
        self.assertTrue(handler_ran, "the child's SIGTERM handler never ran")
        self.assertEqual("", after_exit)
        self.assertEqual("stream retry 1/5 after ReadTimeout", snapshot["row"]["last_activity_description"])


class WholeSessionTests(unittest.TestCase):
    """Run 38248084765's lane C, research then plan, from the files to the operator's lines."""

    def test_a_lane_killed_at_its_plan_budget_reads_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            research = Session(Path(tmp), stem=RESEARCH_STEM, phase_name="research")
            research.ledger(
                calls=3,
                messages=(("assistant", 60, True), ("tool", 70, False), ("assistant", 2100, True), ("tool", 2140, False)),
                activity=(2155, ""),
            )
            research.file(".log", "research notes\n")
            research.file(".usage.json", json.dumps({"model": "z-ai/glm-5.3", "api_calls": 3}))
            row = {
                "last_activity_at": research.launch + 2150,
                "last_activity_description": "receiving stream response",
                "last_activity_provenance": "unknown",
            }
            research.file(".activity.json", json.dumps({"captured_at": research.launch + 2160, "row": row}))
            plan = Session(Path(tmp))
            for session in (research, plan):
                session.witness("2026-10-12T12:21:57.317312761Z")
            records = diagnosis.diagnose_sessions(
                research.dir, [phase(124, 1080.0, False), phase(124, 2160.0, True, name="research")]
            )
        expected = [[line.replace("{plan_home}", str(plan.home)) for line in lines] for lines in GOLDEN_LANE]
        self.assertEqual(expected, [diagnosis.summarize(record) for record in records])


class ReadOnlyTests(unittest.TestCase):
    def test_the_diagnosis_changes_nothing_it_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=1, messages=(("assistant", 60, True),))
            session.witness("2026-10-12T12:57:30Z")
            files = sorted(Path(tmp).rglob("*"))
            before = {path: path.read_bytes() for path in files if path.is_file()}
            diagnosis.diagnose_session(session.dir, session.stem, phase(124, 1080.0, False))
            after = {path: path.read_bytes() for path in sorted(Path(tmp).rglob("*")) if path.is_file()}
        self.assertEqual(before, after)

    def test_message_content_never_enters_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=1)
            with contextlib.closing(sqlite3.connect(session.home / "state.db")) as conn, conn:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'assistant', 'SECRET-PROSE', ?)",
                    (LAUNCH + 9,),
                )
            record = diagnosis.diagnose_session(session.dir, session.stem, phase(124, 1080.0, False))
        self.assertNotIn("SECRET-PROSE", json.dumps(record))


class CellRecordTests(unittest.TestCase):
    def test_every_cell_records_its_session_diagnoses(self) -> None:
        from automation.benchmark import lanes as lanes_module

        class Adapter:
            def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
                Session(out_tar.parent).ledger(row=False)
                strategy = StrategyResult(
                    lane="C",
                    returncode=124,
                    phases=(PhaseResult(phase="plan", returncode=124, seconds=1080.0, prompt_chars=1, artifact_valid=False),),
                )
                return SubmissionResult(task.instance_id, out_tar, out_tar.parent, 124, strategy=strategy)

        with tempfile.TemporaryDirectory() as tmp:
            cell = lanes_module.Cell("C", 1, "abishekvashok__cmatrix.5c082c6")
            provenance = lanes_module.run_cell(cell, Path(tmp), Adapter(), Path(tmp))
        self.assertEqual([diagnosis.NO_TURN_START_RECORDED], [d["verdict"] for d in provenance["session_diagnoses"]])


GOLDEN_LANE = [
    [
        "session       20261012T122043Z-3852 pid 3852 (research, oneshot)",
        "verdict       active_until_budget: tools ran and returned; the budget ended it (last recorded activity at 2155.0s)"
        " after a valid artifact",
        "timeline      turn_started 3.0s, first_completed_model_call 60.0s, last_completed_model_call 180.0s,"
        " first_model_output 60.0s, first_tool_call 60.0s, first_tool_result 70.0s, last_activity 2155.0s,"
        " first_container 74.3s",
        "containers    1 in this session's window, 0 from other sessions",
        "last activity receiving_chunks at 2150.0s (pre_sigterm_snapshot): 'receiving stream response'",
    ],
    [
        "session       20261012T125644Z-42025 pid 42025 (plan, oneshot)",
        "verdict       evidence_unavailable: no session ledger and no event stream, so nothing here can say how far the"
        " session got",
        "timeline      nothing timestamped",
        "containers    0 in this session's window, 1 from other sessions",
        "missing       session log: 0 bytes - --oneshot writes nothing until the turn returns",
        "missing       usage report: absent - --oneshot writes it only when the turn returns",
        "missing       session ledger: {plan_home} exists but Hermes never created state.db in it",
        "missing       activity snapshot: none taken before the budget kill, so SIGTERM may have cleared the label",
    ],
]


if __name__ == "__main__":
    unittest.main()
