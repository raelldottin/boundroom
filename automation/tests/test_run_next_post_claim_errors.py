"""An error after ``run_next`` claims a slice fails the slice; it never leaves it in progress.

Before this, a Git error reading the result, an exception while running the agent or recording the
verdict, or a failed write of the verdict escaped ``main``. The slice stayed ``in_progress`` with no
failure report, and the next run refused with "queue already has in_progress slice". Each test
causes one such error for real, in a temporary Git repository with a real queue or board, and
checks the queue, the printed report, the reason code the exported decision span carries, and the
exit code. Git fails on cue through a ``git`` ahead of the real one on PATH, armed by the agent so
that only the calls after the claim fail.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from automation.supervisor import local_board as lb
from automation.tests import test_run_next_local_board as local
from automation.tests import test_run_next_single_slice as single
from automation.tests.test_run_next_single_slice import (
    FIRST,
    SECOND,
    agent_command,
    arm,
    decision_codes,
    statuses,
    supervise,
    trace_flags,
)

consumer = single.consumer
queue = single.queue
traced = single.traced
git_fault = single.git_fault
agent = local.agent
no_hermes = local.no_hermes

needs_permissions = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")

DIRTY_PATHS = "'diff --name-only --relative'"  # the first of git_dirty_paths' three reads
HEAD = "'rev-parse --verify HEAD'"  # git_head, not the handoff check's rev-parse --verify -q HEAD^


@pytest.mark.parametrize(
    ("pattern", "words"),
    [(DIRTY_PATHS, "git command failed"), (HEAD, "cannot read HEAD")],
    ids=["dirty-paths", "head"],
)
def test_a_git_error_after_the_agent_ran_fails_the_slice(queue, git_fault, traced, monkeypatch, capsys, pattern, words):
    path, log = queue
    trigger = git_fault(pattern)

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", arm(trigger, agent_command(log)), *trace_flags(traced)) == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["slice_id"], report["queue_status"], report["decision"]) == (FIRST, "failed", "stop_failed")
    assert report["reason"].startswith(f"Git error after slice {FIRST} ran: ")
    assert words in report["reason"] and "injected failure" in report["reason"]
    assert report["head_sha"] == report["base_sha"]  # the failure's own HEAD read worked
    assert decision_codes(traced) == ["git_error"]


def test_a_rewritten_history_after_the_agent_ran_fails_the_slice(queue, traced, monkeypatch, capsys):
    """HEAD no longer descends from the commit the slice started at, so its commits can't be told apart."""
    path, log = queue
    rewrite = "git checkout -q --orphan rewritten && git -c user.name=a -c user.email=a@example.invalid commit -q -m rewritten"

    assert (
        supervise(monkeypatch, "--single-slice", "--agent-cmd", f"{rewrite} && {agent_command(log)}", *trace_flags(traced)) == 2
    )

    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("failed", "stop_failed")
    assert report["reason"].startswith(f"Git error after slice {FIRST} ran: HEAD no longer descends")
    assert decision_codes(traced) == ["git_error"]


def test_an_agent_command_that_cannot_be_started_fails_the_slice(queue, traced, monkeypatch, capsys):
    """The template names a placeholder there is no value for, so the session itself errors."""
    path, log = queue

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", "echo {bogus}", *trace_flags(traced)) == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["reason"]) == ("failed", "Supervisor error while running slice: 'bogus'")
    assert decision_codes(traced) == ["supervisor_error"]


def test_the_next_run_is_not_refused_after_such_an_error(queue, git_fault, monkeypatch, capsys):
    """The point of the fix: the queue is left with a failed slice, not a stuck one, so it moves on."""
    path, log = queue
    trigger = git_fault(HEAD)
    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", arm(trigger, agent_command(log))) == 2
    capsys.readouterr()

    assert supervise(monkeypatch, "--single-slice") == 0

    assert statuses(path) == {FIRST: "failed", SECOND: "done"}


def test_a_git_error_before_the_claim_still_stops_without_claiming(queue, git_fault, monkeypatch, capsys):
    """Before the claim nothing changes: the error stops the run with exit 2 and the queue is untouched."""
    path, log = queue
    git_fault(DIRTY_PATHS, armed=True)

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "queued", SECOND: "queued"}
    assert single.runs(log) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stop: git command failed" in captured.err


@needs_permissions
def test_a_handoff_that_cannot_be_read_fails_the_slice(queue, traced, monkeypatch, capsys):
    """The agent wrote a valid handoff, then made it unreadable."""
    path, log = queue
    command = f"{agent_command(log)} && chmod 000 {{handoff_file}}"

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", command, *trace_flags(traced)) == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("failed", "stop_failed")
    assert report["reason"].startswith("Invalid handoff artifact: ")
    assert "Permission denied" in report["reason"]
    assert decision_codes(traced) == ["handoff_invalid"]


def test_a_verdict_the_board_refuses_fails_the_slice(
    consumer, agent, no_hermes, git_fault, traced, monkeypatch, capsys, tmp_path
):
    """The board refuses the done verdict once (its handoff vanished before the record); the failure's own
    record works. The verdict's own decision span comes first, then the failure's."""
    board = local.Board(tmp_path)
    # The handoff check's git call is the hook: it runs after the handoff was read and before the record.
    handoffs = shlex.quote(str(consumer / "automation/handoffs"))
    git_fault("'rev-parse --verify -q HEAD^'", action=f"rm -f {handoffs}/*.json", armed=True)

    assert local.supervise(monkeypatch, board, agent, extra=trace_flags(traced)) == 2

    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("failed", "stop_failed")
    assert report["reason"].startswith(f"Supervisor error while handling slice {board.slice_id(board.tasks[0])}: ")
    assert "needs its handoff" in report["reason"]
    assert board.status(board.tasks[0]) == "blocked"
    assert decision_codes(traced) == [None, "supervisor_error"]


@pytest.fixture
def read_only_queue_dir(queue):
    """Make the queue's directory read-only once the agent runs, and give it back afterwards."""
    directory = queue[0].parent
    yield f"chmod 555 {shlex.quote(str(directory))}"
    directory.chmod(0o755)


@needs_permissions
def test_when_no_status_can_be_recorded_the_report_says_the_slice_is_still_in_progress(
    queue, read_only_queue_dir, traced, monkeypatch, capsys
):
    """Every write after the claim fails. The report doesn't claim a failed status the queue doesn't show."""
    path, log = queue

    command = f"{read_only_queue_dir} && {agent_command(log)}"
    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", command, *trace_flags(traced)) == 2

    assert statuses(path) == {FIRST: "in_progress", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("in_progress", "stop_failed")
    assert f"Supervisor error while handling slice {FIRST}: " in report["reason"]
    assert "Recording the failure also failed, so the slice is still in_progress: " in report["reason"]
    assert "Permission denied" in report["reason"]
    assert decision_codes(traced) == [None, "supervisor_error"]


def test_a_local_board_record_of_the_failure_carries_no_digest(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    """A failure before any handoff is recorded on the board as such."""
    board = local.Board(tmp_path)

    assert local.supervise(monkeypatch, board, agent, extra=("--agent-cmd", f"{shlex.quote(sys.executable)} -c pass")) == 2

    capsys.readouterr()
    payload = board.local.events(board.tasks[0])[-1]["payload"]
    assert (payload["handoff_sha256"], payload["block_kind"]) == ("none", "transient")
    assert payload["reason"].startswith("Missing handoff artifact for slice ")
    assert lb.LocalBoard(Path(board.dir)).task(board.tasks[0])["status"] == "blocked"
