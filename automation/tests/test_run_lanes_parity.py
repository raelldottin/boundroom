"""With a ceiling of one lane, ``run_lanes`` does what ``run_next`` does (SPEC-014 invariant 6, RA-LANE-005).

Each case builds two identical repositories, runs ``run_next`` over one and ``run_lanes --max-lanes 1``
over the other with the same flags and the same scripted agent, and compares:

- the exit code;
- every queue write: the queue the agent saw when it started (the claim) and the queue at the end;
- every decision, failure and dry-run report, as ``--report-file`` received them, and as stdout
  printed them;
- every other line stdout printed, such as what a run with nothing eligible prints;
- where the repository's branch ends, for runs whose slices all land.

Only the fields that name the worktree differ: the worktree path, the handoff path under it, and
the report's ``lane`` block. ``normalize`` replaces those, and the seconds in a timeout message, which
vary between two runs of run_next itself, and nothing else. Commits are deterministic
and the clock run_next names handoffs by is fixed, so ``base_sha``, ``head_sha`` and the handoff's
file name are compared as they are.

The one place the outputs differ on purpose, a ``done`` slice that leaves changes uncommitted, is
in ``test_run_lanes.py``, beside the other lane-only behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from automation.supervisor import lane_workspace
from automation.tests.run_lanes_fixtures import (
    Outcome,
    freeze_run_next_clock,
    isolate_environment,
    make_repo,
    normalize,
    queue_document,
    run_lanes_over,
    run_next_over,
    slice_record,
    split_printed,
)

FIRST, SECOND, THIRD = "first-slice", "second-slice", "third-slice"


def two_slices(**first_fields) -> dict:
    return queue_document([slice_record(FIRST, 10, **first_fields), slice_record(SECOND, 20)])


# name: (queue, agent behaviour per slice, flags, run_next's exit code, the decisions it reports in order)
CASES = {
    "two slices done, the run continues": (two_slices(), {}, [], 0, ["continue", "stop"]),
    "stop for review at the run limit": (two_slices(), {}, ["--autonomous-limit", "1"], 0, ["stop_for_review"]),
    "stop for review on another recommended slice": (two_slices(), {FIRST: "next-other"}, [], 0, ["stop_for_review"]),
    "a handoff committed in a commit of its own": (
        two_slices(),
        {FIRST: "commit-handoff", SECOND: "commit-handoff"},
        [],
        0,
        ["continue", "stop"],
    ),
    "priority and dependencies pick the order": (
        queue_document([slice_record(THIRD, 5, depends_on=[SECOND]), slice_record(SECOND, 30), slice_record(FIRST, 10)]),
        {},
        [],
        0,
        ["continue", "continue", "stop"],
    ),
    "the handoff reports failed": (two_slices(), {FIRST: "failed"}, [], 0, ["stop_failed"]),
    "the handoff reports blocked": (two_slices(), {FIRST: "blocked"}, [], 0, ["stop_blocked"]),
    "the second slice fails after the first is done": (two_slices(), {SECOND: "failed"}, [], 0, ["continue", "stop_failed"]),
    "the agent exits nonzero": (two_slices(), {FIRST: "exit:3"}, [], 3, ["stop_failed"]),
    "the agent exits 78, passed through": (two_slices(), {FIRST: "exit:78"}, [], 78, ["stop_failed"]),
    "no handoff": (two_slices(), {FIRST: "nohandoff"}, [], 2, ["stop_failed"]),
    "a change outside the slice's scope": (two_slices(), {FIRST: "outside"}, [], 0, ["stop_failed"]),
    "too many files changed": (two_slices(), {FIRST: "toomany"}, [], 0, ["stop_failed"]),
    "a replayed validation fails": (
        two_slices(required_validations=["git diff --check"]),
        {FIRST: "whitespace"},
        [],
        0,
        ["stop_failed"],
    ),
    "a replayed validation passes": (
        two_slices(required_validations=["git diff --check"]),
        {},
        ["--single-slice"],
        0,
        ["stop_for_review"],
    ),
    "the agent runs past the handoff timeout": (
        queue_document([slice_record(FIRST, 10)], handoff_timeout_seconds=5),
        {FIRST: "sleep"},
        [],
        2,
        ["stop_failed"],
    ),
    "dry run": (two_slices(), {}, ["--dry-run"], 0, [None]),
    "dry run listing blocked slices": (
        queue_document([slice_record(FIRST, 10), slice_record(SECOND, 20, status="blocked", entry_condition="later")]),
        {},
        ["--dry-run", "--include-blocked"],
        0,
        [None],
    ),
    "nothing eligible": (queue_document([slice_record(FIRST, 10, status="done")]), {}, [], 0, []),
    "nothing eligible, blocked slices listed": (
        queue_document(
            [
                slice_record(FIRST, 10, status="blocked", entry_condition="a person", recommended_unblocker=SECOND),
                slice_record(SECOND, 20, status="deferred", entry_condition="later"),
                slice_record(THIRD, 30, depends_on=[FIRST]),
            ]
        ),
        {},
        ["--include-blocked"],
        0,
        [],
    ),
    "a slice already in progress": (
        queue_document([slice_record(FIRST, 10, status="in_progress"), slice_record(SECOND, 20)]),
        {},
        [],
        2,
        [],
    ),
    "no agent command": (two_slices(), {}, ["--agent-cmd", ""], 2, []),
    "--single-slice with --autonomous-limit": (two_slices(), {}, ["--single-slice", "--autonomous-limit", "1"], 64, []),
    "--single-slice with --autonomous-limit, a slice in progress": (
        queue_document([slice_record(FIRST, 10, status="in_progress"), slice_record(SECOND, 20)]),
        {},
        ["--single-slice", "--autonomous-limit", "1"],
        64,
        [],
    ),
}


def run_both(tmp_path: Path, monkeypatch, document: dict, behaviours: dict, flags: list[str], *, before=None):
    """The same queue, agent and flags through run_next and through run_lanes --max-lanes 1."""
    isolate_environment(monkeypatch, tmp_path / "state")
    freeze_run_next_clock(monkeypatch)
    next_repo = make_repo(tmp_path / "next", document)
    lanes_repo = make_repo(tmp_path / "lanes", document)
    assert next_repo.head() == lanes_repo.head()  # deterministic commits: the same base
    for repo in (next_repo, lanes_repo):
        repo.behave(behaviours)
        if before:
            before(repo)
    expected = run_next_over(next_repo, flags, monkeypatch)
    actual = run_lanes_over(lanes_repo, ["--max-lanes", "1", *flags], monkeypatch)
    paths = [next_repo.root, next_repo.work, lanes_repo.root, lanes_repo.work]
    lane_roots = [lane_workspace.lane_root(lanes_repo.root, os.environ)]
    return expected, actual, (paths, lane_roots), next_repo, lanes_repo


def comparable(outcome: Outcome, where: tuple[list[Path], list[Path]]) -> dict:
    """What invariant 6 compares, with the worktree-naming fields normalized."""
    printed_reports, printed = split_printed(outcome.stdout)
    return normalize(
        {
            "exit_code": outcome.exit_code,
            "queue_at_claim": outcome.snapshots,
            "queue_at_end": outcome.queue_text,
            "reports": outcome.reports,
            "printed_reports": printed_reports,
            "printed_besides_reports": printed,
        },
        *where,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_one_lane_gives_run_next_outputs(name, tmp_path, monkeypatch):
    document, behaviours, flags, exit_code, decisions = CASES[name]
    expected, actual, where, next_repo, lanes_repo = run_both(tmp_path, monkeypatch, document, behaviours, flags)
    # The case is what it says: run_next's own outcome, so the corpus covers every decision and exit.
    assert expected.exit_code == exit_code, expected.stderr
    assert [report.get("decision") for report in expected.reports] == decisions
    assert comparable(actual, where) == comparable(expected, where)
    if decisions and all(decision in ("continue", "stop", "stop_for_review") for decision in decisions):
        # Every slice landed: the branch ends where run_next leaves it, at the same commit.
        assert actual.head == expected.head
        assert lanes_repo.statuses() == next_repo.statuses()


def test_the_corpus_covers_every_decision_and_the_exit_codes():
    decisions = {decision for case in CASES.values() for decision in case[4]}
    assert decisions == {"continue", "stop", "stop_for_review", "stop_blocked", "stop_failed", None}
    assert {case[3] for case in CASES.values()} == {0, 2, 3, 64, 78}


def test_a_worktree_dirty_outside_the_slice_scope_is_refused_by_both(tmp_path, monkeypatch):
    def dirty(repo):
        (repo.root / "src").mkdir()
        (repo.root / "src/stray.txt").write_text("stray\n", encoding="utf-8")

    expected, actual, where, _, _ = run_both(tmp_path, monkeypatch, two_slices(), {}, [], before=dirty)
    assert expected.exit_code == 2
    assert comparable(actual, where) == comparable(expected, where)
    assert "stop: repo is dirty outside the next slice scope:\n- src/stray.txt" in actual.stderr
    assert expected.reports == actual.reports == []


def test_a_run_inside_a_supervised_slice_is_refused_by_both_with_78(tmp_path, monkeypatch):
    expected, actual, where, next_repo, lanes_repo = run_both(
        tmp_path,
        monkeypatch,
        two_slices(),
        {},
        [],
        before=lambda repo: monkeypatch.setenv("BOUNDROOM_SUPERVISOR_SLICE_ID", "outer"),
    )
    assert expected.exit_code == 78
    assert comparable(actual, where) == comparable(expected, where)
    assert next_repo.statuses() == lanes_repo.statuses() == {FIRST: "queued", SECOND: "queued"}
