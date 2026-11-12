"""Acceptance tests for inbound Slack delivery over Socket Mode (SPEC-007 part 2, RA-KNOW-013).

Everything runs on a fake clock against fake connections. Nothing here waits, opens a socket or
calls Slack.

The payloads under ``fixtures/slack/`` are synthetic: written by hand in the shapes Slack
documents for Socket Mode, with made-up ids and no real token or message. They are not recordings,
so they can't settle the spec's three open assumptions (the acknowledgement deadline, whether a
redelivery keeps its ``envelope_id``, and the tier of ``apps.connections.open``). The receiver is
built and tested to hold under either answer instead.
"""

from __future__ import annotations

import ast
import json
import socket
from collections import deque
from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from automation.knowledge import slack_inbound as si
from automation.knowledge.slack_inbound import (
    ACK_DEADLINE_SECONDS,
    DEDUPE_WINDOW_SECONDS,
    SLACK_ACK_LIMIT_SECONDS,
    SLACK_MAX_CONNECTIONS,
    SOCKET_CONNECTIONS,
    STALE_QUESTION_SECONDS,
    ConnectionClosed,
    ConnectionOpenFailed,
    DedupeLedger,
    Gap,
    InboundError,
    InboundWork,
    SocketModeDisabled,
    SocketModeReceiver,
    reconnect_ceiling,
)

MODULE = Path(si.__file__)
FIXTURES = Path(__file__).parent / "fixtures" / "slack"
TEAM = "T0SYNTH0001"
START = 1794441600.0  # 2026-11-12T00:00:00Z, the fixtures' event_time
MINUTE = 60.0
HOUR = 3600.0


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def frame(data: dict[str, Any]) -> str:
    return json.dumps(data)


def mention(event_id: str = "Ev0SYNTH0001", envelope: str = "env-1", *, event_time: float = START, retry: int = 0) -> str:
    data = fixture("events_api_app_mention")
    data["envelope_id"] = envelope
    data["retry_attempt"] = retry
    data["payload"]["event_id"] = event_id
    data["payload"]["event_time"] = int(event_time)
    return frame(data)


def deleted(event_id: str = "Ev0SYNTH0002", envelope: str = "env-del", *, event_time: float = START) -> str:
    data = fixture("events_api_message_deleted")
    data["envelope_id"] = envelope
    data["payload"]["event_id"] = event_id
    data["payload"]["event_time"] = int(event_time)
    return frame(data)


def command(envelope: str = "env-cmd", *, text: Optional[str] = None, team: str = TEAM) -> str:
    data = fixture("slash_command")
    data["envelope_id"] = envelope
    data["payload"]["team_id"] = team
    if text is not None:
        data["payload"]["text"] = text
    return frame(data)


def rate_limited(minute: float, envelope: str = "env-rl", team: str = TEAM) -> str:
    data = fixture("app_rate_limited")
    data["envelope_id"] = envelope
    data["payload"]["minute_rate_limited"] = int(minute)
    data["payload"]["team_id"] = team
    return frame(data)


def disconnect(reason: str) -> str:
    return frame(fixture(f"disconnect_{reason}"))


class FakeClock:
    """Wall-clock time that moves only when a test advances it."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeConnection:
    """One Socket Mode connection. A test queues frames on it and closes it, as Slack would."""

    def __init__(self, name: str, journal: list[tuple[str, str]], clock: FakeClock) -> None:
        self.name = name
        self.journal = journal
        self.clock = clock
        self.inbox: deque[str] = deque()
        self.acks: list[tuple[float, str]] = []
        self.closed = False
        # Slack closes it once the queued frames have been read.
        self.close_when_drained = False
        self.refuse_sends = False

    def receive(self) -> Optional[str]:
        if self.closed:
            raise ConnectionClosed(self.name)
        if self.inbox:
            raw = self.inbox.popleft()
            self.journal.append(("read", self.name))
            return raw
        if self.close_when_drained:
            self.closed = True
            raise ConnectionClosed(self.name)
        return None

    def send(self, text: str) -> None:
        if self.closed or self.refuse_sends:
            self.closed = True
            raise ConnectionClosed(self.name)
        envelope_id = json.loads(text)["envelope_id"]
        self.acks.append((self.clock.now, envelope_id))
        self.journal.append(("ack", envelope_id))

    def close(self) -> None:
        self.closed = True

    def push(self, *raws: str) -> FakeConnection:
        self.inbox.extend(raws)
        return self


class FakeSocketMode:
    """The connection factory: each call opens a new connection that greets with ``hello``."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.journal: list[tuple[str, str]] = []
        self.opened: list[FakeConnection] = []
        self.open_times: list[float] = []
        self.fail_next = 0

    def __call__(self) -> FakeConnection:
        self.open_times.append(self.clock.now)
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionOpenFailed("apps.connections.open answered with an error")
        connection = FakeConnection(f"conn-{len(self.opened) + 1}", self.journal, self.clock)
        connection.push(frame(fixture("hello")))
        self.opened.append(connection)
        self.journal.append(("open", connection.name))
        return connection

    @property
    def open(self) -> list[FakeConnection]:
        return [connection for connection in self.opened if not connection.closed]

    def acked(self) -> list[str]:
        return [envelope for kind, envelope in self.journal if kind == "ack"]


def make(*, jitter: float = 0.5, start: bool = True) -> tuple[SocketModeReceiver, FakeSocketMode, FakeClock]:
    clock = FakeClock()
    slack = FakeSocketMode(clock)
    receiver = SocketModeReceiver(TEAM, slack, clock=clock, random=lambda: jitter)
    if start:
        receiver.start()
        receiver.poll()
    return receiver, slack, clock


def drain(receiver: SocketModeReceiver) -> list[InboundWork]:
    items = []
    while (item := receiver.next_work()) is not None:
        items.append(item)
    return items


# The spec's numbers.


def test_timers_match_the_spec() -> None:
    assert ACK_DEADLINE_SECONDS == 1.0
    assert SLACK_ACK_LIMIT_SECONDS - ACK_DEADLINE_SECONDS == 2.0
    assert DEDUPE_WINDOW_SECONDS == 25 * HOUR
    assert STALE_QUESTION_SECONDS == 10 * MINUTE
    assert SOCKET_CONNECTIONS == 2 and SLACK_MAX_CONNECTIONS == 10
    assert [reconnect_ceiling(attempt) for attempt in range(7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_every_fixture_is_valid_json_with_no_real_token() -> None:
    names = sorted(path.stem for path in FIXTURES.glob("*.json"))
    assert names == [
        "app_rate_limited",
        "disconnect_link_disabled",
        "disconnect_refresh_requested",
        "disconnect_warning",
        "events_api_app_mention",
        "events_api_message_deleted",
        "hello",
        "slash_command",
    ]
    for name in names:
        text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        json.loads(text)
        assert "xoxb-" not in text and "xapp-" not in text and "xoxp-" not in text


# Acknowledge first, then process from the local queue.


def test_acknowledgement_precedes_processing_and_the_next_read() -> None:
    receiver, slack, clock = make()
    connection = slack.opened[0]
    connection.push(mention("Ev1", "env-1"), command("env-2"), deleted("Ev3", "env-3"))
    slack.journal.clear()

    assert receiver.poll() == 3
    # Each frame is acknowledged before the next one is read, and nothing is processed during the poll.
    assert slack.journal == [
        ("read", "conn-1"),
        ("ack", "env-1"),
        ("read", "conn-1"),
        ("ack", "env-2"),
        ("read", "conn-1"),
        ("ack", "env-3"),
    ]
    assert receiver.pending == 3

    clock.advance(5.0)
    items = drain(receiver)
    assert [item.envelope_id for item in items] == ["env-1", "env-2", "env-3"]
    for item in items:
        assert item.acked_at - item.received_at <= ACK_DEADLINE_SECONDS
        assert item.acked_at <= START  # acknowledged before the 5 seconds the caller took to start processing


def test_the_queue_carries_what_processing_needs_without_the_verification_token() -> None:
    receiver, slack, _ = make()
    slack.opened[0].push(frame(fixture("events_api_app_mention")), frame(fixture("slash_command")))
    receiver.poll()
    event, slash = drain(receiver)

    assert (event.source, event.kind, event.question, event.team) == ("events_api", "app_mention", True, TEAM)
    assert event.payload["event"]["text"].endswith("synthetic question about the release process")
    assert event.origin_at == START
    assert (slash.source, slash.kind, slash.question) == ("slash_commands", "/ask", True)
    assert slash.origin_at == slash.received_at
    for item in (event, slash):
        assert "token" not in item.payload
    with pytest.raises(ValidationError):
        InboundWork.model_validate({**event.model_dump(), "unexpected": 1})


@pytest.mark.parametrize(
    "raw",
    [
        frame({"envelope_id": "env-x", "type": "interactive", "payload": {"type": "block_actions"}}),
        frame({"envelope_id": "env-x", "type": "events_api", "payload": {"type": "url_verification"}}),
        frame({"envelope_id": "env-x", "type": "events_api"}),
        frame({"envelope_id": "env-x", "type": "events_api", "payload": {"type": "event_callback", "team_id": TEAM}}),
        frame({"envelope_id": "env-x", "type": "events_api", "payload": {"type": "app_rate_limited", "team_id": TEAM}}),
        frame({"envelope_id": "env-x", "type": "slash_commands", "payload": {"team_id": TEAM}}),
        command("env-x", team="T0OTHER0001"),
        mention(envelope="env-x").replace(TEAM, "T0OTHER0001"),
        rate_limited(START, "env-x", team="T0OTHER0001"),
    ],
)
def test_every_payload_with_an_envelope_is_acknowledged_even_when_dropped(raw: str) -> None:
    receiver, slack, _ = make()
    slack.opened[0].push(raw)
    receiver.poll()
    assert slack.acked() == ["env-x"]
    assert receiver.next_work() is None
    assert not receiver.gap_ledger.gaps


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", frame({"type": "events_api"}), frame({"envelope_id": "", "type": "x"})])
def test_frames_that_cannot_be_acknowledged_are_dropped_and_counted(raw: str) -> None:
    receiver, slack, _ = make()
    slack.opened[0].push(raw, mention())
    receiver.poll()
    assert receiver.stats.malformed == 1
    assert slack.acked() == ["env-1"]
    assert receiver.pending == 1


def test_receiving_needs_a_start_and_a_stopped_receiver_stays_stopped() -> None:
    receiver, slack, _ = make(start=False)
    with pytest.raises(InboundError, match="start"):
        receiver.poll()
    receiver.start()
    receiver.stop()
    assert receiver.open_connections == 0 and not slack.open
    assert receiver.poll() == 0 and len(slack.opened) == 2
    with pytest.raises(InboundError, match="stopped"):
        receiver.start()
    with pytest.raises(ValueError):
        SocketModeReceiver("", slack)


# Deduplication.


@pytest.mark.parametrize("new_envelope", [False, True], ids=["same-envelope", "new-envelope"])
def test_duplicates_at_0s_1m_5m_and_24h_have_one_effect(new_envelope: bool) -> None:
    receiver, slack, clock = make()
    connection = slack.opened[0]
    delays = [0.0, 1.0, MINUTE, 5 * MINUTE, 24 * HOUR]
    previous = 0.0
    for attempt, delay in enumerate(delays):
        clock.advance(delay - previous)
        previous = delay
        envelope = f"env-{attempt}" if new_envelope else "env-0"
        # A deletion, so the 24-hour copy isn't also dropped as a stale question.
        connection.push(deleted("Ev0DUP", envelope))
        receiver.poll()

    assert len(slack.acked()) == len(delays)  # every copy is acknowledged
    assert len(drain(receiver)) == 1
    assert receiver.stats.duplicates == len(delays) - 1


def test_the_window_is_25_hours_from_first_sight() -> None:
    receiver, slack, clock = make()
    connection = slack.opened[0]
    connection.push(deleted("Ev0DUP", "env-a"))
    receiver.poll()
    clock.advance(DEDUPE_WINDOW_SECONDS)
    connection.push(deleted("Ev0DUP", "env-b"))
    receiver.poll()
    assert receiver.stats.duplicates == 1
    clock.advance(1.0)
    connection.push(deleted("Ev0DUP", "env-c"))
    receiver.poll()
    assert len(drain(receiver)) == 2


def test_a_slash_command_redelivered_under_any_envelope_in_the_same_minute_has_one_effect() -> None:
    receiver, slack, clock = make()
    connection = slack.opened[0]
    connection.push(command("env-1"), command("env-1"), command("env-2"))
    receiver.poll()
    assert len(drain(receiver)) == 1

    # The documented cost: the same command repeated in a later minute counts as a new question.
    clock.advance(MINUTE)
    connection.push(command("env-3"))
    # A different question in the same minute is its own question.
    connection.push(command("env-4", text="a different synthetic question"))
    receiver.poll()
    assert len(drain(receiver)) == 2
    assert receiver.stats.duplicates == 2


def test_the_ledger_keeps_only_hashes_and_forgets_after_25_hours() -> None:
    clock = FakeClock()
    ledger = DedupeLedger(clock)
    assert not ledger.check_and_remember([("event", "Ev0SECRET"), ("envelope", "env-secret")])
    assert ledger.check_and_remember([("event", "Ev0SECRET")])
    assert ledger.check_and_remember([("event", "Ev0OTHER"), ("envelope", "env-secret")])
    stored = ledger.stored()
    assert len(stored) == 3
    assert all(len(digest) == 64 and set(digest) <= set("0123456789abcdef") for digest in stored)
    assert not any("SECRET" in digest or "secret" in digest for digest in stored)

    clock.advance(DEDUPE_WINDOW_SECONDS + 1.0)
    assert len(ledger) == 0
    assert not ledger.check_and_remember([("event", "Ev0SECRET")])


def test_a_ledger_handed_to_the_next_receiver_still_drops_redeliveries() -> None:
    clock = FakeClock()
    ledger = DedupeLedger(clock)
    receivers = []
    for envelope in ("env-first", "env-redelivered"):
        slack = FakeSocketMode(clock)
        receiver = SocketModeReceiver(TEAM, slack, clock=clock, random=lambda: 0.0, ledger=ledger)
        receiver.start()
        slack.opened[0].push(deleted("Ev0DUP", envelope))
        receiver.poll()
        receiver.stop()
        receivers.append(receiver)
    assert [receiver.pending for receiver in receivers] == [1, 0]
    assert receivers[1].stats.duplicates == 1


# Stale questions.


def test_a_question_asked_more_than_10_minutes_before_it_arrives_is_dropped() -> None:
    receiver, slack, clock = make()
    clock.advance(STALE_QUESTION_SECONDS + 1.0)
    slack.opened[0].push(
        mention("Ev-late", "env-late"), mention("Ev-edge", "env-edge", event_time=clock.now - STALE_QUESTION_SECONDS)
    )
    receiver.poll()
    assert slack.acked() == ["env-late", "env-edge"]
    assert [item.envelope_id for item in drain(receiver)] == ["env-edge"]
    assert receiver.stats.stale == 1


def test_a_question_that_waits_too_long_in_the_queue_is_dropped() -> None:
    receiver, slack, clock = make()
    slack.opened[0].push(mention(), command(), deleted())
    receiver.poll()
    clock.advance(STALE_QUESTION_SECONDS + 1.0)
    assert [item.kind for item in drain(receiver)] == ["message"]
    assert receiver.stats.stale == 2


def test_events_that_are_not_questions_never_go_stale() -> None:
    receiver, slack, clock = make()
    clock.advance(23 * HOUR)  # a Delayed Events retry of a deletion still has to take effect
    slack.opened[0].push(deleted(event_time=START))
    receiver.poll()
    clock.advance(HOUR)
    (item,) = drain(receiver)
    assert item.payload["event"]["subtype"] == "message_deleted"
    assert receiver.stats.stale == 0


# Two connections, and reconnection.


def test_start_opens_two_connections() -> None:
    receiver, slack, _ = make()
    assert receiver.open_connections == receiver.live_connections == SOCKET_CONNECTIONS
    assert len(slack.opened) == 2


@pytest.mark.parametrize("reason", ["warning", "refresh_requested"])
def test_a_refresh_opens_the_replacement_before_the_old_connection_closes_and_loses_nothing(reason: str) -> None:
    receiver, slack, _ = make()
    old = slack.opened[0]
    # Slack announces the close, then keeps delivering on the old connection until it closes it.
    old.push(disconnect(reason), mention("Ev-in-flight", "env-in-flight"))
    slack.journal.clear()

    receiver.poll()
    assert slack.journal.index(("open", "conn-3")) < slack.journal.index(("read", "conn-1"), 1)
    assert not old.closed
    assert receiver.open_connections == 3 and receiver.live_connections == 2
    assert slack.acked() == ["env-in-flight"]

    old.close_when_drained = True
    receiver.poll()
    assert receiver.open_connections == 2 and len(slack.opened) == 3
    assert receiver.retry_at == 0.0  # an announced close needs no backoff
    assert [item.envelope_id for item in drain(receiver)] == ["env-in-flight"]


def test_both_connections_refreshing_at_once_stay_within_slacks_connection_limit() -> None:
    receiver, slack, _ = make()
    for connection in slack.opened[:2]:
        connection.push(disconnect("warning"))
    receiver.poll()
    assert receiver.live_connections == 2
    assert receiver.open_connections == 4 <= SLACK_MAX_CONNECTIONS
    for connection in slack.opened[:2]:
        connection.close_when_drained = True
    receiver.poll()
    assert receiver.open_connections == 2


def test_an_unknown_disconnect_reason_is_treated_as_a_refresh() -> None:
    receiver, slack, _ = make()
    slack.opened[0].push(frame({"type": "disconnect", "reason": "something_new"}))
    receiver.poll()
    assert receiver.live_connections == 2 and len(slack.opened) == 3


def test_an_unexpected_close_reconnects_after_full_jitter_backoff() -> None:
    receiver, slack, clock = make(jitter=0.5)
    slack.opened[0].close()
    receiver.poll()
    assert receiver.live_connections == 1
    assert receiver.retry_at == START + 0.5 * 1.0

    clock.advance(0.4)
    receiver.poll()
    assert len(slack.opened) == 2
    clock.advance(0.1)
    receiver.poll()
    assert len(slack.opened) == 3 and receiver.live_connections == 2


def test_failed_reconnects_back_off_from_1_to_30_seconds_and_hello_resets_it() -> None:
    receiver, slack, clock = make(jitter=1.0)
    slack.opened[0].close()
    slack.opened[1].close()
    slack.fail_next = 7
    receiver.poll()

    waits = []
    while len(slack.opened) < 4:
        before = clock.now
        clock.now = receiver.retry_at
        waits.append(clock.now - before)
        receiver.poll()
    # Two unexpected closes, then seven refused opens, each wait capped at 30 seconds.
    assert waits[0] == 2.0
    assert waits[1:] == [4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]
    assert receiver.live_connections == 2

    # hello on the new connections resets the schedule.
    receiver.poll()
    slack.open[0].close()
    receiver.poll()
    assert receiver.retry_at == clock.now + 1.0


def test_a_failed_acknowledgement_counts_as_a_close_and_the_payload_still_has_one_effect() -> None:
    receiver, slack, clock = make()
    first = slack.opened[0]
    first.refuse_sends = True
    first.push(mention("Ev-1", "env-1"), mention("Ev-2", "env-2"))
    receiver.poll()
    # The second frame stayed on the dead connection; Slack redelivers both elsewhere.
    assert receiver.live_connections == 1
    slack.opened[1].push(mention("Ev-1", "env-1"), mention("Ev-2", "env-2b"))
    receiver.poll()
    assert [item.payload["event_id"] for item in drain(receiver)] == ["Ev-1", "Ev-2"]
    clock.now = receiver.retry_at
    receiver.poll()
    assert receiver.live_connections == 2


def test_link_disabled_stops_every_connection_and_never_reconnects() -> None:
    receiver, slack, clock = make()
    slack.opened[1].push(mention())
    slack.opened[0].push(disconnect("link_disabled"))
    with pytest.raises(SocketModeDisabled):
        receiver.poll()
    assert receiver.open_connections == 0 and not slack.open
    clock.advance(HOUR)
    with pytest.raises(SocketModeDisabled):
        receiver.poll()
    assert len(slack.opened) == 2


# The app_rate_limited gap ledger.


def test_rate_limit_notices_record_one_gap_that_events_close() -> None:
    receiver, slack, clock = make()
    connection = slack.opened[0]
    connection.push(rate_limited(START, "env-rl-1"))
    receiver.poll()
    assert receiver.gap_ledger.capture_slowed
    assert receiver.gap_ledger.gaps == (Gap(team=TEAM, start=START, end=START + MINUTE),)

    clock.advance(MINUTE)
    connection.push(rate_limited(START + MINUTE, "env-rl-2"), rate_limited(START + MINUTE, "env-rl-2"))
    receiver.poll()
    assert receiver.gap_ledger.gaps == (Gap(team=TEAM, start=START, end=START + 2 * MINUTE),)

    clock.advance(30.0)
    connection.push(deleted())
    receiver.poll()
    assert not receiver.gap_ledger.capture_slowed
    assert receiver.gap_ledger.gaps == (Gap(team=TEAM, start=START, end=START + 2 * MINUTE, closed=True),)
    assert slack.acked() == ["env-rl-1", "env-rl-2", "env-rl-2", "env-del"]


def test_a_gap_closes_after_a_quiet_minute_and_a_later_notice_opens_another() -> None:
    receiver, slack, clock = make()
    connection = slack.opened[0]
    connection.push(rate_limited(START))
    receiver.poll()
    clock.now = START + MINUTE + MINUTE - 1.0
    assert receiver.gap_ledger.capture_slowed
    clock.advance(1.0)
    assert not receiver.gap_ledger.capture_slowed

    clock.advance(HOUR)
    connection.push(rate_limited(clock.now, "env-rl-later"))
    receiver.poll()
    gaps = receiver.gap_ledger.gaps
    assert [gap.closed for gap in gaps] == [True, False]
    assert gaps[1].start == clock.now


# No network, and no path out of the knowledge service.


def test_the_receiver_never_touches_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the Socket Mode receiver tried to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    receiver, slack, clock = make()
    slack.opened[0].push(
        frame(fixture("events_api_app_mention")),
        frame(fixture("slash_command")),
        frame(fixture("app_rate_limited")),
        disconnect("warning"),
    )
    slack.opened[1].close()
    receiver.poll()
    clock.advance(MINUTE)
    receiver.poll()
    assert len(drain(receiver)) == 2
    assert receiver.live_connections == 2


def test_the_receiver_imports_nothing_that_reaches_the_network_or_the_supervisor() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("socket", "ssl", "http", "urllib", "asyncio", "subprocess", "automation.supervisor", "automation.benchmark")
    assert not {name for name in imported if name.split(".")[0] in forbidden or name.startswith(forbidden)}
    assert {name.split(".")[0] for name in imported} <= {
        "__future__",
        "collections",
        "dataclasses",
        "hashlib",
        "json",
        "logging",
        "random",
        "time",
        "typing",
        "pydantic",
    }
