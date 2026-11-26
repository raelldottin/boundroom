"""The local board (SPEC-004, RA-BD-002) and the board contract every backend keeps.

The contract tests run once per backend: the local board, and the Hermes backend against the
synthetic board and stand-in ``hermes`` from ``test_kanban_writer``. The local-board tests after
them cover what only the local board has: atomic claims across processes, expiry and renewal, its
schema checks, its task and link rules and its command line. None of them needs ``hermes``.

The local board is a store this code owns, so its tests use a real SQLite file in ``tmp_path``
and an injected clock. Raw SQL appears only to set up a state the board's own API can't reach.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import pytest

from automation.supervisor import kanban_projection as kp
from automation.supervisor import kanban_writer as kw
from automation.supervisor import local_board as lb
from automation.supervisor.board_backend import BoardBackend, Claim
from automation.tests import test_kanban_writer

board = test_kanban_writer.board
hermes = test_kanban_writer.hermes
handoff = test_kanban_writer.handoff

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "automation/examples/kanban-overlay.json"
SLUG = "local-board"
MISSING = "t_0000dead"  # a well-formed task id that no board here has
# Set by run_agent.sh for every runner. Spelled out here, not read from local_board, so a renamed
# or dropped variable fails a test instead of quietly passing.
NESTED_RUN_ENV = ("BOUNDROOM_SUPERVISOR_SLICE_ID", "REPO_AUTOMATION_SUPERVISOR_SLICE_ID", "OWLORY_SUPERVISOR_SLICE_ID")


@pytest.fixture(autouse=True)
def outside_a_supervised_slice(monkeypatch):
    """These tests may themselves run inside a supervised slice; the board must not see it."""
    for name in {*NESTED_RUN_ENV, *lb.NESTED_RUN_ENV}:  # the module's list too, in case it grows
        monkeypatch.delenv(name, raising=False)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Supersede(Protocol):
    def __call__(self, task_id: str, end_old: bool = True) -> None: ...


@dataclass
class Contract:
    """One backend and the board facts a contract test needs, whatever the backend."""

    backend: BoardBackend
    db: Path
    ready: str  # a ready, unassigned task
    todo: str  # a task waiting on an open parent
    # Give a task a newer current run than the supervisor's. With end_old=False the supervisor's
    # run is left running, as when someone else's claim lands before the old one is closed.
    supersede: Supersede

    def status(self, task_id: str) -> str:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

    def execute(self, sql: str, *params: object) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(sql, params)

    def take(self, ttl_seconds: int = 600) -> Claim:
        return self.backend.claim(self.ready, self.backend.snapshot().revision, ttl_seconds)


def local_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Contract:
    monkeypatch.setenv("PATH", "")  # the local board must not need hermes, or any executable
    local = lb.init_board(tmp_path / "boards" / SLUG, actor="tester", clock=Clock())
    parent = local.add("Add the parser")
    ready = local.add("Speed up the hot loop", priority=1)
    todo = local.add("Document the CLI", parents=[parent])
    supervisor = lb.LocalBoard(local.board_dir, clock=local.clock)
    db = local.board_dir / lb.DB_NAME

    def supersede(task_id: str, end_old: bool = True) -> None:
        with closing(sqlite3.connect(db)) as conn, conn:
            if end_old:
                conn.execute(
                    "UPDATE task_runs SET status = 'ended', outcome = 'reclaimed' WHERE task_id = ? AND status = 'running'",
                    (task_id,),
                )
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, claimed_by, claimed_at, claim_expires_at)"
                " VALUES (?, 'running', 'someone-else', 0, 9e18)",
                (task_id,),
            ).lastrowid
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))

    return Contract(supervisor, db, ready, todo, supersede)


def hermes_contract(board_dir: Path, hermes_cmd: tuple[str, ...]) -> Contract:
    db = board_dir / "kanban.db"

    def supersede(task_id: str, end_old: bool = True) -> None:
        with closing(sqlite3.connect(db)) as conn, conn:
            if end_old:
                conn.execute(
                    "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed' WHERE task_id = ? AND status = 'running'",
                    (task_id,),
                )
            run_id = conn.execute("INSERT INTO task_runs (task_id, status) VALUES (?, 'running')", (task_id,)).lastrowid
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))

    return Contract(kw.HermesBackend(board_dir, hermes_cmd), db, test_kanban_writer.READY, "t_000000a4", supersede)


@pytest.fixture(params=["local", "hermes"])
def contract(request, tmp_path, monkeypatch) -> Contract:
    if request.param == "local":
        return local_contract(tmp_path, monkeypatch)
    return hermes_contract(request.getfixturevalue("board"), request.getfixturevalue("hermes"))


# The board contract, on every backend.


def test_every_backend_is_a_board_backend(contract):
    assert isinstance(contract.backend, BoardBackend)


def test_a_done_verdict_claims_then_asks_for_review_once(contract, handoff):
    held = contract.take()
    assert (held.task_id, contract.status(contract.ready)) == (contract.ready, "running")

    assert contract.backend.record(held, "done", "validated", handoff) is True
    assert contract.status(contract.ready) == "review"
    assert contract.backend.record(held, "done", "validated", handoff) is False  # already recorded


@pytest.mark.parametrize("verdict", ["blocked", "failed"])
def test_blocked_and_failed_verdicts_block_the_task(contract, verdict):
    held = contract.take()

    assert contract.backend.record(held, verdict, "validation failed", None) is True
    assert contract.status(contract.ready) == "blocked"


def test_a_done_verdict_needs_its_handoff(contract):
    held = contract.take()

    with pytest.raises(kp.ProjectionError, match="needs its handoff"):
        contract.backend.record(held, "done", "validated", None)
    assert contract.status(contract.ready) == "running"


def test_a_board_that_moved_since_the_projection_is_not_claimed(contract):
    revision = contract.backend.snapshot().revision
    contract.execute("UPDATE tasks SET priority = priority + 7 WHERE id = ?", contract.todo)

    with pytest.raises(kp.StaleRevisionError):
        contract.backend.claim(contract.ready, revision, 600)
    assert contract.status(contract.ready) == "ready"


def test_tasks_the_supervisor_may_not_take_are_refused(contract):
    with pytest.raises(kp.ProjectionError, match="not ready"):
        contract.backend.claim(contract.todo, contract.backend.snapshot().revision, 600)

    contract.execute("UPDATE tasks SET assignee = 'a-worker' WHERE id = ?", contract.ready)
    with pytest.raises(kp.ProjectionError, match="assigned"):
        contract.take()
    assert contract.status(contract.ready) == "ready"


def test_a_task_missing_from_the_board_is_not_claimed(contract):
    revision = contract.backend.snapshot().revision

    with pytest.raises(kp.ProjectionError, match=f"no task {MISSING} on board '{contract.backend.slug}'"):
        contract.backend.claim(MISSING, revision, 600)
    assert contract.backend.snapshot().revision == revision


def test_a_run_missing_from_the_board_is_refused(contract, handoff):
    contract.take()
    stranger = Claim(contract.backend.slug, contract.ready, 99)

    with pytest.raises(kp.ProjectionError, match=f"run 99 of {contract.ready} is not on board '{contract.backend.slug}'"):
        contract.backend.record(stranger, "done", "validated", handoff)
    assert contract.status(contract.ready) == "running"


def test_a_superseded_run_is_not_ours_to_close(contract, handoff):
    held = contract.take()
    contract.supersede(contract.ready)

    with pytest.raises(kp.ProjectionError, match="not ours"):
        contract.backend.record(held, "done", "validated", handoff)
    assert contract.status(contract.ready) == "running"


def test_a_still_running_run_that_is_no_longer_current_is_not_ours_to_close(contract, handoff):
    held = contract.take()
    contract.supersede(contract.ready, end_old=False)

    with pytest.raises(kp.ProjectionError, match=f"run {held.run_id} of {contract.ready} is running; not ours to close"):
        contract.backend.record(held, "done", "validated", handoff)
    assert contract.status(contract.ready) == "running"


def test_unknown_verdicts_and_foreign_boards_are_refused(contract, handoff):
    held = contract.take()

    with pytest.raises(kp.ProjectionError, match="no Kanban transition"):
        contract.backend.record(held, "queued", "", handoff)
    with pytest.raises(kp.ProjectionError, match="claim is for board"):
        contract.backend.record(Claim("other-board", held.task_id, held.run_id), "done", "", handoff)
    assert contract.status(contract.ready) == "running"


def test_a_snapshot_projects_through_an_approval_overlay(contract):
    example = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document = {
        "version": 1,
        "backend": contract.backend.kind,
        "board": contract.backend.slug,
        "policy": example["policy"],
        "approved": {contract.ready: example["approved"]["t_000000a3"]},
    }
    overlay = kp.KanbanOverlay.model_validate(document)

    projection = kp.project(contract.backend.snapshot(), overlay)

    [only] = projection.queue["slices"]
    prefix = {"local": "board", "hermes": "kanban"}[contract.backend.kind]
    assert (only["slice_id"], only["status"]) == (f"{prefix}.{contract.backend.slug}.{contract.ready}", "queued")
    assert kp.parse_slice_id(only["slice_id"]) == (contract.backend.slug, contract.ready)
    assert projection.revision == contract.backend.snapshot().revision


# What only the local board does.


@pytest.fixture
def local(tmp_path, monkeypatch) -> Contract:
    return local_contract(tmp_path, monkeypatch)


def supervisor_of(local: Contract) -> lb.LocalBoard:
    assert isinstance(local.backend, lb.LocalBoard)
    return local.backend


def clock_of(local: Contract) -> Clock:
    clock = supervisor_of(local).clock
    assert isinstance(clock, Clock)
    return clock


def person(local: Contract, actor: str = "maintainer") -> lb.LocalBoard:
    """The board as a person at the command line sees it: same file and clock, their own actor."""
    return lb.LocalBoard(local.db.parent, clock=clock_of(local), actor=actor)


def parent_of(local: Contract) -> str:
    """The ready task that ``local.todo`` waits on."""
    return next(t["id"] for t in local.backend.snapshot().tasks if t["title"] == "Add the parser")


def take(local: Contract, task_id: str, ttl_seconds: int = 600) -> Claim:
    supervisor = supervisor_of(local)
    return supervisor.claim(task_id, supervisor.snapshot().revision, ttl_seconds)


def review(local: Contract, task_id: str, handoff: Path) -> None:
    supervisor_of(local).record(take(local, task_id), "done", "validated", handoff)


def block(local: Contract, task_id: str) -> None:
    supervisor_of(local).record(take(local, task_id), "failed", "flaky test", None)


def finish(local: Contract, task_id: str, handoff: Path) -> None:
    review(local, task_id, handoff)
    person(local).approve(task_id)


def run_row(local: Contract, run_id: int) -> dict:
    with closing(sqlite3.connect(local.db)) as conn, conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())


def watermark(local: Contract) -> int:
    return local.backend.snapshot().event_watermark


def test_two_processes_claiming_one_task_at_one_revision_get_one_claim(local):
    revision = local.backend.snapshot().revision
    script = (
        "import sys; from pathlib import Path; from automation.supervisor.local_board import LocalBoard; "
        "LocalBoard(Path(sys.argv[1])).claim(sys.argv[2], sys.argv[3], 600)"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PATH": os.defpath}
    argv = [sys.executable, "-c", script, str(local.db.parent), local.ready, revision]
    racers = [subprocess.Popen(argv, cwd=REPO_ROOT, env=env, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    errors = [racer.communicate(timeout=60)[1] for racer in racers]
    results = [(racer.returncode, error) for racer, error in zip(racers, errors)]

    assert sorted(code for code, _ in results) == [0, 1]
    assert "StaleRevisionError" in next(err for code, err in results if code)
    with closing(sqlite3.connect(local.db)) as conn, conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


def test_an_expired_claim_is_released_and_its_record_refused(local, handoff):
    clock = local.backend.clock
    assert isinstance(clock, Clock) and isinstance(local.backend, lb.LocalBoard)
    held = local.take(ttl_seconds=10)
    clock.now += 11

    with pytest.raises(lb.LocalBoardError, match="expired; not ours"):
        local.backend.record(held, "done", "validated", handoff)

    # Every write releases expired claims first, in a transaction of its own, so the refused
    # record still left the task ready again for the next claim.
    assert local.status(local.ready) == "ready"
    expired = [e for e in local.backend.events(local.ready) if e["kind"] == "expired"]
    assert [(e["run_id"], e["actor"]) for e in expired] == [(held.run_id, lb.EXPIRY_ACTOR)]
    with pytest.raises(lb.LocalBoardError, match="expired"):
        local.backend.renew(held, 600)


def test_a_claim_expires_exactly_at_its_ttl(local):
    supervisor, clock = supervisor_of(local), clock_of(local)
    held = local.take(ttl_seconds=10)

    clock.now += 9.5
    supervisor.expire()
    assert (local.status(local.ready), run_row(local, held.run_id)["status"]) == ("running", "running")

    clock.now += 0.5
    supervisor.expire()
    assert local.status(local.ready) == "ready"
    assert run_row(local, held.run_id)["outcome"] == "expired"


def test_an_expired_claim_on_a_task_with_open_parents_returns_to_todo(local):
    clock = clock_of(local)
    held = local.take(ttl_seconds=10)
    # A running task takes no new parents through the API; a board edited by hand can still have one.
    local.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", parent_of(local), local.ready)
    clock.now += 11

    supervisor_of(local).expire()

    assert local.status(local.ready) == "todo"
    [expired] = [e for e in supervisor_of(local).events(local.ready) if e["kind"] == "expired"]
    assert (expired["run_id"], expired["payload"]) == (held.run_id, {"status": "todo"})


def test_expire_releases_expired_claims_without_another_write(local):
    clock = clock_of(local)
    held = local.take(ttl_seconds=10)
    clock.now += 11

    supervisor_of(local).expire()

    assert local.status(local.ready) == "ready"
    run = run_row(local, held.run_id)
    assert (run["status"], run["outcome"], run["ended_at"]) == ("ended", "expired", int(clock.now))
    assert person(local).task(local.ready)["current_run_id"] is None
    [expired] = [e for e in supervisor_of(local).events(local.ready) if e["kind"] == "expired"]
    assert (expired["actor"], expired["payload"]) == (lb.EXPIRY_ACTOR, {"status": "ready"})


def test_a_renewed_claim_outlives_its_first_ttl(local, handoff):
    clock = local.backend.clock
    assert isinstance(clock, Clock)
    held = local.take(ttl_seconds=10)
    clock.now += 5
    assert local.backend.renew(held, 100) == held
    clock.now += 50

    assert local.backend.record(held, "done", "validated", handoff) is True
    assert local.status(local.ready) == "review"


def test_a_superseded_run_cannot_be_renewed(local):
    held = local.take()
    local.supersede(local.ready)

    with pytest.raises(lb.LocalBoardError, match="not ours"):
        local.backend.renew(held, 600)


@pytest.mark.parametrize("ttl", [0, -1])
def test_a_claim_needs_a_positive_ttl(local, ttl):
    supervisor = supervisor_of(local)
    revision = supervisor.snapshot().revision

    with pytest.raises(lb.LocalBoardError, match=f"a claim needs a positive TTL, not {ttl}"):
        supervisor.claim(local.ready, revision, ttl)
    assert (supervisor.snapshot().revision, local.status(local.ready)) == (revision, "ready")


@pytest.mark.parametrize("ttl", [0, -1])
def test_a_renewal_needs_a_positive_ttl(local, ttl):
    supervisor = supervisor_of(local)
    held = local.take(ttl_seconds=1)  # the shortest TTL there is
    expires = run_row(local, held.run_id)["claim_expires_at"]
    events = supervisor.events()
    with pytest.raises(lb.LocalBoardError, match=f"a claim needs a positive TTL, not {ttl}"):
        supervisor.renew(held, ttl)
    assert (run_row(local, held.run_id)["claim_expires_at"], supervisor.events()) == (expires, events)

    assert supervisor.renew(held, 1) == held


def test_a_renewal_for_another_board_is_refused(local):
    held = local.take()
    expires = run_row(local, held.run_id)["claim_expires_at"]

    with pytest.raises(lb.LocalBoardError, match=f"claim is for board 'other-board', not '{SLUG}'"):
        supervisor_of(local).renew(Claim("other-board", held.task_id, held.run_id), 9_999)
    assert run_row(local, held.run_id)["claim_expires_at"] == expires


def _foreign_slug(local: Contract, tmp_path: Path) -> Path:
    other = tmp_path / "boards" / "other-board"
    other.mkdir()
    (other / lb.DB_NAME).write_bytes(local.db.read_bytes())
    return other


def _newer_schema(local: Contract, tmp_path: Path) -> Path:
    local.execute(f"PRAGMA user_version = {lb.SCHEMA_VERSION + 1}")
    return local.db.parent


def _plain_sqlite(local: Contract, tmp_path: Path) -> Path:
    plain = tmp_path / "boards" / "plain-board"
    plain.mkdir()
    with closing(sqlite3.connect(plain / lb.DB_NAME)) as conn, conn:
        conn.execute("CREATE TABLE notes (text TEXT)")
    return plain


def _no_slug(local: Contract, tmp_path: Path) -> Path:
    local.execute("DELETE FROM meta")
    return local.db.parent


def _missing(local: Contract, tmp_path: Path) -> Path:
    return tmp_path / "boards" / "no-board"


def _symlinked(local: Contract, tmp_path: Path) -> Path:
    # Same slug, so only the link itself can be the reason for the refusal.
    linked = tmp_path / "elsewhere" / SLUG
    linked.mkdir(parents=True)
    (linked / lb.DB_NAME).symlink_to(local.db)
    return linked


def _garbage(local: Contract, tmp_path: Path) -> Path:
    junk = tmp_path / "boards" / "junk-board"
    junk.mkdir()
    (junk / lb.DB_NAME).write_bytes(b"this is not a database, " * 100)
    return junk


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (_foreign_slug, f"is board '{SLUG}', not 'other-board'"),
        (_newer_schema, f"has schema version {lb.SCHEMA_VERSION + 1}; this code reads version {lb.SCHEMA_VERSION}"),
        (_plain_sqlite, f"has schema version 0; this code reads version {lb.SCHEMA_VERSION}"),
        (_no_slug, f"is board None, not '{SLUG}'"),
        (_missing, "missing board database"),
        (_symlinked, "missing board database"),
        (_garbage, "file is not a database"),
    ],
    ids=["foreign-slug", "newer-schema", "plain-sqlite", "no-slug", "missing", "symlinked", "garbage"],
)
def test_a_board_this_code_cannot_read_is_refused(local, tmp_path, make: Callable[[Contract, Path], Path], message):
    board_dir = make(local, tmp_path)

    with pytest.raises(lb.LocalBoardError, match=re.escape(message)):
        lb.LocalBoard(board_dir).snapshot()


def test_a_new_board_is_an_empty_schema_version_1_file(tmp_path):
    board_dir = tmp_path / "not-yet" / "boards" / "fresh-board"
    clock = Clock(1234.9)

    created = lb.init_board(board_dir, actor="founder", clock=clock)

    assert (created.board_dir, created.actor, created.clock, created.slug) == (board_dir, "founder", clock, "fresh-board")
    with closing(sqlite3.connect(board_dir / lb.DB_NAME)) as conn, conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert created.events() == [
        {"id": 1, "task_id": None, "run_id": None, "kind": "board_created", "actor": "founder", "at": 1234, "payload": {}}
    ]
    snapshot = created.snapshot()
    assert (snapshot.board, snapshot.tasks, snapshot.links, snapshot.creators) == ("fresh-board", (), (), {})
    assert snapshot.event_watermark == 1


def test_a_board_can_be_created_in_an_existing_empty_directory(tmp_path):
    board_dir = tmp_path / "boards" / "empty-board"
    board_dir.mkdir(parents=True)

    assert lb.init_board(board_dir, actor="founder").snapshot().tasks == ()


def test_an_existing_board_is_not_overwritten(local):
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match="board already exists"):
        lb.init_board(local.db.parent, actor="someone-else")
    assert local.backend.snapshot() == before


def test_a_writer_waits_for_another_writers_lock(local):
    holder = sqlite3.connect(local.db, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.3, holder.commit)
    started = time.monotonic()
    release.start()
    try:
        late = person(local).add("Written after the lock is released")
        waited = time.monotonic() - started
    finally:
        release.join()
        holder.close()

    assert waited >= 0.3
    assert local.status(late) == "ready"


def test_a_failed_claim_release_rolls_back_and_stops_the_write(local):
    held = local.take(ttl_seconds=10)
    # A link to a task that is gone, which the board's foreign keys forbid: a damaged board.
    local.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", MISSING, local.ready)
    clock_of(local).now += 11
    tasks = local.backend.snapshot().tasks

    with pytest.raises(lb.LocalBoardError, match=f"no task {MISSING}"):
        person(local).add("Never written")

    assert run_row(local, held.run_id)["status"] == "running"
    assert local.backend.snapshot().tasks == tasks


def test_a_snapshot_writes_nothing(local):
    before = local.db.read_bytes()
    local.backend.snapshot()
    assert local.db.read_bytes() == before


def test_a_snapshot_lists_tasks_links_and_creators_in_order(local):
    parent = parent_of(local)
    person(local).link(local.ready, local.todo)
    # Hermes records which task created another; the local board's API never writes it.
    local.execute(
        "UPDATE task_events SET payload = ? WHERE kind = 'created' AND task_id = ?",
        '{"creator_task_id": "t_0000beef"}',
        local.ready,
    )

    snapshot = local.backend.snapshot()

    titles = {parent: "Add the parser", local.ready: "Speed up the hot loop", local.todo: "Document the CLI"}
    expected = [
        {"id": task_id, "title": titles[task_id], "status": "todo" if task_id == local.todo else "ready",
         "priority": 1 if task_id == local.ready else 0, "created_at": 1_000_000}
        for task_id in sorted(titles)
    ]  # fmt: skip
    assert list(snapshot.tasks) == expected
    assert list(snapshot.links) == sorted([(parent, local.todo), (local.ready, local.todo)])
    assert snapshot.creators == {local.ready: "t_0000beef"}
    assert snapshot.event_watermark == len(supervisor_of(local).events())


def test_an_added_task_is_ready_unless_a_parent_is_open(local, handoff):
    maintainer = person(local)
    parent = parent_of(local)
    finish(local, parent, handoff)
    maintainer.archive(local.todo)

    assert maintainer.task(maintainer.add("No parents"))["status"] == "ready"
    assert maintainer.task(maintainer.add("After a done task", parents=[parent]))["status"] == "ready"
    assert maintainer.task(maintainer.add("After an archived task", parents=[local.todo]))["status"] == "ready"
    assert maintainer.task(maintainer.add("After one open task", parents=[parent, local.ready]))["status"] == "todo"


def test_an_added_task_gets_a_hermes_format_id_a_trimmed_title_and_priority_zero(local):
    parents = sorted([parent_of(local), local.ready], reverse=True)

    task_id = person(local).add("  Write the docs  ", parents=parents)

    assert re.fullmatch(r"t_[0-9a-f]{8}", task_id)
    assert person(local).task(task_id) == {
        "id": task_id,
        "title": "Write the docs",
        "status": "todo",
        "priority": 0,
        "created_at": 1_000_000,
        "assignee": None,
        "current_run_id": None,
    }
    [created] = person(local).events(task_id)
    assert (created["kind"], created["actor"]) == ("created", "maintainer")
    assert created["payload"] == {"parents": [parents[1], parents[0]], "title": "Write the docs"}


@pytest.mark.parametrize("title", ["", "   "])
def test_a_task_needs_a_title(local, title):
    before = watermark(local)

    with pytest.raises(lb.LocalBoardError, match="a task needs a title"):
        person(local).add(title)
    assert watermark(local) == before


def test_a_task_cannot_depend_on_a_missing_parent(local):
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match=f"no task {MISSING}"):
        person(local).add("Orphan", parents=[local.ready, MISSING])
    assert local.backend.snapshot() == before


def _stale_add(local: Contract, revision: str) -> None:
    person(local).add("Late", expected_revision=revision)


def _stale_link(local: Contract, revision: str) -> None:
    person(local).link(parent_of(local), local.ready, expected_revision=revision)


def _stale_priority(local: Contract, revision: str) -> None:
    person(local).set_priority(local.ready, 5, expected_revision=revision)


def _stale_approve(local: Contract, revision: str) -> None:
    person(local).approve(local.ready, expected_revision=revision)


def _stale_archive(local: Contract, revision: str) -> None:
    person(local).archive(local.todo, expected_revision=revision)


def _stale_unblock(local: Contract, revision: str) -> None:
    person(local).unblock(local.ready, expected_revision=revision)


@pytest.mark.parametrize(
    ("write", "setup"),
    [
        (_stale_add, None),
        (_stale_link, None),
        (_stale_priority, None),
        (_stale_approve, "review"),
        (_stale_archive, None),
        (_stale_unblock, "block"),
    ],
    ids=["add", "link", "priority", "approve", "archive", "unblock"],
)
def test_a_write_at_a_stale_revision_is_refused(local, handoff, write, setup):
    if setup == "review":
        review(local, local.ready, handoff)
    elif setup == "block":
        block(local, local.ready)
    stale = local.backend.snapshot().revision
    local.execute("UPDATE tasks SET priority = priority + 7 WHERE id = ?", parent_of(local))
    moved = local.backend.snapshot().revision

    with pytest.raises(kp.StaleRevisionError, match=f"not the expected {stale}"):
        write(local, stale)
    assert local.backend.snapshot().revision == moved

    write(local, moved)
    assert local.backend.snapshot().revision != moved


def test_links_keep_dependencies_honest(local):
    board_ = person(local)
    late = board_.add("Release notes")
    assert board_.task(late)["status"] == "ready"

    board_.link(local.todo, late)
    assert board_.task(late)["status"] == "todo"  # a ready task behind an open parent waits
    assert board_.events(late)[-1]["payload"] == {"parent_id": local.todo}


def test_linking_moves_only_a_ready_child_behind_an_open_parent(local, handoff):
    maintainer = person(local)
    parent = parent_of(local)
    finish(local, parent, handoff)
    after_done = maintainer.add("After the parser")
    maintainer.link(parent, after_done)
    assert maintainer.task(after_done)["status"] == "ready"  # its only parent is done

    block(local, local.ready)
    maintainer.link(local.todo, local.ready)
    assert local.status(local.ready) == "blocked"  # a blocked task stays blocked

    waiting = maintainer.add("Behind the docs")
    maintainer.link(local.todo, waiting)
    assert local.status(waiting) == "todo"


def _direct_cycle(local: Contract) -> tuple[str, str]:
    return local.todo, parent_of(local)


def _multi_hop_cycle(local: Contract) -> tuple[str, str]:
    grandchild = person(local).add("After the docs", parents=[local.todo])
    return grandchild, parent_of(local)


@pytest.mark.parametrize(
    ("pick", "message"),
    [(_direct_cycle, "would make a cycle"), (_multi_hop_cycle, "would make a cycle"), (None, "can't depend on itself")],
    ids=["direct", "multi-hop", "self"],
)
def test_a_link_that_would_make_a_cycle_is_refused(local, pick, message):
    parent_id, child_id = pick(local) if pick else (local.ready, local.ready)
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match=message):
        person(local).link(parent_id, child_id)
    assert local.backend.snapshot() == before


@pytest.mark.parametrize("status", ["running", "review", "done"])
def test_a_task_that_is_not_open_takes_no_new_parent(local, handoff, status):
    if status == "running":
        take(local, local.ready)
    elif status == "review":
        review(local, local.ready, handoff)
    else:
        finish(local, local.ready, handoff)
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match=f"task {local.ready} is {status}; only todo, ready or blocked"):
        person(local).link(parent_of(local), local.ready)
    assert local.backend.snapshot() == before


def test_a_duplicate_link_is_refused(local):
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match=f"{parent_of(local)} is already a parent of {local.todo}"):
        person(local).link(parent_of(local), local.todo)
    assert local.backend.snapshot() == before


def test_a_link_to_a_missing_task_is_refused(local):
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match=f"no task {MISSING}"):
        person(local).link(MISSING, local.ready)
    with pytest.raises(lb.LocalBoardError, match=f"no task {MISSING}"):
        person(local).link(local.ready, MISSING)
    assert local.backend.snapshot() == before


def test_priority_changes_are_recorded_but_an_archived_task_is_refused(local):
    maintainer = person(local)

    maintainer.set_priority(local.todo, 4)
    assert maintainer.task(local.todo)["priority"] == 4
    last = maintainer.events(local.todo)[-1]
    assert (last["kind"], last["actor"], last["payload"]) == ("prioritized", "maintainer", {"priority": 4})

    maintainer.archive(local.todo)
    with pytest.raises(lb.LocalBoardError, match=f"task {local.todo} is archived"):
        maintainer.set_priority(local.todo, 9)
    assert maintainer.task(local.todo)["priority"] == 4


def test_approving_a_parent_promotes_its_waiting_children(local, handoff):
    maintainer = person(local)
    parent = parent_of(local)
    second = maintainer.add("Review the CLI")
    maintainer.link(second, local.todo)  # the docs now wait on two parents
    review(local, parent, handoff)
    review(local, second, handoff)

    assert maintainer.approve(parent) == []  # one parent is still open
    assert local.status(local.todo) == "todo"
    assert maintainer.approve(second) == [local.todo]
    assert (local.status(parent), local.status(second), local.status(local.todo)) == ("done", "done", "ready")
    approved = [e for e in maintainer.events(second) if e["kind"] == "approved"]
    assert [e["actor"] for e in approved] == ["maintainer"]


def test_only_a_task_in_review_can_be_approved(local):
    before = local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match=f"task {local.ready} is ready, not review"):
        person(local).approve(local.ready)
    assert local.backend.snapshot() == before


def test_archiving_promotes_waiting_children(local):
    parent = parent_of(local)

    assert person(local).archive(parent) == [local.todo]

    assert (local.status(parent), local.status(local.todo)) == ("archived", "ready")
    [promoted] = [e for e in supervisor_of(local).events(local.todo) if e["kind"] == "promoted"]
    assert (promoted["actor"], promoted["payload"]) == ("maintainer", {"after": parent})


def test_a_running_or_archived_task_cannot_be_archived(local):
    maintainer = person(local)
    take(local, local.ready)
    with pytest.raises(lb.LocalBoardError, match=f"task {local.ready} is running and can't be archived"):
        maintainer.archive(local.ready)
    assert local.status(local.ready) == "running"

    maintainer.archive(local.todo)
    with pytest.raises(lb.LocalBoardError, match=f"task {local.todo} is archived and can't be archived"):
        maintainer.archive(local.todo)


def test_unblock_returns_a_blocked_task_to_ready_or_todo(local):
    maintainer = person(local)
    block(local, local.ready)

    assert maintainer.unblock(local.ready) == "ready"
    assert maintainer.events(local.ready)[-1]["payload"] == {"status": "ready"}
    with pytest.raises(lb.LocalBoardError, match=f"task {local.ready} is not blocked"):
        maintainer.unblock(local.ready)

    block(local, local.ready)
    maintainer.link(parent_of(local), local.ready)
    assert maintainer.unblock(local.ready) == "todo"
    assert local.status(local.ready) == "todo"


@pytest.mark.parametrize(
    ("verdict", "outcome", "kind"),
    [("done", "review_requested", None), ("blocked", "blocked", "needs_input"), ("failed", "blocked", "transient")],
)
def test_a_verdict_stores_its_slice_handoff_digest_and_reason(local, handoff, verdict, outcome, kind):
    held = local.take()
    clock_of(local).now += 42

    supervisor_of(local).record(held, verdict, "validated", handoff)

    digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    metadata = {"handoff_sha256": digest, "reason": "validated", "slice_id": f"board.{SLUG}.{local.ready}"}
    if kind:
        metadata["block_kind"] = kind
    run = run_row(local, held.run_id)
    assert (run["status"], run["outcome"], run["ended_at"]) == ("ended", outcome, 1_000_042)
    assert json.loads(run["metadata"]) == metadata
    last = supervisor_of(local).events(local.ready)[-1]
    assert (last["kind"], last["run_id"], last["actor"], last["payload"]) == (
        outcome,
        held.run_id,
        lb.SUPERVISOR_ACTOR,
        metadata,
    )


def test_a_blocked_verdict_with_a_missing_handoff_file_records_none(local, tmp_path):
    held = local.take()

    assert supervisor_of(local).record(held, "blocked", "no handoff", tmp_path / "never-written.json") is True
    assert json.loads(run_row(local, held.run_id)["metadata"])["handoff_sha256"] == "none"


# The command line.


def cli(local: Contract, capsys, *args: str) -> tuple[int, str, str]:
    code = lb.main(["--board", str(local.db.parent), "--actor", "maintainer", *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_the_command_line_runs_a_board_from_start_to_review(tmp_path, capsys, handoff):
    board_dir = tmp_path / "boards" / "cli-board"
    assert lb.main(["--board", str(board_dir), "--actor", "maintainer", "init"]) == 0
    capsys.readouterr()
    assert lb.main(["--board", str(board_dir), "add", "First"]) == 0
    first = capsys.readouterr().out.strip()
    assert lb.main(["--board", str(board_dir), "add", "Second", "--parent", first]) == 0
    second = capsys.readouterr().out.strip()

    supervisor = lb.LocalBoard(board_dir)
    held = supervisor.claim(first, supervisor.snapshot().revision, 600)
    supervisor.record(held, "done", "validated", handoff)

    assert lb.main(["--board", str(board_dir), "--actor", "maintainer", "approve", first]) == 0
    assert capsys.readouterr().out.strip() == f"ready: {second}"
    assert lb.main(["--board", str(board_dir), "snapshot"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["revision"] == supervisor.snapshot().revision
    assert {t["id"]: t["status"] for t in document["tasks"]} == {first: "done", second: "ready"}
    approved = [e for e in supervisor.events(first) if e["kind"] == "approved"]
    assert [e["actor"] for e in approved] == ["maintainer"]


@pytest.mark.parametrize("command", ["claim", "record", "renew"])
def test_the_command_line_offers_no_claim_record_or_renew(local, capsys, command):
    with pytest.raises(SystemExit) as stopped:
        lb.main(["--board", str(local.db.parent), command, local.ready])

    assert stopped.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_the_command_line_refuses_a_stale_write_and_reports_why(local, capsys):
    stale = local.backend.snapshot().revision
    local.execute("UPDATE tasks SET priority = 9 WHERE id = ?", local.todo)

    code, _, err = cli(local, capsys, "--expect-revision", stale, "priority", local.ready, "3")
    assert code == 2 and "stop:" in err and "revision" in err
    code, out, _ = cli(local, capsys, "list")
    assert code == 0 and local.ready in out


def test_init_reports_the_board_it_created(tmp_path, capsys):
    board_dir = tmp_path / "boards" / "cli-board"

    assert lb.main(["--board", str(board_dir), "--actor", "maintainer", "init"]) == 0

    assert capsys.readouterr() == (f"created board 'cli-board' at {board_dir / lb.DB_NAME}\n", "")


def test_the_command_line_requires_a_board_and_a_command(local, capsys):
    with pytest.raises(SystemExit) as stopped:
        lb.main(["list"])
    assert stopped.value.code == 2
    assert "the following arguments are required: --board" in capsys.readouterr().err

    with pytest.raises(SystemExit) as stopped:
        lb.main(["--board", str(local.db.parent)])
    assert stopped.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err


def test_add_on_the_command_line_defaults_to_priority_zero(local, capsys):
    code, out, _ = cli(local, capsys, "add", "Late")

    assert code == 0
    assert person(local).task(out.strip())["priority"] == 0


@pytest.mark.parametrize("command", ["approve", "archive"])
def test_approve_or_archive_with_no_waiting_children_says_so(local, capsys, handoff, command):
    if command == "approve":
        review(local, local.ready, handoff)

    assert cli(local, capsys, command, local.ready) == (0, "no children became ready\n", "")


def test_unblock_on_the_command_line_prints_the_new_status(local, capsys):
    block(local, local.ready)

    assert cli(local, capsys, "unblock", local.ready) == (0, "ready\n", "")
    assert local.status(local.ready) == "ready"


def test_show_prints_a_task_and_its_events(local, capsys):
    code, out, _ = cli(local, capsys, "show", local.ready)

    assert code == 0
    assert json.loads(out) == {"task": person(local).task(local.ready), "events": person(local).events(local.ready)}


def test_list_prints_every_task_with_its_status_priority_and_title(local, capsys):
    code, out, _ = cli(local, capsys, "list")

    assert code == 0
    rows = [line.split(maxsplit=3) for line in out.splitlines()]
    assert [local.ready, "ready", "p1", "Speed up the hot loop"] in rows
    assert [local.todo, "todo", "p0", "Document the CLI"] in rows
    assert [row[0] for row in rows] == sorted(t["id"] for t in local.backend.snapshot().tasks)


@pytest.mark.parametrize("name", NESTED_RUN_ENV)
def test_board_writes_are_refused_inside_a_supervised_slice(local, capsys, monkeypatch, name):
    monkeypatch.setenv(name, "board.local-board.t_00000001")
    before = local.backend.snapshot()

    assert cli(local, capsys, "add", "Sneaky") == (78, "", f"stop: board add refused inside a supervised slice ({name} is set)\n")
    assert cli(local, capsys, "priority", local.ready, "9")[0] == 78
    assert local.backend.snapshot() == before
    for read in (["list"], ["show", local.ready], ["snapshot"]):
        assert cli(local, capsys, *read)[0] == 0

    monkeypatch.setenv(name, "")  # an empty value is no slice
    assert cli(local, capsys, "add", "Allowed")[0] == 0


def test_the_actor_defaults_to_the_os_user(local, capsys, monkeypatch):
    # getpass.getuser() reads these, in this order, before it asks the system.
    for variable in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("LOGNAME", "os-person")

    assert lb.main(["--board", str(local.db.parent), "add", "Late"]) == 0

    task_id = capsys.readouterr().out.strip()
    assert [e["actor"] for e in person(local).events(task_id)] == ["os-person"]


def test_snapshot_prints_the_board_document(local, capsys):
    snapshot = local.backend.snapshot()

    code, out, _ = cli(local, capsys, "snapshot")

    assert code == 0
    assert json.loads(out) == {
        "board": SLUG,
        "revision": snapshot.revision,
        "event_watermark": snapshot.event_watermark,
        "tasks": list(snapshot.tasks),
        "links": [list(link) for link in snapshot.links],
        "creators": snapshot.creators,
    }


def test_the_module_runs_as_a_command(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    argv = [sys.executable, "-m", "automation.supervisor.local_board", "--board", str(tmp_path / "boards" / "x"), "list"]

    result = subprocess.run(argv, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 2
    assert "stop: missing board database" in result.stderr
