"""The Discord adapter behind ``ChatPlatform`` (SPEC-007, RA-KNOW-008).

``docs/architecture/knowledge-loop-platforms.md`` specifies it ("How each platform maps", the Discord
column). The adapter translates and nothing more: it turns gateway dispatches into normalized
``Interaction`` and ``CaptureEvent`` values, and normalized replies and shares into Discord REST calls.
Every rule about what may be answered, cited or shared stays in the platform-neutral code.

It never opens a connection itself. A ``DiscordTransport`` is injected: it holds the bot token, keeps
the gateway session alive (heartbeats, resume, reconnect) and performs REST calls. The adapter sees
only dispatch payloads and REST responses, so its tests run on synthetic payloads with no network,
and the token never reaches the adapter at all: ``discord_factory`` reads it with
``config.read_secret`` when ``start_platforms`` builds the adapter, and hands it straight to the
transport.

What the adapter guarantees, beyond the ``ChatPlatform`` contract:

- **Least privilege by role (CP-3).** ``gateway_intents`` requests the Message Content intent, and
  ``bot_permissions`` the Read Message History permission, only for the ``source`` role. An
  interface-only adapter never lists history and never records a message.
- **Exclusions hold (CP-6).** DMs are never answered or captured: they belong to no guild, and the
  adapter never requests the direct-message intent. A channel answers or is captured only if its id is
  on that role's allowlist. A public thread is covered by its channel's entry; a private thread only
  by its own.
- **Edits and deletions propagate (CP-8).** Capture reports message update and delete events, and
  ``can_see`` asks Discord for the message at call time, so a deleted message or an edited message's
  old revision is never confirmed.
- **Messages are data (CP-9).** Places and people come from the payload's structural fields, never
  from text. The question is the ``question`` option of the ``/ask`` command, or the text of a message
  that mentions the bot; any other option or field is ignored. Bots, webhooks and the adapter's own
  messages can't ask. Every message the adapter sends disables mentions, so an answer can't notify a
  role or ``@everyone``.

A message's revision is ``"0"`` until it is edited, then its ``edited_timestamp`` in microseconds.
Discord has no revision number, and the edit time is the one value every copy of the message agrees on.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Annotated, Any, Literal, Optional, Protocol, TypeVar

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, ValidationError

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

# Gateway intents. Interactions need none. Without Message Content, Discord still delivers the text of
# a message that mentions the bot, which is all the interface role reads.
GUILDS = 1 << 0
GUILD_MESSAGES = 1 << 9
MESSAGE_CONTENT = 1 << 15

# Permission bits.
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_MESSAGE_HISTORY = 1 << 16
MANAGE_THREADS = 1 << 34
SEND_MESSAGES_IN_THREADS = 1 << 38
ALL_PERMISSIONS = (1 << 64) - 1

# Channel and message types.
DM_TYPES = frozenset({1, 3})
THREAD_TYPES = frozenset({10, 11, 12})
PRIVATE_THREAD = 12
FORUM_TYPES = frozenset({15, 16})  # their posts are threads; the channel itself holds no messages
CAPTURED_MESSAGE_TYPES = frozenset({0, 19})  # ordinary messages and replies, not system notices

# Interactions.
APPLICATION_COMMAND = 2
CHAT_INPUT = 1
STRING_OPTION = 3
DEFERRED_CHANNEL_MESSAGE = 5
EPHEMERAL = 1 << 6
ASK_COMMAND = "ask"
QUESTION_OPTION = "question"
# Discord accepts a deferred interaction's answer for 15 minutes. A mention's question is dropped
# after the same time, so unanswered questions can't pile up in memory.
QUESTION_LIFETIME = timedelta(minutes=15)

MESSAGE_LIMIT = 2000
HISTORY_PAGE = 100
JOURNAL_PAGE = 100
JOURNAL_LIMIT = 100_000
MEMBER_PAGE = 1000
# Discord always says how long to wait. If it ever doesn't, the adapter takes the conservative wait
# that SPEC-007 part 2 sets for a Slack 429 with no Retry-After, rather than guessing low.
DEFAULT_RETRY_AFTER = 60.0
NO_MENTIONS: Mapping[str, Any] = {"parse": []}

DISCORD_EPOCH = datetime(2015, 1, 1, tzinfo=timezone.utc)
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

Method = Literal["GET", "POST", "PATCH", "DELETE"]


class NotAllowlisted(PlatformError):
    """The place isn't on the operator's allowlist for this role, or isn't a share destination."""


class RateLimited(PlatformError):
    """Discord asked the adapter to wait. Nothing was sent."""

    def __init__(self, what: str, retry_after_seconds: float) -> None:
        super().__init__(f"Discord rate-limited {what}; retry after {retry_after_seconds:g} seconds")
        self.retry_after_seconds = retry_after_seconds


class CursorExpired(PlatformError):
    """The events after this cursor are no longer held. Capture again from no cursor."""


# The transport


@dataclass(frozen=True)
class RestResponse:
    """One REST answer. ``retry_after`` is set on a 429, from Discord's ``retry_after``."""

    status: int
    body: Any = None
    retry_after: Optional[float] = None


class DiscordTransport(Protocol):
    """The gateway connection and the REST API, holding the bot token. The adapter holds neither.

    - ``connect`` identifies with ``intents`` and returns the ``READY`` dispatch's data.
    - ``receive`` returns the dispatches (``{"t": name, "d": data}``) that arrived since the last call,
      in order, and never blocks. Heartbeats, resume and reconnect are the transport's. A new session
      is delivered as another ``READY``.
    - ``request`` performs one REST call and returns its status, never raising for one. It doesn't
      sleep or retry a 429: it returns it with Discord's ``retry_after``, so the caller decides when
      to try again. It raises ``OSError`` when Discord can't be reached.
    """

    def connect(self, intents: int) -> Mapping[str, Any]: ...

    def receive(self) -> Sequence[Mapping[str, Any]]: ...

    def request(
        self,
        method: Method,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> RestResponse: ...


# Least privilege by role (CP-3)


def gateway_intents(roles: frozenset[Role]) -> int:
    """What the adapter identifies with. Message Content is privileged, and only capture needs it."""
    intents = GUILDS | GUILD_MESSAGES
    if "source" in roles:
        intents |= MESSAGE_CONTENT
    return intents


def bot_permissions(roles: frozenset[Role]) -> int:
    """The channel permissions to grant the bot in allowlisted channels, for the operator's install link."""
    permissions = VIEW_CHANNEL
    if "interface" in roles:
        permissions |= SEND_MESSAGES | SEND_MESSAGES_IN_THREADS
    if "source" in roles:
        permissions |= READ_MESSAGE_HISTORY
    return permissions


def oauth_scopes(roles: frozenset[Role]) -> frozenset[str]:
    """Discord has no history scope: reading history is a permission, ``bot_permissions`` above."""
    return frozenset({"bot", "applications.commands"}) if "interface" in roles else frozenset({"bot"})


def snowflake_time(snowflake: str) -> datetime:
    """When Discord created the object with this id. Its top 42 bits are milliseconds since 2015."""
    return DISCORD_EPOCH + timedelta(milliseconds=int(snowflake) >> 22)


def discord_revision(edited_timestamp: Optional[datetime]) -> str:
    """A message's revision: ``"0"`` until it is edited, then its edit time in microseconds."""
    if edited_timestamp is None:
        return "0"
    return str((edited_timestamp - UNIX_EPOCH) // timedelta(microseconds=1))


# Discord payloads: only the fields the adapter reads. Unknown fields are ignored, so a payload can't
# smuggle a place or a visibility in, and inputs never reach an error message, because an interaction
# payload carries its token.

Snowflake = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


def _bits(value: Any) -> int:
    # Discord sends permission bitfields as decimal strings, because they outgrow a JSON number.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    raise ValueError("a permission bitfield is a decimal string")


Bits = Annotated[int, BeforeValidator(_bits)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


class _User(_Payload):
    id: Snowflake
    bot: bool = False


class _Member(_Payload):
    user: _User
    roles: tuple[Snowflake, ...] = ()


class _Overwrite(_Payload):
    id: Snowflake
    type: Literal[0, 1]  # a role, or a member
    allow: Bits = 0
    deny: Bits = 0


class _Channel(_Payload):
    id: Snowflake
    type: int
    guild_id: Optional[Snowflake] = None
    parent_id: Optional[Snowflake] = None
    permission_overwrites: tuple[_Overwrite, ...] = ()


class _ChannelRef(_Payload):
    id: Snowflake
    guild_id: Optional[Snowflake] = None


class _Message(_Payload):
    id: Snowflake
    channel_id: Snowflake
    guild_id: Optional[Snowflake] = None
    type: int = 0
    author: Optional[_User] = None
    webhook_id: Optional[Snowflake] = None
    content: Optional[str] = None
    timestamp: Optional[AwareDatetime] = None
    edited_timestamp: Optional[AwareDatetime] = None
    mentions: tuple[_User, ...] = ()


class _MessageRef(_Payload):
    id: Snowflake


class _Deletion(_Payload):
    id: Snowflake
    channel_id: Snowflake
    guild_id: Optional[Snowflake] = None


class _BulkDeletion(_Payload):
    ids: tuple[Snowflake, ...]
    channel_id: Snowflake
    guild_id: Optional[Snowflake] = None


class _Option(_Payload):
    name: str
    type: int
    value: Any = None


class _CommandData(_Payload):
    name: str
    type: int = CHAT_INPUT
    options: tuple[_Option, ...] = ()


class _InteractionPayload(_Payload):
    id: Snowflake
    type: int
    token: SecretStr
    guild_id: Optional[Snowflake] = None
    channel_id: Optional[Snowflake] = None
    member: Optional[_Member] = None
    data: Optional[_CommandData] = None


class _Role(_Payload):
    id: Snowflake
    permissions: Bits


class _Guild(_Payload):
    id: Snowflake
    owner_id: Snowflake
    roles: tuple[_Role, ...] = ()


class _GuildChannels(_Payload):
    """A ``GUILD_CREATE``, or a ``THREAD_LIST_SYNC``: the guild's channels and active threads."""

    id: Optional[Snowflake] = None
    guild_id: Optional[Snowflake] = None
    channels: tuple[_Channel, ...] = ()
    threads: tuple[_Channel, ...] = ()


class _ThreadMetadata(_Payload):
    archive_timestamp: AwareDatetime


class _ArchivedThread(_Channel):
    thread_metadata: _ThreadMetadata


class _ThreadPage(_Payload):
    threads: tuple[_Channel, ...] = ()


class _ArchivedPage(_Payload):
    threads: tuple[_ArchivedThread, ...] = ()
    has_more: bool = False


class _ThreadMember(_Payload):
    user_id: Snowflake


class _Ready(_Payload):
    session_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    user: _User
    application: _MessageRef
    guilds: tuple[_MessageRef, ...] = ()


_P = TypeVar("_P", bound=_Payload)


def _parse(model: type[_P], data: Any, what: str) -> _P:
    try:
        return model.model_validate(data)
    except ValidationError:
        raise PlatformError(f"Discord sent a malformed {what}") from None


def _parse_list(model: type[_P], data: Any, what: str) -> list[_P]:
    if not isinstance(data, list):
        raise PlatformError(f"Discord sent a malformed {what}")
    return [_parse(model, item, what) for item in data]


def channel_permissions(user: str, roles: Sequence[str], guild: _Guild, overwrites: Sequence[_Overwrite]) -> int:
    """Discord's own permission algorithm: base role permissions, then channel overwrites."""
    if user == guild.owner_id:
        return ALL_PERMISSIONS
    by_role = {role.id: role.permissions for role in guild.roles}
    permissions = by_role.get(guild.id, 0)  # @everyone's role has the guild's id
    for role in roles:
        permissions |= by_role.get(role, 0)
    if permissions & ADMINISTRATOR:
        return ALL_PERMISSIONS
    by_target = {(overwrite.type, overwrite.id): overwrite for overwrite in overwrites}
    everyone = by_target.get((0, guild.id))
    if everyone is not None:
        permissions = (permissions & ~everyone.deny) | everyone.allow
    allow = deny = 0
    for role in roles:
        overwrite = by_target.get((0, role))
        if overwrite is not None:
            allow |= overwrite.allow
            deny |= overwrite.deny
    permissions = (permissions & ~deny) | allow
    member = by_target.get((1, user))
    if member is not None:
        permissions = (permissions & ~member.deny) | member.allow
    return permissions


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Pending:
    """A question waiting for its reply, with what Discord needs to answer it."""

    interaction: Interaction
    target: str  # the Discord channel it was asked in: the thread's own id in a thread
    token: Optional[SecretStr]  # a command's interaction token; a mention has none


@dataclass(frozen=True)
class _Cursor:
    """Where a capture run is: backfilling ``targets`` from ``index`` and ``after``, or in the journal."""

    backfill: bool
    position: int  # the journal sequence the run reads after
    index: int = 0
    after: int = 0
    targets: tuple[str, ...] = ()


class DiscordAdapter:
    """One Discord guild, as a ``ChatPlatform``. Build it with ``discord_factory``."""

    def __init__(
        self,
        config: PlatformConfig,
        transport: DiscordTransport,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if config.kind != "discord":
            raise PlatformSelectionError(f"the Discord adapter can't serve a {config.kind} workspace")
        self._config = config
        self._transport = transport
        self._clock = clock
        self._channels: dict[str, _Channel] = {}
        self._questions: deque[Interaction] = deque()
        self._pending: dict[str, _Pending] = {}
        # The capture journal: every message event in a source-allowlisted place since the session
        # began. History before it comes from REST, which reports no edits or deletions.
        self._journal: deque[tuple[int, CaptureEvent]] = deque()
        self._seq = 0
        self._dropped_through = 0
        # Counts journal restarts. A cursor names its epoch, so one issued before a restart expires
        # even when its position is still in range.
        self._epoch = 0
        # The first message created in each Discord channel during this session. Backfill reads up
        # to it, and the journal from it, so the two never leave a gap between them.
        self._first_live: dict[str, int] = {}
        self._revisions: dict[str, str] = {}
        self._authors: dict[str, Principal] = {}
        self._handlers: dict[str, Callable[[Mapping[str, Any]], None]] = {
            "READY": self._on_ready,
            "GUILD_CREATE": self._on_guild_channels,
            "THREAD_LIST_SYNC": self._on_guild_channels,
            "CHANNEL_CREATE": self._on_channel,
            "CHANNEL_UPDATE": self._on_channel,
            "THREAD_CREATE": self._on_channel,
            "THREAD_UPDATE": self._on_channel,
            "CHANNEL_DELETE": self._on_channel_delete,
            "THREAD_DELETE": self._on_channel_delete,
            "MESSAGE_CREATE": self._on_message_create,
            "MESSAGE_UPDATE": self._on_message_update,
            "MESSAGE_DELETE": self._on_message_delete,
            "MESSAGE_DELETE_BULK": self._on_message_delete_bulk,
            "INTERACTION_CREATE": self._on_interaction,
        }
        ready = _parse(_Ready, transport.connect(gateway_intents(config.roles)), "READY payload")
        if config.workspace not in {guild.id for guild in ready.guilds}:
            raise PlatformError(f"the bot is not a member of Discord guild {config.workspace}")
        self._session = ready.session_id
        self._bot = ready.user.id
        self._application = ready.application.id

    # ChatPlatform

    @property
    def kind(self) -> PlatformKind:
        return "discord"

    @property
    def workspace(self) -> str:
        return self._config.workspace

    @property
    def roles(self) -> frozenset[Role]:
        return self._config.roles

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
        _check_length(answer.text)
        self._expire_questions()
        pending = self._pending.get(interaction.id)
        # The whole interaction must match, so a copy pointed at another place can't borrow the token.
        if pending is None or pending.interaction != interaction:
            raise PlatformError(f"interaction {interaction.id} was not received here, was already answered, or expired")
        content = {"content": answer.text, "allowed_mentions": NO_MENTIONS}
        if pending.token is None:
            if checked == "ephemeral":
                raise PlatformError("Discord answers a mention only in its place; only a slash command gets a private reply")
            body = {**content, "message_reference": {"message_id": interaction.id, "fail_if_not_exists": False}}
            sent = self._call("POST", f"/channels/{pending.target}/messages", "the reply", body=body)
        elif checked == "ephemeral":
            # The question was acknowledged privately on arrival. Filling in that acknowledgement is
            # the private answer.
            path = f"/webhooks/{self._application}/{pending.token.get_secret_value()}/messages/@original"
            sent = self._call("PATCH", path, "the reply", body=content)
        else:
            sent = self._call("POST", f"/channels/{pending.target}/messages", "the reply", body=content)
        message = _parse(_MessageRef, _ok(sent, "the reply"), "reply response")
        del self._pending[interaction.id]
        if pending.token is not None and checked == "thread":
            self._remove_acknowledgement(pending.token)
        return Delivery(place=interaction.place, message=message.id)

    def can_see(self, principal: Principal, source_ref: SourceRef) -> bool:
        own = (self.kind, self.workspace)
        if (principal.platform, principal.workspace) != own or (source_ref.platform, source_ref.workspace) != own:
            return False
        target = source_ref.thread or source_ref.channel
        found = self._call("GET", f"/channels/{target}/messages/{source_ref.message}", "the message")
        if found.status in (403, 404):
            return False
        message = _parse(_Message, _ok(found, "the message"), "message")
        if message.channel_id != target or discord_revision(message.edited_timestamp) != source_ref.revision:
            return False
        located = self._current_place(source_ref.place)
        if located is None:
            return False
        member = self._member(principal.user)
        if member is None:
            return False
        channel, parent = located
        needed = VIEW_CHANNEL | READ_MESSAGE_HISTORY
        return self._visible(member, self._guild(), channel, parent, needed, lambda user: self._joined(channel.id, user))

    def audience(self, place: Place) -> Audience:
        require_own(self, place)
        located = self._current_place(place)
        if located is None:
            raise PlatformError(f"Discord channel {place.thread or place.channel} can't be seen in guild {self.workspace}")
        channel, parent = located
        guild = self._guild()
        joined = self._thread_members(channel.id) if channel.type == PRIVATE_THREAD else frozenset()
        readers = frozenset(
            self._principal(member.user.id)
            for member in self._members()
            # The bot is the one speaking, not part of who it speaks to.
            if member.user.id != self._bot and self._visible(member, guild, channel, parent, VIEW_CHANNEL, joined.__contains__)
        )
        return Audience(place=place, members=readers)

    def fetch_since(self, place: Place, cursor: Optional[str]) -> CapturePage:
        require_role(self, "source")
        require_own(self, place)
        allowed = self._config.channels.source
        # Refused on the ids alone first, so a channel nobody allowlisted costs no call at all.
        if place.channel not in allowed and place.thread not in allowed:
            raise NotAllowlisted(f"Discord channel {place.thread or place.channel} is not allowlisted for capture")
        self._pump()
        try:
            channel = self._resolve(place)
            if not self._allowed("source", place, channel):
                raise NotAllowlisted(f"Discord thread {channel.id} is private and not allowlisted for capture")
            return self._capture(place, channel, cursor)
        except RateLimited as limited:
            return CapturePage(
                place=place, events=(), cursor=cursor, complete=False, retry_after_seconds=limited.retry_after_seconds
            )

    def publish(self, destination: Place, candidate: ShareCandidate, receipt: Optional[ApprovalReceipt]) -> Delivery:
        require_role(self, "interface")
        require_own(self, destination)
        check_receipt(destination, candidate, receipt)
        if ShareDestination(channel=destination.channel, thread=destination.thread) not in self._config.share_destinations:
            raise NotAllowlisted(f"Discord channel {destination.thread or destination.channel} is not a share destination")
        _check_length(candidate.text)
        channel = self._resolve(destination)
        body = {"content": candidate.text, "allowed_mentions": NO_MENTIONS}
        sent = self._call("POST", f"/channels/{channel.id}/messages", "the share", body=body)
        message = _parse(_MessageRef, _ok(sent, "the share"), "share response")
        return Delivery(place=destination, message=message.id)

    # The gateway

    def _pump(self) -> None:
        for dispatch in self._transport.receive():
            name, data = dispatch.get("t"), dispatch.get("d")
            if not isinstance(name, str) or not isinstance(data, Mapping):
                continue
            handler = self._handlers.get(name)
            if handler is None:
                continue
            try:
                handler(data)
            except (ValidationError, PlatformError):
                # A payload the adapter can't read, or a channel it couldn't look up, is dropped. If
                # it was a message event, capture can't vouch for what came after it.
                if name.startswith("MESSAGE_"):
                    self._restart_journal()
        self._expire_questions()

    def _on_ready(self, data: Mapping[str, Any]) -> None:
        ready = _Ready.model_validate(data)
        if ready.session_id != self._session:
            # Events between the two sessions were never delivered, so no cursor spans them.
            self._session = ready.session_id
            self._restart_journal()
        self._bot = ready.user.id
        self._application = ready.application.id

    def _on_guild_channels(self, data: Mapping[str, Any]) -> None:
        guild = _GuildChannels.model_validate(data)
        if (guild.id or guild.guild_id) == self.workspace:
            # A guild's own channel list leaves each channel's guild id out.
            for channel in guild.channels + guild.threads:
                self._channels[channel.id] = channel.model_copy(update={"guild_id": self.workspace})

    def _on_channel(self, data: Mapping[str, Any]) -> None:
        channel = _Channel.model_validate(data)
        if channel.guild_id == self.workspace:
            self._channels[channel.id] = channel

    def _on_channel_delete(self, data: Mapping[str, Any]) -> None:
        channel = _ChannelRef.model_validate(data)
        if channel.guild_id == self.workspace:
            self._channels.pop(channel.id, None)

    def _on_message_create(self, data: Mapping[str, Any]) -> None:
        message = _Message.model_validate(data)
        if message.guild_id != self.workspace:  # a DM has no guild
            return
        located = self._locate(message.channel_id)
        if located is None or message.author is None:
            return
        place, channel = located
        author = self._principal(message.author.id)
        if "source" in self.roles and self._allowed("source", place, channel):
            self._first_live.setdefault(channel.id, int(message.id))
            if message.author.id != self._bot and message.type in CAPTURED_MESSAGE_TYPES:
                ref = self._ref(place, message.id, discord_revision(message.edited_timestamp))
                self._remember(ref, author)
                at = message.timestamp or snowflake_time(message.id)
                self._record(CaptureEvent(kind="message", source=ref, author=author, text=message.content or "", at=at))
        if "interface" in self.roles and self._asks(message) and self._allowed("interface", place, channel):
            text = re.sub(rf"<@!?{self._bot}>", "", message.content or "").strip()
            if text:
                question = Interaction(
                    id=message.id,
                    via="mention",
                    principal=author,
                    place=place,
                    text=text,
                    received_at=snowflake_time(message.id),
                )
                self._queue(_Pending(interaction=question, target=channel.id, token=None))

    def _on_message_update(self, data: Mapping[str, Any]) -> None:
        if "source" not in self.roles:
            return
        message = _Message.model_validate(data)
        if message.guild_id != self.workspace:
            return
        located = self._locate(message.channel_id)
        if located is None or not self._allowed("source", *located):
            return
        # An update with no new edit time is Discord filling in an embed, not someone editing.
        if message.edited_timestamp is None or message.content is None:
            return
        revision = discord_revision(message.edited_timestamp)
        if self._revisions.get(message.id) == revision:
            return
        author = self._principal(message.author.id) if message.author is not None else self._authors.get(message.id)
        if author is None:
            raise PlatformError(f"the edit of message {message.id} names no author")
        if author.user == self._bot:
            return
        ref = self._ref(located[0], message.id, revision)
        self._remember(ref, author)
        self._record(CaptureEvent(kind="edit", source=ref, author=author, text=message.content, at=message.edited_timestamp))

    def _on_message_delete(self, data: Mapping[str, Any]) -> None:
        deletion = _Deletion.model_validate(data)
        self._deleted(deletion.guild_id, deletion.channel_id, (deletion.id,))

    def _on_message_delete_bulk(self, data: Mapping[str, Any]) -> None:
        deletion = _BulkDeletion.model_validate(data)
        self._deleted(deletion.guild_id, deletion.channel_id, deletion.ids)

    def _deleted(self, guild: Optional[str], channel_id: str, messages: Sequence[str]) -> None:
        if "source" not in self.roles or guild != self.workspace:
            return
        located = self._locate(channel_id)
        if located is None or not self._allowed("source", *located):
            return
        for message in messages:
            # A deletion removes every revision. It names the last one seen, or "unknown" for a
            # message the adapter never read.
            ref = self._ref(located[0], message, self._revisions.pop(message, "unknown"))
            self._authors.pop(message, None)
            self._record(CaptureEvent(kind="delete", source=ref, at=self._clock()))

    def _on_interaction(self, data: Mapping[str, Any]) -> None:
        if "interface" not in self.roles:
            return
        payload = _InteractionPayload.model_validate(data)
        command = payload.data
        if payload.type != APPLICATION_COMMAND or command is None or command.type != CHAT_INPUT or command.name != ASK_COMMAND:
            return
        # A DM interaction carries a user and no guild or member.
        if payload.guild_id != self.workspace or payload.member is None or payload.channel_id is None:
            return
        text = next(
            (
                option.value.strip()
                for option in command.options
                if option.name == QUESTION_OPTION and option.type == STRING_OPTION and isinstance(option.value, str)
            ),
            "",
        )
        located = self._locate(payload.channel_id)
        if not text or located is None or not self._allowed("interface", *located):
            return
        question = Interaction(
            id=payload.id,
            via="command",
            principal=self._principal(payload.member.user.id),
            place=located[0],
            text=text,
            received_at=snowflake_time(payload.id),
        )
        # Discord fails a command that isn't acknowledged within three seconds. The acknowledgement
        # is private and says nothing, so the asker alone sees that an answer is coming.
        acknowledgement = {"type": DEFERRED_CHANNEL_MESSAGE, "data": {"flags": EPHEMERAL}}
        path = f"/interactions/{payload.id}/{payload.token.get_secret_value()}/callback"
        _ok(self._call("POST", path, "the acknowledgement", body=acknowledgement), "the acknowledgement")
        self._queue(_Pending(interaction=question, target=located[1].id, token=payload.token))

    # Places and allowlists

    def _locate(self, channel_id: str) -> Optional[tuple[Place, _Channel]]:
        channel = self._channels.get(channel_id) or self._fetch_channel(channel_id)
        if channel is None or channel.guild_id != self.workspace or channel.type in DM_TYPES:
            return None
        if channel.type in THREAD_TYPES:
            if channel.parent_id is None:
                return None
            return self._place(channel.parent_id, channel.id), channel
        return self._place(channel.id, None), channel

    def _resolve(self, place: Place) -> _Channel:
        """The Discord channel behind a place, checked to be the thread of that channel it claims."""
        target = place.thread or place.channel
        located = self._locate(target)
        if located is None:
            raise PlatformError(f"Discord channel {target} can't be seen in guild {self.workspace}")
        if located[0] != place:
            raise PlatformError(f"Discord channel {target} is not the place it was named as")
        return located[1]

    def _allowed(self, role: Role, place: Place, channel: _Channel) -> bool:
        allowlist = self._config.channels.interface if role == "interface" else self._config.channels.source
        if channel.type == PRIVATE_THREAD:
            # Allowlisting a channel doesn't open the private threads in it.
            return channel.id in allowlist
        return place.channel in allowlist or (place.thread is not None and place.thread in allowlist)

    def _fetch_channel(self, channel_id: str) -> Optional[_Channel]:
        found = self._call("GET", f"/channels/{channel_id}", "the channel")
        if found.status in (403, 404):
            return None
        channel = _parse(_Channel, _ok(found, "the channel"), "channel")
        if channel.guild_id == self.workspace:
            self._channels[channel.id] = channel
        return channel

    def _current_place(self, place: Place) -> Optional[tuple[_Channel, Optional[_Channel]]]:
        """A place's channel, and a thread's parent, as Discord has them now: never from the cache."""
        target = place.thread or place.channel
        channel = self._fetch_channel(target)
        if channel is None or channel.guild_id != self.workspace:
            return None
        if place.thread is None:
            return (channel, None) if channel.type not in THREAD_TYPES | DM_TYPES else None
        if channel.type not in THREAD_TYPES or channel.parent_id != place.channel:
            return None
        parent = self._fetch_channel(place.channel)
        return (channel, parent) if parent is not None else None

    # Access

    def _guild(self) -> _Guild:
        return _parse(_Guild, _ok(self._call("GET", f"/guilds/{self.workspace}", "the guild"), "the guild"), "guild")

    def _member(self, user: str) -> Optional[_Member]:
        found = self._call("GET", f"/guilds/{self.workspace}/members/{user}", "the member")
        if found.status == 404:
            return None
        return _parse(_Member, _ok(found, "the member"), "member")

    def _members(self) -> list[_Member]:
        members: list[_Member] = []
        after = 0
        while True:
            query = {"limit": str(MEMBER_PAGE), "after": str(after)}
            listed = self._call("GET", f"/guilds/{self.workspace}/members", "the member list", query=query)
            page = _parse_list(_Member, _ok(listed, "the member list"), "member list")
            members.extend(page)
            if len(page) < MEMBER_PAGE:
                return members
            last = max(int(member.user.id) for member in page)
            if last <= after:
                raise PlatformError("Discord's member list did not advance")
            after = last

    def _visible(
        self,
        member: _Member,
        guild: _Guild,
        channel: _Channel,
        parent: Optional[_Channel],
        needed: int,
        joined: Callable[[str], bool],
    ) -> bool:
        # A thread takes its permissions from its parent channel. A private thread also needs its
        # members to have joined it, unless they may manage threads.
        overwrites = (parent or channel).permission_overwrites
        permissions = channel_permissions(member.user.id, member.roles, guild, overwrites)
        if permissions & needed != needed:
            return False
        return channel.type != PRIVATE_THREAD or bool(permissions & MANAGE_THREADS) or joined(member.user.id)

    def _joined(self, thread: str, user: str) -> bool:
        found = self._call("GET", f"/channels/{thread}/thread-members/{user}", "the thread member")
        if found.status == 404:
            return False
        return _parse(_ThreadMember, _ok(found, "the thread member"), "thread member").user_id == user

    def _thread_members(self, thread: str) -> frozenset[str]:
        listed = self._call("GET", f"/channels/{thread}/thread-members", "the thread members")
        return frozenset(
            member.user_id for member in _parse_list(_ThreadMember, _ok(listed, "the thread members"), "thread members")
        )

    # Capture

    def _capture(self, place: Place, channel: _Channel, cursor: Optional[str]) -> CapturePage:
        state = self._read_cursor(cursor, channel.id)
        if state is None:
            state = _Cursor(backfill=True, position=self._dropped_through, targets=self._targets(place, channel))
        events: list[CaptureEvent] = []
        if state.backfill and state.index < len(state.targets):
            target = state.targets[state.index]
            self._check_target(place, channel, target)
            found, done, last = self._backfill(place, target, state.after)
            events.extend(found)
            following = None
            if not done:
                following = _Cursor(True, state.position, state.index, last, state.targets)
            elif state.index + 1 < len(state.targets):
                following = _Cursor(True, state.position, state.index + 1, 0, state.targets)
            if following is not None:
                cursor = self._cursor(channel.id, following)
                return CapturePage(place=place, events=tuple(events), cursor=cursor, complete=False)
        found, position, complete = self._journal_page(place, state.position)
        events.extend(found)
        cursor = self._cursor(channel.id, _Cursor(backfill=False, position=position))
        return CapturePage(place=place, events=tuple(events), cursor=cursor, complete=complete)

    def _check_target(self, place: Place, channel: _Channel, target: str) -> None:
        # A cursor names the channels it backfills, so each one is checked again before it is read:
        # an edited cursor can't point capture at a channel outside this place or its allowlist.
        if target == channel.id:
            return
        located = self._locate(target)
        if located is None or located[0] != self._place(channel.id, target) or not self._allowed("source", *located):
            raise PlatformError(f"Discord channel {target} is not an allowlisted thread of channel {channel.id}")

    def _targets(self, place: Place, channel: _Channel) -> tuple[str, ...]:
        """The Discord channels a capture run backfills: the channel, then its allowlisted threads."""
        if place.thread is not None:
            return (channel.id,)
        threads: list[_Channel] = []
        active = self._call("GET", f"/guilds/{self.workspace}/threads/active", "the active threads")
        threads.extend(_parse(_ThreadPage, _ok(active, "the active threads"), "thread list").threads)
        before: Optional[datetime] = None
        while True:
            query = {"limit": "100"} if before is None else {"limit": "100", "before": before.isoformat()}
            listed = self._call("GET", f"/channels/{channel.id}/threads/archived/public", "the archived threads", query=query)
            page = _parse(_ArchivedPage, _ok(listed, "the archived threads"), "archived thread list")
            threads.extend(page.threads)
            if not page.has_more or not page.threads:
                break
            oldest = min(thread.thread_metadata.archive_timestamp for thread in page.threads)
            if before is not None and oldest >= before:
                raise PlatformError("Discord's archived thread list did not advance")
            before = oldest
        found: set[str] = set()
        for thread in threads:
            self._channels[thread.id] = thread.model_copy(update={"guild_id": self.workspace})
            if thread.parent_id == channel.id and self._allowed("source", self._place(channel.id, thread.id), thread):
                found.add(thread.id)
        own = () if channel.type in FORUM_TYPES else (channel.id,)
        return own + tuple(sorted(found, key=int))

    def _backfill(self, place: Place, target: str, after: int) -> tuple[list[CaptureEvent], bool, int]:
        """One page of a Discord channel's history from before this session, oldest first."""
        query = {"after": str(after), "limit": str(HISTORY_PAGE)}
        listed = self._call("GET", f"/channels/{target}/messages", "the history", query=query)
        messages = sorted(_parse_list(_Message, _ok(listed, "the history"), "history page"), key=lambda m: int(m.id))
        boundary = self._first_live.get(target)
        done = len(messages) < HISTORY_PAGE
        events: list[CaptureEvent] = []
        last = after
        thread = None if target == place.channel else target
        for message in messages:
            if boundary is not None and int(message.id) >= boundary:
                # The journal holds this message and everything after it.
                done = True
                break
            last = int(message.id)
            if message.author is None or message.author.id == self._bot or message.type not in CAPTURED_MESSAGE_TYPES:
                continue
            author = self._principal(message.author.id)
            ref = SourceRef(
                platform="discord",
                workspace=self.workspace,
                channel=place.channel,
                thread=thread,
                message=message.id,
                revision=discord_revision(message.edited_timestamp),
            )
            self._revisions.setdefault(message.id, ref.revision)
            self._authors.setdefault(message.id, author)
            at = message.timestamp or snowflake_time(message.id)
            events.append(CaptureEvent(kind="message", source=ref, author=author, text=message.content or "", at=at))
        return events, done, last

    def _journal_page(self, place: Place, position: int) -> tuple[list[CaptureEvent], int, bool]:
        if position < self._dropped_through or position > self._seq:
            raise CursorExpired(f"the capture journal no longer holds the events after {position}; capture again from no cursor")
        events: list[CaptureEvent] = []
        last = position
        for seq, event in islice(self._journal, position - self._dropped_through, None):
            if len(events) == JOURNAL_PAGE:
                return events, last, False
            last = seq
            source = event.source
            if source.channel == place.channel and (place.thread is None or source.thread == place.thread):
                events.append(event)
        return events, self._seq, True

    def _record(self, event: CaptureEvent) -> None:
        self._seq += 1
        self._journal.append((self._seq, event))
        if len(self._journal) > JOURNAL_LIMIT:
            self._restart_journal()

    def _restart_journal(self) -> None:
        # Every cursor into the old journal expires, and the next run backfills everything current,
        # because no message is the first one this session saw any more.
        self._journal.clear()
        self._dropped_through = self._seq
        self._epoch += 1
        self._first_live.clear()

    def _remember(self, ref: SourceRef, author: Principal) -> None:
        self._revisions[ref.message] = ref.revision
        self._authors[ref.message] = author

    def _cursor(self, target: str, state: _Cursor) -> str:
        if state.backfill:
            targets = ",".join(state.targets)
            return f"{self._session}.{self._epoch}:{target}:b:{state.position}:{state.index}:{state.after}:{targets}"
        return f"{self._session}.{self._epoch}:{target}:j:{state.position}"

    def _read_cursor(self, cursor: Optional[str], target: str) -> Optional[_Cursor]:
        if cursor is None:
            return None
        refused = PlatformError(f"cursor {cursor!r} was not issued by this adapter for Discord channel {target}")
        parts = cursor.split(":")
        backfill = len(parts) == 7 and parts[2] == "b"
        if not (backfill or (len(parts) == 4 and parts[2] == "j")) or parts[1] != target:
            raise refused
        if parts[0] != f"{self._session}.{self._epoch}":
            raise CursorExpired("the events after this cursor are no longer held; capture again from no cursor")
        if not all(re.fullmatch(r"[0-9]{1,20}", part) for part in parts[3 : 6 if backfill else 4]):
            raise refused
        if not backfill:
            return _Cursor(backfill=False, position=int(parts[3]))
        targets = tuple(parts[6].split(",")) if parts[6] else ()
        state = _Cursor(True, int(parts[3]), int(parts[4]), int(parts[5]), targets)
        if state.index >= len(targets) or not all(re.fullmatch(r"[0-9]{1,20}", t) for t in targets):
            raise refused
        return state

    # Questions

    def _queue(self, pending: _Pending) -> None:
        self._pending[pending.interaction.id] = pending
        self._questions.append(pending.interaction)

    def _drain(self) -> Iterator[Interaction]:
        while self._questions:
            question = self._questions.popleft()
            if question.id in self._pending:
                yield question

    def _expire_questions(self) -> None:
        oldest = self._clock() - QUESTION_LIFETIME
        for key in [key for key, pending in self._pending.items() if pending.interaction.received_at <= oldest]:
            del self._pending[key]

    def _asks(self, message: _Message) -> bool:
        # Bots and webhooks can't ask, so no automation, the adapter's own answers included, can
        # chain the service.
        return (
            message.author is not None
            and not message.author.bot
            and message.webhook_id is None
            and message.type in CAPTURED_MESSAGE_TYPES
            and self._bot in {user.id for user in message.mentions}
        )

    def _remove_acknowledgement(self, token: SecretStr) -> None:
        # The answer is already posted. A placeholder only the asker can see is not worth failing
        # the reply over, and failing it would invite a duplicate post on retry.
        path = f"/webhooks/{self._application}/{token.get_secret_value()}/messages/@original"
        try:
            self._call("DELETE", path, "the acknowledgement")
        except PlatformError:
            pass

    # REST

    def _call(
        self,
        method: Method,
        path: str,
        what: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> RestResponse:
        # Errors name what was asked, never the path: an interaction's path holds its token.
        try:
            response = self._transport.request(method, path, query=query, body=body)
        except OSError:
            raise PlatformError(f"Discord could not be reached for {what}") from None
        if response.status == 429:
            wait = response.retry_after if response.retry_after is not None and response.retry_after > 0 else DEFAULT_RETRY_AFTER
            raise RateLimited(what, wait)
        return response

    def _principal(self, user: str) -> Principal:
        return Principal(platform="discord", workspace=self.workspace, user=user)

    def _place(self, channel: str, thread: Optional[str]) -> Place:
        return Place(platform="discord", workspace=self.workspace, channel=channel, thread=thread)

    def _ref(self, place: Place, message: str, revision: str) -> SourceRef:
        return SourceRef(
            platform="discord",
            workspace=self.workspace,
            channel=place.channel,
            thread=place.thread,
            message=message,
            revision=revision,
        )


def _ok(response: RestResponse, what: str) -> Any:
    if not 200 <= response.status < 300:
        raise PlatformError(f"Discord refused {what} (HTTP {response.status})")
    return response.body


def _check_length(text: str) -> None:
    # Cutting an answer short could drop its citations or its caveat, so a long one is refused whole.
    if len(text) > MESSAGE_LIMIT:
        raise PlatformError(f"Discord accepts at most {MESSAGE_LIMIT} characters in a message, not {len(text)}")


Connect = Callable[[SecretStr], DiscordTransport]


def discord_factory(
    settings: KnowledgeSettings,
    connect: Connect,
    *,
    environ: Optional[Mapping[str, str]] = None,
    clock: Callable[[], datetime] = _now,
) -> PlatformFactory:
    """The factory ``start_platforms`` calls for a Discord selection.

    Building the factory reads nothing. Calling it reads the bot token with ``config.read_secret``,
    hands it to ``connect`` to open the transport, and builds the adapter on that transport. The
    adapter never sees the token.
    """

    def build(selection: PlatformSelection) -> ChatPlatform:
        if selection.kind != "discord":
            raise PlatformSelectionError(f"the Discord factory was handed a {selection.kind} selection")
        platform = settings.platform(selection)
        # Checked before the token is read, so a selection the operator didn't write connects nothing.
        if platform.selection() != selection:
            raise PlatformSelectionError(f"Discord guild {selection.workspace} is configured with other roles")
        reference = platform.credentials.bot_token
        if reference is None:
            raise PlatformSelectionError(f"Discord guild {selection.workspace} has no bot token reference")
        token = knowledge_config.read_secret(reference, environ)
        return DiscordAdapter(platform, connect(token), clock=clock)

    return build
