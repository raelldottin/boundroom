"""``run_lanes`` against real Git, a real lane root and a scripted agent (SPEC-014 RA-LANE-005).

What a lane adds to ``run_next``: the attempt record written before the runner starts, the worktree
the runner runs in, provisioning, the clean-end check, the fast-forward, retention, the start
refusals, interrupts and ``--reconcile``. ``test_run_lanes_parity.py`` covers what stays the same.
The agent is a script; nothing else is a double.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from automation.supervisor import lane_recovery, lane_workspace, policy, run_next
from automation.tests.run_lanes_fixtures import (
    Repo,
    git,
    isolate_environment,
    lane_root,
    make_repo,
    queue_document,
    run_lanes_over,
    run_next_over,
    slice_record,
)

FIRST, SECOND = "first-slice", "second-slice"


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "state"
    isolate_environment(monkeypatch, path)
    return path.resolve()


def repo_with(tmp_path: Path, behaviours: dict | None = None, *slices: dict, **policy_fields) -> Repo:
    records = list(slices) or [slice_record(FIRST, 10), slice_record(SECOND, 20)]
    repo = make_repo(tmp_path / "case", queue_document(records, **policy_fields))
    repo.behave(behaviours or {})
    return repo


def records(state: Path) -> list[lane_recovery.AttemptRecord]:
    loaded = lane_recovery.read_records(lane_root(state))
    assert all(isinstance(item, lane_recovery.AttemptRecord) for item in loaded), loaded
    return [item for item in loaded if isinstance(item, lane_recovery.AttemptRecord)]


def lane_branches(repo: Repo) -> list[str]:
    return git(repo.root, "branch", "--list", "boundroom/lane/*", "--format=%(refname:short)").split()


# --- a done lane -------------------------------------------------------------------------------------


def test_a_done_lane_runs_in_its_worktree_lands_on_the_branch_and_is_removed(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    base = repo.head()
    outcome = run_lanes_over(repo, ["--single-slice"], monkeypatch)

    assert outcome.exit_code == 0, outcome.stderr
    # The record existed, open and unjudged, before the runner started, naming the commit it starts from.
    seen = json.loads((repo.snapshots / f"{FIRST}.record").read_text(encoding="utf-8"))
    assert (seen["slice_id"], seen["base_sha"], seen["judgement"], seen["closure"]) == (FIRST, base, None, None)
    # The runner ran in the lane's worktree, not the repository's.
    cwd = Path((repo.snapshots / f"{FIRST}.cwd").read_text(encoding="utf-8"))
    assert cwd.parent == lane_root(state) and cwd.name == seen["attempt_id"]
    # The judged head was fast-forwarded onto main, and the record says so, closed done.
    (record,) = records(state)
    assert record.judgement is not None and record.judgement.head == repo.head() != base
    assert git(repo.root, "rev-parse", "HEAD^") == base  # the lane's own commit, not a merge
    assert record.integration == lane_recovery.Integration(
        lane_recovery.IntegrationMethod.FAST_FORWARD,
        record.integration.started_at if record.integration else "",
        lane_recovery.IntegrationOutcome.LANDED,
        record.integration.finished_at if record.integration else None,
    )
    assert record.closure is not None and record.closure.queue_status == "done"
    assert repo.statuses() == {FIRST: "done", SECOND: "queued"}
    # The worktree and its branch are gone; the report names the lane and says so.
    assert not cwd.exists() and lane_branches(repo) == []
    (report,) = outcome.reports
    assert report["lane"] == {
        "attempt_id": record.attempt_id,
        "worktree": str(cwd),
        "branch": f"boundroom/lane/{FIRST}/{record.attempt_id}",
        "worktree_kept": False,
    }
    # The handoff is where run_next leaves it, in the repository's handoff directory.
    (handoff,) = (repo.root / "automation/handoffs").iterdir()
    assert handoff.name.endswith(f"-{FIRST}.json") and json.loads(handoff.read_text(encoding="utf-8"))["slice_id"] == FIRST


def test_a_handoff_left_untracked_and_not_ignored_still_lets_the_worktree_go(tmp_path, state, monkeypatch):
    repo = make_repo(tmp_path / "case", queue_document([slice_record(FIRST, 10)]), ignore_handoffs=False)
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.reports[0]["lane"]["worktree_kept"] is False
    # The repository's worktree ends as run_next leaves it: the queue written, the handoff beside it.
    status = git(repo.root, "status", "--porcelain").splitlines()
    assert sorted(line.split()[-1] for line in status) == ["automation/handoffs/", "automation/queue/slices.json"]


def test_lane_setup_runs_in_the_worktree_before_the_runner(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, None, slice_record(FIRST, 10), lane_setup=["touch setup-ran.marker"])
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 0, outcome.stderr
    assert (repo.snapshots / f"{FIRST}.setup").read_text(encoding="utf-8") == "yes"
    assert not (repo.root / "setup-ran.marker").exists()  # it ran in the lane, not the repository


# --- lanes that don't land ---------------------------------------------------------------------------


def test_a_failed_verdict_keeps_the_worktree_and_leaves_the_branch_alone(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, {FIRST: "failed"})
    base = repo.head()
    outcome = run_lanes_over(repo, [], monkeypatch)

    assert outcome.exit_code == 0
    assert repo.statuses() == {FIRST: "failed", SECOND: "queued"}
    assert repo.head() == base  # not integrated
    (record,) = records(state)
    assert record.judgement is not None and record.judgement.queue_status == "failed"
    assert record.integration is None
    assert record.closure is not None and record.closure.queue_status == "failed"
    (report,) = outcome.reports
    assert report["lane"]["worktree_kept"] is True and Path(report["lane"]["worktree"]).is_dir()
    assert git(Path(report["lane"]["worktree"]), "rev-parse", "HEAD") == report["head_sha"]
    # The kept worktree blocks the next lane, before anything is claimed.
    again = run_lanes_over(repo, [], monkeypatch)
    assert again.exit_code == 2 and again.reports == []
    assert f"stop: a lane worktree is kept at {report['lane']['worktree']}" in again.stderr
    assert repo.statuses() == {FIRST: "failed", SECOND: "queued"}


def test_a_done_lane_that_left_changes_uncommitted_fails_where_run_next_records_done(tmp_path, state, monkeypatch):
    """The one verdict SPEC-014 changes: integration takes commits, so the judged tree must be the shipped tree."""
    repo = repo_with(tmp_path, {FIRST: "uncommitted"})
    base = repo.head()
    outcome = run_lanes_over(repo, [], monkeypatch)

    assert outcome.exit_code == 2
    (report,) = outcome.reports
    assert report["decision"] == "stop_failed" and report["queue_status"] == "failed"
    assert report["reason"].startswith(
        f"Lane for slice {FIRST} left uncommitted changes, which integration would drop: docs/{FIRST}.md."
    )
    assert repo.statuses() == {FIRST: "failed", SECOND: "queued"} and repo.head() == base
    (record,) = records(state)
    assert record.closure is not None and record.closure.reason.startswith("lane_uncommitted_changes: ")

    other = make_repo(tmp_path / "next", queue_document([slice_record(FIRST, 10), slice_record(SECOND, 20)]))
    other.behave({FIRST: "uncommitted"})
    assert run_next_over(other, ["--single-slice"], monkeypatch).reports[0]["queue_status"] == "done"


def test_a_branch_that_moved_while_the_lane_ran_is_not_merged(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, {FIRST: "move-main"})
    outcome = run_lanes_over(repo, [], monkeypatch)

    assert outcome.exit_code == 2
    (report,) = outcome.reports
    assert "while the lane ran, and this version only fast-forwards" in report["reason"]
    assert report["reason"].startswith(f"Supervisor error integrating slice {FIRST}: refs/heads/main moved from ")
    assert git(repo.root, "log", "-1", "--format=%s") == "moved"
    (record,) = records(state)
    assert record.integration is not None and record.integration.outcome is lane_recovery.IntegrationOutcome.REFUSED
    assert record.closure is not None and record.closure.queue_status == "failed"
    assert repo.statuses()[FIRST] == "failed"
    assert report["lane"]["worktree_kept"] is True


def test_a_failing_lane_setup_fails_the_slice_before_the_runner_starts(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, None, slice_record(FIRST, 10), lane_setup=["echo preparing; exit 7"])
    outcome = run_lanes_over(repo, [], monkeypatch)

    assert outcome.exit_code == 2
    (report,) = outcome.reports
    assert "lane setup command 'echo preparing; exit 7' exited with code 7" in report["reason"]
    assert not repo.snapshots.exists()  # the agent never ran
    assert repo.statuses() == {FIRST: "failed"}
    (record,) = records(state)
    assert record.closure is not None and record.closure.reason.startswith("supervisor_error: ")


def test_a_runner_past_the_handoff_timeout_is_stopped_with_its_group(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, {FIRST: "sleep"}, slice_record(FIRST, 10), handoff_timeout_seconds=1)
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2
    assert "timed out after 1 second" in outcome.reports[0]["reason"]
    assert repo.statuses() == {FIRST: "failed"}


# --- refusals before anything is claimed -------------------------------------------------------------


def nothing_claimed(repo: Repo, outcome, state: Path) -> None:
    assert outcome.reports == []
    assert set(repo.statuses().values()) == {"queued"}
    assert not (state / "boundroom" / "lanes").exists() or not list(lane_root(state).glob(".attempts/*.json"))


@pytest.mark.parametrize("flag", ["--board", "--kanban-board"])
def test_a_board_is_refused(tmp_path, state, monkeypatch, flag):
    repo = repo_with(tmp_path)
    outcome = run_lanes_over(repo, [flag, str(tmp_path / "board")], monkeypatch)
    assert outcome.exit_code == 2
    assert "lanes work with the JSON queue only" in outcome.stderr
    nothing_claimed(repo, outcome, state)


def test_an_open_attempt_record_refuses_a_start_until_reconciled(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    root = lane_root_for(repo)
    stale = lane_recovery.new_attempt(
        slice_id=SECOND,
        attempt_id="20261126T000000Z-00000000",
        run_id="earlier",
        base_sha=repo.head(),
        lane_root=root,
        integration_ref="refs/heads/main",
        now=datetime(2026, 11, 26, tzinfo=timezone.utc),
    )
    lane_recovery.write_record(root, stale, new=True)
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2
    assert "was never closed; run 'run_lanes --reconcile' to resolve it" in outcome.stderr
    assert outcome.reports == [] and set(repo.statuses().values()) == {"queued"}


def lane_root_for(repo: Repo) -> Path:
    return lane_workspace.lane_root(repo.root, os.environ)


@pytest.mark.parametrize(("flags", "code"), [(["--max-lanes", "2"], 78), (["--max-lanes", "0"], 64)])
def test_a_ceiling_flag_that_widens_or_is_below_one_is_refused(tmp_path, state, monkeypatch, flags, code):
    repo = repo_with(tmp_path)
    outcome = run_lanes_over(repo, flags, monkeypatch)
    assert outcome.exit_code == code
    nothing_claimed(repo, outcome, state)


def test_a_queue_that_allows_more_lanes_still_runs_one_at_a_time(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, None, slice_record(FIRST, 10), slice_record(SECOND, 20, allowed_paths=["src/"]), max_lanes=3)
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 0, outcome.stderr
    assert "a ceiling of 3 lanes was asked for; this version runs one lane at a time" in outcome.stderr
    assert [report["slice_id"] for report in outcome.reports] == [FIRST, SECOND]


def test_a_detached_head_is_refused(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    git(repo.root, "checkout", "-q", "--detach")
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2 and "HEAD is detached" in outcome.stderr
    nothing_claimed(repo, outcome, state)


def test_a_change_inside_the_slices_scope_is_refused_because_integration_moves_the_branch(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    (repo.root / "docs/README.md").write_text("edited\n", encoding="utf-8")
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2
    assert outcome.stderr.rstrip().endswith("Commit or stash them first:\n- docs/README.md")
    nothing_claimed(repo, outcome, state)


def test_a_dry_run_says_what_it_would_admit_and_claims_nothing(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, None, slice_record(FIRST, 10), slice_record(SECOND, 20, allowed_paths=["src/"]))
    outcome = run_lanes_over(repo, ["--dry-run"], monkeypatch)
    assert outcome.exit_code == 0
    assert "lanes: ceiling 1; would start first-slice\nlanes: held second-slice (ceiling)\n" in outcome.stderr
    (report,) = outcome.reports
    assert Path(report["handoff_path"]).is_relative_to(state)  # the lane it would run in
    assert set(repo.statuses().values()) == {"queued"}
    assert not (state / "boundroom" / "lanes").exists() or not list(lane_root(state).iterdir())


# --- interrupts and reconcile ------------------------------------------------------------------------


def test_a_stop_signal_stops_the_runner_and_records_its_slice_as_failed(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, {FIRST: f"signal:{os.getpid()}"})
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 143
    assert f"stop: interrupted. Slice {FIRST}: its runner was stopped, the queue records failed" in outcome.stderr
    assert repo.statuses() == {FIRST: "failed", SECOND: "queued"}
    (record,) = records(state)
    assert record.closure is not None
    assert record.closure.reason == "supervisor_error: The run was interrupted and its runner was stopped."
    assert record.worktree.is_dir()  # kept for a person, as every lane that didn't land
    assert lane_recovery.check_start(json.loads(repo.queue.read_text(encoding="utf-8")), lane_root(state)) is None


class Crash(BaseException):
    """A crash nothing in the supervisor catches, as a power cut or kill -9 would leave it."""


def crash_when(monkeypatch, status: str) -> None:
    original = run_next.JsonQueue.set_status

    def set_status(self, slice_record, new_status, reason="", handoff_path=None):
        if new_status == status:
            raise Crash(new_status)
        return original(self, slice_record, new_status, reason, handoff_path)

    monkeypatch.setattr(run_next.JsonQueue, "set_status", set_status)


def test_reconcile_finishes_a_lane_that_landed_before_its_status_was_written(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    with monkeypatch.context() as crashing:
        crash_when(crashing, "done")
        with pytest.raises(Crash):
            run_lanes_over(repo, ["--single-slice"], monkeypatch)
    landed = repo.head()
    assert repo.statuses()[FIRST] == "in_progress"
    (record,) = records(state)
    assert record.is_open and record.judgement is not None and record.judgement.head == landed

    refused = run_lanes_over(repo, [], monkeypatch)
    assert refused.exit_code == 2
    assert f"in_progress slice {FIRST}; run 'run_lanes --reconcile'" in refused.stderr

    preview = run_lanes_over(repo, ["--reconcile", "--dry-run"], monkeypatch)
    assert preview.exit_code == 0 and json.loads(preview.stdout)["applied"] is False
    assert repo.statuses()[FIRST] == "in_progress"

    reconciled = run_lanes_over(repo, ["--reconcile"], monkeypatch)
    assert reconciled.exit_code == 0
    report = json.loads(reconciled.stdout)
    assert [(item["slice_id"], item["queue_status"]) for item in report["reconcile"]["slices"]] == [(FIRST, "done")]
    assert report["removed_worktrees"] == [str(record.worktree)] and report["kept_worktrees"] == []
    assert repo.statuses() == {FIRST: "done", SECOND: "queued"}
    assert not record.worktree.exists() and lane_branches(repo) == []

    after = run_lanes_over(repo, [], monkeypatch)
    assert after.exit_code == 0 and repo.statuses() == {FIRST: "done", SECOND: "done"}


def test_reconcile_fails_a_lane_the_supervisor_never_judged_and_keeps_its_worktree(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    base = repo.head()

    def crash(*args, **kwargs):
        raise Crash("before the verdict")

    with monkeypatch.context() as crashing:
        crashing.setattr(policy, "evaluate_completion", crash)
        with pytest.raises(Crash):
            run_lanes_over(repo, [], monkeypatch)
    reconciled = run_lanes_over(repo, ["--reconcile"], monkeypatch)
    assert reconciled.exit_code == 0
    (item,) = json.loads(reconciled.stdout)["reconcile"]["slices"]
    assert (item["queue_status"], item["reason_code"]) == ("failed", "supervisor_error")
    assert len(item["worktrees"]) == 1 and Path(item["worktrees"][0]).is_dir()
    assert repo.statuses()[FIRST] == "failed" and repo.head() == base


def test_a_handoff_edited_after_it_was_committed_is_an_uncommitted_change(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, {FIRST: "edit-handoff"})
    base = repo.head()
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2
    (report,) = outcome.reports
    assert report["reason"].startswith(f"Lane for slice {FIRST} left uncommitted changes")
    assert "automation/handoffs/" in report["reason"]
    assert repo.head() == base and repo.statuses()[FIRST] == "failed"


def test_lane_setup_that_leaves_files_git_doesnt_ignore_fails_before_the_runner(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path, None, slice_record(FIRST, 10), lane_setup=["echo built > generated.txt"])
    outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2
    (report,) = outcome.reports
    assert "lane_setup left files Git doesn't ignore, which the clean-end check would refuse: generated.txt" in report["reason"]
    assert not repo.snapshots.exists()  # the agent never ran
    assert repo.statuses() == {FIRST: "failed"}


def test_an_interrupt_after_integration_started_leaves_the_slice_for_reconcile(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    original = run_next.JsonQueue.set_status

    def interrupted_at_done(self, slice_record, status, reason="", handoff_path=None):
        if status == "done":
            raise lane_recovery.Terminated(15)
        return original(self, slice_record, status, reason, handoff_path)

    with monkeypatch.context() as patched:
        patched.setattr(run_next.JsonQueue, "set_status", interrupted_at_done)
        outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 143
    assert f"interrupted while integrating slice {FIRST}. It stays in_progress" in outcome.stderr
    assert repo.statuses()[FIRST] == "in_progress"
    (record,) = records(state)
    assert record.is_open and record.integration is not None
    assert record.integration.outcome is lane_recovery.IntegrationOutcome.LANDED

    reconciled = run_lanes_over(repo, ["--reconcile"], monkeypatch)
    assert [item["queue_status"] for item in json.loads(reconciled.stdout)["reconcile"]["slices"]] == ["done"]
    assert repo.statuses()[FIRST] == "done"


def test_a_claim_whose_write_fails_closes_its_attempt_record(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    original = run_next.JsonQueue.set_status

    def refuse_the_claim(self, slice_record, status, reason="", handoff_path=None):
        if status == "in_progress":
            raise policy.ConfigError("the queue file is read-only")
        return original(self, slice_record, status, reason, handoff_path)

    with monkeypatch.context() as patched:
        patched.setattr(run_next.JsonQueue, "set_status", refuse_the_claim)
        outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 2 and "the queue file is read-only" in outcome.stderr
    assert set(repo.statuses().values()) == {"queued"}
    (record,) = records(state)
    assert record.closure is not None and record.closure.queue_status == "queued"
    assert run_lanes_over(repo, ["--single-slice"], monkeypatch).exit_code == 0  # nothing left to reconcile


def test_reconcile_carries_an_untracked_handoff_out_so_the_worktree_can_go(tmp_path, state, monkeypatch):
    repo = make_repo(tmp_path / "case", queue_document([slice_record(FIRST, 10)]), ignore_handoffs=False)
    with monkeypatch.context() as crashing:
        crash_when(crashing, "done")
        with pytest.raises(Crash):
            run_lanes_over(repo, [], monkeypatch)
    (record,) = records(state)
    reconciled = run_lanes_over(repo, ["--reconcile"], monkeypatch)
    report = json.loads(reconciled.stdout)
    assert report["removed_worktrees"] == [str(record.worktree)] and report["kept_worktrees"] == []
    (handoff,) = (repo.root / "automation/handoffs").iterdir()
    assert handoff.name.endswith(f"-{FIRST}.json")


def test_an_interrupt_between_slices_says_nothing_was_in_flight(tmp_path, state, monkeypatch):
    repo = repo_with(tmp_path)
    original = run_next.JsonQueue.load
    calls = []

    def interrupted_on_the_third_load(self):
        calls.append(1)
        if len(calls) == 5:  # the run, the first slice, its claim and its verdict (set_status loads), the second slice
            raise KeyboardInterrupt
        return original(self)

    with monkeypatch.context() as patched:
        patched.setattr(run_next.JsonQueue, "load", interrupted_on_the_third_load)
        outcome = run_lanes_over(repo, [], monkeypatch)
    assert outcome.exit_code == 130
    assert "interrupted between slices; nothing was in flight" in outcome.stderr
    assert repo.statuses() == {FIRST: "done", SECOND: "queued"}
