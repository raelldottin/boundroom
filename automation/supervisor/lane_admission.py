"""Which queued slices may start a lane now (SPEC-014 RA-LANE-003).

``admit`` is the rule SPEC-014 "How many lanes run" states, as a pure function: it reads only
its arguments, with no clock, environment, Git, subprocess or file access, so every case is an
output-based test. The lane controller, ``run_lanes`` (RA-LANE-005), calls it to pick each
slice; with several lanes (RA-LANE-006) it will call it each time a lane starts or ends.

    lanes started now = min(free lane slots under the ceiling,
                            slices admissible now,
                            lanes left in this run's autonomous budget)

- **Running.** The lanes in flight are the ones the caller passes, plus any slice the queue marks
  ``in_progress`` that the caller didn't pass, so a slice left ``in_progress`` keeps its paths.
- **Eligible.** A slice is eligible when ``policy.slice_is_eligible`` says so (queued, every
  dependency ``done``) and no lane is already running it. That is the per-slice part of
  ``policy.select_next_slice`` and ``policy.next_slice_eligibility``. The rest of
  ``next_slice_eligibility`` judges a finished lane's handoff (the run limit, the recommended next
  slice); the run limit is the budget here, and a handoff's recommendation closes admission in the
  controller, not here.
- **Queue order.** Eligible slices are considered in ``policy.eligible_slices`` order (priority
  number, the earlier slice in the queue on a tie), whose first slice is the one
  ``select_next_slice`` returns, so with nothing running the first slice admitted is that one.
- **Overlap.** Two ``allowed_paths`` lists overlap when a prefix of one contains a prefix of the
  other, by the rule ``policy.path_matches_prefix`` uses. An empty list, or an entry that
  normalizes to ``.``, claims the whole repository.
- **Holding.** An eligible slice blocked by a running lane, or by a slice held before it, is held
  and reserves its paths, so no later slice may start on paths it needs. A slice that claims the
  whole repository therefore lets running lanes drain, then runs alone.
- **Exclusive.** A slice with ``"exclusive": true`` runs alone: it waits for every running lane,
  and nothing starts beside it. The schema has the field since RA-LANE-005; absent, it is read
  as false.

SPEC-014 writes the signature as ``admit(queue, running, held, limits)``. Holds are recomputed from
queue order on every call, so they are an output here, not an input the caller must keep in step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from automation.supervisor import policy

HoldReason = Literal["overlap", "held", "exclusive", "ceiling", "budget"]


@dataclass(frozen=True)
class Lane:
    """What a lane, running or about to start, claims: its slice's paths and whether it runs alone."""

    slice_id: str
    allowed_paths: tuple[str, ...]
    exclusive: bool = False

    @classmethod
    def for_slice(cls, slice_record: dict[str, Any]) -> Lane:
        return cls(
            slice_id=slice_record["slice_id"],
            allowed_paths=tuple(slice_record["allowed_paths"]),
            exclusive=bool(slice_record.get("exclusive", False)),
        )


@dataclass(frozen=True)
class Limits:
    """The ceiling on lanes running at once, and how many more lanes this run may start."""

    ceiling: int
    budget: int

    def __post_init__(self) -> None:
        if self.ceiling < 1:
            raise ValueError(f"the lane ceiling must be at least 1, not {self.ceiling}")
        if self.budget < 0:
            raise ValueError(f"the remaining autonomous budget cannot be negative, not {self.budget}")


@dataclass(frozen=True)
class Hold:
    """An eligible slice that doesn't start now, why, and the slice whose lane or hold blocks it.

    ``held_by`` is a running lane's slice for ``overlap`` and ``exclusive``, an earlier held slice
    for ``held``, and None for ``ceiling`` and ``budget``.
    """

    slice_id: str
    reason: HoldReason
    held_by: Optional[str] = None


@dataclass(frozen=True)
class Admission:
    """The slices to start now, in queue order, and every other eligible slice with its hold."""

    start: tuple[str, ...]
    held: tuple[Hold, ...]


def paths_overlap(first: Sequence[str], second: Sequence[str]) -> bool:
    # An empty list claims the whole repository. So does ".", which path_matches_prefix already
    # treats as a prefix of every path.
    if not first or not second:
        return True
    return any(
        policy.path_matches_prefix(one, other) or policy.path_matches_prefix(other, one) for one in first for other in second
    )


def eligible_slices(queue_data: dict[str, Any], running_slice_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The eligible slices no lane is running, in the order ``select_next_slice`` ranks them."""
    running = set(running_slice_ids)
    return [record for record in policy.eligible_slices(queue_data) if record["slice_id"] not in running]


def lanes_in_flight(queue_data: dict[str, Any], running: Sequence[Lane]) -> list[Lane]:
    """The lanes passed in, then every slice the queue marks ``in_progress`` that isn't among them.

    A slice left ``in_progress`` (by ``run_next``, or by a run that stopped before recording its
    verdict) still owns its paths, so it is counted as a running lane until someone resolves it.
    """
    known = {lane.slice_id for lane in running}
    return [
        *running,
        *(
            Lane.for_slice(record)
            for record in queue_data["slices"]
            if record["status"] == "in_progress" and record["slice_id"] not in known
        ),
    ]


def _blocker(candidate: Lane, lanes: Sequence[Lane], reserved: Sequence[Lane]) -> Optional[Hold]:
    for lane in lanes:
        if lane.exclusive or candidate.exclusive:
            return Hold(candidate.slice_id, "exclusive", lane.slice_id)
    for lane in lanes:
        if paths_overlap(candidate.allowed_paths, lane.allowed_paths):
            return Hold(candidate.slice_id, "overlap", lane.slice_id)
    # A slice is held only after some lane blocked it, so lanes is never empty here and an
    # exclusive candidate has already been held by the first loop.
    for slice_held in reserved:
        if slice_held.exclusive or paths_overlap(candidate.allowed_paths, slice_held.allowed_paths):
            return Hold(candidate.slice_id, "held", slice_held.slice_id)
    return None


def admit(queue_data: dict[str, Any], running: Sequence[Lane], limits: Limits) -> Admission:
    """Which eligible slices start a lane now, given the lanes in flight and the run's limits."""
    lanes = lanes_in_flight(queue_data, running)
    reserved: list[Lane] = []
    free_slots = limits.ceiling - len(lanes)
    start: list[str] = []
    held: list[Hold] = []
    for slice_record in eligible_slices(queue_data, [lane.slice_id for lane in lanes]):
        candidate = Lane.for_slice(slice_record)
        blocker = _blocker(candidate, lanes, reserved)
        if blocker is not None:
            held.append(blocker)
            reserved.append(candidate)
        elif len(start) >= limits.budget:
            held.append(Hold(candidate.slice_id, "budget"))
        elif len(start) >= free_slots:
            held.append(Hold(candidate.slice_id, "ceiling"))
        else:
            start.append(candidate.slice_id)
            lanes.append(candidate)
    return Admission(start=tuple(start), held=tuple(held))
