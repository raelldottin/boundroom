"""The Slack adapter behind ``ChatPlatform`` (SPEC-007), in the ``interface`` and ``source`` roles.

RA-KNOW-009 built the ``interface`` role and RA-KNOW-011 the ``source`` role.

``docs/architecture/knowledge-loop-platforms.md`` specifies it ("How each platform maps", the Slack
column), and ``docs/architecture/knowledge-loop-slack.md`` sets its limits, retries and data rules.
The adapter translates and nothing more: Socket Mode deliveries become normalized ``Interaction``
values, and normalized replies and shares become Slack Web API calls. Every rule about what may be
answered, cited or shared stays in the platform-neutral code.

It opens nothing itself. Every Web API call goes through RA-KNOW-012's ``SlackClient``, so it is
throttled, classified and retried as part 2 requires, and every delivery comes through RA-KNOW-013's
``SocketModeReceiver``, so it is acknowledged first, deduplicated and dropped when stale. Both sit on
transports the caller injects: ``slack_factory`` reads the bot token and the app-level token with
``config.read_secret`` when ``start_platforms`` builds the adapter, and hands each straight to its
transport. The adapter never holds either token.

What the adapter guarantees, beyond the ``ChatPlatform`` contract:

- **Least privilege by role (CP-3).** ``bot_scopes`` asks for a history scope, and
  ``event_subscriptions`` for a message event, only for the ``source`` role. An interface-only adapter
  never reads a page of history, and drops a message event delivered anyway.
- **Capture waits on the terms decision (CP-7).** The owner decided on 2026-11-12 (PR #228) that an
  internal app may keep a derived index of its own workspace's messages, and a distributed app may
  not. So the ``source`` role is built only for an entry that declares ``app: "internal"`` and records
  ``slack_terms_decision``, which the configuration already requires, and only with the data rules to
  hand deletions to. Anything else is refused before a token is read.
- **Capture reads only allowlisted channels (CP-6, SD-9).** A public channel, or a private channel
  allowlisted by its own id. Never a DM, a group DM or a Slack Connect channel, allowlisted or not.
  Capture is incremental, a page of 200 at a time, with no workspace-wide read or export.
- **Edits and deletions propagate (CP-8, SD-2).** Message, edit and deletion events are captured as
  they arrive. A deletion is handed to the data rules at once, as well as returned on a page, and
  ``can_see`` asks Slack for the message at call time, so a deleted message or an edited message's
  old revision is never confirmed. A span whose events Slack dropped (``app_rate_limited``) is read
  again from history, and captured messages missing from it are reconciled away.
- **Retention (SD-11).** Evidence older than the shorter of the operator's ``retention_days`` and the
  workspace's ``workspace_retention_days`` is neither read from history nor held for capture.
- **Exclusions hold (CP-6).** A question is answered, and a share posted, only in a channel the
  operator allowlisted, checked again with ``conversations.info`` at reply time. DMs and group DMs
  are never answered. A private channel is answered only when its own id is allowlisted. A Slack
  Connect channel, shared with another organization, is never answered or shared to, allowlisted or
  not: this slice has no setting that opts one in.
- **Messages are data (CP-9).** Places and people come from the payload's structural fields, never
  from text. The question is the text of the ``/ask`` command, or of a message that mentions the
  bot; every other command, field and event is ignored. Bots can't ask. Every message the adapter
  sends escapes Slack's control sequences and turns off name linking, so an answer can't notify a
  channel, a group or a person, or hide a link.
- **At most one answer (part 2, Idempotence).** A question is answered once. A post whose result is
  unknown is not retried, because ``chat.postEphemeral`` and ``chat.postMessage`` have no
  idempotency key. A share carries its ``share_id`` in the message's metadata, so whoever retries
  it later can look for it first.
- **Interface-only keeps nothing (SD-5).** A question's text is held only until it is answered, or
  until it goes stale, and is never logged.

A message's revision is ``"0"`` until it is edited, then its ``edited.ts``. Slack has no revision
number, and the edit's timestamp is the one value every copy of the message agrees on.

How capture reads a channel. Slack has no event log to resume, so the adapter keeps one: a journal of
the message events delivered since it started, as the Discord adapter does. History before the
journal began comes from ``conversations.history``, and thread replies from
``conversations.replies``, between the cursor's watermark (or the retention horizon) and the
journal's start. A cursor names the journal it counts in. One from an earlier journal, after a
restart, reads history again from its watermark, less a five-minute overlap for deliveries Slack was
still retrying. Pages that overlap are harmless, because a captured message is an upsert.
"""

from __future__ import annotations

import logging
import random as _random
import re
import secrets
import time
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, NoReturn, Optional, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from automation.knowledge import config as knowledge_config
from automation.knowledge.config import KnowledgeSettings, PlatformConfig, ShareDestination
from automation.knowledge.platform import (
    Answer,
    ApprovalReceipt,
    Audience,
    CaptureEvent,
    CapturePage,
    ChatPlatform,
    Delivery,
    Interaction,
    Place,
    PlatformError,
    PlatformFactory,
    PlatformKind,
    PlatformSelection,
    PlatformSelectionError,
    Principal,
    Role,
    ShareCandidate,
    SourceRef,
    Visibility,
    VisibilityRefused,
    check_receipt,
    check_visibility,
    require_own,
    require_role,
)
from automation.knowledge.slack_client import (
    CAPTURE_BUDGET,
    QUESTION_BUDGET,
    RETRY_AFTER_MISSING_SECONDS,
    ErrorClass,
    MethodSpec,
    RetryBudget,
    SlackCallError,
    SlackClient,
    Transport,
)
from automation.knowledge.slack_inbound import (
    RATE_LIMIT_NOTICE_SECONDS,
    STALE_QUESTION_SECONDS,
    ConnectionOpenFailed,
    Gap,
    InboundError,
    InboundWork,
    SocketConnection,
    SocketModeReceiver,
)

log = logging.getLogger(__name__)

# Least privilege for the interface role. SPEC-007 lists commands, app_mentions:read and chat:write.
# The contract also needs ``audience`` and the call-time channel checks, which read a channel's
# members and type: channels:read for public channels and groups:read for an allowlisted private one.
# Neither reads a message.
INTERFACE_SCOPES: frozenset[str] = frozenset({"commands", "app_mentions:read", "chat:write", "channels:read", "groups:read"})
# Requested only by the source role: history in public channels, and in private channels the operator
# allowlisted by id. No im:history or mpim:history, because DMs and group DMs are never captured.
HISTORY_SCOPES: frozenset[str] = frozenset({"channels:history", "groups:history"})
# Capture checks a channel's type before reading it, and can_see its members, with the read scopes.
SOURCE_SCOPES: frozenset[str] = HISTORY_SCOPES | {"channels:read", "groups:read"}
# Each scope's reason, for the app's description (SD-8).
SCOPE_REASONS: Mapping[str, str] = {
    "commands": "Receive the /ask command people ask questions with",
    "app_mentions:read": "Receive questions asked by mentioning the app",
    "chat:write": "Post each answer where its question was asked, and shares a person approved",
    "channels:read": "Check an allowlisted public channel's type and members before answering in it or citing it",
    "groups:read": "Check a private channel allowlisted by id: its type and members, before answering in it or citing it",
    "channels:history": "Capture messages, edits and deletions from the public channels the operator allowlisted",
    "groups:history": "Capture messages, edits and deletions from the private channels the operator allowlisted by id",
}
# Socket Mode's app-level token needs only this.
APP_TOKEN_SCOPES: frozenset[str] = frozenset({"connections:write"})
# Asking arrives as an app_mention event or a slash command. Slash commands need no subscription.
INTERFACE_EVENTS: frozenset[str] = frozenset({"app_mention"})
# New messages, edits and deletions in public and private channels the app is in, and the two events
# that start SD-1's purge. No message.im or message.mpim.
SOURCE_EVENTS: frozenset[str] = frozenset({"message.channels", "message.groups", "app_uninstalled", "tokens_revoked"})

ASK_COMMAND = "/ask"

# Slack truncates a message past 40,000 characters. Cutting an answer short could drop its
# citations or its caveat, so a longer one is refused whole.
MESSAGE_LIMIT = 40_000
MEMBER_PAGE = 200
# 100 pages of 200 is 20,000 members. A channel larger than that isn't one a knowledge answer
# should be broadcast to, and the bound keeps a misbehaving cursor from looping forever.
MEMBER_PAGES = 100

# The Web API methods this adapter calls beyond the ones RA-KNOW-012 defines, with the tiers Slack
# documents. auth.test is "special"; it is called once per start, so the slowest tier costs nothing.
ADAPTER_METHODS: Mapping[str, MethodSpec] = {
    "auth.test": MethodSpec(tier=1, idempotent=True),
    "conversations.info": MethodSpec(tier=3, idempotent=True),
    "conversations.members": MethodSpec(tier=4, idempotent=True),
}

# Capture. Slack recommends no more than 200 messages per history page (part 2, "Timers and budgets").
HISTORY_PAGE = 200
JOURNAL_PAGE = 200
# The journal holds message text only until it is read or ages past retention. Past this many events
# it starts again, and cursors into it read history instead.
JOURNAL_LIMIT = 100_000
# Slack retries an event delivery after about 0 seconds, 1 minute and 5 minutes, so a message sent
# just before the last one read may still have been on its way when the journal was lost.
RESUME_OVERLAP_SECONDS = 300.0
DAY_SECONDS = 24 * 3600.0
# Message subtypes a person writes. Joins, topic changes, bot messages and the rest aren't evidence.
CAPTURED_SUBTYPES: frozenset[str] = frozenset({"thread_broadcast", "file_share"})
# Capture can wait, so transient failures get part 2's capture budget. A 429 is handed back to the
# caller as an incomplete page instead of waited out inside one call, as the ChatPlatform contract
# asks; the client's limiter stays armed, so the next page still waits for Retry-After.
CAPTURE_PAGE_BUDGET = replace(CAPTURE_BUDGET, wait_on_throttle=False)

# Opening a Socket Mode connection runs inside the receiver's poll, between acknowledgements. A
# limiter wait that long would hold up acknowledgements on the other connection, so the open gives
# up instead and the receiver's reconnect backoff tries again.
CONNECTION_BUDGET = RetryBudget(cap_seconds=1.0, max_attempts=1, deadline_seconds=1.0)

# Slack's own control sequences: <!channel>, <!here>, <!everyone>, <@U…>, <!subteam^…>, <#C…> and
# <https://…|label>. Escaping these three characters is how Slack says to send text literally.
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
# Errors that mean "this message can't be read by the app", which can_see answers with False.
_UNREADABLE = frozenset({"missing_scope", "message_not_found", "thread_not_found"})


class NotAllowlisted(PlatformError):
    """The channel isn't on the operator's interface allowlist, or the place isn't a share destination."""


class ExcludedConversation(PlatformError):
    """A DM, a group DM or a Slack Connect channel, which the adapter never answers in or shares to."""


class OutcomeUnknown(PlatformError):
    """Slack may or may not have posted it. It is not retried, because a duplicate is worse than a gap."""


class CaptureFailed(PlatformError):
    """This channel's capture failed for this run. Other channels are unaffected; the cursor is still good."""


class _Throttled(Exception):
    """Slack answered a capture call with a 429. Becomes an incomplete page, never reaches the caller."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds:g} seconds")
        self.seconds = seconds


class CaptureRules(Protocol):
    """What capture hands the Slack data rules (part 2, RA-KNOW-014) directly, beside the pages it returns.

    ``DataRules`` in ``automation/knowledge/slack_data.py`` satisfies it as written: ``on_event``
    withdraws a ``message_deleted`` message from answers at once and starts SD-1's purge on
    ``app_uninstalled`` or ``tokens_revoked``, and ``reconcile`` withdraws captured messages that a
    fresh read of a span no longer shows (SD-2). Messages and edits are not handed over here. They go
    out on the page, and the platform-neutral capture stage writes them through ``DataRules.write``,
    which applies SD-2, SD-9 and SD-11 again before any store sees them.
    """

    def on_event(self, workspace: str, event: Mapping[str, Any]) -> None: ...

    def reconcile(self, workspace: str, channel: str, present: Collection[str], since: float, until: float) -> object: ...


def bot_scopes(roles: frozenset[Role]) -> frozenset[str]:
    """The bot token scopes the app asks for: the history scopes only with the source role."""
    scopes: frozenset[str] = frozenset()
    if "interface" in roles:
        scopes |= INTERFACE_SCOPES
    if "source" in roles:
        scopes |= SOURCE_SCOPES
    return scopes


def scope_reasons(roles: frozenset[Role]) -> dict[str, str]:
    """Each scope ``bot_scopes`` asks for, with the reason the app's description gives (SD-8)."""
    return {scope: SCOPE_REASONS[scope] for scope in sorted(bot_scopes(roles))}


def event_subscriptions(roles: frozenset[Role]) -> frozenset[str]:
    """The Events API subscriptions the app asks for: message events only with the source role."""
    events: frozenset[str] = frozenset()
    if "interface" in roles:
        events |= INTERFACE_EVENTS
    if "source" in roles:
        events |= SOURCE_EVENTS
    return events


def _check_capture(config: PlatformConfig, rules: Optional[CaptureRules]) -> None:
    """CP-7, checked again where capture is built: the configuration is not the only way to make one."""
    if "source" not in config.roles:
        return
    where = f"Slack workspace {config.workspace}"
    if config.app != "internal":
        raise PlatformSelectionError(f'capture on {where} needs an internal app, declared as "app": "internal" (CP-7)')
    if config.slack_terms_decision is None:
        raise PlatformSelectionError(f"capture on {where} waits on the owner's recorded decision on Slack's terms (CP-7)")
    if config.capture_retention_days is None:
        raise PlatformSelectionError(f"capture on {where} needs a retention; captured evidence has no default (SD-11)")
    if rules is None:
        raise PlatformSelectionError(f"capture on {where} needs the Slack data rules to hand deletions to (RA-KNOW-014)")


def slack_revision(message: Mapping[str, Any]) -> str:
    edited = message.get("edited")
    if isinstance(edited, Mapping) and isinstance(edited.get("ts"), str) and edited["ts"]:
        return edited["ts"]
    return "0"


def slack_text(text: str) -> str:
    """``text`` as Slack will show it literally, with no mention, broadcast or hidden link."""
    for character, escaped in _ESCAPES:
        text = text.replace(character, escaped)
    return text


_M = TypeVar("_M", bound=BaseModel)


class _Payload(BaseModel):
    # Slack's payloads carry many more fields, and add new ones; the adapter reads only these.
    model_config = ConfigDict(extra="ignore", strict=True)


class _Command(_Payload):
    team_id: str
    channel_id: str
    user_id: str
    command: str
    text: str = ""
    enterprise_id: Optional[str] = None
    is_enterprise_install: Optional[str] = None


class _Mention(_Payload):
    type: Literal["app_mention"]
    user: str
    text: str
    ts: str
    channel: str
    thread_ts: Optional[str] = None
    bot_id: Optional[str] = None
    subtype: Optional[str] = None


class _EventCallback(_Payload):
    team_id: str
    event_id: str
    event: dict[str, Any]
    enterprise_id: Optional[str] = None
    is_ext_shared_channel: bool = False


class _Conversation(_Payload):
    id: str
    is_im: bool = False
    is_mpim: bool = False
    is_private: bool = False
    is_shared: bool = False
    is_ext_shared: bool = False
    is_org_shared: bool = False
    is_pending_ext_shared: bool = False


class _AuthTest(_Payload):
    team_id: str
    user_id: str


# Slack's message timestamp, which is also the message's id in its channel.
SlackTs = Annotated[str, Field(pattern=r"^[0-9]{1,12}\.[0-9]{1,6}$")]


class _Edited(_Payload):
    ts: SlackTs


class _Message(_Payload):
    ts: SlackTs
    user: Optional[str] = None
    text: str = ""
    subtype: Optional[str] = None
    bot_id: Optional[str] = None
    thread_ts: Optional[SlackTs] = None
    edited: Optional[_Edited] = None
    reply_count: int = 0
    latest_reply: Optional[SlackTs] = None

    @property
    def revision(self) -> str:
        return self.edited.ts if self.edited is not None else "0"

    @property
    def thread(self) -> Optional[str]:
        # A thread's parent carries its own ts as thread_ts. Only a reply belongs to a thread.
        return self.thread_ts if self.thread_ts not in (None, self.ts) else None


class _Changed(_Payload):
    message: _Message
    previous_message: Optional[_Message] = None


class _Deleted(_Payload):
    deleted_ts: SlackTs
    previous_message: Optional[dict[str, Any]] = None


class _History(_Payload):
    messages: list[dict[str, Any]]
    has_more: bool = False


class _State(BaseModel):
    # A cursor comes back from the caller, so it is read as untrusted input.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Window(_State):
    """A span of history being read: the channel's own messages first, then each queued thread's replies."""

    oldest: SlackTs
    latest: SlackTs
    # Set for a span whose events Slack dropped: captured messages it no longer shows are reconciled.
    reconcile: bool = False
    history_done: bool = False
    threads: tuple[SlackTs, ...] = ()
    # Slack's own cursor for the next page of whatever is being read.
    page: Optional[Annotated[str, Field(pattern=r"^[A-Za-z0-9+/=_\-]{1,512}$")]] = None
    # What the span showed, kept only when it is to be reconciled.
    seen: tuple[SlackTs, ...] = ()


class _Cursor(_State):
    """Where capture of one place resumes. Opaque to the caller, who stores it after committing a page."""

    v: Literal[1] = 1
    channel: str
    thread: Optional[str] = None
    # The journal ``position`` counts in. Another journal means a restart or an overflow.
    journal: str
    position: Annotated[int, Field(ge=0)]
    # Every message in the place sent before this has been read, from history or the journal.
    watermark: SlackTs
    # Recorded gaps that end at or before this have been read again.
    gaps_through: SlackTs
    window: Optional[_Window] = None


def _ts_key(ts: str) -> tuple[int, int]:
    # Compared as integers: a float can't hold every microsecond of a Slack timestamp exactly.
    seconds, _, micros = ts.partition(".")
    return int(seconds), int(micros.ljust(6, "0"))


def _ts_at(moment: float) -> str:
    """A wall-clock time as a Slack timestamp."""
    micros = round(moment * 1_000_000)
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def _later(first: str, second: str) -> str:
    return first if _ts_key(first) >= _ts_key(second) else second


def _earlier(first: str, second: str) -> str:
    return first if _ts_key(first) <= _ts_key(second) else second


@dataclass(frozen=True)
class _Pending:
    interaction: Interaction
    # Wall-clock seconds after which the question is stale and is forgotten unanswered.
    expires_at: float


def _seconds(moment: float) -> datetime:
    return datetime.fromtimestamp(moment, tz=timezone.utc)


class SlackAdapter:
    """One Slack workspace, in the roles the operator enabled, as a ``ChatPlatform``. Build it with ``slack_factory``."""

    # ponytail: not thread-safe, like the client and the receiver it drives. One caller at a time.

    def __init__(
        self,
        config: PlatformConfig,
        client: SlackClient,
        receiver: SocketModeReceiver,
        *,
        clock: Callable[[], float] = time.time,
        rules: Optional[CaptureRules] = None,
    ) -> None:
        if config.kind != "slack":
            raise PlatformSelectionError(f"the Slack adapter can't serve a {config.kind} workspace")
        _check_capture(config, rules)
        if client.workspace != config.workspace or receiver.workspace != config.workspace:
            raise PlatformSelectionError(f"the Slack client and receiver must both serve workspace {config.workspace}")
        self._config = config
        self._client = client
        self._receiver = receiver
        self._clock = clock
        self._questions: deque[Interaction] = deque()
        self._pending: dict[str, _Pending] = {}
        # Why deliveries were dropped, by reason only: never the text, which SD-5 says not to keep.
        self.dropped: Counter[str] = Counter()
        self._rules = rules
        # The capture journal: message events from source-allowlisted channels since it began.
        self._journal: deque[tuple[int, CaptureEvent]] = deque()
        self._seq = 0
        self._dropped_through = 0
        self._epoch = 0
        # A cursor from another adapter, or from before a restart, must not count in this journal.
        self._session = secrets.token_hex(8)
        self._journal_start = clock()
        # The token is the one thing that says which workspace it really serves, so it is checked
        # before anything is received.
        identity = self._parse(_AuthTest, self._call("auth.test"), "auth.test")
        if identity.team_id != config.workspace:
            raise PlatformSelectionError(f"the bot token serves another workspace than Slack workspace {config.workspace}")
        self._bot = identity.user_id
        self._bot_mention = re.compile(rf"<@{re.escape(self._bot)}(?:\|[^>]*)?>")
        receiver.start()

    # ChatPlatform

    @property
    def kind(self) -> PlatformKind:
        return "slack"

    @property
    def workspace(self) -> str:
        return self._config.workspace

    @property
    def roles(self) -> frozenset[Role]:
        return self._config.roles

    @property
    def scopes(self) -> frozenset[str]:
        return bot_scopes(self.roles)

    @property
    def pending_questions(self) -> int:
        """Questions received and not yet answered or expired. Their text is held only this long (SD-5)."""
        self._expire()
        return len(self._pending)

    def interactions(self) -> Iterator[Interaction]:
        # Checked here rather than inside the generator, so the refusal comes from the call itself.
        require_role(self, "interface")
        self._pump()
        return self._drain()

    def reply(self, interaction: Interaction, answer: Answer, visibility: Visibility) -> Delivery:
        require_role(self, "interface")
        require_own(self, interaction)
        checked = check_visibility(visibility)
        if checked not in self._config.answer_audiences:
            raise VisibilityRefused(f"the operator allows answers only as {sorted(self._config.answer_audiences)}")
        self._expire()
        pending = self._pending.get(interaction.id)
        # The whole interaction must match, so a copy pointed at another place or person is refused.
        if pending is None or pending.interaction != interaction:
            raise PlatformError(f"interaction {interaction.id} was not received here, was already answered, or expired")
        text = self._message_text(answer.text)
        place = interaction.place
        # Access can change while a question waits, so the place is checked again now.
        self._check_interface_place(place)
        params: dict[str, Any] = {"channel": place.channel, "text": text, **_LITERAL}
        if place.thread is not None:
            params["thread_ts"] = place.thread
        if checked == "ephemeral":
            method = "chat.postEphemeral"
            params["user"] = interaction.principal.user
        else:
            method = "chat.postMessage"
            params |= _NO_UNFURL
        # The question is answered, or abandoned, once: from here it is never sent twice (SD-5).
        del self._pending[interaction.id]
        sent = self._post(method, params, "the reply")
        return Delivery(place=place, message=self._message_ts(sent, method))

    def can_see(self, principal: Principal, source_ref: SourceRef) -> bool:
        own = (self.kind, self.workspace)
        if (principal.platform, principal.workspace) != own or (source_ref.platform, source_ref.workspace) != own:
            return False
        try:
            conversation = self._conversation(source_ref.channel)
            if _excluded(conversation) is not None:
                return False
            message = self._read_message(source_ref)
            if message is None or slack_revision(message) != source_ref.revision:
                return False
            return principal.user in self._members(source_ref.channel)
        except SlackCallError as error:
            # An interface-only app has no history scope, so for it Slack answers missing_scope and
            # nothing is ever confirmed. Failing closed is the point: it holds no Slack evidence.
            if error.error_class is ErrorClass.ACCESS_GONE or error.error in _UNREADABLE:
                return False
            raise PlatformError(f"Slack couldn't say who can see message {source_ref.message}: {error}") from None

    def audience(self, place: Place) -> Audience:
        require_own(self, place)
        try:
            reason = _excluded(self._conversation(place.channel))
            if reason is not None:
                raise ExcludedConversation(f"Slack conversation {place.channel} is {reason}")
            members = self._members(place.channel)
        except SlackCallError as error:
            raise PlatformError(f"Slack conversation {place.channel} can't be read: {error}") from None
        # A thread is readable by everyone in its channel. The bot is the one speaking, not part of
        # who it speaks to.
        return Audience(place=place, members=frozenset(self._principal(user) for user in members if user != self._bot))

    def fetch_since(self, place: Place, cursor: Optional[str]) -> CapturePage:
        require_role(self, "source")
        require_own(self, place)
        # Refused on the ids alone first, so a channel nobody allowlisted costs no call at all.
        if place.channel not in self._config.channels.source:
            raise NotAllowlisted(f"Slack channel {place.channel} is not allowlisted for capture")
        state = self._read_cursor(place, cursor)
        self._pump()
        try:
            # A channel can become shared with another organization at any time, so every page checks.
            self._check_place(place, capture=True)
            return self._capture(place, state)
        except _Throttled as throttled:
            return CapturePage(place=place, events=(), cursor=cursor, complete=False, retry_after_seconds=throttled.seconds)

    def publish(self, destination: Place, candidate: ShareCandidate, receipt: Optional[ApprovalReceipt]) -> Delivery:
        require_role(self, "interface")
        require_own(self, destination)
        check_receipt(destination, candidate, receipt)
        if ShareDestination(channel=destination.channel, thread=destination.thread) not in self._config.share_destinations:
            raise NotAllowlisted(f"Slack channel {destination.channel} is not a share destination")
        text = self._message_text(candidate.text)
        self._check_place(destination)
        params: dict[str, Any] = {
            "channel": destination.channel,
            "text": text,
            # Whoever retries a share reads the destination for this first (part 2, Idempotence).
            "metadata": {
                "event_type": "boundroom_share",
                "event_payload": {"share_id": candidate.id, "candidate_sha256": candidate.digest()},
            },
            **_LITERAL,
            **_NO_UNFURL,
        }
        if destination.thread is not None:
            params["thread_ts"] = destination.thread
        sent = self._post("chat.postMessage", params, "the share")
        return Delivery(place=destination, message=self._message_ts(sent, "chat.postMessage"))

    # Inbound

    def _pump(self) -> None:
        while True:
            # Checking a question calls Slack, which can wait on the limiter. Polling before each one
            # acknowledges whatever arrived meanwhile, so no wait stacks up against Slack's deadline.
            try:
                self._receiver.poll()
            except InboundError as error:
                raise PlatformError(f"Slack Socket Mode delivery stopped for workspace {self.workspace}: {error}") from None
            work = self._receiver.next_work()
            if work is None:
                return
            if not work.question:
                self._capture_work(work)
                continue
            if "interface" not in self.roles:
                # Nobody can collect it, so holding its text would only keep it (SD-5).
                self._drop("interface role not enabled")
                continue
            interaction = self._question(work)
            if interaction is not None:
                self._pending[interaction.id] = _Pending(interaction, work.origin_at + STALE_QUESTION_SECONDS)
                self._questions.append(interaction)

    def _drain(self) -> Iterator[Interaction]:
        while self._questions:
            yield self._questions.popleft()

    def _question(self, work: InboundWork) -> Optional[Interaction]:
        if work.source == "slash_commands":
            asked = self._from_command(work)
        else:
            asked = self._from_mention(work)
        if asked is None:
            return None
        via, ident, user, channel, thread, text = asked
        if not text:
            return self._drop("empty question")
        try:
            interaction = Interaction(
                id=ident,
                via=via,
                principal=self._principal(user),
                place=Place(platform="slack", workspace=self.workspace, channel=channel, thread=thread),
                text=text,
                received_at=_seconds(work.origin_at),
            )
        except ValidationError:
            return self._drop("malformed id")
        try:
            self._check_interface_place(interaction.place)
        except NotAllowlisted:
            return self._drop("channel not allowlisted")
        except ExcludedConversation:
            return self._drop("excluded conversation")
        except PlatformError:
            return self._drop("channel check failed")
        return interaction

    def _from_command(self, work: InboundWork) -> Optional[tuple[Literal["command"], str, str, str, None, str]]:
        try:
            command = _Command.model_validate(work.payload)
        except ValidationError:
            return self._drop("malformed command")
        # Only the one command the app defines asks anything. Any other name is not a request to do
        # something else; it does nothing.
        if command.command != ASK_COMMAND:
            return self._drop("unknown command")
        if command.team_id != self.workspace:
            return self._drop("another workspace")
        if command.enterprise_id or command.is_enterprise_install == "true":
            return self._drop("enterprise install")
        # Slack can't run a slash command in a thread, so a command's place is always its channel.
        return "command", work.envelope_id, command.user_id, command.channel_id, None, command.text.strip()

    def _from_mention(self, work: InboundWork) -> Optional[tuple[Literal["mention"], str, str, str, Optional[str], str]]:
        try:
            callback = _EventCallback.model_validate(work.payload)
            mention = _Mention.model_validate(callback.event)
        except ValidationError:
            return self._drop("malformed mention")
        if callback.team_id != self.workspace:
            return self._drop("another workspace")
        if callback.enterprise_id or callback.is_ext_shared_channel:
            return self._drop("enterprise or Slack Connect delivery")
        if mention.bot_id is not None or mention.subtype is not None or mention.user == self._bot:
            # Bots, integrations and the adapter's own messages can't ask.
            return self._drop("not a person")
        thread = mention.thread_ts if mention.thread_ts not in (None, mention.ts) else None
        text = self._bot_mention.sub("", mention.text).strip()
        return "mention", callback.event_id, mention.user, mention.channel, thread, text

    def _drop(self, reason: str) -> None:
        self.dropped[reason] += 1
        log.info("slack workspace %s: dropped a delivery: %s", self.workspace, reason)
        return None

    def _expire(self) -> None:
        now = self._clock()
        for ident in [ident for ident, pending in self._pending.items() if pending.expires_at < now]:
            del self._pending[ident]

    # Places

    def _check_interface_place(self, place: Place) -> None:
        if place.channel not in self._config.channels.interface:
            raise NotAllowlisted(f"Slack channel {place.channel} is not allowlisted for questions")
        self._check_place(place)

    def _check_place(self, place: Place, *, capture: bool = False) -> None:
        # A DM's id says what it is, so it costs no call.
        if place.channel.startswith("D"):
            raise ExcludedConversation(f"Slack conversation {place.channel} is a direct message")
        try:
            conversation = self._conversation(place.channel, budget=CAPTURE_PAGE_BUDGET if capture else QUESTION_BUDGET)
        except SlackCallError as error:
            if capture:
                self._capture_failure(place, error)
            raise PlatformError(f"Slack conversation {place.channel} can't be checked: {error}") from None
        reason = _excluded(conversation)
        if reason is not None:
            raise ExcludedConversation(f"Slack conversation {place.channel} is {reason}")

    def _conversation(self, channel: str, *, budget: RetryBudget = QUESTION_BUDGET) -> _Conversation:
        if channel.startswith("D"):
            return _Conversation(id=channel, is_im=True)
        found = self._client.call("conversations.info", {"channel": channel}, budget=budget)
        conversation = self._parse(_Conversation, found.get("channel"), "conversations.info")
        if conversation.id != channel:
            raise PlatformError(f"Slack answered for conversation {conversation.id} when asked about {channel}")
        return conversation

    def _members(self, channel: str) -> frozenset[str]:
        members: set[str] = set()
        cursor = ""
        for _ in range(MEMBER_PAGES):
            params: dict[str, Any] = {"channel": channel, "limit": MEMBER_PAGE}
            if cursor:
                params["cursor"] = cursor
            page = self._call("conversations.members", params)
            listed = page.get("members")
            if not isinstance(listed, list) or not all(isinstance(user, str) for user in listed):
                raise PlatformError("Slack's conversations.members answer has no member list")
            members.update(listed)
            cursor = _next_cursor(page)
            if not cursor:
                return frozenset(members)
        raise PlatformError(f"Slack conversation {channel} has more than {MEMBER_PAGE * MEMBER_PAGES} members")

    def _read_message(self, ref: SourceRef) -> Optional[Mapping[str, Any]]:
        """The message as Slack has it now, or None if it is gone. One message, never a page of history."""
        params: dict[str, Any] = {"channel": ref.channel, "latest": ref.message, "oldest": ref.message, "inclusive": True}
        if ref.thread is None or ref.thread == ref.message:
            found = self._call("conversations.history", {**params, "limit": 1})
        else:
            # conversations.replies always lists the thread's parent first, so one more than the
            # reply itself.
            found = self._call("conversations.replies", {**params, "ts": ref.thread, "limit": 2})
        messages = found.get("messages")
        if not isinstance(messages, list):
            return None
        for message in messages:
            if isinstance(message, Mapping) and message.get("ts") == ref.message:
                # Slack keeps a deleted parent with replies as a tombstone.
                return None if message.get("subtype") == "tombstone" else message
        return None

    # Capture: live events

    def _capture_work(self, work: InboundWork) -> None:
        if "source" not in self.roles:
            # Message, edit and deletion events need history scopes this app doesn't ask for. One
            # delivered anyway is dropped: the interface role captures nothing (CP-3).
            self._drop("not a question")
            return
        try:
            callback = _EventCallback.model_validate(work.payload)
        except ValidationError:
            self._drop("malformed event")
            return
        event = callback.event
        kind = event.get("type")
        if callback.team_id != self.workspace:
            self._drop("another workspace")
        elif kind in ("app_uninstalled", "tokens_revoked"):
            # SD-1: the data rules purge everything from this workspace.
            self._hand_off({"type": kind})
        elif kind != "message":
            self._drop("not a capture event")
        elif event.get("channel") not in self._config.channels.source:
            self._drop("channel not allowlisted for capture")
        elif (
            callback.enterprise_id
            or callback.is_ext_shared_channel
            or event.get("channel_type") not in (None, "channel", "group")
        ):
            self._drop("excluded conversation")
        else:
            self._capture_message_event(str(event["channel"]), event, work.origin_at)

    def _capture_message_event(self, channel: str, event: Mapping[str, Any], origin_at: float) -> None:
        try:
            captured = self._live_event(channel, event, origin_at)
        except ValidationError:
            # Capture can't vouch for what came after an event it couldn't read, so every cursor into
            # this journal reads history again from its watermark.
            self._drop("unreadable message event")
            self._restart_journal()
            return
        if captured is None:
            self._drop("not captured")
            return
        if captured.kind != "delete" and self._expired(captured.source.message):
            # Older than retention: neither captured nor kept (SD-11). A deletion always propagates.
            self._drop("past retention")
            return
        self._record(captured)
        if captured.kind == "delete":
            # Withdrawn from answers at once (SD-2), not when the page is next read. Only the ids go:
            # the deleted message's text stays behind.
            self._hand_off(
                {"type": "message", "subtype": "message_deleted", "channel": channel, "deleted_ts": captured.source.message}
            )

    def _live_event(self, channel: str, event: Mapping[str, Any], origin_at: float) -> Optional[CaptureEvent]:
        subtype = event.get("subtype")
        if subtype == "message_deleted":
            deleted = _Deleted.model_validate(event)
            previous = deleted.previous_message or {}
            thread_ts = previous.get("thread_ts")
            thread = thread_ts if isinstance(thread_ts, str) and thread_ts != deleted.deleted_ts else None
            return self._deletion(channel, thread, deleted.deleted_ts, slack_revision(previous), origin_at)
        if subtype == "message_changed":
            changed = _Changed.model_validate(event)
            message, previous = changed.message, changed.previous_message
            if message.subtype == "tombstone":
                # A deleted parent that still has replies stays as a tombstone: it is a deletion.
                revision = previous.revision if previous is not None else "0"
                return self._deletion(channel, message.thread, message.ts, revision, origin_at)
            # Slack also sends message_changed when a reply is added or a link unfurls. Only a new
            # edited.ts is a new revision.
            if message.edited is None or (previous is not None and previous.revision == message.revision):
                return None
            if not self._capturable(message):
                return None
            return self._message_event("edit", channel, message, float(message.edited.ts))
        message = _Message.model_validate(event)
        if not self._capturable(message):
            return None
        return self._message_event("message", channel, message, float(message.ts))

    def _capturable(self, message: _Message) -> bool:
        # Bots, integrations, system notices and the adapter's own messages aren't evidence.
        return (
            message.user is not None
            and message.bot_id is None
            and message.user != self._bot
            and (message.subtype is None or message.subtype in CAPTURED_SUBTYPES)
        )

    def _message_event(self, kind: Literal["message", "edit"], channel: str, message: _Message, at: float) -> CaptureEvent:
        source = SourceRef(
            platform="slack",
            workspace=self.workspace,
            channel=channel,
            thread=message.thread,
            message=message.ts,
            revision=message.revision,
        )
        return CaptureEvent(
            kind=kind, source=source, author=self._principal(str(message.user)), text=message.text, at=_seconds(at)
        )

    def _deletion(self, channel: str, thread: Optional[str], ts: str, revision: str, at: float) -> CaptureEvent:
        source = SourceRef(
            platform="slack", workspace=self.workspace, channel=channel, thread=thread, message=ts, revision=revision
        )
        return CaptureEvent(kind="delete", source=source, at=_seconds(at))

    def _hand_off(self, event: Mapping[str, Any]) -> None:
        if self._rules is None:
            return
        try:
            self._rules.on_event(self.workspace, event)
        except Exception as error:  # the data rules are outside this module; a failure must not pass silently
            raise PlatformError(f"the Slack data rules refused a {event.get('type')} event: {error}") from error

    def _record(self, event: CaptureEvent) -> None:
        self._seq += 1
        self._journal.append((self._seq, event))
        if len(self._journal) > JOURNAL_LIMIT:
            self._restart_journal()

    def _restart_journal(self) -> None:
        self._journal.clear()
        self._dropped_through = self._seq
        self._epoch += 1
        self._journal_start = self._clock()

    # Capture: pages

    @property
    def _journal_id(self) -> str:
        return f"{self._session}.{self._epoch}"

    def _horizon(self) -> str:
        """The oldest timestamp still within retention: the shorter of the operator's and the workspace's."""
        days = self._config.capture_retention_days
        assert days is not None  # _check_capture refuses a source role without one
        return _ts_at(self._clock() - days * DAY_SECONDS)

    def _expired(self, ts: str) -> bool:
        return _ts_key(ts) < _ts_key(self._horizon())

    def _capture(self, place: Place, state: Optional[_Cursor]) -> CapturePage:
        horizon = self._horizon()
        start = _ts_at(self._journal_start)
        if state is None:
            state = _Cursor(
                channel=place.channel,
                thread=place.thread,
                journal=self._journal_id,
                position=self._dropped_through,
                watermark=horizon,
                gaps_through=start,
                window=self._window(place, horizon, start),
            )
        elif state.journal != self._journal_id:
            # The journal this cursor counted in is gone. Read what it may have missed from history.
            oldest = _later(_ts_at(float(state.watermark) - RESUME_OVERLAP_SECONDS), horizon)
            state = state.model_copy(
                update={
                    "journal": self._journal_id,
                    "position": self._dropped_through,
                    "gaps_through": start,
                    "window": self._window(place, oldest, start),
                }
            )
        gaps = self._receiver.gap_ledger.gaps
        # While Slack is dropping events, capture slows to a page a minute (part 2, "Timers and budgets").
        slowed = bool(gaps) and not gaps[-1].closed
        if state.window is None:
            pending = self._pending_gap(state)
            if pending is not None:
                oldest, latest = _later(_ts_at(pending.start), horizon), _ts_at(pending.end)
                window = self._window(place, oldest, latest, reconcile=place.thread is None)
                state = state.model_copy(update={"gaps_through": latest, "window": window})
        retry = RATE_LIMIT_NOTICE_SECONDS if slowed else None
        events: list[CaptureEvent] = []
        if state.window is not None:
            found, window = self._read_window(place, state.window, horizon)
            events.extend(found)
            if window is not None:
                state = state.model_copy(update={"window": window})
                return CapturePage(
                    place=place, events=tuple(events), cursor=self._cursor(state), complete=False, retry_after_seconds=retry
                )
            # The span is read through, so the watermark can move past it if it reached the span.
            # Nothing older than the horizon is read at all, so a watermark behind it counts as there.
            watermark = _later(state.watermark, horizon)
            if _ts_key(watermark) >= _ts_key(_later(state.window.oldest, horizon)):
                watermark = _later(watermark, state.window.latest)
            state = state.model_copy(update={"window": None, "watermark": watermark})
        found, position, exhausted, newest = self._journal_page(place, state.position)
        events.extend(found)
        watermark = state.watermark if newest is None else _later(state.watermark, newest)
        # Never past a span Slack dropped that hasn't been read again, so a restart still reads it.
        for gap in gaps:
            if not gap.closed or _ts_key(_ts_at(gap.end)) > _ts_key(state.gaps_through):
                watermark = _earlier(watermark, _ts_at(gap.start))
        state = state.model_copy(update={"position": position, "watermark": watermark})
        complete = exhausted and not slowed and self._pending_gap(state) is None
        return CapturePage(
            place=place, events=tuple(events), cursor=self._cursor(state), complete=complete, retry_after_seconds=retry
        )

    def _pending_gap(self, state: _Cursor) -> Optional[Gap]:
        for gap in self._receiver.gap_ledger.gaps:
            if gap.closed and _ts_key(_ts_at(gap.end)) > _ts_key(state.gaps_through):
                return gap
        return None

    def _window(self, place: Place, oldest: str, latest: str, *, reconcile: bool = False) -> Optional[_Window]:
        if _ts_key(oldest) >= _ts_key(latest):
            return None
        if place.thread is not None:
            # A thread's place reads only that thread's replies.
            return _Window(oldest=oldest, latest=latest, history_done=True, threads=(place.thread,))
        return _Window(oldest=oldest, latest=latest, reconcile=reconcile)

    def _read_window(self, place: Place, window: _Window, horizon: str) -> tuple[list[CaptureEvent], Optional[_Window]]:
        """One page of a span's history or replies, and the span's state after it, or None once it is read through."""
        # Retention may have moved on since the span was planned, even past all of it.
        oldest = _later(window.oldest, horizon)
        if _ts_key(oldest) > _ts_key(window.latest):
            return [], None
        events: list[CaptureEvent] = []
        if not window.history_done or window.threads:
            params: dict[str, Any] = {
                "channel": place.channel,
                "oldest": oldest,
                "latest": window.latest,
                "inclusive": True,
                "limit": HISTORY_PAGE,
            }
            if window.page is not None:
                params["cursor"] = window.page
            reading = None if not window.history_done else window.threads[0]
            if reading is None:
                data = self._capture_call(place, "conversations.history", params)
            else:
                data = self._capture_call(place, "conversations.replies", {**params, "ts": reading})
            page = self._parse(_History, data, "history")
            seen = list(window.seen)
            threads = list(window.threads)
            for raw in page.messages:
                try:
                    message = _Message.model_validate(raw)
                except ValidationError:
                    raise CaptureFailed(
                        f"Slack's history for channel {place.channel} holds a message in an unknown shape"
                    ) from None
                in_span = _ts_key(oldest) <= _ts_key(message.ts) <= _ts_key(window.latest)
                if not in_span or (reading is not None and (message.ts == reading or message.thread_ts != reading)):
                    # Out of the span asked for, or the thread's parent, which replies always list first.
                    continue
                if window.reconcile:
                    seen.append(message.ts)
                if (
                    reading is None
                    and message.thread is None
                    and message.reply_count > 0
                    and message.latest_reply is not None
                    and _ts_key(message.latest_reply) >= _ts_key(oldest)
                    and message.ts not in threads
                ):
                    threads.append(message.ts)
                if self._capturable(message):
                    events.append(self._message_event("message", place.channel, message, float(message.ts)))
            following = _next_cursor(data) if page.has_more else ""
            if page.has_more and (not following or following == window.page):
                raise CaptureFailed(f"Slack's history for channel {place.channel} did not advance")
            update: dict[str, Any] = {"page": following or None, "seen": tuple(seen), "threads": tuple(threads)}
            if not following:
                if reading is None:
                    update["history_done"] = True
                else:
                    update["threads"] = tuple(threads[1:])
            window = window.model_copy(update=update)
            events.sort(key=lambda event: _ts_key(event.source.message))
            if not window.history_done or window.threads:
                return events, window
        if window.reconcile:
            self._reconcile(place, window, oldest)
        return events, None

    def _reconcile(self, place: Place, window: _Window, oldest: str) -> None:
        """Withdraw captured messages the span no longer shows: deletions whose events Slack dropped (SD-2)."""
        if self._rules is None:
            return
        present = set(window.seen)
        # History can't show a reply to a thread that started before the span. One delivered live is
        # counted present, so it isn't withdrawn for that alone.
        for _, event in self._journal:
            source = event.source
            if source.channel == place.channel and source.thread is not None and event.kind != "delete":
                present.add(source.message)
        try:
            self._rules.reconcile(self.workspace, place.channel, frozenset(present), float(oldest), float(window.latest))
        except Exception as error:  # see _hand_off
            raise PlatformError(f"the Slack data rules couldn't reconcile channel {place.channel}: {error}") from error

    def _journal_page(self, place: Place, position: int) -> tuple[list[CaptureEvent], int, bool, Optional[str]]:
        if position < self._dropped_through or position > self._seq:
            raise PlatformError(f"cursor position {position} is not in this capture journal")
        horizon = _ts_key(self._horizon())
        # Text that aged past retention while it waited is not kept (SD-11). Deletions carry none.
        self._journal = deque(
            (seq, event) for seq, event in self._journal if event.kind == "delete" or _ts_key(event.source.message) >= horizon
        )
        events: list[CaptureEvent] = []
        last = position
        newest: Optional[str] = None
        for seq, event in self._journal:
            if seq <= position:
                continue
            if len(events) == JOURNAL_PAGE:
                return events, last, False, newest
            last = seq
            source = event.source
            if source.channel == place.channel and (place.thread is None or source.thread == place.thread):
                events.append(event)
                if event.kind == "message":
                    newest = source.message if newest is None else _later(newest, source.message)
        return events, self._seq, True, newest

    def _capture_call(self, place: Place, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._client.call(method, params, budget=CAPTURE_PAGE_BUDGET)
        except SlackCallError as error:
            self._capture_failure(place, error)

    def _capture_failure(self, place: Place, error: SlackCallError) -> NoReturn:
        if error.error_class is ErrorClass.THROTTLED:
            wait = error.retry_after_seconds
            raise _Throttled(wait if wait is not None and wait > 0 else RETRY_AFTER_MISSING_SECONDS) from None
        if error.error_class is ErrorClass.ACCESS_GONE:
            raise CaptureFailed(f"Slack channel {place.channel} can no longer be read: {error}") from None
        raise CaptureFailed(f"capture of Slack channel {place.channel} failed for this run: {error}") from None

    def _cursor(self, state: _Cursor) -> str:
        return state.model_dump_json(exclude_defaults=True)

    def _read_cursor(self, place: Place, cursor: Optional[str]) -> Optional[_Cursor]:
        if cursor is None:
            return None
        try:
            state = _Cursor.model_validate_json(cursor)
        except ValidationError:
            raise PlatformError("the capture cursor was not issued by this adapter") from None
        # An edited cursor can't point capture at another channel or thread than the one asked for.
        if (state.channel, state.thread) != (place.channel, place.thread):
            raise PlatformError(f"the capture cursor was issued for another place than Slack channel {place.channel}")
        return state

    # Calls

    def _call(self, method: str, params: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self._client.call(method, params, budget=QUESTION_BUDGET)

    def _post(self, method: str, params: Mapping[str, Any], what: str) -> dict[str, Any]:
        try:
            return self._call(method, params)
        except SlackCallError as error:
            if error.outcome_unknown:
                raise OutcomeUnknown(f"Slack may or may not have posted {what}, so it is not retried: {error}") from None
            raise PlatformError(f"Slack refused {what}: {error}") from None

    def _message_text(self, text: str) -> str:
        escaped = slack_text(text)
        if len(escaped) > MESSAGE_LIMIT:
            raise PlatformError(f"Slack shows at most {MESSAGE_LIMIT} characters in a message, not {len(escaped)}")
        return escaped

    def _message_ts(self, sent: Mapping[str, Any], method: str) -> str:
        # chat.postMessage answers with ``ts``, chat.postEphemeral with ``message_ts``.
        ts = sent.get("message_ts" if method == "chat.postEphemeral" else "ts")
        if not isinstance(ts, str) or not ts:
            raise PlatformError(f"Slack's {method} answer names no message")
        return ts

    def _parse(self, model: type[_M], data: Any, what: str) -> _M:
        try:
            return model.model_validate(data)
        except ValidationError:
            raise PlatformError(f"Slack's {what} answer is not in the shape Slack documents") from None

    def _principal(self, user: str) -> Principal:
        return Principal(platform="slack", workspace=self.workspace, user=user)


# Sent with every message: no name linking and no markup parsing, on top of the escaping.
_LITERAL: Mapping[str, Any] = {"link_names": False, "parse": "none"}
# A visible message must not pull in a preview of a link it quotes.
_NO_UNFURL: Mapping[str, Any] = {"unfurl_links": False, "unfurl_media": False}


def _excluded(conversation: _Conversation) -> Optional[str]:
    if conversation.is_im:
        return "a direct message"
    if conversation.is_mpim:
        return "a group direct message"
    if conversation.is_shared or conversation.is_ext_shared or conversation.is_org_shared or conversation.is_pending_ext_shared:
        return "shared with another organization (Slack Connect)"
    return None


def _next_cursor(page: Mapping[str, Any]) -> str:
    metadata = page.get("response_metadata")
    cursor = metadata.get("next_cursor") if isinstance(metadata, Mapping) else None
    return cursor if isinstance(cursor, str) else ""


# The factory

WebConnect = Callable[[SecretStr], Transport]
"""Opens a Web API transport that sends every request with this token."""


class SocketOpener(Protocol):
    """Opens a Socket Mode WebSocket at the URL ``apps.connections.open`` returned."""

    def __call__(self, url: str) -> SocketConnection: ...


def slack_factory(
    settings: KnowledgeSettings,
    web: WebConnect,
    open_socket: SocketOpener,
    *,
    environ: Optional[Mapping[str, str]] = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    random: Callable[[], float] = _random.random,
    rules: Optional[CaptureRules] = None,
) -> PlatformFactory:
    """The factory ``start_platforms`` calls for a Slack selection.

    Building the factory reads nothing. Calling it checks the selection, and for the source role
    checks CP-7 and that ``rules`` is there to hand deletions to, then reads the bot token and the
    app-level token with ``config.read_secret`` and hands each to its own Web API transport.
    ``apps.connections.open`` goes through the app token's client, so opening a connection is
    throttled like every other call. The adapter never sees either token.
    """

    def build(selection: PlatformSelection) -> ChatPlatform:
        if selection.kind != "slack":
            raise PlatformSelectionError(f"the Slack factory was handed a {selection.kind} selection")
        platform = settings.platform(selection)
        # Checked before any token is read, so a selection the operator didn't write, or capture
        # without the terms decision or the data rules, connects nothing.
        if platform.selection() != selection:
            raise PlatformSelectionError(f"Slack workspace {selection.workspace} is configured with other roles")
        _check_capture(platform, rules)
        bot_ref, app_ref = platform.credentials.bot_token, platform.credentials.app_token
        if bot_ref is None or app_ref is None:
            raise PlatformSelectionError(f"Slack workspace {selection.workspace} needs both a bot and an app-level token")
        bot = knowledge_config.read_secret(bot_ref, environ)
        app = knowledge_config.read_secret(app_ref, environ)
        client = SlackClient(platform.workspace, web(bot), clock=monotonic, sleep=sleep, random=random, methods=ADAPTER_METHODS)
        connections = SlackClient(platform.workspace, web(app), clock=monotonic, sleep=sleep, random=random)

        def connect() -> SocketConnection:
            try:
                opened = connections.call("apps.connections.open", budget=CONNECTION_BUDGET)
            except SlackCallError as error:
                raise ConnectionOpenFailed(str(error)) from None
            url = opened.get("url")
            if not isinstance(url, str) or not url.startswith("wss://"):
                raise ConnectionOpenFailed("apps.connections.open returned no WebSocket URL")
            return open_socket(url)

        receiver = SocketModeReceiver(platform.workspace, connect, clock=clock, random=random)
        return SlackAdapter(platform, client, receiver, clock=clock, rules=rules)

    return build
