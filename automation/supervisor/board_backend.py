"""The one interface the supervisor uses to reach a Kanban board (SPEC-004, RA-BD-001).

``docs/architecture/agent-agnostic-board.md`` specifies it. The supervisor's Kanban queue
(``run_next.KanbanQueue``) talks to a board only through ``BoardBackend``, so the projection,
the approval overlay and the supervisor's rules don't depend on which board is behind it, or on
which agent harness is the parent.

Today the only backend is Hermes Kanban (``kanban_writer.HermesBackend``). RA-BD-002 adds a local
backend with no Hermes dependency, and with it ``renew``.

Every backend keeps the write-back contract in ``docs/architecture/kanban-cutover.md``:

- ``snapshot`` is read-only, and its ``revision`` is the projection's.
- ``claim`` refuses a stale revision, a task that isn't ``ready`` and an assigned task, without writing.
- ``record`` writes only on the backend's own current run, never writes ``done`` (a supervisor
  ``done`` becomes ``review``), and returns ``False`` when the verdict is already recorded.
- Every refusal raises a ``kanban_projection.ProjectionError``. Nothing is retried or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from automation.supervisor.kanban_projection import BoardSnapshot


@dataclass(frozen=True)
class Claim:
    """The supervisor's hold on one task: the run every later record is conditional on."""

    board: str
    task_id: str
    run_id: int


@runtime_checkable
class BoardBackend(Protocol):
    @property
    def slug(self) -> str: ...

    def snapshot(self) -> BoardSnapshot: ...

    def claim(self, task_id: str, expected_revision: str, ttl_seconds: int) -> Claim: ...

    def record(self, held: Claim, verdict: str, reason: str, handoff_path: Optional[Path]) -> bool: ...
