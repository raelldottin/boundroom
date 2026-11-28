"""The pure rules of ``run_lanes`` (SPEC-014 RA-LANE-005): output-based tests, no Git, files or processes."""

from __future__ import annotations

import pytest

from automation.supervisor import lane_recovery, lane_workspace, policy, run_lanes, run_next
from automation.tests.run_lanes_fixtures import queue_document, slice_record

OWNED = ["automation/queue/slices.json", "automation/handoffs/"]


# --- the ceiling -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy_max", "requested", "lanes", "notice"),
    [
        (None, None, 1, ""),
        (1, None, 1, ""),
        (None, 1, 1, ""),
        (1, 1, 1, ""),
        (3, None, 1, "a ceiling of 3 lanes was asked for"),
        (3, 2, 1, "a ceiling of 2 lanes was asked for"),
        (8, 8, 1, "a ceiling of 8 lanes was asked for"),
        (3, 1, 1, ""),
    ],
)
def test_the_ceiling_is_the_queues_lowered_by_the_flag_and_capped_at_one_lane(policy_max, requested, lanes, notice):
    ceiling = run_lanes.resolve_ceiling(policy_max, requested)
    assert isinstance(ceiling, run_lanes.Ceiling)
    assert ceiling.lanes == lanes
    if notice:
        assert notice in ceiling.notice and "one lane at a time" in ceiling.notice
    else:
        assert ceiling.notice == ""


@pytest.mark.parametrize(("policy_max", "requested"), [(None, 2), (1, 2), (3, 4), (2, 8)])
def test_a_flag_that_would_raise_the_ceiling_is_refused_by_configuration(policy_max, requested):
    refusal = run_lanes.resolve_ceiling(policy_max, requested)
    assert isinstance(refusal, run_lanes.CeilingRefusal)
    assert refusal == run_lanes.CeilingRefusal(refusal.message, 78)
    assert f"--max-lanes {requested} is above the queue's max_lanes of {policy_max or 1}" in refusal.message


@pytest.mark.parametrize("requested", [0, -1])
def test_a_ceiling_below_one_is_a_usage_error(requested):
    refusal = run_lanes.resolve_ceiling(None, requested)
    assert refusal == run_lanes.CeilingRefusal(f"stop: --max-lanes must be at least 1, not {requested}.", 64)


# --- admission ---------------------------------------------------------------------------------------

QUEUES = {
    "priority": queue_document([slice_record("b", 20), slice_record("a", 10)]),
    "a tie keeps queue order": queue_document([slice_record("b", 10), slice_record("a", 10)]),
    "dependencies": queue_document([slice_record("a", 10, depends_on=["b"]), slice_record("b", 20)]),
    "overlapping paths": queue_document(
        [slice_record("a", 10, allowed_paths=["."]), slice_record("b", 20, allowed_paths=["docs/"])]
    ),
    "exclusive first": queue_document([slice_record("a", 10, exclusive=True), slice_record("b", 20)]),
    "a done dependency": queue_document([slice_record("a", 10, status="done"), slice_record("b", 20, depends_on=["a"])]),
    "blocked and deferred skipped": queue_document(
        [slice_record("a", 1, status="blocked"), slice_record("b", 2, status="deferred"), slice_record("c", 3)]
    ),
}


@pytest.mark.parametrize("name", list(QUEUES))
@pytest.mark.parametrize("ceiling", [1, 8])
def test_the_slice_admitted_between_lanes_is_the_one_run_next_selects(name, ceiling):
    queue = QUEUES[name]
    step = run_lanes.admit_step(queue, include_blocked=False, ceiling=ceiling)
    assert step == run_next.select_step(queue, include_blocked=False)
    assert isinstance(step, run_next.Selected)
    assert step.slice_record is policy.find_slice(queue, step.slice_record["slice_id"])


@pytest.mark.parametrize("include_blocked", [False, True])
def test_with_nothing_eligible_admission_prints_what_run_next_prints(include_blocked):
    queue = queue_document(
        [
            slice_record("a", 1, status="blocked", entry_condition="a person", recommended_unblocker="ask"),
            slice_record("b", 2, status="done"),
            slice_record("c", 3, depends_on=["a"]),
        ]
    )
    step = run_lanes.admit_step(queue, include_blocked=include_blocked, ceiling=1)
    assert step == run_next.select_step(queue, include_blocked=include_blocked)
    assert isinstance(step, run_next.NothingEligible)
    assert ("- blocked: a" in step.lines) is include_blocked


def test_nothing_eligible_is_select_steps_own_answer():
    queue = queue_document([slice_record("a", 1, status="done")])
    assert run_next.nothing_eligible(queue, False) == run_next.NothingEligible(("stop: no eligible queued slice found.",))


def test_a_dry_run_names_what_starts_what_is_held_and_the_worst_case_time():
    queue = queue_document([slice_record("a", 10), slice_record("b", 20, allowed_paths=["src/"])], handoff_timeout_seconds=90)
    assert run_lanes.admission_lines(queue, ceiling=1, budget=5) == [
        "lanes: ceiling 1; would start a",
        "lanes: held b (ceiling)",
        "lanes: worst-case session time 90s",
    ]
    assert run_lanes.admission_lines(queue, ceiling=1, budget=1)[1] == "lanes: held b (budget)"
    assert run_lanes.admission_lines(queue, ceiling=2, budget=5) == [
        "lanes: ceiling 2; would start a, b",
        "lanes: worst-case session time 180s",
    ]


def test_a_dry_run_names_the_lane_a_held_slice_waits_on():
    overlapping = queue_document([slice_record("a", 10), slice_record("b", 20)])
    assert run_lanes.admission_lines(overlapping, ceiling=2, budget=5)[1] == "lanes: held b (overlap by a)"
    exclusive = queue_document([slice_record("a", 10, exclusive=True), slice_record("b", 20, allowed_paths=["src/"])])
    assert run_lanes.admission_lines(exclusive, ceiling=2, budget=5)[1] == "lanes: held b (exclusive by a)"


def test_a_dry_run_with_nothing_eligible_starts_nothing():
    queue = queue_document([slice_record("a", 10, status="done")])
    assert run_lanes.admission_lines(queue, ceiling=1, budget=2) == [
        "lanes: ceiling 1; would start nothing",
        "lanes: worst-case session time 0s",
    ]


# --- refusals ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("dirty", [[], ["automation/queue/slices.json"], ["automation/handoffs/x.json"]])
def test_a_worktree_dirty_only_where_the_supervisor_writes_is_allowed(dirty):
    assert run_lanes.dirty_refusal(dirty, OWNED) is None


def test_any_other_dirty_path_is_refused_even_inside_the_slices_scope():
    message = run_lanes.dirty_refusal(["automation/queue/slices.json", "docs/b.md", "docs/a.md"], OWNED)
    assert message is not None
    assert message.endswith("Commit or stash them first:\n- docs/a.md\n- docs/b.md")


def test_an_attempt_id_is_the_timestamp_token_and_a_random_token():
    assert run_lanes.new_attempt_id("20261127T000000Z", "0a1b2c3d") == "20261127T000000Z-0a1b2c3d"
    with pytest.raises(lane_workspace.LaneRefused):
        run_lanes.new_attempt_id("20261127T000000Z", "../x")


@pytest.mark.parametrize(
    ("error", "code"),
    [(KeyboardInterrupt(), 130), (lane_recovery.Terminated(15), 143), (lane_recovery.Terminated(1), 129)],
)
def test_an_interrupt_exits_with_128_plus_the_signal(error, code):
    assert run_lanes.interrupted_exit_code(error) == code


# --- the command line --------------------------------------------------------------------------------


def options(parser) -> set[str]:
    return {option for action in parser._actions for option in action.option_strings}


def test_run_lanes_takes_every_run_next_flag_and_the_lane_flags():
    assert options(run_lanes.build_parser()) == options(run_next.build_parser()) | {"--max-lanes", "--reconcile"}


def test_run_nexts_command_line_is_unchanged():
    assert not {"--max-lanes", "--reconcile"} & options(run_next.build_parser())
    defaults = run_lanes.parse_args([])
    assert defaults.max_lanes is None and defaults.reconcile is False


def test_run_nexts_own_workspace_changes_nothing(tmp_path):
    """The seam's defaults: run_next's repository, its selection, no extra refusal, the report as built."""
    workspace = run_next.Workspace(tmp_path)
    queue = QUEUES["priority"]
    assert workspace.entry_point == "run_next"
    assert workspace.launch is run_next.launch_in_place
    assert workspace.proof_root is None
    assert workspace.select(queue, False) == run_next.select_step(queue, False)
    assert workspace.refusal(slice_record("a", 1), ["src/x"], queue["policy"]) is None
    assert workspace.root(slice_record("a", 1), "20261127T000000Z") == tmp_path
    assert workspace.prepare(queue["policy"], tmp_path / "h.json", ["x"]) == ["x"]
    assert workspace.settle("done", "0" * 40, "h.json") is None
    report = {"slice_id": "a"}
    assert workspace.report(report) is report
