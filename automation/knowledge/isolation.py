"""Cross-platform isolation and identity links (SPEC-007, RA-KNOW-010).

``docs/architecture/knowledge-loop-platforms.md`` specifies it: decision 4 ("Platforms are isolated
by default") and requirements CP-4, CP-5 and CP-12. It is platform-neutral, as decision 1 asks: the
rules are written here once, over ``ChatPlatform``, and hold the same way when Discord and Slack run
together. The adapters only translate, and keep their own refusals as a second line.

``Isolation`` sits between the knowledge service and the started adapters. It answers a question
only through the adapter that received it, and shares only through ``publish``. What it guarantees:

- **Platforms are isolated (CP-4).** An answer cites only evidence from the asker's own platform and
  workspace. Evidence from anywhere else is withheld from every answer, whether or not the operator
  linked the asker to someone there, because a reply carries no approval receipt. It can reach
  another platform only as a reviewed share.
- **Access is checked twice (CP-5).** ``screen`` checks each source at retrieval time, and ``reply``
  runs the same check again just before posting: the asker must still be able to read the source, as
  the platform says at call time, and for a ``thread`` answer everyone who can read the thread must
  be able to read the source's place. If any citation fails, the answer is replaced by an abstention
  that cites nothing, because the text was written from that evidence and could repeat it.
- **Shares need a receipt (CP-12), and an identity link to cross platforms.** ``share`` refuses a
  candidate without an approval receipt for that exact destination and candidate, then one sent to a
  place the operator did not list as a share destination, before asking any platform anything. A
  citation from the destination's own workspace must be readable by the requester. A citation from
  another platform needs the identity link the operator recorded between the requester and one
  person there, and that person must be able to read it, asked of the source platform at call time.
- **Links are honored only as recorded.** A link joins exactly one Discord user in one guild and one
  Slack user in one workspace. It is never inferred from a matching id, name or address, never
  chained through a third identity, and never widens a reply. Two links that give the requester two
  different identities in the same workspace contradict each other, and neither is honored.

Every refusal raises a ``PlatformError`` and sends nothing. A failed or unreadable access check
counts as "can't see": the check fails closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, Optional

from automation.knowledge.config import IdentityLink, KnowledgeConfig
from automation.knowledge.platform import (
    Answer,
    ApprovalReceipt,
    ChatPlatform,
    Delivery,
    Interaction,
    Normalized,
    Place,
    PlatformError,
    PlatformSelectionError,
    Principal,
    ShareCandidate,
    SourceRef,
    Visibility,
    check_receipt,
    check_visibility,
)

# Says why without naming what was withheld: the asker may not be allowed to know it exists.
ABSTENTION = "I can't answer that from evidence you can see here."

WithheldReason = Literal["another_platform", "not_visible", "audience"]
Scope = tuple[str, str]


class IsolationRefused(PlatformError):
    """A reply or share would cross a platform, workspace or audience the rules keep apart."""


class PlatformNotStarted(IsolationRefused):
    """No started adapter serves that platform and workspace, so nothing there can be asked or checked."""


class NotAShareDestination(IsolationRefused):
    """The operator did not list that place as a share destination."""


class NotLinked(IsolationRefused):
    """No single identity link joins the requester to someone on the source's platform and workspace."""


class SourceNotVisible(IsolationRefused):
    """The person a share relies on can't read a cited source now."""


class Withheld(Normalized):
    source: SourceRef
    reason: WithheldReason


class Screening(Normalized):
    """The sources an answer may cite, and the ones it may not, in the order they were given."""

    usable: tuple[SourceRef, ...]
    withheld: tuple[Withheld, ...]


class Replied(Normalized):
    """What was posted, and what was withheld at reply time. ``answer`` is the abstention if any was."""

    delivery: Delivery
    answer: Answer
    withheld: tuple[Withheld, ...]


def _scope(item: Principal | Place | SourceRef | Interaction) -> Scope:
    return (item.platform, item.workspace)


def _sees(platform: ChatPlatform, principal: Principal, source: SourceRef) -> bool:
    try:
        return platform.can_see(principal, source)
    except PlatformError:
        # The platform couldn't say. Citing the source anyway would trust what was never checked.
        return False


class _Readers:
    """Who can read each place, asked once per place within one check and never kept past it."""

    def __init__(self, platform: ChatPlatform) -> None:
        self._platform = platform
        self._seen: dict[Place, Optional[frozenset[Principal]]] = {}

    def covers(self, reply: Place, source: Place) -> bool:
        readers, allowed = self._members(reply), self._members(source)
        return readers is not None and allowed is not None and readers <= allowed

    def _members(self, place: Place) -> Optional[frozenset[Principal]]:
        if place not in self._seen:
            try:
                self._seen[place] = self._platform.audience(place).members
            except PlatformError:
                self._seen[place] = None
        return self._seen[place]


class Isolation:
    """The started adapters, the operator's share destinations and identity links, and the rules between them."""

    def __init__(self, config: KnowledgeConfig, platforms: Sequence[ChatPlatform]) -> None:
        configured = {(entry.kind, entry.workspace): entry for entry in config.platforms}
        self._platforms: dict[Scope, ChatPlatform] = {}
        for platform in platforms:
            key = (platform.kind, platform.workspace)
            entry = configured.get(key)
            # An adapter the operator did not configure would answer, and be trusted, where nobody
            # allowed it.
            if entry is None or frozenset(platform.roles) != entry.roles:
                raise PlatformSelectionError(f"{platform.kind} workspace {platform.workspace} is not configured with those roles")
            if key in self._platforms:
                raise PlatformSelectionError(f"{platform.kind} workspace {platform.workspace} is started twice")
            self._platforms[key] = platform
        self._destinations: dict[Scope, frozenset[Place]] = {
            key: frozenset(entry.destination(share) for share in entry.share_destinations) for key, entry in configured.items()
        }
        self._links: tuple[IdentityLink, ...] = config.identity_links

    def screen(self, interaction: Interaction, sources: Iterable[SourceRef], visibility: Visibility) -> Screening:
        """Which sources an answer to ``interaction``, posted as ``visibility``, may cite now."""
        platform = self._own(interaction)
        checked = check_visibility(visibility)
        # A fresh reader cache for each check: one kept between retrieval and reply would make the
        # second check repeat the first instead of asking again.
        readers = _Readers(platform)
        usable: list[SourceRef] = []
        withheld: list[Withheld] = []
        for source in sources:
            reason = self._withhold(platform, interaction, source, checked, readers)
            if reason is None:
                usable.append(source)
            else:
                withheld.append(Withheld(source=source, reason=reason))
        return Screening(usable=tuple(usable), withheld=tuple(withheld))

    def reply(self, interaction: Interaction, answer: Answer, visibility: Visibility) -> Replied:
        """Post ``answer`` where it was asked, or an abstention if any citation fails its second check."""
        platform = self._own(interaction)
        screening = self.screen(interaction, answer.citations, visibility)
        sent = answer if not screening.withheld else Answer(text=ABSTENTION, abstained=True)
        delivery = platform.reply(interaction, sent, visibility)
        return Replied(delivery=delivery, answer=sent, withheld=screening.withheld)

    def share(
        self,
        requester: Principal,
        destination: Place,
        candidate: ShareCandidate,
        receipt: Optional[ApprovalReceipt],
    ) -> Delivery:
        """Publish a reviewed candidate for ``requester``, who asked on the destination's platform."""
        where = _scope(destination)
        platform = self._platforms.get(where)
        if platform is None:
            raise PlatformNotStarted(f"no started adapter serves {destination.platform} workspace {destination.workspace}")
        # The receipt and the destination are checked on their values alone, so a share that lacks
        # either asks no platform anything (CP-12).
        check_receipt(destination, candidate, receipt)
        if destination not in self._destinations.get(where, frozenset()):
            raise NotAShareDestination(
                f"{destination.platform} channel {destination.thread or destination.channel} is not a share destination"
            )
        if _scope(requester) != where:
            # A link is what carries evidence across. A requester from the source's side would
            # need none, and the share would cross without one.
            raise IsolationRefused("the requester must be on the destination's platform and workspace")
        for source in candidate.citations:
            reader = requester if _scope(source) == where else self._linked(requester, source)
            source_platform = self._platforms.get(_scope(source))
            if source_platform is None:
                raise PlatformNotStarted(
                    f"no started adapter serves {source.platform} workspace {source.workspace}, so message {source.message} "
                    "can't be checked"
                )
            if not _sees(source_platform, reader, source):
                raise SourceNotVisible(
                    f"{source.platform} message {source.message} can't be read by the person the share relies on"
                )
        return platform.publish(destination, candidate, receipt)

    # Internals

    def _own(self, interaction: Interaction) -> ChatPlatform:
        platform = self._platforms.get(_scope(interaction))
        if platform is None:
            raise PlatformNotStarted(f"no started adapter serves {interaction.platform} workspace {interaction.workspace}")
        return platform

    def _withhold(
        self,
        platform: ChatPlatform,
        interaction: Interaction,
        source: SourceRef,
        visibility: Visibility,
        readers: _Readers,
    ) -> Optional[WithheldReason]:
        if _scope(source) != _scope(interaction):
            return "another_platform"
        if not _sees(platform, interaction.principal, source):
            return "not_visible"
        if visibility == "thread" and not readers.covers(interaction.place, source.place):
            return "audience"
        return None

    def _linked(self, requester: Principal, source: SourceRef) -> Principal:
        found: set[Principal] = set()
        for link in self._links:
            discord, slack = link.principals()
            for mine, theirs in ((discord, slack), (slack, discord)):
                if mine == requester and _scope(theirs) == _scope(source):
                    found.add(theirs)
        if not found:
            raise NotLinked(
                f"no identity link joins {requester.platform} user {requester.user} to anyone in "
                f"{source.platform} workspace {source.workspace}"
            )
        if len(found) > 1:
            raise NotLinked(
                f"{requester.platform} user {requester.user} is linked to more than one user in "
                f"{source.platform} workspace {source.workspace}, so no link is honored"
            )
        return found.pop()
