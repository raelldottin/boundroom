"""Inbound Slack delivery over Socket Mode for the knowledge service (SPEC-007 part 2, RA-KNOW-013).

The contract is ``docs/architecture/knowledge-loop-slack.md``: decision 3, "Timers and budgets",
"Idempotence" and the RA-KNOW-013 slice. Every number here comes from those tables.

At-least-once in, exactly-once effect. A payload is acknowledged by its ``envelope_id`` as soon
as its frame is read, before it is validated, deduplicated or queued, and the receiver never
processes anything itself: accepted work waits in a local queue for the caller. So no processing
can push an acknowledgement toward Slack's deadline. A redelivery, under the same envelope or a
new one, is acknowledged and dropped for 25 hours. A question that arrives, or waits in the
queue, more than 10 minutes after it was asked is dropped. Two connections stay open, so a
refresh never leaves the app without one. An ``app_rate_limited`` notice opens a gap that capture
backfills later, because Slack never redelivers the events it dropped.

The receiver opens nothing itself. The caller injects the connection factory, which owns
``apps.connections.open``, the WebSocket and the app-level token, along with the clock and the
random source, so tests run on a fake clock with fake connections. It can reach nothing but the
connections it is given: no supervisor, shell, queue, board, GitHub or consumer sync.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random as _random
import time
from collections import deque
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

# Slack allows 3 seconds (assumed for Socket Mode until a recording settles it). Acknowledging
# within 1 second, before any processing, leaves 2 seconds of margin. The receiver meets it by
# sending each acknowledgement before it reads the next frame.
ACK_DEADLINE_SECONDS = 1.0
SLACK_ACK_LIMIT_SECONDS = 3.0

# Covers Slack's retries at about 0 seconds, 1 minute and 5 minutes, and Delayed Events' hourly
# retries for 24 hours.
DEDUPE_WINDOW_SECONDS = 25 * 3600.0

# An answer later than this, after retries or a reconnect, would surprise the asker and may
# reflect access they no longer have.
STALE_QUESTION_SECONDS = 10 * 60.0

# Slack allows 10. Two ride out a refresh while apps.connections.open is treated as Tier 1.
SOCKET_CONNECTIONS = 2
SLACK_MAX_CONNECTIONS = 10
RECONNECT_BASE_SECONDS = 1.0
RECONNECT_CAP_SECONDS = 30.0

# Slack sends ``app_rate_limited`` once a minute while it is dropping events, and names the minute.
RATE_LIMIT_NOTICE_SECONDS = 60.0

# The Socket Mode payload types this receiver accepts. SPEC-007 asks through slash commands and
# app_mention events; everything else is acknowledged and dropped.
QUESTION_EVENT_TYPES = frozenset({"app_mention"})
REPLACE_REASONS = frozenset({"warning", "refresh_requested"})

NonEmptyStr = Annotated[str, Field(min_length=1)]


class InboundError(Exception):
    """Inbound delivery can't continue."""


class ConnectionOpenFailed(InboundError):
    """The factory couldn't open a connection: ``apps.connections.open`` failed or the WebSocket didn't connect."""


class ConnectionClosed(InboundError):
    """The connection is closed. Raised by ``receive`` and ``send`` on a closed connection."""


class SocketModeDisabled(InboundError):
    """Slack sent ``link_disabled``: Socket Mode is off for the app, so reconnecting can't help."""


class SocketConnection(Protocol):
    """One Socket Mode WebSocket, already connected.

    ``receive`` returns the next text frame, or None when none is waiting, without blocking.
    ``receive`` and ``send`` raise ``ConnectionClosed`` once the connection has closed. ``close``
    is safe to call on a connection that is already closed.
    """

    def receive(self) -> Optional[str]: ...

    def send(self, text: str) -> None: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], SocketConnection]


class InboundWork(BaseModel):
    """One accepted payload, acknowledged and deduplicated, waiting to be processed.

    ``origin_at`` is when Slack says it happened (``event_time``), or when it was received for a
    slash command, which carries no time of its own. Staleness is measured from it.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source: Literal["events_api", "slash_commands"]
    envelope_id: NonEmptyStr
    team: NonEmptyStr
    kind: NonEmptyStr
    question: bool
    payload: dict[str, Any]
    origin_at: float
    received_at: float
    acked_at: float
    retry_attempt: int = 0


class Gap(BaseModel):
    """A span of time whose events Slack dropped, from the first rate-limited minute to the end of the last."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    team: NonEmptyStr
    start: float
    end: float
    closed: bool = False


@dataclass
class InboundStats:
    acknowledged: int = 0
    accepted: int = 0
    duplicates: int = 0
    stale: int = 0
    malformed: int = 0
    unsupported: int = 0
    foreign: int = 0


# Slack's payloads carry many more fields than these, and add new ones; the receiver reads only
# what it needs and keeps the rest in the payload untouched.


class _SlackShape(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Envelope(_SlackShape):
    envelope_id: NonEmptyStr
    type: str
    payload: dict[str, Any]
    retry_attempt: int = 0


class _EventCallback(_SlackShape):
    type: Literal["event_callback"]
    team_id: NonEmptyStr
    event_id: NonEmptyStr
    event_time: int
    event: dict[str, Any]


class _RateLimited(_SlackShape):
    type: Literal["app_rate_limited"]
    team_id: NonEmptyStr
    minute_rate_limited: int


class _SlashCommand(_SlackShape):
    team_id: NonEmptyStr
    channel_id: NonEmptyStr
    user_id: NonEmptyStr
    command: NonEmptyStr
    text: str = ""


def _key_hash(parts: tuple[str, ...]) -> str:
    # The unit separator can't appear in a Slack id, so distinct tuples can't collide by joining.
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class DedupeLedger:
    """Keys seen in the last 25 hours, kept only as sha256 hashes, never as the ids or text they came from.

    A key's window starts when it is first seen; a repeat inside it doesn't extend it.
    """

    def __init__(self, clock: Callable[[], float], *, window_seconds: float = DEDUPE_WINDOW_SECONDS) -> None:
        self._clock = clock
        self._window = window_seconds
        self._seen: dict[str, float] = {}
        self._order: deque[tuple[float, str]] = deque()

    def __len__(self) -> int:
        self._prune(self._clock())
        return len(self._seen)

    def stored(self) -> frozenset[str]:
        self._prune(self._clock())
        return frozenset(self._seen)

    def check_and_remember(self, keys: Sequence[tuple[str, ...]]) -> bool:
        """True when any key was seen within the window. Every key is remembered either way."""
        now = self._clock()
        self._prune(now)
        hashes = [_key_hash(key) for key in keys]
        duplicate = any(digest in self._seen for digest in hashes)
        for digest in hashes:
            if digest not in self._seen:
                self._seen[digest] = now
                self._order.append((now, digest))
        return duplicate

    def _prune(self, now: float) -> None:
        while self._order and now - self._order[0][0] > self._window:
            _, digest = self._order.popleft()
            self._seen.pop(digest, None)


class GapLedger:
    """Spans whose events Slack dropped while the app was over its event delivery limit.

    A notice opens a gap at ``minute_rate_limited``, and each later notice extends it to the end of
    the minute it names. The gap closes when events resume, or when a whole notice interval passes
    after its end with no new notice. While a gap is open, capture is slowed.
    """

    def __init__(self, workspace: str, clock: Callable[[], float]) -> None:
        self._workspace = workspace
        self._clock = clock
        self._gaps: list[Gap] = []

    def notice(self, minute_rate_limited: int) -> None:
        start = float(minute_rate_limited)
        end = start + RATE_LIMIT_NOTICE_SECONDS
        self._settle()
        if self._gaps and not self._gaps[-1].closed:
            last = self._gaps[-1]
            self._gaps[-1] = last.model_copy(update={"start": min(last.start, start), "end": max(last.end, end)})
            return
        self._gaps.append(Gap(team=self._workspace, start=start, end=end))
        log.warning(
            "slack workspace %s is over its event delivery limit; events from %d are being dropped", self._workspace, start
        )

    def events_resumed(self) -> None:
        if self._gaps and not self._gaps[-1].closed:
            self._gaps[-1] = self._gaps[-1].model_copy(update={"closed": True})

    @property
    def gaps(self) -> tuple[Gap, ...]:
        self._settle()
        return tuple(self._gaps)

    @property
    def capture_slowed(self) -> bool:
        self._settle()
        return bool(self._gaps) and not self._gaps[-1].closed

    def _settle(self) -> None:
        if self._gaps and not self._gaps[-1].closed and self._clock() >= self._gaps[-1].end + RATE_LIMIT_NOTICE_SECONDS:
            self._gaps[-1] = self._gaps[-1].model_copy(update={"closed": True})


@dataclass(eq=False)
class _Link:
    connection: SocketConnection
    # Slack announced this connection's close; its replacement is open or scheduled.
    draining: bool = False
    alive: bool = True


def reconnect_ceiling(attempt: int) -> float:
    """The upper bound of the full-jitter wait before reconnect ``attempt`` (0 for the first)."""
    return min(RECONNECT_CAP_SECONDS, RECONNECT_BASE_SECONDS * 2.0**attempt)


class SocketModeReceiver:
    """Socket Mode delivery for one workspace's app: connections, acknowledgement, deduplication and the work queue.

    The caller drives it: ``start`` once, then ``poll`` often to read and acknowledge frames and to
    keep two connections open, and ``next_work`` to take accepted work. ``poll`` never processes
    work, so a slow handler can't delay an acknowledgement.
    """

    # ponytail: not thread-safe. One caller drives poll and next_work; add a lock if a later
    # slice processes work on another thread.

    def __init__(
        self,
        workspace: str,
        connect: ConnectionFactory,
        *,
        clock: Callable[[], float] = time.time,
        random: Callable[[], float] = _random.random,
        ledger: Optional[DedupeLedger] = None,
    ) -> None:
        if not workspace:
            raise ValueError("workspace must be a Slack workspace id")
        self.workspace = workspace
        self.stats = InboundStats()
        self.gap_ledger = GapLedger(workspace, clock)
        self.ledger = ledger if ledger is not None else DedupeLedger(clock)
        self._connect = connect
        self._clock = clock
        self._random = random
        self._links: list[_Link] = []
        self._queue: deque[InboundWork] = deque()
        self._attempt = 0
        self._retry_at = 0.0
        self._started = False
        self._stopped = False
        self._disabled = False

    # Connections

    @property
    def open_connections(self) -> int:
        return len(self._links)

    @property
    def live_connections(self) -> int:
        """Open connections that Slack hasn't announced it will close."""
        return sum(1 for link in self._links if not link.draining)

    @property
    def retry_at(self) -> float:
        """When the next connection attempt may run, after a failure or an unexpected close."""
        return self._retry_at

    def start(self) -> None:
        if self._stopped:
            raise InboundError("a stopped receiver can't be started again")
        self._started = True
        self._fill()

    def stop(self) -> None:
        self._stopped = True
        self._close_all()

    def poll(self) -> int:
        """Keep two connections open, then read and acknowledge every waiting frame. Returns the frames read."""
        if not self._started:
            raise InboundError("start the receiver before polling it")
        if self._disabled:
            raise SocketModeDisabled(f"Socket Mode is disabled for workspace {self.workspace}")
        self._fill()
        read = 0
        for link in list(self._links):
            while link.alive:
                try:
                    raw = link.connection.receive()
                except ConnectionClosed:
                    self._closed(link)
                    break
                if raw is None:
                    break
                read += 1
                self._frame(link, raw)
        return read

    def _fill(self) -> None:
        if self._stopped or self._disabled:
            return
        while self.live_connections < SOCKET_CONNECTIONS:
            if self._clock() < self._retry_at:
                return
            try:
                connection = self._connect()
            except ConnectionOpenFailed as error:
                log.warning("slack workspace %s: couldn't open a Socket Mode connection: %s", self.workspace, error)
                self._back_off()
                return
            self._links.append(_Link(connection))

    def _back_off(self) -> None:
        wait = self._random() * reconnect_ceiling(self._attempt)
        self._attempt += 1
        self._retry_at = max(self._retry_at, self._clock() + wait)

    def _closed(self, link: _Link) -> None:
        link.alive = False
        self._links.remove(link)
        if link.draining:
            # Slack closed it as announced, after its replacement was opened.
            self._fill()
            return
        log.warning("slack workspace %s: a Socket Mode connection closed unexpectedly", self.workspace)
        self._back_off()

    def _disconnect(self, link: _Link, reason: object) -> None:
        if reason == "link_disabled":
            self._disabled = True
            self._close_all()
            raise SocketModeDisabled(f"Slack disabled Socket Mode for workspace {self.workspace}")
        if reason not in REPLACE_REASONS:
            # An unlisted reason still means Slack is about to close this connection.
            log.warning("slack workspace %s: unexpected disconnect reason %r", self.workspace, reason)
        # Keep reading the old connection until Slack closes it, so nothing still in flight on it
        # is lost, while the replacement opens beside it.
        link.draining = True
        self._fill()

    def _close_all(self) -> None:
        for link in self._links:
            link.alive = False
            link.connection.close()
        self._links.clear()

    # Frames

    def _frame(self, link: _Link, raw: str) -> None:
        received_at = self._clock()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.stats.malformed += 1
            log.warning("slack workspace %s: dropped a frame that isn't a JSON object", self.workspace)
            return
        envelope_id = data.get("envelope_id")
        if isinstance(envelope_id, str) and envelope_id:
            # Acknowledge before anything else, duplicates and malformed payloads included, so
            # Slack never retries what this receiver already holds or will never accept.
            self._ack(link, envelope_id)
            self._accept(data, received_at, self._clock())
            return
        kind = data.get("type")
        if kind == "hello":
            # The connection works end to end, so the next failure starts the backoff afresh.
            self._attempt = 0
            return
        if kind == "disconnect":
            self._disconnect(link, data.get("reason"))
            return
        self.stats.malformed += 1
        log.warning("slack workspace %s: dropped a frame with no envelope_id, of type %r", self.workspace, kind)

    def _ack(self, link: _Link, envelope_id: str) -> None:
        try:
            link.connection.send(json.dumps({"envelope_id": envelope_id}))
        except ConnectionClosed:
            # Slack will redeliver it; deduplication drops the redelivery, so it is still
            # accepted below exactly once.
            self._closed(link)
            return
        self.stats.acknowledged += 1

    def _accept(self, data: dict[str, Any], received_at: float, acked_at: float) -> None:
        try:
            envelope = _Envelope.model_validate(data)
        except ValidationError:
            self.stats.malformed += 1
            log.warning("slack workspace %s: dropped a malformed Socket Mode envelope", self.workspace)
            return
        if envelope.type == "events_api":
            self._event(envelope, received_at, acked_at)
        elif envelope.type == "slash_commands":
            self._command(envelope, received_at, acked_at)
        else:
            self.stats.unsupported += 1
            log.info("slack workspace %s: ignored a %s payload", self.workspace, envelope.type)

    def _event(self, envelope: _Envelope, received_at: float, acked_at: float) -> None:
        payload_type = envelope.payload.get("type")
        if payload_type == "app_rate_limited":
            try:
                notice = _RateLimited.model_validate(envelope.payload)
            except ValidationError:
                self.stats.malformed += 1
                return
            if self._foreign(notice.team_id):
                return
            # A notice needs no deduplication: recording the same minute twice changes nothing.
            self.gap_ledger.notice(notice.minute_rate_limited)
            return
        if payload_type != "event_callback":
            self.stats.unsupported += 1
            log.info("slack workspace %s: ignored an events_api payload of type %r", self.workspace, payload_type)
            return
        try:
            callback = _EventCallback.model_validate(envelope.payload)
        except ValidationError:
            self.stats.malformed += 1
            log.warning("slack workspace %s: dropped a malformed event_callback", self.workspace)
            return
        if self._foreign(callback.team_id):
            return
        self.gap_ledger.events_resumed()
        # event_id is globally unique, so it catches a redelivery under a new envelope too.
        keys = [("event", callback.event_id), ("envelope", envelope.envelope_id)]
        if self.ledger.check_and_remember(keys):
            self.stats.duplicates += 1
            return
        kind = callback.event.get("type")
        self._enqueue(
            InboundWork(
                source="events_api",
                envelope_id=envelope.envelope_id,
                team=callback.team_id,
                kind=kind if isinstance(kind, str) and kind else "unknown",
                question=kind in QUESTION_EVENT_TYPES,
                payload=_without_token(envelope.payload),
                origin_at=float(callback.event_time),
                received_at=received_at,
                acked_at=acked_at,
                retry_attempt=envelope.retry_attempt,
            )
        )

    def _command(self, envelope: _Envelope, received_at: float, acked_at: float) -> None:
        try:
            command = _SlashCommand.model_validate(envelope.payload)
        except ValidationError:
            self.stats.malformed += 1
            log.warning("slack workspace %s: dropped a malformed slash command", self.workspace)
            return
        if self._foreign(command.team_id):
            return
        # A redelivery under a new envelope has no id to match, so the command's content and the
        # minute it arrived stand in. An identical command repeated within that minute is ignored.
        minute = str(int(received_at // 60))
        keys = [
            ("envelope", envelope.envelope_id),
            ("command", command.team_id, command.channel_id, command.user_id, command.command, command.text, minute),
        ]
        if self.ledger.check_and_remember(keys):
            self.stats.duplicates += 1
            return
        self._enqueue(
            InboundWork(
                source="slash_commands",
                envelope_id=envelope.envelope_id,
                team=command.team_id,
                kind=command.command,
                question=True,
                payload=_without_token(envelope.payload),
                origin_at=received_at,
                received_at=received_at,
                acked_at=acked_at,
                retry_attempt=envelope.retry_attempt,
            )
        )

    def _foreign(self, team: str) -> bool:
        # One app per workspace (decision 1), so another workspace's payload is a misconfiguration.
        if team == self.workspace:
            return False
        self.stats.foreign += 1
        log.warning("slack workspace %s: dropped a payload for workspace %s", self.workspace, team)
        return True

    # The work queue

    @property
    def pending(self) -> int:
        return len(self._queue)

    def next_work(self) -> Optional[InboundWork]:
        """The next accepted payload to process, skipping questions that waited too long. None when empty."""
        while self._queue:
            item = self._queue.popleft()
            if self._stale(item, self._clock()):
                continue
            return item
        return None

    def _enqueue(self, item: InboundWork) -> None:
        if self._stale(item, item.received_at):
            return
        self.stats.accepted += 1
        self._queue.append(item)
        log.debug("slack workspace %s: queued %s (envelope %s)", self.workspace, item.kind, item.envelope_id)

    def _stale(self, item: InboundWork, now: float) -> bool:
        # Only questions go stale. A deletion or an uninstall must take effect however late it is.
        if not item.question or now - item.origin_at <= STALE_QUESTION_SECONDS:
            return False
        self.stats.stale += 1
        # The question's text is not logged: interface-only mode keeps none (SD-5).
        log.info(
            "slack workspace %s: dropped a %s question asked %.0f seconds ago (envelope %s)",
            self.workspace,
            item.kind,
            now - item.origin_at,
            item.envelope_id,
        )
        return True


def _without_token(payload: dict[str, Any]) -> dict[str, Any]:
    # Slack still sends its deprecated verification token in payloads. Nothing downstream needs
    # it, and the queue shouldn't hold a credential.
    return {key: value for key, value in payload.items() if key != "token"}
