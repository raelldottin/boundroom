"""The Slack adapter behind ``ChatPlatform``, in the ``interface`` role (SPEC-007, RA-KNOW-009).

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

- **Interface only (CP-3).** The ``source`` role is RA-KNOW-011, which waits on the owner's reading
  of Slack's API terms (SPEC-007 decision 5). The factory refuses a selection that includes it before
  reading any token, even when the configuration records that decision, so ``fetch_since`` is always
  refused by the contract's role check. ``bot_scopes`` asks for no history scope and
  ``event_subscriptions`` for no message event, and a message event delivered anyway is dropped.
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
"""

from __future__ import annotations

import logging
import random as _random
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from automation.knowledge import config as knowledge_config
from automation.knowledge.config import KnowledgeSettings, PlatformConfig, ShareDestination
from automation.knowledge.platform import (
    Answer,
    ApprovalReceipt,
    Audience,
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
    QUESTION_BUDGET,
    ErrorClass,
    MethodSpec,
    RetryBudget,
    SlackCallError,
    SlackClient,
    Transport,
)
from automation.knowledge.slack_inbound import (
    STALE_QUESTION_SECONDS,
    ConnectionOpenFailed,
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
# Requested only by the source role, which this adapter doesn't build.
HISTORY_SCOPES: frozenset[str] = frozenset({"channels:history", "groups:history", "im:history", "mpim:history"})
# Socket Mode's app-level token needs only this.
APP_TOKEN_SCOPES: frozenset[str] = frozenset({"connections:write"})
# Asking arrives as an app_mention event or a slash command. Slash commands need no subscription.
INTERFACE_EVENTS: frozenset[str] = frozenset({"app_mention"})

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


def bot_scopes(roles: frozenset[Role]) -> frozenset[str]:
    """The bot token scopes the app asks for, for the roles this adapter builds."""
    _refuse_source(roles)
    return INTERFACE_SCOPES


def event_subscriptions(roles: frozenset[Role]) -> frozenset[str]:
    """The Events API subscriptions the app asks for. No message event: they need history scopes."""
    _refuse_source(roles)
    return INTERFACE_EVENTS


def _refuse_source(roles: frozenset[Role]) -> None:
    if "source" in roles:
        raise PlatformSelectionError(
            "Slack's source role is not built: it is RA-KNOW-011, which waits on the owner's recorded reading "
            "of Slack's API terms (SPEC-007 decision 5 and open decision 2)"
        )


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


@dataclass(frozen=True)
class _Pending:
    interaction: Interaction
    # Wall-clock seconds after which the question is stale and is forgotten unanswered.
    expires_at: float


def _seconds(moment: float) -> datetime:
    return datetime.fromtimestamp(moment, tz=timezone.utc)


class SlackAdapter:
    """One Slack workspace, in the ``interface`` role, as a ``ChatPlatform``. Build it with ``slack_factory``."""

    # ponytail: not thread-safe, like the client and the receiver it drives. One caller at a time.

    def __init__(
        self,
        config: PlatformConfig,
        client: SlackClient,
        receiver: SocketModeReceiver,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if config.kind != "slack":
            raise PlatformSelectionError(f"the Slack adapter can't serve a {config.kind} workspace")
        _refuse_source(config.roles)
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
        # The factory never builds the source role, so this always refuses, before any call.
        require_role(self, "source")
        raise PlatformSelectionError("Slack's source role is not built (RA-KNOW-011)")

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
            interaction = self._question(work)
            if interaction is not None:
                self._pending[interaction.id] = _Pending(interaction, work.origin_at + STALE_QUESTION_SECONDS)
                self._questions.append(interaction)

    def _drain(self) -> Iterator[Interaction]:
        while self._questions:
            yield self._questions.popleft()

    def _question(self, work: InboundWork) -> Optional[Interaction]:
        if not work.question:
            # Message, edit and deletion events need history scopes this app doesn't ask for. One
            # delivered anyway is dropped: the interface role captures nothing (CP-3).
            return self._drop("not a question")
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

    def _check_place(self, place: Place) -> None:
        # A DM's id says what it is, so it costs no call.
        if place.channel.startswith("D"):
            raise ExcludedConversation(f"Slack conversation {place.channel} is a direct message")
        try:
            conversation = self._conversation(place.channel)
        except SlackCallError as error:
            raise PlatformError(f"Slack conversation {place.channel} can't be checked: {error}") from None
        reason = _excluded(conversation)
        if reason is not None:
            raise ExcludedConversation(f"Slack conversation {place.channel} is {reason}")

    def _conversation(self, channel: str) -> _Conversation:
        if channel.startswith("D"):
            return _Conversation(id=channel, is_im=True)
        found = self._call("conversations.info", {"channel": channel})
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
) -> PlatformFactory:
    """The factory ``start_platforms`` calls for a Slack selection.

    Building the factory reads nothing. Calling it checks the selection, refuses the source role,
    then reads the bot token and the app-level token with ``config.read_secret`` and hands each to
    its own Web API transport. ``apps.connections.open`` goes through the app token's client, so
    opening a connection is throttled like every other call. The adapter never sees either token.
    """

    def build(selection: PlatformSelection) -> ChatPlatform:
        if selection.kind != "slack":
            raise PlatformSelectionError(f"the Slack factory was handed a {selection.kind} selection")
        platform = settings.platform(selection)
        # Checked before any token is read, so a selection the operator didn't write, or a role this
        # adapter doesn't build, connects nothing.
        if platform.selection() != selection:
            raise PlatformSelectionError(f"Slack workspace {selection.workspace} is configured with other roles")
        _refuse_source(selection.roles)
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
        return SlackAdapter(platform, client, receiver, clock=clock)

    return build
