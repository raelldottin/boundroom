"""Tests for the handoff review summary (RA-PROD-105).

The handoff is ``automation/examples/example-handoff.json``. The supervisor reports are made
by the functions that make them for real (``run_next.make_decision_report``,
``run_next.make_failure_report`` and ``evaluate_handoff.report_to_dict``), so a change to
either report's shape fails here rather than in front of a reviewer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from automation.supervisor import evaluate_handoff, policy, run_next
from automation.supervisor import handoff_summary as hs

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_HANDOFF = REPO_ROOT / "automation/examples/example-handoff.json"
SLICE_ID = "today-nonfocus-add-to-focus"
SLICE = {"slice_id": SLICE_ID}
HEAD = "a" * 40
BASE = "b" * 40


def make_handoff(**overrides: Any) -> dict[str, Any]:
    handoff = json.loads(EXAMPLE_HANDOFF.read_text(encoding="utf-8"))
    handoff.update(overrides)
    return handoff


def make_decision_report(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "queue_status": "done",
        "decision": "stop",
        "should_continue": False,
        "stop_reason": "No eligible queued next slice remains after the current slice completed.",
        "next_slice_id": None,
        "recommended_next_slice": "",
        "required_validation_failures": [],
        "dirty_paths_outside_scope": [],
        "files_touched_outside_scope": [],
        "changed_file_count": 2,
        "supervisor_validation_replays": [policy.ValidationReplayResult("git diff --check", True, 0, "")],
    }
    fields.update(overrides)
    return run_next.make_decision_report(SLICE, policy.CompletionDecision(**fields), completed_runs=1, autonomous_limit=1)


def make_evaluation_report(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "slice_id": SLICE_ID,
        "queue_status": "done",
        "decision": "accept",
        "reason": "Historical handoff evaluation passed all checks.",
        "changed_files": ["docs/product/domains/today.md"],
        "changed_file_count": 1,
        "out_of_scope_files": [],
        "files_touched_outside_scope": [],
        "required_validation_failures": [],
        "supervisor_validation_replays": [{"command": "git diff --check", "success": True, "exit_code": 0, "reason": ""}],
        "missing_manual_proofs": [],
        "missing_proof_level": False,
        "head_sha": HEAD,
        "base_sha": BASE,
        "dry_run": False,
    }
    fields.update(overrides)
    return evaluate_handoff.report_to_dict(evaluate_handoff.EvaluationReport(**fields))


def write(directory: Path, name: str, payload: Any) -> Path:
    path = directory / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def summarize(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = hs.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def summarize_json(capsys: pytest.CaptureFixture[str], tmp_path: Path, handoff: dict, report: dict | None) -> dict:
    argv = ["--handoff", str(write(tmp_path, "handoff.json", handoff)), "--json"]
    if report is not None:
        argv += ["--report", str(write(tmp_path, "report.json", report))]
    code, out, err = summarize(capsys, *argv)
    assert (code, err) == (0, "")
    return json.loads(out)


# ---------------------------------------------------------------------------
# Verified and unverified labels
# ---------------------------------------------------------------------------


def test_without_a_report_nothing_is_presented_as_verified(capsys, tmp_path):
    code, out, err = summarize(capsys, "--handoff", str(EXAMPLE_HANDOFF))

    assert (code, err) == (0, "")
    assert "NOT INDEPENDENTLY VERIFIED" in out.splitlines()[1]
    assert "Supervisor evidence: none. Nothing below has been independently verified." in out
    assert "Agent claims (from the handoff: the agent's own account, not verified by this summary):" in out
    assert "Independently checked" not in out

    summary = summarize_json(capsys, tmp_path, make_handoff(), None)
    assert summary["verification"]["status"] == "not_independently_verified"
    assert summary["supervisor_evidence"] is None
    assert summary["agent_claims"]["verified"] is False


@pytest.mark.parametrize(
    ("report", "source"),
    [
        (make_decision_report(), "the supervisor's decision report (run_next.py)"),
        (make_evaluation_report(), "the supervisor's evaluation report (evaluate_handoff.py)"),
    ],
)
def test_a_report_is_labelled_independent_and_the_claims_stay_unverified(capsys, tmp_path, report, source):
    report_path = write(tmp_path, "report.json", report)
    code, out, _ = summarize(capsys, "--handoff", str(EXAMPLE_HANDOFF), "--report", str(report_path))

    assert code == 0
    assert f"Verification: Independently checked by {source}." in out
    assert f"Supervisor evidence (independent), from {source}:" in out
    assert "Agent claims (from the handoff: the agent's own account, not verified by this summary):" in out
    assert "NOT INDEPENDENTLY VERIFIED" not in out
    # The agent's validations are listed as what it reported, the supervisor's as what it ran.
    assert "Validations reported passed (3):" in out
    assert "    - PASS `git diff --check` (exit 0)" in out

    summary = summarize_json(capsys, tmp_path, make_handoff(), report)
    assert summary["verification"]["status"] == "independently_checked"
    assert summary["supervisor_evidence"]["independent"] is True
    assert summary["agent_claims"]["verified"] is False


def test_a_claimed_head_commit_is_labelled_a_claim_unless_the_evaluator_recorded_it(capsys, tmp_path):
    handoff = make_handoff(verified_commit_sha=HEAD)

    commits = summarize_json(capsys, tmp_path, handoff, None)["commits"]
    assert commits["head"] == {"sha": HEAD, "source": "agent claim (handoff verified_commit_sha), not verified"}
    assert commits["base"] == {"sha": None, "source": "not recorded: no supervisor report"}

    commits = summarize_json(capsys, tmp_path, handoff, make_decision_report())["commits"]
    assert commits["head"]["source"] == "agent claim (handoff verified_commit_sha), not verified"
    assert commits["base"] == {"sha": None, "source": "not recorded: the decision report has no commit identities"}

    commits = summarize_json(capsys, tmp_path, handoff, make_evaluation_report())["commits"]
    assert commits == {
        "base": {"sha": BASE, "source": "supervisor evaluation report"},
        "head": {"sha": HEAD, "source": "supervisor evaluation report"},
    }


# ---------------------------------------------------------------------------
# Next action
# ---------------------------------------------------------------------------

FAILING_REPLAY = policy.ValidationReplayResult("make test", False, 1, "Supervisor replay failed with exit code 1.")

NEXT_ACTION_CASES = [
    pytest.param(None, {}, "get_independent_evidence", "Not independently verified.", id="no-report"),
    pytest.param(make_decision_report(), {}, "queue_stop", "No action: queue stop.", id="stop"),
    pytest.param(
        make_decision_report(decision="continue", should_continue=True, next_slice_id="today-continue-ui-regression-coverage"),
        {},
        "human_review",
        "names `today-continue-ui-regression-coverage` as the eligible next slice",
        id="continue",
    ),
    pytest.param(
        make_decision_report(decision="stop_for_review", stop_reason="Consecutive autonomous run limit reached."),
        {},
        "human_review",
        "The supervisor stopped: Consecutive autonomous run limit reached.",
        id="stop-for-review",
    ),
    pytest.param(make_evaluation_report(dry_run=True), {}, "human_review", "was a dry run", id="accept-dry-run"),
    pytest.param(
        make_decision_report(queue_status="blocked", decision="stop_blocked", stop_reason="Slice reported blocked status."),
        {"status": "blocked", "open_questions": ["Which simulator runtime is approved?"]},
        "resolve_blocker",
        "The agent's open questions: `Which simulator runtime is approved?`",
        id="stop-blocked",
    ),
    pytest.param(
        make_decision_report(
            queue_status="failed",
            decision="stop_failed",
            required_validation_failures=["make test"],
            supervisor_validation_replays=[FAILING_REPLAY],
        ),
        {},
        "fix_validation",
        "Fix the failing validation: `make test`.",
        id="stop-failed-validation",
    ),
    pytest.param(
        # A replay that failed counts even when the report lists no required failure.
        make_evaluation_report(
            queue_status="failed",
            decision="reject",
            supervisor_validation_replays=[{"command": "git diff --check", "success": False, "exit_code": 2, "reason": "x"}],
        ),
        {},
        "fix_validation",
        "Fix the failing validation: `git diff --check`.",
        id="reject-replay",
    ),
    pytest.param(
        make_decision_report(queue_status="failed", decision="stop_failed", dirty_paths_outside_scope=["Makefile"]),
        {},
        "fix_scope",
        "outside the slice's allowed paths: `Makefile`.",
        id="stop-failed-scope",
    ),
    pytest.param(
        make_evaluation_report(queue_status="failed", decision="reject", out_of_scope_files=["a.txt", "b.txt"]),
        {},
        "fix_scope",
        "`a.txt`, `b.txt`",
        id="reject-scope",
    ),
    pytest.param(
        make_decision_report(queue_status="failed", decision="stop_failed", missing_manual_proofs=["screenshot"]),
        {},
        "provide_proof",
        "Provide the missing manual proof: `screenshot`.",
        id="missing-proof",
    ),
    pytest.param(
        make_evaluation_report(queue_status="failed", decision="reject", missing_proof_level=True),
        {},
        "provide_proof",
        "Reach the slice's required proof level.",
        id="missing-proof-level",
    ),
    pytest.param(
        run_next.make_failure_report(SLICE, "Agent command exited with code 3 for slice x.", 1, 1),
        {"status": "failed"},
        "investigate_failure",
        "Investigate the failure the supervisor reported: Agent command exited with code 3",
        id="failure-report",
    ),
]


@pytest.mark.parametrize(("report", "handoff_overrides", "kind", "phrase"), NEXT_ACTION_CASES)
def test_next_action_follows_the_report(capsys, tmp_path, report, handoff_overrides, kind, phrase):
    summary = summarize_json(capsys, tmp_path, make_handoff(**handoff_overrides), report)
    action = summary["next_action"]

    assert action["kind"] == kind
    assert phrase in action["text"]
    if kind not in ("get_independent_evidence", "queue_stop"):
        assert "A person" in action["text"]
    # Never an instruction to start the next slice, whatever the report decided.
    assert not re.search(r"\b(run|start|launch)\b[^.]*\bnext slice\b", action["text"], re.IGNORECASE)
    assert "automatically" not in action["text"].lower()

    argv = ["--handoff", str(tmp_path / "handoff.json")] + (["--report", str(tmp_path / "report.json")] if report else [])
    code, out, _ = summarize(capsys, *argv)
    assert code == 0
    assert out.rstrip("\n").splitlines()[-1] == f"Next action: {action['text']}"


def test_many_names_in_a_next_action_are_cut_with_a_count(capsys, tmp_path):
    failures = [f"make check-{index}" for index in range(8)]
    report = make_decision_report(queue_status="failed", decision="stop_failed", required_validation_failures=failures)

    text = summarize_json(capsys, tmp_path, make_handoff(), report)["next_action"]["text"]

    assert "`make check-4` (and 3 more)" in text
    assert "make check-5" not in text


# ---------------------------------------------------------------------------
# Bounded output
# ---------------------------------------------------------------------------


def test_long_lists_and_long_text_are_truncated_with_a_count(capsys, tmp_path):
    handoff = make_handoff(summary="s" * 2000, files_touched=[f"src/file_{index:03}.py" for index in range(50)])
    replays = [policy.ValidationReplayResult(f"make step-{index}", True, 0, "") for index in range(30)]
    report = make_decision_report(supervisor_validation_replays=replays)
    handoff_path = write(tmp_path, "handoff.json", handoff)
    report_path = write(tmp_path, "report.json", report)

    code, out, _ = summarize(capsys, "--handoff", str(handoff_path), "--report", str(report_path))

    assert code == 0
    assert "  Files touched (50):" in out
    assert "src/file_019.py" in out and "src/file_020.py" not in out
    assert "    - ... 30 more not shown" in out
    assert "  Validation replays (30):" in out
    assert "make step-19" in out and "make step-20" not in out
    assert "    - ... 10 more not shown" in out
    assert f"  Summary: {'s' * hs.MAX_TEXT_CHARS}... [1600 more characters]" in out
    assert len(out) < 10_000

    summary = summarize_json(capsys, tmp_path, handoff, report)
    assert summary["agent_claims"]["files_touched"]["total"] == 50
    assert len(summary["agent_claims"]["files_touched"]["items"]) == hs.MAX_LIST_ITEMS
    assert summary["supervisor_evidence"]["validation_replays"]["total"] == 30
    assert len(summary["supervisor_evidence"]["validation_replays"]["items"]) == hs.MAX_LIST_ITEMS
    assert summary["agent_claims"]["summary"].endswith("... [1600 more characters]")


# ---------------------------------------------------------------------------
# Control characters
# ---------------------------------------------------------------------------

HOSTILE = "\x1b[2J\x1b]0;owned\x07ok\r\u202egnp.exe\x9b31m\u2028"


def unsafe_characters(text: str) -> list[str]:
    return [char for char in text if char != "\n" and unicodedata.category(char) in hs.UNSAFE_CATEGORIES]


def test_agent_text_cannot_drive_the_reviewer_terminal(capsys, tmp_path):
    handoff = make_handoff(
        summary=HOSTILE,
        files_touched=[f"docs/{HOSTILE}.md"],
        open_questions=[HOSTILE],
        recommended_next_slice=HOSTILE,
        verified_commit_sha=HOSTILE,
    )
    report = make_decision_report(
        queue_status="blocked", decision="stop_blocked", stop_reason=HOSTILE, files_touched_outside_scope=[HOSTILE]
    )
    handoff_path = write(tmp_path, "handoff.json", handoff)
    report_path = write(tmp_path, "report.json", report)

    code, out, _ = summarize(capsys, "--handoff", str(handoff_path), "--report", str(report_path))

    assert code == 0
    assert unsafe_characters(out) == []
    assert "\\x1b[2J\\x1b]0;owned\\x07ok\\x0d\\u202egnp.exe\\x9b31m\\u2028" in out
    assert all(HOSTILE not in line for line in out.splitlines())

    code, out, _ = summarize(capsys, "--handoff", str(handoff_path), "--report", str(report_path), "--json")
    assert code == 0
    assert out.isascii()
    assert unsafe_characters(out) == []
    # JSON keeps the agent's text exact for tools; only its encoding is escaped.
    assert json.loads(out)["agent_claims"]["summary"] == HOSTILE


def test_a_lone_surrogate_is_refused_like_the_supervisor_refuses_it(capsys, tmp_path):
    handoff_path = write(tmp_path, "handoff.json", make_handoff(summary="placeholder"))
    text = handoff_path.read_text(encoding="utf-8").replace("placeholder", "bad \\ud800")
    handoff_path.write_text(text, encoding="utf-8")

    code, out, err = summarize(capsys, "--handoff", str(handoff_path))

    assert (code, out) == (hs.EX_INPUT, "")
    assert err.startswith("error: invalid handoff")
    assert unsafe_characters(err) == []


def test_a_path_that_is_not_utf8_is_printed_escaped(capsys, tmp_path):
    # Python passes undecodable bytes in a path argument on as lone surrogates. capsys writes
    # strict UTF-8, so printing one unescaped would raise instead of failing this assertion.
    odd = str(tmp_path / ("handoff-" + chr(0xDCFF) + ".json"))

    code, out, err = summarize(capsys, "--handoff", odd)

    assert (code, out) == (hs.EX_INPUT, "")
    assert "handoff-\\udcff.json" in err

    text = hs.render_text(hs.build_summary(make_handoff(), odd))
    assert unsafe_characters(text) == []
    assert "Handoff: " + str(tmp_path) + "/handoff-\\udcff.json" in text


def test_an_error_message_cannot_drive_the_terminal_either(capsys, tmp_path):
    report = make_decision_report()
    report["slice_id"] = HOSTILE
    report_path = write(tmp_path, "report.json", report)

    code, out, err = summarize(capsys, "--handoff", str(EXAMPLE_HANDOFF), "--report", str(report_path))

    assert (code, out) == (hs.EX_INPUT, "")
    assert err.startswith("error: the report is for slice")
    assert unsafe_characters(err) == []


# ---------------------------------------------------------------------------
# Invalid input
# ---------------------------------------------------------------------------


def _report_with(**changes: Any) -> dict[str, Any]:
    report = make_decision_report()
    report.update(changes)
    return report


INVALID_REPORTS = [
    pytest.param("[]", "expected a JSON object", id="not-an-object"),
    pytest.param(_report_with(decision="ship_it"), "decision", id="unknown-decision"),
    pytest.param(_report_with(verdict="fine"), "additional property not allowed", id="unknown-key"),
    pytest.param(
        _report_with(supervisor_validation_replays=[{"command": "make test", "success": "yes", "exit_code": 0, "reason": ""}]),
        "success",
        id="wrong-type",
    ),
    pytest.param(make_handoff(), "missing required property", id="a-handoff-passed-as-report"),
    pytest.param(_report_with(slice_id="another-slice"), "the report is for slice 'another-slice'", id="other-slice"),
]


@pytest.mark.parametrize(("payload", "message"), INVALID_REPORTS)
def test_an_invalid_report_is_refused(capsys, tmp_path, payload, message):
    report_path = write(tmp_path, "report.json", payload)

    code, out, err = summarize(capsys, "--handoff", str(EXAMPLE_HANDOFF), "--report", str(report_path))

    assert (code, out) == (hs.EX_INPUT, "")
    assert err.startswith("error: ")
    assert message in err


def test_an_invalid_handoff_is_refused(capsys, tmp_path):
    handoff = make_handoff()
    del handoff["summary"]
    handoff["status"] = "finished"

    code, out, err = summarize(capsys, "--handoff", str(write(tmp_path, "handoff.json", handoff)))

    assert (code, out) == (hs.EX_INPUT, "")
    assert err.startswith(f"error: invalid handoff {tmp_path / 'handoff.json'}:")
    assert "$: missing required property 'summary'" in err
    assert "$.status:" in err


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        pytest.param("missing.json", None, "handoff is not a readable file", id="missing"),
        pytest.param("adir", "dir", "handoff is not a readable file", id="directory"),
        pytest.param("broken.json", "{not json", "handoff is not valid JSON", id="not-json"),
        pytest.param("latin1.json", b"\xff\xfe{}", "handoff is not UTF-8 text", id="not-utf8"),
        pytest.param("list.json", "[]", "invalid handoff", id="not-an-object"),
    ],
)
def test_an_unreadable_handoff_is_refused(capsys, tmp_path, name, content, message):
    path = tmp_path / name
    if content == "dir":
        path.mkdir()
    elif isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content, encoding="utf-8")

    code, out, err = summarize(capsys, "--handoff", str(path))

    assert (code, out) == (hs.EX_INPUT, "")
    assert message in err


def test_an_oversized_input_is_refused_before_it_is_parsed(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "MAX_INPUT_BYTES", 64)

    code, out, err = summarize(capsys, "--handoff", str(EXAMPLE_HANDOFF))

    assert (code, out) == (hs.EX_INPUT, "")
    assert "handoff is larger than 64 bytes" in err


def test_the_handoff_is_required(capsys):
    with pytest.raises(SystemExit) as stop:
        hs.main([])
    assert stop.value.code == 2


# ---------------------------------------------------------------------------
# --json shape
# ---------------------------------------------------------------------------

AGENT_CLAIM_KEYS = {
    "source",
    "verified",
    "status",
    "summary",
    "files_touched",
    "validations_passed",
    "validations_failed",
    "proof_level",
    "missing_proof_levels",
    "provided_proofs",
    "contract_status_changes",
    "dirty_paths_outside_scope",
    "residual_risks",
    "open_questions",
    "recommended_next_slice",
    "recommended_next_reason",
    "verified_commit_sha",
    "repo_clean_status",
    "git_mirror_status",
    "timestamp",
}
EVIDENCE_KEYS = {
    "source",
    "independent",
    "queue_status",
    "decision",
    "reason",
    "changed_file_count",
    "changed_files",
    "files_touched_outside_scope",
    "dirty_paths_outside_scope",
    "out_of_scope_files",
    "required_validation_failures",
    "validation_replays",
    "missing_manual_proofs",
    "missing_proof_level",
    "next_slice_id",
    "completed_autonomous_runs",
    "autonomous_run_limit",
    "dry_run",
}


def test_json_shape(capsys, tmp_path):
    proof = {
        "proof_type": "screenshot",
        "path": "proofs/today.png",
        "commit_sha": HEAD,
        "created_at": "2026-04-21T15:30:00Z",
        "slice_id": SLICE_ID,
        "sha256": "c" * 64,
    }
    summary = summarize_json(capsys, tmp_path, make_handoff(provided_proofs=[proof]), make_evaluation_report())

    assert set(summary) == {
        "summary_version",
        "slice_id",
        "inputs",
        "verification",
        "supervisor_evidence",
        "agent_claims",
        "commits",
        "next_action",
    }
    assert summary["summary_version"] == 1
    assert summary["slice_id"] == SLICE_ID
    assert summary["inputs"] == {"handoff": str(tmp_path / "handoff.json"), "report": str(tmp_path / "report.json")}
    assert set(summary["verification"]) == {"status", "report_kind", "label"}
    assert summary["verification"]["report_kind"] == "evaluation_report"
    assert set(summary["agent_claims"]) == AGENT_CLAIM_KEYS
    assert set(summary["supervisor_evidence"]) == EVIDENCE_KEYS
    assert set(summary["commits"]) == {"base", "head"}
    assert set(summary["next_action"]) == {"kind", "text"}
    # Every list is bounded the same way, so a tool can always tell whether it saw everything.
    assert summary["agent_claims"]["files_touched"] == {"total": 2, "items": make_handoff()["files_touched"]}
    assert summary["agent_claims"]["provided_proofs"]["items"] == [{k: v for k, v in proof.items() if k != "slice_id"}]
    assert summary["supervisor_evidence"]["changed_files"] == {"total": 1, "items": ["docs/product/domains/today.md"]}
    assert summary["supervisor_evidence"]["validation_replays"]["items"] == [
        {"command": "git diff --check", "success": True, "exit_code": 0, "reason": ""}
    ]
    # Fields one report kind does not carry are null, not guessed.
    assert summary["supervisor_evidence"]["next_slice_id"] is None
    assert summary["supervisor_evidence"]["dirty_paths_outside_scope"] is None

    decision = summarize_json(capsys, tmp_path, make_handoff(), make_decision_report())["supervisor_evidence"]
    assert set(decision) == EVIDENCE_KEYS
    assert decision["changed_files"] is None and decision["out_of_scope_files"] is None and decision["dry_run"] is None
    assert decision["completed_autonomous_runs"] == 1


def test_the_report_models_match_what_the_supervisor_emits():
    """A field added to either report must be added here too, or the summary refuses it."""
    assert set(hs.DecisionReport.model_fields) == set(make_decision_report())
    assert set(hs.DecisionReport.model_fields) == set(run_next.make_failure_report(SLICE, "x", 0, 1))
    assert set(hs.EvaluationReport.model_fields) == set(make_evaluation_report())


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()
    }


def test_the_summary_writes_nothing_and_runs_no_subprocess(capsys, tmp_path, monkeypatch):
    write(tmp_path, "handoff.json", make_handoff(verified_commit_sha=HEAD))
    write(tmp_path, "decision.json", make_decision_report())
    write(tmp_path, "evaluation.json", make_evaluation_report(queue_status="failed", decision="reject"))
    (tmp_path / "queue").mkdir()
    write(tmp_path / "queue", "slices.json", {"version": 1})
    before = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("handoff_summary must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    for name in ("system", "fork", "posix_spawn", "posix_spawnp", "execv", "execvp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse)

    for report in (None, "decision.json", "evaluation.json"):
        for json_flag in ([], ["--json"]):
            argv = ["--handoff", "handoff.json"] + (["--report", report] if report else []) + json_flag
            code, out, _ = summarize(capsys, *argv)
            assert code == 0 and out

    assert _tree(tmp_path) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["decision.json", "evaluation.json", "handoff.json", "queue"]


def test_the_script_runs_from_any_directory(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "automation/supervisor/handoff_summary.py"),
            "--handoff",
            str(EXAMPLE_HANDOFF),
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["slice_id"] == SLICE_ID
