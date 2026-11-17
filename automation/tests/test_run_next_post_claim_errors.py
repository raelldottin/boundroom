"""An error after ``run_next`` claims a slice fails the slice; it never leaves it in progress.

Before this, a Git error reading the result, or an exception while replaying validations or
evaluating the handoff, escaped ``_run_slices``. The slice stayed ``in_progress`` with no
failure report, and the next run refused with "queue already has in_progress slice". Each test
fakes one such error and checks the queue, the printed report, the reason code and exit 2.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Optional

import pytest

from automation.supervisor import policy, telemetry
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
