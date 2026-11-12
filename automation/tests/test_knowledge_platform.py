"""The chat-platform interface (SPEC-007, RA-KNOW-006) and the contract every adapter keeps.

``ChatPlatformContract`` is the contract. It runs here on the fake platform, once as Discord and once
as Slack. An adapter's tests (RA-KNOW-008 for Discord, RA-KNOW-009 for Slack) subclass it and override
``make`` with a factory that builds the adapter over its synthetic payloads, so every adapter is held
to the same tests. Then come the normalized models, CP-1 and CP-2 on ``start_platforms``, and the
trust boundary: nothing in ``automation/knowledge`` may import the supervisor, the benchmark, a
subprocess or a GitHub client, directly or through another module.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, cast

import pytest
from pydantic import ValidationError

from automation.knowledge.platform import (
    FAKE_TIME,
    Answer,
    ApprovalReceipt,
    Audience,
    CaptureEvent,
    CapturePage,
    ChatPlatform,
    FakePlatform,
    Interaction,
    OutsideWorkspace,
    Place,
    PlatformError,
    PlatformKind,
    PlatformSelection,
    PlatformSelectionError,
    Principal,
    ReceiptRefused,
    Role,
    RoleNotEnabled,
    Sent,
    ShareCandidate,
    SourceRef,
    Visibility,
    VisibilityRefused,
    start_platforms,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = REPO_ROOT / "automation/knowledge"

INTERFACE: frozenset[Role] = frozenset({"interface"})
SOURCE: frozenset[Role] = frozenset({"source"})
BOTH: frozenset[Role] = frozenset({"interface", "source"})
OTHER_KIND: dict[str, PlatformKind] = {"discord": "slack", "slack": "discord"}


class SyntheticPlatform(Protocol):
    """The platform's side of an adapter under test: what people do, and what the adapter sent."""

    sent: list[Sent]
    history_requests: list[tuple[Place, Optional[str]]]

    def ask(self, user: str, channel: str, text: str, *, thread: Optional[str] = None) -> Interaction: ...

    def set_members(self, channel: str, users: list[str]) -> None: ...

    def post(self, user: str, channel: str, text: str, *, thread: Optional[str] = None) -> SourceRef: ...

    def edit(self, ref: SourceRef, text: str) -> SourceRef: ...

    def delete(self, ref: SourceRef) -> None: ...

    def throttle_next_history_request(self, retry_after_seconds: float) -> None: ...


@dataclass
class Adapter:
    """One adapter under contract, the synthetic platform behind it, and ids valid on that platform."""

    platform: ChatPlatform
    world: SyntheticPlatform
    channels: tuple[str, str]
    users: tuple[str, str]

    def principal(self, user: str) -> Principal:
        return Principal(platform=self.platform.kind, workspace=self.platform.workspace, user=user)

    def place(self, channel: str, thread: Optional[str] = None) -> Place:
        return Place(platform=self.platform.kind, workspace=self.platform.workspace, channel=channel, thread=thread)


AdapterFactory = Callable[[frozenset[Role]], Adapter]

FAKE_IDS: dict[str, tuple[str, tuple[str, str], tuple[str, str]]] = {
    "discord": (
        "1100000000000000001",
        ("1100000000000000101", "1100000000000000102"),
        ("1100000000000000201", "1100000000000000202"),
    ),
    "slack": ("T0FAKE0001", ("C0FAKE0001", "C0FAKE0002"), ("U0FAKE0001", "U0FAKE0002")),
}


def fake_adapter(kind: PlatformKind) -> AdapterFactory:
    workspace, channels, users = FAKE_IDS[kind]

    def make(roles: frozenset[Role]) -> Adapter:
        fake = FakePlatform(PlatformSelection(kind=kind, workspace=workspace, roles=roles))
        return Adapter(platform=fake, world=fake, channels=channels, users=users)

    return make


def elsewhere(item: Any, **changes: str) -> Any:
    """The same value in another workspace, or on the other platform, for the isolation checks."""
    return item.model_copy(update=changes)


class ChatPlatformContract:
    """Every ``ChatPlatform`` adapter passes these. Subclass it and override ``make``."""

    @pytest.fixture
    def make(self) -> AdapterFactory:
        raise NotImplementedError("an adapter's contract tests supply its factory")

    # Identity and roles

    @pytest.mark.parametrize("roles", [INTERFACE, SOURCE, BOTH], ids=["interface", "source", "both"])
    def test_it_is_a_chat_platform_serving_exactly_its_selection(self, make, roles):
        adapter = make(roles)

        assert isinstance(adapter.platform, ChatPlatform)
        assert adapter.platform.kind in ("discord", "slack")
        assert adapter.platform.workspace
        assert adapter.platform.roles == roles

    def test_a_source_only_adapter_takes_no_questions_and_sends_nothing(self, make):
        adapter = make(SOURCE)
        channel, _ = adapter.channels
        question = Interaction(
            id="1",
            via="command",
            principal=adapter.principal(adapter.users[0]),
            place=adapter.place(channel),
            text="anything?",
            received_at=FAKE_TIME,
        )
        candidate, receipt = reviewed_share(adapter, adapter.place(channel))

        with pytest.raises(RoleNotEnabled):
            adapter.platform.interactions()
        with pytest.raises(RoleNotEnabled):
            adapter.platform.reply(question, Answer(text="no"), "ephemeral")
        with pytest.raises(RoleNotEnabled):
            adapter.platform.publish(adapter.place(channel), candidate, receipt)
        assert adapter.world.sent == []

    # Asking and answering

    def test_questions_arrive_normalized_from_its_own_workspace(self, make):
        adapter = make(INTERFACE)
        channel, _ = adapter.channels
        first, second = adapter.users
        parent = adapter.world.post(first, channel, "the deploy runbook moved")
        adapter.world.ask(first, channel, "where is the runbook?")
        adapter.world.ask(second, channel, "since when?", thread=parent.message)

        questions = list(adapter.platform.interactions())

        assert [q.text for q in questions] == ["where is the runbook?", "since when?"]
        assert [q.principal for q in questions] == [adapter.principal(first), adapter.principal(second)]
        assert [q.place for q in questions] == [adapter.place(channel), adapter.place(channel, parent.message)]

    @pytest.mark.parametrize("visibility", ["ephemeral", "thread"])
    def test_a_reply_lands_in_the_place_it_was_asked(self, make, visibility):
        adapter = make(INTERFACE)
        channel, _ = adapter.channels
        parent = adapter.world.post(adapter.users[0], channel, "the deploy runbook moved")
        adapter.world.ask(adapter.users[0], channel, "where is the runbook?", thread=parent.message)
        (question,) = adapter.platform.interactions()

        delivery = adapter.platform.reply(question, Answer(text="docs/runbook.md", citations=(parent,)), visibility)

        assert delivery.place == question.place
        assert adapter.world.sent == [Sent(delivery=delivery, text="docs/runbook.md", visibility=visibility)]

    @pytest.mark.parametrize("visibility", ["public", "channel", "dm", "Ephemeral", "", None])
    def test_a_reply_with_any_other_visibility_is_refused(self, make, visibility):
        adapter = make(INTERFACE)
        adapter.world.ask(adapter.users[0], adapter.channels[0], "where is the runbook?")
        (question,) = adapter.platform.interactions()

        with pytest.raises(VisibilityRefused):
            adapter.platform.reply(question, Answer(text="docs/runbook.md"), cast(Visibility, visibility))
        assert adapter.world.sent == []

    @pytest.mark.parametrize("move", ["workspace", "platform"])
    def test_a_question_from_elsewhere_gets_no_reply(self, make, move):
        adapter = make(INTERFACE)
        adapter.world.ask(adapter.users[0], adapter.channels[0], "where is the runbook?")
        (question,) = adapter.platform.interactions()
        field = {"workspace": {"workspace": "W0ELSEWHERE"}, "platform": {"platform": OTHER_KIND[adapter.platform.kind]}}[move]
        foreign = question.model_copy(
            update={"principal": elsewhere(question.principal, **field), "place": elsewhere(question.place, **field)}
        )

        with pytest.raises(OutsideWorkspace):
            adapter.platform.reply(foreign, Answer(text="docs/runbook.md"), "ephemeral")
        assert adapter.world.sent == []

    # Access, checked at call time

    def test_can_see_asks_the_platform_at_the_time_of_the_call(self, make):
        adapter = make(INTERFACE)
        channel, _ = adapter.channels
        first, second = adapter.users
        ref = adapter.world.post(first, channel, "the deploy runbook moved")

        adapter.world.set_members(channel, [first])
        assert adapter.platform.can_see(adapter.principal(first), ref) is True
        assert adapter.platform.can_see(adapter.principal(second), ref) is False

        adapter.world.set_members(channel, [second])
        assert adapter.platform.can_see(adapter.principal(first), ref) is False
        assert adapter.platform.can_see(adapter.principal(second), ref) is True

    def test_a_deleted_source_or_an_old_revision_cannot_be_seen(self, make):
        adapter = make(INTERFACE)
        channel, _ = adapter.channels
        reader = adapter.principal(adapter.users[0])
        adapter.world.set_members(channel, [adapter.users[0]])
        original = adapter.world.post(adapter.users[0], channel, "the runbook is in docs/")

        edited = adapter.world.edit(original, "the runbook is in the wiki")
        assert edited.message == original.message and edited.revision != original.revision
        assert adapter.platform.can_see(reader, original) is False
        assert adapter.platform.can_see(reader, edited) is True

        adapter.world.delete(edited)
        assert adapter.platform.can_see(reader, edited) is False

    @pytest.mark.parametrize("move", ["source", "principal", "both"])
    def test_can_see_never_reaches_another_workspace_or_platform(self, make, move):
        adapter = make(INTERFACE)
        channel, _ = adapter.channels
        adapter.world.set_members(channel, [adapter.users[0]])
        ref = adapter.world.post(adapter.users[0], channel, "the deploy runbook moved")
        reader = adapter.principal(adapter.users[0])

        for field in ({"workspace": "W0ELSEWHERE"}, {"platform": OTHER_KIND[adapter.platform.kind]}):
            source = elsewhere(ref, **field) if move in ("source", "both") else ref
            principal = elsewhere(reader, **field) if move in ("principal", "both") else reader
            assert adapter.platform.can_see(principal, source) is False

    def test_the_audience_is_who_can_read_the_place_now(self, make):
        adapter = make(INTERFACE)
        channel, _ = adapter.channels
        first, second = adapter.users

        adapter.world.set_members(channel, [first, second])
        audience = adapter.platform.audience(adapter.place(channel))
        assert audience == Audience(
            place=adapter.place(channel), members=frozenset({adapter.principal(first), adapter.principal(second)})
        )

        adapter.world.set_members(channel, [second])
        assert adapter.platform.audience(adapter.place(channel)).members == frozenset({adapter.principal(second)})

        with pytest.raises(OutsideWorkspace):
            adapter.platform.audience(elsewhere(adapter.place(channel), workspace="W0ELSEWHERE"))

    # Capture, for the source role only

    def test_an_interface_only_adapter_reads_no_history(self, make):
        adapter = make(INTERFACE)
        adapter.world.post(adapter.users[0], adapter.channels[0], "the deploy runbook moved")

        with pytest.raises(RoleNotEnabled):
            adapter.platform.fetch_since(adapter.place(adapter.channels[0]), None)
        assert adapter.world.history_requests == []

    def test_capture_returns_messages_edits_and_deletions_after_the_cursor(self, make):
        adapter = make(SOURCE)
        channel, other = adapter.channels
        author = adapter.users[0]
        first = adapter.world.post(author, channel, "one")
        second = adapter.world.post(author, channel, "two")
        adapter.world.post(author, other, "not in this channel")
        edited = adapter.world.edit(first, "one, corrected")
        adapter.world.delete(second)

        events, cursor = capture_all(adapter.platform, adapter.place(channel), None)

        assert [(e.kind, e.source) for e in events] == [
            ("message", first),
            ("message", second),
            ("edit", edited),
            ("delete", second),
        ]
        assert [e.text for e in events] == ["one", "two", "one, corrected", None]
        assert all(e.author == adapter.principal(author) for e in events if e.kind != "delete")

        third = adapter.world.post(author, channel, "three")
        later, _ = capture_all(adapter.platform, adapter.place(channel), cursor)
        assert [(e.kind, e.source) for e in later] == [("message", third)]

    def test_a_rate_limited_page_is_incomplete_and_keeps_its_cursor(self, make):
        adapter = make(SOURCE)
        channel, _ = adapter.channels
        ref = adapter.world.post(adapter.users[0], channel, "one")
        adapter.world.throttle_next_history_request(30.0)

        page = adapter.platform.fetch_since(adapter.place(channel), None)

        assert (page.events, page.cursor, page.complete, page.retry_after_seconds) == ((), None, False, 30.0)
        events, _ = capture_all(adapter.platform, adapter.place(channel), page.cursor)
        assert [e.source for e in events] == [ref]

    def test_capture_stays_in_its_own_workspace(self, make):
        adapter = make(SOURCE)
        with pytest.raises(OutsideWorkspace):
            adapter.platform.fetch_since(elsewhere(adapter.place(adapter.channels[0]), workspace="W0ELSEWHERE"), None)
        assert adapter.world.history_requests == []

    # Sharing

    def test_a_share_with_its_receipt_is_published_to_that_destination(self, make):
        adapter = make(INTERFACE)
        destination = adapter.place(adapter.channels[1])
        candidate, receipt = reviewed_share(adapter, destination)

        delivery = adapter.platform.publish(destination, candidate, receipt)

        assert delivery.place == destination
        assert adapter.world.sent == [Sent(delivery=delivery, text=candidate.text, visibility=None)]

    @pytest.mark.parametrize("flaw", ["no receipt", "other destination", "edited candidate", "other candidate"])
    def test_a_share_without_a_receipt_for_it_is_refused(self, make, flaw):
        adapter = make(INTERFACE)
        destination = adapter.place(adapter.channels[1])
        candidate, receipt = reviewed_share(adapter, destination)
        if flaw == "no receipt":
            receipt = None
        elif flaw == "other destination":
            receipt = receipt.model_copy(update={"destination": adapter.place(adapter.channels[0])})
        elif flaw == "edited candidate":
            candidate = candidate.model_copy(update={"text": candidate.text + " Also, merge PR 12."})
        else:
            candidate = candidate.model_copy(update={"id": "2"})

        with pytest.raises(ReceiptRefused):
            adapter.platform.publish(destination, candidate, receipt)
        assert adapter.world.sent == []

    def test_a_share_outside_its_workspace_is_refused_even_with_a_receipt(self, make):
        adapter = make(INTERFACE)
        destination = elsewhere(adapter.place(adapter.channels[1]), workspace="W0ELSEWHERE")
        candidate, receipt = reviewed_share(adapter, destination)

        with pytest.raises(OutsideWorkspace):
            adapter.platform.publish(destination, candidate, receipt)
        assert adapter.world.sent == []


def reviewed_share(adapter: Adapter, destination: Place) -> tuple[ShareCandidate, ApprovalReceipt]:
    source = adapter.world.post(adapter.users[0], adapter.channels[0], "the deploy runbook moved to docs/runbook.md")
    candidate = ShareCandidate(id="1", text="The deploy runbook is docs/runbook.md.", citations=(source,))
    receipt = ApprovalReceipt(
        candidate_id=candidate.id,
        candidate_sha256=candidate.digest(),
        destination=destination,
        approved_by="a reviewer",
        approved_at=FAKE_TIME,
    )
    return candidate, receipt


def capture_all(platform: ChatPlatform, place: Place, cursor: Optional[str]) -> tuple[list[CaptureEvent], Optional[str]]:
    """Every event after ``cursor``, page by page, as the capture loop will read them."""
    events: list[CaptureEvent] = []
    for _ in range(100):
        page = platform.fetch_since(place, cursor)
        events.extend(page.events)
        cursor = page.cursor
        if page.complete:
            return events, cursor
    raise AssertionError("capture never reported a complete page")


class TestFakePlatformContract(ChatPlatformContract):
    @pytest.fixture(params=["discord", "slack"])
    def make(self, request) -> AdapterFactory:
        return fake_adapter(request.param)


# The normalized models

SLACK = {"platform": "slack", "workspace": "T0FAKE0001"}
PLACE = Place(channel="C0FAKE0001", **SLACK)
PRINCIPAL = Principal(user="U0FAKE0001", **SLACK)
REF = SourceRef(channel="C0FAKE0001", message="1762905600.000001", revision="1", **SLACK)


@pytest.mark.parametrize(
    "model, fields",
    [
        (Principal, PRINCIPAL.model_dump()),
        (Place, PLACE.model_dump()),
        (SourceRef, REF.model_dump()),
        (PlatformSelection, {"kind": "slack", "workspace": "T0FAKE0001", "roles": INTERFACE}),
        (Answer, {"text": "docs/runbook.md"}),
    ],
)
def test_normalized_models_refuse_unknown_keys(model, fields):
    model(**fields)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model(**fields, token="xoxb-not-a-real-token")


@pytest.mark.parametrize("value", ["", " C1", "C1\n", "C1 C2", "a/b", "#general", "x" * 129])
def test_ids_are_plain_tokens(value):
    with pytest.raises(ValidationError):
        Place(platform="slack", workspace="T0FAKE0001", channel=value)


def test_normalized_values_cannot_change_after_validation():
    with pytest.raises(ValidationError, match="frozen"):
        PLACE.channel = "C0FAKE0002"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize("field, value", [("platform", "matrix"), ("platform", "Slack"), ("workspace", 1)])
def test_only_known_platforms_and_string_ids_are_accepted(field, value):
    with pytest.raises(ValidationError):
        Principal(**{**PRINCIPAL.model_dump(), field: value})


def test_an_interaction_comes_from_one_platform_and_workspace():
    with pytest.raises(ValidationError, match="same platform and workspace"):
        Interaction(
            id="1",
            via="mention",
            principal=PRINCIPAL.model_copy(update={"workspace": "T0FAKE0002"}),
            place=PLACE,
            text="where is the runbook?",
            received_at=FAKE_TIME,
        )


def test_an_audience_has_members_only_from_its_own_workspace():
    with pytest.raises(ValidationError, match="platform and workspace"):
        Audience(place=PLACE, members=frozenset({PRINCIPAL.model_copy(update={"platform": "discord"})}))


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"kind": "delete", "text": "still here"}, "carries no text"),
        ({"kind": "message", "author": PRINCIPAL}, "needs its author and text"),
        ({"kind": "edit", "text": "new"}, "needs its author and text"),
        ({"kind": "message", "author": PRINCIPAL.model_copy(update={"workspace": "T0FAKE0002"}), "text": "x"}, "author"),
    ],
)
def test_capture_events_have_the_shape_of_their_kind(fields, message):
    with pytest.raises(ValidationError, match=message):
        CaptureEvent(source=REF, at=FAKE_TIME, **fields)


def test_a_capture_page_holds_only_its_place_and_is_never_complete_while_rate_limited():
    event = CaptureEvent(kind="message", source=REF, author=PRINCIPAL, text="one", at=FAKE_TIME)
    with pytest.raises(ValidationError, match="outside the fetched place"):
        CapturePage(place=PLACE.model_copy(update={"channel": "C0FAKE0002"}), events=(event,), complete=True)
    with pytest.raises(ValidationError, match="outside the fetched place"):
        CapturePage(place=PLACE.model_copy(update={"thread": "1762905600.000009"}), events=(event,), complete=True)
    with pytest.raises(ValidationError, match="not complete"):
        CapturePage(place=PLACE, events=(), complete=True, retry_after_seconds=30.0)


def test_a_candidate_digest_pins_its_text_and_its_evidence():
    candidate = ShareCandidate(id="1", text="The runbook is docs/runbook.md.", citations=(REF,))
    assert candidate.digest() == ShareCandidate(id="1", text="The runbook is docs/runbook.md.", citations=(REF,)).digest()
    assert candidate.digest() != candidate.model_copy(update={"text": "The runbook is gone."}).digest()
    assert candidate.digest() != candidate.model_copy(update={"citations": (REF.model_copy(update={"revision": "2"}),)}).digest()
    with pytest.raises(ValidationError):
        ShareCandidate(id="1", text="no evidence", citations=())


@pytest.mark.parametrize("roles", [frozenset(), frozenset({"admin"}), {"interface"}])
def test_a_selection_names_at_least_one_known_role(roles):
    with pytest.raises(ValidationError):
        PlatformSelection(kind="slack", workspace="T0FAKE0001", roles=roles)


# CP-1 and CP-2: start_platforms builds only what the operator selected


class Launches:
    """Factories that record reading a token and opening a connection, as a real adapter's would."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []

    def factory(self, kind: PlatformKind):
        def build(selection: PlatformSelection) -> ChatPlatform:
            assert selection.kind == kind, f"the {kind} factory was handed a {selection.kind} selection"
            self.log.append(("token", f"{kind}:{selection.workspace}"))
            self.log.append(("connect", f"{kind}:{selection.workspace}"))
            return FakePlatform(selection)

        return build

    def factories(self) -> dict[str, Callable[[PlatformSelection], ChatPlatform]]:
        return {"discord": self.factory("discord"), "slack": self.factory("slack")}


def selection(kind: PlatformKind, roles: frozenset[Role] = INTERFACE) -> PlatformSelection:
    return PlatformSelection(kind=kind, workspace=FAKE_IDS[kind][0], roles=roles)


def test_cp1_with_nothing_selected_no_adapter_starts_no_token_is_read_and_nothing_connects():
    launches = Launches()

    assert start_platforms([], launches.factories()) == ()
    assert launches.log == []
    # Nothing selected needs nothing installed.
    assert start_platforms([], {}) == ()


@pytest.mark.parametrize("kinds", [("discord",), ("slack",), ("discord", "slack")], ids=["discord", "slack", "both"])
def test_cp2_each_platform_is_started_on_its_own(kinds):
    launches = Launches()

    started = start_platforms([selection(kind) for kind in kinds], launches.factories())

    assert [platform.kind for platform in started] == list(kinds)
    assert {target.split(":")[0] for _, target in launches.log} == set(kinds)


def test_cp2_a_platform_selected_alone_never_reaches_the_other_platform_code():
    def never(selection: PlatformSelection) -> ChatPlatform:
        raise AssertionError(f"{selection.kind} code ran although only the other platform was selected")

    launches = Launches()
    (discord,) = start_platforms([selection("discord")], {"discord": launches.factory("discord"), "slack": never})
    (slack,) = start_platforms([selection("slack")], {"discord": never, "slack": launches.factory("slack")})

    assert (discord.kind, slack.kind) == ("discord", "slack")


def test_cp2_a_started_platform_has_only_the_roles_selected():
    (platform,) = start_platforms([selection("discord", INTERFACE)], Launches().factories())

    assert platform.roles == INTERFACE
    with pytest.raises(RoleNotEnabled):
        platform.fetch_since(Place(platform="discord", workspace=platform.workspace, channel="1100000000000000101"), None)


@pytest.mark.parametrize(
    "selections, factories, message",
    [
        ([selection("slack"), selection("slack", BOTH)], ("discord", "slack"), "selected twice"),
        ([selection("discord"), selection("slack")], ("discord",), "no adapter is installed for slack"),
    ],
    ids=["duplicate", "not installed"],
)
def test_a_bad_selection_is_refused_before_any_platform_starts(selections, factories, message):
    launches = Launches()

    with pytest.raises(PlatformSelectionError, match=message):
        start_platforms(selections, {kind: launches.factory(kind) for kind in factories})
    assert launches.log == []


def test_two_workspaces_on_one_platform_start_two_adapters():
    second = PlatformSelection(kind="slack", workspace="T0FAKE0002", roles=INTERFACE)

    started = start_platforms([selection("slack"), second], Launches().factories())

    assert [p.workspace for p in started] == ["T0FAKE0001", "T0FAKE0002"]


@pytest.mark.parametrize(
    "served",
    [
        PlatformSelection(kind="slack", workspace="T0FAKE0001", roles=BOTH),
        PlatformSelection(kind="slack", workspace="T0FAKE0002", roles=INTERFACE),
        PlatformSelection(kind="discord", workspace="T0FAKE0001", roles=INTERFACE),
    ],
    ids=["wider roles", "other workspace", "other platform"],
)
def test_an_adapter_that_serves_anything_but_its_selection_is_refused(served):
    with pytest.raises(PlatformSelectionError, match="does not serve exactly that selection"):
        start_platforms([selection("slack")], {"slack": lambda _: FakePlatform(served)})


def test_the_fake_refuses_a_cursor_it_did_not_issue():
    fake = FakePlatform(selection("slack", SOURCE))
    with pytest.raises(PlatformError, match="not issued"):
        fake.fetch_since(PLACE, "next-page-please")


# The trust boundary (AGENTS.md, "Knowledge Loop on Discord or Slack"; SPEC-007 decision 6)

FORBIDDEN_MODULES = (
    "automation.supervisor",  # run_next, the policy, the queue, Kanban writes
    "automation.benchmark",
    "Tools",  # the consumer sync tool and the release tooling
    "subprocess",
    "pty",
    # GitHub clients
    "github",
    "github3",
    "ghapi",
    "gidgethub",
    "githubkit",
    "agithub",
    "octokit",
)
# A dynamic import or evaluated string would hide every other import from a static scan.
FORBIDDEN_STATIC_ONLY = ("importlib",)
FORBIDDEN_BUILTINS = frozenset({"__import__", "exec", "eval"})
FORBIDDEN_OS_PREFIXES = ("system", "popen", "exec", "spawn", "posix_spawn", "fork")


def _forbidden_module(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in FORBIDDEN_MODULES + FORBIDDEN_STATIC_ONLY)


def _forbidden_os_function(name: str) -> bool:
    return name.startswith(FORBIDDEN_OS_PREFIXES)


def boundary_violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Every import or call in ``source`` that would cross the knowledge service's trust boundary."""
    tree = ast.parse(source)
    package = module if is_package else module.rpartition(".")[0]
    os_names = {"os"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os" and alias.asname:
                    os_names.add(alias.asname)
                if _forbidden_module(alias.name):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            for name in [base] + [f"{base}.{alias.name}" for alias in node.names]:
                if _forbidden_module(name) or (base == "os" and name != base and _forbidden_os_function(name[3:])):
                    found.append(f"from {base} import ... ({name})")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
                found.append(f"{func.id}()")
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in os_names
                and _forbidden_os_function(func.attr)
            ):
                found.append(f"os.{func.attr}()")
    return found


def test_the_knowledge_package_imports_nothing_across_the_trust_boundary():
    scanned = {}
    for path in sorted(KNOWLEDGE.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).with_suffix("")
        is_package = path.name == "__init__.py"
        module = ".".join(relative.parent.parts if is_package else relative.parts)
        scanned[module] = boundary_violations(path.read_text(encoding="utf-8"), module, is_package=is_package)

    assert {"automation.knowledge", "automation.knowledge.platform", "automation.knowledge.config"} <= scanned.keys()
    assert {module: found for module, found in scanned.items() if found} == {}


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "from automation.supervisor import run_next",
        "from automation import supervisor",
        "import automation.supervisor.policy",
        "from ..supervisor import policy",
        "from .. import benchmark",
        "import automation.benchmark.cleanroom",
        "from Tools import check_plugin_version",
        "import github",
        "from github import Github",
        "from ghapi.all import GhApi",
        "import githubkit",
        "import os\nos.system('ls')",
        "import os as o\no.execv('/bin/sh', [])",
        "from os import popen",
        "import importlib\nimportlib.import_module('subprocess')",
        "__import__('subprocess')",
        "exec('import subprocess')",
    ],
)
def test_the_boundary_scan_catches_each_way_across(source):
    assert boundary_violations(source, "automation.knowledge.example") != []


@pytest.mark.parametrize(
    "source", ["import os\nos.environ.get('X')", "from os import path", "import hashlib", "from . import platform"]
)
def test_the_boundary_scan_allows_ordinary_imports(source):
    assert boundary_violations(source, "automation.knowledge.example") == []


def test_importing_the_knowledge_package_loads_nothing_across_the_trust_boundary():
    # A fresh interpreter, so the modules other tests imported can't hide or fake a transitive import.
    probe = (
        "import importlib, pkgutil, sys\n"
        "import automation.knowledge as package\n"
        "for info in pkgutil.walk_packages(package.__path__, 'automation.knowledge.'):\n"
        "    importlib.import_module(info.name)\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    loaded = result.stdout.split()
    assert {"automation.knowledge.platform", "automation.knowledge.config"} <= set(loaded)
    assert [name for name in loaded if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES)] == []
