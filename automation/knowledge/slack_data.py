"""Slack data rules for the knowledge service (SPEC-007 part 2, RA-KNOW-014).

The contract is ``docs/architecture/knowledge-loop-slack.md``: "Data rules" SD-1 to SD-11, the
deletion row of "Idempotence" and the RA-KNOW-014 slice. Every deadline here comes from that table.

Retries can't meet Slack's terms; these rules do. They are written once, against a small store
protocol, so every store a later slice adds (captured evidence, the Hindsight and Wiki projections,
dedupe hashes, cursors, questions waiting for an answer) is purged, swept and checked the same way.
Everything that writes Slack data goes through ``DataRules.write``, which refuses what the rules
forbid before a store sees it. Everything that reads it for an answer goes through
``DataRules.answerable``, which withholds a deleted message the moment the deletion is known, before
any store has finished deleting it.

The stores don't exist yet. ``FakeStore`` and ``FakeDirectoryStore`` stand in for them, so the
acceptance tests scan real stores. The standard library has no cipher, so this module checks
encryption at rest instead of providing it: a store must declare a strong cipher and a key
reference, and a store whose raw bytes can be inspected must not show a plaintext canary written to
it. Encrypting is each store's job.

The rules hold no credentials and open nothing. The caller injects the stores, the alert sink and
the clock, so tests run on a fake clock with no network. They can reach nothing but the stores they
are given: no supervisor, shell, queue, board, GitHub or consumer sync.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Callable,
    Collection,
    Generator,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

# SD-1 and SD-3. The Developer Policy allows 14 business days after an uninstall; the spec starts at
# once, aims for 24 hours, alerts at 5 business days and treats 14 as a hard deadline.
PURGE_TARGET_SECONDS = 24 * 3600.0
PURGE_ALERT_BUSINESS_DAYS = 5
PURGE_DEADLINE_BUSINESS_DAYS = 14

# SD-2. Withdrawn from answers at once; derived entries are gone or rebuilt within 24 hours.
REBUILD_DEADLINE_SECONDS = 24 * 3600.0

# SD-5. A dedupe hash outlives its question by the inbound deduplication window and no longer.
DEDUPE_RETENTION_SECONDS = 25 * 3600.0
# A question may wait in the inbound queue for up to 10 minutes (older ones are dropped as stale) and
# then has 20 seconds to be answered. Question text still held after that was left by a crash.
QUESTION_HOLD_LIMIT_SECONDS = 10 * 60.0 + 20.0

# SD-11 is checked daily.
SWEEP_INTERVAL_SECONDS = 24 * 3600.0

DAY_SECONDS = 24 * 3600.0

# SD-7 asks for strong encryption without naming a cipher. These are the authenticated ciphers, and
# the one disk-encryption mode, that current guidance treats as strong; anything else is refused by
# name so a reviewer sees what was declared.
STRONG_CIPHERS = frozenset({"AES-256-GCM", "AES-256-GCM-SIV", "AES-256-XTS", "ChaCha20-Poly1305", "XChaCha20-Poly1305"})
TLS_SCHEMES = frozenset({"https", "wss"})

# SD-4. Retrieval for answers is the only use of Slack data. Training, fine-tuning, evaluation sets
# built from messages, analytics and export are all refused because they aren't listed.
ALLOWED_USES = frozenset({"retrieval"})

# SD-8. Scopes per role, as SPEC-007 lists them, each with the reason the app's description gives.
# Private-channel and DM scopes are absent on purpose: SPEC-007 excludes those by default.
SCOPES_BY_ROLE: Mapping[str, Mapping[str, str]] = {
    "interface": {
        "commands": "Receive the slash command people ask questions with",
        "app_mentions:read": "Receive questions asked by mentioning the app",
        "chat:write": "Post each answer where its question was asked",
    },
    "source": {
        "channels:history": "Capture messages from the operator's allowlisted public channels",
        "channels:read": "Check who can read an allowlisted channel before citing it",
    },
}

# The encryption probe's canary is written under this workspace, which no real workspace id can
# equal because Slack team ids are upper-case alphanumerics.
PROBE_WORKSPACE = "boundroom-probe"

Role = Literal["interface", "source"]
RecordKind = Literal["message", "derived", "dedupe", "cursor", "question", "probe"]
PurgeReason = Literal["app_uninstalled", "tokens_revoked", "operator_removed", "user_request"]
TombstoneReason = Literal["message_deleted", "missing_on_reconciliation", "user_request"]
AlertLevel = Literal["warning", "critical"]

NonEmptyStr = Annotated[str, Field(min_length=1)]
# Slack's message timestamp: seconds and microseconds, which is also the message's id in its channel.
SlackTs = Annotated[str, Field(pattern=r"^\d+\.\d+$")]
# Evidence is the only kind an answer can cite, and the only kind SD-9 and SD-11 govern.
EVIDENCE_KINDS = frozenset({"message", "derived"})


class DataRuleError(Exception):
    """A data rule can't be applied as asked."""


class Refused(DataRuleError):
    """A data rule forbids this. ``rule`` names it, for example ``SD-9``."""

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(f"{rule}: {reason}")
        self.rule = rule


class UnencryptedStore(Refused):
    """A store fails the SD-7 encryption check, so nothing may be written to it."""

    def __init__(self, store: str, problems: Collection[str]) -> None:
        super().__init__("SD-7", f"store {store} is refused: {'; '.join(problems)}")
        self.store = store


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class State(BaseModel):
    """Rule state the caller persists between runs, through ``DataRules.snapshot``."""

    model_config = ConfigDict(extra="forbid")


class MessageRef(Model):
    """One Slack message: (``team``, ``channel``, ``ts``), the key the spec's Idempotence table uses."""

    workspace: NonEmptyStr
    channel: NonEmptyStr
    ts: SlackTs

    @property
    def sent_at(self) -> float:
        return float(self.ts)


class StoredRecord(Model):
    """One record in a store, with the identifiers every rule selects by kept in clear.

    ``workspace`` is required on every kind, dedupe hashes and cursors included, because SD-1 purges
    by workspace and a hash alone can't be traced back to one. ``body`` is whatever Slack-derived
    content the record holds: message text, a derived summary, a question, a hash or a cursor.
    """

    key: NonEmptyStr
    kind: RecordKind
    workspace: NonEmptyStr
    channel: Optional[NonEmptyStr] = None
    message: Optional[MessageRef] = None
    sources: frozenset[MessageRef] = frozenset()
    user: Optional[NonEmptyStr] = None
    body: Optional[str] = None
    stored_at: float

    @model_validator(mode="after")
    def _check(self) -> StoredRecord:
        if self.kind == "message":
            if self.message is None:
                raise ValueError("message: a captured message names its (team, channel, ts)")
            if (self.message.workspace, self.message.channel) != (self.workspace, self.channel):
                raise ValueError("message: a captured message's workspace and channel are its record's")
            if self.sources:
                raise ValueError("sources: only a derived entry has sources")
        elif self.kind == "derived":
            if not self.sources:
                raise ValueError("sources: a derived entry names the messages it was derived from")
            if self.message is not None:
                raise ValueError("message: a derived entry cites its messages as sources")
            # SPEC-007 decision 4: platforms and workspaces are isolated, so one entry can't mix them.
            if any(source.workspace != self.workspace for source in self.sources):
                raise ValueError("sources: a derived entry cites messages from its own workspace only")
        elif self.message is not None or self.sources:
            raise ValueError(f"message: a {self.kind} record cites no message")
        return self

    def cites(self) -> frozenset[MessageRef]:
        """Every message this record holds or was derived from."""
        return self.sources | (frozenset({self.message}) if self.message is not None else frozenset())


class Encryption(Model):
    """What a store declares about its encryption (SD-7).

    ``at_rest`` names the cipher, or is None for plaintext. ``key_ref`` says where the key lives, as
    a secret reference, never the key. ``in_transit`` is ``local`` for in-process or local-disk
    storage, ``tls`` for a remote store reached over TLS, and ``none`` for one reached without it.
    """

    at_rest: Optional[NonEmptyStr]
    key_ref: Optional[NonEmptyStr]
    in_transit: Literal["local", "tls", "none"]


class DataStore(Protocol):
    """A place Slack data is kept. Later slices implement it for each real store.

    ``put`` inserts or replaces the record with the same ``key``. ``records`` yields every record,
    decrypted. ``delete`` removes the named keys, ignores keys it doesn't hold, and returns how many
    it removed. A store that can't complete an operation raises.
    """

    name: str

    def encryption(self) -> Encryption: ...

    def put(self, record: StoredRecord) -> None: ...

    def records(self) -> Iterable[StoredRecord]: ...

    def delete(self, keys: Collection[str]) -> int: ...


@runtime_checkable
class RawInspectable(Protocol):
    """A store whose raw stored bytes can be searched, so SD-7 can test its encryption, not trust it."""

    def raw_contains(self, needle: bytes) -> bool: ...


def encryption_problems(store: DataStore, now: float) -> list[str]:
    """Why a store fails SD-7, or an empty list. Writes and deletes one canary when it can inspect raw bytes."""
    info = store.encryption()
    problems: list[str] = []
    if info.at_rest is None:
        problems.append("it isn't encrypted at rest")
    elif info.at_rest not in STRONG_CIPHERS:
        problems.append(f"cipher {info.at_rest!r} isn't one of {', '.join(sorted(STRONG_CIPHERS))}")
    elif info.key_ref is None:
        problems.append("it names no key reference")
    if info.in_transit == "none":
        problems.append("it is reached without TLS")
    if problems or not isinstance(store, RawInspectable):
        return problems
    # A declaration is only a claim. A store whose bytes can be read must show it can't be read.
    canary = secrets.token_hex(16)
    probe = StoredRecord(key=f"probe-{canary}", kind="probe", workspace=PROBE_WORKSPACE, body=canary, stored_at=now)
    store.put(probe)
    try:
        if store.raw_contains(canary.encode("ascii")):
            problems.append("it stores plaintext: a canary written to it is readable in its raw bytes")
    finally:
        store.delete([probe.key])
    return problems


def require_tls(url: str) -> str:
    """Refuse an endpoint that Slack data would reach without TLS (SD-7, in transit)."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in TLS_SCHEMES:
        raise Refused("SD-7", f"endpoint {url!r} doesn't use TLS ({scheme or 'no scheme'}); use https or wss")
    return url


def require_use(use: str) -> None:
    """Refuse any use of Slack data but retrieval for answers (SD-4)."""
    if use not in ALLOWED_USES:
        raise Refused("SD-4", f"Slack data is used for retrieval only, never for {use!r}")


def scope_problems(roles: Collection[str], requested: Mapping[str, str]) -> list[str]:
    """Why a requested scope set isn't minimal for these roles, or an empty list (SD-8)."""
    allowed: dict[str, str] = {}
    for role in roles:
        allowed.update(SCOPES_BY_ROLE.get(role, {}))
    problems = []
    for scope in sorted(requested):
        if scope not in allowed:
            problems.append(f"{scope} isn't needed by the {' and '.join(sorted(roles))} role")
        elif not requested[scope].strip():
            problems.append(f"{scope} has no reason in the app's description")
    return problems


def add_business_days(start: float, days: int) -> float:
    """``days`` business days after ``start``, at the same time of day.

    Business days are Monday to Friday in UTC. Public holidays aren't skipped, so a deadline can only
    fall earlier than a holiday calendar would put it, never later.
    """
    moment = datetime.fromtimestamp(start, tz=timezone.utc)
    counted = 0
    while counted < days:
        moment += timedelta(days=1)
        if moment.weekday() < 5:
            counted += 1
    return moment.timestamp()


class WorkspacePolicy(Model):
    """What the operator allows for one Slack workspace, as the data rules need it.

    It repeats the checks RA-KNOW-007's loader makes on the operator's file, because these rules must
    hold for whatever constructs them, not only for a loaded configuration.
    """

    workspace: NonEmptyStr
    roles: Annotated[frozenset[Role], Field(min_length=1)] = frozenset({"interface"})
    app: Optional[Literal["internal", "distributed"]] = None
    # Where the owner recorded how Slack's API terms apply to capturing this workspace (SD-6).
    terms_decision: Optional[NonEmptyStr] = None
    source_channels: frozenset[NonEmptyStr] = frozenset()
    # The operator's retention, and the workspace's own message retention when it has one (SD-11).
    retention_days: Optional[Annotated[int, Field(ge=1)]] = None
    workspace_retention_days: Optional[Annotated[int, Field(ge=1)]] = None
    # Each scope the app requests, with the reason its description gives (SD-8).
    scopes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> WorkspacePolicy:
        where = f"Slack workspace {self.workspace}"
        if "source" in self.roles:
            if self.app != "internal":
                raise ValueError(f"app: capture on {where} needs an internal app (SD-6)")
            if self.terms_decision is None:
                raise ValueError(
                    f"terms_decision: capture on {where} waits on the owner's recorded reading of Slack's terms (SD-6)"
                )
            if not self.source_channels:
                raise ValueError(f"source_channels: capture on {where} reads only allowlisted channels (SD-9)")
            if self.retention_days is None:
                raise ValueError(f"retention_days: captured evidence on {where} has no default retention (SD-11)")
        else:
            if self.source_channels:
                raise ValueError(f"source_channels: the source role is not enabled on {where}")
            if self.retention_days is not None or self.workspace_retention_days is not None:
                raise ValueError(f"retention_days: {where} captures nothing to retain")
        problems = scope_problems(self.roles, self.scopes)
        if problems:
            raise ValueError(f"scopes: {'; '.join(problems)} (SD-8)")
        return self

    @property
    def retention_seconds(self) -> Optional[float]:
        """How long captured evidence may be kept: the shorter of the workspace's and the operator's."""
        days = [d for d in (self.retention_days, self.workspace_retention_days) if d is not None]
        return min(days) * DAY_SECONDS if days else None


class Tombstone(State):
    """A withdrawn message. It outranks any edit or re-capture of that message (SD-2)."""

    message: MessageRef
    at: float
    reason: TombstoneReason
    # The user purge that tombstoned it, so that purge can find what derives from it.
    purge: Optional[str] = None
    # True once no store holds the message or anything derived from it.
    settled: bool = False
    alerted: bool = False
    last_error: Optional[str] = None


class PurgeJob(State):
    """An open purge of a workspace (SD-1), or of one user's data in it (SD-3)."""

    id: str
    reason: PurgeReason
    workspace: str
    user: Optional[str] = None
    requested_at: float
    attempts: int = 0
    deleted: int = 0
    alerted: list[Literal["alert", "deadline"]] = Field(default_factory=list)
    last_error: Optional[str] = None

    @property
    def rule(self) -> str:
        return "SD-1" if self.user is None else "SD-3"

    @property
    def alert_at(self) -> float:
        return add_business_days(self.requested_at, PURGE_ALERT_BUSINESS_DAYS)

    @property
    def deadline_at(self) -> float:
        return add_business_days(self.requested_at, PURGE_DEADLINE_BUSINESS_DAYS)

    def covers(self, record: StoredRecord, tombstones: Mapping[MessageRef, Tombstone]) -> bool:
        if record.workspace != self.workspace:
            return False
        if self.user is None or record.user == self.user:
            return True
        return any((stone := tombstones.get(ref)) is not None and stone.purge == self.id for ref in record.cites())


class PurgeReceipt(Model):
    """What a completed purge did, kept so the operator can show the request was honored in time."""

    id: str
    reason: PurgeReason
    workspace: str
    user: Optional[str]
    requested_at: float
    completed_at: float
    deleted: int
    met_target: bool
    met_deadline: bool


class RulesState(State):
    tombstones: list[Tombstone] = Field(default_factory=list)
    purges: list[PurgeJob] = Field(default_factory=list)
    receipts: list[PurgeReceipt] = Field(default_factory=list)
    last_sweep_at: Optional[float] = None
    last_sweep_alert_at: Optional[float] = None


@dataclass(frozen=True)
class Alert:
    rule: str
    level: AlertLevel
    message: str
    at: float
    workspace: Optional[str] = None


@dataclass
class TickReport:
    completed: list[PurgeReceipt] = field(default_factory=list)
    settled: int = 0
    expired: int = 0
    swept: Optional[int] = None


class DataRules:
    """SD-1 to SD-11 over a set of stores.

    Construction refuses any store that fails SD-7. ``write`` is the only way rule-abiding code adds
    Slack data, and ``answerable`` the only way it reads data for an answer. ``on_event``,
    ``remove_workspace``, ``forget_user`` and ``reconcile`` start deletions, which begin at once;
    ``tick`` retries what didn't finish, raises deadline alerts, expires question text and dedupe
    hashes, and runs the daily retention sweep. The caller persists ``snapshot()`` after each call
    that changes it and passes it back as ``state`` on restart, so a purge outlives a crash.
    """

    def __init__(
        self,
        policies: Iterable[WorkspacePolicy],
        stores: Iterable[DataStore],
        *,
        alert: Callable[[Alert], None],
        clock: Callable[[], float] = time.time,
        state: Optional[RulesState] = None,
    ) -> None:
        self._clock = clock
        self._alert = alert
        self._policies: dict[str, WorkspacePolicy] = {}
        for policy in policies:
            if policy.workspace in self._policies:
                raise DataRuleError(f"workspace {policy.workspace} has two policies")
            self._policies[policy.workspace] = policy
        self._stores: dict[str, DataStore] = {}
        for store in stores:
            if store.name in self._stores:
                raise DataRuleError(f"two stores are named {store.name}")
            self._stores[store.name] = store
        now = self._clock()
        for store in self._stores.values():
            problems = encryption_problems(store, now)
            if problems:
                raise UnencryptedStore(store.name, problems)
        # A store that later fails SD-7 takes no more writes, but is still purged and swept.
        self._disabled: dict[str, str] = {}
        restored = state.model_copy(deep=True) if state is not None else RulesState()
        self._tombstones: dict[MessageRef, Tombstone] = {stone.message: stone for stone in restored.tombstones}
        self._state = restored.model_copy(update={"tombstones": []})

    # Reading and writing

    def write(self, store: str, record: StoredRecord) -> None:
        """Store ``record`` if every data rule allows it, or raise ``Refused`` naming the rule."""
        target = self._writable(store)
        self._admit(record, self._clock())
        target.put(record)

    def answerable(self, records: Iterable[StoredRecord], *, use: str) -> list[StoredRecord]:
        """The records an answer may cite: evidence that isn't withdrawn, purged or past retention."""
        require_use(use)
        now = self._clock()
        return [record for record in records if not self._withheld(record, now)]

    def read(self, store: str, *, use: str) -> list[StoredRecord]:
        require_use(use)
        return self.answerable(self._known(store).records(), use=use)

    @contextmanager
    def hold_question(
        self, store: str, workspace: str, key: str, text: str, user: Optional[str] = None
    ) -> Generator[StoredRecord, None, None]:
        """Hold a question's text only while it is answered, then delete it (SD-5).

        ``key`` is the question's idempotency key. The dedupe hash the inbound layer keeps is a
        separate record, expired after 25 hours by ``tick``.
        """
        record = StoredRecord(
            key=f"question-{key}", kind="question", workspace=workspace, user=user, body=text, stored_at=self._clock()
        )
        self.write(store, record)
        try:
            yield record
        finally:
            # A failed delete raises rather than pretend; ``tick`` expires what a crash leaves.
            self._stores[store].delete([record.key])

    # Deletions

    def on_event(self, workspace: str, event: Mapping[str, Any]) -> None:
        """Apply an inbound Slack event that the data rules act on, and ignore every other event."""
        kind = event.get("type")
        if kind == "app_uninstalled" or kind == "tokens_revoked":
            self._start_purge(kind, workspace, None)
        elif kind == "message" and event.get("subtype") == "message_deleted":
            channel, ts = event.get("channel"), event.get("deleted_ts")
            if not isinstance(channel, str) or not isinstance(ts, str):
                raise DataRuleError("a message_deleted event names its channel and deleted_ts")
            self.withdraw(MessageRef(workspace=workspace, channel=channel, ts=ts), "message_deleted")

    def remove_workspace(self, workspace: str) -> PurgeJob:
        """The operator removed a workspace: refuse its data from now on and purge what is stored (SD-1)."""
        self._policies.pop(workspace, None)
        return self._start_purge("operator_removed", workspace, None)

    def forget_user(self, workspace: str, user: str) -> PurgeJob:
        """Honor a user's deletion request: their captured messages and everything derived from them (SD-3)."""
        return self._start_purge("user_request", workspace, user)

    def withdraw(self, message: MessageRef, reason: TombstoneReason = "message_deleted") -> Tombstone:
        """Withdraw a message from answers at once, then delete it and what derives from it (SD-2)."""
        stone = self._tombstone(message, reason, self._clock())
        if not stone.settled:
            self._settle(stone)
        return stone

    def reconcile(self, workspace: str, channel: str, present: Collection[str], since: float, until: float) -> list[Tombstone]:
        """Withdraw captured messages sent in [since, until] that a fresh read of the channel no longer shows (SD-2)."""
        present_ts = set(present)
        missing: set[MessageRef] = set()
        for store in self._stores.values():
            for record in store.records():
                ref = record.message
                if (
                    record.kind == "message"
                    and ref is not None
                    and (ref.workspace, ref.channel) == (workspace, channel)
                    and since <= ref.sent_at <= until
                    and ref.ts not in present_ts
                ):
                    missing.add(ref)
        return [self.withdraw(ref, "missing_on_reconciliation") for ref in sorted(missing, key=lambda ref: ref.sent_at)]

    def report_compromise(self, description: str, workspaces: Collection[str] = ()) -> list[Alert]:
        """Alert the operator at once about a suspected compromise of Slack data (SD-10)."""
        now = self._clock()
        alerts = [
            Alert(
                rule="SD-10", level="critical", message=f"suspected compromise of Slack data: {description}", at=now, workspace=w
            )
            for w in (sorted(workspaces) or [None])
        ]
        for alert in alerts:
            self._alert(alert)
        return alerts

    # The periodic job

    def tick(self) -> TickReport:
        """Retry open deletions, raise deadline alerts, expire short-lived records, and sweep once a day."""
        now = self._clock()
        report = TickReport()
        for job in list(self._state.purges):
            receipt = self._run_purge(job, now)
            if receipt is not None:
                report.completed.append(receipt)
            else:
                self._purge_alerts(job, now)
        for stone in list(self._tombstones.values()):
            if stone.settled:
                continue
            if self._settle(stone):
                report.settled += 1
            elif now >= stone.at + REBUILD_DEADLINE_SECONDS and not stone.alerted:
                stone.alerted = True
                self._alert(
                    Alert(
                        rule="SD-2",
                        level="critical",
                        message=f"deleted message {stone.message.channel}/{stone.message.ts} still has stored copies after 24 hours"
                        + (f": {stone.last_error}" if stone.last_error else ""),
                        at=now,
                        workspace=stone.message.workspace,
                    )
                )
        report.expired = self._expire(now)
        last = self._state.last_sweep_at
        if last is None or now - last >= SWEEP_INTERVAL_SECONDS:
            report.swept = self._sweep(now)
        return report

    def snapshot(self) -> RulesState:
        """The state to persist, encrypted like any other store, and to pass back as ``state`` on restart."""
        # Copied whole, so the caller can't change live state through it.
        return self._state.model_copy(update={"tombstones": list(self._tombstones.values())}).model_copy(deep=True)

    def open_purges(self) -> list[PurgeJob]:
        return [job.model_copy() for job in self._state.purges]

    def receipts(self) -> list[PurgeReceipt]:
        return list(self._state.receipts)

    def disabled_stores(self) -> dict[str, str]:
        return dict(self._disabled)

    # Internals

    def _known(self, name: str) -> DataStore:
        store = self._stores.get(name)
        if store is None:
            raise DataRuleError(f"no store is named {name}")
        return store

    def _writable(self, name: str) -> DataStore:
        store = self._known(name)
        if name in self._disabled:
            raise UnencryptedStore(name, [self._disabled[name]])
        return store

    def _expired(self, policy: WorkspacePolicy, ref: MessageRef, now: float) -> bool:
        retention = policy.retention_seconds
        return retention is not None and ref.sent_at < now - retention

    def _purging(self, record: StoredRecord) -> Optional[PurgeJob]:
        for job in self._state.purges:
            if job.covers(record, self._tombstones):
                return job
        return None

    def _admit(self, record: StoredRecord, now: float) -> None:
        if record.kind == "probe":
            raise Refused("SD-7", "probe records are written only by the encryption check")
        policy = self._policies.get(record.workspace)
        if policy is None:
            raise Refused("SD-1", f"workspace {record.workspace} isn't configured, so its data is purged, not stored")
        job = self._purging(record)
        if job is not None:
            raise Refused(job.rule, f"a purge of {self._scope(job)} is under way")
        if record.kind not in EVIDENCE_KINDS:
            return
        if "source" not in policy.roles:
            raise Refused(
                "SD-5", f"workspace {record.workspace} is interface-only and keeps no captured messages or derived entries"
            )
        for ref in record.cites():
            if ref.channel not in policy.source_channels:
                raise Refused("SD-9", f"channel {ref.channel} isn't allowlisted for capture in workspace {ref.workspace}")
            if ref in self._tombstones:
                raise Refused("SD-2", f"message {ref.channel}/{ref.ts} was withdrawn, which outranks any edit or re-capture")
            if self._expired(policy, ref, now):
                raise Refused("SD-11", f"message {ref.channel}/{ref.ts} is older than workspace {ref.workspace}'s retention")

    def _withheld(self, record: StoredRecord, now: float) -> bool:
        if record.kind not in EVIDENCE_KINDS:
            return True
        policy = self._policies.get(record.workspace)
        if policy is None or "source" not in policy.roles or self._purging(record) is not None:
            return True
        return any(ref in self._tombstones or self._expired(policy, ref, now) for ref in record.cites())

    def _scope(self, job: PurgeJob) -> str:
        return f"workspace {job.workspace}" + (f" user {job.user}" if job.user is not None else "")

    def _delete_where(self, wanted: Callable[[StoredRecord], bool]) -> tuple[int, list[str]]:
        """Delete matching records from every store, disabled ones too. Returns the count and each store's failure.

        One store failing mustn't stop deletion from the others, and the failure is reported, not raised,
        so the caller keeps the deletion open and retries it.
        """
        deleted = 0
        errors: list[str] = []
        for store in self._stores.values():
            try:
                keys = [record.key for record in store.records() if wanted(record)]
                if keys:
                    deleted += store.delete(keys)
            except Exception as exc:  # a store is outside this module; any failure keeps the deletion open
                errors.append(f"{store.name}: {exc}")
        return deleted, errors

    def _remaining(self, wanted: Callable[[StoredRecord], bool]) -> tuple[int, list[str]]:
        """Scan every store for records still matching. A deletion is complete only when this finds none."""
        found = 0
        errors: list[str] = []
        for store in self._stores.values():
            try:
                found += sum(1 for record in store.records() if wanted(record))
            except Exception as exc:  # an unreadable store can't be shown empty
                errors.append(f"{store.name}: {exc}")
        return found, errors

    def _tombstone(self, message: MessageRef, reason: TombstoneReason, now: float, purge: Optional[str] = None) -> Tombstone:
        stone = self._tombstones.get(message)
        if stone is None:
            stone = Tombstone(message=message, at=now, reason=reason, purge=purge)
            self._tombstones[message] = stone
        elif purge is not None and stone.purge is None:
            stone.purge = purge
        return stone

    def _settle(self, stone: Tombstone) -> bool:
        def cites(record: StoredRecord) -> bool:
            return stone.message in record.cites()

        _, errors = self._delete_where(cites)
        remaining, scan_errors = self._remaining(cites)
        errors += scan_errors
        stone.last_error = "; ".join(errors) or None
        stone.settled = not errors and remaining == 0
        return stone.settled

    def _start_purge(self, reason: PurgeReason, workspace: str, user: Optional[str]) -> PurgeJob:
        for job in self._state.purges:
            # A repeat, or a narrower request while a wider purge is open, joins the open purge.
            if job.workspace == workspace and (job.user is None or job.user == user):
                return job
        now = self._clock()
        digest = hashlib.sha256(json.dumps([reason, workspace, user, now]).encode("utf-8")).hexdigest()
        job = PurgeJob(id=f"purge-{digest[:16]}", reason=reason, workspace=workspace, user=user, requested_at=now)
        self._state.purges.append(job)
        # SD-1: start immediately, not at the next tick.
        self._run_purge(job, now)
        return job

    def _run_purge(self, job: PurgeJob, now: float) -> Optional[PurgeReceipt]:
        job.attempts += 1
        errors: list[str] = []
        if job.user is not None:
            # Tombstone the user's messages before deleting anything, so their answers stop at once and
            # entries derived from them are still found after the messages themselves are gone.
            for store in self._stores.values():
                try:
                    refs = [
                        r.message
                        for r in store.records()
                        if r.kind == "message" and r.workspace == job.workspace and r.user == job.user
                    ]
                except Exception as exc:  # see _delete_where
                    errors.append(f"{store.name}: {exc}")
                    continue
                for ref in refs:
                    if ref is not None:
                        self._tombstone(ref, "user_request", now, purge=job.id)

        def covered(record: StoredRecord) -> bool:
            return job.covers(record, self._tombstones)

        deleted, delete_errors = self._delete_where(covered)
        job.deleted += deleted
        remaining, scan_errors = self._remaining(covered)
        errors += delete_errors + scan_errors
        if errors or remaining:
            job.last_error = "; ".join(errors) or f"{remaining} records remain after deletion"
            return None
        receipt = PurgeReceipt(
            id=job.id,
            reason=job.reason,
            workspace=job.workspace,
            user=job.user,
            requested_at=job.requested_at,
            completed_at=now,
            deleted=job.deleted,
            met_target=now - job.requested_at <= PURGE_TARGET_SECONDS,
            met_deadline=now < job.deadline_at,
        )
        self._state.purges.remove(job)
        self._state.receipts.append(receipt)
        for ref, stone in list(self._tombstones.items()):
            if ref.workspace != job.workspace:
                continue
            if job.user is None:
                # The workspace's data is gone, and its tombstones are workspace data too.
                del self._tombstones[ref]
            elif stone.purge == job.id:
                stone.settled = True
        return receipt

    def _purge_alerts(self, job: PurgeJob, now: float) -> None:
        detail = f" ({job.last_error})" if job.last_error else ""
        if now >= job.alert_at and "alert" not in job.alerted:
            job.alerted.append("alert")
            self._alert(
                Alert(
                    rule=job.rule,
                    level="warning",
                    message=f"purge of {self._scope(job)} still open after {PURGE_ALERT_BUSINESS_DAYS} business days{detail}",
                    at=now,
                    workspace=job.workspace,
                )
            )
        if now >= job.deadline_at and "deadline" not in job.alerted:
            job.alerted.append("deadline")
            self._alert(
                Alert(
                    rule=job.rule,
                    level="critical",
                    message=f"purge of {self._scope(job)} missed the {PURGE_DEADLINE_BUSINESS_DAYS}-business-day deadline{detail}",
                    at=now,
                    workspace=job.workspace,
                )
            )

    def _expire(self, now: float) -> int:
        """Delete question text past its hold limit, dedupe hashes past 25 hours, and stray probe canaries (SD-5)."""

        def expired(record: StoredRecord) -> bool:
            if record.kind == "question":
                return now - record.stored_at > QUESTION_HOLD_LIMIT_SECONDS
            if record.kind == "dedupe":
                return now - record.stored_at > DEDUPE_RETENTION_SECONDS
            return record.kind == "probe"

        deleted, _ = self._delete_where(expired)
        return deleted

    def _sweep(self, now: float) -> Optional[int]:
        """The daily check: SD-7 on every store, SD-11 retention, and purging workspaces the operator removed."""
        for store in self._stores.values():
            if store.name in self._disabled:
                continue
            try:
                problems = encryption_problems(store, now)
            except Exception as exc:  # a store that can't be checked can't be trusted with writes
                problems = [f"its encryption can't be checked: {exc}"]
            if problems:
                self._disabled[store.name] = "; ".join(problems)
                # Data may already sit in it unencrypted, which is a suspected compromise.
                self.report_compromise(f"store {store.name} failed its encryption check: {self._disabled[store.name]}")

        orphans: set[str] = set()

        def past_retention(record: StoredRecord) -> bool:
            if record.kind == "probe":
                return False
            policy = self._policies.get(record.workspace)
            if policy is None:
                orphans.add(record.workspace)
                return False
            return record.kind in EVIDENCE_KINDS and any(self._expired(policy, ref, now) for ref in record.cites())

        swept, errors = self._delete_where(past_retention)
        for ref, stone in list(self._tombstones.items()):
            policy = self._policies.get(ref.workspace)
            # A settled tombstone past retention guards nothing: a re-capture would be refused as too old,
            # and an interface-only workspace never captures at all.
            if stone.settled and policy is not None and (policy.retention_seconds is None or self._expired(policy, ref, now)):
                del self._tombstones[ref]
        for workspace in sorted(orphans):
            self._start_purge("operator_removed", workspace, None)
        if errors:
            last_alert = self._state.last_sweep_alert_at
            if last_alert is None or now - last_alert >= SWEEP_INTERVAL_SECONDS:
                self._state.last_sweep_alert_at = now
                self._alert(
                    Alert(rule="SD-11", level="warning", message=f"retention sweep incomplete: {'; '.join(errors)}", at=now)
                )
            return None
        self._state.last_sweep_at = now
        return swept


# Fakes for tests and for later slices' contract tests. Neither encrypts anything.


class FakeStore:
    """An in-memory store. It declares whatever ``encryption`` it is given and has no raw bytes to inspect."""

    def __init__(self, name: str, encryption: Encryption) -> None:
        self.name = name
        self._encryption = encryption
        self._records: dict[str, StoredRecord] = {}

    def encryption(self) -> Encryption:
        return self._encryption

    def put(self, record: StoredRecord) -> None:
        self._records[record.key] = record

    def records(self) -> list[StoredRecord]:
        return list(self._records.values())

    def delete(self, keys: Collection[str]) -> int:
        return sum(1 for key in set(keys) if self._records.pop(key, None) is not None)


class FakeDirectoryStore:
    """A store of one file per record in a directory, with inspectable raw bytes.

    ``seal`` and ``unseal`` transform each file's bytes. They default to doing nothing, so the files are
    plaintext JSON and the SD-7 probe finds its canary whatever cipher the store declares. A test that
    needs a store passing the probe injects a reversible transform; that is not encryption either.
    """

    def __init__(
        self,
        name: str,
        root: Path,
        encryption: Encryption,
        *,
        seal: Callable[[bytes], bytes] = lambda data: data,
        unseal: Callable[[bytes], bytes] = lambda data: data,
    ) -> None:
        self.name = name
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._encryption = encryption
        self._seal = seal
        self._unseal = unseal

    def _path(self, key: str) -> Path:
        # Hash the key so no identifier appears in a file name.
        return self.root / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.record"

    def encryption(self) -> Encryption:
        return self._encryption

    def put(self, record: StoredRecord) -> None:
        self._path(record.key).write_bytes(self._seal(record.model_dump_json().encode("utf-8")))

    def records(self) -> list[StoredRecord]:
        return [StoredRecord.model_validate_json(self._unseal(path.read_bytes())) for path in sorted(self.root.glob("*.record"))]

    def delete(self, keys: Collection[str]) -> int:
        removed = 0
        for key in set(keys):
            path = self._path(key)
            if path.exists():
                path.unlink()
                removed += 1
        return removed

    def raw_contains(self, needle: bytes) -> bool:
        return any(needle in path.read_bytes() for path in self.root.iterdir() if path.is_file())
