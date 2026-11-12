"""Tests for the Slack data rules (SPEC-007 part 2, RA-KNOW-014).

Each test names the rule it covers, SD-1 to SD-11 in ``docs/architecture/knowledge-loop-slack.md``.
Everything runs on a fake clock against in-memory and temporary-directory stores, with synthetic
payloads and no network.
"""

from __future__ import annotations

import ast
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Iterable

import pytest
from pydantic import ValidationError

from automation.knowledge import slack_data
from automation.knowledge.slack_data import (
    DEDUPE_RETENTION_SECONDS,
    QUESTION_HOLD_LIMIT_SECONDS,
    SCOPES_BY_ROLE,
    Alert,
    DataRuleError,
    DataRules,
    Encryption,
    FakeDirectoryStore,
    FakeStore,
    MessageRef,
    Refused,
    RulesState,
    StoredRecord,
    UnencryptedStore,
    WorkspacePolicy,
    add_business_days,
    encryption_problems,
    require_tls,
    require_use,
    scope_problems,
)

DAY = 24 * 3600.0
HOUR = 3600.0
# Monday 2026-11-09, 12:00 UTC.
MONDAY_NOON = datetime(2026, 11, 9, 12, tzinfo=timezone.utc).timestamp()

TEAM = "T0001"
OTHER_TEAM = "T0002"
CHANNEL = "C0001"
UNLISTED = "C0999"
ALICE = "U0001"
BOB = "U0002"

ENCRYPTED = Encryption(at_rest="AES-256-GCM", key_ref="env:KNOWLEDGE_STORE_KEY", in_transit="local")
INTERFACE_SCOPES = dict(SCOPES_BY_ROLE["interface"])
SOURCE_SCOPES = {**SCOPES_BY_ROLE["interface"], **SCOPES_BY_ROLE["source"]}


class FakeClock:
    def __init__(self, now: float = MONDAY_NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FlakyStore(FakeStore):
    """A store whose deletes fail while ``failing`` is set."""

    def __init__(self, name: str) -> None:
        super().__init__(name, ENCRYPTED)
        self.failing = False

    def delete(self, keys: Collection[str]) -> int:
        if self.failing:
            raise OSError("disk unavailable")
        return super().delete(keys)


def opaque(data: bytes) -> bytes:
    # Not encryption: it only makes the raw bytes differ from the plaintext, so the SD-7 probe has a
    # store that passes. Real stores encrypt.
    return base64.b64encode(data)[::-1]


def unopaque(data: bytes) -> bytes:
    return base64.b64decode(data[::-1])


def source_policy(workspace: str = TEAM, **changes: Any) -> WorkspacePolicy:
    fields: dict[str, Any] = {
        "workspace": workspace,
        "roles": frozenset({"interface", "source"}),
        "app": "internal",
        "terms_decision": "docs/decisions/slack-terms.md",
        "source_channels": frozenset({CHANNEL}),
        "retention_days": 90,
        "scopes": SOURCE_SCOPES,
    }
    fields.update(changes)
    return WorkspacePolicy(**fields)


def interface_policy(workspace: str = TEAM) -> WorkspacePolicy:
    return WorkspacePolicy(workspace=workspace, scopes=INTERFACE_SCOPES)


def ref(ts: float, channel: str = CHANNEL, workspace: str = TEAM) -> MessageRef:
    return MessageRef(workspace=workspace, channel=channel, ts=f"{ts:.6f}")


def message(at: MessageRef, user: str = ALICE, text: str = "the deploy runbook is in docs/ops", stored_at: float = MONDAY_NOON):
    return StoredRecord(
        key=f"msg:{at.workspace}:{at.channel}:{at.ts}",
        kind="message",
        workspace=at.workspace,
        channel=at.channel,
        message=at,
        user=user,
        body=text,
        stored_at=stored_at,
    )


def derived(key: str, *sources: MessageRef, text: str = "summary: deploys follow docs/ops") -> StoredRecord:
    return StoredRecord(
        key=key, kind="derived", workspace=sources[0].workspace, sources=frozenset(sources), body=text, stored_at=MONDAY_NOON
    )


def dedupe(key: str, workspace: str = TEAM, stored_at: float = MONDAY_NOON) -> StoredRecord:
    return StoredRecord(key=key, kind="dedupe", workspace=workspace, body="9f2c" * 16, stored_at=stored_at)


def cursor(workspace: str = TEAM, channel: str = CHANNEL) -> StoredRecord:
    return StoredRecord(
        key=f"cursor:{workspace}:{channel}",
        kind="cursor",
        workspace=workspace,
        channel=channel,
        body="1.0",
        stored_at=MONDAY_NOON,
    )


class Harness:
    def __init__(self, tmp_path: Path, policies: Iterable[WorkspacePolicy] = (), clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.alerts: list[Alert] = []
        self.evidence = FakeStore("evidence", ENCRYPTED)
        self.hindsight = FlakyStore("hindsight")
        self.wiki = FakeDirectoryStore("wiki", tmp_path / "wiki", ENCRYPTED, seal=opaque, unseal=unopaque)
        self.dedupe = FakeStore("dedupe", ENCRYPTED)
        self.cursors = FakeStore("cursors", ENCRYPTED)
        self.questions = FakeDirectoryStore("questions", tmp_path / "questions", ENCRYPTED, seal=opaque, unseal=unopaque)
        self.stores = [self.evidence, self.hindsight, self.wiki, self.dedupe, self.cursors, self.questions]
        self.rules = DataRules(
            list(policies) or [source_policy(), source_policy(OTHER_TEAM)],
            self.stores,
            alert=self.alerts.append,
            clock=self.clock,
        )

    def scan(self, wanted) -> list[tuple[str, StoredRecord]]:
        """An independent scan of every store, not through the rules."""
        return [(store.name, record) for store in self.stores for record in store.records() if wanted(record)]

    def capture(self, at: MessageRef, user: str = ALICE, text: str = "the deploy runbook is in docs/ops") -> StoredRecord:
        record = message(at, user=user, text=text)
        self.rules.write("evidence", record)
        return record

    def populate(self, workspace: str = TEAM) -> list[MessageRef]:
        refs = [ref(MONDAY_NOON - 3600 * i, workspace=workspace) for i in (1, 2)]
        for i, at in enumerate(refs):
            self.capture(at, user=ALICE if i == 0 else BOB, text=f"note {workspace} {i}")
        self.rules.write("hindsight", derived(f"h:{workspace}", refs[0], refs[1]))
        self.rules.write("wiki", derived(f"w:{workspace}", refs[0], text=f"wiki page for {workspace}"))
        self.rules.write("dedupe", dedupe(f"d:{workspace}", workspace))
        self.rules.write("cursors", cursor(workspace))
        return refs


def in_workspace(workspace: str):
    return lambda record: record.workspace == workspace


# SD-1: purge on uninstall


@pytest.mark.parametrize("event", [{"type": "app_uninstalled"}, {"type": "tokens_revoked", "tokens": {"bot": ["B0001"]}}])
def test_sd1_an_uninstall_purges_every_store_checked_by_scanning_them(tmp_path, event):
    h = Harness(tmp_path)
    h.populate(TEAM)
    h.populate(OTHER_TEAM)
    assert {name for name, _ in h.scan(in_workspace(TEAM))} == {"evidence", "hindsight", "wiki", "dedupe", "cursors"}

    h.rules.on_event(TEAM, event)

    assert h.scan(in_workspace(TEAM)) == []
    # The directory stores are scanned as raw bytes too: no file still holds the workspace's data.
    for store in (h.wiki, h.questions):
        assert not store.raw_contains(TEAM.encode())
        assert not any(b"wiki page for T0001" in unopaque(path.read_bytes()) for path in store.root.iterdir())
    (receipt,) = h.rules.receipts()
    assert (receipt.workspace, receipt.reason, receipt.deleted, receipt.met_target) == (TEAM, event["type"], 6, True)
    assert h.rules.open_purges() == []
    # The other workspace is untouched.
    assert len(h.scan(in_workspace(OTHER_TEAM))) == 6


def test_sd1_the_purge_starts_immediately_and_repeats_join_it(tmp_path):
    h = Harness(tmp_path)
    h.populate()
    h.hindsight.failing = True

    h.rules.on_event(TEAM, {"type": "app_uninstalled"})
    h.rules.on_event(TEAM, {"type": "tokens_revoked"})

    (job,) = h.rules.open_purges()
    assert (job.reason, job.attempts) == ("app_uninstalled", 1)
    assert "hindsight: disk unavailable" in (job.last_error or "")
    # Everything but the failing store is already empty, before any tick.
    assert {name for name, _ in h.scan(in_workspace(TEAM))} == {"hindsight"}


def test_sd1_a_failed_purge_is_retried_and_alerts_at_5_and_14_business_days(tmp_path):
    h = Harness(tmp_path)
    h.populate()
    h.hindsight.failing = True
    h.rules.on_event(TEAM, {"type": "app_uninstalled"})

    # Monday noon plus 5 business days is the next Monday noon, not Saturday.
    h.clock.advance(5 * DAY)
    h.rules.tick()
    assert h.alerts == []
    h.clock.advance(2 * DAY)
    h.rules.tick()
    assert [(a.rule, a.level, a.workspace) for a in h.alerts] == [("SD-1", "warning", TEAM)]
    assert "5 business days" in h.alerts[0].message and "disk unavailable" in h.alerts[0].message

    # 14 business days is the Friday of the third week.
    h.clock.now = add_business_days(MONDAY_NOON, 14) - 1
    h.rules.tick()
    assert len(h.alerts) == 1
    h.clock.advance(1)
    h.rules.tick()
    h.rules.tick()
    assert [(a.rule, a.level) for a in h.alerts] == [("SD-1", "warning"), ("SD-1", "critical")]

    h.hindsight.failing = False
    report = h.rules.tick()
    (receipt,) = report.completed
    assert (receipt.met_target, receipt.met_deadline) == (False, False)
    assert h.scan(in_workspace(TEAM)) == []


def test_sd1_writes_for_a_workspace_are_refused_while_its_purge_is_open(tmp_path):
    h = Harness(tmp_path)
    h.populate()
    h.hindsight.failing = True
    h.rules.on_event(TEAM, {"type": "app_uninstalled"})

    with pytest.raises(Refused, match="SD-1: a purge of workspace T0001"):
        h.capture(ref(MONDAY_NOON - 60))
    with pytest.raises(Refused, match="SD-1"):
        h.rules.write("dedupe", dedupe("late-event"))


def test_sd1_a_workspace_the_operator_removes_is_purged_and_refused_after(tmp_path):
    h = Harness(tmp_path)
    h.populate()

    job = h.rules.remove_workspace(TEAM)

    assert job.reason == "operator_removed"
    assert h.scan(in_workspace(TEAM)) == []
    with pytest.raises(Refused, match="SD-1: workspace T0001 isn't configured"):
        h.rules.write("cursors", cursor())


def test_sd1_data_left_from_a_workspace_no_longer_configured_is_purged_by_the_sweep(tmp_path):
    first = Harness(tmp_path)
    first.populate(TEAM)
    first.populate(OTHER_TEAM)
    # The service restarts with the operator's configuration no longer naming OTHER_TEAM.
    restarted = DataRules([source_policy()], first.stores, alert=first.alerts.append, clock=first.clock)

    restarted.tick()

    assert first.scan(in_workspace(OTHER_TEAM)) == []
    assert [r.reason for r in restarted.receipts()] == ["operator_removed"]
    assert len(first.scan(in_workspace(TEAM))) == 6


def test_sd1_business_days_skip_weekends():
    friday = datetime(2026, 11, 13, 9, tzinfo=timezone.utc).timestamp()
    saturday = datetime(2026, 11, 14, 9, tzinfo=timezone.utc).timestamp()
    assert add_business_days(friday, 1) == datetime(2026, 11, 16, 9, tzinfo=timezone.utc).timestamp()
    assert add_business_days(saturday, 5) == datetime(2026, 11, 20, 9, tzinfo=timezone.utc).timestamp()
    assert add_business_days(MONDAY_NOON, 14) == datetime(2026, 11, 27, 12, tzinfo=timezone.utc).timestamp()


# SD-2: deletions propagate


def test_sd2_a_deleted_message_stops_answering_at_once_even_before_its_copies_are_gone(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    assert len(h.rules.read("hindsight", use="retrieval")) == 1
    h.hindsight.failing = True

    h.rules.on_event(TEAM, {"type": "message", "subtype": "message_deleted", "channel": CHANNEL, "deleted_ts": refs[1].ts})

    # The Hindsight entry derived from it couldn't be deleted, but no answer can cite it.
    assert len(h.hindsight.records()) == 1
    assert h.rules.read("hindsight", use="retrieval") == []
    assert [r.message for r in h.rules.read("evidence", use="retrieval")] == [refs[0]]
    # The wiki page cites only the other message and still answers.
    assert len(h.rules.read("wiki", use="retrieval")) == 1

    h.hindsight.failing = False
    assert h.rules.tick().settled == 1
    assert h.scan(lambda r: refs[1] in r.cites()) == []


def test_sd2_derived_copies_still_stored_after_24_hours_alert(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    h.hindsight.failing = True
    h.rules.withdraw(refs[0])

    h.clock.advance(24 * HOUR - 1)
    h.rules.tick()
    assert [a.rule for a in h.alerts] == []
    h.clock.advance(1)
    h.rules.tick()
    h.rules.tick()
    assert [(a.rule, a.level) for a in h.alerts if a.rule == "SD-2"] == [("SD-2", "critical")]
    assert "disk unavailable" in h.alerts[0].message


def test_sd2_a_withdrawn_message_outranks_any_edit_or_recapture(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    stone = h.rules.withdraw(refs[0])
    assert stone.settled
    assert h.rules.withdraw(refs[0]) is stone

    with pytest.raises(Refused, match="SD-2: message .* was withdrawn"):
        h.capture(refs[0], text="edited text arriving late")
    with pytest.raises(Refused, match="SD-2"):
        h.rules.write("wiki", derived("w:rebuilt", refs[0], refs[1]))
    # Rebuilding without the deleted message is allowed.
    h.rules.write("wiki", derived("w:rebuilt", refs[1]))


def test_sd2_a_message_missing_on_reconciliation_is_withdrawn(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    outside = ref(MONDAY_NOON - 30 * DAY)
    h.capture(outside)

    withdrawn = h.rules.reconcile(TEAM, CHANNEL, present=[refs[0].ts], since=MONDAY_NOON - DAY, until=MONDAY_NOON)

    assert [s.message for s in withdrawn] == [refs[1]]
    assert all(s.reason == "missing_on_reconciliation" for s in withdrawn)
    assert {r.message for _, r in h.scan(lambda r: r.kind == "message")} == {refs[0], outside}


def test_sd2_a_malformed_deletion_event_is_refused(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(DataRuleError, match="channel and deleted_ts"):
        h.rules.on_event(TEAM, {"type": "message", "subtype": "message_deleted", "channel": CHANNEL})
    # Other events are not the data rules' business.
    h.rules.on_event(TEAM, {"type": "app_mention", "text": "hi"})
    assert h.rules.open_purges() == []


# SD-3: a user's deletion request


def test_sd3_a_user_request_removes_their_messages_and_what_derives_from_them(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    alone = ref(MONDAY_NOON - 600)
    h.capture(alone, user=BOB, text="bob alone")
    h.rules.write("wiki", derived("w:bob", alone))

    job = h.rules.forget_user(TEAM, ALICE)

    assert (job.reason, job.user) == ("user_request", ALICE)
    assert h.scan(lambda r: r.user == ALICE) == []
    # Alice's message fed both derived entries citing refs[0]; Bob's own entry stays.
    assert h.scan(lambda r: refs[0] in r.cites()) == []
    assert {r.key for _, r in h.scan(lambda r: r.kind == "derived")} == {"w:bob"}
    assert {r.message for _, r in h.scan(lambda r: r.kind == "message")} == {refs[1], alone}
    (receipt,) = h.rules.receipts()
    assert receipt.user == ALICE


def test_sd3_follows_sd1_deadlines_and_stops_answers_while_open(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    h.hindsight.failing = True

    h.rules.forget_user(TEAM, ALICE)

    assert h.rules.read("hindsight", use="retrieval") == []
    with pytest.raises(Refused, match="SD-3: a purge of workspace T0001 user U0001"):
        h.capture(ref(MONDAY_NOON - 5), user=ALICE)
    # Bob is not covered by Alice's request.
    h.capture(ref(MONDAY_NOON - 5), user=BOB)
    h.clock.now = add_business_days(MONDAY_NOON, 14)
    h.rules.tick()
    assert [(a.rule, a.level) for a in h.alerts if a.rule == "SD-3"] == [("SD-3", "warning"), ("SD-3", "critical")]
    h.hindsight.failing = False
    h.rules.tick()
    assert h.scan(lambda r: refs[0] in r.cites() or r.user == ALICE) == []


# SD-4: no LLM training


@pytest.mark.parametrize("use", ["training", "fine_tuning", "evaluation_set", "analytics", "export", ""])
def test_sd4_slack_data_is_used_for_retrieval_only(tmp_path, use):
    h = Harness(tmp_path)
    h.populate()
    with pytest.raises(Refused, match="SD-4"):
        require_use(use)
    with pytest.raises(Refused, match="SD-4"):
        h.rules.read("evidence", use=use)
    with pytest.raises(Refused, match="SD-4"):
        h.rules.answerable(h.evidence.records(), use=use)
    assert len(h.rules.read("evidence", use="retrieval")) == 2


# SD-5: interface-only keeps nothing


def test_sd5_in_interface_only_mode_no_question_text_remains_after_answering(tmp_path):
    h = Harness(tmp_path, [interface_policy()])
    question = "what is the on-call rotation for payments?"

    with h.rules.hold_question("questions", TEAM, "evt-1", question, user=ALICE) as held:
        assert held.body == question
        assert h.questions.records() == [held]
    with pytest.raises(RuntimeError):
        with h.rules.hold_question("questions", TEAM, "evt-2", question + " again", user=ALICE):
            raise RuntimeError("the answer failed")

    assert h.scan(lambda r: r.body is not None and "on-call" in r.body) == []
    assert not h.questions.raw_contains(b"on-call")
    assert list(h.questions.root.iterdir()) == []


def test_sd5_interface_only_refuses_captured_messages_and_derived_entries(tmp_path):
    h = Harness(tmp_path, [interface_policy()])
    with pytest.raises(Refused, match="SD-5: workspace T0001 is interface-only"):
        h.capture(ref(MONDAY_NOON - 1))
    with pytest.raises(Refused, match="SD-5"):
        h.rules.write("hindsight", derived("h", ref(MONDAY_NOON - 1)))


def test_sd5_only_the_dedupe_hash_is_kept_and_only_for_25_hours(tmp_path):
    h = Harness(tmp_path, [interface_policy()])
    h.rules.write("dedupe", dedupe("d:evt-1"))
    # A crash between holding a question and deleting it leaves text behind; the tick removes it.
    h.questions.put(
        StoredRecord(key="question-lost", kind="question", workspace=TEAM, body="left by a crash", stored_at=MONDAY_NOON)
    )

    h.clock.advance(QUESTION_HOLD_LIMIT_SECONDS)
    assert h.rules.tick().expired == 0
    h.clock.advance(1)
    assert h.rules.tick().expired == 1
    assert h.questions.records() == []
    assert [r.key for r in h.dedupe.records()] == ["d:evt-1"]

    h.clock.now = MONDAY_NOON + DEDUPE_RETENTION_SECONDS + 1
    h.rules.tick()
    assert h.dedupe.records() == []


def test_sd5_questions_and_hashes_never_answer(tmp_path):
    h = Harness(tmp_path)
    h.rules.write("dedupe", dedupe("d"))
    with h.rules.hold_question("questions", TEAM, "evt", "a question") as held:
        assert h.rules.answerable([held, *h.dedupe.records()], use="retrieval") == []


# SD-6: capture is internal-only and decision-gated


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"app": "distributed"}, "needs an internal app"),
        ({"app": None}, "needs an internal app"),
        ({"terms_decision": None}, "recorded reading of Slack's terms"),
    ],
)
def test_sd6_capture_needs_an_internal_app_and_the_owners_recorded_decision(changes, message):
    with pytest.raises(ValidationError, match=message):
        source_policy(**changes)


def test_sd6_an_interface_only_workspace_needs_neither():
    policy = interface_policy()
    assert policy.app is None and policy.terms_decision is None and policy.retention_seconds is None


# SD-7: encrypted at rest and in transit


@pytest.mark.parametrize(
    "encryption, message",
    [
        (Encryption(at_rest=None, key_ref=None, in_transit="local"), "isn't encrypted at rest"),
        (Encryption(at_rest="AES-128-CBC", key_ref="env:K", in_transit="local"), "cipher 'AES-128-CBC' isn't one of"),
        (Encryption(at_rest="AES-256-GCM", key_ref=None, in_transit="local"), "names no key reference"),
        (Encryption(at_rest="AES-256-GCM", key_ref="env:K", in_transit="none"), "reached without TLS"),
    ],
)
def test_sd7_a_store_that_isnt_encrypted_is_refused(encryption, message):
    with pytest.raises(UnencryptedStore, match=message) as raised:
        DataRules([source_policy()], [FakeStore("dedupe", encryption)], alert=lambda alert: None, clock=FakeClock())
    assert raised.value.rule == "SD-7" and raised.value.store == "dedupe"


def test_sd7_a_store_whose_raw_bytes_show_plaintext_is_refused_whatever_it_declares(tmp_path):
    lying = FakeDirectoryStore("cursors", tmp_path / "cursors", ENCRYPTED)
    with pytest.raises(UnencryptedStore, match="readable in its raw bytes"):
        DataRules([source_policy()], [lying], alert=lambda alert: None, clock=FakeClock())
    # The probe's canary is gone either way.
    assert list((tmp_path / "cursors").iterdir()) == []

    sealed = FakeDirectoryStore("cursors", tmp_path / "sealed", ENCRYPTED, seal=opaque, unseal=unopaque)
    assert encryption_problems(sealed, MONDAY_NOON) == []
    assert list((tmp_path / "sealed").iterdir()) == []


@pytest.mark.parametrize("url", ["https://hindsight.internal:8443/v1", "wss://wss-primary.slack.com/link"])
def test_sd7_endpoints_use_tls(url):
    assert require_tls(url) == url


@pytest.mark.parametrize("url", ["http://hindsight.internal:8080", "ws://localhost:9000", "hindsight.internal"])
def test_sd7_an_endpoint_without_tls_is_refused(url):
    with pytest.raises(Refused, match="SD-7: endpoint .* doesn't use TLS"):
        require_tls(url)


def test_sd7_a_store_that_fails_its_daily_check_takes_no_writes_alerts_sd10_and_is_still_purged(tmp_path):
    h = Harness(tmp_path)
    h.populate()
    h.hindsight._encryption = Encryption(at_rest=None, key_ref=None, in_transit="local")

    h.rules.tick()

    assert "isn't encrypted at rest" in h.rules.disabled_stores()["hindsight"]
    assert [(a.rule, a.level) for a in h.alerts] == [("SD-10", "critical")]
    with pytest.raises(UnencryptedStore, match="SD-7: store hindsight"):
        h.rules.write("hindsight", derived("h:new", ref(MONDAY_NOON - 1)))
    h.rules.on_event(TEAM, {"type": "app_uninstalled"})
    assert h.scan(in_workspace(TEAM)) == []


def test_sd7_probe_records_are_written_only_by_the_check(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(Refused, match="SD-7: probe records"):
        h.rules.write("cursors", StoredRecord(key="p", kind="probe", workspace=TEAM, stored_at=MONDAY_NOON))


# SD-8: minimal scopes per role


def test_sd8_each_roles_listed_scopes_with_reasons_pass():
    assert scope_problems({"interface"}, INTERFACE_SCOPES) == []
    assert scope_problems({"interface", "source"}, SOURCE_SCOPES) == []
    assert all(reason.strip() for scopes in SCOPES_BY_ROLE.values() for reason in scopes.values())


@pytest.mark.parametrize(
    "roles, scopes, message",
    [
        (
            {"interface"},
            {**INTERFACE_SCOPES, "channels:history": "capture"},
            "channels:history isn't needed by the interface role",
        ),
        ({"interface", "source"}, {**SOURCE_SCOPES, "groups:history": "private channels"}, "groups:history isn't needed"),
        ({"interface", "source"}, {**SOURCE_SCOPES, "im:history": "DMs"}, "im:history isn't needed"),
        ({"interface"}, {**INTERFACE_SCOPES, "chat:write": "  "}, "chat:write has no reason"),
    ],
)
def test_sd8_an_extra_scope_or_one_without_a_reason_is_refused(roles, scopes, message):
    assert any(message in problem for problem in scope_problems(roles, scopes))
    with pytest.raises(ValidationError, match="SD-8"):
        if "source" in roles:
            source_policy(scopes=scopes)
        else:
            WorkspacePolicy(workspace=TEAM, scopes=scopes)


# SD-9: no bulk export


def test_sd9_capture_comes_only_from_allowlisted_channels(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(Refused, match="SD-9: channel C0999 isn't allowlisted"):
        h.capture(ref(MONDAY_NOON - 1, channel=UNLISTED))
    with pytest.raises(Refused, match="SD-9"):
        h.rules.write("wiki", derived("w", ref(MONDAY_NOON - 1), ref(MONDAY_NOON - 2, channel=UNLISTED)))


def test_sd9_capture_needs_an_allowlist_and_there_is_no_export_path():
    with pytest.raises(ValidationError, match="SD-9"):
        source_policy(source_channels=frozenset())
    with pytest.raises(Refused, match="SD-4"):
        require_use("export")
    public = {name.lower() for name in dir(DataRules) if not name.startswith("_")}
    assert not {name for name in public if any(word in name for word in ("export", "dump", "archive", "backup"))}


# SD-10: breach notice


def test_sd10_a_suspected_compromise_alerts_the_operator_at_once(tmp_path):
    h = Harness(tmp_path)
    alerts = h.rules.report_compromise("evidence volume mounted on an unknown host", workspaces=[OTHER_TEAM, TEAM])
    assert h.alerts == alerts
    assert [(a.rule, a.level, a.workspace, a.at) for a in alerts] == [
        ("SD-10", "critical", TEAM, MONDAY_NOON),
        ("SD-10", "critical", OTHER_TEAM, MONDAY_NOON),
    ]
    h.rules.report_compromise("token seen in a log")
    assert h.alerts[-1].workspace is None


# SD-11: retention matches the workspace


def test_sd11_the_daily_sweep_deletes_evidence_past_the_shorter_retention(tmp_path):
    clock = FakeClock()
    h = Harness(tmp_path, [source_policy(retention_days=30, workspace_retention_days=7)], clock=clock)
    fresh, aging = ref(MONDAY_NOON - DAY), ref(MONDAY_NOON - 6 * DAY)
    h.capture(fresh)
    h.capture(aging)
    h.rules.write("wiki", derived("w:aging", aging, fresh))
    h.rules.write("hindsight", derived("h:fresh", fresh))
    assert h.rules.tick().swept == 0

    clock.advance(DAY + 1)
    report = h.rules.tick()

    # The workspace keeps 7 days, shorter than the operator's 30, so that wins.
    assert report.swept == 2
    assert {r.key for _, r in h.scan(lambda r: True)} == {message(fresh).key, "h:fresh"}


def test_sd11_the_sweep_runs_once_a_day_and_answers_never_cite_expired_evidence(tmp_path):
    h = Harness(tmp_path, [source_policy(retention_days=2)])
    h.capture(ref(MONDAY_NOON - DAY - 10))
    assert h.rules.tick().swept == 0

    # Past retention before the next sweep: answers stop citing it at once.
    h.clock.now = MONDAY_NOON + DAY - 1
    assert h.rules.read("evidence", use="retrieval") == []
    # The sweep isn't due until a day after the last one.
    assert h.rules.tick().swept is None
    assert len(h.evidence.records()) == 1
    h.clock.now = MONDAY_NOON + DAY
    assert h.rules.tick().swept == 1


def test_sd11_expired_evidence_is_refused_on_write(tmp_path):
    h = Harness(tmp_path, [source_policy(retention_days=7)])
    with pytest.raises(Refused, match="SD-11: message .* is older than workspace T0001's retention"):
        h.capture(ref(MONDAY_NOON - 8 * DAY))


def test_sd11_a_failing_sweep_is_retried_and_alerts_once_a_day(tmp_path):
    h = Harness(tmp_path, [source_policy(retention_days=1)])
    h.capture(ref(MONDAY_NOON - HOUR))
    h.rules.tick()
    h.hindsight.put(derived("h:old", ref(MONDAY_NOON - HOUR)))
    h.hindsight.failing = True
    h.clock.advance(DAY)

    assert h.rules.tick().swept is None
    assert h.rules.tick().swept is None
    assert [(a.rule, a.level) for a in h.alerts] == [("SD-11", "warning")]
    h.hindsight.failing = False
    # The message went in the first failed sweep; only the entry derived from it was left.
    assert h.rules.tick().swept == 1
    assert h.scan(lambda r: True) == []


# Rule state survives a restart


def test_open_purges_and_tombstones_survive_a_restart_through_the_snapshot(tmp_path):
    h = Harness(tmp_path)
    refs = h.populate()
    h.populate(OTHER_TEAM)
    h.hindsight.failing = True
    h.rules.withdraw(refs[1])
    h.rules.on_event(OTHER_TEAM, {"type": "app_uninstalled"})

    saved = RulesState.model_validate_json(h.rules.snapshot().model_dump_json())
    restarted = DataRules(
        [source_policy(), source_policy(OTHER_TEAM)], h.stores, alert=h.alerts.append, clock=h.clock, state=saved
    )

    assert [job.workspace for job in restarted.open_purges()] == [OTHER_TEAM]
    with pytest.raises(Refused, match="SD-2"):
        restarted.write("evidence", message(refs[1]))
    h.hindsight.failing = False
    report = restarted.tick()
    assert [r.workspace for r in report.completed] == [OTHER_TEAM]
    assert report.settled == 1
    assert h.scan(in_workspace(OTHER_TEAM)) == []


def test_the_snapshot_is_a_copy(tmp_path):
    h = Harness(tmp_path)
    h.rules.withdraw(ref(MONDAY_NOON - 1))
    snapshot = h.rules.snapshot()
    snapshot.tombstones.clear()
    assert len(h.rules.snapshot().tombstones) == 1


# Records and policies refuse what the rules can't apply


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"kind": "message"}, "names its \\(team, channel, ts\\)"),
        ({"kind": "message", "channel": "C9", "message": ref(1.0)}, "workspace and channel are its record's"),
        ({"kind": "derived"}, "names the messages it was derived from"),
        ({"kind": "derived", "sources": frozenset({ref(1.0, workspace=OTHER_TEAM)})}, "its own workspace only"),
        ({"kind": "cursor", "message": ref(1.0)}, "a cursor record cites no message"),
        ({"kind": "dedupe", "unknown": 1}, "Extra inputs"),
    ],
)
def test_a_record_must_carry_what_the_rules_select_by(fields, message):
    with pytest.raises(ValidationError, match=message):
        StoredRecord.model_validate({"key": "k", "workspace": TEAM, "stored_at": 0.0, **fields})


def test_a_message_ref_is_a_slack_timestamp():
    assert ref(1700000000.5).sent_at == 1700000000.5
    with pytest.raises(ValidationError):
        MessageRef(workspace=TEAM, channel=CHANNEL, ts="yesterday")


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"source_channels": frozenset({CHANNEL})}, "source role is not enabled"),
        ({"retention_days": 30}, "captures nothing to retain"),
    ],
)
def test_an_interface_only_policy_holds_no_capture_settings(fields, message):
    with pytest.raises(ValidationError, match=message):
        WorkspacePolicy(workspace=TEAM, scopes=INTERFACE_SCOPES, **fields)


def test_capture_needs_a_retention():
    with pytest.raises(ValidationError, match="SD-11"):
        source_policy(retention_days=None)


def test_construction_refuses_duplicates_and_unknown_stores(tmp_path):
    with pytest.raises(DataRuleError, match="two policies"):
        DataRules([source_policy(), source_policy()], [], alert=lambda a: None)
    with pytest.raises(DataRuleError, match="two stores"):
        DataRules([], [FakeStore("s", ENCRYPTED), FakeStore("s", ENCRYPTED)], alert=lambda a: None)
    h = Harness(tmp_path)
    with pytest.raises(DataRuleError, match="no store is named"):
        h.rules.write("elsewhere", cursor())


# The trust boundary (AGENTS.md, "Knowledge Loop on Discord or Slack")

FORBIDDEN_PREFIXES = ("automation.supervisor", "automation.benchmark", "Tools", "subprocess", "pty", "importlib", "github")


def test_the_data_rules_import_nothing_across_the_trust_boundary():
    tree = ast.parse(Path(slack_data.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"__import__", "exec", "eval"}
    assert not [name for name in imported if name == "os" or name.startswith(FORBIDDEN_PREFIXES)]
