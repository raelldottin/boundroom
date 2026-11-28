"""Run queued slices in supervised lanes (SPEC-014 RA-LANE-005). This version runs one lane at a time.

``run_lanes`` is ``run_next`` with each slice in its own Git worktree. It runs ``run_next``'s own loop
(``run_next._run``) and passes it a workspace, ``LaneRun``, whose hooks put the claimed slice in a lane:

- **Before anything.** A run inside a supervised slice and a board are refused, the run lock is
  taken, and ``lane_recovery``'s start check refuses while a slice is ``in_progress`` or an attempt
  record is open or unreadable. A detached ``HEAD`` is refused: integration moves a branch.
- **Admission.** ``lane_admission.admit`` picks the slice. With no lane running, its first slice is
  the one ``policy.select_next_slice`` returns, so the order is ``run_next``'s.
- **Before the claim.** A kept lane worktree, or a repository worktree dirty outside the paths the
  supervisor owns, is refused. Then the attempt record is written.
- **After the claim.** ``lane_workspace`` makes the worktree at the commit ``run_next`` would start
  from, and runs the queue's ``lane_setup`` commands in it. The runner runs there, in its own process
  group, with the prompt, profile and paths of that worktree. The slice is measured, replayed and
  judged there by ``run_next``'s own code.
- **Integration.** A ``done`` verdict on a lane that left nothing uncommitted is fast-forwarded onto
  the repository's branch, which hasn't moved: the branch ends where ``run_next`` would leave it, at
  the same commits. Then the queue records ``done``, the record is closed, the handoff is copied to
  where ``run_next`` leaves it, and the worktree and its branch are removed. Any other verdict keeps
  the worktree, and the report names it.
- **Interrupts.** Ctrl-C, ``SIGTERM`` and ``SIGHUP`` stop the runner's process group and record its
  slice as failed (``lane_recovery``).
- ``--reconcile`` resolves what an interrupted run left, from the attempt records.

With a ceiling of one, the queue writes, decision reports and exit codes are ``run_next``'s, apart
from the fields that name the worktree (SPEC-014 invariant 6; ``test_run_lanes_parity.py``). Merging
onto a branch that moved, the integration validations and several lanes at once are RA-LANE-006.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import (  # noqa: E402
    kanban_projection,
    lane_admission,
    lane_recovery,
    lane_workspace,
    policy,
    run_next,
    runner_profile,
    settings,
)

REFUSED = 2  # refused before claiming, or a claimed slice failed under the supervisor, as run_next
EX_USAGE = 64  # sysexits.h, as run_next
EX_CONFIG = 78  # sysexits.h, as run_next
INTERRUPTED = 130  # 128 + SIGINT, what a shell reports for Ctrl-C
# The most lanes this version runs at once. RA-LANE-006 runs more.
SUPPORTED_LANES = 1
LANE_REPORT_KEY = "lane"


def build_parser() -> argparse.ArgumentParser:
    """``run_next``'s command line, plus the lane flags. The settings registry lists every option (SPEC-008)."""
    parser = run_next.build_parser()
    parser.description = (
        "Run queued slices in supervised lanes, each in its own Git worktree (SPEC-014). "
        "This version runs one lane at a time, as run_next does, and works with the JSON queue only."
    )
    parser.add_argument(
        "--max-lanes",
        type=int,
        help="Lower the queue's policy.max_lanes for this run; never raise it. This version runs one lane at a time.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Resolve the slices an interrupted run left in_progress, from its lane attempt records, then stop. "
        "With --dry-run, print what it would record and change nothing.",
    )
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


# ---------------------------------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ceiling:
    """The most lanes this run runs at once, and a note for stderr when that is less than was asked for."""

    lanes: int
    notice: str = ""


@dataclass(frozen=True)
class CeilingRefusal:
    message: str
    exit_code: int


def resolve_ceiling(policy_max_lanes: Optional[int], requested: Optional[int]) -> Ceiling | CeilingRefusal:
    """The queue's ``max_lanes`` (1 when absent), lowered by ``--max-lanes``, never raised, and capped at what this version runs."""
    queue_ceiling = 1 if policy_max_lanes is None else policy_max_lanes
    if requested is not None and requested < 1:
        return CeilingRefusal(f"stop: --max-lanes must be at least 1, not {requested}.", EX_USAGE)
    if requested is not None and requested > queue_ceiling:
        return CeilingRefusal(
            f"stop: --max-lanes {requested} is above the queue's max_lanes of {queue_ceiling}. "
            "A flag may lower the ceiling, never raise it; raising it is a reviewed change to the queue.",
            EX_CONFIG,
        )
    asked = queue_ceiling if requested is None else requested
    if asked > SUPPORTED_LANES:
        return Ceiling(
            SUPPORTED_LANES,
            f"note: a ceiling of {asked} lanes was asked for; this version runs one lane at a time (RA-LANE-006 runs more).",
        )
    return Ceiling(asked)


def admit_step(queue_data: dict, include_blocked: bool, ceiling: int) -> run_next.NothingEligible | run_next.Selected:
    """The slice the admission rule starts next, between lanes, or what to print when none can start.

    ``run_next``'s loop starts one slice per step, and the run limit in ``evaluate_completion``
    decides whether another step follows, so each step's budget is one lane.
    """
    admission = lane_admission.admit(queue_data, (), lane_admission.Limits(ceiling=ceiling, budget=1))
    if not admission.start:
        return run_next.nothing_eligible(queue_data, include_blocked)
    found = policy.find_slice(queue_data, admission.start[0])
    assert found is not None, admission.start[0]  # admit only starts slices the queue has
    return run_next.Selected(found)


def admission_lines(queue_data: dict, ceiling: int, budget: int) -> list[str]:
    """What a dry run says about admission: the slices it would start, those held and why, and the worst-case session time."""
    admission = lane_admission.admit(queue_data, (), lane_admission.Limits(ceiling=ceiling, budget=budget))
    lines = [f"lanes: ceiling {ceiling}; would start {', '.join(admission.start) or 'nothing'}"]
    for hold in admission.held:
        by = f" by {hold.held_by}" if hold.held_by else ""
        lines.append(f"lanes: held {hold.slice_id} ({hold.reason}{by})")
    worst = len(admission.start) * queue_data["policy"]["handoff_timeout_seconds"]
    lines.append(f"lanes: worst-case session time {worst}s")
    return lines


def dirty_refusal(dirty_paths: Sequence[str], supervisor_owned_paths: Sequence[str]) -> Optional[str]:
    """Refuse a repository worktree dirty outside supervisor-owned paths: integration moves its branch (SPEC-014)."""
    unexpected = policy.out_of_scope_paths(list(dirty_paths), [], list(supervisor_owned_paths))
    if not unexpected:
        return None
    return (
        "stop: the repository's worktree has changes outside the supervisor's own paths, and integrating a lane "
        "moves its branch. Commit or stash them first:\n- " + "\n- ".join(unexpected)
    )


def new_attempt_id(timestamp_token: str, token: str) -> str:
    """An attempt id: the run's timestamp token and a random token, safe as a path and ref component."""
    return lane_workspace.check_component(f"{timestamp_token}-{token}", "attempt id")


def interrupted_exit_code(error: KeyboardInterrupt) -> int:
    """128 plus the signal's number, as a shell reports a process a signal stopped: 130 for Ctrl-C."""
    if isinstance(error, lane_recovery.Terminated):
        return 128 + error.signum
    return INTERRUPTED


# ---------------------------------------------------------------------------------------------------
# The shell: the lane workspace run_next's loop drives
# ---------------------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LaneRun(run_next.Workspace):
    """Runs each claimed slice in a lane worktree, then integrates it (SPEC-014). ``run_next._run`` calls the hooks."""

    entry_point: settings.EntryPoint = "run_lanes"

    def __init__(
        self,
        repo_root: Path,
        environ: Mapping[str, str],
        *,
        queue_path: Path,
        lane_root: Path,
        integration_ref: str,
        ceiling: int,
    ) -> None:
        super().__init__(repo_root)
        self.environ = environ
        self.queue_path = queue_path
        self.lane_root = lane_root
        self.integration_ref = integration_ref
        self.ceiling = ceiling
        self.launch = self._launch
        self._start_slice("")

    def _start_slice(self, attempt_id: str) -> None:
        self.attempt_id = attempt_id
        self.record: Optional[lane_recovery.AttemptRecord] = None
        self.workspace: Optional[lane_workspace.LaneWorkspace] = None
        self.process: Optional[subprocess.Popen[Any]] = None
        self.lane_report: Optional[dict] = None

    # --- before the claim ----------------------------------------------------------------------

    def select(self, queue_data: dict, include_blocked: bool) -> run_next.NothingEligible | run_next.Selected:
        return admit_step(queue_data, include_blocked, self.ceiling)

    def refusal(self, slice_record: dict, dirty_paths_before_run: list[str], queue_policy: dict) -> Optional[str]:
        kept = lane_workspace.live_lanes(self.repo_root, self.environ)
        if kept:
            return (
                f"stop: a lane worktree is kept at {kept[0].path} (branch {kept[0].branch or 'none'}), and this version "
                "runs one lane at a time. Recover its work, then remove it with 'git worktree remove' and delete its branch."
            )
        return dirty_refusal(dirty_paths_before_run, queue_policy["supervisor_owned_paths"])

    def root(self, slice_record: dict, timestamp_token: str) -> Path:
        self._start_slice(new_attempt_id(timestamp_token, secrets.token_hex(4)))
        self.proof_root = self.lane_root / self.attempt_id
        return self.proof_root

    def before_claim(self, slice_record: dict, base_sha: str, run_id: str) -> None:
        record = lane_recovery.new_attempt(
            slice_id=slice_record["slice_id"],
            attempt_id=self.attempt_id,
            run_id=run_id,
            base_sha=base_sha,
            lane_root=self.lane_root,
            integration_ref=self.integration_ref,
            now=_now(),
        )
        lane_recovery.write_record(self.lane_root, record, new=True)
        self.record = record

    def unclaimed(self, slice_record: dict) -> None:
        """The claim's queue write failed and the slice is still queued: close the record written for it."""
        if self.record is not None:
            self._write(lane_recovery.closed(self.record, "queued", "The claim failed; the slice stayed queued.", _now()))
        self._finish(kept=True)

    # --- after the claim -----------------------------------------------------------------------

    def prepare(self, queue_policy: dict, handoff_path: Path, dirty_paths_before_run: list[str]) -> list[str]:
        record = self._open_record()
        self.workspace = lane_workspace.create(self.repo_root, record.slice_id, record.attempt_id, record.base_sha, self.environ)
        # A fresh worktree lacks an ignored handoff directory, and the runner writes its handoff there.
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        lane_workspace.provision(self.workspace, queue_policy.get("lane_setup") or [], self.environ)
        # The judged tree must be the shipped tree, so setup may leave only files Git ignores.
        left = policy.git_dirty_paths(self.workspace.path)
        if left:
            raise lane_workspace.LaneWorkspaceError(
                "lane_setup left files Git doesn't ignore, which the clean-end check would refuse: "
                + ", ".join(left)
                + ". Add them to .gitignore, or have lane_setup write them elsewhere"
            )
        return left

    def _launch(self, command: str, cwd: Path, env: Optional[Mapping[str, str]], timeout_seconds: float) -> int:
        """The runner leads its own process group, so an interrupt or the timeout stops all of it."""
        process = subprocess.Popen(command, cwd=cwd, shell=True, env=env, start_new_session=True)
        self.process = process
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            lane_workspace.stop_process_group(process)
            self.process = None
            raise
        # Once its leader is reaped, its group id may be reused, so an interrupt mustn't signal it. An
        # interrupt during the wait skips this: the runner is still running, and must be stopped.
        self.process = None
        return code

    def settle(self, queue_status: str, head_sha: str, handoff_relative: str) -> Optional[run_next.Failure]:
        record, workspace = self._open_record(), self._workspace()
        if queue_status == "done":
            left = lane_workspace.lane_uncommitted_changes(workspace, handoff_relative, self.environ)
            if handoff_edited_after_commit(workspace.path, handoff_relative, self.environ):
                left = sorted({*left, policy.normalize_repo_path(handoff_relative)})
            if left:
                return run_next.Failure(
                    f"Lane for slice {record.slice_id} left uncommitted changes, which integration would drop: "
                    + ", ".join(left)
                    + f". Its worktree is kept at {workspace.path}.",
                    "lane_uncommitted_changes",
                    REFUSED,
                )
        self._write(lane_recovery.judged(record, head_sha, queue_status, _now()))
        if queue_status != "done":
            return None
        return self._integrate(head_sha)

    def _integrate(self, head_sha: str) -> Optional[run_next.Failure]:
        """Fast-forward the integration branch to the judged head, if it is still checked out where the lane started."""
        record = self._write(
            lane_recovery.integration_started(self._open_record(), lane_recovery.IntegrationMethod.FAST_FORWARD, _now())
        )
        problem = self._fast_forward_problem(record, head_sha)
        if problem:
            self._write(lane_recovery.integration_finished(record, lane_recovery.IntegrationOutcome.REFUSED, _now()))
            return run_next.Failure(
                f"Supervisor error integrating slice {record.slice_id}: {problem}. Its worktree is kept at {record.worktree}.",
                "supervisor_error",
                REFUSED,
            )
        self._write(lane_recovery.integration_finished(record, lane_recovery.IntegrationOutcome.LANDED, _now()))
        return None

    def _fast_forward_problem(self, record: lane_recovery.AttemptRecord, head_sha: str) -> Optional[str]:
        if current_branch(self.repo_root, self.environ) != record.integration_ref:
            return f"the repository is no longer on {record.integration_ref}"
        head = _git(self.repo_root, self.environ, "rev-parse", "--verify", "HEAD^{commit}")
        if head.returncode != 0 or head.stdout.strip() != record.base_sha:
            # Merging onto a branch that moved is RA-LANE-006's merge integration.
            return (
                f"{record.integration_ref} moved from {record.base_sha} while the lane ran, and this version only fast-forwards"
            )
        merged = _git(self.repo_root, self.environ, "merge", "--ff-only", "--quiet", head_sha)
        if merged.returncode != 0:
            return f"git merge --ff-only {head_sha} failed: {(merged.stderr or merged.stdout).strip() or 'no output'}"
        return None

    def recorded(self, queue_status: str, reason: str, handoff_path: Path) -> None:
        """Copy the handoff, close the record, and remove a done lane's worktree. A failure here is a warning."""
        record, workspace = self.record, self.workspace
        kept = True
        try:
            if record is None or workspace is None:
                return
            self._write(lane_recovery.closed(record, queue_status, reason, _now()))
            carry_handoff(self.repo_root, workspace.path, handoff_path, self.environ, drop_untracked=queue_status == "done")
            if queue_status == "done":
                assert record.judgement is not None  # settle judged it
                lane_workspace.retain_or_remove(
                    self.repo_root, workspace, self.environ, done_and_integrated=True, recorded_head=record.judgement.head
                )
                kept = False
        except Exception as error:
            print(f"warning: after recording slice {record.slice_id if record else '?'}: {error}", file=sys.stderr)
        finally:
            self._finish(kept)

    def failed(self, failure: run_next.Failure, write_error: Optional[str], handoff_path: Path) -> None:
        """Copy the handoff and close the record as failed. The worktree is kept for a person."""
        record, workspace = self.record, self.workspace
        try:
            # When the queue refused the failure too, the slice stays in_progress and reconcile closes the record.
            if record is not None and record.is_open and write_error is None:
                self._write(lane_recovery.closed(record, "failed", f"{failure.code}: {failure.reason}", _now()))
            if workspace is not None:
                carry_handoff(self.repo_root, workspace.path, handoff_path, self.environ, drop_untracked=False)
        except Exception as error:
            print(f"warning: after failing slice {record.slice_id if record else '?'}: {error}", file=sys.stderr)
        finally:
            self._finish(kept=True)

    def report(self, report: dict) -> dict:
        """The decision report, with the lane that ran the slice and whether its worktree is kept (SPEC-014)."""
        return report if self.lane_report is None else report | {LANE_REPORT_KEY: self.lane_report}

    # --- interrupts ------------------------------------------------------------------------------

    def running(self) -> list[lane_recovery.RunningLane]:
        if self.record is None or self.process is None:
            return []
        return [lane_recovery.RunningLane(self.record, self.process)]

    def interrupted(self, lanes: Sequence[lane_recovery.RunningLane]) -> None:
        """Record the slice of the attempt in flight as failed, whether or not its runner had started.

        Once integration has started, the judged head may already be on the branch, and only
        reconcile can tell. The slice is then left ``in_progress`` with its record open.
        """
        record = self.record
        if record is None:
            print("stop: interrupted between slices; nothing was in flight.", file=sys.stderr)
            return
        if record.integration is not None:
            print(
                f"stop: interrupted while integrating slice {record.slice_id}. It stays in_progress; {lane_recovery.RECONCILE_HINT}.",
                file=sys.stderr,
            )
            return
        lane_recovery.record_interrupted_attempts(self.queue_path, self.lane_root, [record], _now())
        found = policy.find_slice(policy.load_queue(self.queue_path), record.slice_id)
        stopped = "its runner was stopped, " if lanes else ""
        print(
            f"stop: interrupted. Slice {record.slice_id}: {stopped}the queue records "
            f"{found['status'] if found else 'nothing'}, and its worktree, if any, is kept.",
            file=sys.stderr,
        )

    # --- helpers -----------------------------------------------------------------------------------

    def _open_record(self) -> lane_recovery.AttemptRecord:
        assert self.record is not None, "no attempt in flight"
        return self.record

    def _workspace(self) -> lane_workspace.LaneWorkspace:
        assert self.workspace is not None, "no lane worktree"
        return self.workspace

    def _write(self, record: lane_recovery.AttemptRecord) -> lane_recovery.AttemptRecord:
        lane_recovery.write_record(self.lane_root, record)
        self.record = record
        return record

    def _finish(self, kept: bool) -> None:
        workspace = self.workspace
        if workspace is not None:
            self.lane_report = {
                "attempt_id": workspace.attempt_id,
                "worktree": str(workspace.path),
                "branch": workspace.branch,
                "worktree_kept": kept,
            }
        self.record, self.process = None, None


def _git(cwd: Path, environ: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, env=dict(environ), capture_output=True, text=True, check=False)


def current_branch(repo_root: Path, environ: Mapping[str, str]) -> Optional[str]:
    """The full ref of the branch the repository's worktree has checked out, or None when ``HEAD`` is detached."""
    result = _git(repo_root, environ, "symbolic-ref", "-q", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def handoff_edited_after_commit(lane: Path, handoff_relative: str, environ: Mapping[str, str]) -> bool:
    """Whether the handoff is tracked in the lane and has changes its runner didn't commit."""
    status = _git(lane, environ, "status", "--porcelain", "--", handoff_relative).stdout
    return any(line and not line.startswith("??") for line in status.splitlines())


def carry_handoff(repo_root: Path, lane: Path, handoff_path: Path, environ: Mapping[str, str], *, drop_untracked: bool) -> None:
    """Copy the lane's handoff to where run_next leaves it, in the repository's worktree, unless one is there.

    Only a regular file inside the lane is copied, never a symbolic link. With ``drop_untracked``,
    an untracked handoff is then deleted from the lane, so its worktree can be removed.
    """
    try:
        relative = handoff_path.relative_to(lane)
    except ValueError:
        return  # a --handoff-dir outside the repository: run_next and the lane write the same file
    if handoff_path.is_symlink() or not handoff_path.is_file() or not handoff_path.resolve().is_relative_to(lane):
        return
    target = repo_root / relative
    if not (target.exists() or target.is_symlink()):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(handoff_path, target)
    if drop_untracked and not _git(lane, environ, "ls-files", "--", relative.as_posix()).stdout.strip():
        handoff_path.unlink()


# ---------------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------------


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    repo_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Run lanes. ``repo_root`` defaults to ``REPO_ROOT``, read when called, and ``environ`` to ``os.environ``."""
    args = parse_args(argv)
    repo_root = REPO_ROOT if repo_root is None else repo_root
    environ = os.environ if environ is None else environ
    for warning in settings.legacy_env_warnings(environ):
        print(warning, file=sys.stderr)
    try:
        runner_profile.refuse_if_nested(environ, "run_lanes")
        board = lane_recovery.board_refusal(args.kanban_board)
        if board:
            print(board.message, file=sys.stderr)
            return REFUSED
        if args.dry_run:
            return _start(args, repo_root=repo_root, environ=environ)
        with runner_profile.run_lock(environ):
            return _start(args, repo_root=repo_root, environ=environ)
    except runner_profile.ProfileRefused as error:
        print(f"stop: {error}", file=sys.stderr)
        return EX_CONFIG


def _start(args: argparse.Namespace, *, repo_root: Path, environ: Mapping[str, str]) -> int:
    if args.single_slice and args.autonomous_limit is not None:
        # run_next refuses this before anything else, and so does run_lanes.
        print("stop: --single-slice runs one slice and refuses --autonomous-limit.", file=sys.stderr)
        return EX_USAGE
    queue_path = repo_root / args.queue
    lane_root = lane_workspace.lane_root(repo_root, environ)
    if args.reconcile:
        return _reconcile(args, repo_root=repo_root, environ=environ, queue_path=queue_path, lane_root=lane_root)
    queue_data = policy.load_queue(queue_path)
    refusal = lane_recovery.check_start(queue_data, lane_root)
    if refusal:
        print(refusal.message, file=sys.stderr)
        return REFUSED
    # Read through the settings registry (SPEC-008), as run_next reads its own settings.
    sources = settings.Sources(
        entry_point="run_lanes",
        flags=vars(args),
        environ=environ,
        repository=settings.repository_values(queue_data["policy"]),
    )
    ceiling = resolve_ceiling(settings.resolve("max_lanes", sources).value, settings.resolve("lower_max_lanes", sources).value)
    if isinstance(ceiling, CeilingRefusal):
        print(ceiling.message, file=sys.stderr)
        return ceiling.exit_code
    if ceiling.notice:
        print(ceiling.notice, file=sys.stderr)
    integration_ref = current_branch(repo_root, environ)
    if integration_ref is None:
        print("stop: HEAD is detached. Lanes land on a branch: check out the branch they should land on.", file=sys.stderr)
        return REFUSED
    if args.dry_run:
        # The run's slice limit, as run_next's _supervise computes it.
        budget = (
            1
            if args.single_slice
            else run_next.autonomous_limit(
                settings.resolve("autonomous_limit", sources).value, queue_data["policy"]["consecutive_autonomous_limit"]
            )
        )
        for line in admission_lines(queue_data, ceiling.lanes, budget):
            print(line, file=sys.stderr)
    lanes = LaneRun(
        repo_root, environ, queue_path=queue_path, lane_root=lane_root, integration_ref=integration_ref, ceiling=ceiling.lanes
    )
    with lane_recovery.interrupt_handling(lanes.running, lanes.interrupted):
        return run_next._run(args, repo_root=repo_root, environ=environ, workspace=lanes)


def _reconcile(
    args: argparse.Namespace, *, repo_root: Path, environ: Mapping[str, str], queue_path: Path, lane_root: Path
) -> int:
    """Resolve every in_progress slice from the attempt records; remove a reconciled done slice's worktree."""
    if args.dry_run:
        queue_data = policy.load_queue(queue_path)
        loaded = lane_recovery.read_records(lane_root)
        records = [item for item in loaded if isinstance(item, lane_recovery.AttemptRecord)]
        unreadable = [item for item in loaded if isinstance(item, lane_recovery.UnreadableAttempt)]
        plan = lane_recovery.decide_reconcile(queue_data, lane_recovery.gather_facts(repo_root, records, environ), unreadable)
        print(json.dumps({"reconcile": lane_recovery.plan_report(plan), "applied": False}, indent=2))
        return 0
    plan = lane_recovery.reconcile(repo_root, queue_path, lane_root, environ, now=_now())
    by_id = {
        item.attempt_id: item for item in lane_recovery.read_records(lane_root) if isinstance(item, lane_recovery.AttemptRecord)
    }
    removed: list[str] = []
    kept: list[dict[str, str]] = []
    for resolution in plan.slices:
        if resolution.queue_status != "done":
            continue
        record = by_id[resolution.attempt_ids[0]]
        if not resolution.worktrees:
            continue
        assert record.judgement is not None  # reconcile marks done only a judged head
        workspace = lane_workspace.LaneWorkspace(
            slice_id=record.slice_id,
            attempt_id=record.attempt_id,
            base_sha=record.base_sha,
            path=record.worktree,
            branch=record.branch,
        )
        try:
            pattern = policy.make_handoff_filename(record.slice_id, "*")
            for handoff in sorted((record.worktree / args.handoff_dir).glob(pattern)):
                carry_handoff(repo_root, record.worktree, handoff, environ, drop_untracked=True)
            lane_workspace.retain_or_remove(
                repo_root, workspace, environ, done_and_integrated=True, recorded_head=record.judgement.head
            )
        except (OSError, lane_workspace.LaneWorkspaceError) as error:
            kept.append({"worktree": str(record.worktree), "reason": str(error)})
        else:
            removed.append(str(record.worktree))
    report = {"reconcile": lane_recovery.plan_report(plan), "applied": True, "removed_worktrees": removed, "kept_worktrees": kept}
    print(json.dumps(report, indent=2))
    return 0


def _cli_entry(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return main(argv)
    except KeyboardInterrupt as error:
        for note in getattr(error, "__notes__", []):
            print(f"stop: {note}", file=sys.stderr)
        return interrupted_exit_code(error)
    except (
        policy.ConfigError,
        kanban_projection.ProjectionError,
        lane_workspace.LaneWorkspaceError,
        lane_recovery.AttemptRecordError,
    ) as error:
        print(f"stop: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    raise SystemExit(_cli_entry())
