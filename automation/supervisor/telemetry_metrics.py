"""The catalogue of Boundroom's metrics, declared once (SPEC-006 RA-TEL-010).

Each instrument has a name, a kind, a UCUM unit, a description and the attribute keys it may
carry, each with a fixed vocabulary. A value outside the vocabulary is recorded as ``other`` and
a key outside the list is dropped, so the number of series stays bounded whatever a caller
passes. No id, path, slice name, command or free text is ever a metric attribute: those belong on
spans, which are per run.

Nothing here imports an SDK. ``telemetry.Telemetry.metric`` records through this catalogue, and
only when telemetry is on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

Kind = Literal["counter", "histogram"]

OTHER = "other"

ENTRIES = ("run_next", "evaluate_handoff", "sync", "benchmark")
# How the process ended, not the slice's verdict: ok is exit 0, failed any other exit code,
# refused a refusal the entry raised (the sync tool's), error an exception that escaped.
OUTCOMES = ("ok", "failed", "refused", "error")
# CompletionDecision.decision, run_next's own "failed", and evaluate_handoff's accept and reject.
DECISIONS = ("continue", "stop", "stop_for_review", "stop_blocked", "stop_failed", "failed", "accept", "reject")
# automation/schemas/models.py SliceStatus.
QUEUE_STATUSES = ("queued", "in_progress", "blocked", "deferred", "done", "failed")
# ok is exit 0; timeout is exit 124, timeout(1)'s code, which the benchmark's runner also uses,
# or the supervisor's own session timeout; error is an exception that stopped the session.
SESSION_OUTCOMES = ("ok", "nonzero", "timeout", "error")
# The runner setting's choices (BOUNDROOM_AGENT_RUNNER). auto is recorded as auto: Boundroom
# doesn't learn which runner run_agent.sh picked.
RUNNERS = ("auto", "claude", "codex", "hermes")
BOOLEANS = ("true", "false")
CHANGE_KINDS = ("synced", "removed")


@dataclass(frozen=True)
class Instrument:
    name: str
    kind: Kind
    unit: str  # UCUM: "1" for a count, "s" for seconds
    description: str
    attributes: Mapping[str, tuple[str, ...]]


CATALOGUE: tuple[Instrument, ...] = (
    Instrument(
        "boundroom.runs",
        "counter",
        "1",
        "Runs of an entry point, by how the process ended.",
        {"entry": ENTRIES, "outcome": OUTCOMES},
    ),
    Instrument(
        "boundroom.run.duration",
        "histogram",
        "s",
        "Wall-clock time of a run, from its root span's start to its end.",
        {"entry": ENTRIES, "outcome": OUTCOMES},
    ),
    Instrument(
        "boundroom.slice.decisions",
        "counter",
        "1",
        "Slice verdicts the supervisor reached, with the queue status it wrote.",
        {"decision": DECISIONS, "queue_status": QUEUE_STATUSES},
    ),
    Instrument(
        "boundroom.agent_session.duration",
        "histogram",
        "s",
        "Wall-clock time of one agent session, as the supervisor measured it.",
        {"entry": ENTRIES, "outcome": SESSION_OUTCOMES, "runner": RUNNERS},
    ),
    Instrument(
        "boundroom.validation.replays",
        "counter",
        "1",
        "Validation commands the supervisor replayed, by result.",
        {"entry": ENTRIES, "success": BOOLEANS},
    ),
    Instrument(
        "boundroom.sync.changes",
        "counter",
        "1",
        "Files the sync tool wrote into a consumer or removed from it.",
        {"kind": CHANGE_KINDS},
    ),
)
BY_NAME: dict[str, Instrument] = {instrument.name: instrument for instrument in CATALOGUE}


def bounded(instrument: Instrument, attributes: Mapping[str, Any]) -> dict[str, str]:
    """The attributes an instrument may carry: unknown keys dropped, unknown values ``other``."""
    result: dict[str, str] = {}
    for key, value in attributes.items():
        allowed = instrument.attributes.get(key)
        if allowed is None or value is None:
            continue
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        result[key] = text if text in allowed else OTHER
    return result


def session_outcome(exit_code: int) -> str:
    return "ok" if exit_code == 0 else "timeout" if exit_code == 124 else "nonzero"


def run_outcome(exit_code: int) -> str:
    return "ok" if exit_code == 0 else "failed"
