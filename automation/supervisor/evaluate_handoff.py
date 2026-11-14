"""Evaluate a pre-existing handoff artifact against a named slice.

This is a supervisor-owned CLI for closing slices whose implementation
was completed outside the normal ``run_next.py`` agent-launch flow.

Usage::

    python3 automation/supervisor/evaluate_handoff.py \\
        --slice-id write-linked-task-rendering \\
        --handoff automation/handoffs/20260831T...-write-linked-task-rendering.json \\
        --base-sha abc123 \\
        --head-sha def456 \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import policy, runner_profile, telemetry, telemetry_settings


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EvaluationReport:
    slice_id: str
    queue_status: str
    decision: str
    reason: str
    changed_files: list[str]
    changed_file_count: int
    out_of_scope_files: list[str]
    files_touched_outside_scope: list[str]
    required_validation_failures: list[str]
    supervisor_validation_replays: list[dict[str, Any]]
    missing_manual_proofs: list[str]
    missing_proof_level: bool
    head_sha: str
    base_sha: str
    dry_run: bool


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def sha_exists(repo_root: Path, sha: str) -> bool:
    """Return True if *sha* names a valid object in the repository."""
    result = subprocess.run(
        ["git", "cat-file", "-t", sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def changed_files_between(repo_root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return the list of files changed between *base_sha* and *head_sha*."""
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, head_sha],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(policy.normalize_repo_path(line) for line in result.stdout.splitlines() if line.strip())


# Every slice field that decides whether a candidate passes. The contract is
# read at base_sha and frozen: automation/queue/slices.json is supervisor-owned,
# so out_of_scope_paths() exempts it unconditionally, which would otherwise let
# a candidate widen its own allowed_paths in the very diff under evaluation.
FROZEN_CONTRACT_FIELDS = (
    "allowed_paths",
    "required_validations",
    "max_files_changed",
    "manual_proof",
    "required_proof_level",
    "depends_on",
)


def slice_at_sha(repo_root: Path, sha: str, queue_rel_path: str, slice_id: str) -> Optional[dict[str, Any]]:
    """Return the *slice_id* record as it stood in the queue at *sha*."""
    result = subprocess.run(
        ["git", "show", f"{sha}:{queue_rel_path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        queue_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return policy.find_slice(queue_data, slice_id)


def contract_drift(base_slice: dict[str, Any], head_slice: dict[str, Any]) -> list[str]:
    """Return the frozen contract fields the candidate changed at head."""
    return [field for field in FROZEN_CONTRACT_FIELDS if base_slice.get(field) != head_slice.get(field)]


# ---------------------------------------------------------------------------
# Worktree-based validation replay
# ---------------------------------------------------------------------------


def replay_validations_in_worktree(
    repo_root: Path,
    head_sha: str,
    required_validations: list[str],
    validations_passed: list[str],
    base_sha: str = "",
) -> list[policy.ValidationReplayResult]:
    """Replay supervisor-owned validations at *head_sha* using a temp worktree.

    The worktree is a clean checkout of *head_sha*, so ``git diff --check`` only sees the slice's
    changes when it is compared with *base_sha* (``policy.replay_argv``).
    """
    replayable = policy.supervisor_replayable_validations(required_validations)
    passed_set = set(validations_passed)
    commands_to_replay = [cmd for cmd in replayable if cmd in passed_set]

    if not commands_to_replay:
        return []

    worktree_dir = tempfile.mkdtemp(prefix="evaluate_handoff_worktree_")
    worktree_path = Path(worktree_dir)

    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), head_sha],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

        def worktree_runner(_repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(argv, cwd=worktree_path, check=False, capture_output=True, text=True)

        return policy.replay_validation_commands(
            repo_root=worktree_path,
            required_validations=required_validations,
            validations_passed=validations_passed,
            runner=worktree_runner,
            base_sha=base_sha,
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------


def evaluate(
    repo_root: Path,
    queue_path: Path,
    handoff_path: Path,
    slice_id: str,
    base_sha: str,
    head_sha: str,
    dry_run: bool,
) -> EvaluationReport:
    """Run the full evaluation and return a structured report."""

    # --- Load and validate inputs -------------------------------------------

    queue_data = policy.load_queue(queue_path)
    handoff = policy.load_handoff(handoff_path)

    slice_record = policy.find_slice(queue_data, slice_id)
    if slice_record is None:
        return _fail(slice_id, base_sha, head_sha, dry_run, f"Slice '{slice_id}' not found in queue.")

    if slice_record["status"] != "queued":
        return _fail(slice_id, base_sha, head_sha, dry_run, f"Slice status is '{slice_record['status']}', expected 'queued'.")

    if handoff["slice_id"] != slice_id:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Handoff slice_id '{handoff['slice_id']}' does not match --slice-id '{slice_id}'.",
        )

    # The handoff must name the commit it was verified against, and it must be
    # the commit under evaluation. An absent value used to short-circuit here,
    # which let one handoff validate against any --head-sha.
    verified_sha = handoff.get("verified_commit_sha", "")
    if not verified_sha:
        return _fail(slice_id, base_sha, head_sha, dry_run, "Handoff does not declare verified_commit_sha.")
    if verified_sha != head_sha:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Handoff verified_commit_sha '{verified_sha}' does not match --head-sha '{head_sha}'.",
        )

    if handoff["repo_clean_status"] != "clean":
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Handoff repo_clean_status is '{handoff['repo_clean_status']}', expected 'clean'.",
        )

    if handoff["dirty_paths_outside_scope"]:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Handoff declares dirty paths outside slice scope: {', '.join(handoff['dirty_paths_outside_scope'])}.",
        )

    # --- SHA verification ---------------------------------------------------

    if not sha_exists(repo_root, base_sha):
        return _fail(slice_id, base_sha, head_sha, dry_run, f"Base SHA '{base_sha}' does not exist in the repository.")

    if not sha_exists(repo_root, head_sha):
        return _fail(slice_id, base_sha, head_sha, dry_run, f"Head SHA '{head_sha}' does not exist in the repository.")

    # --- Contract freeze at base SHA -----------------------------------------
    # Enforce the contract the slice carried *before* the work, never the one
    # the candidate diff or the working tree presents now.

    queue_rel_path = policy.normalize_repo_path(str(queue_path.relative_to(repo_root)))
    base_slice = slice_at_sha(repo_root, base_sha, queue_rel_path, slice_id)
    if base_slice is None:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Slice '{slice_id}' has no contract at base SHA '{base_sha}'; there is nothing to evaluate the work against.",
        )

    head_slice = slice_at_sha(repo_root, head_sha, queue_rel_path, slice_id)
    if head_slice is None:
        return _fail(
            slice_id, base_sha, head_sha, dry_run, f"Slice '{slice_id}' is absent from the queue at head SHA '{head_sha}'."
        )

    drift = contract_drift(base_slice, head_slice)
    if drift:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Candidate changed its own enforcement contract between base and head: {', '.join(drift)}.",
        )

    slice_record = base_slice

    # --- Independent changed-file computation --------------------------------

    changed = changed_files_between(repo_root, base_sha, head_sha)

    # --- Scope enforcement ---------------------------------------------------

    supervisor_owned = queue_data["policy"]["supervisor_owned_paths"]
    out_of_scope = policy.out_of_scope_paths(changed, slice_record["allowed_paths"], supervisor_owned)

    if out_of_scope:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            "Historical diff contains files outside allowed_paths.",
            changed_files=changed,
            out_of_scope_files=out_of_scope,
        )

    # --- File budget check ---------------------------------------------------

    changed_in_scope = policy.count_paths_within_scope(changed, slice_record["allowed_paths"], supervisor_owned)
    budget = slice_record["max_files_changed"]
    file_count = max(changed_in_scope, len(set(handoff["files_touched"])))

    if file_count > budget:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            f"Changed file count ({file_count}) exceeds max_files_changed ({budget}).",
            changed_files=changed,
        )

    # --- Files-touched scope check -------------------------------------------

    files_touched = policy.normalize_paths(handoff["files_touched"])
    files_touched_outside = policy.out_of_scope_paths(files_touched, slice_record["allowed_paths"], supervisor_owned)
    if files_touched_outside:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            "Handoff files_touched contains paths outside allowed_paths.",
            changed_files=changed,
            files_touched_outside_scope=files_touched_outside,
        )

    # --- Required validation check -------------------------------------------

    validation_failures = policy.required_validation_failures(
        slice_record["required_validations"],
        handoff["validations_passed"],
        handoff["validations_failed"],
    )

    if validation_failures:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            "Required validations were missing or failed.",
            changed_files=changed,
            required_validation_failures=validation_failures,
        )

    # --- Supervisor validation replay at head SHA ----------------------------

    replays = replay_validations_in_worktree(
        repo_root=repo_root,
        head_sha=head_sha,
        required_validations=slice_record["required_validations"],
        validations_passed=handoff["validations_passed"],
        base_sha=base_sha,
    )

    replay_failures = policy.validation_replay_failures(replays)
    if replay_failures:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            "Supervisor validation replay failed at verified SHA.",
            changed_files=changed,
            required_validation_failures=replay_failures,
            replays=replays,
        )

    # --- Proof level / manual proof checks -----------------------------------

    missing_proofs, missing_level = policy.verify_manual_proofs(slice_record, handoff, head_sha)

    if missing_proofs or missing_level:
        return _fail(
            slice_id,
            base_sha,
            head_sha,
            dry_run,
            "Required manual proof or proof level was missing.",
            changed_files=changed,
            missing_manual_proofs=missing_proofs,
            missing_proof_level=missing_level,
            replays=replays,
        )

    # --- Success: apply queue transition if not dry-run ----------------------

    replay_dicts = [{"command": r.command, "success": r.success, "exit_code": r.exit_code, "reason": r.reason} for r in replays]

    if not dry_run:
        queue_data = policy.load_queue(queue_path)
        queue_data = policy.set_slice_status(queue_data, slice_id, "done")
        policy.write_json(queue_path, queue_data)

    return EvaluationReport(
        slice_id=slice_id,
        queue_status="done",
        decision="accept",
        reason="Historical handoff evaluation passed all checks.",
        changed_files=changed,
        changed_file_count=file_count,
        out_of_scope_files=[],
        files_touched_outside_scope=[],
        required_validation_failures=[],
        supervisor_validation_replays=replay_dicts,
        missing_manual_proofs=[],
        missing_proof_level=False,
        head_sha=head_sha,
        base_sha=base_sha,
        dry_run=dry_run,
    )


def _fail(
    slice_id: str,
    base_sha: str,
    head_sha: str,
    dry_run: bool,
    reason: str,
    changed_files: Optional[list[str]] = None,
    out_of_scope_files: Optional[list[str]] = None,
    files_touched_outside_scope: Optional[list[str]] = None,
    required_validation_failures: Optional[list[str]] = None,
    replays: Optional[list[policy.ValidationReplayResult]] = None,
    missing_manual_proofs: Optional[list[str]] = None,
    missing_proof_level: bool = False,
) -> EvaluationReport:
    replay_dicts = [
        {"command": r.command, "success": r.success, "exit_code": r.exit_code, "reason": r.reason} for r in (replays or [])
    ]
    return EvaluationReport(
        slice_id=slice_id,
        queue_status="failed",
        decision="reject",
        reason=reason,
        changed_files=changed_files or [],
        changed_file_count=len(changed_files or []),
        out_of_scope_files=out_of_scope_files or [],
        files_touched_outside_scope=files_touched_outside_scope or [],
        required_validation_failures=required_validation_failures or [],
        supervisor_validation_replays=replay_dicts,
        missing_manual_proofs=missing_manual_proofs or [],
        missing_proof_level=missing_proof_level,
        head_sha=head_sha,
        base_sha=base_sha,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command line, shared with the settings registry's completeness tests (SPEC-008)."""
    parser = argparse.ArgumentParser(description="Evaluate a pre-existing handoff artifact against a named slice.")
    parser.add_argument("--slice-id", required=True, help="The slice to evaluate.")
    parser.add_argument("--handoff", required=True, help="Path to the handoff JSON artifact.")
    parser.add_argument("--base-sha", required=True, help="Git SHA before the implementation.")
    parser.add_argument("--head-sha", required=True, help="Git SHA of the verified implementation.")
    parser.add_argument("--queue", default="automation/queue/slices.json", help="Path to the queue JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Emit the decision without mutating the queue.")
    telemetry_settings.add_flags(parser)
    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


def report_to_dict(report: EvaluationReport) -> dict[str, Any]:
    return {
        "slice_id": report.slice_id,
        "queue_status": report.queue_status,
        "decision": report.decision,
        "reason": report.reason,
        "changed_files": report.changed_files,
        "changed_file_count": report.changed_file_count,
        "out_of_scope_files": report.out_of_scope_files,
        "files_touched_outside_scope": report.files_touched_outside_scope,
        "required_validation_failures": report.required_validation_failures,
        "supervisor_validation_replays": report.supervisor_validation_replays,
        "missing_manual_proofs": report.missing_manual_proofs,
        "missing_proof_level": report.missing_proof_level,
        "head_sha": report.head_sha,
        "base_sha": report.base_sha,
        "dry_run": report.dry_run,
    }


def main() -> int:
    args = parse_args()
    nested = runner_profile.nested_run(os.environ)
    if nested and not args.dry_run:
        print(f"stop: evaluate_handoff without --dry-run refused inside a supervised slice ({nested} is set)", file=sys.stderr)
        return 78
    repo_root = REPO_ROOT
    queue_path = repo_root / args.queue
    repository_telemetry = policy.load_queue(queue_path)["policy"].get("telemetry") if queue_path.exists() else None
    resolved = telemetry_settings.resolve(flags=vars(args), environ=os.environ, repository=repository_telemetry)
    for message in resolved.messages:
        print(message, file=sys.stderr)
    if resolved.refusal:
        print(f"stop: {resolved.refusal}", file=sys.stderr)
        return 78  # sysexits.h EX_CONFIG, as run_next.py uses

    try:
        tracer = telemetry.configure(
            resolved, repo_root=repo_root, run_id=telemetry.new_run_id(), service_version=telemetry.version(repo_root)
        )
    except telemetry.TelemetryRefused as error:
        print(f"stop: {error}", file=sys.stderr)
        return 78
    for message in tracer.messages:
        print(message, file=sys.stderr)
    telemetry_block = resolved.report()
    try:
        with tracer.span("boundroom.run", entry="evaluate_handoff", run_id=tracer.run_id, slice_id=args.slice_id) as root:
            telemetry_block["trace_id"] = telemetry.trace_id(root)
            report = evaluate(
                repo_root=repo_root,
                queue_path=repo_root / args.queue,
                handoff_path=Path(args.handoff),
                slice_id=args.slice_id,
                base_sha=args.base_sha,
                head_sha=args.head_sha,
                dry_run=args.dry_run,
            )
            for replay in report.supervisor_validation_replays:
                tracer.mark("boundroom.validation.replay", command=replay.get("command"), success=replay.get("success"))
            result = report_to_dict(report) | {"telemetry": telemetry_block}
            tracer.mark(
                "boundroom.decision",
                error_code=telemetry.verdict_error(report.decision),
                verdict=report.decision,
                reason=report.reason,
                changed_file_count=report.changed_file_count,
                report_sha256=hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest(),
            )
            code = 0 if report.queue_status == "done" else 1
            tracer.annotate(root, exit_code=code)
            if code != 0:
                tracer.fail(root, "exit_nonzero")
    finally:
        tracer.flush()

    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
