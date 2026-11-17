"""A handoff ``run_next`` can't read fails the claimed slice as ``handoff_invalid``.

Before this, only a handoff that failed the schema check did. A handoff that wasn't valid JSON
raised ``ConfigError``, and one that couldn't be read at all, such as a directory at the handoff
path, raised ``OSError``. Either escaped after the claim: the slice stayed ``in_progress`` with
no failure report, and the next run refused with "queue already has in_progress slice".
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from automation.supervisor import telemetry
from automation.tests import test_run_next_single_slice
from automation.tests.test_run_next_single_slice import FIRST, SECOND, statuses, supervise

consumer = test_run_next_single_slice.consumer
queue = test_run_next_single_slice.queue


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


AGENTS = {
    # The agent command leaves this at the handoff path, then exits 0.
    "not_json": "printf 'not json' > {handoff_file}",
    "a_directory": "mkdir -p {handoff_file}",
    "against_the_schema": "printf '{{}}' > {handoff_file}",
}


@pytest.mark.parametrize("agent", AGENTS.values(), ids=AGENTS.keys())
def test_an_unreadable_handoff_fails_the_slice(queue, monkeypatch, capsys, decision_codes, agent):
    path, _ = queue

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", agent) == 2

    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["slice_id"], report["queue_status"], report["decision"]) == (FIRST, "failed", "stop_failed")
    assert report["reason"].startswith("Invalid handoff artifact: ")
    assert decision_codes == ["handoff_invalid"]


@pytest.mark.parametrize("agent", AGENTS.values(), ids=AGENTS.keys())
def test_the_next_run_is_not_stopped_by_the_handoff_left_behind(queue, monkeypatch, capsys, agent):
    """The bad handoff stays in automation/handoffs/. The next run's context scan skips it."""
    path, _ = queue
    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", agent) == 2
    capsys.readouterr()

    assert supervise(monkeypatch, "--single-slice") == 0

    assert statuses(path) == {FIRST: "failed", SECOND: "done"}
