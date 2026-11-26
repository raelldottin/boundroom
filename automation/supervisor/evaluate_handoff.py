"""Evaluate a pre-existing handoff artifact against a named slice.

This is a supervisor-owned CLI for closing slices whose implementation
was completed outside the normal ``run_next.py`` agent-launch flow.

The decision is a pure function, ``decide(facts)``, that runs the gates in a fixed order and stops
at the first that fails. ``evaluate`` is the shell around it: it gathers each fact from git, the
proof files or a validation replay only when the gates reach it (``next_stage``), then writes the
queue on an accept that isn't a dry run.

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
import time
from dataclasses import dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Final, Optional, assert_never, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import policy, runner_profile, telemetry, telemetry_metrics, telemetry_settings


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
# The decision: a pure function of the facts the shell gathered
# ---------------------------------------------------------------------------


class Stage(StrEnum):
    """A fact the gates read from git, the file system or a validation run, in the order they need it.

    The gates stop at the first that fails, and the shell gathers a fact only once every gate
    before it has passed, so a rejected candidate costs no more reads than it did before the
    gates were pure.
    """

    BASE_EXISTS = "base_exists"
    HEAD_EXISTS = "head_exists"
    HANDOFF_COMMIT_PARENT = "handoff_commit_parent"
    BASE_SLICE = "base_slice"
    HEAD_SLICE = "head_slice"
    CHANGED_FILES = "changed_files"
    REPLAYS = "replays"
    PROOFS = "proofs"


class _Unknown(Enum):
    """The value of a fact not gathered yet. None can't serve: a slice absent at base is None."""

    UNKNOWN = "UNKNOWN"


UNKNOWN: Final = _Unknown.UNKNOWN


@dataclass(frozen=True)
class Facts:
    """Everything ``decide`` judges: the request, the queue and handoff as loaded, the handoff's
    path relative to the repository (None outside it), and the facts named by ``Stage``, each
    ``UNKNOWN`` until the shell gathers it."""

    slice_id: str
    base_sha: str
    head_sha: str
    dry_run: bool
    queue: dict[str, Any]
    handoff: dict[str, Any]
    handoff_relative: Optional[str]
    base_exists: bool | _Unknown = UNKNOWN
    head_exists: bool | _Unknown = UNKNOWN
    # head's parent when head is a commit that changed only the handoff, else None
    handoff_commit_parent: Optional[str] | _Unknown = UNKNOWN
    base_slice: Optional[dict[str, Any]] | _Unknown = UNKNOWN  # the slice's record in the queue at base_sha
    head_slice: Optional[dict[str, Any]] | _Unknown = UNKNOWN
    changed_files: list[str] | _Unknown = UNKNOWN  # git's diff from base_sha to head_sha
    replays: list[policy.ValidationReplayResult] | _Unknown = UNKNOWN
    proofs: tuple[list[str], bool] | _Unknown = UNKNOWN  # policy.verify_manual_proofs: missing proofs, level short


class IncompleteFacts(RuntimeError):
    """``decide`` reached a gate whose fact is still ``UNKNOWN``: a bug in the caller, not bad input."""

    def __init__(self, stage: Stage) -> None:
        super().__init__(f"decide needs the {stage.value} fact")
        self.stage = stage


def next_stage(facts: Facts) -> Optional[Stage]:
    """The fact the gates need next, or None once *facts* are enough to decide."""
    outcome = _judge(facts)
    return outcome if isinstance(outcome, Stage) else None


def decide(facts: Facts) -> EvaluationReport:
    """The report on *facts*: a reject from the first gate that fails, or an accept.

    Raises ``IncompleteFacts`` when a gate needs a fact that is still ``UNKNOWN`` (``next_stage``).
    """
    outcome = _judge(facts)
    if isinstance(outcome, Stage):
        raise IncompleteFacts(outcome)
    return outcome


def _judge(f: Facts) -> EvaluationReport | Stage:
    """The gates, in order: the first failure's report, the accept, or the fact needed next."""

    # --- The queue and the handoff, as loaded ----------------------------------
    handoff = f.handoff
    slice_record = policy.find_slice(f.queue, f.slice_id)
    if slice_record is None:
        return _fail(f, f"Slice '{f.slice_id}' not found in queue.")

    if slice_record["status"] != "queued":
        return _fail(f, f"Slice status is '{slice_record['status']}', expected 'queued'.")

    if handoff["slice_id"] != f.slice_id:
        return _fail(f, f"Handoff slice_id '{handoff['slice_id']}' does not match --slice-id '{f.slice_id}'.")

    # The handoff must name the commit it verified; which commits it may name is checked
    # below, once the SHAs are known to exist.
    verified_sha = handoff.get("verified_commit_sha", "")
    if not verified_sha:
        return _fail(f, "Handoff does not declare verified_commit_sha.")

    if handoff["repo_clean_status"] != "clean":
        return _fail(f, f"Handoff repo_clean_status is '{handoff['repo_clean_status']}', expected 'clean'.")

    if handoff["dirty_paths_outside_scope"]:
        return _fail(f, f"Handoff declares dirty paths outside slice scope: {', '.join(handoff['dirty_paths_outside_scope'])}.")

    # --- SHA verification ---------------------------------------------------
    if f.base_exists is UNKNOWN:
        return Stage.BASE_EXISTS
    if not f.base_exists:
        return _fail(f, f"Base SHA '{f.base_sha}' does not exist in the repository.")

    if f.head_exists is UNKNOWN:
        return Stage.HEAD_EXISTS
    if not f.head_exists:
        return _fail(f, f"Head SHA '{f.head_sha}' does not exist in the repository.")

    if f.handoff_commit_parent is UNKNOWN:
        return Stage.HANDOFF_COMMIT_PARENT
    if verified_sha not in policy.verified_commits(f.head_sha, f.handoff_commit_parent):
        return _fail(
            f,
            f"Handoff verified_commit_sha '{verified_sha}' does not match --head-sha '{f.head_sha}', "
            "or the commit before it when --head-sha changes only the handoff.",
        )

    # --- Contract freeze at base SHA -----------------------------------------
    # Enforce the contract the slice carried *before* the work, never the one
    # the candidate diff or the working tree presents now.
    if f.base_slice is UNKNOWN:
        return Stage.BASE_SLICE
    if f.base_slice is None:
        return _fail(
            f, f"Slice '{f.slice_id}' has no contract at base SHA '{f.base_sha}'; there is nothing to evaluate the work against."
        )

    if f.head_slice is UNKNOWN:
        return Stage.HEAD_SLICE
    if f.head_slice is None:
        return _fail(f, f"Slice '{f.slice_id}' is absent from the queue at head SHA '{f.head_sha}'.")

    drift = contract_drift(f.base_slice, f.head_slice)
    if drift:
        return _fail(f, f"Candidate changed its own enforcement contract between base and head: {', '.join(drift)}.")

    # --- Scope enforcement, on the diff the supervisor computed ------------------
    if f.changed_files is UNKNOWN:
        return Stage.CHANGED_FILES
    changed = f.changed_files
    contract = f.base_slice
    allowed = contract["allowed_paths"]
    supervisor_owned = f.queue["policy"]["supervisor_owned_paths"]
    out_of_scope = sorted(
        set(policy.out_of_scope_paths(changed, allowed, supervisor_owned, f.handoff_relative))
        | set(policy.stray_owned_paths(changed, allowed, supervisor_owned, f.handoff_relative))
    )
    if out_of_scope:
        return _fail(f, "Historical diff contains files outside allowed_paths.", changed, out_of_scope_files=out_of_scope)

    # --- File budget check ---------------------------------------------------
    # The larger of the in-scope diff and the files the handoff names, so understating either
    # doesn't pass the budget.
    budget = contract["max_files_changed"]
    file_count = max(policy.count_paths_within_scope(changed, allowed, supervisor_owned), len(set(handoff["files_touched"])))
    if file_count > budget:
        return _fail(f, f"Changed file count ({file_count}) exceeds max_files_changed ({budget}).", changed)

    # --- Files-touched scope check -------------------------------------------
    files_touched_outside = policy.out_of_scope_paths(
        policy.normalize_paths(handoff["files_touched"]), allowed, supervisor_owned, f.handoff_relative
    )
    if files_touched_outside:
        return _fail(
            f,
            "Handoff files_touched contains paths outside allowed_paths.",
            changed,
            files_touched_outside_scope=files_touched_outside,
        )

    # --- Required validation check -------------------------------------------
    validation_failures = policy.required_validation_failures(
        contract["required_validations"], handoff["validations_passed"], handoff["validations_failed"]
    )
    if validation_failures:
        return _fail(f, "Required validations were missing or failed.", changed, required_validation_failures=validation_failures)

    # --- Supervisor validation replay at head SHA ----------------------------
    if f.replays is UNKNOWN:
        return Stage.REPLAYS
    replay_failures = policy.validation_replay_failures(f.replays)
    if replay_failures:
        return _fail(
            f,
            "Supervisor validation replay failed at verified SHA.",
            changed,
            required_validation_failures=replay_failures,
            replays=f.replays,
        )

    # --- Proof level / manual proof checks -----------------------------------
    if f.proofs is UNKNOWN:
        return Stage.PROOFS
    missing_proofs, missing_level = f.proofs
    if missing_proofs or missing_level:
        return _fail(
            f,
            "Required manual proof or proof level was missing.",
            changed,
            missing_manual_proofs=missing_proofs,
            missing_proof_level=missing_level,
            replays=f.replays,
        )

    return EvaluationReport(
        slice_id=f.slice_id,
        queue_status="done",
        decision="accept",
        reason="Historical handoff evaluation passed all checks.",
        changed_files=changed,
        changed_file_count=file_count,
        out_of_scope_files=[],
        files_touched_outside_scope=[],
        required_validation_failures=[],
        supervisor_validation_replays=_replay_dicts(f.replays),
        missing_manual_proofs=[],
        missing_proof_level=False,
        head_sha=f.head_sha,
        base_sha=f.base_sha,
        dry_run=f.dry_run,
    )


def _fail(
    f: Facts,
    reason: str,
    changed_files: Optional[list[str]] = None,
    *,
    out_of_scope_files: Optional[list[str]] = None,
    files_touched_outside_scope: Optional[list[str]] = None,
    required_validation_failures: Optional[list[str]] = None,
    replays: Optional[list[policy.ValidationReplayResult]] = None,
    missing_manual_proofs: Optional[list[str]] = None,
    missing_proof_level: bool = False,
) -> EvaluationReport:
    """A reject. Its ``changed_file_count`` counts the diff, not the budget's count."""
    return EvaluationReport(
        slice_id=f.slice_id,
        queue_status="failed",
        decision="reject",
        reason=reason,
        changed_files=changed_files or [],
        changed_file_count=len(changed_files or []),
        out_of_scope_files=out_of_scope_files or [],
        files_touched_outside_scope=files_touched_outside_scope or [],
        required_validation_failures=required_validation_failures or [],
        supervisor_validation_replays=_replay_dicts(replays or []),
        missing_manual_proofs=missing_manual_proofs or [],
        missing_proof_level=missing_proof_level,
        head_sha=f.head_sha,
        base_sha=f.base_sha,
        dry_run=f.dry_run,
    )


def _replay_dicts(replays: list[policy.ValidationReplayResult]) -> list[dict[str, Any]]:
    return [{"command": r.command, "success": r.success, "exit_code": r.exit_code, "reason": r.reason} for r in replays]


# ---------------------------------------------------------------------------
# The shell: gather the facts decide() asks for, then apply its decision
# ---------------------------------------------------------------------------


def _gather(stage: Stage, facts: Facts, repo_root: Path, queue_path: Path) -> Facts:
    """*facts* with the fact *stage* names read from the repository. Reads only; decides nothing.

    Called only with the stage ``next_stage`` names, so the gates before it have passed.
    """
    match stage:
        case Stage.BASE_EXISTS:
            return replace(facts, base_exists=sha_exists(repo_root, facts.base_sha))
        case Stage.HEAD_EXISTS:
            return replace(facts, head_exists=sha_exists(repo_root, facts.head_sha))
        case Stage.HANDOFF_COMMIT_PARENT:
            parent = policy.git_handoff_commit_parent(repo_root, facts.base_sha, facts.handoff_relative, facts.head_sha)
            return replace(facts, handoff_commit_parent=parent)
        case Stage.BASE_SLICE:
            base_slice = slice_at_sha(repo_root, facts.base_sha, _queue_in_history(queue_path, repo_root), facts.slice_id)
            return replace(facts, base_slice=base_slice)
        case Stage.HEAD_SLICE:
            head_slice = slice_at_sha(repo_root, facts.head_sha, _queue_in_history(queue_path, repo_root), facts.slice_id)
            return replace(facts, head_slice=head_slice)
        case Stage.CHANGED_FILES:
            return replace(facts, changed_files=changed_files_between(repo_root, facts.base_sha, facts.head_sha))
        case Stage.REPLAYS:
            replays = replay_validations_in_worktree(
                repo_root=repo_root,
                head_sha=facts.head_sha,
                required_validations=cast(dict[str, Any], facts.base_slice)["required_validations"],
                validations_passed=facts.handoff["validations_passed"],
                base_sha=facts.base_sha,
            )
            return replace(facts, replays=replays)
        case Stage.PROOFS:
            contract = cast(dict[str, Any], facts.base_slice)
            parent = cast(Optional[str], facts.handoff_commit_parent)
            return replace(facts, proofs=policy.verify_manual_proofs(contract, facts.handoff, facts.head_sha, parent))
        case _:
            assert_never(stage)


def _queue_in_history(queue_path: Path, repo_root: Path) -> str:
    """The queue's path in the repository's history. Raises ValueError for a queue outside the
    repository, whose history holds no queue to read."""
    return policy.normalize_repo_path(str(queue_path.relative_to(repo_root)))


def _repo_relative(path: Path, repo_root: Path) -> Optional[str]:
    """*path* relative to the repository, or None outside it."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def evaluate(
    repo_root: Path,
    queue_path: Path,
    handoff_path: Path,
    slice_id: str,
    base_sha: str,
    head_sha: str,
    dry_run: bool,
) -> EvaluationReport:
    """Run the full evaluation and return a structured report; an accept without *dry_run* marks the slice done."""
    queue_data = policy.load_queue(queue_path)
    handoff = policy.load_handoff(handoff_path)
    facts = Facts(
        slice_id=slice_id,
        base_sha=base_sha,
        head_sha=head_sha,
        dry_run=dry_run,
        queue=queue_data,
        handoff=handoff,
        handoff_relative=_repo_relative(handoff_path, repo_root),
    )
    # Each pass reruns the gates from the start on what is gathered so far: pure checks over the
    # queue, the handoff and the diff, cheap next to the git call or replay a pass may add.
    while (stage := next_stage(facts)) is not None:
        facts = _gather(stage, facts, repo_root, queue_path)
    report = decide(facts)

    if report.decision == "accept" and not dry_run:
        queue_data = policy.set_slice_status(policy.load_queue(queue_path), slice_id, "done")
        policy.write_json(queue_path, queue_data)
    return report


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
    started = time.monotonic()
    code: Optional[int] = None
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
                tracer.metric("boundroom.validation.replays", entry="evaluate_handoff", success=replay.get("success"))
            result = report_to_dict(report) | {"telemetry": telemetry_block}
            tracer.mark(
                "boundroom.decision",
                error_code=telemetry.verdict_error(report.decision),
                verdict=report.decision,
                reason=report.reason,
                changed_file_count=report.changed_file_count,
                report_sha256=hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest(),
            )
            tracer.metric("boundroom.slice.decisions", decision=report.decision, queue_status=report.queue_status)
            code = 0 if report.queue_status == "done" else 1
            tracer.annotate(root, exit_code=code)
            if code != 0:
                tracer.fail(root, "exit_nonzero")
    finally:
        outcome = "error" if code is None else telemetry_metrics.run_outcome(code)
        tracer.record_run("evaluate_handoff", outcome, time.monotonic() - started)
        tracer.flush()

    print(json.dumps(result, indent=2))
    if code is None:  # unreachable: the try assigned code or raised
        raise RuntimeError("no exit code")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
