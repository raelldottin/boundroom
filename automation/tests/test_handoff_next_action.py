"""The handoff summary's safe next action (RA-PROD-105), as plain data in and out.

``decide_next_action`` chooses the step from the supervisor's report and ``render_next_action``
words it for a person. Both are pure, so these tests need no files and no CLI; the CLI's use
of them is covered in ``test_handoff_summary.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from automation.supervisor import handoff_summary as hs
from automation.supervisor.handoff_summary import NextAction

REQUEUE = " A person decides whether to re-queue the slice."
APPROVE = "A person reviews and approves this slice's changes."


def decision_report(decision: str, **fields: Any) -> dict[str, Any]:
    """The fields of run_next.py's decision report that the next action reads."""
    report: dict[str, Any] = {
        "decision": decision,
        "reason": "",
        "next_slice_id": None,
        "required_validation_failures": [],
        "supervisor_validation_replays": [],
        "files_touched_outside_scope": [],
        "dirty_paths_outside_scope": [],
        "missing_manual_proofs": [],
        "missing_proof_level": False,
    }
    report.update(fields)
    return report


def evaluation_report(decision: str, **fields: Any) -> dict[str, Any]:
    """The fields of evaluate_handoff.py's evaluation report that the next action reads."""
    report: dict[str, Any] = {
        "decision": decision,
        "reason": "",
        "required_validation_failures": [],
        "supervisor_validation_replays": [],
        "files_touched_outside_scope": [],
        "out_of_scope_files": [],
        "missing_manual_proofs": [],
        "missing_proof_level": False,
        "dry_run": False,
    }
    report.update(fields)
    return report


def replay(command: str, success: bool) -> dict[str, Any]:
    return {"command": command, "success": success, "exit_code": 0 if success else 1, "reason": ""}


DECISION = hs.DECISION_REPORT
EVALUATION = hs.EVALUATION_REPORT


# ---------------------------------------------------------------------------
# Choosing the step
# ---------------------------------------------------------------------------

DECIDE_CASES = [
    pytest.param({}, None, None, NextAction("get_independent_evidence"), id="no-report"),
    pytest.param({}, None, decision_report("continue"), NextAction("get_independent_evidence"), id="report-without-kind"),
    pytest.param({}, DECISION, None, NextAction("get_independent_evidence"), id="kind-without-report"),
    pytest.param({}, DECISION, decision_report("stop"), NextAction("queue_stop", "stop"), id="stop"),
    pytest.param(
        {},
        DECISION,
        decision_report("continue", next_slice_id="S-2"),
        NextAction("human_review", "continue", next_slice="S-2"),
        id="continue-with-next-slice",
    ),
    pytest.param(
        {}, DECISION, decision_report("continue"), NextAction("human_review", "continue"), id="continue-without-next-slice"
    ),
    pytest.param(
        {},
        DECISION,
        decision_report("stop_for_review", reason="Consecutive autonomous run limit reached."),
        NextAction("human_review", "stop_for_review", reason="Consecutive autonomous run limit reached."),
        id="stop-for-review",
    ),
    pytest.param(
        {},
        EVALUATION,
        evaluation_report("accept", dry_run=True),
        NextAction("human_review", "accept", dry_run=True),
        id="accept-dry-run",
    ),
    pytest.param(
        {},
        EVALUATION,
        evaluation_report("accept", dry_run=False),
        NextAction("human_review", "accept", dry_run=False),
        id="accept",
    ),
    pytest.param(
        {"open_questions": ["Which runtime?", "Who approves?"]},
        DECISION,
        decision_report("stop_blocked"),
        NextAction("resolve_blocker", "stop_blocked", open_questions=("Which runtime?", "Who approves?")),
        id="stop-blocked-with-questions",
    ),
    pytest.param({}, DECISION, decision_report("stop_blocked"), NextAction("resolve_blocker", "stop_blocked"), id="stop-blocked"),
    pytest.param(
        {},
        DECISION,
        decision_report(
            "stop_failed",
            required_validation_failures=["make b", "make a"],
            # A failed replay counts whether or not it is a required failure; a passing one never does.
            supervisor_validation_replays=[replay("make a", False), replay("make c", False), replay("make d", True)],
        ),
        NextAction("fix_validation", "stop_failed", failing_validations=("make a", "make b", "make c")),
        id="validation-union",
    ),
    pytest.param(
        {},
        DECISION,
        decision_report(
            "stop_failed",
            required_validation_failures=["make test"],
            files_touched_outside_scope=["Makefile"],
            missing_manual_proofs=["screenshot"],
            missing_proof_level=True,
        ),
        NextAction("fix_validation", "stop_failed", failing_validations=("make test",)),
        id="validation-before-scope-and-proof",
    ),
    pytest.param(
        {},
        EVALUATION,
        evaluation_report("reject", out_of_scope_files=["b.txt"], missing_manual_proofs=["screenshot"], missing_proof_level=True),
        NextAction("fix_scope", "reject", outside_paths=("b.txt",)),
        id="scope-before-proof",
    ),
    pytest.param(
        {},
        DECISION,
        {
            **decision_report("stop_failed"),
            "files_touched_outside_scope": ["c.txt", "a.txt"],
            "dirty_paths_outside_scope": ["b.txt", "a.txt"],
            "out_of_scope_files": ["d.txt", "c.txt"],
        },
        NextAction("fix_scope", "stop_failed", outside_paths=("a.txt", "b.txt", "c.txt", "d.txt")),
        id="scope-union",
    ),
    pytest.param(
        {},
        DECISION,
        decision_report("stop_failed", missing_manual_proofs=["screenshot"]),
        NextAction("provide_proof", "stop_failed", missing_manual_proofs=("screenshot",), missing_proof_level=False),
        id="manual-proofs-only",
    ),
    pytest.param(
        {},
        EVALUATION,
        evaluation_report("reject", missing_proof_level=True),
        NextAction("provide_proof", "reject", missing_proof_level=True),
        id="proof-level-only",
    ),
    pytest.param(
        {},
        DECISION,
        decision_report("stop_failed", reason="Agent command exited with code 3."),
        NextAction("investigate_failure", "stop_failed", reason="Agent command exited with code 3."),
        id="investigate",
    ),
]


@pytest.mark.parametrize(("handoff", "kind", "report", "expected"), DECIDE_CASES)
def test_the_next_action_is_chosen_from_the_report(handoff, kind, report, expected):
    assert hs.decide_next_action(handoff, kind, report) == expected


# ---------------------------------------------------------------------------
# Wording the step
# ---------------------------------------------------------------------------

RENDER_CASES = [
    pytest.param(
        NextAction("get_independent_evidence"),
        "Not independently verified. Get the supervisor's own evidence first: its decision report from run_next.py, "
        "or evaluate_handoff.py --dry-run for this slice. Then a person reviews it.",
        id="get-independent-evidence",
    ),
    pytest.param(
        NextAction("queue_stop", "stop"),
        f"No action: queue stop. The supervisor found no eligible queued slice after this one. {APPROVE}",
        id="queue-stop",
    ),
    pytest.param(
        NextAction("human_review", "continue", next_slice="S-2"),
        f"{APPROVE} The supervisor's report names `S-2` as the eligible next slice. Whether anything runs next is "
        "the supervisor's decision under its own gates; this summary starts nothing.",
        id="continue-with-next-slice",
    ),
    pytest.param(
        NextAction("human_review", "continue"),
        f"{APPROVE} Whether anything runs next is the supervisor's decision under its own gates; this summary starts nothing.",
        id="continue-without-next-slice",
    ),
    pytest.param(
        NextAction("human_review", "stop_for_review", reason="Limit reached."),
        f"{APPROVE} The supervisor stopped: Limit reached.",
        id="stop-for-review",
    ),
    pytest.param(
        NextAction("human_review", "accept", dry_run=True),
        f"{APPROVE} The evaluation was a dry run, so the queue was not changed.",
        id="accept-dry-run",
    ),
    pytest.param(NextAction("human_review", "accept", dry_run=False), APPROVE, id="accept"),
    pytest.param(
        NextAction("resolve_blocker", "stop_blocked", open_questions=("Which runtime?",)),
        "A person resolves what blocked the slice, then decides whether to re-queue it. "
        "The agent's open questions: `Which runtime?`.",
        id="resolve-blocker-with-questions",
    ),
    pytest.param(
        NextAction("resolve_blocker", "stop_blocked"),
        "A person resolves what blocked the slice, then decides whether to re-queue it.",
        id="resolve-blocker",
    ),
    pytest.param(
        NextAction("fix_validation", "stop_failed", failing_validations=("make test",)),
        f"Fix the failing validation: `make test`.{REQUEUE}",
        id="fix-one-validation",
    ),
    pytest.param(
        NextAction("fix_validation", "reject", failing_validations=("make a", "make b")),
        f"Fix the failing validations: `make a`, `make b`.{REQUEUE}",
        id="fix-two-validations",
    ),
    pytest.param(
        NextAction("fix_scope", "reject", outside_paths=("a.txt", "b.txt")),
        f"Revert or move the changes outside the slice's allowed paths: `a.txt`, `b.txt`.{REQUEUE}",
        id="fix-scope",
    ),
    pytest.param(
        NextAction("provide_proof", "stop_failed", missing_manual_proofs=("screenshot",), missing_proof_level=False),
        f"Provide the missing manual proof: `screenshot`.{REQUEUE}",
        id="provide-manual-proof",
    ),
    pytest.param(
        NextAction("provide_proof", "reject", missing_proof_level=True),
        f"Reach the slice's required proof level.{REQUEUE}",
        id="reach-proof-level",
    ),
    pytest.param(
        NextAction("provide_proof", "reject", missing_manual_proofs=("screenshot",), missing_proof_level=True),
        f"Provide the missing manual proof: `screenshot`; reach the slice's required proof level.{REQUEUE}",
        id="provide-proof-and-reach-level",
    ),
    pytest.param(
        NextAction("investigate_failure", "stop_failed", reason="Agent command exited with code 3."),
        f"Investigate the failure the supervisor reported: Agent command exited with code 3.{REQUEUE}",
        id="investigate",
    ),
]


@pytest.mark.parametrize(("action", "text"), RENDER_CASES)
def test_each_next_action_is_worded_for_a_person(action, text):
    assert hs.render_next_action(action) == text


def test_report_text_in_a_next_action_is_escaped_and_clipped():
    stopped = hs.render_next_action(NextAction("human_review", "stop_for_review", reason="\x1b[2Jlimit"))
    assert stopped == f"{APPROVE} The supervisor stopped: \\x1b[2Jlimit"

    named = hs.render_next_action(NextAction("human_review", "continue", next_slice="s" * 450))
    assert f"`{'s' * 400}... [50 more characters]`" in named


def test_five_names_are_shown_whole_and_a_sixth_is_counted():
    five = tuple(f"p{index}" for index in range(5))
    assert hs.render_next_action(NextAction("fix_scope", outside_paths=five)) == (
        f"Revert or move the changes outside the slice's allowed paths: `p0`, `p1`, `p2`, `p3`, `p4`.{REQUEUE}"
    )
    six = (*five, "p5")
    assert hs.render_next_action(NextAction("fix_scope", outside_paths=six)) == (
        f"Revert or move the changes outside the slice's allowed paths: `p0`, `p1`, `p2`, `p3`, `p4` (and 1 more).{REQUEUE}"
    )


def test_next_action_words_the_step_it_chose():
    assert hs.next_action({}, None, None) == {
        "kind": "get_independent_evidence",
        "text": "Not independently verified. Get the supervisor's own evidence first: its decision report from run_next.py, "
        "or evaluate_handoff.py --dry-run for this slice. Then a person reviews it.",
    }
    report = decision_report("stop_failed", required_validation_failures=[f"make check-{index}" for index in range(8)])
    assert hs.next_action({}, DECISION, report) == {
        "kind": "fix_validation",
        "text": "Fix the failing validations: `make check-0`, `make check-1`, `make check-2`, `make check-3`, `make check-4` "
        f"(and 3 more).{REQUEUE}",
    }


@pytest.mark.parametrize("action", [case.values[0] for case in RENDER_CASES])
def test_no_next_action_tells_anyone_to_start_the_next_slice(action):
    text = hs.render_next_action(action)
    assert not re.search(r"\b(run|start|launch)\b[^.]*\bnext slice\b", text, re.IGNORECASE)
    assert "automatically" not in text.lower()
