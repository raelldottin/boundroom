"""The one interface the knowledge service uses to reach a chat platform (SPEC-007, RA-KNOW-006).

``docs/architecture/knowledge-loop-platforms.md`` specifies it. Discord and Slack are adapters
behind ``ChatPlatform``, as boards are behind ``BoardBackend``: capture, retrieval, sharing and the
trust boundary are written once, against the normalized models here, and an adapter only translates.

Every adapter keeps this contract, which ``automation/tests/test_knowledge_platform.py`` checks for
each one:

- ``kind``, ``workspace`` and ``roles`` are exactly what the operator selected. One adapter serves one
  workspace, and every place, person and source it accepts or returns belongs to that workspace.
- ``interactions``, ``reply`` and ``publish`` need the ``interface`` role, and ``fetch_since`` needs
  ``source``. A missing role is refused before the platform is asked anything.
- ``reply`` answers in the interaction's own place, as ``ephemeral`` or ``thread`` and nothing else.
- ``can_see`` and ``audience`` ask the platform at call time. A deleted source, or an edited source's
  old revision, can't be seen.
- ``fetch_since`` returns events after a cursor and says whether the page is complete, so a
  rate-limited or partial page is never taken for a complete capture.
- ``publish`` refuses a candidate unless the approval receipt names that destination and that exact
  candidate.
- Every refusal raises a ``PlatformError`` and sends nothing.

``start_platforms`` builds adapters from the operator's selections and nothing else, so an empty
selection starts no adapter (CP-1) and selecting one platform never builds another (CP-2). The
selections come from the operator's configuration, which RA-KNOW-007 loads.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Protocol, get_args, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PlatformKind = Literal["discord", "slack"]
Role = Literal["interface", "source"]
Visibility = Literal["ephemeral", "thread"]
VISIBILITIES: frozenset[str] = frozenset(get_args(Visibility))

# Discord snowflakes, Slack ids and Slack message timestamps all fit. Ids end up in evidence records
# and log lines, so whitespace, separators and control characters are refused at the door.
PLATFORM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$"
PlatformId = Annotated[str, Field(pattern=PLATFORM_ID_PATTERN)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PlatformError(Exception):
    """A platform call was refused. Nothing was sent."""


class RoleNotEnabled(PlatformError):
    """The operator did not enable the role this call needs."""


class OutsideWorkspace(PlatformError):
    """A person, place or source belongs to another platform or workspace than this adapter's."""


class VisibilityRefused(PlatformError):
    """A reply asked for a visibility other than ``ephemeral`` or ``thread``."""


class ReceiptRefused(PlatformError):
    """A share has no approval receipt for this exact destination and candidate."""


class PlatformSelectionError(PlatformError):
    """The operator's selections can't be started as given."""


class Normalized(BaseModel):
    """Platform-neutral values. Unknown keys are refused, and nothing changes after validation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Principal(Normalized):
    platform: PlatformKind
    workspace: PlatformId
    user: PlatformId


class Place(Normalized):
    """A channel, or a thread in it. ``thread`` is None for the channel itself."""

    platform: PlatformKind
    workspace: PlatformId
    channel: PlatformId
    thread: Optional[PlatformId] = None

    @property
    def channel_place(self) -> Place:
        return self.model_copy(update={"thread": None})


class SourceRef(Normalized):
    """One revision of one message. An edit is a new revision, so a citation pins what was read."""

    platform: PlatformKind
    workspace: PlatformId
    channel: PlatformId
    thread: Optional[PlatformId] = None
    message: PlatformId
    revision: PlatformId

    @property
    def place(self) -> Place:
        return Place(platform=self.platform, workspace=self.workspace, channel=self.channel, thread=self.thread)


def _same_scope(*items: Principal | Place | SourceRef) -> bool:
    return len({(item.platform, item.workspace) for item in items}) == 1


class Interaction(Normalized):
    """A question someone asked the service, through a slash command or a mention."""

    id: PlatformId
    via: Literal["command", "mention"]
    principal: Principal
    place: Place
    text: NonEmptyStr
    received_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Interaction:
        if not _same_scope(self.principal, self.place):
            raise ValueError("the asker and the place must be on the same platform and workspace")
        return self

    @property
    def platform(self) -> PlatformKind:
        return self.place.platform

    @property
    def workspace(self) -> str:
        return self.place.workspace


class Answer(Normalized):
    """What the service says back. An abstention says why it couldn't answer, in ``text``."""

    text: NonEmptyStr
    citations: tuple[SourceRef, ...] = ()
    abstained: bool = False


class Audience(Normalized):
    """Who can read a place at the time it was asked."""

    place: Place
    members: frozenset[Principal]

    @model_validator(mode="after")
    def _check(self) -> Audience:
        if not _same_scope(self.place, *self.members):
            raise ValueError("an audience's members must be on the place's platform and workspace")
        return self


class CaptureEvent(Normalized):
    """A message, an edit or a deletion seen by the ``source`` role."""

    kind: Literal["message", "edit", "delete"]
    source: SourceRef
    author: Optional[Principal] = None
    text: Optional[str] = None
    at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> CaptureEvent:
        if self.kind == "delete":
            if self.text is not None:
                raise ValueError("a deletion carries no text")
        elif self.text is None or self.author is None:
            raise ValueError(f"a {self.kind} needs its author and text")
        if self.author is not None and not _same_scope(self.source, self.author):
            raise ValueError("the author must be on the source's platform and workspace")
        return self


class CapturePage(Normalized):
    """One ``fetch_since`` result. ``cursor`` resumes after these events, on this run or the next.

    ``complete`` is True only when nothing after ``cursor`` was left unread. A rate-limited page is
    incomplete, keeps the cursor it was given and says how long to wait.
    """

    place: Place
    events: tuple[CaptureEvent, ...]
    cursor: Optional[NonEmptyStr] = None
    complete: bool
    retry_after_seconds: Optional[Annotated[float, Field(gt=0)]] = None

    @model_validator(mode="after")
    def _check(self) -> CapturePage:
        for event in self.events:
            source = event.source
            if source.place.channel_place != self.place.channel_place or (
                self.place.thread is not None and source.thread != self.place.thread
            ):
                raise ValueError(f"event for message {source.message} is outside the fetched place")
        if self.complete and self.retry_after_seconds is not None:
            raise ValueError("a rate-limited page is not complete")
        return self


class ShareCandidate(Normalized):
    """A reviewed, sanitized answer proposed for another audience, published with its evidence."""

    id: PlatformId
    text: NonEmptyStr
    citations: Annotated[tuple[SourceRef, ...], Field(min_length=1)]

    def digest(self) -> str:
        """The sha256 a receipt pins, so a candidate changed after review is not covered by it."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class ApprovalReceipt(Normalized):
    """A person's approval to publish one candidate, exactly as reviewed, to one destination."""

    candidate_id: PlatformId
    candidate_sha256: Sha256
    destination: Place
    approved_by: NonEmptyStr
    approved_at: AwareDatetime


class Delivery(Normalized):
    """Where the platform put a reply or a share, so a retry can be reconciled first."""

    place: Place
    message: PlatformId


class PlatformSelection(Normalized):
    """One platform the operator enabled: the part of its configuration the interface itself needs."""

    kind: PlatformKind
    workspace: PlatformId
    roles: Annotated[frozenset[Role], Field(min_length=1)]


@runtime_checkable
class ChatPlatform(Protocol):
    @property
    def kind(self) -> PlatformKind: ...

    @property
    def workspace(self) -> str: ...

    @property
    def roles(self) -> frozenset[Role]: ...

    def interactions(self) -> Iterator[Interaction]: ...

    def reply(self, interaction: Interaction, answer: Answer, visibility: Visibility) -> Delivery: ...

    def can_see(self, principal: Principal, source_ref: SourceRef) -> bool: ...

    def audience(self, place: Place) -> Audience: ...

    def fetch_since(self, place: Place, cursor: Optional[str]) -> CapturePage: ...

    def publish(self, destination: Place, candidate: ShareCandidate, receipt: Optional[ApprovalReceipt]) -> Delivery: ...


# The checks below are the contract's refusals, written once so adapters can't drift apart.


def require_role(platform: ChatPlatform, role: Role) -> None:
    if role not in platform.roles:
        raise RoleNotEnabled(f"the {role} role is not enabled for {platform.kind} workspace {platform.workspace}")


def require_own(platform: ChatPlatform, *items: Principal | Place | SourceRef | Interaction) -> None:
    for item in items:
        if (item.platform, item.workspace) != (platform.kind, platform.workspace):
            raise OutsideWorkspace(
                f"{item.platform} workspace {item.workspace} is not {platform.kind} workspace {platform.workspace}"
            )


def check_visibility(visibility: object) -> Visibility:
    # A model or a message may supply the value, so it is checked at run time, not trusted to the type.
    if visibility == "ephemeral":
        return "ephemeral"
    if visibility == "thread":
        return "thread"
    raise VisibilityRefused(f"visibility must be one of {sorted(VISIBILITIES)}, not {visibility!r}")


def check_receipt(destination: Place, candidate: ShareCandidate, receipt: Optional[ApprovalReceipt]) -> None:
    if receipt is None:
        raise ReceiptRefused(f"candidate {candidate.id} has no approval receipt")
    if receipt.destination != destination:
        raise ReceiptRefused(f"the receipt for candidate {candidate.id} approves a different destination")
    if receipt.candidate_id != candidate.id or receipt.candidate_sha256 != candidate.digest():
        raise ReceiptRefused(f"the receipt does not approve candidate {candidate.id} as it is now")


PlatformFactory = Callable[[PlatformSelection], ChatPlatform]


def start_platforms(
    selections: Sequence[PlatformSelection], factories: Mapping[str, PlatformFactory]
) -> tuple[ChatPlatform, ...]:
    """Build one adapter per selection, and none for a platform nobody selected.

    A factory is where an adapter imports its platform code, reads its token and connects, so a
    factory that isn't called does none of those. Every selection is checked before any is built.
    """
    seen: set[tuple[str, str]] = set()
    for selection in selections:
        key = (selection.kind, selection.workspace)
        if key in seen:
            raise PlatformSelectionError(f"{selection.kind} workspace {selection.workspace} is selected twice")
        seen.add(key)
        if selection.kind not in factories:
            raise PlatformSelectionError(f"no adapter is installed for {selection.kind}")

    started: list[ChatPlatform] = []
    for selection in selections:
        platform = factories[selection.kind](selection)
        # An adapter that serves another platform, workspace or role set would enable what the
        # operator did not.
        if (platform.kind, platform.workspace, frozenset(platform.roles)) != (
            selection.kind,
            selection.workspace,
            selection.roles,
        ):
            raise PlatformSelectionError(
                f"the {selection.kind} adapter for workspace {selection.workspace} does not serve exactly that selection"
            )
        started.append(platform)
    return tuple(started)


# The fake platform: the whole contract in memory, with no network, no token and no clock.

FAKE_TIME = datetime(2026, 11, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Sent:
    """Something the fake platform delivered: a reply (with its visibility) or a share."""

    delivery: Delivery
    text: str
    visibility: Optional[Visibility]


class FakePlatform:
    """A ``ChatPlatform`` held in memory, for the contract tests and the knowledge service's own tests.

    The methods after ``publish`` stand in for the platform's side: people asking, joining channels,
    posting, editing and deleting, and the platform throttling history reads. ``sent`` and
    ``history_requests`` record what the adapter asked the platform to do.
    """

    def __init__(self, selection: PlatformSelection, *, page_size: int = 2) -> None:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        self._selection = selection
        self._page_size = page_size
        self._questions: deque[Interaction] = deque()
        self._members: dict[str, set[str]] = {}
        self._events: list[CaptureEvent] = []
        self._current: dict[tuple[str, str], SourceRef] = {}
        self._authors: dict[tuple[str, str], Principal] = {}
        self._deleted: set[tuple[str, str]] = set()
        self._throttle: Optional[float] = None
        self._next_id = 0
        self.sent: list[Sent] = []
        self.history_requests: list[tuple[Place, Optional[str]]] = []

    # ChatPlatform

    @property
    def kind(self) -> PlatformKind:
        return self._selection.kind

    @property
    def workspace(self) -> str:
        return self._selection.workspace

    @property
    def roles(self) -> frozenset[Role]:
        return self._selection.roles

    def interactions(self) -> Iterator[Interaction]:
        # Checked here rather than inside the generator, so the refusal comes from the call itself.
        require_role(self, "interface")
        return self._drain()

    def reply(self, interaction: Interaction, answer: Answer, visibility: Visibility) -> Delivery:
        require_role(self, "interface")
        require_own(self, interaction)
        checked = check_visibility(visibility)
        return self._send(interaction.place, answer.text, checked)

    def can_see(self, principal: Principal, source_ref: SourceRef) -> bool:
        if (principal.platform, principal.workspace) != (self.kind, self.workspace) or not _same_scope(principal, source_ref):
            return False
        key = (source_ref.channel, source_ref.message)
        if key in self._deleted or self._current.get(key) != source_ref:
            return False
        return principal.user in self._members.get(source_ref.channel, set())

    def audience(self, place: Place) -> Audience:
        require_own(self, place)
        members = self._members.get(place.channel, set())
        return Audience(
            place=place,
            members=frozenset(Principal(platform=self.kind, workspace=self.workspace, user=user) for user in members),
        )

    def fetch_since(self, place: Place, cursor: Optional[str]) -> CapturePage:
        require_role(self, "source")
        require_own(self, place)
        self.history_requests.append((place, cursor))
        if self._throttle is not None:
            wait, self._throttle = self._throttle, None
            return CapturePage(place=place, events=(), cursor=cursor, complete=False, retry_after_seconds=wait)
        start = 0
        if cursor is not None:
            if not cursor.isdigit():
                raise PlatformError(f"cursor {cursor!r} was not issued by this platform")
            start = int(cursor)
        matching = [
            event
            for event in self._events
            if event.source.channel == place.channel and (place.thread is None or event.source.thread == place.thread)
        ]
        page = matching[start : start + self._page_size]
        end = start + len(page)
        return CapturePage(place=place, events=tuple(page), cursor=str(end), complete=end >= len(matching))

    def publish(self, destination: Place, candidate: ShareCandidate, receipt: Optional[ApprovalReceipt]) -> Delivery:
        require_role(self, "interface")
        require_own(self, destination)
        check_receipt(destination, candidate, receipt)
        return self._send(destination, candidate.text, None)

    # The platform's side

    def ask(
        self,
        user: str,
        channel: str,
        text: str,
        *,
        thread: Optional[str] = None,
        via: Literal["command", "mention"] = "command",
    ) -> Interaction:
        interaction = Interaction(
            id=self._new_id(),
            via=via,
            principal=self._principal(user),
            place=self._place(channel, thread),
            text=text,
            received_at=FAKE_TIME,
        )
        self._questions.append(interaction)
        return interaction

    def set_members(self, channel: str, users: Iterable[str]) -> None:
        self._members[channel] = set(users)

    def post(self, user: str, channel: str, text: str, *, thread: Optional[str] = None) -> SourceRef:
        ref = SourceRef(
            platform=self.kind,
            workspace=self.workspace,
            channel=channel,
            thread=thread,
            message=self._new_id(),
            revision="1",
        )
        self._record("message", ref, self._principal(user), text)
        return ref

    def edit(self, ref: SourceRef, text: str) -> SourceRef:
        current = self._current[(ref.channel, ref.message)]
        edited = current.model_copy(update={"revision": str(int(current.revision) + 1)})
        self._record("edit", edited, self._authors[(ref.channel, ref.message)], text)
        return edited

    def delete(self, ref: SourceRef) -> None:
        self._deleted.add((ref.channel, ref.message))
        self._events.append(CaptureEvent(kind="delete", source=self._current[(ref.channel, ref.message)], at=FAKE_TIME))

    def throttle_next_history_request(self, retry_after_seconds: float) -> None:
        self._throttle = retry_after_seconds

    # Internals

    def _drain(self) -> Iterator[Interaction]:
        while self._questions:
            yield self._questions.popleft()

    def _send(self, place: Place, text: str, visibility: Optional[Visibility]) -> Delivery:
        delivery = Delivery(place=place, message=self._new_id())
        self.sent.append(Sent(delivery=delivery, text=text, visibility=visibility))
        return delivery

    def _record(self, kind: Literal["message", "edit"], ref: SourceRef, author: Principal, text: str) -> None:
        self._current[(ref.channel, ref.message)] = ref
        self._authors[(ref.channel, ref.message)] = author
        self._events.append(CaptureEvent(kind=kind, source=ref, author=author, text=text, at=FAKE_TIME))

    def _principal(self, user: str) -> Principal:
        return Principal(platform=self.kind, workspace=self.workspace, user=user)

    def _place(self, channel: str, thread: Optional[str]) -> Place:
        return Place(platform=self.kind, workspace=self.workspace, channel=channel, thread=thread)

    def _new_id(self) -> str:
        # Shaped like the platform's own ids, so the contract runs on ids an adapter would see.
        self._next_id += 1
        if self.kind == "slack":
            return f"1762905600.{self._next_id:06d}"
        return str(1_300_000_000_000_000_000 + self._next_id)
