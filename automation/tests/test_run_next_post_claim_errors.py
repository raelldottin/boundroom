"""An error after ``run_next`` claims a slice fails the slice; it never leaves it in progress.

Before this, a Git error reading the result, an exception while replaying validations or judging
the handoff, or a failed write of the verdict escaped ``main``. The slice stayed ``in_progress``
with no failure report, and the next run refused with "queue already has in_progress slice". Each
test fakes one such error and checks the queue, the printed report, the reason code and exit 2.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from automation.supervisor import policy, run_next, telemetry
from automation.tests import test_run_next_single_slice
from automation.tests.test_run_next_single_slice import FIRST, SECOND, statuses, supervise

consumer = test_run_next_single_slice.consumer
queue = test_run_next_single_slice.queue


def raise_on_call(monkeypatch, name: str, call: int, error: Exception) -> None:
    """Make ``policy.<name>`` raise ``error`` on its ``call``-th call and behave as before otherwise.

    The Git helpers also run before the claim, so the error is aimed at the call after the session.
    """
    original: Callable[..., Any] = getattr(policy, name)
    calls = 0

    def fake(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == call:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(policy, name, fake)


@pytest.fixture
def decision_codes(monkeypatch) -> list[Optional[str]]:
    """The ``error_code`` of every ``boundroom.decision`` mark: the reason code a failure carries."""
    codes: list[Optional[str]] = []
    original = telemetry.Telemetry.mark

    def mark(self, name: str, error_code: Optional[str] = None, **attributes: Any) -> None:
        if name == "boundroom.decision":
            codes.append(error_code)
        original(self, name, error_code, **attributes)

    monkeypatch.setattr(telemetry.Telemetry, "mark", mark)
    return codes


GIT_FAILURE = subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"], stderr="fatal: bad object HEAD")

CASES = [
    # name, failing call, error, reason code, words the reason must carry
    ("git_dirty_paths", 2, policy.ConfigError("git command failed: git diff"), "git_error", "git command failed"),
    ("git_committed_paths", 1, GIT_FAILURE, "git_error", "Git error"),
    ("git_head", 2, policy.ConfigError("cannot read HEAD"), "git_error", "cannot read HEAD"),
    ("replay_validation_commands", 1, RuntimeError("replay broke"), "supervisor_error", "replay broke"),
    ("evaluate_completion", 1, KeyError("policy"), "supervisor_error", "Supervisor error"),
]


@pytest.mark.parametrize(("name", "call", "error", "code", "words"), CASES, ids=[case[0] for case in CASES])
def test_an_error_after_the_claim_fails_the_slice(queue, monkeypatch, capsys, decision_codes, name, call, error, code, words):
    path, _ = queue
    raise_on_call(monkeypatch, name, call, error)

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["slice_id"], report["queue_status"], report["decision"]) == (FIRST, "failed", "stop_failed")
    assert words in report["reason"]
    assert decision_codes == [code]


def test_the_next_run_is_not_refused_after_such_an_error(queue, monkeypatch, capsys):
    """The point of the fix: the queue is left with a failed slice, not a stuck one, so it moves on."""
    path, _ = queue
    raise_on_call(monkeypatch, "git_head", 2, policy.ConfigError("cannot read HEAD"))
    assert supervise(monkeypatch, "--single-slice") == 2
    capsys.readouterr()

    assert supervise(monkeypatch, "--single-slice") == 0

    assert statuses(path) == {FIRST: "failed", SECOND: "done"}


def test_a_git_error_before_the_claim_still_stops_without_claiming(queue, monkeypatch, capsys):
    """Before the claim nothing changes: the error stops the run with exit 2 and the queue is untouched."""
    path, _ = queue
    raise_on_call(monkeypatch, "git_dirty_paths", 1, policy.ConfigError("git command failed: git diff"))

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "queued", SECOND: "queued"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stop: git command failed" in captured.err


def test_a_handoff_that_cannot_be_read_back_fails_the_slice(queue, monkeypatch, capsys, decision_codes):
    """The handoff passed its schema check, then reading its bytes for the trace failed."""
    path, _ = queue
    original = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self.parent.name == "handoffs":
            raise PermissionError(13, "Permission denied", str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("failed", "stop_failed")
    assert "Permission denied" in report["reason"]
    assert decision_codes == ["supervisor_error"]


def test_a_decision_report_that_cannot_be_built_fails_the_slice(queue, monkeypatch, capsys, decision_codes):
    path, _ = queue

    def broken(**kwargs: Any) -> dict:
        raise TypeError("report broke")

    monkeypatch.setattr(run_next, "make_decision_report", broken)

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], "report broke" in report["reason"]) == ("failed", True)
    assert decision_codes == ["supervisor_error"]


def refuse_writes(monkeypatch, refused: Callable[[str, int], bool]) -> None:
    """Make the JSON queue refuse a status write when ``refused(status, write_number)`` says so."""
    original = run_next.JsonQueue.set_status
    writes = 0

    def set_status(self, slice_record: dict, status: str, *args: Any, **kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        if refused(status, writes):
            raise OSError(28, "No space left on device")
        original(self, slice_record, status, *args, **kwargs)

    monkeypatch.setattr(run_next.JsonQueue, "set_status", set_status)


def test_a_verdict_that_cannot_be_recorded_fails_the_slice(queue, monkeypatch, capsys, decision_codes):
    """The final write fails once, as a busy board or a full disk might; fail()'s own write works."""
    path, _ = queue
    refuse_writes(monkeypatch, lambda status, write: status == "done")

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("failed", "stop_failed")
    assert "No space left on device" in report["reason"]
    # The verdict's own decision mark, then the failure's.
    assert decision_codes[-1] == "supervisor_error"


def test_when_no_status_can_be_recorded_the_report_says_the_slice_is_still_in_progress(queue, monkeypatch, capsys):
    """Every write after the claim fails. The report doesn't claim a failed status the queue doesn't show."""
    path, _ = queue
    refuse_writes(monkeypatch, lambda status, write: status != "in_progress")

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "in_progress", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["decision"]) == ("in_progress", "stop_failed")
    assert "Recording the failure also failed, so the slice is still in_progress" in report["reason"]
