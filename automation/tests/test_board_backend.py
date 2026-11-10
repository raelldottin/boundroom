"""The board interface the supervisor uses (SPEC-004, RA-BD-001).

The board, the stand-in ``hermes`` and its call log come from ``test_kanban_writer``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from automation.supervisor import kanban_projection as kp
from automation.supervisor import kanban_writer as kw
from automation.supervisor import run_next
from automation.supervisor.board_backend import BoardBackend, Claim
from automation.tests import test_kanban_writer
from automation.tests.test_kanban_writer import BOARD, READY, calls, row

board = test_kanban_writer.board
hermes = test_kanban_writer.hermes
handoff = test_kanban_writer.handoff

OVERLAY = Path(__file__).resolve().parents[2] / "automation/examples/kanban-overlay.json"


class RecordingBackend:
    """A board backend with no Hermes behind it: a fixed snapshot, and claims and records kept in lists."""

    def __init__(self, snapshot: kp.BoardSnapshot) -> None:
        self._snapshot = snapshot
        self.claims: list[tuple[str, str, int]] = []
        self.records: list[tuple[Claim, str, str, Optional[Path]]] = []

    @property
    def slug(self) -> str:
        return self._snapshot.board

    def snapshot(self) -> kp.BoardSnapshot:
        return self._snapshot

    def claim(self, task_id: str, expected_revision: str, ttl_seconds: int) -> Claim:
        self.claims.append((task_id, expected_revision, ttl_seconds))
        return Claim(self.slug, task_id, len(self.claims))

    def renew(self, held: Claim, ttl_seconds: int) -> Claim:
        return held

    def record(self, held: Claim, verdict: str, reason: str, handoff_path: Optional[Path]) -> bool:
        self.records.append((held, verdict, reason, handoff_path))
        return True


def test_the_hermes_backend_is_a_board_backend(board, hermes):
    backend = kw.HermesBackend(board, hermes)

    assert isinstance(backend, BoardBackend)
    assert backend.slug == BOARD
    assert backend.snapshot() == kp.read_board(board)


def test_the_hermes_backend_claims_and_records_through_hermes(board, hermes, handoff, tmp_path):
    backend = kw.HermesBackend(board, hermes)

    held = backend.claim(READY, backend.snapshot().revision, 600)
    assert held == Claim(BOARD, READY, 1)
    assert backend.record(held, "done", "ok", handoff) is True

    assert row(board, "SELECT status FROM tasks WHERE id = ?", READY) == ("review",)
    assert [call["argv"][3] for call in calls(tmp_path)] == ["claim", "request-review"]


def test_the_kanban_queue_reaches_the_board_only_through_its_backend(board, handoff, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")  # no hermes anywhere: the queue must not need one
    backend = RecordingBackend(kp.read_board(board))
    assert isinstance(backend, BoardBackend)
    queue = run_next.KanbanQueue(backend, OVERLAY)

    queue_data = queue.load()
    slice_record = next(s for s in queue_data["slices"] if s["slice_id"] == f"kanban.{BOARD}.{READY}")
    queue.set_status(slice_record, "in_progress")
    queue.set_status(slice_record, "done", "ok", handoff)

    ttl = queue.overlay.policy.handoff_timeout_seconds + run_next.CLAIM_MARGIN_SECONDS
    assert backend.claims == [(READY, backend.snapshot().revision, ttl)]
    assert backend.records == [(Claim(BOARD, READY, 1), "done", "ok", handoff)]
    assert calls(tmp_path) == []  # the stand-in hermes was never run
