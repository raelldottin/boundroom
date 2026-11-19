"""Read-only candidate admission check for a runner-neutral Boundroom batch.

This is *not* a batch dispatcher or approval mechanism. It refuses proposed units
that differ from the existing queue and never starts workers, creates tasks or
modifies canonical work state. See SPEC-011 and its cross-runner addendum.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, ValidationError

from automation.schemas.models import Contract, NonEmptyStr, SliceQueue
from automation.supervisor.policy import validate_queue_integrity

Runner = Literal["claude", "codex", "hermes"]


class BatchUnit(Contract):
    """One proposed unit bound exactly to a preclassified queue slice."""

    unit_id: NonEmptyStr
    slice_id: NonEmptyStr
    depends_on: list[NonEmptyStr]
    allowed_paths: list[NonEmptyStr] = Field(min_length=1)
    required_validations: list[NonEmptyStr]
    max_files_changed: int = Field(ge=1)
    manual_proof: list[NonEmptyStr] = Field(default_factory=list)
    required_proof_level: str = ""


class BatchPlan(Contract):
    """Runner-agnostic proposal: execution/approval state is intentionally absent."""

    version: Literal[1]
    repository: NonEmptyStr
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_concurrency: int = Field(ge=1, le=32)
    units: list[BatchUnit] = Field(min_length=1)


def _safe_scope(path: str) -> bool:
    """Reject root, absolute, wildcard and traversal-style scopes even if queued."""
    if not path or path.startswith("/") or "\\" in path or "*" in path:
        return False
    parts = path.rstrip("/").split("/")
    return all(part not in ("", ".", "..") for part in parts) and str(PurePosixPath(path)) != "."


def _overlap(left: str, right: str) -> bool:
    left, right = left.rstrip("/"), right.rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def evaluate_candidate(
    proposed: Any,
    canonical_queue: Any,
    *,
    repository: str,
    observed_base_sha: str,
    queue_bytes: bytes,
    runner: str,
    max_concurrency: int,
) -> dict[str, Any]:
    """Return a deterministic, non-authorizing assessment; never launch or write.

    The caller must independently supply the observed repository and HEAD, actual
    queue bytes and an operator-approved concurrency ceiling. A valid result is
    only a candidate for *separate* human authorization and runner capability tests.
    """
    refusals: list[str] = []
    try:
        plan = BatchPlan.model_validate(proposed)
    except ValidationError:
        return {
            "admissible_candidate": False,
            "runner": runner,
            "execution_started": False,
            "unit_count": 0,
            "refusals": ["invalid_plan_contract"],
        }
    try:
        queue = SliceQueue.model_validate(canonical_queue)
    except ValidationError:
        return {
            "admissible_candidate": False,
            "runner": runner,
            "execution_started": False,
            "unit_count": len(plan.units),
            "refusals": ["invalid_queue_contract"],
        }

    if validate_queue_integrity(queue.model_dump(exclude_unset=True)):
        refusals.append("invalid_queue_integrity")
    if runner not in ("claude", "codex", "hermes"):
        refusals.append("unsupported_runner")
    if plan.repository != repository:
        refusals.append("repository_mismatch")
    if plan.base_sha != observed_base_sha:
        refusals.append("stale_base_sha")
    if plan.queue_sha256 != hashlib.sha256(queue_bytes).hexdigest():
        refusals.append("stale_queue_digest")
    if max_concurrency < 1 or plan.max_concurrency > max_concurrency:
        refusals.append("concurrency_ceiling_exceeded")

    known = {item.slice_id: item for item in queue.slices}
    proposed_ids: set[str] = set()
    proposed_units: set[str] = set()
    for unit in plan.units:
        if unit.unit_id in proposed_units:
            refusals.append("duplicate_unit_id")
        proposed_units.add(unit.unit_id)
        if unit.slice_id in proposed_ids:
            refusals.append("duplicate_slice_id")
        proposed_ids.add(unit.slice_id)

        slice_record = known.get(unit.slice_id)
        if slice_record is None:
            refusals.append("unknown_slice")
            continue
        if slice_record.status != "queued":
            refusals.append("slice_not_queued")
        if unit.allowed_paths != slice_record.allowed_paths or unit.max_files_changed != slice_record.max_files_changed:
            refusals.append("scope_mismatch")
        if unit.required_validations != slice_record.required_validations:
            refusals.append("validation_mismatch")
        if unit.depends_on != slice_record.depends_on:
            refusals.append("dependency_mismatch")
        if unit.manual_proof != slice_record.manual_proof or unit.required_proof_level != (
            slice_record.required_proof_level or ""
        ):
            refusals.append("proof_mismatch")
        if not all(_safe_scope(path) for path in unit.allowed_paths):
            refusals.append("unsafe_scope")

    for unit in plan.units:
        record = known.get(unit.slice_id)
        if record is None:
            continue
        for dependency in record.depends_on:
            other = known.get(dependency)
            if other is None or (dependency not in proposed_ids and other.status != "done"):
                refusals.append("unmet_dependency")

    # Conservative first milestone: no path-sharing between units, even when ordered
    # by dependencies. The implementation of merging overlapping work is not accepted.
    for index, left in enumerate(plan.units):
        for right in plan.units[index + 1 :]:
            if any(_overlap(a, b) for a in left.allowed_paths for b in right.allowed_paths):
                refusals.append("overlapping_scopes")

    return {
        "admissible_candidate": not refusals,
        "runner": runner,
        "execution_started": False,
        "unit_count": len(plan.units),
        "refusals": sorted(set(refusals)),
    }


def main() -> int:
    """Read-only CLI with an observed Git HEAD; no write/dispatch path exists."""
    parser = argparse.ArgumentParser(description="Check a batch candidate against the canonical queue; never execute")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--runner", choices=("claude", "codex", "hermes"), required=True)
    parser.add_argument("--max-concurrency", type=int, required=True)
    args = parser.parse_args()
    try:
        raw_queue = args.queue.read_bytes()
        raw_plan = args.plan.read_bytes()
        queue = json.loads(raw_queue)
        plan = json.loads(raw_plan)
        head = subprocess.run(
            ["git", "-C", str(args.repo_root), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        outcome = evaluate_candidate(
            plan,
            queue,
            repository=args.repository,
            observed_base_sha=head,
            queue_bytes=raw_queue,
            runner=args.runner,
            max_concurrency=args.max_concurrency,
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
        outcome = {
            "admissible_candidate": False,
            "runner": args.runner,
            "execution_started": False,
            "unit_count": 0,
            "refusals": ["unreadable_input_or_repository"],
        }
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["admissible_candidate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
