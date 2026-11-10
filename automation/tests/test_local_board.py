"""The local board (SPEC-004, RA-BD-002) and the board contract every backend keeps.

The contract tests run once per backend: the local board, and the Hermes backend against the
synthetic board and stand-in ``hermes`` from ``test_kanban_writer``. The local-board tests after
them cover what only the local board has: atomic claims across processes, expiry and renewal, its
schema checks and its command line. None of them needs ``hermes``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class Contract:
    """One backend and the board facts a contract test needs, whatever the backend."""

    backend: BoardBackend
    db: Path
    ready: str  # a ready, unassigned task
    todo: str  # a task waiting on an open parent
    supersede: Callable[[str], None]  # give a task a newer current run than the supervisor's

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

    def supersede(task_id: str) -> None:
        with closing(sqlite3.connect(db)) as conn, conn:
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, claimed_by, claimed_at, claim_expires_at)"
                " VALUES (?, 'running', 'someone-else', 0, 9e18)",
                (task_id,),
            ).lastrowid
            conn.execute(
                "UPDATE task_runs SET status = 'ended', outcome = 'reclaimed' WHERE task_id = ? AND id != ?", (task_id, run_id)
            )
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))

    return Contract(supervisor, db, ready, todo, supersede)


def hermes_contract(board_dir: Path, hermes_cmd: tuple[str, ...]) -> Contract:
    db = board_dir / "kanban.db"

    def supersede(task_id: str) -> None:
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed' WHERE task_id = ?", (task_id,))
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


def test_a_superseded_run_is_not_ours_to_close(contract, handoff):
    held = contract.take()
    contract.supersede(contract.ready)

    with pytest.raises(kp.ProjectionError, match="not ours"):
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
        "board": contract.backend.slug,
        "policy": example["policy"],
        "approved": {contract.ready: example["approved"]["t_000000a3"]},
    }
    overlay = kp.KanbanOverlay.model_validate(document)

    projection = kp.project(contract.backend.snapshot(), overlay)

    [only] = projection.queue["slices"]
    assert (only["slice_id"], only["status"]) == (kp.slice_id_for(contract.backend.slug, contract.ready), "queued")
    assert projection.revision == contract.backend.snapshot().revision


# What only the local board does.


@pytest.fixture
def local(tmp_path, monkeypatch) -> Contract:
    return local_contract(tmp_path, monkeypatch)


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


def test_a_hermes_claim_cannot_be_renewed(board, hermes):
    backend = kw.HermesBackend(board, hermes)
    held = backend.claim(test_kanban_writer.READY, backend.snapshot().revision, 600)

    with pytest.raises(kw.KanbanWriteError, match="can't be renewed"):
        backend.renew(held, 600)


def test_a_board_this_code_cannot_read_is_refused(local, tmp_path):
    other = tmp_path / "boards" / "other-board"
    other.mkdir()
    (other / lb.DB_NAME).write_bytes(local.db.read_bytes())
    with pytest.raises(lb.LocalBoardError, match="is board 'local-board', not 'other-board'"):
        lb.LocalBoard(other).snapshot()

    local.execute(f"PRAGMA user_version = {lb.SCHEMA_VERSION + 1}")
    with pytest.raises(lb.LocalBoardError, match="schema version"):
        local.backend.snapshot()

    with pytest.raises(lb.LocalBoardError, match="missing board database"):
        lb.LocalBoard(tmp_path / "boards" / "no-board").snapshot()
    with pytest.raises(lb.LocalBoardError, match="already exists"):
        lb.init_board(local.db.parent, actor="tester")


def test_a_snapshot_writes_nothing(local):
    before = local.db.read_bytes()
    local.backend.snapshot()
    assert local.db.read_bytes() == before


def test_links_keep_dependencies_honest(local):
    board_ = lb.LocalBoard(local.db.parent, clock=Clock())
    late = board_.add("Release notes")
    assert board_.task(late)["status"] == "ready"

    board_.link(local.todo, late)
    assert board_.task(late)["status"] == "todo"  # a ready task behind an open parent waits
    with pytest.raises(lb.LocalBoardError, match="cycle"):
        board_.link(late, local.todo)
    with pytest.raises(lb.LocalBoardError, match="itself"):
        board_.link(late, late)


def test_approving_a_parent_promotes_its_waiting_children(local, handoff):
    parent = next(t["id"] for t in local.backend.snapshot().tasks if t["title"] == "Add the parser")
    local.execute("UPDATE tasks SET status = 'review' WHERE id = ?", parent)

    assert lb.LocalBoard(local.db.parent, actor="maintainer").approve(parent) == [local.todo]
    assert (local.status(parent), local.status(local.todo)) == ("done", "ready")


def test_unblock_and_archive(local):
    held = local.take()
    local.backend.record(held, "failed", "flaky test", None)
    person = lb.LocalBoard(local.db.parent, actor="maintainer")

    assert person.unblock(local.ready) == "ready"
    with pytest.raises(lb.LocalBoardError, match="not blocked"):
        person.unblock(local.ready)
    local.take()
    with pytest.raises(lb.LocalBoardError, match="running and can't be archived"):
        person.archive(local.ready)


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


def test_the_command_line_offers_no_claim_record_or_renew():
    subcommands = next(a for a in lb.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert not {"claim", "record", "renew"} & set(subcommands.choices)


def test_the_command_line_refuses_a_stale_write_and_reports_why(local, capsys):
    stale = local.backend.snapshot().revision
    local.execute("UPDATE tasks SET priority = 9 WHERE id = ?", local.todo)

    code, _, err = cli(local, capsys, "--expect-revision", stale, "priority", local.ready, "3")
    assert code == 2 and "stop:" in err and "revision" in err
    code, out, _ = cli(local, capsys, "list")
    assert code == 0 and local.ready in out
