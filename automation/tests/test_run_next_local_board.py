"""``run_next`` on a local board, with no Hermes anywhere (SPEC-004, RA-BD-003).

The consumer repository and the agent script come from ``test_run_next_kanban``. The board is
a local board in a temporary directory outside the repository. ``PATH`` holds only what the
supervisor itself needs (``git`` and ``sh``), and ``--hermes`` names a script that leaves a mark
if anything ever runs it.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from automation.supervisor import kanban_projection as kp
from automation.supervisor import local_board as lb
from automation.supervisor import run_next
from automation.tests import test_run_next_kanban

consumer = test_run_next_kanban.consumer
agent = test_run_next_kanban.agent

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_OVERLAY = json.loads((REPO_ROOT / "automation/examples/kanban-overlay.json").read_text(encoding="utf-8"))
SLUG = "local-board"


class Board:
    def __init__(self, tmp_path: Path, tasks: int = 1) -> None:
        self.dir = tmp_path / "boards" / SLUG
        self.local = lb.init_board(self.dir, actor="maintainer")
        self.tasks = [self.local.add(f"Task {n}", priority=tasks - n) for n in range(tasks)]
        self.overlay = tmp_path / "overlay.json"
        self.write_overlay("local")
        self.hermes_mark = tmp_path / "hermes-was-run"
        self.hermes = tmp_path / "hermes"
        self.hermes.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(self.hermes_mark))}\nexit 1\n", encoding="utf-8")
        self.hermes.chmod(0o755)

    def write_overlay(self, backend: str) -> None:
        approval = EXAMPLE_OVERLAY["approved"]["t_000000a3"]  # no validations, one allowed path
        document = {
            "version": 1,
            "backend": backend,
            "board": SLUG,
            "policy": EXAMPLE_OVERLAY["policy"],
            "approved": {task_id: approval for task_id in self.tasks},
        }
        self.overlay.write_text(json.dumps(document), encoding="utf-8")

    def status(self, task_id: str) -> str:
        return self.local.task(task_id)["status"]

    def slice_id(self, task_id: str) -> str:
        return f"board.{SLUG}.{task_id}"


@pytest.fixture
def no_hermes(monkeypatch):
    """A PATH with git and sh, and no hermes."""
    git, sh = shutil.which("git"), shutil.which("sh")
    assert git and sh
    path = os.pathsep.join(dict.fromkeys([str(Path(git).parent), str(Path(sh).parent)]))
    if shutil.which("hermes", path=path):
        pytest.skip("a hermes executable sits beside git or sh on this machine")
    monkeypatch.setenv("PATH", path)


def supervise(monkeypatch, board: Board, agent: Path, exit_code: int = 0, extra: tuple[str, ...] = ()) -> int:
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} {exit_code}"
    argv = ["run_next", "--board", str(board.dir), "--kanban-overlay", str(board.overlay), "--agent-cmd", command]
    monkeypatch.setattr(sys, "argv", [*argv, "--single-slice", "--hermes", str(board.hermes), *extra])
    return run_next._cli_entry()


def test_one_slice_runs_on_a_local_board_and_waits_for_a_person(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = Board(tmp_path, tasks=2)
    first, second = board.tasks

    assert supervise(monkeypatch, board, agent) == 0

    report = json.loads(capsys.readouterr().out)
    assert (report["slice_id"], report["queue_status"]) == (board.slice_id(first), "done")
    assert (board.status(first), board.status(second)) == ("review", "ready")  # one slice, then stop
    kinds = [(e["kind"], e["actor"]) for e in board.local.events(first)]
    assert ("claimed", lb.SUPERVISOR_ACTOR) in kinds and ("review_requested", lb.SUPERVISOR_ACTOR) in kinds
    assert not any(kind == "approved" for kind, _ in kinds)  # only a person approves
    run = board.local.events(first)[-1]["payload"]
    assert run["slice_id"] == board.slice_id(first) and len(run["handoff_sha256"]) == 64
    assert not board.hermes_mark.exists()
    assert not (consumer / "automation/queue").exists()


def test_a_failed_agent_blocks_the_task_on_a_local_board(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = Board(tmp_path)

    assert supervise(monkeypatch, board, agent, exit_code=3) == 3

    assert json.loads(capsys.readouterr().out)["decision"] == "stop_failed"
    assert board.status(board.tasks[0]) == "blocked"
    blocked = board.local.events(board.tasks[0])[-1]["payload"]
    assert (blocked["block_kind"], blocked["handoff_sha256"]) == ("transient", "none")
    assert not board.hermes_mark.exists()


def test_a_dry_run_never_writes_to_a_local_board(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = Board(tmp_path)
    before = (board.dir / lb.DB_NAME).read_bytes()

    assert supervise(monkeypatch, board, agent, extra=("--dry-run",)) == 0

    assert (board.dir / lb.DB_NAME).read_bytes() == before
    assert board.status(board.tasks[0]) == "ready"


def test_the_backend_flag_must_agree_with_the_overlay(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = Board(tmp_path)

    assert supervise(monkeypatch, board, agent, extra=("--board-backend", "hermes")) == 2
    assert "--board-backend hermes but the overlay is for a local board" in capsys.readouterr().err

    board.write_overlay("hermes")  # a Hermes overlay pointed at a local board looks for kanban.db
    assert supervise(monkeypatch, board, agent) == 2
    assert "missing board database" in capsys.readouterr().err
    assert board.status(board.tasks[0]) == "ready" and not board.hermes_mark.exists()


def test_a_board_inside_the_repository_is_refused(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = Board(tmp_path)
    inside = consumer / "boards" / SLUG
    shutil.copytree(board.dir, inside)
    board.dir = inside

    assert supervise(monkeypatch, board, agent) == 2

    assert "is inside the repository" in capsys.readouterr().err
    assert lb.LocalBoard(inside).task(board.tasks[0])["status"] == "ready"


def test_the_projection_command_reads_a_local_board(no_hermes, capsys, tmp_path):
    board = Board(tmp_path)

    assert kp.main(["--board-dir", str(board.dir), "--overlay", str(board.overlay)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert [s["slice_id"] for s in output["queue"]["slices"]] == [board.slice_id(board.tasks[0])]
    assert output["provenance"][board.slice_id(board.tasks[0])]["backend"] == "local"
    assert output["revision"] == board.local.snapshot().revision
