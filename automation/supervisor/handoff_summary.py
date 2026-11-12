"""Summarize one slice's handoff for a person reviewing it, without reading raw JSON (RA-PROD-105).

Usage::

    python3 automation/supervisor/handoff_summary.py \\
        --handoff automation/handoffs/<artifact>.json \\
        [--report <decision-or-evaluation-report>.json] \\
        [--json]

The two inputs carry different trust. The handoff is the agent's own account of its work.
The report is what the supervisor measured for itself: the decision report ``run_next.py``
prints, or the evaluation report ``evaluate_handoff.py`` prints. The summary keeps the two
apart and labels each, because a summary is not independent verification (SPEC-001):
without a report, nothing in it has been checked by anyone but the agent.

The command is read-only. It reads the files it is named, writes only to stdout and stderr,
runs no subprocess and never touches the queue, so it cannot start or advance any work. Its
safe next action is advice for a person; it never says to run the next slice.

Exit codes: 0 when a summary was printed, whatever the decision was; 2 when an input is
missing, unreadable or malformed.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.schemas.models import Handoff, NonEmptyStr, SliceStatus, StrList
from automation.supervisor import policy

SUMMARY_VERSION = 1
EX_INPUT = 2  # the code run_next.py uses for a stop on bad input

# Bounds on the summary's own size. A handoff is agent-written and a report can list every
# path a run touched, so without them a single input could flood the reviewer's terminal.
MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_LIST_ITEMS = 20
MAX_TEXT_CHARS = 400
MAX_NAMES_IN_ACTION = 5

# Characters a terminal acts on or a reader cannot see: C0/C1 controls (ANSI escapes start
# with ESC), format characters such as the bidirectional overrides used to disguise text,
# line/paragraph separators, and lone surrogates. The contract models refuse a surrogate in
# a document, but a path argument with undecodable bytes still arrives carrying one, and
# printing it unescaped would crash the summary rather than show it.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

DECISION_REPORT = "decision_report"
EVALUATION_REPORT = "evaluation_report"
REPORT_SOURCES = {
    DECISION_REPORT: "the supervisor's decision report (run_next.py)",
    EVALUATION_REPORT: "the supervisor's evaluation report (evaluate_handoff.py)",
}
EVALUATION_DECISIONS = frozenset({"accept", "reject"})


class InputError(Exception):
    """An input the summary cannot use. The message is shown to the reviewer as is."""


class ValidationReplay(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str
    success: bool
    exit_code: Optional[int]
    reason: str


# The two report shapes, matching run_next.make_decision_report / make_failure_report and
# evaluate_handoff.report_to_dict field for field. Unknown keys are refused rather than
# ignored: a field added to a report later could carry a blocker this summary would
# otherwise drop without saying so.
class _Report(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    slice_id: NonEmptyStr
    queue_status: SliceStatus
    reason: str
    changed_file_count: Annotated[int, Field(ge=0)]
    files_touched_outside_scope: StrList
    required_validation_failures: StrList
    supervisor_validation_replays: list[ValidationReplay]
    missing_manual_proofs: StrList
    missing_proof_level: bool


class DecisionReport(_Report):
    decision: Literal["continue", "stop", "stop_for_review", "stop_blocked", "stop_failed"]
    recommended_next_slice: str
    next_slice_id: Optional[str]
    dirty_paths_outside_scope: StrList
    completed_autonomous_runs: Annotated[int, Field(ge=0)]
    autonomous_run_limit: Annotated[int, Field(ge=0)]


class EvaluationReport(_Report):
    decision: Literal["accept", "reject"]
    changed_files: StrList
    out_of_scope_files: StrList
    head_sha: str
    base_sha: str
    dry_run: bool


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------


def read_json(path: Path, label: str) -> Any:
    # Only a regular file: a FIFO or device such as /dev/zero would block or never end.
    try:
        if not path.is_file():
            raise InputError(f"{label} is not a readable file: {path}")
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as error:
        raise InputError(f"cannot read {label} {path}: {error.strerror or error}") from error
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError(f"{label} is larger than {MAX_INPUT_BYTES} bytes: {path}")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise InputError(f"{label} is not UTF-8 text: {path}") from error
    except json.JSONDecodeError as error:
        raise InputError(f"{label} is not valid JSON: {path}: {error.msg} at line {error.lineno}") from error


def load_handoff(path: Path) -> dict[str, Any]:
    handoff = read_json(path, "handoff")
    validation = policy.validate_document(handoff, Handoff)
    if not validation.is_valid:
        raise InputError(f"invalid handoff {path}:\n- " + "\n- ".join(validation.errors))
    return handoff


def load_report(path: Path) -> tuple[str, dict[str, Any]]:
    report = read_json(path, "report")
    if not isinstance(report, dict):
        raise InputError(f"invalid report {path}: expected a JSON object")
    kind = EVALUATION_REPORT if report.get("decision") in EVALUATION_DECISIONS else DECISION_REPORT
    model = EvaluationReport if kind == EVALUATION_REPORT else DecisionReport
    validation = policy.validate_document(report, model)
    if not validation.is_valid:
        raise InputError(
            f"invalid report {path}: not a run_next.py decision report or evaluate_handoff.py evaluation report:\n- "
            + "\n- ".join(validation.errors)
        )
    return kind, report


# ---------------------------------------------------------------------------
# Bounding and escaping
# ---------------------------------------------------------------------------


def clip(value: str) -> str:
    """Shorten one string to the summary's bound, saying how much was cut."""
    if len(value) <= MAX_TEXT_CHARS:
        return value
    return f"{value[:MAX_TEXT_CHARS]}... [{len(value) - MAX_TEXT_CHARS} more characters]"


def bounded(items: Sequence[Any]) -> dict[str, Any]:
    """A list cut to the summary's bound, with its full length, so a cut is never silent."""
    kept = [clip(item) if isinstance(item, str) else item for item in items[:MAX_LIST_ITEMS]]
    return {"total": len(items), "items": kept}


def safe(value: str) -> str:
    """Render agent- or report-written text as inert data for a terminal.

    Every unsafe character becomes a visible escape (ESC is shown as ``\\x1b``), so the
    reviewer sees that it was there and the terminal never acts on it.
    """
    out: list[str] = []
    for char in value:
        if unicodedata.category(char) not in UNSAFE_CATEGORIES:
            out.append(char)
            continue
        point = ord(char)
        if point <= 0xFF:
            out.append(f"\\x{point:02x}")
        elif point <= 0xFFFF:
            out.append(f"\\u{point:04x}")
        else:
            out.append(f"\\U{point:08x}")
    return "".join(out)


def _names(values: Sequence[str]) -> str:
    shown = ", ".join(f"`{safe(clip(value))}`" for value in values[:MAX_NAMES_IN_ACTION])
    extra = len(values) - MAX_NAMES_IN_ACTION
    return f"{shown} (and {extra} more)" if extra > 0 else shown


# ---------------------------------------------------------------------------
# Building the summary
# ---------------------------------------------------------------------------


def _agent_claims(handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "handoff",
        # The agent's own account. Nothing in this section is checked by this summary.
        "verified": False,
        "status": handoff["status"],
        "summary": clip(handoff["summary"]),
        "files_touched": bounded(handoff["files_touched"]),
        "validations_passed": bounded(handoff["validations_passed"]),
        "validations_failed": bounded(handoff["validations_failed"]),
        "proof_level": handoff["proof_level"],
        "missing_proof_levels": bounded(handoff["missing_proof_levels"]),
        "provided_proofs": bounded(
            [
                {key: clip(proof[key]) for key in ("proof_type", "path", "commit_sha", "sha256", "created_at")}
                for proof in handoff.get("provided_proofs", [])
            ]
        ),
        "contract_status_changes": bounded(
            [
                {
                    "contract": clip(change["contract"]),
                    "before": clip(change["before"]),
                    "after": clip(change["after"]),
                    "proof": bounded(change["proof"]),
                }
                for change in handoff["contract_status_changes"]
            ]
        ),
        "dirty_paths_outside_scope": bounded(handoff["dirty_paths_outside_scope"]),
        "residual_risks": bounded(handoff["residual_risks"]),
        "open_questions": bounded(handoff.get("open_questions", [])),
        "recommended_next_slice": clip(handoff["recommended_next_slice"]),
        "recommended_next_reason": clip(handoff["recommended_next_reason"]),
        "verified_commit_sha": clip(handoff["verified_commit_sha"]) if handoff.get("verified_commit_sha") else None,
        "repo_clean_status": handoff["repo_clean_status"],
        "git_mirror_status": handoff["git_mirror_status"],
        "timestamp": clip(handoff["timestamp"]),
    }


def _supervisor_evidence(kind: str, report: dict[str, Any]) -> dict[str, Any]:
    is_evaluation = kind == EVALUATION_REPORT
    return {
        "source": kind,
        "independent": True,
        "queue_status": report["queue_status"],
        "decision": report["decision"],
        "reason": clip(report["reason"]),
        "changed_file_count": report["changed_file_count"],
        # Only the evaluation report lists the changed paths; the decision report counts them.
        "changed_files": bounded(report["changed_files"]) if is_evaluation else None,
        "files_touched_outside_scope": bounded(report["files_touched_outside_scope"]),
        "dirty_paths_outside_scope": None if is_evaluation else bounded(report["dirty_paths_outside_scope"]),
        "out_of_scope_files": bounded(report["out_of_scope_files"]) if is_evaluation else None,
        "required_validation_failures": bounded(report["required_validation_failures"]),
        "validation_replays": bounded(
            [
                {
                    "command": clip(replay["command"]),
                    "success": replay["success"],
                    "exit_code": replay["exit_code"],
                    "reason": clip(replay["reason"]),
                }
                for replay in report["supervisor_validation_replays"]
            ]
        ),
        "missing_manual_proofs": bounded(report["missing_manual_proofs"]),
        "missing_proof_level": report["missing_proof_level"],
        "next_slice_id": None if is_evaluation else report["next_slice_id"],
        "completed_autonomous_runs": None if is_evaluation else report["completed_autonomous_runs"],
        "autonomous_run_limit": None if is_evaluation else report["autonomous_run_limit"],
        "dry_run": report["dry_run"] if is_evaluation else None,
    }


def _commits(handoff: dict[str, Any], kind: Optional[str], report: Optional[dict[str, Any]]) -> dict[str, Any]:
    if kind == EVALUATION_REPORT and report is not None:
        source = "supervisor evaluation report"
        return {
            "base": {"sha": clip(report["base_sha"]), "source": source},
            "head": {"sha": clip(report["head_sha"]), "source": source},
        }
    # run_next's decision report does not record commit identities, so the base is unknown
    # and the head, if any, is only the agent's word.
    base_source = "not recorded: the decision report has no commit identities" if kind else "not recorded: no supervisor report"
    claimed_head = handoff.get("verified_commit_sha")
    head = (
        {"sha": clip(claimed_head), "source": "agent claim (handoff verified_commit_sha), not verified"}
        if claimed_head
        else {"sha": None, "source": "not recorded"}
    )
    return {"base": {"sha": None, "source": base_source}, "head": head}


def next_action(handoff: dict[str, Any], kind: Optional[str], report: Optional[dict[str, Any]]) -> dict[str, str]:
    """Derive one safe next step for a person from the supervisor's fields.

    Every branch ends with a person deciding. None of them tells anyone, person or agent,
    to run the next slice: continuation is the supervisor's decision under its own gates.
    """
    if report is None or kind is None:
        return {
            "kind": "get_independent_evidence",
            "text": (
                "Not independently verified. Get the supervisor's own evidence first: its decision report "
                "from run_next.py, or evaluate_handoff.py --dry-run for this slice. Then a person reviews it."
            ),
        }

    decision = report["decision"]
    reason = safe(clip(report["reason"]))
    if decision == "stop":
        return {
            "kind": "queue_stop",
            "text": (
                "No action: queue stop. The supervisor found no eligible queued slice after this one. "
                "A person reviews and approves this slice's changes."
            ),
        }
    if decision == "continue":
        next_slice = report["next_slice_id"]
        named = f" The supervisor's report names `{safe(clip(next_slice))}` as the eligible next slice." if next_slice else ""
        return {
            "kind": "human_review",
            "text": (
                f"A person reviews and approves this slice's changes.{named} Whether anything runs next is "
                "the supervisor's decision under its own gates; this summary starts nothing."
            ),
        }
    if decision == "stop_for_review":
        return {
            "kind": "human_review",
            "text": f"A person reviews and approves this slice's changes. The supervisor stopped: {reason}",
        }
    if decision == "accept":
        dry_run = " The evaluation was a dry run, so the queue was not changed." if report["dry_run"] else ""
        return {"kind": "human_review", "text": f"A person reviews and approves this slice's changes.{dry_run}"}
    if decision == "stop_blocked":
        questions = handoff.get("open_questions", [])
        asked = f" The agent's open questions: {_names(questions)}." if questions else ""
        return {
            "kind": "resolve_blocker",
            "text": f"A person resolves what blocked the slice, then decides whether to re-queue it.{asked}",
        }

    # stop_failed or reject: the most specific finding first, so the reviewer fixes the cause
    # rather than the symptom the later checks would report once it is gone.
    requeue = " A person decides whether to re-queue the slice."
    failing = sorted(
        set(report["required_validation_failures"])
        | {replay["command"] for replay in report["supervisor_validation_replays"] if not replay["success"]}
    )
    if failing:
        noun = "validation" if len(failing) == 1 else "validations"
        return {"kind": "fix_validation", "text": f"Fix the failing {noun}: {_names(failing)}.{requeue}"}
    outside = sorted(
        set(report["files_touched_outside_scope"])
        | set(report.get("dirty_paths_outside_scope", []))
        | set(report.get("out_of_scope_files", []))
    )
    if outside:
        return {
            "kind": "fix_scope",
            "text": f"Revert or move the changes outside the slice's allowed paths: {_names(outside)}.{requeue}",
        }
    if report["missing_manual_proofs"] or report["missing_proof_level"]:
        parts = []
        if report["missing_manual_proofs"]:
            parts.append(f"provide the missing manual proof: {_names(report['missing_manual_proofs'])}")
        if report["missing_proof_level"]:
            parts.append("reach the slice's required proof level")
        text = "; ".join(parts)
        return {"kind": "provide_proof", "text": f"{text[0].upper()}{text[1:]}.{requeue}"}
    return {"kind": "investigate_failure", "text": f"Investigate the failure the supervisor reported: {reason}{requeue}"}


def build_summary(
    handoff: dict[str, Any],
    handoff_path: str,
    report_kind: Optional[str] = None,
    report: Optional[dict[str, Any]] = None,
    report_path: Optional[str] = None,
) -> dict[str, Any]:
    if report is not None and report["slice_id"] != handoff["slice_id"]:
        # Pairing one slice's handoff with another's report would show evidence for work it is not about.
        raise InputError(
            f"the report is for slice '{clip(report['slice_id'])}' but the handoff is for slice '{clip(handoff['slice_id'])}'"
        )
    if report is None or report_kind is None:
        verification = {
            "status": "not_independently_verified",
            "report_kind": None,
            "label": "NOT INDEPENDENTLY VERIFIED: no supervisor report was given, so everything here is the agent's own claim.",
        }
    else:
        verification = {
            "status": "independently_checked",
            "report_kind": report_kind,
            "label": f"Independently checked by {REPORT_SOURCES[report_kind]}. The agent's claims are shown separately.",
        }
    return {
        "summary_version": SUMMARY_VERSION,
        "slice_id": clip(handoff["slice_id"]),
        "inputs": {"handoff": clip(handoff_path), "report": clip(report_path) if report_path else None},
        "verification": verification,
        "supervisor_evidence": _supervisor_evidence(report_kind, report) if report is not None and report_kind else None,
        "agent_claims": _agent_claims(handoff),
        "commits": _commits(handoff, report_kind, report),
        "next_action": next_action(handoff, report_kind, report),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _list_lines(title: str, values: dict[str, Any], empty: str = "none") -> list[str]:
    total = values["total"]
    if total == 0:
        return [f"  {title}: {empty}"]
    lines = [f"  {title} ({total}):"]
    lines.extend(f"    - {safe(item)}" for item in values["items"])
    if total > len(values["items"]):
        lines.append(f"    - ... {total - len(values['items'])} more not shown")
    return lines


def _replay_line(replay: dict[str, Any]) -> str:
    result = "PASS" if replay["success"] else "FAIL"
    exit_code = "no exit code" if replay["exit_code"] is None else f"exit {replay['exit_code']}"
    reason = f": {safe(replay['reason'])}" if replay["reason"] else ""
    return f"    - {result} `{safe(replay['command'])}` ({exit_code}){reason}"


def _proof_line(proof: dict[str, Any]) -> str:
    return (
        f"{proof['proof_type']} {proof['path']} at commit {proof['commit_sha']} "
        f"(sha256 {proof['sha256'][:12]}..., created {proof['created_at']})"
    )


def _change_line(change: dict[str, Any]) -> str:
    proofs = ", ".join(change["proof"]["items"]) or "none"
    if change["proof"]["total"] > len(change["proof"]["items"]):
        proofs += f", ... {change['proof']['total'] - len(change['proof']['items'])} more"
    return f"{change['contract']}: {change['before']} -> {change['after']} (proof: {proofs})"


def render_text(summary: dict[str, Any]) -> str:
    lines = [
        f"Handoff review: {safe(summary['slice_id'])}",
        f"Verification: {summary['verification']['label']}",
        f"Handoff: {safe(summary['inputs']['handoff'])}",
        f"Report: {safe(summary['inputs']['report']) if summary['inputs']['report'] else 'none'}",
        "",
    ]

    evidence = summary["supervisor_evidence"]
    if evidence is None:
        lines += ["Supervisor evidence: none. Nothing below has been independently verified.", ""]
    else:
        lines.append(f"Supervisor evidence (independent), from {REPORT_SOURCES[evidence['source']]}:")
        lines.append(f"  Decision: {evidence['decision']} (queue status: {evidence['queue_status']})")
        lines.append(f"  Reason: {safe(evidence['reason'])}")
        if evidence["changed_files"] is not None:
            lines += _list_lines("Changed files", evidence["changed_files"])
        else:
            lines.append(f"  Changed files: {evidence['changed_file_count']} (the decision report counts them, not lists them)")
        lines += _list_lines("Files touched outside scope", evidence["files_touched_outside_scope"])
        if evidence["dirty_paths_outside_scope"] is not None:
            lines += _list_lines("Dirty paths outside scope", evidence["dirty_paths_outside_scope"])
        if evidence["out_of_scope_files"] is not None:
            lines += _list_lines("Changed files outside scope", evidence["out_of_scope_files"])
        replays = evidence["validation_replays"]
        if replays["total"] == 0:
            lines.append("  Validation replays: none run")
        else:
            lines.append(f"  Validation replays ({replays['total']}):")
            lines.extend(_replay_line(replay) for replay in replays["items"])
            if replays["total"] > len(replays["items"]):
                lines.append(f"    - ... {replays['total'] - len(replays['items'])} more not shown")
        lines += _list_lines("Required validation failures", evidence["required_validation_failures"])
        lines += _list_lines("Missing manual proofs", evidence["missing_manual_proofs"])
        lines.append(f"  Required proof level: {'missing' if evidence['missing_proof_level'] else 'not reported missing'}")
        if evidence["source"] == DECISION_REPORT:
            next_slice = safe(evidence["next_slice_id"]) if evidence["next_slice_id"] else "none"
            lines.append(f"  Next slice the supervisor judged eligible: {next_slice}")
            lines.append(f"  Autonomous runs: {evidence['completed_autonomous_runs']} of {evidence['autonomous_run_limit']}")
        else:
            lines.append(f"  Dry run: {'yes, the queue was not changed' if evidence['dry_run'] else 'no'}")
        lines.append("")

    claims = summary["agent_claims"]
    lines.append("Agent claims (from the handoff: the agent's own account, not verified by this summary):")
    lines.append(f"  Status: {claims['status']}")
    lines.append(f"  Summary: {safe(claims['summary'])}")
    lines += _list_lines("Files touched", claims["files_touched"])
    lines += _list_lines("Validations reported passed", claims["validations_passed"])
    lines += _list_lines("Validations reported failed", claims["validations_failed"])
    lines.append(f"  Proof level: {claims['proof_level']}")
    lines += _list_lines("Missing proof levels", claims["missing_proof_levels"])
    proofs = claims["provided_proofs"]
    lines += _list_lines(
        "Evidence: provided proofs",
        {"total": proofs["total"], "items": [_proof_line(proof) for proof in proofs["items"]]},
    )
    changes = claims["contract_status_changes"]
    lines += _list_lines(
        "Evidence: contract status changes",
        {"total": changes["total"], "items": [_change_line(change) for change in changes["items"]]},
    )
    lines += _list_lines("Dirty paths outside scope (reported)", claims["dirty_paths_outside_scope"])
    lines += _list_lines("Residual risks", claims["residual_risks"])
    lines += _list_lines("Open questions", claims["open_questions"])
    recommended = safe(claims["recommended_next_slice"]) or "none"
    lines.append(f"  Recommended next slice (a recommendation, not a decision): {recommended}")
    if claims["recommended_next_reason"]:
        lines.append(f"    reason: {safe(claims['recommended_next_reason'])}")
    lines.append(
        f"  Verified commit (claimed): {safe(claims['verified_commit_sha']) if claims['verified_commit_sha'] else 'none'}"
    )
    lines.append(
        f"  Repository: {claims['repo_clean_status']}, mirror {claims['git_mirror_status']}, at {safe(claims['timestamp'])}"
    )
    lines.append("")

    commits = summary["commits"]
    lines.append("Commits:")
    for name in ("base", "head"):
        commit = commits[name]
        value = safe(commit["sha"]) if commit["sha"] else "unknown"
        lines.append(f"  {name.capitalize()}: {value} ({commit['source']})")
    lines.append("")

    lines.append(f"Next action: {summary['next_action']['text']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize a slice's handoff for review, keeping the agent's claims apart from the supervisor's evidence. Read-only."
        )
    )
    parser.add_argument("--handoff", required=True, help="Path to the handoff JSON artifact.")
    parser.add_argument(
        "--report",
        help="Path to the supervisor's report: run_next.py's decision report or evaluate_handoff.py's output, saved as JSON.",
    )
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON for tools.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        handoff = load_handoff(Path(args.handoff))
        report_kind, report = load_report(Path(args.report)) if args.report else (None, None)
        summary = build_summary(handoff, args.handoff, report_kind, report, args.report)
    except InputError as error:
        # The message can quote input text, so each line is escaped like any other input.
        print("error: " + "\n".join(safe(line) for line in str(error).split("\n")), file=sys.stderr)
        return EX_INPUT
    if args.json:
        # ensure_ascii escapes every control and non-ASCII character, so the JSON can carry the
        # agent's text unaltered for tools and still cannot drive the terminal it is printed to.
        print(json.dumps(summary, indent=2, ensure_ascii=True))
    else:
        print(render_text(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
