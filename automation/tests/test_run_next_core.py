"""The functional core of ``run_next``: each decision the supervisor makes, from the facts it gathered.

Every test here calls a function and checks only what it returns, so the tests survive any rewrite
of the shell that keeps the decisions the same. The core functions are pure; the few path helpers
tested at the end (``board_inside_repository``, ``repo_relative``, ``report_file_for``) only resolve
paths. The shell itself, with a real Git
repository, queue, board and agent script, is tested in the other ``test_run_next_*`` files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from automation.supervisor import policy
from automation.supervisor.run_next import (
    Claim,
    DryRun,
    Failure,
    HandoffInvalid,
    HandoffLoaded,
    HandoffMissing,
    NothingEligible,
    Refuse,
    Selected,
    Verdict,
    active_refusal,
    autonomous_limit,
    board_inside_repository,
    command_refusal,
    dry_run_report,
    fail,
    failure_report,
    format_adjacent_slices,
    format_documents,
    format_plain_list,
    format_validation_ownership,
    format_value_list,
    handoff_failure,
    judge_step,
    make_decision_report,
    make_failure_report,
    plan_step,
    repo_relative,
    report_file_for,
    select_step,
    serialize_validation_replays,
    session_error,
    session_failure,
)

OWNED = ["automation/queue/slices.json", "automation/handoffs/"]
POLICY = {"supervisor_owned_paths": OWNED, "consecutive_autonomous_limit": 5}
HANDOFF_PATH = Path("/repo/automation/handoffs/20260101T000000Z-s1.json")


def record(slice_id: str, status: str = "queued", priority: int = 10, depends_on: tuple[str, ...] = (), **extra: Any) -> dict:
    return {
        "slice_id": slice_id,
        "title": slice_id,
        "status": status,
        "priority": priority,
        "domain": "example",
        "allowed_paths": ["docs/"],
        "required_validations": ["git diff --check", "make test"],
        "depends_on": list(depends_on),
        "max_files_changed": 1,
        "notes": "",
        **extra,
    }


def queue(*slices: dict) -> dict:
    return {"version": 1, "policy": POLICY, "slices": list(slices)}


# --- start-of-run refusals -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slices", "expected"),
    [
        ((), None),
        ((record("a"), record("b", "done"), record("c", "failed")), None),
        ((record("a"), record("b", "in_progress")), "stop: queue already has in_progress slice b"),
        ((record("b", "in_progress"), record("c", "in_progress")), "stop: queue already has in_progress slice b"),
    ],
    ids=["empty", "none-in-progress", "one-in-progress", "first-of-two-named"],
)
def test_a_slice_already_in_progress_refuses_the_run(slices, expected):
    assert active_refusal(queue(*slices)) == expected


NO_COMMAND = "stop: no agent command template configured. Pass --agent-cmd or set policy.agent_command_template."


@pytest.mark.parametrize(
    ("template", "dry_run", "expected"),
    [
        ("", False, NO_COMMAND),
        ("  \n\t", False, NO_COMMAND),
        ("", True, None),
        ("  ", True, None),
        ("run {slice_id}", False, None),
        ("run {slice_id}", True, None),
    ],
)
def test_a_real_run_needs_an_agent_command_and_a_dry_run_does_not(template, dry_run, expected):
    assert command_refusal(template, dry_run) == expected


@pytest.mark.parametrize(
    ("resolved", "policy_limit", "expected"),
    [(None, 5, 5), (0, 5, 5), (1, 5, 1), (3, 5, 3), (7, 5, 7)],
    ids=["unset", "zero-falls-back", "one", "lower", "higher"],
)
def test_the_autonomous_limit_falls_back_to_the_policy_when_unset_or_zero(resolved, policy_limit, expected):
    assert autonomous_limit(resolved, policy_limit) == expected


# --- selection ---------------------------------------------------------------------------------------


NONE_ELIGIBLE = "stop: no eligible queued slice found."


def test_the_lowest_priority_number_is_selected_and_ties_keep_queue_order():
    first, second, third = record("x", priority=20), record("y", priority=10), record("z", priority=10)

    assert select_step(queue(first, second, third), include_blocked=False) == Selected(second)


def test_a_slice_with_unfinished_dependencies_is_skipped():
    waiting = record("waiting", priority=1, depends_on=("dep",))
    ready = record("ready", priority=50)

    assert select_step(queue(record("dep", "queued", 99), waiting, ready), include_blocked=False) == Selected(ready)
    assert select_step(queue(record("dep", "done"), waiting, ready), include_blocked=False) == Selected(waiting)


@pytest.mark.parametrize("include_blocked", [False, True])
def test_with_nothing_queued_the_run_stops_with_one_line(include_blocked):
    assert select_step(queue(record("a", "done")), include_blocked) == NothingEligible((NONE_ELIGIBLE,))


def test_blocked_and_deferred_slices_are_listed_only_when_asked_for():
    blocked = record("b", "blocked", entry_condition=" needs a key ", recommended_unblocker=" ask the owner ")
    deferred = record("d", "deferred", entry_condition="later")
    data = queue(record("a", "done"), blocked, deferred)

    assert select_step(data, include_blocked=False) == NothingEligible((NONE_ELIGIBLE,))
    assert select_step(data, include_blocked=True) == NothingEligible(
        (
            NONE_ELIGIBLE,
            "",
            "blocked/deferred slices:",
            "- blocked: b",
            "  missing entry condition: needs a key",
            "  recommended unblocker: ask the owner",
            "- deferred: d",
            "  missing entry condition: later",
            "  recommended unblocker: <none recorded>",
        )
    )


# --- planning: refuse, dry run or claim -------------------------------------------------------------


@pytest.mark.parametrize(
    ("dirty", "dry_run", "expected"),
    [
        ([], False, Claim),
        ([], True, DryRun),
        (["docs/a.md", "docs/sub/b.md"], False, Claim),
        (["automation/queue/slices.json", "automation/handoffs/x.json"], True, DryRun),
    ],
    ids=["clean-claim", "clean-dry-run", "in-scope", "supervisor-owned"],
)
def test_a_worktree_dirty_only_in_scope_goes_ahead(dirty, dry_run, expected):
    slice_record = record("s1")

    assert plan_step(slice_record, dirty_paths_before_run=dirty, queue_policy=POLICY, dry_run=dry_run) == expected(slice_record)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_worktree_dirty_outside_scope_is_refused_before_anything_else(dry_run):
    dirty = ["src/z.py", "docs/ok.md", "docs-private/a.md", "src/z.py"]

    assert plan_step(record("s1"), dirty_paths_before_run=dirty, queue_policy=POLICY, dry_run=dry_run) == Refuse(
        "stop: repo is dirty outside the next slice scope:\n- docs-private/a.md\n- src/z.py"
    )


BUNDLE = {
    "validation_ownership": [{"command": "git diff --check", "tier": "supervisor_replayable", "reason": "r"}],
    "documents": [{"path": "AGENTS.md", "reason": "rules"}, {"path": "docs/a.md", "reason": "scope"}],
}


def dry_run(include_blocked: bool, profile: Optional[dict] = None, data: Optional[dict] = None) -> dict:
    slice_record = record("s1")
    return dry_run_report(
        slice_record,
        handoff_path=HANDOFF_PATH,
        context_bundle=BUNDLE,
        telemetry_report={"mode": "off"},
        profile_record=profile,
        queue_data=data or queue(slice_record),
        include_blocked=include_blocked,
    )


def test_a_dry_run_reports_the_slice_its_validations_and_its_context_in_order():
    report = dry_run(include_blocked=False)

    assert list(report.items()) == [
        ("selected_slice", "s1"),
        ("handoff_path", str(HANDOFF_PATH)),
        ("required_validations", ["git diff --check", "make test"]),
        ("supervisor_replayable_validations", ["git diff --check"]),
        ("validation_ownership", BUNDLE["validation_ownership"]),
        ("allowed_paths", ["docs/"]),
        ("context_documents", ["AGENTS.md", "docs/a.md"]),
        ("telemetry", {"mode": "off"}),
    ]


def test_a_dry_run_adds_the_runner_profile_and_blocked_slices_when_there_are_any():
    blocked = record("b", "blocked", entry_condition="key", recommended_unblocker="owner")
    report = dry_run(include_blocked=True, profile={"effective": "edits"}, data=queue(record("s1"), blocked))

    assert report["runner_profile"] == {"effective": "edits"}
    assert report["blocked_slices"] == [
        {"slice_id": "b", "status": "blocked", "entry_condition": "key", "recommended_unblocker": "owner"}
    ]
    assert list(report)[-2:] == ["runner_profile", "blocked_slices"]


def test_a_dry_run_with_no_profile_and_blocked_asked_for_lists_none():
    report = dry_run(include_blocked=True, profile=None)

    assert "runner_profile" not in report
    assert report["blocked_slices"] == []


# --- judging the session and the handoff ------------------------------------------------------------


def test_a_failure_exits_2_unless_told_otherwise():
    assert fail("broke", "git_error") == Failure("broke", "git_error", 2)
    assert fail("broke", "agent_exit_nonzero", 3) == Failure("broke", "agent_exit_nonzero", 3)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (0, None),
        (3, Failure("Agent command exited with code 3 for slice s1.", "agent_exit_nonzero", 3)),
        (1, Failure("Agent command exited with code 1 for slice s1.", "agent_exit_nonzero", 1)),
        (124, Failure("Agent command exited with code 124 for slice s1.", "agent_timeout", 124)),
        (125, Failure("Agent command exited with code 125 for slice s1.", "agent_exit_nonzero", 125)),
        (-9, Failure("Agent command exited with code -9 for slice s1.", "agent_exit_nonzero", -9)),
    ],
)
def test_an_agent_that_exits_nonzero_fails_the_slice_with_its_own_exit_code(exit_code, expected):
    assert session_failure(exit_code, "s1") == expected


@pytest.mark.parametrize(("exit_code", "code"), [(124, "agent_timeout"), (123, "agent_exit_nonzero"), (2, "agent_exit_nonzero")])
def test_only_timeouts_exit_code_is_a_timeout(exit_code, code):
    assert session_error(exit_code) == code


LOADED = HandoffLoaded({"slice_id": "s1", "status": "done"}, b"{}")


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (HandoffMissing(), Failure(f"Missing handoff artifact for slice s1 at {HANDOFF_PATH}", "handoff_missing", 2)),
        (HandoffInvalid("bad json"), Failure("Invalid handoff artifact: bad json", "handoff_invalid", 2)),
        (
            HandoffLoaded({"slice_id": "s2", "status": "done"}, b"{}"),
            Failure("Handoff slice_id does not match the in-progress slice.", "handoff_slice_mismatch", 2),
        ),
        (LOADED, None),
    ],
    ids=["missing", "invalid", "other-slice", "valid"],
)
def test_a_handoff_must_exist_be_valid_and_name_the_claimed_slice(read, expected):
    assert handoff_failure(read, "s1", HANDOFF_PATH) == expected


# --- reports ----------------------------------------------------------------------------------------


def report_kwargs(**overrides: Any) -> dict:
    kwargs: dict[str, Any] = {
        "slice_record": record("s1"),
        "completed_runs": 1,
        "autonomous_limit": 5,
        "telemetry": {"mode": "off"},
        "runner_profile": None,
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
    }
    return kwargs | overrides


def test_a_recorded_failure_reports_the_slice_failed():
    report = failure_report(Failure("agent broke", "agent_exit_nonzero", 3), None, **report_kwargs())

    assert report == {
        "telemetry": {"mode": "off"},
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "slice_id": "s1",
        "queue_status": "failed",
        "decision": "stop_failed",
        "reason": "agent broke",
        "recommended_next_slice": "",
        "next_slice_id": None,
        "changed_file_count": 0,
        "required_validation_failures": [],
        "missing_manual_proofs": [],
        "missing_proof_level": False,
        "supervisor_validation_replays": [],
        "dirty_paths_outside_scope": [],
        "files_touched_outside_scope": [],
        "completed_autonomous_runs": 1,
        "autonomous_run_limit": 5,
    }


def test_a_failure_the_store_refused_reports_the_slice_still_in_progress():
    report = failure_report(Failure("agent broke", "agent_exit_nonzero", 3), "disk full", **report_kwargs(head_sha=None))

    assert (report["queue_status"], report["decision"], report["head_sha"]) == ("in_progress", "stop_failed", None)
    assert report["reason"] == "agent broke\nRecording the failure also failed, so the slice is still in_progress: disk full"


def test_a_failure_before_a_base_commit_carries_no_commits_and_a_profile_when_chosen():
    report = failure_report(Failure("x", "git_error", 2), None, **report_kwargs(base_sha=None, runner_profile={"p": 1}))

    assert "base_sha" not in report and "head_sha" not in report
    assert list(report)[:2] == ["telemetry", "runner_profile"] and report["runner_profile"] == {"p": 1}


def test_the_failure_report_alone_defaults_to_failed_and_omits_absent_blocks():
    report = make_failure_report(slice_record=record("s1"), reason="r", completed_runs=0, autonomous_limit=1)

    assert list(report)[:2] == ["slice_id", "queue_status"] and report["queue_status"] == "failed"


def decision(**overrides: Any) -> policy.CompletionDecision:
    values: dict[str, Any] = {
        "queue_status": "done",
        "decision": "stop_for_review",
        "should_continue": False,
        "stop_reason": "",
        "next_slice_id": "s2",
        "recommended_next_slice": "s2",
        "required_validation_failures": ["make test"],
        "dirty_paths_outside_scope": ["x"],
        "files_touched_outside_scope": ["y"],
        "changed_file_count": 2,
        "supervisor_validation_replays": [policy.ValidationReplayResult("git diff --check", True, 0, "")],
        "missing_manual_proofs": ["p"],
        "missing_proof_level": True,
    }
    return policy.CompletionDecision(**(values | overrides))


def judge(**overrides: Any) -> Verdict:
    return judge_step(record("s1"), decision(**overrides), **{k: v for k, v in report_kwargs().items() if k != "slice_record"})


def test_a_judged_slice_reports_every_decision_field():
    verdict = judge()

    assert verdict.report == {
        "telemetry": {"mode": "off"},
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "slice_id": "s1",
        "queue_status": "done",
        "decision": "stop_for_review",
        "reason": "",
        "recommended_next_slice": "s2",
        "next_slice_id": "s2",
        "changed_file_count": 2,
        "required_validation_failures": ["make test"],
        "missing_manual_proofs": ["p"],
        "missing_proof_level": True,
        "supervisor_validation_replays": [{"command": "git diff --check", "success": True, "exit_code": 0, "reason": ""}],
        "dirty_paths_outside_scope": ["x"],
        "files_touched_outside_scope": ["y"],
        "completed_autonomous_runs": 1,
        "autonomous_run_limit": 5,
    }
    assert verdict.report_sha256 == hashlib.sha256(json.dumps(verdict.report, sort_keys=True).encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("stop_reason", "verdict_decision", "expected"),
    [("", "stop_for_review", "stop_for_review"), ("scope broken", "stop_failed", "scope broken")],
)
def test_the_store_records_the_stop_reason_or_else_the_decision(stop_reason, verdict_decision, expected):
    assert judge(stop_reason=stop_reason, decision=verdict_decision).store_reason == expected


@pytest.mark.parametrize("should_continue", [True, False])
def test_the_verdict_says_whether_the_loop_continues(should_continue):
    assert judge(should_continue=should_continue).should_continue is should_continue


def test_the_decision_report_alone_omits_absent_telemetry_and_commits():
    report = make_decision_report(slice_record=record("s1"), decision=decision(), completed_runs=2, autonomous_limit=3)

    assert list(report)[0] == "slice_id"
    assert (report["completed_autonomous_runs"], report["autonomous_run_limit"]) == (2, 3)


def test_validation_replays_serialize_every_field_in_order():
    replays = [policy.ValidationReplayResult("a", False, 1, "failed"), policy.ValidationReplayResult("b", True, 0, "")]

    assert serialize_validation_replays(replays) == [
        {"command": "a", "success": False, "exit_code": 1, "reason": "failed"},
        {"command": "b", "success": True, "exit_code": 0, "reason": ""},
    ]


# --- prompt formatting ------------------------------------------------------------------------------


def test_value_and_plain_lists_render_one_line_per_value_or_a_placeholder():
    assert format_value_list(["a", "b"]) == "- `a`\n- `b`"
    assert format_value_list([]) == "- none"
    assert format_value_list([], empty_message="nothing") == "nothing"
    assert format_plain_list(["a", "b"]) == "- a\n- b"
    assert format_plain_list([]) == "- none"
    assert format_plain_list([], empty_message="nothing") == "nothing"


def test_adjacent_slices_documents_and_ownership_render_one_line_each_or_none():
    adjacent = [{"slice_id": "s2", "title": "T", "priority": 3, "domain": "d"}]
    assert format_adjacent_slices(adjacent) == "- `s2`: T (priority 3, domain `d`)"
    assert format_adjacent_slices([]) == "- none"
    assert format_documents([{"path": "a", "reason": "r"}, {"path": "b", "reason": "s"}]) == "- `a`: r\n- `b`: s"
    assert format_documents([]) == "- none"
    ownership = [{"command": "c", "tier": "t", "reason": "r"}]
    assert format_validation_ownership(ownership) == "- `c` -> `t`: r"
    assert format_validation_ownership([]) == "- none"


# --- paths ------------------------------------------------------------------------------------------


def test_paths_are_reported_relative_to_the_repository_or_absolute_outside_it(tmp_path):
    repo = tmp_path / "repo"
    assert repo_relative(repo / "automation/handoffs/h.json", repo) == "automation/handoffs/h.json"
    assert repo_relative(tmp_path / "elsewhere/h.json", repo) == str((tmp_path / "elsewhere/h.json").resolve())


@pytest.mark.parametrize(
    ("board", "inside"),
    [("repo", True), ("repo/boards/b", True), ("repo-boards/b", False), ("other/b", False)],
)
def test_a_board_counts_as_inside_the_repository_only_at_or_below_its_root(tmp_path, board, inside):
    assert board_inside_repository(tmp_path / board, tmp_path / "repo") is inside


def test_a_report_file_must_lie_outside_the_repository(tmp_path):
    repo = tmp_path / "repo"
    assert report_file_for(None, repo) is None
    assert report_file_for("", repo) is None
    assert report_file_for(str(tmp_path / "out/r.jsonl"), repo) == (tmp_path / "out/r.jsonl").resolve()
    with pytest.raises(policy.ConfigError, match="outside the repository"):
        report_file_for(str(repo / "r.jsonl"), repo)
