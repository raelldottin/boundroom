"""The Slack adapter in the ``source`` role (SPEC-007, RA-KNOW-011).

``TestSlackContract`` in ``test_knowledge_slack.py`` already runs the contract's capture tests on
this role. These are the slice's own acceptance tests: CP-7 (capture waits on the terms decision),
CP-6 for capture (only allowlisted public and private channels), CP-8 (edits and deletions
propagate, and deletions reach the data rules at once), ``fetch_since`` itself (pages of 200,
partial pages, rate limits, thread replies, restarts and the ``app_rate_limited`` gap backfill),
and the retention-policy tests (SD-11).

Everything runs on ``SyntheticSlack``, with synthetic payloads, a fake clock and no network.
``RecordingRules`` stands in for RA-KNOW-014's ``DataRules``, which is not on this branch: it has
the same two methods capture calls, ``on_event`` and ``reconcile``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from automation.knowledge import slack_adapter as sa
from automation.knowledge.config import KnowledgeConfigError
from automation.knowledge.platform import CaptureEvent, PlatformError, PlatformSelectionError, RoleNotEnabled, start_platforms
from automation.knowledge.slack_adapter import (
    HISTORY_PAGE,
    HISTORY_SCOPES,
    RESUME_OVERLAP_SECONDS,
    SOURCE_SCOPES,
    CaptureFailed,
    ExcludedConversation,
    NotAllowlisted,
    SlackAdapter,
    bot_scopes,
    event_subscriptions,
    scope_reasons,
)
from automation.knowledge.slack_inbound import RATE_LIMIT_NOTICE_SECONDS

from automation.tests.test_knowledge_platform import BOTH, INTERFACE, SOURCE, capture_all
from automation.tests.test_knowledge_slack import (
    BOT,
    C1,
    C2,
    CONNECT,
    DM,
    ENVIRON,
    GROUP_DM,
    PRIVATE,
    PUBLIC_ELSEWHERE,
    START,
    TEAM,
    U1,
    U2,
    RecordingEnviron,
    RecordingRules,
    SyntheticSlack,
    answer,
    block_network,
    conversation,
    error,
    factory,
    fixture,
    place,
    principal,
    settings,
    start,
)

DAY = 24 * 3600.0


def at(moment: float) -> str:
    """A wall-clock time as a Slack timestamp."""
    micros = round(moment * 1_000_000)
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def kinds(events: list[CaptureEvent]) -> list[tuple[str, str]]:
    return [(event.kind, event.source.message) for event in events]


def history_calls(world: SyntheticSlack, method: str = "conversations.history") -> list[dict[str, Any]]:
    return [params for _, called, params in world.calls if called == method]


def rules(world: SyntheticSlack) -> RecordingRules:
    assert world.rules is not None
    return world.rules


def restart(world: SyntheticSlack, **overrides: Any) -> SlackAdapter:
    """Stop the running adapter's connections and start a new one on the same Slack, as after a restart."""
    for connection in world.connections:
        connection.close()
    adapter, _ = start(SOURCE, world=world, **overrides)
    return adapter


# CP-7: capture waits on the terms decision, and is least privilege


def test_cp7_the_source_role_asks_for_the_history_scopes_with_reasons_and_nothing_for_dms():
    assert bot_scopes(SOURCE) == SOURCE_SCOPES == {"channels:history", "groups:history", "channels:read", "groups:read"}
    assert HISTORY_SCOPES == {"channels:history", "groups:history"}
    assert not {"im:history", "mpim:history", "im:read", "mpim:read"} & bot_scopes(BOTH)
    reasons = scope_reasons(BOTH)
    assert set(reasons) == bot_scopes(BOTH)
    assert all(reason.strip() for reason in reasons.values())
    assert "allowlisted" in reasons["channels:history"] and "by id" in reasons["groups:history"]
    assert event_subscriptions(SOURCE) == {"message.channels", "message.groups", "app_uninstalled", "tokens_revoked"}
    assert event_subscriptions(BOTH) == event_subscriptions(SOURCE) | {"app_mention"}


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"slack_terms_decision": None}, "recorded decision on Slack's API terms"),
        ({"app": "distributed"}, "needs an internal app"),
        ({"app": None}, "needs an internal app"),
    ],
    ids=["no decision", "distributed app", "no app declared"],
)
def test_cp7_the_configuration_refuses_capture_without_the_decision_and_an_internal_app(overrides, reason):
    with pytest.raises(KnowledgeConfigError, match=reason):
        settings(SOURCE, **overrides)


def test_cp7_the_factory_refuses_capture_without_the_data_rules_before_any_token_is_read():
    world = SyntheticSlack()
    environ = RecordingEnviron(ENVIRON)
    chosen = settings(SOURCE)

    with pytest.raises(PlatformSelectionError, match="data rules"):
        start_platforms(chosen.selections, {"slack": factory(world, chosen, environ, rules=None)})
    assert (environ.read, world.opened, world.calls, world.connections) == ([], [], [], [])


@pytest.mark.parametrize(
    "change, given_rules, reason",
    [
        ({"slack_terms_decision": None}, True, "recorded decision"),
        ({"app": "distributed"}, True, "internal app"),
        ({"retention_days": None}, True, "needs a retention"),
        ({}, False, "data rules"),
    ],
    ids=["no decision", "distributed app", "no retention", "no data rules"],
)
def test_cp7_the_adapter_itself_refuses_capture_however_its_configuration_was_made(change, given_rules, reason):
    # model_copy skips validation, as any code that builds a configuration by hand could.
    world = SyntheticSlack()
    config = settings(BOTH).config.platforms[0].model_copy(update=change)
    client = sa.SlackClient(TEAM, world.web(sa.SecretStr("synthetic-bot-credential-marker")), clock=world.clock)
    receiver = sa.SocketModeReceiver(TEAM, lambda: world.open_socket("wss://wss-synthetic.slack.invalid/x"), clock=world.clock)

    with pytest.raises(PlatformSelectionError, match=reason):
        SlackAdapter(config, client, receiver, clock=world.clock, rules=RecordingRules() if given_rules else None)
    assert (world.calls, world.connections) == ([], [])


def test_cp7_with_the_decision_an_internal_app_and_the_data_rules_capture_runs():
    adapter, world = start(SOURCE)
    ref = world.post(U1, C1, "the deploy runbook moved")

    events, _ = capture_all(adapter, place(C1), None)

    assert kinds(events) == [("message", ref.message)]
    assert adapter.scopes == SOURCE_SCOPES


# CP-6 for capture: only allowlisted public and private channels


def test_a_channel_nobody_allowlisted_for_capture_is_refused_before_any_call():
    adapter, world = start(SOURCE)
    before = list(world.calls)

    with pytest.raises(NotAllowlisted):
        adapter.fetch_since(place(PUBLIC_ELSEWHERE), None)
    assert world.calls == before


@pytest.mark.parametrize("channel", [DM, GROUP_DM, CONNECT], ids=["DM", "group DM", "Slack Connect"])
def test_a_dm_group_dm_or_slack_connect_channel_is_never_captured_even_when_allowlisted(channel):
    adapter, world = start(SOURCE, channels={"source": [C1, channel]})

    with pytest.raises(ExcludedConversation):
        adapter.fetch_since(place(channel), None)
    assert world.history_requests == []


def test_a_private_channel_allowlisted_by_its_own_id_is_captured():
    adapter, world = start(SOURCE, channels={"source": [C1, PRIVATE]})
    ref = world.post(U1, PRIVATE, "the private runbook moved")

    events, _ = capture_all(adapter, place(PRIVATE), None)

    assert kinds(events) == [("message", ref.message)]


def test_a_channel_that_became_shared_with_another_organization_stops_being_captured():
    adapter, world = start(SOURCE)
    _, cursor = capture_all(adapter, place(C1), None)
    world.conversations[C1] = conversation(C1, is_shared=True, is_ext_shared=True)

    with pytest.raises(ExcludedConversation, match="Slack Connect"):
        adapter.fetch_since(place(C1), cursor)


def test_live_events_from_other_channels_dms_or_slack_connect_are_not_captured():
    adapter, world = start(SOURCE, channels={"source": [C1, DM]})
    world.post(U1, PUBLIC_ELSEWHERE, "not allowlisted")
    message = {"type": "message", "user": U1, "text": "from elsewhere", "ts": f"{int(START)}.000900", "channel": C1}
    world.deliver(world.event(message, is_ext_shared_channel=True))
    world.deliver(world.event({**message, "channel": DM, "channel_type": "im", "ts": f"{int(START)}.000901"}))
    world.deliver(world.event(message, enterprise_id="E0SYNTH0001"))

    events, _ = capture_all(adapter, place(C1), None)

    assert events == []
    assert adapter.dropped["channel not allowlisted for capture"] == 1
    assert adapter.dropped["excluded conversation"] == 3


def test_bots_system_notices_and_the_adapter_itself_are_not_captured_but_people_s_files_and_broadcasts_are():
    adapter, world = start(SOURCE)
    base = {"type": "message", "user": U1, "text": "synthetic", "channel": C1}
    skipped = [
        {**base, "bot_id": "B0SYNTHBOT2"},
        {**base, "subtype": "bot_message"},
        {**base, "subtype": "channel_join"},
        {**base, "user": BOT},
        {key: value for key, value in base.items() if key != "user"},
    ]
    kept = [{**base, "subtype": "file_share"}, {**base, "subtype": "thread_broadcast", "thread_ts": f"{int(START)}.000001"}]
    for index, event in enumerate(skipped + kept):
        world.deliver(world.event({**event, "ts": f"{int(START)}.0005{index:02d}"}))

    events, _ = capture_all(adapter, place(C1), None)

    assert [event.source.message for event in events] == [f"{int(START)}.000505", f"{int(START)}.000506"]
    assert events[1].source.thread == f"{int(START)}.000001"
    assert adapter.dropped["not captured"] == len(skipped)


# CP-8: edits and deletions propagate


def test_cp8_a_deletion_reaches_the_data_rules_at_once_and_carries_no_text():
    adapter, world = start(BOTH)
    ref = world.post(U1, C1, "a message someone will delete")
    capture_all(adapter, place(C1), None)

    world.delete(ref)
    # Asking for questions reads the same deliveries, so the deletion is handed over before any capture runs.
    assert list(adapter.interactions()) == []

    assert rules(world).events == [
        (TEAM, {"type": "message", "subtype": "message_deleted", "channel": C1, "deleted_ts": ref.message})
    ]
    assert (C1, ref.message) in rules(world).withdrawn
    page = adapter.fetch_since(place(C1), None)
    assert ("delete", ref.message) in kinds(list(page.events))


def test_cp8_an_edited_or_deleted_source_stops_answering_in_the_source_role():
    adapter, world = start(SOURCE)
    world.set_members(C1, [U1])
    original = world.post(U1, C1, "the runbook is in docs/")
    edited = world.edit(original, "the runbook is in the wiki")

    assert adapter.can_see(principal(U1), original) is False
    assert adapter.can_see(principal(U1), edited) is True
    world.delete(edited)
    assert adapter.can_see(principal(U1), edited) is False

    events, _ = capture_all(adapter, place(C1), None)
    assert [(event.kind, event.source) for event in events] == [
        ("message", original),
        ("edit", edited),
        ("delete", edited),
    ]


def test_cp8_a_message_changed_that_is_not_an_edit_is_not_captured_and_a_tombstone_is_a_deletion():
    adapter, world = start(SOURCE)
    parent = world.post(U1, C1, "a thread starts here")
    capture_all(adapter, place(C1), None)
    message = {"type": "message", "user": U1, "text": "a thread starts here", "ts": parent.message, "thread_ts": parent.message}
    changed = {"type": "message", "subtype": "message_changed", "channel": C1}
    # Slack sends this when a reply is added: the reply count changed, the text didn't.
    world.deliver(world.event({**changed, "message": {**message, "reply_count": 1}, "previous_message": message}))
    tombstone = {"type": "message", "subtype": "tombstone", "ts": parent.message, "thread_ts": parent.message, "text": "deleted"}
    world.deliver(world.event({**changed, "message": tombstone, "previous_message": message}))

    page = adapter.fetch_since(place(C1), None)

    assert kinds([event for event in page.events if event.source.message == parent.message]) == [
        ("message", parent.message),
        ("delete", parent.message),
    ]
    assert rules(world).events[-1][1]["deleted_ts"] == parent.message


@pytest.mark.parametrize("kind", ["app_uninstalled", "tokens_revoked"])
def test_sd1_an_uninstall_or_revoked_tokens_reaches_the_data_rules(kind):
    adapter, world = start(SOURCE)
    world.deliver(world.event({"type": kind}))

    capture_all(adapter, place(C1), None)

    assert rules(world).events == [(TEAM, {"type": kind})]


# fetch_since: pages, rate limits, threads and restarts


def test_capture_reads_history_from_before_it_started_in_pages_of_200():
    world = SyntheticSlack()
    posted = [world.post(U1, C1, f"message {n}", at=START - 3600).message for n in range(450)]
    adapter, _ = start(SOURCE, world=world)

    pages = []
    cursor: Optional[str] = None
    while True:
        page = adapter.fetch_since(place(C1), cursor)
        pages.append(page)
        cursor = page.cursor
        if page.complete:
            break

    assert HISTORY_PAGE == 200
    reads = history_calls(world)
    assert [params["limit"] for params in reads] == [200, 200, 200]
    assert [params.get("cursor") for params in reads] == [None, "page_200", "page_400"]
    assert reads[0]["oldest"] == at(START - 30 * DAY) and reads[0]["latest"] == at(START) and reads[0]["inclusive"] is True
    assert [len(page.events) for page in pages] == [200, 200, 50]
    assert [page.complete for page in pages] == [False, False, True]
    assert sorted(event.source.message for page in pages for event in page.events) == sorted(posted)


def test_a_partial_page_with_more_to_come_is_followed_to_the_end():
    world = SyntheticSlack()
    posted = {world.post(U1, C1, f"message {n}", at=START - 60).message for n in range(120)}
    world.short_pages = 50
    adapter, _ = start(SOURCE, world=world)

    events, _ = capture_all(adapter, place(C1), None)

    assert len(history_calls(world)) == 3
    assert {event.source.message for event in events} == posted


def test_capture_reads_the_replies_of_threads_from_before_it_started():
    world = SyntheticSlack()
    parent = world.post(U1, C1, "the deploy failed", at=START - 600)
    replies = [world.post(U2, C1, f"reply {n}", thread=parent.message, at=START - 500) for n in range(3)]
    adapter, _ = start(SOURCE, world=world)

    events, _ = capture_all(adapter, place(C1), None)

    assert [event.source for event in events] == [parent, *replies]
    assert [params["ts"] for params in history_calls(world, "conversations.replies")] == [parent.message]


def test_a_thread_s_place_captures_only_that_thread():
    adapter, world = start(SOURCE)
    parent = world.post(U1, C1, "the deploy failed")
    reply = world.post(U2, C1, "it was the cache", thread=parent.message)
    world.post(U1, C1, "unrelated")

    events, _ = capture_all(adapter, place(C1, parent.message), None)

    assert [event.source for event in events] == [reply]
    assert history_calls(world) == []
    assert [params["ts"] for params in history_calls(world, "conversations.replies")] == [parent.message]


def test_a_rate_limited_page_mid_capture_keeps_its_cursor_and_waits_before_slack_is_asked_again():
    world = SyntheticSlack()
    posted = {world.post(U1, C1, f"message {n}", at=START - 3600).message for n in range(450)}
    adapter, _ = start(SOURCE, world=world)
    first = adapter.fetch_since(place(C1), None)
    world.throttle_next_history_request(30.0)

    limited = adapter.fetch_since(place(C1), first.cursor)

    assert (limited.events, limited.cursor, limited.complete, limited.retry_after_seconds) == ((), first.cursor, False, 30.0)
    waited_from = world.clock.now
    rest, _ = capture_all(adapter, place(C1), limited.cursor)
    assert world.clock.now - waited_from >= 30.0
    assert {event.source.message for event in [*first.events, *rest]} == posted


def test_transient_failures_are_retried_within_the_capture_budget():
    adapter, world = start(SOURCE)
    world.script("conversations.history", *[answer({"ok": False, "error": "internal_error"}, status=500)] * 3)
    ref = world.post(U1, C1, "one")

    events, _ = capture_all(adapter, place(C1), None)

    assert kinds(events) == [("message", ref.message)]
    assert len(history_calls(world)) == 4


@pytest.mark.parametrize("code", ["not_in_channel", "channel_not_found"])
def test_a_channel_that_can_no_longer_be_read_fails_for_this_run_and_the_others_carry_on(code):
    adapter, world = start(SOURCE)
    world.script("conversations.history", error(code))
    ref = world.post(U1, C2, "two")

    with pytest.raises(CaptureFailed, match="can no longer be read"):
        adapter.fetch_since(place(C1), None)
    events, _ = capture_all(adapter, place(C2), None)
    assert kinds(events) == [("message", ref.message)]


@pytest.mark.parametrize(
    "cursor",
    [
        "not a cursor",
        json.dumps({"channel": C1, "journal": "x", "position": 0, "watermark": "1.000000", "gaps_through": "1.0", "x": 1}),
        json.dumps({"channel": C2, "journal": "x", "position": 0, "watermark": "1.000000", "gaps_through": "1.0"}),
    ],
    ids=["garbage", "unknown field", "another channel"],
)
def test_a_cursor_this_adapter_did_not_issue_for_this_place_is_refused_before_any_call(cursor):
    adapter, world = start(SOURCE)
    before = list(world.calls)

    with pytest.raises(PlatformError, match="cursor"):
        adapter.fetch_since(place(C1), cursor)
    assert world.calls == before


def test_a_cursor_from_before_a_restart_reads_history_from_its_watermark():
    adapter, world = start(SOURCE)
    world.post(U1, C1, "before the restart")
    _, cursor = capture_all(adapter, place(C1), None)
    assert cursor is not None
    watermark = json.loads(cursor)["watermark"]
    world.clock.now += 600
    for connection in world.connections:
        connection.close()
    missed = world.post(U2, C1, "sent while the service was down")
    world.clock.now += 60

    later = restart(world)
    events, _ = capture_all(later, place(C1), cursor)

    assert ("message", missed.message) in kinds(events)
    resumed = history_calls(world)[-1]
    assert (resumed["oldest"], resumed["latest"]) == (at(float(watermark) - RESUME_OVERLAP_SECONDS), at(world.clock.now))


def test_a_journal_that_overflows_reads_history_again_instead_of_losing_events(monkeypatch):
    monkeypatch.setattr(sa, "JOURNAL_LIMIT", 2)
    adapter, world = start(SOURCE)
    _, cursor = capture_all(adapter, place(C1), None)
    posted = [world.post(U1, C1, f"message {n}").message for n in range(3)]
    # The journal starts again when the third event is read, a moment after the messages were sent.
    world.clock.now += 1

    events, _ = capture_all(adapter, place(C1), cursor)

    assert set(posted) <= {event.source.message for event in events}


# The app_rate_limited gap backfill (part 2, "Timers and budgets")


def rate_limited(world: SyntheticSlack, minute: int) -> dict[str, Any]:
    data = fixture("app_rate_limited")
    data["envelope_id"] = f"synthetic-envelope-ratelimited-{minute}"
    data["payload"]["minute_rate_limited"] = minute
    return data


def test_a_gap_slack_dropped_is_read_again_from_history_and_its_deletions_reconciled():
    adapter, world = start(SOURCE)
    kept = world.post(U1, C1, "sent before Slack started dropping events")
    _, cursor = capture_all(adapter, place(C1), None)
    rules(world).captured.add((C1, kept.message))
    minute = int(world.clock.now) // 60 * 60

    world.deliver(rate_limited(world, minute))
    world.live = False
    dropped = world.post(U2, C1, "sent while Slack dropped events")
    world.delete(kept)
    slowed = adapter.fetch_since(place(C1), cursor)
    # Events resume, and a minute later the gap is closed.
    world.live = True
    world.clock.now += 2 * RATE_LIMIT_NOTICE_SECONDS
    events, _ = capture_all(adapter, place(C1), slowed.cursor)

    assert (slowed.complete, slowed.retry_after_seconds) == (False, RATE_LIMIT_NOTICE_SECONDS)
    assert ("message", dropped.message) in kinds(events)
    ((workspace, channel, present, since, until),) = rules(world).reconciled
    assert (workspace, channel, since, until) == (TEAM, C1, float(minute), float(minute + RATE_LIMIT_NOTICE_SECONDS))
    assert dropped.message in present and kept.message not in present
    assert (C1, kept.message) in rules(world).withdrawn
    gap_read = history_calls(world)[-1]
    assert (gap_read["oldest"], gap_read["latest"]) == (at(minute), at(minute + RATE_LIMIT_NOTICE_SECONDS))


def test_while_slack_is_dropping_events_capture_slows_and_never_claims_to_be_complete():
    adapter, world = start(SOURCE)
    _, cursor = capture_all(adapter, place(C1), None)
    world.deliver(rate_limited(world, int(world.clock.now) // 60 * 60))

    pages = [adapter.fetch_since(place(C1), cursor) for _ in range(3)]

    assert all(not page.complete and page.retry_after_seconds == RATE_LIMIT_NOTICE_SECONDS for page in pages)
    assert rules(world).reconciled == []


def test_a_thread_s_gap_is_read_again_without_reconciling_the_whole_channel():
    adapter, world = start(SOURCE)
    parent = world.post(U1, C1, "the deploy failed")
    _, cursor = capture_all(adapter, place(C1, parent.message), None)
    world.deliver(rate_limited(world, int(world.clock.now) // 60 * 60))
    world.live = False
    reply = world.post(U2, C1, "it was the cache", thread=parent.message)
    world.live = True
    world.clock.now += 2 * RATE_LIMIT_NOTICE_SECONDS

    events, _ = capture_all(adapter, place(C1, parent.message), cursor)

    assert [event.source for event in events] == [reply]
    assert rules(world).reconciled == []


def test_a_restart_before_a_gap_is_read_again_still_reads_it():
    adapter, world = start(SOURCE)
    _, cursor = capture_all(adapter, place(C1), None)
    minute = int(world.clock.now) // 60 * 60
    # Slack names the minute it started dropping, so events from earlier in it were delivered.
    first = world.post(U1, C1, "delivered just before Slack started dropping events")
    world.deliver(rate_limited(world, minute))
    world.live = False
    dropped = world.post(U2, C1, "sent while Slack dropped events")

    page = adapter.fetch_since(place(C1), cursor)

    assert ("message", first.message) in kinds(list(page.events))
    # The watermark stops at the gap that hasn't been read again, however far the journal got.
    assert page.cursor is not None and float(json.loads(page.cursor)["watermark"]) <= minute
    world.live = True
    world.clock.now += 60
    later = restart(world)
    events, _ = capture_all(later, place(C1), page.cursor)
    assert ("message", dropped.message) in kinds(events)


# Retention (SD-11): older than the shorter retention is neither captured nor kept


@pytest.mark.parametrize("operator_days, workspace_days, kept", [(30, 7, 7), (3, 7, 3)])
def test_retention_history_is_read_back_only_to_the_shorter_retention(operator_days, workspace_days, kept):
    adapter, world = start(SOURCE, retention_days=operator_days, workspace_retention_days=workspace_days)

    capture_all(adapter, place(C1), None)

    assert history_calls(world)[0]["oldest"] == at(START - kept * DAY)


def test_retention_a_message_older_than_retention_is_not_captured_even_if_slack_returns_it():
    world = SyntheticSlack()
    old = world.post(U1, C1, "older than the workspace keeps", at=START - 8 * DAY)
    recent = world.post(U1, C1, "recent enough", at=START - 6 * DAY)
    adapter, _ = start(SOURCE, world=world, workspace_retention_days=7)
    messages = [world.messages[(C1, recent.message)], world.messages[(C1, old.message)]]
    world.script("conversations.history", answer({"ok": True, "messages": messages, "has_more": False}))

    events, _ = capture_all(adapter, place(C1), None)

    assert kinds(events) == [("message", recent.message)]


def test_retention_a_live_edit_of_an_old_message_is_not_captured_but_its_deletion_propagates():
    world = SyntheticSlack()
    old = world.post(U1, C1, "older than the workspace keeps", at=START - 8 * DAY)
    adapter, _ = start(SOURCE, world=world, workspace_retention_days=7)
    _, cursor = capture_all(adapter, place(C1), None)

    world.edit(old, "edited long after")
    world.delete(old)
    events, _ = capture_all(adapter, place(C1), cursor)

    assert kinds(events) == [("delete", old.message)]
    assert adapter.dropped["past retention"] == 1
    assert rules(world).events[-1][1]["deleted_ts"] == old.message


def test_retention_text_that_ages_past_retention_while_it_waits_is_not_returned_or_kept():
    adapter, world = start(SOURCE, workspace_retention_days=7)
    world.post(U1, C1, "synthetic text that will age out")
    adapter.fetch_since(place(C2), None)  # reads the delivery into the journal
    assert "synthetic text that will age out" in repr(vars(adapter))

    world.clock.now += 7 * DAY + 1
    events, _ = capture_all(adapter, place(C1), None)

    assert events == []
    assert "synthetic text that will age out" not in repr(vars(adapter))
    # Nothing in the window is within retention any more, so history isn't read at all.
    assert all(params["channel"] != C1 for params in history_calls(world))


def test_retention_a_cursor_older_than_retention_reads_only_from_the_horizon():
    adapter, world = start(SOURCE, workspace_retention_days=7)
    _, cursor = capture_all(adapter, place(C1), None)
    world.clock.now += 10 * DAY

    later = restart(world, workspace_retention_days=7)
    capture_all(later, place(C1), cursor)

    assert history_calls(world)[-1]["oldest"] == at(world.clock.now - 7 * DAY)


# Hermetic: no network, ever


def test_capture_runs_with_every_socket_call_blocked(monkeypatch):
    block_network(monkeypatch)
    world = SyntheticSlack()
    before = world.post(U1, C1, "from before", at=START - 60)
    adapter, _ = start(SOURCE, world=world)
    live = world.post(U2, C1, "live")
    world.delete(live)

    events, _ = capture_all(adapter, place(C1), None)

    assert kinds(events) == [("message", before.message), ("message", live.message), ("delete", live.message)]


def test_an_interface_only_adapter_still_never_captures():
    adapter, world = start(INTERFACE)
    world.post(U1, C1, "not captured")

    with pytest.raises(RoleNotEnabled):
        adapter.fetch_since(place(C1), None)
    assert world.history_requests == []


@pytest.mark.parametrize("roles", [frozenset({"interface"}), frozenset({"source"}), frozenset({"interface", "source"})])
def test_sd8_the_adapter_asks_for_exactly_the_scopes_the_data_rules_allow(roles):
    """The adapter's scopes and the data rules' SD-8 list can't drift apart."""
    from automation.knowledge import slack_adapter, slack_data

    assert slack_data.scope_problems(roles, slack_adapter.scope_reasons(roles)) == []
    allowed = {scope for role in roles for scope in slack_data.SCOPES_BY_ROLE[role]}
    assert slack_adapter.bot_scopes(roles) == allowed
