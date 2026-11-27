"""Lane attempts outlive the run that made them (SPEC-014 RA-LANE-004, RA-SYM-003 for the JSON queue).

This is a library. Nothing calls it yet: ``run_next`` is unchanged, and RA-LANE-005 wires it into
``run_lanes`` and adds its ``--reconcile`` flag. It has four parts.

- **Attempt records.** One JSON file per lane attempt, at ``<lane root>/.attempts/<attempt_id>.json``,
  beside the worktrees ``lane_workspace`` makes. No attempt id starts with a dot, so the directory
  can't collide with a worktree. The controller writes the record before it claims the slice or
  makes the worktree, and rewrites it when it reads a new head, when the supervisor judges the
  slice, when integration starts and ends, and when the queue's status is written (the record is
  then *closed*). Every write is atomic: a temporary file in the same directory, flushed with
  ``fsync``, renamed over the record, and the directory flushed too. A crash mid-write leaves the
  previous record and a temporary file ending in ``.tmp``, which every read ignores.
- **Reconcile.** ``decide_reconcile`` is a pure decision over facts: the queue, each open record,
  whether its worktree exists, and whether its judged head is on the integration branch.
  ``reconcile`` gathers those facts from Git and the lane root, writes the queue once, durably, and
  then closes the records. SPEC-014 says a slice whose recorded head is already on the integration
  branch is marked ``done``. The Symphony decision (RA-SYM-003) says an unknown earlier outcome
  never becomes ``done``. This takes the reading that satisfies both: an ``in_progress`` slice
  becomes ``done`` only when its one open record shows the supervisor's own ``done`` verdict on a
  judged head, that exact head is an ancestor of the integration branch the record names, and the
  record doesn't show an integration that was refused or a merge that never finished. That is the
  case of an integration that landed whose status write was lost. Every other ``in_progress`` slice
  becomes ``failed`` with ``supervisor_error``. A head the supervisor didn't judge is never ``done``.
  Reconcile never removes a worktree: a ``done`` slice's is removed by
  ``lane_workspace.retain_or_remove``, as after any integration, and every other one is kept.
- **Refusing to start.** ``start_refusal`` refuses while any slice is ``in_progress``, with
  ``run_next``'s own message and exit code 2, or while any attempt record is open or unreadable.
  ``board_refusal`` refuses ``--board`` and ``--kanban-board`` (SPEC-014 "Board support").
- **Interrupts.** ``interrupt_handling`` turns ``SIGTERM`` and ``SIGHUP`` into ``Terminated``, a
  ``KeyboardInterrupt``. On either it stops every running lane runner by process group (``SIGTERM``,
  then ``SIGKILL`` after one shared grace period, through ``lane_workspace``'s process-group stop),
  lets the caller record their slices as failed (``record_interrupted`` does that for the JSON
  queue), and re-raises.

Clock and paths are parameters: the pure parts take ``now`` and the lane root, and only the shell
functions at the end touch Git, files, signals or processes.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Generator, Mapping, Optional, Sequence

from automation.supervisor import lane_workspace, policy, run_next

RECORD_FORMAT = "boundroom.lane-attempt/1"
ATTEMPTS_DIRNAME = ".attempts"
RECORD_SUFFIX = ".json"
# What reconcile and an interrupt record for a slice whose outcome isn't known. It is the failure
# reason run_next records for an error before the verdict (docs/reference/exit-codes.md).
UNKNOWN_OUTCOME = "supervisor_error"
# The exit code for a refusal before anything is claimed, an in_progress slice included, as run_next uses.
REFUSAL_EXIT_CODE = 2
DEFAULT_STOP_GRACE_SECONDS = 10.0
RECONCILE_HINT = "run 'run_lanes --reconcile' to resolve it"
# What a closed record says when its slice has left the queue.
NOT_IN_QUEUE = "not_in_queue"
BRANCH_REF_PREFIX = "refs/heads/"

# The statuses a supervisor's verdict can record. queued and in_progress are never a verdict.
VERDICT_STATUSES = frozenset({"done", "failed", "blocked", "deferred"})
# What a closed record can say the queue recorded.
CLOSURE_STATUSES = frozenset({"queued", "blocked", "deferred", "done", "failed", NOT_IN_QUEUE})
# The signals that stop a run as Ctrl-C does: a terminate, and a closed terminal.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class AttemptRecordError(ValueError):
    """An attempt record is invalid, or a change to it is not allowed."""


class IntegrationMethod(StrEnum):
    FAST_FORWARD = "fast_forward"
    MERGE = "merge"


class IntegrationOutcome(StrEnum):
    LANDED = "landed"
    REFUSED = "refused"


@dataclass(frozen=True)
class Judgement:
    """The supervisor's verdict on a lane: the head it judged and the status it chose."""

    head: str
    queue_status: str
    judged_at: str


@dataclass(frozen=True)
class Integration:
    """How the controller integrates a judged head, and how that ended. No outcome yet means it never finished."""

    method: IntegrationMethod
    started_at: str
    outcome: Optional[IntegrationOutcome] = None
    finished_at: Optional[str] = None


@dataclass(frozen=True)
class Closure:
    """The status the queue records for the attempt's slice, written before the record is closed."""

    queue_status: str
    reason: str
    closed_at: str


@dataclass(frozen=True)
class AttemptRecord:
    slice_id: str
    attempt_id: str
    run_id: str
    base_sha: str
    worktree: Path
    branch: str
    integration_ref: str
    started_at: str
    recorded_head: str
    judgement: Optional[Judgement] = None
    integration: Optional[Integration] = None
    closure: Optional[Closure] = None

    @property
    def is_open(self) -> bool:
        return self.closure is None


@dataclass(frozen=True)
class UnreadableAttempt:
    """A file in the attempts directory that isn't a valid record."""

    path: Path
    error: str


LoadedAttempt = AttemptRecord | UnreadableAttempt


# ---------------------------------------------------------------------------
# Pure rules: records and their changes
# ---------------------------------------------------------------------------


def timestamp(now: datetime) -> str:
    """*now* as UTC, to the second, in ISO 8601."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _component(value: str, what: str) -> str:
    try:
        return lane_workspace.check_component(value, what)
    except lane_workspace.LaneRefused as error:
        raise AttemptRecordError(str(error)) from error


def _sha(value: str, what: str) -> str:
    try:
        return lane_workspace.check_full_sha(value, what)
    except lane_workspace.LaneRefused as error:
        raise AttemptRecordError(str(error)) from error


def check_integration_ref(value: str) -> str:
    """A full branch ref, each of whose components is safe by ``lane_workspace``'s rule.

    ``HEAD`` is refused: it is resolved when reconcile runs, not when the integration ran, so a
    person checking out another branch would change what "landed" means.
    """
    name = value.removeprefix(BRANCH_REF_PREFIX)
    if name == value or not name:
        raise AttemptRecordError(f"integration ref {value!r} must be a full branch ref such as refs/heads/main")
    for component in name.split("/"):
        _component(component, "integration ref component")
    return value


def new_attempt(
    *,
    slice_id: str,
    attempt_id: str,
    run_id: str,
    base_sha: str,
    lane_root: Path,
    integration_ref: str,
    now: datetime,
) -> AttemptRecord:
    """The record of an attempt about to start: its worktree and branch are the ones ``lane_workspace.create`` makes."""
    if not run_id.strip():
        raise AttemptRecordError("an attempt record needs the run id")
    if not lane_root.is_absolute():
        raise AttemptRecordError(f"lane root {lane_root} must be an absolute path")
    _component(slice_id, "slice id")
    _component(attempt_id, "attempt id")
    return AttemptRecord(
        slice_id=slice_id,
        attempt_id=attempt_id,
        run_id=run_id,
        base_sha=_sha(base_sha, "base"),
        worktree=lane_root / attempt_id,
        branch=lane_workspace.lane_branch(slice_id, attempt_id),
        integration_ref=check_integration_ref(integration_ref),
        started_at=timestamp(now),
        recorded_head=base_sha,
    )


def _still_open(record: AttemptRecord, change: str) -> None:
    if not record.is_open:
        raise AttemptRecordError(f"attempt {record.attempt_id} is closed; it can't record {change}")


def with_head(record: AttemptRecord, head: str) -> AttemptRecord:
    """Record a head the controller read in the lane before its verdict, such as after the runner exits."""
    _still_open(record, "a new head")
    if record.judgement is not None:
        raise AttemptRecordError(f"attempt {record.attempt_id} was judged at {record.judgement.head}; its head is fixed")
    return replace(record, recorded_head=_sha(head, "recorded head"))


def judged(record: AttemptRecord, head: str, queue_status: str, now: datetime) -> AttemptRecord:
    """Record the supervisor's verdict: the head it judged and the status it chose for the slice."""
    _still_open(record, "a verdict")
    if record.judgement is not None:
        raise AttemptRecordError(f"attempt {record.attempt_id} was already judged")
    if queue_status not in VERDICT_STATUSES:
        raise AttemptRecordError(f"{queue_status!r} is not a verdict status; use one of {sorted(VERDICT_STATUSES)}")
    _sha(head, "judged head")
    return replace(record, recorded_head=head, judgement=Judgement(head, queue_status, timestamp(now)))


def integration_started(record: AttemptRecord, method: IntegrationMethod, now: datetime) -> AttemptRecord:
    """Record that integration of the judged head starts, before the integration branch can move."""
    _still_open(record, "an integration")
    if record.judgement is None or record.judgement.queue_status != "done":
        raise AttemptRecordError(f"attempt {record.attempt_id} has no done verdict to integrate")
    if record.integration is not None:
        raise AttemptRecordError(f"attempt {record.attempt_id} already started its integration")
    return replace(record, integration=Integration(IntegrationMethod(method), timestamp(now)))


def integration_finished(record: AttemptRecord, outcome: IntegrationOutcome, now: datetime) -> AttemptRecord:
    _still_open(record, "an integration outcome")
    if record.integration is None or record.integration.outcome is not None:
        raise AttemptRecordError(f"attempt {record.attempt_id} has no integration in progress to finish")
    finished = replace(record.integration, outcome=IntegrationOutcome(outcome), finished_at=timestamp(now))
    return replace(record, integration=finished)


def closed(record: AttemptRecord, queue_status: str, reason: str, now: datetime) -> AttemptRecord:
    """Close the attempt once the queue records *queue_status* for its slice. A closed record never changes."""
    _still_open(record, "a second closure")
    if queue_status not in CLOSURE_STATUSES:
        raise AttemptRecordError(f"{queue_status!r} is not a status a closed record can hold")
    if not reason.strip():
        raise AttemptRecordError("a closed record needs a reason")
    return replace(record, closure=Closure(queue_status, reason, timestamp(now)))


def to_json(record: AttemptRecord) -> dict[str, Any]:
    judgement, integration, closure = record.judgement, record.integration, record.closure
    return {
        "format": RECORD_FORMAT,
        "slice_id": record.slice_id,
        "attempt_id": record.attempt_id,
        "run_id": record.run_id,
        "base_sha": record.base_sha,
        "worktree": str(record.worktree),
        "branch": record.branch,
        "integration_ref": record.integration_ref,
        "started_at": record.started_at,
        "recorded_head": record.recorded_head,
        "judgement": None
        if judgement is None
        else {"head": judgement.head, "queue_status": judgement.queue_status, "judged_at": judgement.judged_at},
        "integration": None
        if integration is None
        else {
            "method": integration.method.value,
            "started_at": integration.started_at,
            "outcome": None if integration.outcome is None else integration.outcome.value,
            "finished_at": integration.finished_at,
        },
        "closure": None
        if closure is None
        else {"queue_status": closure.queue_status, "reason": closure.reason, "closed_at": closure.closed_at},
    }


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AttemptRecordError(f"{key!r} must be a non-empty string")
    return value


def _section(data: Mapping[str, Any], key: str) -> Optional[Mapping[str, Any]]:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AttemptRecordError(f"{key!r} must be an object or null")
    return value


def _choice(value: str, choices: frozenset[str], what: str) -> str:
    if value not in choices:
        raise AttemptRecordError(f"{what} {value!r} is not one of {sorted(choices)}")
    return value


def from_json(data: Any) -> AttemptRecord:
    """Read a record back, refusing what the changes above could never produce."""
    if not isinstance(data, dict):
        raise AttemptRecordError("an attempt record must be a JSON object")
    if data.get("format") != RECORD_FORMAT:
        raise AttemptRecordError(f"format is {data.get('format')!r}, not {RECORD_FORMAT!r}")
    slice_id = _component(_text(data, "slice_id"), "slice id")
    attempt_id = _component(_text(data, "attempt_id"), "attempt id")
    branch = _text(data, "branch")
    if branch != lane_workspace.lane_branch(slice_id, attempt_id):
        raise AttemptRecordError(f"branch {branch!r} is not the lane branch of attempt {attempt_id}")
    worktree = Path(_text(data, "worktree"))
    if not worktree.is_absolute() or worktree.name != attempt_id:
        raise AttemptRecordError(f"worktree {worktree} is not an absolute lane path ending in {attempt_id}")
    judgement_data = _section(data, "judgement")
    integration_data = _section(data, "integration")
    closure_data = _section(data, "closure")
    judgement = (
        None
        if judgement_data is None
        else Judgement(
            head=_sha(_text(judgement_data, "head"), "judged head"),
            queue_status=_choice(_text(judgement_data, "queue_status"), VERDICT_STATUSES, "judgement status"),
            judged_at=_text(judgement_data, "judged_at"),
        )
    )
    recorded_head = _sha(_text(data, "recorded_head"), "recorded head")
    if judgement is not None and recorded_head != judgement.head:
        raise AttemptRecordError(f"recorded head {recorded_head} is not judged head {judgement.head}")
    integration = None
    if integration_data is not None:
        if judgement is None or judgement.queue_status != "done":
            raise AttemptRecordError("an integration needs a done verdict")
        outcome = integration_data.get("outcome")
        finished_at = integration_data.get("finished_at")
        if (outcome is None) != (finished_at is None):
            raise AttemptRecordError("an integration outcome and its finish time go together")
        try:
            integration = Integration(
                method=IntegrationMethod(_text(integration_data, "method")),
                started_at=_text(integration_data, "started_at"),
                outcome=None if outcome is None else IntegrationOutcome(_text(integration_data, "outcome")),
                finished_at=None if finished_at is None else _text(integration_data, "finished_at"),
            )
        except AttemptRecordError:
            raise
        except ValueError as error:  # an unknown method or outcome
            raise AttemptRecordError(str(error)) from error
    closure = (
        None
        if closure_data is None
        else Closure(
            queue_status=_choice(_text(closure_data, "queue_status"), CLOSURE_STATUSES, "closure status"),
            reason=_text(closure_data, "reason"),
            closed_at=_text(closure_data, "closed_at"),
        )
    )
    return AttemptRecord(
        slice_id=slice_id,
        attempt_id=attempt_id,
        run_id=_text(data, "run_id"),
        base_sha=_sha(_text(data, "base_sha"), "base"),
        worktree=worktree,
        branch=branch,
        integration_ref=check_integration_ref(_text(data, "integration_ref")),
        started_at=_text(data, "started_at"),
        recorded_head=recorded_head,
        judgement=judgement,
        integration=integration,
        closure=closure,
    )


# ---------------------------------------------------------------------------
# Pure rules: reconcile, refusals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptFacts:
    """What the shell learned about one record. ``judged_head_landed`` is None when unchecked or unknown."""

    record: AttemptRecord
    worktree_exists: bool
    judged_head_landed: Optional[bool]


@dataclass(frozen=True)
class SliceResolution:
    """What reconcile records for one ``in_progress`` slice, and the worktrees of its attempts that still exist."""

    slice_id: str
    queue_status: str
    reason_code: str
    reason: str
    attempt_ids: tuple[str, ...]
    worktrees: tuple[Path, ...]


@dataclass(frozen=True)
class RecordClosure:
    attempt_id: str
    queue_status: str
    reason: str


@dataclass(frozen=True)
class ReconcilePlan:
    slices: tuple[SliceResolution, ...]
    closures: tuple[RecordClosure, ...]
    unreadable: tuple[Path, ...]


def _existing(facts: Sequence[AttemptFacts]) -> tuple[Path, ...]:
    return tuple(fact.record.worktree for fact in facts if fact.worktree_exists)


def _failed(slice_id: str, reason: str, facts: Sequence[AttemptFacts]) -> SliceResolution:
    return SliceResolution(
        slice_id=slice_id,
        queue_status="failed",
        reason_code=UNKNOWN_OUTCOME,
        reason=f"{reason} Its worktree, if any, is kept.",
        attempt_ids=tuple(fact.record.attempt_id for fact in facts),
        worktrees=_existing(facts),
    )


def resolve_slice(slice_id: str, facts: Sequence[AttemptFacts], records_in_doubt: bool) -> SliceResolution:
    """``done`` only for a judged ``done`` head that landed; ``failed`` with ``supervisor_error`` otherwise."""
    if records_in_doubt:
        return _failed(slice_id, "An attempt record couldn't be read, so which attempt was this slice's is in doubt.", facts)
    if not facts:
        return _failed(slice_id, "No open attempt record names this slice, so its outcome is unknown.", facts)
    if len(facts) > 1:
        ids = ", ".join(fact.record.attempt_id for fact in facts)
        return _failed(slice_id, f"Several open attempt records name this slice ({ids}), so its outcome is in doubt.", facts)
    fact = facts[0]
    record = fact.record
    judgement = record.judgement
    if judgement is None:
        return _failed(slice_id, f"The supervisor never judged attempt {record.attempt_id}.", facts)
    if judgement.queue_status != "done":
        return _failed(
            slice_id,
            f"The supervisor judged attempt {record.attempt_id} {judgement.queue_status}, and that status was never written.",
            facts,
        )
    integration = record.integration
    if integration is not None and integration.outcome is IntegrationOutcome.REFUSED:
        return _failed(slice_id, f"Integration of attempt {record.attempt_id} was refused.", facts)
    if integration is not None and integration.outcome is None and integration.method is IntegrationMethod.MERGE:
        return _failed(slice_id, f"The merge of attempt {record.attempt_id} started and was never recorded as finished.", facts)
    if fact.judged_head_landed is None:
        return _failed(
            slice_id, f"Git couldn't tell whether {record.integration_ref} contains judged head {judgement.head}.", facts
        )
    if not fact.judged_head_landed:
        return _failed(
            slice_id, f"{record.integration_ref} doesn't contain judged head {judgement.head}, so it never landed.", facts
        )
    return SliceResolution(
        slice_id=slice_id,
        queue_status="done",
        reason_code="",
        reason=(
            f"The supervisor judged attempt {record.attempt_id} done at {judgement.head}, and {record.integration_ref} "
            "contains that head: it landed, and only the status write was lost."
        ),
        attempt_ids=(record.attempt_id,),
        worktrees=_existing(facts),
    )


def _coded(resolution: SliceResolution) -> str:
    return f"{resolution.reason_code}: {resolution.reason}" if resolution.reason_code else resolution.reason


def decide_reconcile(queue_data: dict, facts: Sequence[AttemptFacts], unreadable: Sequence[UnreadableAttempt]) -> ReconcilePlan:
    """Resolve every ``in_progress`` slice, and close every open record. Only ``in_progress`` slices change."""
    open_facts = [fact for fact in facts if fact.record.is_open]
    resolutions: dict[str, SliceResolution] = {}
    for slice_record in queue_data["slices"]:
        if slice_record["status"] != "in_progress":
            continue
        slice_id = slice_record["slice_id"]
        mine = [fact for fact in open_facts if fact.record.slice_id == slice_id]
        resolutions[slice_id] = resolve_slice(slice_id, mine, records_in_doubt=bool(unreadable))
    statuses = {slice_record["slice_id"]: slice_record["status"] for slice_record in queue_data["slices"]}
    closures: list[RecordClosure] = []
    for fact in open_facts:
        slice_id = fact.record.slice_id
        resolution = resolutions.get(slice_id)
        if resolution is not None:
            closures.append(RecordClosure(fact.record.attempt_id, resolution.queue_status, _coded(resolution)))
        elif slice_id in statuses:
            closures.append(
                RecordClosure(fact.record.attempt_id, statuses[slice_id], f"The queue already records {statuses[slice_id]}.")
            )
        else:
            closures.append(RecordClosure(fact.record.attempt_id, NOT_IN_QUEUE, "The queue no longer has this slice."))
    return ReconcilePlan(
        slices=tuple(resolutions.values()),
        closures=tuple(closures),
        unreadable=tuple(item.path for item in unreadable),
    )


def plan_report(plan: ReconcilePlan) -> dict[str, Any]:
    """The plan as JSON, for the decision report and for a person.

    ``worktrees`` lists the attempt's worktrees that still exist. A ``done`` slice's is removed with
    ``lane_workspace.retain_or_remove``; until then the next lane can't start.
    """
    return {
        "slices": [
            {
                "slice_id": item.slice_id,
                "queue_status": item.queue_status,
                "reason_code": item.reason_code,
                "reason": item.reason,
                "attempt_ids": list(item.attempt_ids),
                "worktrees": [str(path) for path in item.worktrees],
            }
            for item in plan.slices
        ],
        "closed_attempts": [item.attempt_id for item in plan.closures],
        "unreadable_records": [str(path) for path in plan.unreadable],
    }


@dataclass(frozen=True)
class Refusal:
    message: str
    exit_code: int = REFUSAL_EXIT_CODE


def start_refusal(queue_data: dict, attempts: Sequence[LoadedAttempt]) -> Optional[Refusal]:
    """Refuse to start while an attempt is unreconciled: a slice ``in_progress``, or an open or unreadable record."""
    active = run_next.active_refusal(queue_data)
    if active:
        return Refusal(f"{active}; {RECONCILE_HINT}")
    for attempt in attempts:
        if isinstance(attempt, UnreadableAttempt):
            return Refusal(f"stop: lane attempt record {attempt.path} can't be read ({attempt.error}); {RECONCILE_HINT}")
        if attempt.is_open:
            return Refusal(
                f"stop: lane attempt {attempt.attempt_id} for slice {attempt.slice_id} was never closed; {RECONCILE_HINT}"
            )
    return None


def board_refusal(board: Optional[str]) -> Optional[Refusal]:
    """Lanes work with the JSON queue only (SPEC-014 "Board support")."""
    if not board:
        return None
    return Refusal(
        "stop: lanes work with the JSON queue only. --board and --kanban-board are refused until board claims "
        "can be renewed per lane (SPEC-014, Board support)."
    )


# ---------------------------------------------------------------------------
# The file system: attempt records and the queue
# ---------------------------------------------------------------------------


def attempts_dir(lane_root: Path) -> Path:
    return lane_root / ATTEMPTS_DIRNAME


def record_path(lane_root: Path, attempt_id: str) -> Path:
    return attempts_dir(lane_root) / f"{_component(attempt_id, 'attempt id')}{RECORD_SUFFIX}"


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durably(path: Path, text: str, prefix: str) -> None:
    """Write *text* to *path* atomically and durably: temporary file, fsync, rename, directory fsync."""
    directory = path.parent
    descriptor, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    _fsync_dir(directory)


def write_record(lane_root: Path, record: AttemptRecord, *, new: bool = False) -> Path:
    """Write *record* atomically. With *new*, refuse when the attempt already has a record."""
    path = record_path(lane_root, record.attempt_id)
    lane_workspace.make_private_dirs(path.parent)
    if new and path.exists():
        raise AttemptRecordError(f"attempt {record.attempt_id} already has a record at {path}; each attempt needs its own id")
    _write_durably(path, json.dumps(to_json(record), indent=2, sort_keys=True) + "\n", f".{record.attempt_id}.")
    return path


def write_queue(queue_path: Path, queue_data: dict) -> None:
    """Write the queue as ``policy.write_json`` does, and durably, because closing a record relies on it."""
    _write_durably(queue_path, json.dumps(queue_data, indent=2, ensure_ascii=False) + "\n", f".{queue_path.name}.")


def read_records(lane_root: Path) -> list[LoadedAttempt]:
    """Every record under *lane_root*, by file name. A temporary file a crash left ends in ``.tmp`` and is skipped."""
    directory = attempts_dir(lane_root)
    if not directory.is_dir():
        return []
    loaded: list[LoadedAttempt] = []
    for path in sorted(directory.glob(f"*{RECORD_SUFFIX}")):
        try:
            record = from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttemptRecordError) as error:
            loaded.append(UnreadableAttempt(path, str(error)))
            continue
        if path.name != f"{record.attempt_id}{RECORD_SUFFIX}":
            loaded.append(UnreadableAttempt(path, f"the file holds attempt {record.attempt_id}"))
        elif record.worktree.parent != lane_root:
            loaded.append(UnreadableAttempt(path, f"its worktree {record.worktree} is not under {lane_root}"))
        else:
            loaded.append(record)
    return loaded


def set_aside(path: Path, now: datetime) -> Path:
    """Rename an unreadable record so it stops blocking, and keep it for a person to read."""
    stem = f"{path.name}.unreadable-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    target, number = path.with_name(stem), 1
    while target.exists():
        number += 1
        target = path.with_name(f"{stem}-{number}")
    os.replace(path, target)
    return target


# ---------------------------------------------------------------------------
# The shell: Git, the queue, signals and processes
# ---------------------------------------------------------------------------


def gather_facts(repo_root: Path, records: Sequence[AttemptRecord], environ: Mapping[str, str]) -> list[AttemptFacts]:
    """The facts about each open record. Git is asked only about a judged ``done`` head."""
    facts: list[AttemptFacts] = []
    for record in records:
        if not record.is_open:
            continue
        landed: Optional[bool] = None
        if record.judgement is not None and record.judgement.queue_status == "done":
            try:
                landed = lane_workspace.is_ancestor(repo_root, record.judgement.head, record.integration_ref, environ)
            except lane_workspace.LaneWorkspaceError:
                landed = None
        facts.append(AttemptFacts(record, record.worktree.is_dir(), landed))
    return facts


def apply_plan(
    plan: ReconcilePlan,
    queue_data: dict,
    queue_path: Path,
    lane_root: Path,
    records: Sequence[AttemptRecord],
    now: datetime,
) -> None:
    """Write the queue first, durably, then close the records, then set unreadable ones aside.

    A crash between the steps leaves open records for slices the queue no longer has
    ``in_progress``, and the next reconcile closes them without changing the queue.
    """
    if plan.slices:
        for item in plan.slices:
            queue_data = policy.set_slice_status(queue_data, item.slice_id, item.queue_status)
        write_queue(queue_path, queue_data)
    by_id = {record.attempt_id: record for record in records}
    for closure in plan.closures:
        write_record(lane_root, closed(by_id[closure.attempt_id], closure.queue_status, closure.reason, now))
    for path in plan.unreadable:
        set_aside(path, now)


def reconcile(repo_root: Path, queue_path: Path, lane_root: Path, environ: Mapping[str, str], *, now: datetime) -> ReconcilePlan:
    """Resolve every ``in_progress`` slice of the JSON queue at *queue_path* from the records under *lane_root*.

    The caller holds ``runner_profile.run_lock``, so no runner or other run writes the queue meanwhile.
    """
    queue_data = policy.load_queue(queue_path)
    loaded = read_records(lane_root)
    records = [item for item in loaded if isinstance(item, AttemptRecord)]
    unreadable = [item for item in loaded if isinstance(item, UnreadableAttempt)]
    plan = decide_reconcile(queue_data, gather_facts(repo_root, records, environ), unreadable)
    apply_plan(plan, queue_data, queue_path, lane_root, records, now)
    return plan


def check_start(queue_data: dict, lane_root: Path) -> Optional[Refusal]:
    """``start_refusal`` over the records under *lane_root*."""
    return start_refusal(queue_data, read_records(lane_root))


class Terminated(KeyboardInterrupt):
    """A stop signal arrived while lanes were running. A caller exits with ``128 + signum``, not Ctrl-C's 130."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class RunningLane:
    """A lane whose runner is running. The runner leads its own process group (``start_new_session=True``)."""

    record: AttemptRecord
    process: subprocess.Popen[Any]


def stop_lanes(lanes: Sequence[RunningLane], grace_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
    """Stop every lane's process group: each gets one ``SIGTERM``, all share one grace period, then ``SIGKILL``.

    A lane whose leader was already reaped is still signalled, so a grandchild it left doesn't
    outlive the run. The group's id can't be reused while any member is alive.
    """
    if grace_seconds > 0:
        deadline = clock() + grace_seconds
        for lane in lanes:
            lane_workspace.signal_group(lane.process.pid, signal.SIGTERM)
        for lane in lanes:
            lane_workspace.wait_for_group(lane.process, deadline, clock=clock)
    for lane in lanes:
        lane_workspace.stop_process_group(lane.process)


def _raise_terminated(signum: int, frame: object) -> None:
    raise Terminated(signum)


def _set_handlers(handler: Any, signals: Sequence[int]) -> dict[int, Any]:
    previous = {}
    for signum in signals:
        old = signal.signal(signum, handler)
        previous[signum] = signal.SIG_DFL if old is None else old
    return previous


@contextlib.contextmanager
def interrupt_handling(
    running: Callable[[], Sequence[RunningLane]],
    on_stopped: Callable[[Sequence[RunningLane]], None],
    grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
) -> Generator[None, None, None]:
    """On ``KeyboardInterrupt``, ``SIGTERM`` or ``SIGHUP`` in the body, stop the lanes, call *on_stopped*, re-raise.

    Signals are handled only in the main thread, where Python delivers them. While the lanes are
    being stopped and recorded, ``SIGINT`` and the stop signals are ignored, so a second Ctrl-C can't
    leave runners orphaned; the previous handlers come back on exit. If *on_stopped* fails, the
    interrupt still propagates, with a note: the slices stay ``in_progress`` with open records, and
    the next start refuses until reconcile records them as failed.
    """
    main = threading.current_thread() is threading.main_thread()
    previous = _set_handlers(_raise_terminated, STOP_SIGNALS) if main else {}
    try:
        yield
    except KeyboardInterrupt as interrupt:
        if main:
            interrupt_handler = signal.getsignal(signal.SIGINT)
            previous = {signal.SIGINT: signal.SIG_DFL if interrupt_handler is None else interrupt_handler, **previous}
            _set_handlers(signal.SIG_IGN, (signal.SIGINT, *STOP_SIGNALS))
        lanes = list(running())
        stop_lanes(lanes, grace_seconds)
        try:
            on_stopped(lanes)
        except Exception as error:
            interrupt.add_note(f"recording the stopped lanes as failed also failed: {error}; {RECONCILE_HINT}")
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def record_interrupted(queue_path: Path, lane_root: Path, lanes: Sequence[RunningLane], now: datetime) -> None:
    """Record each stopped lane's slice as ``failed`` in the JSON queue, durably, then close its record.

    The record on disk decides, not the one the caller holds: a slice whose record is already closed,
    or that the queue doesn't have ``in_progress``, keeps its status, and its record says so.
    """
    on_disk = {item.attempt_id: item for item in read_records(lane_root) if isinstance(item, AttemptRecord)}
    current = [on_disk.get(lane.record.attempt_id, lane.record) for lane in lanes]
    open_records = [record for record in current if record.is_open]
    queue_data = policy.load_queue(queue_path)
    failed: set[str] = set()
    for record in open_records:
        found = policy.find_slice(queue_data, record.slice_id)
        if found is not None and found["status"] == "in_progress":
            queue_data = policy.set_slice_status(queue_data, record.slice_id, "failed")
            failed.add(record.attempt_id)
    if failed:
        write_queue(queue_path, queue_data)
    for record in open_records:
        if record.attempt_id in failed:
            write_record(
                lane_root,
                closed(record, "failed", f"{UNKNOWN_OUTCOME}: The run was interrupted and its runner was stopped.", now),
            )
            continue
        found = policy.find_slice(queue_data, record.slice_id)
        status = NOT_IN_QUEUE if found is None else found["status"]
        reason = f"The run was interrupted and its runner was stopped; the queue records {status}."
        write_record(lane_root, closed(record, status, reason, now))
