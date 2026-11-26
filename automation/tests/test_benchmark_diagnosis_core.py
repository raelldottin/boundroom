"""The session diagnosis's pure core: what a session's evidence can decide, with no files at all.

``gather_evidence`` (the shell, tested in ``test_benchmark_diagnosis``) reads one session's
leftovers into a ``SessionEvidence``; everything here builds that evidence directly and checks
the whole output - verdicts, explanations, missing-evidence lines in their order, the timeline,
container attribution at its window edges, phase matching and the operator summary.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Optional

from automation.benchmark import diagnosis

# 2026-10-12T12:56:44Z, the launch second of run 38248084765's plan session.
LAUNCH = 1791809804.0
STEM = "20261012T125644Z-42025"
RESEARCH_STEM = "20261012T122043Z-3852"  # launched 2026-10-12T12:20:43Z, LAUNCH - 2161
NEXT_STEM = "20261012T131444Z-50111"  # launched 2026-10-12T13:14:44Z, LAUNCH + 1080
UNAVAILABLE = {"available": False, "reason": "/h exists but Hermes never created state.db in it"}

# The verdicts' explanations, verbatim: the operator reads these, so they are pinned.
COMPLETED_WHY = "the phase exited 0 and owed no artifact it failed to leave"
UNAVAILABLE_WHY = "no session ledger and no event stream, so nothing here can say how far the session got"
NO_TURN_WHY = (
    "the ledger exists but records nothing: Hermes writes a session row at turn start, before the first model "
    "call, but logs and swallows a failed write - so either the turn never started or its persistence failed"
)
NO_CALL_WHY = (
    "the turn started but no model call ever completed; a provider that never answered and a model reasoning "
    "through one unfinished call leave the same evidence: the usage row is written only when a call completes, "
    "and neither transport reports reasoning in progress"
)
NO_TOOL_WHY = (
    "a model call completed but no tool call was recorded; Hermes persists a tool call only after the tool makes "
    "progress, so the model answering without tools and a requested tool stalling before that flush leave the "
    "same evidence"
)
NO_PHASE_WHY = "the session called tools; without its phase result there is no termination to judge"
NO_ARTIFACT_WHY = "the session exited on its own without leaving a valid artifact"


def ledger(
    *,
    sessions: tuple[dict[str, Any], ...] = (),
    messages: tuple[dict[str, Any], ...] = (),
    usage: tuple[dict[str, Any], ...] = (),
    has_activity: bool = True,
) -> dict[str, Any]:
    """An available ledger in ``read_ledger``'s shape."""
    return {
        "available": True,
        "sessions": list(sessions),
        "messages": list(messages),
        "usage": list(usage),
        "has_activity_description": has_activity,
    }


def evidence(**overrides: Any) -> diagnosis.SessionEvidence:
    """A oneshot session that left every source; each test overrides only what it is about."""
    fields: dict[str, Any] = {
        "stem": STEM,
        "controls_present": True,
        "transport": "oneshot",
        "slice_id": "task-1a2b:plan",
        "usage_report_present": True,
        "log_bytes": 120,
        "events": None,
        "ledger": ledger(),
        "sandbox_receipt": {"observed": True, "containers": []},
        "snapshot": None,
        "credential_pool": None,
    }
    fields.update(overrides)
    return diagnosis.SessionEvidence(**fields)


def phase(returncode: Optional[int], seconds: Any, artifact_valid: Optional[bool], name: str = "plan", **extra: Any) -> dict:
    return {"phase": name, "returncode": returncode, "seconds": seconds, "artifact_valid": artifact_valid, **extra}


def at(seconds: float) -> float:
    return LAUNCH + seconds


# A turn that called one tool and got its result: past every verdict that needs no phase result.
TOOL_CALL = {"role": "assistant", "timestamp": at(30), "has_tool_calls": 1}
TOOL_RESULT = {"role": "tool", "timestamp": at(40)}
ONE_TOOL_ROUND = ledger(
    sessions=({"started_at": at(3)},),
    usage=({"api_call_count": 2, "first_seen": at(20), "last_seen": at(70)},),
    messages=(TOOL_CALL, TOOL_RESULT),
)


class VerdictTests(unittest.TestCase):
    """One row per branch of the verdict, each the furthest stage its evidence proves."""

    def test_each_stage_of_evidence_reaches_its_own_verdict(self) -> None:
        cases: list[tuple[str, dict[str, Any], Optional[dict], str, str]] = [
            ("exit 0 with a valid artifact", {}, phase(0, 130.0, True), diagnosis.COMPLETED, COMPLETED_WHY),
            ("exit 0 owing no artifact", {}, phase(0, 130.0, None), diagnosis.COMPLETED, COMPLETED_WHY),
            ("no returncode recorded", {}, phase(None, 130.0, None), diagnosis.COMPLETED, COMPLETED_WHY),
            (
                "no ledger, no stream",
                {"ledger": UNAVAILABLE},
                phase(124, 1080.0, False),
                diagnosis.EVIDENCE_UNAVAILABLE,
                UNAVAILABLE_WHY,
            ),
            (
                "no ledger, an empty stream",
                {"ledger": UNAVAILABLE, "events": []},
                phase(124, 1080.0, False),
                diagnosis.EVIDENCE_UNAVAILABLE,
                UNAVAILABLE_WHY,
            ),
            ("an empty ledger", {}, phase(124, 1080.0, False), diagnosis.NO_TURN_START_RECORDED, NO_TURN_WHY),
            (
                "an empty ledger and an empty stream",
                {"events": []},
                phase(124, 1080.0, False),
                diagnosis.NO_TURN_START_RECORDED,
                NO_TURN_WHY,
            ),
            # Each of these starts the turn through exactly one signal.
            (
                "only a session row",
                {"ledger": ledger(sessions=({"started_at": at(3)},))},
                phase(124, 1080.0, False),
                diagnosis.NO_COMPLETED_MODEL_CALL,
                NO_CALL_WHY,
            ),
            (
                "only a usage row, of no calls",
                {"ledger": ledger(usage=({"api_call_count": 0},))},
                phase(124, 1080.0, False),
                diagnosis.NO_COMPLETED_MODEL_CALL,
                NO_CALL_WHY,
            ),
            (
                "only a user message",
                {"ledger": ledger(messages=({"role": "user", "timestamp": at(4)},))},
                phase(124, 1080.0, False),
                diagnosis.NO_COMPLETED_MODEL_CALL,
                NO_CALL_WHY,
            ),
            (
                "only a stream event",
                {"events": [{"type": "system"}]},
                phase(124, 1080.0, False),
                diagnosis.NO_COMPLETED_MODEL_CALL,
                NO_CALL_WHY,
            ),
            (
                "only a stream event, no ledger",
                {"ledger": UNAVAILABLE, "events": [{"type": "system"}]},
                phase(124, 1080.0, False),
                diagnosis.NO_COMPLETED_MODEL_CALL,
                NO_CALL_WHY,
            ),
            (
                "an answer without tools",
                {"ledger": ledger(messages=({"role": "assistant", "timestamp": at(9)},))},
                phase(124, 1080.0, False),
                diagnosis.NO_TOOL_CALL_RECORDED,
                NO_TOOL_WHY,
            ),
            (
                "a completed call and no message",
                {"ledger": ledger(usage=({"api_call_count": 1},))},
                phase(124, 1080.0, False),
                diagnosis.NO_TOOL_CALL_RECORDED,
                NO_TOOL_WHY,
            ),
            (
                "a streamed tool use with no result",
                {"events": [{"type": "tool_use"}]},
                phase(124, 1080.0, False),
                diagnosis.TOOL_CALL_UNANSWERED,
                "1 tool calls recorded, 0 results: no result was recorded for the last one",
            ),
            (
                "two tool calls, one result",
                {"ledger": ledger(messages=(TOOL_CALL, TOOL_RESULT, TOOL_CALL))},
                phase(124, 1080.0, False),
                diagnosis.TOOL_CALL_UNANSWERED,
                "2 tool calls recorded, 1 results: no result was recorded for the last one",
            ),
            ("tools answered, no phase result", {"ledger": ONE_TOOL_ROUND}, None, diagnosis.FAILED, NO_PHASE_WHY),
            (
                "tools answered, budget kill after the artifact",
                {"ledger": ONE_TOOL_ROUND},
                phase(124, 1080.0, True),
                diagnosis.ACTIVE_UNTIL_BUDGET,
                "tools ran and returned; the budget ended it (last recorded activity at 70.0s) after a valid artifact",
            ),
            (
                "tools answered, budget kill before the artifact",
                {"ledger": ONE_TOOL_ROUND},
                phase(124, 1080.0, False),
                diagnosis.ACTIVE_UNTIL_BUDGET,
                "tools ran and returned; the budget ended it (last recorded activity at 70.0s) before a valid artifact",
            ),
            (
                "tools answered, budget kill owing no artifact",
                {"ledger": ONE_TOOL_ROUND},
                phase(124, 1080.0, None),
                diagnosis.ACTIVE_UNTIL_BUDGET,
                "tools ran and returned; the budget ended it (last recorded activity at 70.0s) before a valid artifact",
            ),
            (
                "tools answered, exit 0 without the artifact",
                {"ledger": ONE_TOOL_ROUND},
                phase(0, 300.0, False),
                diagnosis.EXITED_WITHOUT_ARTIFACT,
                NO_ARTIFACT_WHY,
            ),
            (
                "tools answered, exit 3 with the artifact",
                {"ledger": ONE_TOOL_ROUND},
                phase(3, 300.0, True),
                diagnosis.FAILED,
                "exited 3",
            ),
            (
                "tools answered, exit 3 owing none",
                {"ledger": ONE_TOOL_ROUND},
                phase(3, 300.0, None),
                diagnosis.FAILED,
                "exited 3",
            ),
        ]
        for name, fields, result, verdict, explanation in cases:
            with self.subTest(name):
                record = diagnosis.diagnose(evidence(**fields), result)
                self.assertEqual((verdict, explanation), (record["verdict"], record["explanation"]))


class MissingEvidenceTests(unittest.TestCase):
    """Every absent source is named, in a fixed order, with the reason it is absent."""

    def test_each_absent_source_is_named_once_and_exactly(self) -> None:
        cases: list[tuple[str, dict[str, Any], Optional[dict], list[str]]] = [
            ("nothing missing", {}, None, []),
            (
                "no controls",
                {"controls_present": False},
                None,
                ["controls receipt: run_agent.sh never reached the Hermes launch"],
            ),
            ("no log", {"log_bytes": None}, None, ["session log: absent"]),
            (
                "an empty oneshot log",
                {"log_bytes": 0},
                None,
                ["session log: 0 bytes - --oneshot writes nothing until the turn returns"],
            ),
            ("an empty log of another transport", {"log_bytes": 0, "transport": None}, None, ["session log: 0 bytes"]),
            (
                "no oneshot usage report",
                {"usage_report_present": False},
                None,
                ["usage report: absent - --oneshot writes it only when the turn returns"],
            ),
            (
                "no usage report from stream-json",
                {"usage_report_present": False, "transport": "stream-json", "events": [{"type": "system"}]},
                None,
                ["usage report: absent - this transport writes none"],
            ),
            (
                "no stream from stream-json",
                {"transport": "stream-json"},
                None,
                ["event stream: absent although the transport was stream-json"],
            ),
            ("an empty stream", {"events": []}, None, ["event stream: empty - not even system/init was flushed"]),
            ("no ledger", {"ledger": UNAVAILABLE}, None, [f"session ledger: {UNAVAILABLE['reason']}"]),
            (
                "no witness receipt",
                {"sandbox_receipt": None},
                None,
                ["sandbox observation: no witness receipt, or no launch time to place it"],
            ),
            (
                "no launch time to place the receipt",
                {"stem": "not-a-stem"},
                None,
                ["sandbox observation: no witness receipt, or no launch time to place it"],
            ),
            (
                "a store older than the activity column",
                {"ledger": ledger(has_activity=False)},
                None,
                ["activity description: this state.db predates sessions.last_activity_description"],
            ),
            (
                "a failed snapshot",
                {"snapshot": {"captured_at": at(1080), "error": "OperationalError: locked"}},
                phase(124, 1080.0, False),
                ["activity snapshot: OperationalError: locked"],
            ),
            (
                "no snapshot before a budget kill",
                {},
                phase(124, 1080.0, False),
                ["activity snapshot: none taken before the budget kill, so SIGTERM may have cleared the label"],
            ),
            ("no snapshot, and no budget kill", {}, phase(1, 40.0, False), []),
            ("a good snapshot before a budget kill", {"snapshot": {"row": None}}, phase(124, 1080.0, False), []),
            (
                "every source missing, in order",
                {
                    "controls_present": False,
                    "transport": None,
                    "log_bytes": None,
                    "usage_report_present": False,
                    "events": [],
                    "ledger": {"available": False, "reason": "controls receipt names no hermes_home"},
                    "sandbox_receipt": None,
                },
                phase(124, 1080.0, False),
                [
                    "controls receipt: run_agent.sh never reached the Hermes launch",
                    "session log: absent",
                    "usage report: absent - this transport writes none",
                    "event stream: empty - not even system/init was flushed",
                    "session ledger: controls receipt names no hermes_home",
                    "sandbox observation: no witness receipt, or no launch time to place it",
                    "activity snapshot: none taken before the budget kill, so SIGTERM may have cleared the label",
                ],
            ),
            (
                "a killed oneshot on an old store, in order",
                {
                    "log_bytes": 0,
                    "usage_report_present": False,
                    "ledger": ledger(has_activity=False),
                    "snapshot": {"error": "FileNotFoundError: gone"},
                },
                phase(124, 1080.0, False),
                [
                    "session log: 0 bytes - --oneshot writes nothing until the turn returns",
                    "usage report: absent - --oneshot writes it only when the turn returns",
                    "activity description: this state.db predates sessions.last_activity_description",
                    "activity snapshot: FileNotFoundError: gone",
                ],
            ),
        ]
        for name, fields, result, missing in cases:
            with self.subTest(name):
                self.assertEqual(missing, diagnosis.diagnose(evidence(**fields), result)["missing_evidence"])


def receipt(*created_at: Any) -> dict[str, Any]:
    return {"observed": True, "containers": [{"task_id": "t", "container_id": "c", "created_at": c} for c in created_at]}


class ContainerWindowTests(unittest.TestCase):
    """A lane-wide witness's containers, credited only to the session whose window holds them."""

    def test_a_container_belongs_to_the_window_its_creation_falls_in(self) -> None:
        # Launched 12:56:44Z; a 100s phase with no grace ends at 12:58:24Z.
        cases: list[tuple[str, Any, Optional[float]]] = [
            ("a second before launch, inside the widened start", "2026-10-12T12:56:43Z", -1.0),
            ("two seconds before launch", "2026-10-12T12:56:42Z", None),
            ("exactly at the window's end", "2026-10-12T12:58:24Z", 100.0),
            ("a tenth past the window's end", "2026-10-12T12:58:24.1Z", None),
            ("Docker's nanoseconds, rounded to a tenth", "2026-10-12T12:57:30.06Z", 46.1),
            ("no zone, read as UTC", "2026-10-12T12:57:00", 16.0),
            ("an offset zone", "2026-10-12T14:57:10+02:00", 26.0),
            ("research's container", "2026-10-12T12:21:57.317312761Z", None),
            ("a number, not a timestamp", 1791809830, None),
            ("no timestamp", None, None),
            ("not a timestamp", "yesterday", None),
            ("an impossible date", "2026-13-45T12:00:00Z", None),
        ]
        for name, created_at, offset in cases:
            with self.subTest(name):
                record = diagnosis.diagnose(evidence(sandbox_receipt=receipt(created_at)), phase(124, 100.0, False))
                own = [{"task_id": "t", "container_id": "c", "created_at": created_at, "at_seconds": offset}]
                expected = (own, 0, offset) if offset is not None else ([], 1, None)
                self.assertEqual(
                    expected,
                    (
                        record["containers"],
                        record["containers_from_other_sessions"],
                        record["timeline_seconds"]["first_container"],
                    ),
                )

    def test_the_window_ends_at_the_phase_its_grace_or_the_next_launch(self) -> None:
        late = receipt("2026-10-12T12:58:27Z")  # 103s after launch
        cases: list[tuple[str, Optional[dict], Optional[float], Optional[float], int]] = [
            ("the phase's seconds alone", phase(124, 100.0, False), None, 100.0, 0),
            ("a zero grace", phase(124, 100.0, False, grace_seconds=0.0), None, 100.0, 0),
            ("the grace widens it", phase(124, 100.0, False, grace_seconds=5.0), None, 105.0, 1),
            ("no seconds recorded", phase(124, None, False), None, 0.0, 0),
            ("the phase wins over a given end", phase(124, 100.0, False), at(200), 100.0, 0),
        ]
        for name, result, window_end, terminated_at, owned in cases:
            with self.subTest(name):
                record = diagnosis.diagnose(evidence(sandbox_receipt=late), result, window_end)
                self.assertEqual(
                    (terminated_at, owned, 1 - owned),
                    (
                        record["terminated"]["at_seconds"],
                        len(record["containers"]),
                        record["containers_from_other_sessions"],
                    ),
                )

    def test_without_a_phase_the_given_end_or_nothing_bounds_the_window(self) -> None:
        late = receipt("2026-10-12T12:58:27Z", "2026-10-13T00:00:00Z")
        cases: list[tuple[str, Optional[float], int]] = [("next launch at 104s", at(104), 1), ("no end", None, 2)]
        for name, window_end, owned in cases:
            with self.subTest(name):
                record = diagnosis.diagnose(evidence(sandbox_receipt=late), None, window_end)
                self.assertEqual(
                    (None, owned, 2 - owned),
                    (record["terminated"], len(record["containers"]), record["containers_from_other_sessions"]),
                )

    def test_no_place_for_a_container_is_none_and_zero(self) -> None:
        cases: list[tuple[str, dict[str, Any], Optional[list], int]] = [
            ("a receipt with no container list", {"sandbox_receipt": {"observed": True, "containers": None}}, [], 0),
            ("no receipt", {"sandbox_receipt": None}, None, 0),
            ("a receipt that is not an object", {"sandbox_receipt": ["c"]}, None, 0),
            ("no launch time", {"stem": "not-a-stem", "sandbox_receipt": receipt("2026-10-12T12:57:00Z")}, None, 0),
        ]
        for name, fields, containers, foreign in cases:
            with self.subTest(name):
                record = diagnosis.diagnose(evidence(**fields), phase(124, 100.0, False))
                self.assertEqual((containers, foreign), (record["containers"], record["containers_from_other_sessions"]))


# One stream-json session that called a tool and was killed at the budget, every source present.
RICH_LEDGER = ledger(
    sessions=({"started_at": at(3.04), "last_activity_at": at(80), "end_reason": None, "api_call_count": 9},),
    usage=(
        {"api_call_count": 2, "reasoning_tokens": 900, "first_seen": at(20), "last_seen": at(70)},
        {"api_call_count": None, "reasoning_tokens": None, "first_seen": at(10), "last_seen": None},
    ),
    messages=(
        {"role": "user", "timestamp": at(4)},
        {"role": "assistant", "timestamp": at(30), "has_tool_calls": 1},
        {"role": "tool", "timestamp": at(40)},
        {"role": "assistant", "timestamp": at(60), "has_tool_calls": 0},
    ),
)
RICH_EVENTS = [
    {"type": "system", "timestamp": at(5) * 1000},
    {"type": "text", "timestamp": at(25) * 1000},
    {"type": "tool_use", "timestamp": at(28) * 1000},
    {"type": "tool_result", "timestamp": at(35) * 1000},
    {"type": "result", "timestamp": at(90) * 1000},
    {"type": "text"},
]


class TimelineTests(unittest.TestCase):
    def test_every_source_lands_on_the_launch_clock(self) -> None:
        record = diagnosis.diagnose(
            evidence(
                transport="stream-json", events=RICH_EVENTS, ledger=RICH_LEDGER, sandbox_receipt=receipt("2026-10-12T12:57:00Z")
            ),
            phase(124, 1080.0, True),
        )
        self.assertEqual(
            {
                "turn_started": 3.0,
                "first_stream_event": 5.0,
                "first_completed_model_call": 10.0,
                "last_completed_model_call": 70.0,
                "first_model_output": 25.0,
                "first_tool_call": 28.0,
                "first_tool_result": 35.0,
                "last_activity": 90.0,
                "first_container": 16.0,
            },
            record["timeline_seconds"],
        )

    def test_nothing_timestamped_is_none_throughout(self) -> None:
        record = diagnosis.diagnose(evidence(events=[{"type": "system"}]), None)
        self.assertEqual(dict.fromkeys(record["timeline_seconds"]), record["timeline_seconds"])

    def test_no_launch_time_places_nothing_on_the_clock(self) -> None:
        record = diagnosis.diagnose(
            evidence(stem="not-a-stem", events=RICH_EVENTS, ledger=RICH_LEDGER), phase(124, 1080.0, True), at(2000)
        )
        self.assertEqual(
            (None, None, None, dict.fromkeys(record["timeline_seconds"])),
            (record["launched_at"], record["pid"], record["terminated"]["at_seconds"], record["timeline_seconds"]),
        )


class ObservedTests(unittest.TestCase):
    def test_the_counts_of_a_rich_session(self) -> None:
        record = diagnosis.diagnose(evidence(transport="stream-json", events=RICH_EVENTS, ledger=RICH_LEDGER), None)
        self.assertEqual(
            {
                "session_rows": 1,
                "usage_rows": 2,
                "messages": 4,
                "completed_model_calls": 2,
                "reasoning_tokens": 900,
                "assistant_messages": 2,
                "tool_calls": 1,
                "tool_results": 1,
                "stream_events": 6,
                "stream_kinds": ["result", "system", "text", "tool_result", "tool_use"],
                "terminal_result": True,
                "end_reason": None,
            },
            record["observed"],
        )

    def test_each_count_from_the_source_that_holds_it(self) -> None:
        cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            (
                "calls from usage rows over session rows",
                {"ledger": ledger(sessions=({"api_call_count": 5},), usage=({"api_call_count": 3},))},
                {"completed_model_calls": 3, "reasoning_tokens": 0},
            ),
            (
                "calls from session rows without usage rows",
                {"ledger": ledger(sessions=({"api_call_count": 2}, {"api_call_count": None}))},
                {"completed_model_calls": 2, "reasoning_tokens": None},
            ),
            ("no rows at all", {}, {"completed_model_calls": 0, "reasoning_tokens": None}),
            (
                "the first end reason any row gives",
                {"ledger": ledger(sessions=({"end_reason": ""}, {"end_reason": "timeout"}, {"end_reason": "other"}))},
                {"end_reason": "timeout"},
            ),
            ("no stream", {}, {"stream_events": None, "stream_kinds": [], "terminal_result": False}),
            ("an empty stream", {"events": []}, {"stream_events": 0, "stream_kinds": [], "terminal_result": False}),
            (
                "a stream without a result, untyped events left out",
                {"events": [{"type": "system"}, {}, {"type": "text"}]},
                {"stream_events": 3, "stream_kinds": ["system", "text"], "terminal_result": False},
            ),
            (
                "more streamed tool events than persisted ones",
                {
                    "events": [{"type": "tool_use"}, {"type": "tool_result"}, {"type": "tool_use"}, {"type": "tool_result"}],
                    "ledger": ledger(messages=(TOOL_CALL, TOOL_RESULT)),
                },
                {"tool_calls": 2, "tool_results": 2},
            ),
        ]
        for name, fields, expected in cases:
            with self.subTest(name):
                observed = diagnosis.diagnose(evidence(**fields), None)["observed"]
                self.assertEqual(expected, {key: observed[key] for key in expected})


class HeaderTests(unittest.TestCase):
    def test_the_header_reads_the_stem_and_the_controls(self) -> None:
        cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            (
                "a parseable stem and a phased slice",
                {},
                {
                    "session": STEM,
                    "phase": "plan",
                    "pid": 42025,
                    "transport": "oneshot",
                    "launched_at": "2026-10-12T12:56:44+00:00",
                },
            ),
            (
                "an unparseable stem",
                {"stem": "not-a-stem"},
                {"session": "not-a-stem", "phase": "plan", "pid": None, "transport": "oneshot", "launched_at": None},
            ),
            (
                "a slice id with no phase",
                {"slice_id": "task-1a2b", "transport": None},
                {"session": STEM, "phase": None, "pid": 42025, "transport": None, "launched_at": "2026-10-12T12:56:44+00:00"},
            ),
        ]
        for name, fields, expected in cases:
            with self.subTest(name):
                record = diagnosis.diagnose(evidence(**fields), None)
                self.assertEqual(expected, {key: record[key] for key in expected})

    def test_termination_is_read_from_the_phase_result(self) -> None:
        cases: list[tuple[str, Optional[dict], Optional[dict]]] = [
            ("no phase result", None, None),
            (
                "a budget kill",
                phase(124, 1080.0, False),
                {"returncode": 124, "at_seconds": 1080.0, "by_budget": True, "artifact_valid": False},
            ),
            (
                "an exit of its own",
                phase(1, 40.5, None),
                {"returncode": 1, "at_seconds": 40.5, "by_budget": False, "artifact_valid": None},
            ),
        ]
        for name, result, terminated in cases:
            with self.subTest(name):
                self.assertEqual(terminated, diagnosis.diagnose(evidence(), result)["terminated"])

    def test_the_credential_pool_is_passed_through(self) -> None:
        pool = [{"provider": "nvidia", "source": "env:NVIDIA_API_KEY", "last_status": None, "last_error_code": None}]
        self.assertEqual(pool, diagnosis.diagnose(evidence(credential_pool=pool), None)["credential_pool"])


class PhaseOfTests(unittest.TestCase):
    def test_the_phase_is_the_last_colon_field(self) -> None:
        for slice_id, name in (
            ("task-1a2b:plan", "plan"),
            ("a:b:research", "research"),
            ("task-1a2b", None),
            ("", None),
            ("task:", ""),
        ):
            with self.subTest(slice_id=slice_id):
                self.assertEqual(name, diagnosis.phase_of(slice_id))


class LastPersistedActivityTests(unittest.TestCase):
    def test_the_snapshot_wins_else_the_latest_row(self) -> None:
        row = {
            "last_activity_at": at(1025),
            "last_activity_description": "receiving stream response",
            "last_activity_provenance": "unknown",
        }
        cleared = {"last_activity_at": at(1080), "last_activity_description": "", "last_activity_provenance": "unknown"}
        cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            (
                "the pre-SIGTERM snapshot over the cleared row",
                {"snapshot": {"captured_at": at(1080), "row": row}, "ledger": ledger(sessions=(cleared,))},
                {
                    "source": "pre_sigterm_snapshot",
                    "at_seconds": 1025.0,
                    "description": "receiving stream response",
                    "provenance": "unknown",
                    "state": "receiving_chunks",
                },
            ),
            (
                "a snapshot that found no row",
                {"snapshot": {"captured_at": at(1080), "row": None}, "ledger": ledger(sessions=(cleared,))},
                {
                    "source": "state_db_after_exit",
                    "at_seconds": 1080.0,
                    "description": "",
                    "provenance": "unknown",
                    "state": "cleared",
                },
            ),
            (
                "the row with the latest activity",
                {"ledger": ledger(sessions=(row, cleared, {"last_activity_at": None, "last_activity_description": "x"}))},
                {
                    "source": "state_db_after_exit",
                    "at_seconds": 1080.0,
                    "description": "",
                    "provenance": "unknown",
                    "state": "cleared",
                },
            ),
            (
                "an unstamped row over an earlier stamp",
                {
                    "ledger": ledger(
                        sessions=(
                            {"last_activity_at": None, "last_activity_description": "first"},
                            {"last_activity_at": 0.5, "last_activity_description": "second"},
                        )
                    )
                },
                {
                    "source": "state_db_after_exit",
                    "at_seconds": round(0.5 - LAUNCH, 1),
                    "description": "second",
                    "provenance": None,
                    "state": "other",
                },
            ),
            (
                "a description never persisted",
                {"ledger": ledger(sessions=({"last_activity_at": at(3), "last_activity_description": None},))},
                {
                    "source": "state_db_after_exit",
                    "at_seconds": 3.0,
                    "description": None,
                    "provenance": None,
                    "state": "never_persisted",
                },
            ),
            (
                "a store without the column",
                {"ledger": ledger(sessions=({"last_activity_at": at(3)},), has_activity=False)},
                {
                    "source": "state_db_after_exit",
                    "at_seconds": 3.0,
                    "description": None,
                    "provenance": None,
                    "state": "unavailable",
                },
            ),
            (
                "no row at all",
                {},
                {"source": None, "at_seconds": None, "description": None, "provenance": None, "state": "unavailable"},
            ),
        ]
        for name, fields, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, diagnosis.diagnose(evidence(**fields), None)["last_persisted_activity"])


class ClassifyActivityTests(unittest.TestCase):
    """The last persisted activity label, verbatim from pinned Hermes ``b6a87db``'s streaming path."""

    def test_each_label_names_its_state(self) -> None:
        cases = {
            "receiving_chunks": ("receiving stream response",),
            "awaiting_first_chunk": (
                "waiting for provider response (streaming)",
                "waiting for stream response (120s, first_chunk)",
                "⏳ waiting on z-ai/glm-5.3 — 60s waiting for the first stream chunk (auto-reconnect: stream stale watchdog in 120s)",
            ),
            "silent_after_chunks": (
                "waiting for stream response (90s, post_chunk)",
                "⏳ waiting on z-ai/glm-5.3 — stream open; 60s without stream output",
            ),
            "reconnecting": (
                "stale stream detected after 181s, reconnecting",
                "stream retry 2/5 after ReadTimeout",
                "⚠ no output from provider for 181s — reconnecting...",
            ),
            "retry_backoff": (
                "retry backoff (2/3), 30s remaining",
                "error retry backoff (1/3), 45s remaining",
                "empty response retry backoff (1/2), 12s remaining",
                "auto-recovery wait (1/4), 270s remaining",
                "API error recovery (attempt 2/3)",
                "⏳ waiting on provider — retrying in 60s (attempt 2/3)",
            ),
            "non_streaming": ("waiting for non-streaming API response", "stale non-streaming call killed after 300s"),
            # Hermes writes it under 60s since the last chunk or the stream's opening, so it settles nothing.
            "other": ("waiting for stream response (30s, no chunks yet)", "starting API call #1"),
        }
        for state, descriptions in cases.items():
            for description in descriptions:
                with self.subTest(description=description):
                    self.assertEqual(state, diagnosis.classify_activity(description))

    def test_a_description_never_persisted_is_not_one_that_was_cleared(self) -> None:
        """NULL: nothing was ever written. '': the turn's finally - SIGTERM's too - wiped it."""
        self.assertEqual(
            (diagnosis.ACTIVITY_NEVER_PERSISTED, diagnosis.ACTIVITY_CLEARED),
            (diagnosis.classify_activity(None), diagnosis.classify_activity("")),
        )


def judged(stem: str, slice_id: str, sandbox: Optional[dict] = None) -> diagnosis.SessionEvidence:
    return evidence(stem=stem, slice_id=slice_id, sandbox_receipt=sandbox or receipt())


class JudgeSessionsTests(unittest.TestCase):
    def test_sessions_are_matched_to_phases_by_slice_id_not_order(self) -> None:
        records = diagnosis.judge_sessions(
            [judged(RESEARCH_STEM, "task:research"), judged(STEM, "task:plan"), judged(NEXT_STEM, "task")],
            [phase(124, 1080.0, False), phase(0, 2160.0, True, name="research"), {"returncode": 3, "seconds": 9.0}],
        )
        self.assertEqual(
            [
                ("research", 2160.0, diagnosis.COMPLETED),
                ("plan", 1080.0, diagnosis.NO_TURN_START_RECORDED),
                (None, None, diagnosis.NO_TURN_START_RECORDED),
            ],
            [(r["phase"], (r["terminated"] or {}).get("at_seconds"), r["verdict"]) for r in records],
        )

    def test_a_session_with_no_phase_is_bounded_by_the_next_launch(self) -> None:
        # One witness copied into every receipt: a container 10s after each launch.
        lane = {
            "observed": True,
            "containers": [
                {"task_id": "t", "container_id": "research", "created_at": "2026-10-12T12:20:53Z"},
                {"task_id": "t", "container_id": "plan", "created_at": "2026-10-12T12:56:54Z"},
                {"task_id": "t", "container_id": "next", "created_at": "2026-10-12T13:14:54Z"},
            ],
        }
        records = diagnosis.judge_sessions(
            [judged(RESEARCH_STEM, "task:research", lane), judged(STEM, "task:plan", lane), judged(NEXT_STEM, "task:test", lane)],
            [],
        )
        self.assertEqual(
            [(["research"], 2), (["plan"], 2), (["next"], 2)],
            [([c["container_id"] for c in r["containers"]], r["containers_from_other_sessions"]) for r in records],
        )

    def test_sessions_handed_over_out_of_order_are_judged_in_launch_order(self) -> None:
        lane = {
            "observed": True,
            "containers": [
                {"task_id": "t", "container_id": "research", "created_at": "2026-10-12T12:20:53Z"},
                {"task_id": "t", "container_id": "plan", "created_at": "2026-10-12T12:56:54Z"},
            ],
        }
        in_order = [judged(RESEARCH_STEM, "task:research", lane), judged(STEM, "task:plan", lane)]
        self.assertEqual(
            diagnosis.judge_sessions(in_order, []),
            diagnosis.judge_sessions(list(reversed(in_order)), []),
        )

    def test_nothing_to_judge_is_an_empty_list(self) -> None:
        self.assertEqual([], diagnosis.judge_sessions([], [phase(0, 1.0, True)]))


class SummarizeTests(unittest.TestCase):
    def test_the_operator_lines_of_a_rich_session(self) -> None:
        record = diagnosis.diagnose(
            evidence(
                transport="stream-json",
                events=RICH_EVENTS,
                ledger=RICH_LEDGER,
                snapshot={
                    "captured_at": at(1080),
                    "row": {"last_activity_at": at(1025), "last_activity_description": "receiving stream response"},
                },
                sandbox_receipt=receipt("2026-10-12T12:57:00Z", "2026-10-12T12:21:57Z"),
            ),
            phase(124, 1080.0, True),
        )
        self.assertEqual(
            [
                f"session       {STEM} pid 42025 (plan, stream-json)",
                "verdict       active_until_budget: tools ran and returned; the budget ended it (last recorded activity at"
                " 90.0s) after a valid artifact",
                "timeline      turn_started 3.0s, first_stream_event 5.0s, first_completed_model_call 10.0s,"
                " last_completed_model_call 70.0s, first_model_output 25.0s, first_tool_call 28.0s,"
                " first_tool_result 35.0s, last_activity 90.0s, first_container 16.0s",
                "containers    1 in this session's window, 1 from other sessions",
                "last activity receiving_chunks at 1025.0s (pre_sigterm_snapshot): 'receiving stream response'",
            ],
            diagnosis.summarize(record),
        )

    def test_the_operator_lines_of_a_session_that_left_nothing(self) -> None:
        record = diagnosis.diagnose(
            evidence(
                stem="not-a-stem",
                slice_id="task",
                transport=None,
                controls_present=False,
                log_bytes=None,
                usage_report_present=False,
                ledger={"available": False, "reason": "controls receipt names no hermes_home"},
                sandbox_receipt=None,
            ),
            None,
        )
        self.assertEqual(
            [
                "session       not-a-stem pid None (unknown phase, None)",
                f"verdict       evidence_unavailable: {UNAVAILABLE_WHY}",
                "timeline      nothing timestamped",
                "containers    0 in this session's window, 0 from other sessions",
                "missing       controls receipt: run_agent.sh never reached the Hermes launch",
                "missing       session log: absent",
                "missing       usage report: absent - this transport writes none",
                "missing       session ledger: controls receipt names no hermes_home",
                "missing       sandbox observation: no witness receipt, or no launch time to place it",
            ],
            diagnosis.summarize(record),
        )


class GoldenRecordTests(unittest.TestCase):
    """Whole records, serialised the way run.json carries them: key order and every string pinned."""

    def test_the_plan_session_of_run_38248084765(self) -> None:
        record = diagnosis.diagnose(
            evidence(
                log_bytes=0,
                usage_report_present=False,
                ledger={"available": False, "reason": "/runner/home exists but Hermes never created state.db in it"},
                sandbox_receipt=receipt("2026-10-12T12:21:57.317312761Z"),
            ),
            phase(124, 1080.0, False),
        )
        self.assertEqual(GOLDEN_PLAN, json.dumps(record, indent=2))

    def test_a_stream_json_session_active_until_its_budget(self) -> None:
        pool = [{"provider": "nvidia", "source": "env:NVIDIA_API_KEY", "last_status": "exhausted", "last_error_code": 429}]
        record = diagnosis.diagnose(
            evidence(
                transport="stream-json",
                events=RICH_EVENTS,
                ledger=RICH_LEDGER,
                sandbox_receipt=receipt("2026-10-12T12:57:00Z"),
                credential_pool=pool,
            ),
            phase(124, 1080.0, True, grace_seconds=2.5),
        )
        self.assertEqual(GOLDEN_ACTIVE, json.dumps(record, indent=2))


GOLDEN_PLAN = """{
  "session": "20261012T125644Z-42025",
  "phase": "plan",
  "pid": 42025,
  "transport": "oneshot",
  "launched_at": "2026-10-12T12:56:44+00:00",
  "terminated": {
    "returncode": 124,
    "at_seconds": 1080.0,
    "by_budget": true,
    "artifact_valid": false
  },
  "timeline_seconds": {
    "turn_started": null,
    "first_stream_event": null,
    "first_completed_model_call": null,
    "last_completed_model_call": null,
    "first_model_output": null,
    "first_tool_call": null,
    "first_tool_result": null,
    "last_activity": null,
    "first_container": null
  },
  "observed": {
    "session_rows": 0,
    "usage_rows": 0,
    "messages": 0,
    "completed_model_calls": 0,
    "reasoning_tokens": null,
    "assistant_messages": 0,
    "tool_calls": 0,
    "tool_results": 0,
    "stream_events": null,
    "stream_kinds": [],
    "terminal_result": false,
    "end_reason": null
  },
  "credential_pool": null,
  "last_persisted_activity": {
    "source": null,
    "at_seconds": null,
    "description": null,
    "provenance": null,
    "state": "unavailable"
  },
  "containers": [],
  "containers_from_other_sessions": 1,
  "missing_evidence": [
    "session log: 0 bytes - --oneshot writes nothing until the turn returns",
    "usage report: absent - --oneshot writes it only when the turn returns",
    "session ledger: /runner/home exists but Hermes never created state.db in it",
    "activity snapshot: none taken before the budget kill, so SIGTERM may have cleared the label"
  ],
  "verdict": "evidence_unavailable",
  "explanation": "no session ledger and no event stream, so nothing here can say how far the session got"
}"""
GOLDEN_ACTIVE = """{
  "session": "20261012T125644Z-42025",
  "phase": "plan",
  "pid": 42025,
  "transport": "stream-json",
  "launched_at": "2026-10-12T12:56:44+00:00",
  "terminated": {
    "returncode": 124,
    "at_seconds": 1082.5,
    "by_budget": true,
    "artifact_valid": true
  },
  "timeline_seconds": {
    "turn_started": 3.0,
    "first_stream_event": 5.0,
    "first_completed_model_call": 10.0,
    "last_completed_model_call": 70.0,
    "first_model_output": 25.0,
    "first_tool_call": 28.0,
    "first_tool_result": 35.0,
    "last_activity": 90.0,
    "first_container": 16.0
  },
  "observed": {
    "session_rows": 1,
    "usage_rows": 2,
    "messages": 4,
    "completed_model_calls": 2,
    "reasoning_tokens": 900,
    "assistant_messages": 2,
    "tool_calls": 1,
    "tool_results": 1,
    "stream_events": 6,
    "stream_kinds": [
      "result",
      "system",
      "text",
      "tool_result",
      "tool_use"
    ],
    "terminal_result": true,
    "end_reason": null
  },
  "credential_pool": [
    {
      "provider": "nvidia",
      "source": "env:NVIDIA_API_KEY",
      "last_status": "exhausted",
      "last_error_code": 429
    }
  ],
  "last_persisted_activity": {
    "source": "state_db_after_exit",
    "at_seconds": 80.0,
    "description": null,
    "provenance": null,
    "state": "unavailable"
  },
  "containers": [
    {
      "task_id": "t",
      "container_id": "c",
      "created_at": "2026-10-12T12:57:00Z",
      "at_seconds": 16.0
    }
  ],
  "containers_from_other_sessions": 0,
  "missing_evidence": [
    "activity snapshot: none taken before the budget kill, so SIGTERM may have cleared the label"
  ],
  "verdict": "active_until_budget",
  "explanation": "tools ran and returned; the budget ended it (last recorded activity at 90.0s) after a valid artifact"
}"""


if __name__ == "__main__":
    unittest.main()
