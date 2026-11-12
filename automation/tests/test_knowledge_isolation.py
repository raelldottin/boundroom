"""Cross-platform isolation and identity links (SPEC-007, RA-KNOW-010), with both adapters running together.

Every test starts the real Discord adapter and the real Slack adapter side by side, from one operator
configuration, through ``start_platforms`` and each adapter's own factory. Their transports are the
synthetic Discord and Slack worlds from ``test_knowledge_discord.py`` and ``test_knowledge_slack.py``,
so nothing here opens a connection, and every socket call is blocked for the whole module.

The acceptance is the slice's: CP-4 (platforms are isolated: no answer cites the other platform, with
or without an identity link, and a share crosses only with a link and a receipt), CP-5 (access is
checked at retrieval and again at reply) and CP-12 (a share needs a receipt for that exact
destination). After those come the identity-link rules and how ``Isolation`` is put together.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from automation.knowledge.config import KnowledgeConfig, KnowledgeSettings, RepositoryKnowledgeScope, narrow, parse
from automation.knowledge.discord_adapter import DiscordAdapter, discord_factory
from automation.knowledge.isolation import (
    ABSTENTION,
    Isolation,
    IsolationRefused,
    NotAShareDestination,
    NotLinked,
    PlatformNotStarted,
    Screening,
    SourceNotVisible,
    Withheld,
)
from automation.knowledge.platform import (
    FAKE_TIME,
    Answer,
    ApprovalReceipt,
    ChatPlatform,
    FakePlatform,
    Interaction,
    Place,
    PlatformError,
    PlatformSelection,
    PlatformSelectionError,
    Principal,
    ReceiptRefused,
    ShareCandidate,
    SourceRef,
    start_platforms,
)
from automation.knowledge.slack_adapter import SlackAdapter, slack_factory
from automation.tests import test_knowledge_discord as discord_world
from automation.tests import test_knowledge_slack as slack_world
from automation.tests.test_knowledge_platform import INTERFACE

D_GUILD, (D_C1, D_C2), (D_U1, D_U2) = (
    discord_world.GUILD,
    (discord_world.C1, discord_world.C2),
    (discord_world.U1, discord_world.U2),
)
S_TEAM, (S_C1, S_C2), (S_U1, S_U2) = slack_world.TEAM, (slack_world.C1, slack_world.C2), (slack_world.U1, slack_world.U2)
ENVIRON = {discord_world.TOKEN_ENV: discord_world.TOKEN_MARKER, **slack_world.ENVIRON}
QUESTION = "where is the deploy runbook?"
EVIDENCE = "the deploy runbook moved to docs/runbook.md"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Both adapters run on injected transports. Any attempt to reach the network fails the test.
    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an adapter tried to open a network connection")

    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, blocked)
    yield


def link(discord_user: str = D_U1, slack_user: str = S_U1, *, discord_guild: str = D_GUILD) -> dict[str, Any]:
    return {
        "discord": {"workspace": discord_guild, "user": discord_user},
        "slack": {"workspace": S_TEAM, "user": slack_user},
        "recorded_by": "the operator",
        "recorded_at": "2026-11-12",
    }


def configuration(*links: dict[str, Any]) -> KnowledgeSettings:
    """Discord and Slack enabled together, each as an interface with one share destination: channel 2."""
    platforms = [
        discord_world.entry(INTERFACE, share_destinations=[{"channel": D_C2}]),
        slack_world.entry(INTERFACE),
    ]
    return KnowledgeSettings(config=parse(KnowledgeConfig, {"platforms": platforms, "identity_links": list(links)}))


@dataclass
class Side:
    """One running platform: its adapter, the synthetic world behind it, and ids valid there."""

    platform: ChatPlatform
    world: Any
    users: tuple[str, str]
    channels: tuple[str, str]

    def principal(self, user: str) -> Principal:
        return Principal(platform=self.platform.kind, workspace=self.platform.workspace, user=user)

    def place(self, channel: str, thread: Optional[str] = None) -> Place:
        return Place(platform=self.platform.kind, workspace=self.platform.workspace, channel=channel, thread=thread)

    def ask(self, user: str, channel: str) -> Interaction:
        self.world.ask(user, channel, QUESTION)
        (interaction,) = list(self.platform.interactions())
        return interaction

    def evidence(self, *readers: str) -> SourceRef:
        """A message in channel 1 that only ``readers`` can read."""
        self.world.set_members(self.channels[0], list(readers))
        return self.world.post(readers[0], self.channels[0], EVIDENCE)

    @property
    def calls(self) -> int:
        return len(self.world.calls)


@dataclass
class Both:
    isolation: Isolation
    discord: Side
    slack: Side

    def side(self, kind: str) -> Side:
        return self.discord if kind == "discord" else self.slack

    def other(self, kind: str) -> Side:
        return self.slack if kind == "discord" else self.discord

    @property
    def sent(self) -> list[Any]:
        return self.discord.world.sent + self.slack.world.sent


def start_both(*links: dict[str, Any]) -> Both:
    dworld, sworld = discord_world.SyntheticDiscord(), slack_world.SyntheticSlack()
    settings = configuration(*links)
    factories = {
        "discord": discord_factory(settings, discord_world.Connections(dworld), environ=ENVIRON, clock=dworld.clock),
        "slack": slack_factory(
            settings,
            sworld.web,
            sworld.open_socket,
            environ=ENVIRON,
            clock=sworld.clock,
            monotonic=sworld.clock,
            sleep=sworld.clock.sleep,
            random=lambda: 0.5,
        ),
    }
    discord, slack = start_platforms(settings.selections, factories)
    assert isinstance(discord, DiscordAdapter) and isinstance(slack, SlackAdapter)
    return Both(
        isolation=Isolation(settings.config, (discord, slack)),
        discord=Side(discord, dworld, (D_U1, D_U2), (D_C1, D_C2)),
        slack=Side(slack, sworld, (S_U1, S_U2), (S_C1, S_C2)),
    )


def candidate(*citations: SourceRef, text: str = "The runbook is docs/runbook.md.") -> ShareCandidate:
    return ShareCandidate(id="share-1", text=text, citations=citations)


def receipt(shared: ShareCandidate, destination: Place) -> ApprovalReceipt:
    return ApprovalReceipt(
        candidate_id=shared.id,
        candidate_sha256=shared.digest(),
        destination=destination,
        approved_by="a reviewer",
        approved_at=FAKE_TIME,
    )


KINDS = pytest.mark.parametrize("kind", ["slack", "discord"])
LINKED = pytest.mark.parametrize("linked", [False, True], ids=["no link", "linked"])


# CP-4: platforms are isolated


@KINDS
@LINKED
def test_cp4_an_answer_never_cites_the_other_platform_with_or_without_a_link(kind, linked):
    both = start_both(*([link()] if linked else []))
    asking, other = both.side(kind), both.other(kind)
    own = asking.evidence(asking.users[0])
    # The linked person on the other platform can read this, so only isolation keeps it out.
    elsewhere = other.evidence(other.users[0])
    question = asking.ask(asking.users[0], asking.channels[0])
    before = other.calls

    screening = both.isolation.screen(question, [elsewhere, own], "ephemeral")

    assert screening == Screening(usable=(own,), withheld=(Withheld(source=elsewhere, reason="another_platform"),))
    assert other.calls == before  # the other platform is never even asked

    replied = both.isolation.reply(question, Answer(text=EVIDENCE, citations=(own, elsewhere)), "ephemeral")

    assert replied.answer == Answer(text=ABSTENTION, abstained=True)
    assert [Withheld(source=elsewhere, reason="another_platform")] == list(replied.withheld)
    assert [sent.text for sent in asking.world.sent] == [ABSTENTION]
    assert other.world.sent == [] and other.calls == before


@KINDS
def test_cp4_a_link_without_an_approved_share_still_carries_nothing(kind):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    elsewhere = other.evidence(other.users[0])
    destination = asking.place(asking.channels[1])
    before = (asking.calls, other.calls)

    with pytest.raises(ReceiptRefused):
        both.isolation.share(asking.principal(asking.users[0]), destination, candidate(elsewhere), None)

    assert (asking.calls, other.calls) == before
    assert both.sent == []


@KINDS
def test_cp4_a_reviewed_share_with_a_link_carries_evidence_across(kind):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    elsewhere = other.evidence(other.users[0])
    destination = asking.place(asking.channels[1])
    shared = candidate(elsewhere)
    before = other.calls

    delivery = both.isolation.share(asking.principal(asking.users[0]), destination, shared, receipt(shared, destination))

    assert delivery.place == destination
    assert [(sent.delivery.place, sent.text, sent.visibility) for sent in asking.world.sent] == [(destination, shared.text, None)]
    # Access was asked of the source platform, at call time, and nothing was posted there.
    assert other.calls > before and other.world.sent == []


@KINDS
def test_cp4_a_reviewed_share_without_a_link_is_refused_before_the_other_platform_is_asked(kind):
    both = start_both()
    asking, other = both.side(kind), both.other(kind)
    elsewhere = other.evidence(other.users[0])
    destination = asking.place(asking.channels[1])
    shared = candidate(elsewhere)
    before = other.calls

    with pytest.raises(NotLinked):
        both.isolation.share(asking.principal(asking.users[0]), destination, shared, receipt(shared, destination))

    assert other.calls == before and both.sent == []


@KINDS
def test_cp4_a_share_goes_only_to_an_operator_destination_even_with_a_receipt(kind):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    elsewhere = other.evidence(other.users[0])
    # Channel 1 accepts questions but is not a share destination.
    destination = asking.place(asking.channels[0])
    shared = candidate(elsewhere)
    before = (asking.calls, other.calls)

    with pytest.raises(NotAShareDestination):
        both.isolation.share(asking.principal(asking.users[0]), destination, shared, receipt(shared, destination))

    assert (asking.calls, other.calls) == before and both.sent == []


@KINDS
def test_cp4_a_share_is_made_for_someone_on_the_destinations_platform(kind):
    # A requester from the source's side would need no link, so the share would cross without one.
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    elsewhere = other.evidence(other.users[0])
    destination = asking.place(asking.channels[1])
    shared = candidate(elsewhere)

    with pytest.raises(IsolationRefused, match="destination's platform"):
        both.isolation.share(other.principal(other.users[0]), destination, shared, receipt(shared, destination))

    assert both.sent == []


@KINDS
def test_cp4_an_answer_is_posted_only_by_the_adapter_that_received_the_question(kind):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    question = asking.ask(asking.users[0], asking.channels[0])
    # The same question, re-labelled as if it had been asked on the other platform.
    forged = Interaction(
        id=question.id,
        via=question.via,
        principal=other.principal(other.users[0]),
        place=other.place(other.channels[0]),
        text=question.text,
        received_at=question.received_at,
    )

    with pytest.raises(PlatformError):
        both.isolation.reply(forged, Answer(text="the runbook is in the wiki"), "ephemeral")

    assert both.sent == []


def test_cp4_a_question_from_a_workspace_nobody_started_is_not_answered():
    both = start_both()
    stray = Interaction(
        id="synthetic-envelope-elsewhere",
        via="command",
        principal=Principal(platform="slack", workspace="T0ELSEWHERE", user=S_U1),
        place=Place(platform="slack", workspace="T0ELSEWHERE", channel=S_C1),
        text=QUESTION,
        received_at=FAKE_TIME,
    )

    with pytest.raises(PlatformNotStarted):
        both.isolation.screen(stray, [], "ephemeral")
    with pytest.raises(PlatformNotStarted):
        both.isolation.reply(stray, Answer(text="anything"), "ephemeral")
    assert both.sent == []


# CP-5: access is checked twice


def _revoke(side: Side, ref: SourceRef, change: str) -> None:
    if change == "removed":
        # The asker leaves the channel; someone else still reads it.
        side.world.set_members(side.channels[0], [side.users[1]])
    elif change == "deleted":
        side.world.delete(ref)
    else:
        side.world.edit(ref, "the runbook is gone")


@KINDS
@pytest.mark.parametrize("change", ["removed", "deleted", "edited"])
def test_cp5_evidence_seen_at_retrieval_but_not_at_reply_is_withheld_with_an_abstention(kind, change):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    ref = asking.evidence(asking.users[0])
    question = asking.ask(asking.users[0], asking.channels[0])

    assert both.isolation.screen(question, [ref], "ephemeral").usable == (ref,)
    _revoke(asking, ref, change)
    replied = both.isolation.reply(question, Answer(text=EVIDENCE, citations=(ref,)), "ephemeral")

    assert replied.answer == Answer(text=ABSTENTION, abstained=True)
    assert list(replied.withheld) == [Withheld(source=ref, reason="not_visible")]
    assert [sent.text for sent in asking.world.sent] == [ABSTENTION]
    assert other.world.sent == []


@KINDS
def test_cp5_evidence_still_readable_at_reply_is_answered_and_was_checked_both_times(kind):
    both = start_both()
    asking = both.side(kind)
    ref = asking.evidence(asking.users[0])
    question = asking.ask(asking.users[0], asking.channels[0])
    checks: list[tuple[Principal, SourceRef]] = []
    real = asking.platform.can_see

    def counted(principal: Principal, source: SourceRef) -> bool:
        checks.append((principal, source))
        return real(principal, source)

    asking.platform.can_see = counted  # ty: ignore[invalid-assignment]
    assert both.isolation.screen(question, [ref], "ephemeral").usable == (ref,)
    answer = Answer(text="The runbook moved to docs/runbook.md.", citations=(ref,))
    replied = both.isolation.reply(question, answer, "ephemeral")

    assert replied.answer == answer and replied.withheld == ()
    assert [sent.text for sent in asking.world.sent] == [answer.text]
    assert checks == [(question.principal, ref)] * 2


@KINDS
def test_cp5_a_thread_answer_cites_only_what_everyone_in_the_thread_can_read(kind):
    both = start_both()
    asking = both.side(kind)
    ref = asking.evidence(asking.users[0])  # channel 1: only the asker
    asking.world.set_members(asking.channels[1], list(asking.users))  # channel 2: the asker and one more
    question = asking.ask(asking.users[0], asking.channels[1])

    assert both.isolation.screen(question, [ref], "ephemeral").usable == (ref,)
    assert both.isolation.screen(question, [ref], "thread").withheld == (Withheld(source=ref, reason="audience"),)

    replied = both.isolation.reply(question, Answer(text=EVIDENCE, citations=(ref,)), "thread")

    assert replied.answer.abstained and replied.answer.citations == ()
    assert [(sent.text, sent.visibility) for sent in asking.world.sent] == [(ABSTENTION, "thread")]


def test_cp5_an_access_check_the_platform_cannot_answer_counts_as_cannot_see():
    both = start_both()
    ref = both.discord.evidence(D_U1)
    question = both.discord.ask(D_U1, D_C1)
    both.discord.world.offline = True

    assert both.isolation.screen(question, [ref], "ephemeral").withheld == (Withheld(source=ref, reason="not_visible"),)


@KINDS
def test_cp5_a_linked_person_who_lost_access_carries_nothing_across(kind):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    elsewhere = other.evidence(other.users[0])
    destination = asking.place(asking.channels[1])
    shared = candidate(elsewhere)
    approved = receipt(shared, destination)
    # Reviewed while the linked person could read it; they lose access before it is published.
    other.world.set_members(other.channels[0], [other.users[1]])

    with pytest.raises(SourceNotVisible):
        both.isolation.share(asking.principal(asking.users[0]), destination, shared, approved)

    assert both.sent == []


@KINDS
def test_cp5_a_same_platform_share_needs_the_requester_to_read_its_evidence(kind):
    both = start_both()
    asking = both.side(kind)
    ref = asking.evidence(asking.users[1])  # only the other user reads it
    destination = asking.place(asking.channels[1])
    shared = candidate(ref)

    with pytest.raises(SourceNotVisible):
        both.isolation.share(asking.principal(asking.users[0]), destination, shared, receipt(shared, destination))

    delivery = both.isolation.share(asking.principal(asking.users[1]), destination, shared, receipt(shared, destination))
    assert [sent.delivery for sent in asking.world.sent] == [delivery]


# CP-12: shares need a receipt for that exact destination


def _receipts(both: Both, kind: str, shared: ShareCandidate) -> dict[str, Optional[ApprovalReceipt]]:
    asking, other = both.side(kind), both.other(kind)
    destination = asking.place(asking.channels[1])
    changed = candidate(*shared.citations, text="Something the reviewer never saw.")
    return {
        "none": None,
        "the other platform's destination": receipt(shared, other.place(other.channels[1])),
        "another channel": receipt(shared, asking.place(asking.channels[0])),
        "a thread of the destination": receipt(shared, asking.place(asking.channels[1], thread=asking.channels[1])),
        "another workspace": receipt(shared, destination.model_copy(update={"workspace": "W0ELSEWHERE"})),
        "a changed candidate": receipt(changed, destination),
        "another candidate": receipt(shared, destination).model_copy(update={"candidate_id": "share-2"}),
    }


RECEIPT_CASES = [
    "none",
    "the other platform's destination",
    "another channel",
    "a thread of the destination",
    "another workspace",
    "a changed candidate",
    "another candidate",
]


@KINDS
@pytest.mark.parametrize("case", RECEIPT_CASES)
def test_cp12_a_share_without_a_receipt_for_that_exact_destination_and_candidate_is_refused(kind, case):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    shared = candidate(other.evidence(other.users[0]))
    before = (asking.calls, other.calls)

    with pytest.raises(ReceiptRefused):
        both.isolation.share(
            asking.principal(asking.users[0]), asking.place(asking.channels[1]), shared, _receipts(both, kind, shared)[case]
        )

    # Refused on its values alone: neither platform was asked anything, and nothing was sent.
    assert (asking.calls, other.calls) == before and both.sent == []


@KINDS
def test_cp12_each_adapter_refuses_the_other_platforms_receipt_when_called_directly(kind):
    both = start_both(link())
    asking, other = both.side(kind), both.other(kind)
    shared = candidate(asking.evidence(asking.users[0]))
    borrowed = receipt(shared, other.place(other.channels[1]))

    with pytest.raises(ReceiptRefused):
        asking.platform.publish(asking.place(asking.channels[1]), shared, borrowed)

    assert both.sent == []


# Identity links are honored only as the configuration records them


@KINDS
def test_a_link_speaks_only_for_the_two_people_it_records(kind):
    both = start_both(link())  # Discord user 1 and Slack user 1
    asking, other = both.side(kind), both.other(kind)
    destination = asking.place(asking.channels[1])

    # The second user on each side can read the evidence, but nobody linked them.
    readable_by_second = other.evidence(other.users[1])
    shared = candidate(readable_by_second)
    with pytest.raises(NotLinked):
        both.isolation.share(asking.principal(asking.users[1]), destination, shared, receipt(shared, destination))

    # The linked requester relies on their own linked identity, not on whoever else can read it.
    with pytest.raises(SourceNotVisible):
        both.isolation.share(asking.principal(asking.users[0]), destination, shared, receipt(shared, destination))

    assert both.sent == []


def test_two_links_giving_one_person_two_identities_in_a_workspace_are_not_honored():
    both = start_both(link(D_U1, S_U1), link(D_U2, S_U1))
    elsewhere = both.discord.evidence(D_U1, D_U2)
    destination = both.slack.place(S_C2)
    shared = candidate(elsewhere)

    with pytest.raises(NotLinked, match="more than one"):
        both.isolation.share(both.slack.principal(S_U1), destination, shared, receipt(shared, destination))

    assert both.sent == []


def test_a_link_to_a_platform_that_is_not_running_cannot_be_checked():
    both = start_both(link())
    discord_only = Isolation(configuration(link()).config, (both.discord.platform,))
    slack_message = SourceRef(platform="slack", workspace=S_TEAM, channel=S_C1, message="1794441600.000001", revision="0")
    destination = both.discord.place(D_C2)
    shared = candidate(slack_message)

    with pytest.raises(PlatformNotStarted):
        discord_only.share(both.discord.principal(D_U1), destination, shared, receipt(shared, destination))

    assert both.sent == []


def test_the_repository_narrowing_a_platform_away_drops_its_links():
    # Links live in the operator's file only. A narrowed configuration keeps a link only while both
    # of its platforms are still enabled, so the link can't outlive what it joined.
    config = configuration(link()).config
    narrowed = narrow(
        config, parse(RepositoryKnowledgeScope, {"platforms": [{"kind": "slack", "workspace": S_TEAM, "enabled": False}]})
    )

    assert narrowed.identity_links == ()


# Putting Isolation together


def test_isolation_refuses_an_adapter_the_operator_did_not_configure():
    both = start_both()
    stray = FakePlatform(PlatformSelection(kind="slack", workspace="T0ELSEWHERE", roles=INTERFACE))

    with pytest.raises(PlatformSelectionError, match="not configured"):
        Isolation(configuration().config, (both.discord.platform, stray))
    with pytest.raises(PlatformSelectionError, match="started twice"):
        Isolation(configuration().config, (both.discord.platform, both.discord.platform))


def test_isolation_refuses_an_adapter_running_other_roles_than_configured():
    both = start_both()
    wider = FakePlatform(PlatformSelection(kind="slack", workspace=S_TEAM, roles=frozenset({"interface", "source"})))

    with pytest.raises(PlatformSelectionError, match="not configured"):
        Isolation(configuration().config, (both.discord.platform, wider))
