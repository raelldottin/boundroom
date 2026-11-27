"""Lane attempt records, reconcile and interrupts (SPEC-014 RA-LANE-004).

The record rules and the reconcile decision are pure, so they are tested by their output over
tables of facts. The records' files, Git's ancestry check and the queue writes are the module's own
tools, so those tests use a real lane root, a real repository and a real queue in temporary
directories. Interrupts use real process groups: a shell whose ``sleep`` grandchild must die with it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from automation.supervisor import lane_recovery as lr
from automation.supervisor import lane_workspace as lw
from automation.supervisor import policy
from automation.tests.test_lane_workspace import LaneRepoTestCase

SHA = "0123456789abcdef0123456789abcdef01234567"
HEAD2 = "89abcdef0123456789abcdef0123456789abcdef"
ROOT = Path("/state/boundroom/lanes/0123456789abcdef")
NOW = datetime(2026, 11, 27, 9, 30, 15, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)
EXAMPLE_QUEUE = Path(__file__).resolve().parents[1] / "examples" / "example-slices.json"


def attempt(slice_id: str = "S1", attempt_id: str = "a1", integration_ref: str = "refs/heads/main") -> lr.AttemptRecord:
    return lr.new_attempt(
        slice_id=slice_id,
        attempt_id=attempt_id,
        run_id="run-1",
        base_sha=SHA,
        lane_root=ROOT,
        integration_ref=integration_ref,
        now=NOW,
    )


def judged_done(slice_id: str = "S1", attempt_id: str = "a1") -> lr.AttemptRecord:
    return lr.judged(attempt(slice_id, attempt_id), HEAD2, "done", LATER)


def facts(record: lr.AttemptRecord, worktree_exists: bool = True, landed: Optional[bool] = None) -> lr.AttemptFacts:
    return lr.AttemptFacts(record=record, worktree_exists=worktree_exists, judged_head_landed=landed)


def only(loaded: list[lr.LoadedAttempt]) -> lr.AttemptRecord:
    (record,) = loaded
    assert isinstance(record, lr.AttemptRecord)
    return record


def closure_of(record: lr.AttemptRecord) -> lr.Closure:
    assert record.closure is not None
    return record.closure


def verdict_of(record: lr.AttemptRecord) -> lr.Judgement:
    assert record.judgement is not None
    return record.judgement


def queue(**statuses: str) -> dict[str, Any]:
    return {"slices": [{"slice_id": slice_id, "status": status} for slice_id, status in statuses.items()]}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class NewAttemptTest(unittest.TestCase):
    def test_records_what_the_lane_will_be_before_it_exists(self) -> None:
        self.assertEqual(
            attempt(),
            lr.AttemptRecord(
                slice_id="S1",
                attempt_id="a1",
                run_id="run-1",
                base_sha=SHA,
                worktree=ROOT / "a1",
                branch="boundroom/lane/S1/a1",
                integration_ref="refs/heads/main",
                started_at="2026-11-27T09:30:15Z",
                recorded_head=SHA,
            ),
        )
        self.assertTrue(attempt().is_open)

    def test_the_start_time_is_written_in_utc(self) -> None:
        eastern = datetime(2026, 11, 27, 4, 30, 15, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(lr.timestamp(eastern), "2026-11-27T09:30:15Z")

    def test_refuses_what_lane_workspace_would_refuse(self) -> None:
        for change in ({"base_sha": "main"}, {"slice_id": "../x"}, {"attempt_id": ".hidden"}):
            arguments: dict[str, Any] = {
                "slice_id": "S1",
                "attempt_id": "a1",
                "run_id": "r",
                "base_sha": SHA,
                "lane_root": ROOT,
                "integration_ref": "refs/heads/main",
                "now": NOW,
                **change,
            }
            with self.subTest(change=change), self.assertRaises(lr.AttemptRecordError):
                lr.new_attempt(**arguments)

    def test_refuses_a_missing_run_id_or_a_relative_root(self) -> None:
        for run_id, root in ((" ", ROOT), ("r", Path("relative/root"))):
            with self.subTest(run_id=run_id, root=root), self.assertRaises(lr.AttemptRecordError):
                lr.new_attempt(
                    slice_id="S1",
                    attempt_id="a1",
                    run_id=run_id,
                    base_sha=SHA,
                    lane_root=root,
                    integration_ref="refs/heads/main",
                    now=NOW,
                )


class IntegrationRefTest(unittest.TestCase):
    def test_accepts_full_branch_refs(self) -> None:
        for value in ("refs/heads/main", "refs/heads/feat/lanes-1.2"):
            with self.subTest(value=value):
                self.assertEqual(lr.check_integration_ref(value), value)

    def test_refuses_options_ranges_and_other_refs(self) -> None:
        for value in (
            "",
            "HEAD",  # resolved at reconcile, not at integration
            "main",
            "-x",
            "--all",
            "HEAD~1",
            "refs/tags/v1",
            "refs/heads/",
            "refs/heads/a..b",
            "refs/heads/x.lock",
            "refs/heads/x.",
            "refs/heads/x/",
            "refs/heads/-x",
            "refs/heads/a b",
            "refs/heads/a//b",
            "refs/heads/a/.b",
            "refs/heads/x.lock/y",
            "xHEAD",
            "HEADx",
        ):
            with self.subTest(value=value), self.assertRaises(lr.AttemptRecordError):
                lr.check_integration_ref(value)


class RecordChangesTest(unittest.TestCase):
    def test_a_new_head_is_recorded(self) -> None:
        self.assertEqual(lr.with_head(attempt(), HEAD2).recorded_head, HEAD2)

    def test_a_new_head_must_be_a_full_sha(self) -> None:
        with self.assertRaises(lr.AttemptRecordError):
            lr.with_head(attempt(), HEAD2[:12])

    def test_the_head_is_fixed_once_judged(self) -> None:
        with self.assertRaisesRegex(lr.AttemptRecordError, "its head is fixed"):
            lr.with_head(judged_done(), SHA)

    def test_a_verdict_records_the_judged_head_and_status(self) -> None:
        record = lr.judged(attempt(), HEAD2, "blocked", LATER)
        self.assertEqual(record.judgement, lr.Judgement(HEAD2, "blocked", "2026-11-27T09:35:15Z"))
        self.assertEqual(record.recorded_head, HEAD2)

    def test_a_verdict_is_one_of_the_verdict_statuses(self) -> None:
        for status in ("done", "failed", "blocked", "deferred"):
            with self.subTest(status=status):
                self.assertEqual(verdict_of(lr.judged(attempt(), HEAD2, status, LATER)).queue_status, status)
        for status in ("queued", "in_progress", "landed", ""):
            with self.subTest(status=status), self.assertRaises(lr.AttemptRecordError):
                lr.judged(attempt(), HEAD2, status, LATER)

    def test_a_verdict_needs_a_full_sha_and_comes_once(self) -> None:
        with self.assertRaises(lr.AttemptRecordError):
            lr.judged(attempt(), "HEAD", "done", LATER)
        with self.assertRaisesRegex(lr.AttemptRecordError, "already judged"):
            lr.judged(judged_done(), HEAD2, "done", LATER)

    def test_integration_starts_only_after_a_done_verdict(self) -> None:
        started = lr.integration_started(judged_done(), lr.IntegrationMethod.FAST_FORWARD, LATER)
        self.assertEqual(started.integration, lr.Integration(lr.IntegrationMethod.FAST_FORWARD, "2026-11-27T09:35:15Z"))
        for record in (attempt(), lr.judged(attempt(), HEAD2, "failed", LATER)):
            with self.subTest(record=record.judgement), self.assertRaisesRegex(lr.AttemptRecordError, "no done verdict"):
                lr.integration_started(record, lr.IntegrationMethod.MERGE, LATER)
        with self.assertRaisesRegex(lr.AttemptRecordError, "already started"):
            lr.integration_started(started, lr.IntegrationMethod.MERGE, LATER)

    def test_integration_finishes_once_after_it_starts(self) -> None:
        started = lr.integration_started(judged_done(), lr.IntegrationMethod.MERGE, NOW)
        finished = lr.integration_finished(started, lr.IntegrationOutcome.LANDED, LATER)
        self.assertEqual(
            finished.integration,
            lr.Integration(
                lr.IntegrationMethod.MERGE, "2026-11-27T09:30:15Z", lr.IntegrationOutcome.LANDED, "2026-11-27T09:35:15Z"
            ),
        )
        for record in (judged_done(), finished):
            with self.subTest(integration=record.integration), self.assertRaisesRegex(lr.AttemptRecordError, "no integration"):
                lr.integration_finished(record, lr.IntegrationOutcome.REFUSED, LATER)

    def test_a_closure_needs_a_queue_status_and_a_reason(self) -> None:
        for status in ("queued", "blocked", "deferred", "done", "failed", lr.NOT_IN_QUEUE):
            with self.subTest(status=status):
                self.assertEqual(closure_of(lr.closed(attempt(), status, "why", LATER)).queue_status, status)
        for status, reason in (("in_progress", "why"), ("", "why"), ("failed", " ")):
            with self.subTest(status=status, reason=reason), self.assertRaises(lr.AttemptRecordError):
                lr.closed(attempt(), status, reason, LATER)

    def test_a_closed_record_never_changes(self) -> None:
        record = lr.closed(attempt(), "failed", "why", LATER)
        self.assertEqual(record.closure, lr.Closure("failed", "why", "2026-11-27T09:35:15Z"))
        self.assertFalse(record.is_open)
        changes = (
            lambda: lr.with_head(record, HEAD2),
            lambda: lr.judged(record, HEAD2, "done", LATER),
            lambda: lr.closed(record, "done", "again", LATER),
            lambda: lr.integration_started(replace(record, judgement=judged_done().judgement), lr.IntegrationMethod.MERGE, LATER),
            lambda: lr.integration_finished(
                replace(record, integration=lr.Integration(lr.IntegrationMethod.MERGE, "t")), lr.IntegrationOutcome.LANDED, LATER
            ),
        )
        for index, change in enumerate(changes):
            with self.subTest(change=index), self.assertRaisesRegex(lr.AttemptRecordError, "is closed"):
                change()


def every_stage() -> list[lr.AttemptRecord]:
    started = lr.integration_started(judged_done(), lr.IntegrationMethod.FAST_FORWARD, LATER)
    landed = lr.integration_finished(started, lr.IntegrationOutcome.LANDED, LATER)
    return [attempt(), judged_done(), started, landed, lr.closed(landed, "done", "landed", LATER)]


class RecordJsonTest(unittest.TestCase):
    def test_every_stage_survives_a_round_trip(self) -> None:
        for record in every_stage():
            with self.subTest(record=record):
                self.assertEqual(lr.from_json(json.loads(json.dumps(lr.to_json(record)))), record)

    def test_writes_the_format_and_plain_values(self) -> None:
        data = lr.to_json(every_stage()[-1])
        self.assertEqual(data["format"], "boundroom.lane-attempt/1")
        self.assertEqual(data["worktree"], str(ROOT / "a1"))
        self.assertEqual(data["integration"]["method"], "fast_forward")
        self.assertEqual(data["integration"]["outcome"], "landed")
        self.assertEqual(data["closure"], {"queue_status": "done", "reason": "landed", "closed_at": "2026-11-27T09:35:15Z"})
        self.assertIsNone(lr.to_json(attempt())["judgement"])

    def test_refuses_what_this_module_would_never_write(self) -> None:
        good = lr.to_json(every_stage()[-1])

        def changed(**fields: Any) -> dict[str, Any]:
            return {**good, **fields}

        def nested(section: str, **fields: Any) -> dict[str, Any]:
            return {**good, section: {**good[section], **fields}}

        bad: dict[str, Any] = {
            "a list": [good],
            "no format": {key: value for key, value in good.items() if key != "format"},
            "another format": changed(format="boundroom.lane-attempt/2"),
            "missing slice": {key: value for key, value in good.items() if key != "slice_id"},
            "empty run id": changed(run_id=""),
            "number run id": changed(run_id=7),
            "unsafe attempt": changed(attempt_id="../a1"),
            "short base": changed(base_sha=SHA[:7]),
            "short recorded head": changed(recorded_head="abc"),
            "foreign branch": changed(branch="main"),
            "relative worktree": changed(worktree="a1"),
            "worktree of another attempt": changed(worktree=str(ROOT / "a2")),
            "unsafe integration ref": changed(integration_ref="--all"),
            "judgement not an object": changed(judgement="done"),
            "judged head short": nested("judgement", head="abc"),
            "judged in progress": nested("judgement", queue_status="in_progress"),
            "unknown method": nested("integration", method="rebase"),
            "unknown outcome": nested("integration", outcome="maybe"),
            "empty finish time": nested("integration", finished_at=""),
            "closure without reason": nested("closure", reason=""),
            "closure with an unknown status": nested("closure", queue_status="in_progress"),
            "blank run id": changed(run_id="  "),
            "recorded head that isn't the judged head": changed(recorded_head=SHA),
            "outcome without a finish time": nested("integration", finished_at=None),
            "finish time without an outcome": nested("integration", outcome=None),
            "integration without a verdict": changed(judgement=None),
            "integration of a failed verdict": nested("judgement", queue_status="failed"),
        }
        for name, data in bad.items():
            with self.subTest(name=name), self.assertRaises(lr.AttemptRecordError):
                lr.from_json(data)


# ---------------------------------------------------------------------------
# The reconcile decision
# ---------------------------------------------------------------------------


class ResolveSliceTest(unittest.TestCase):
    def resolve(self, *items: lr.AttemptFacts, in_doubt: bool = False) -> lr.SliceResolution:
        return lr.resolve_slice("S1", items, records_in_doubt=in_doubt)

    def assertFailed(self, resolution: lr.SliceResolution, reason: str) -> None:
        self.assertEqual((resolution.queue_status, resolution.reason_code), ("failed", "supervisor_error"))
        self.assertIn(reason, resolution.reason)
        self.assertIn("worktree, if any, is kept", resolution.reason)

    def test_a_judged_done_head_that_landed_is_done(self) -> None:
        resolution = self.resolve(facts(judged_done(), landed=True))
        self.assertEqual(
            resolution,
            lr.SliceResolution(
                slice_id="S1",
                queue_status="done",
                reason_code="",
                reason=(
                    f"The supervisor judged attempt a1 done at {HEAD2}, and refs/heads/main contains that head: it landed, "
                    "and only the status write was lost."
                ),
                attempt_ids=("a1",),
                worktrees=(ROOT / "a1",),
            ),
        )

    def test_a_landed_head_is_done_even_when_its_worktree_is_gone(self) -> None:
        resolution = self.resolve(facts(judged_done(), worktree_exists=False, landed=True))
        self.assertEqual((resolution.queue_status, resolution.worktrees), ("done", ()))

    def test_a_judged_done_head_that_did_not_land_fails(self) -> None:
        self.assertFailed(self.resolve(facts(judged_done(), landed=False)), "doesn't contain judged head")

    def test_a_judged_done_head_git_could_not_place_fails(self) -> None:
        self.assertFailed(self.resolve(facts(judged_done(), landed=None)), "Git couldn't tell")

    def test_a_head_the_supervisor_never_judged_fails_even_when_it_landed(self) -> None:
        for record in (attempt(), lr.with_head(attempt(), HEAD2)):
            with self.subTest(head=record.recorded_head):
                self.assertFailed(self.resolve(facts(record, landed=True)), "never judged attempt a1")

    def test_a_verdict_other_than_done_fails_even_when_it_landed(self) -> None:
        for status in ("failed", "blocked", "deferred"):
            with self.subTest(status=status):
                record = lr.judged(attempt(), HEAD2, status, LATER)
                self.assertFailed(self.resolve(facts(record, landed=True)), f"judged attempt a1 {status}")

    def test_no_open_record_fails(self) -> None:
        resolution = self.resolve()
        self.assertFailed(resolution, "No open attempt record")
        self.assertEqual((resolution.attempt_ids, resolution.worktrees), ((), ()))

    def test_an_unreadable_record_puts_every_slice_in_doubt(self) -> None:
        self.assertFailed(self.resolve(facts(judged_done(), landed=True), in_doubt=True), "couldn't be read")

    def test_several_open_records_for_one_slice_fail(self) -> None:
        resolution = self.resolve(
            facts(judged_done(attempt_id="a1"), landed=True), facts(judged_done(attempt_id="a2"), landed=True)
        )
        self.assertFailed(resolution, "Several open attempt records name this slice (a1, a2)")
        self.assertEqual(resolution.attempt_ids, ("a1", "a2"))

    def test_a_failed_slice_keeps_only_the_worktrees_that_exist(self) -> None:
        self.assertEqual(self.resolve(facts(attempt(), worktree_exists=True)).worktrees, (ROOT / "a1",))
        self.assertEqual(self.resolve(facts(attempt(), worktree_exists=False)).worktrees, ())

    def test_integration_records_decide_whether_a_landed_head_is_done(self) -> None:
        def integrated(method: lr.IntegrationMethod, outcome: Optional[lr.IntegrationOutcome]) -> lr.AttemptRecord:
            record = lr.integration_started(judged_done(), method, LATER)
            return record if outcome is None else lr.integration_finished(record, outcome, LATER)

        ff, merge = lr.IntegrationMethod.FAST_FORWARD, lr.IntegrationMethod.MERGE
        landed, refused = lr.IntegrationOutcome.LANDED, lr.IntegrationOutcome.REFUSED
        cases = [
            (ff, None, "done"),  # a fast-forward is one ref update: if the head is there, it finished
            (ff, landed, "done"),
            (merge, landed, "done"),
            (merge, None, "failed"),  # the merge may be on the branch before its validations passed
            (ff, refused, "failed"),
            (merge, refused, "failed"),
        ]
        for method, outcome, expected in cases:
            with self.subTest(method=method, outcome=outcome):
                resolution = self.resolve(facts(integrated(method, outcome), landed=True))
                self.assertEqual(resolution.queue_status, expected)
        self.assertIn("never recorded as finished", self.resolve(facts(integrated(merge, None), landed=True)).reason)
        self.assertIn("was refused", self.resolve(facts(integrated(ff, refused), landed=True)).reason)


class DecideReconcileTest(unittest.TestCase):
    def test_resolves_every_in_progress_slice_in_queue_order(self) -> None:
        plan = lr.decide_reconcile(
            queue(S0="done", S1="in_progress", S2="in_progress", S3="in_progress", S4="queued"),
            [
                facts(judged_done("S3", "a3"), landed=True),
                facts(judged_done("S1", "a1"), landed=False),
                facts(attempt("S2", "a2")),
            ],
            [],
        )
        self.assertEqual(
            [(item.slice_id, item.queue_status, item.attempt_ids) for item in plan.slices],
            [("S1", "failed", ("a1",)), ("S2", "failed", ("a2",)), ("S3", "done", ("a3",))],
        )
        self.assertEqual(
            [(item.attempt_id, item.queue_status) for item in plan.closures], [("a3", "done"), ("a1", "failed"), ("a2", "failed")]
        )
        self.assertEqual(plan.unreadable, ())

    def test_a_failed_closure_carries_the_reason_code(self) -> None:
        plan = lr.decide_reconcile(queue(S1="in_progress"), [facts(attempt())], [])
        self.assertTrue(plan.closures[0].reason.startswith("supervisor_error: The supervisor never judged attempt a1."))

    def test_a_done_closure_carries_the_reason_alone(self) -> None:
        plan = lr.decide_reconcile(queue(S1="in_progress"), [facts(judged_done(), landed=True)], [])
        self.assertTrue(plan.closures[0].reason.startswith("The supervisor judged attempt a1 done"))

    def test_an_in_progress_slice_without_a_record_fails(self) -> None:
        plan = lr.decide_reconcile(queue(S1="in_progress"), [], [])
        self.assertEqual([(item.slice_id, item.queue_status) for item in plan.slices], [("S1", "failed")])
        self.assertEqual(plan.closures, ())

    def test_slices_not_in_progress_never_change(self) -> None:
        for status in ("queued", "done", "failed", "blocked", "deferred"):
            with self.subTest(status=status):
                plan = lr.decide_reconcile(queue(S1=status), [facts(judged_done(), landed=True)], [])
                self.assertEqual(plan.slices, ())
                self.assertEqual(plan.closures, (lr.RecordClosure("a1", status, f"The queue already records {status}."),))

    def test_an_open_record_whose_slice_left_the_queue_is_closed(self) -> None:
        plan = lr.decide_reconcile(queue(S2="queued"), [facts(attempt())], [])
        self.assertEqual(plan.closures, (lr.RecordClosure("a1", lr.NOT_IN_QUEUE, "The queue no longer has this slice."),))

    def test_closed_records_are_neither_evidence_nor_closed_again(self) -> None:
        old = lr.closed(judged_done(), "done", "landed", LATER)
        plan = lr.decide_reconcile(queue(S1="in_progress"), [facts(old, landed=True)], [])
        self.assertEqual([(item.queue_status, item.attempt_ids) for item in plan.slices], [("failed", ())])
        self.assertEqual(plan.closures, ())

    def test_an_unreadable_record_fails_every_in_progress_slice_and_is_set_aside(self) -> None:
        unreadable = lr.UnreadableAttempt(ROOT / ".attempts" / "a9.json", "invalid JSON")
        plan = lr.decide_reconcile(queue(S1="in_progress", S2="queued"), [facts(judged_done(), landed=True)], [unreadable])
        self.assertEqual([(item.slice_id, item.queue_status) for item in plan.slices], [("S1", "failed")])
        self.assertEqual(plan.unreadable, (unreadable.path,))

    def test_the_report_lists_the_worktrees_that_still_exist(self) -> None:
        plan = lr.decide_reconcile(
            queue(S1="in_progress", S2="in_progress"),
            [facts(judged_done("S1", "a1"), landed=True), facts(attempt("S2", "a2"))],
            [],
        )
        report = lr.plan_report(plan)
        self.assertEqual(
            [(item["slice_id"], item["queue_status"], item["reason_code"], item["worktrees"]) for item in report["slices"]],
            [("S1", "done", "", [str(ROOT / "a1")]), ("S2", "failed", "supervisor_error", [str(ROOT / "a2")])],
        )
        self.assertEqual(report["closed_attempts"], ["a1", "a2"])
        self.assertEqual(report["unreadable_records"], [])
        self.assertEqual(report["slices"][0]["attempt_ids"], ["a1"])
        self.assertIn("landed", report["slices"][0]["reason"])


class RefusalTest(unittest.TestCase):
    def test_an_in_progress_slice_refuses_with_run_next_s_message_and_exit_code(self) -> None:
        refusal = lr.start_refusal(queue(S1="done", S2="in_progress"), [])
        self.assertEqual(
            refusal,
            lr.Refusal("stop: queue already has in_progress slice S2; run 'run_lanes --reconcile' to resolve it", 2),
        )

    def test_an_open_record_refuses(self) -> None:
        refusal = lr.start_refusal(queue(S1="queued"), [lr.closed(attempt("S0", "a0"), "done", "x", NOW), attempt()])
        self.assertEqual(
            refusal, lr.Refusal("stop: lane attempt a1 for slice S1 was never closed; run 'run_lanes --reconcile' to resolve it")
        )

    def test_an_unreadable_record_refuses(self) -> None:
        refusal = lr.start_refusal(queue(S1="queued"), [lr.UnreadableAttempt(Path("/r/.attempts/x.json"), "bad")])
        self.assertEqual(
            refusal,
            lr.Refusal(
                "stop: lane attempt record /r/.attempts/x.json can't be read (bad); run 'run_lanes --reconcile' to resolve it"
            ),
        )

    def test_nothing_unreconciled_starts(self) -> None:
        self.assertIsNone(lr.start_refusal(queue(S1="queued", S0="done"), [lr.closed(attempt(), "done", "x", NOW)]))

    def test_a_board_is_refused(self) -> None:
        self.assertIsNone(lr.board_refusal(None))
        self.assertIsNone(lr.board_refusal(""))
        refusal = lr.board_refusal("/boards/team")
        assert refusal is not None
        self.assertIn("JSON queue only", refusal.message)
        self.assertEqual(refusal.exit_code, 2)


# ---------------------------------------------------------------------------
# Records on disk
# ---------------------------------------------------------------------------


class RecordStoreTest(LaneRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.state / "lanes"

    def record(self, attempt_id: str = "a1", slice_id: str = "S1") -> lr.AttemptRecord:
        return lr.new_attempt(
            slice_id=slice_id,
            attempt_id=attempt_id,
            run_id="run-1",
            base_sha=self.base,
            lane_root=self.root,
            integration_ref="refs/heads/main",
            now=NOW,
        )

    def test_a_written_record_reads_back_and_its_directory_is_private(self) -> None:
        path = lr.write_record(self.root, self.record(), new=True)
        self.assertEqual(path, self.root / ".attempts" / "a1.json")
        self.assertEqual(lr.read_records(self.root), [self.record()])
        for directory in (self.root, path.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(path.parent), ["a1.json"])

    def test_a_new_attempt_refuses_an_id_that_already_has_a_record(self) -> None:
        lr.write_record(self.root, self.record(), new=True)
        with self.assertRaisesRegex(lr.AttemptRecordError, "already has a record"):
            lr.write_record(self.root, self.record(), new=True)

    def test_an_update_replaces_the_record(self) -> None:
        lr.write_record(self.root, self.record(), new=True)
        later = lr.judged(self.record(), self.base, "done", LATER)
        lr.write_record(self.root, later)
        self.assertEqual(lr.read_records(self.root), [later])

    def test_a_crash_mid_write_leaves_the_previous_record_and_a_temporary_file_reads_ignore(self) -> None:
        lr.write_record(self.root, self.record(), new=True)
        # What a crash between the temporary file's write and its rename leaves behind.
        (self.root / ".attempts" / ".a1.k3j9x.tmp").write_text('{"format": "boundroom.lane-at', encoding="utf-8")
        self.assertEqual(lr.read_records(self.root), [self.record()])
        later = lr.closed(self.record(), "done", "x", LATER)
        lr.write_record(self.root, later)
        self.assertEqual(lr.read_records(self.root), [later])
        self.assertIsNone(lr.check_start(queue(S1="done"), self.root))

    def test_a_failed_rename_leaves_no_temporary_file(self) -> None:
        (self.root / ".attempts" / "a1.json").mkdir(parents=True)
        (self.root / ".attempts" / "a1.json" / "x").write_text("", encoding="utf-8")
        with self.assertRaises(OSError):
            lr.write_record(self.root, self.record())
        self.assertEqual(os.listdir(self.root / ".attempts"), ["a1.json"])

    def test_no_attempts_directory_is_no_records(self) -> None:
        self.assertEqual(lr.read_records(self.root), [])

    def test_an_unreadable_or_misnamed_record_is_reported_not_skipped(self) -> None:
        directory = self.root / ".attempts"
        directory.mkdir(parents=True)
        (directory / "a1.json").write_text("{not json", encoding="utf-8")
        (directory / "a2.json").write_text(json.dumps(lr.to_json(self.record("a3"))), encoding="utf-8")
        (directory / "a4.json").write_bytes(b"\xff\xfe")
        (directory / "a5.json").write_text(json.dumps({"format": "other"}), encoding="utf-8")
        loaded = lr.read_records(self.root)
        self.assertEqual([type(item) for item in loaded], [lr.UnreadableAttempt] * 4)
        unreadable = [item for item in loaded if isinstance(item, lr.UnreadableAttempt)]
        self.assertEqual([item.path.name for item in unreadable], ["a1.json", "a2.json", "a4.json", "a5.json"])
        self.assertIn("holds attempt a3", unreadable[1].error)

    def test_record_path_refuses_an_unsafe_attempt_id(self) -> None:
        with self.assertRaises(lr.AttemptRecordError):
            lr.record_path(self.root, "../a1")

    def test_a_record_whose_worktree_is_outside_the_lane_root_is_unreadable(self) -> None:
        elsewhere = replace(self.record(), worktree=self.state / "elsewhere" / "a1")
        lr.write_record(self.root, elsewhere, new=True)
        (loaded,) = lr.read_records(self.root)
        assert isinstance(loaded, lr.UnreadableAttempt)
        self.assertIn("is not under", loaded.error)

    def test_the_queue_is_written_as_policy_writes_it(self) -> None:
        data = policy.load_queue(EXAMPLE_QUEUE)
        data["slices"][0]["title"] = "naïve — unicode stays as written"
        ours, theirs = self.state / "ours.json", self.state / "theirs.json"
        lr.write_queue(ours, data)
        policy.write_json(theirs, data)
        self.assertEqual(ours.read_bytes(), theirs.read_bytes())
        self.assertEqual(sorted(os.listdir(self.state)), ["ours.json", "theirs.json"])  # no temporary file left

    def test_set_aside_never_overwrites_an_earlier_one(self) -> None:
        directory = self.root / ".attempts"
        directory.mkdir(parents=True)
        targets = []
        for text in ("first", "second", "third"):
            (directory / "a1.json").write_text(text, encoding="utf-8")
            targets.append(lr.set_aside(directory / "a1.json", NOW))
        self.assertEqual([path.read_text(encoding="utf-8") for path in targets], ["first", "second", "third"])
        self.assertEqual(targets[2].name, "a1.json.unreadable-20261127T093015Z-3")

    def test_set_aside_keeps_the_file_out_of_later_reads(self) -> None:
        directory = self.root / ".attempts"
        directory.mkdir(parents=True)
        (directory / "a1.json").write_text("{not json", encoding="utf-8")
        target = lr.set_aside(directory / "a1.json", NOW)
        self.assertEqual(target, directory / "a1.json.unreadable-20261127T093015Z")
        self.assertEqual(target.read_text(encoding="utf-8"), "{not json")
        self.assertEqual(lr.read_records(self.root), [])


# ---------------------------------------------------------------------------
# Reconcile against a real repository and queue
# ---------------------------------------------------------------------------


class ReconcileTest(LaneRepoTestCase):
    SLICE = "today-continue-ui-regression-coverage"
    OTHER = "today-continue-copy-proofread"

    def setUp(self) -> None:
        super().setUp()
        self.root = lw.lane_root(self.repo, self.environ)
        self.queue_path = self.temp_dir() / "slices.json"
        shutil.copyfile(EXAMPLE_QUEUE, self.queue_path)

    def set_queue(self, **statuses: str) -> None:
        data = policy.load_queue(self.queue_path)
        for slice_id, status in statuses.items():
            data = policy.set_slice_status(data, slice_id, status)
        policy.write_json(self.queue_path, data)

    def status(self, slice_id: str) -> str:
        found = policy.find_slice(policy.load_queue(self.queue_path), slice_id)
        assert found is not None
        return found["status"]

    def start_lane(self, slice_id: str = SLICE, attempt_id: str = "a1") -> tuple[lr.AttemptRecord, lw.LaneWorkspace]:
        record = lr.new_attempt(
            slice_id=slice_id,
            attempt_id=attempt_id,
            run_id="run-1",
            base_sha=self.base,
            lane_root=self.root,
            integration_ref="refs/heads/main",
            now=NOW,
        )
        lr.write_record(self.root, record, new=True)
        self.set_queue(**{slice_id: "in_progress"})
        lane = lw.create(self.repo, slice_id, attempt_id, self.base, self.environ)
        self.assertEqual((lane.path, lane.branch), (record.worktree, record.branch))
        return record, lane

    def lane_commit(self, lane: lw.LaneWorkspace) -> str:
        self.write("src/app.py", "changed\n", lane.path)
        return self.commit("lane work", cwd=lane.path)

    def reconcile(self) -> lr.ReconcilePlan:
        return lr.reconcile(self.repo, self.queue_path, self.root, self.environ, now=LATER)

    def only_record(self) -> lr.AttemptRecord:
        return only(lr.read_records(self.root))

    def test_a_judged_done_head_already_on_the_branch_is_marked_done(self) -> None:
        record, lane = self.start_lane()
        head = self.lane_commit(lane)
        lr.write_record(self.root, lr.judged(record, head, "done", NOW))
        self.git("merge", "-q", "--ff-only", head)  # integration landed; then the run died

        plan = self.reconcile()

        self.assertEqual([(item.slice_id, item.queue_status) for item in plan.slices], [(self.SLICE, "done")])
        self.assertEqual(self.status(self.SLICE), "done")
        closure = self.only_record().closure
        assert closure is not None
        self.assertEqual((closure.queue_status, closure.closed_at), ("done", "2026-11-27T09:35:15Z"))
        self.assertTrue(lane.path.is_dir())
        self.assertIsNone(lr.check_start(policy.load_queue(self.queue_path), self.root))

    def test_a_judged_done_head_not_on_the_branch_fails_and_keeps_its_worktree(self) -> None:
        record, lane = self.start_lane()
        head = self.lane_commit(lane)
        lr.write_record(self.root, lr.judged(record, head, "done", NOW))

        plan = self.reconcile()

        self.assertEqual(self.status(self.SLICE), "failed")
        self.assertEqual(lr.plan_report(plan)["slices"][0]["worktrees"], [str(lane.path)])
        closure = self.only_record().closure
        assert closure is not None
        self.assertEqual(closure.queue_status, "failed")
        self.assertTrue(closure.reason.startswith("supervisor_error: refs/heads/main doesn't contain judged head"))
        self.assertTrue(lane.path.is_dir())
        self.assertIn(lane.branch, self.branches())

    def test_the_ancestry_is_checked_against_the_recorded_ref_not_the_checked_out_branch(self) -> None:
        record, lane = self.start_lane()
        head = self.lane_commit(lane)
        lr.write_record(self.root, lr.judged(record, head, "done", NOW))
        self.git("checkout", "-q", "-b", "elsewhere")
        self.git("merge", "-q", "--ff-only", head)

        self.reconcile()

        self.assertEqual(self.status(self.SLICE), "failed")

    def test_a_judged_head_git_does_not_know_fails(self) -> None:
        record, _ = self.start_lane()
        lr.write_record(self.root, lr.judged(record, HEAD2, "done", NOW))
        self.reconcile()
        self.assertEqual(self.status(self.SLICE), "failed")
        self.assertIn("Git couldn't tell", closure_of(self.only_record()).reason)

    def test_an_unjudged_head_on_the_branch_fails(self) -> None:
        record, lane = self.start_lane()
        head = self.lane_commit(lane)
        lr.write_record(self.root, lr.with_head(record, head))
        self.git("merge", "-q", "--ff-only", head)
        self.reconcile()
        self.assertEqual(self.status(self.SLICE), "failed")

    def test_every_open_record_is_resolved_in_one_pass(self) -> None:
        # The queue allows one in_progress slice today, so the second record is an attempt that died
        # before its claim: its slice is still queued, and only its record is closed.
        first, lane = self.start_lane(self.SLICE, "a1")
        head = self.lane_commit(lane)
        lr.write_record(self.root, lr.judged(first, head, "done", NOW))
        self.git("merge", "-q", "--ff-only", head)
        second = lr.new_attempt(
            slice_id=self.OTHER,
            attempt_id="a2",
            run_id="run-1",
            base_sha=self.base,
            lane_root=self.root,
            integration_ref="refs/heads/main",
            now=NOW,
        )
        lr.write_record(self.root, second, new=True)

        plan = self.reconcile()

        self.assertEqual([(item.slice_id, item.queue_status) for item in plan.slices], [(self.SLICE, "done")])
        self.assertEqual([(item.attempt_id, item.queue_status) for item in plan.closures], [("a1", "done"), ("a2", "queued")])
        self.assertEqual((self.status(self.SLICE), self.status(self.OTHER)), ("done", "queued"))
        self.assertTrue(all(not item.is_open for item in lr.read_records(self.root) if isinstance(item, lr.AttemptRecord)))

    def test_a_status_written_before_the_crash_only_closes_the_record(self) -> None:
        record, _ = self.start_lane()
        self.set_queue(**{self.SLICE: "failed"})
        before = self.queue_path.read_bytes()
        plan = self.reconcile()
        self.assertEqual(plan.slices, ())
        self.assertEqual(self.queue_path.read_bytes(), before)
        self.assertEqual(closure_of(self.only_record()).queue_status, "failed")
        self.assertEqual(lr.read_records(self.root), [lr.closed(record, "failed", "The queue already records failed.", LATER)])

    def test_an_unreadable_record_fails_the_slice_and_is_set_aside(self) -> None:
        record, lane = self.start_lane()
        head = self.lane_commit(lane)
        lr.write_record(self.root, lr.judged(record, head, "done", NOW))
        self.git("merge", "-q", "--ff-only", head)
        (self.root / ".attempts" / "a0.json").write_text("{torn", encoding="utf-8")

        plan = self.reconcile()

        self.assertEqual(self.status(self.SLICE), "failed")
        self.assertEqual(plan.unreadable, (self.root / ".attempts" / "a0.json",))
        self.assertTrue((self.root / ".attempts" / "a0.json.unreadable-20261127T093515Z").exists())
        self.assertIsNone(lr.check_start(policy.load_queue(self.queue_path), self.root))

    def test_a_second_reconcile_changes_nothing(self) -> None:
        self.start_lane()
        self.reconcile()
        queue_bytes = self.queue_path.read_bytes()
        records = lr.read_records(self.root)
        plan = self.reconcile()
        self.assertEqual(plan, lr.ReconcilePlan((), (), ()))
        self.assertEqual(self.queue_path.read_bytes(), queue_bytes)
        self.assertEqual(lr.read_records(self.root), records)

    def test_check_start_refuses_until_reconciled(self) -> None:
        self.start_lane()
        refusal = lr.check_start(policy.load_queue(self.queue_path), self.root)
        assert refusal is not None
        self.assertIn(f"in_progress slice {self.SLICE}", refusal.message)
        self.set_queue(**{self.SLICE: "queued"})
        refusal = lr.check_start(policy.load_queue(self.queue_path), self.root)
        assert refusal is not None
        self.assertIn("lane attempt a1", refusal.message)
        self.reconcile()
        self.assertIsNone(lr.check_start(policy.load_queue(self.queue_path), self.root))


# ---------------------------------------------------------------------------
# Interrupts
# ---------------------------------------------------------------------------


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_soon(pid: int, seconds: float = 3.0) -> bool:
    """A grandchild is reparented when its group dies, and reaped a moment later."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.01)
    return False


class GroupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def runner(
        self, name: str, ignore_term: bool = False, grandchild_ignores_term: bool = False
    ) -> tuple[subprocess.Popen[bytes], int]:
        """A shell leading its own group with a ``sleep`` grandchild. Returns the shell and the grandchild's pid."""
        pid_file = self.dir / f"{name}.pid"
        trap = "trap '' TERM; " if ignore_term else ""
        # A subshell that ignores TERM and then becomes the sleep, so its pid is the grandchild's.
        sleeper = "(trap '' TERM; exec sleep 30)" if grandchild_ignores_term else "sleep 30"
        process = subprocess.Popen(
            ["/bin/sh", "-c", f"{trap}{sleeper} & echo $! > {pid_file}.tmp; mv {pid_file}.tmp {pid_file}; wait"],
            start_new_session=True,
        )
        self.addCleanup(lambda: lw.signal_group(process.pid, signal.SIGKILL) or process.wait())
        deadline = time.monotonic() + 5
        while not pid_file.exists():
            self.assertLess(time.monotonic(), deadline, "the runner never started its grandchild")
            time.sleep(0.005)
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        self.addCleanup(lambda: alive(grandchild) and os.kill(grandchild, signal.SIGKILL))
        return process, grandchild

    def lane(self, slice_id: str, attempt_id: str, ignore_term: bool = False) -> tuple[lr.RunningLane, int]:
        process, grandchild = self.runner(attempt_id, ignore_term)
        return lr.RunningLane(attempt(slice_id, attempt_id), process), grandchild


class StopLanesTest(GroupTestCase):
    def test_sigterm_stops_every_group_without_waiting_out_the_grace(self) -> None:
        lanes = [self.lane("S1", "a1"), self.lane("S2", "a2")]
        started = time.monotonic()
        lr.stop_lanes([lane for lane, _ in lanes], grace_seconds=20)
        self.assertLess(time.monotonic() - started, 5)
        for lane, grandchild in lanes:
            self.assertEqual(lane.process.returncode, -signal.SIGTERM)
            self.assertTrue(gone_soon(grandchild))

    def test_a_group_that_ignores_sigterm_is_killed_after_the_grace(self) -> None:
        (lane, grandchild) = self.lane("S1", "a1", ignore_term=True)
        started = time.monotonic()
        lr.stop_lanes([lane], grace_seconds=0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(lane.process.returncode, -signal.SIGKILL)
        self.assertTrue(gone_soon(grandchild))

    def test_a_grandchild_that_outlives_its_leader_is_killed_after_the_grace(self) -> None:
        process, grandchild = self.runner("a1", grandchild_ignores_term=True)
        started = time.monotonic()
        lr.stop_lanes([lr.RunningLane(attempt(), process)], grace_seconds=0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(process.returncode, -signal.SIGTERM)  # the leader obeyed; its grandchild didn't
        self.assertTrue(gone_soon(grandchild))

    def test_without_a_grace_the_group_is_killed_at_once(self) -> None:
        (lane, grandchild) = self.lane("S1", "a1")
        lr.stop_lanes([lane], grace_seconds=0)
        self.assertEqual(lane.process.returncode, -signal.SIGKILL)
        self.assertTrue(gone_soon(grandchild))

    def test_the_lanes_share_one_grace_period(self) -> None:
        lanes = [self.lane("S1", "a1", ignore_term=True), self.lane("S2", "a2", ignore_term=True)]
        started = time.monotonic()
        lr.stop_lanes([lane for lane, _ in lanes], grace_seconds=1.5)
        self.assertLess(time.monotonic() - started, 2.7)  # one after the other would take 3
        for lane, grandchild in lanes:
            self.assertEqual(lane.process.returncode, -signal.SIGKILL)
            self.assertTrue(gone_soon(grandchild))

    def test_a_lane_whose_runner_already_ended_is_no_error(self) -> None:
        (lane, _) = self.lane("S1", "a1")
        lw.stop_process_group(lane.process)
        lr.stop_lanes([lane], grace_seconds=0.1)
        self.assertEqual(lane.process.returncode, -signal.SIGKILL)


class InterruptHandlingTest(GroupTestCase):
    def test_a_keyboard_interrupt_stops_the_lanes_records_them_and_propagates(self) -> None:
        (lane, grandchild) = self.lane("S1", "a1")
        recorded: list[list[lr.RunningLane]] = []
        with self.assertRaises(KeyboardInterrupt):
            with lr.interrupt_handling(lambda: [lane], lambda lanes: recorded.append(list(lanes)), grace_seconds=5):
                raise KeyboardInterrupt
        self.assertEqual(recorded, [[lane]])
        self.assertIsNotNone(lane.process.returncode)
        self.assertTrue(gone_soon(grandchild))

    def test_sigterm_becomes_an_interrupt_and_the_old_handler_comes_back(self) -> None:
        def previous_handler(signum: int, frame: object) -> None:
            raise AssertionError("the previous handler ran inside the guard")

        old = signal.signal(signal.SIGTERM, previous_handler)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        (lane, grandchild) = self.lane("S1", "a1")
        recorded: list[str] = []
        with self.assertRaises(lr.Terminated):
            with lr.interrupt_handling(lambda: [lane], lambda lanes: recorded.append(lanes[0].record.slice_id), grace_seconds=5):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)  # the signal interrupts this at once
        self.assertEqual(recorded, ["S1"])
        self.assertTrue(gone_soon(grandchild))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)
        self.assertTrue(issubclass(lr.Terminated, KeyboardInterrupt))

    def test_a_failure_to_record_still_propagates_the_interrupt_with_a_note(self) -> None:
        (lane, _) = self.lane("S1", "a1")

        def broken(lanes: object) -> None:
            raise OSError("queue is read-only")

        with self.assertRaises(KeyboardInterrupt) as caught:
            with lr.interrupt_handling(lambda: [lane], broken, grace_seconds=1):
                raise KeyboardInterrupt
        self.assertIn("queue is read-only", " ".join(getattr(caught.exception, "__notes__", [])))
        self.assertIsNotNone(lane.process.returncode)

    def test_without_an_interrupt_nothing_is_stopped_and_the_handler_is_restored(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        calls: list[str] = []
        with lr.interrupt_handling(lambda: calls.append("running") or [], lambda lanes: calls.append("stopped")):
            self.assertIs(signal.getsignal(signal.SIGTERM), lr._raise_terminated)
        self.assertEqual(calls, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_sighup_is_a_stop_signal_too(self) -> None:
        (lane, grandchild) = self.lane("S1", "a1")
        with self.assertRaises(lr.Terminated) as caught:
            with lr.interrupt_handling(lambda: [lane], lambda lanes: None, grace_seconds=5):
                os.kill(os.getpid(), signal.SIGHUP)
                time.sleep(5)  # the signal interrupts this at once
        self.assertEqual(caught.exception.signum, signal.SIGHUP)
        self.assertTrue(gone_soon(grandchild))

    def test_a_second_ctrl_c_is_ignored_while_the_lanes_stop(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        seen: list[object] = []

        def record(lanes: object) -> None:
            seen.extend(signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
            os.kill(os.getpid(), signal.SIGINT)  # ignored: recording finishes

        with self.assertRaises(KeyboardInterrupt):
            with lr.interrupt_handling(lambda: [], record):
                raise KeyboardInterrupt
        self.assertEqual(seen, [signal.SIG_IGN] * 3)
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_another_error_is_not_an_interrupt(self) -> None:
        calls: list[str] = []
        with self.assertRaises(ValueError):
            with lr.interrupt_handling(lambda: calls.append("running") or [], lambda lanes: calls.append("stopped")):
                raise ValueError("boom")
        self.assertEqual(calls, [])

    def test_outside_the_main_thread_signals_are_left_alone(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        seen: list[object] = []

        def body() -> None:
            try:
                with lr.interrupt_handling(lambda: [], lambda lanes: seen.append("stopped")):
                    seen.append(signal.getsignal(signal.SIGTERM))
                    raise KeyboardInterrupt
            except KeyboardInterrupt:
                seen.append("propagated")

        thread = threading.Thread(target=body)
        thread.start()
        thread.join()
        self.assertEqual(seen, [before, "stopped", "propagated"])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class RecordInterruptedTest(LaneRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.state / "lanes"
        self.queue_path = self.temp_dir() / "slices.json"
        shutil.copyfile(EXAMPLE_QUEUE, self.queue_path)
        data = policy.set_slice_status(policy.load_queue(self.queue_path), ReconcileTest.SLICE, "in_progress")
        policy.write_json(self.queue_path, data)

    def running(self, slice_id: str, attempt_id: str) -> lr.RunningLane:
        record = lr.new_attempt(
            slice_id=slice_id,
            attempt_id=attempt_id,
            run_id="run-1",
            base_sha=self.base,
            lane_root=self.root,
            integration_ref="refs/heads/main",
            now=NOW,
        )
        lr.write_record(self.root, record, new=True)
        process = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        process.wait()
        return lr.RunningLane(record, process)

    def test_in_progress_slices_fail_and_their_records_close(self) -> None:
        lane = self.running(ReconcileTest.SLICE, "a1")
        lr.record_interrupted(self.queue_path, self.root, [lane], LATER)
        found = policy.find_slice(policy.load_queue(self.queue_path), ReconcileTest.SLICE)
        assert found is not None
        self.assertEqual(found["status"], "failed")
        self.assertEqual(
            lr.read_records(self.root),
            [lr.closed(lane.record, "failed", "supervisor_error: The run was interrupted and its runner was stopped.", LATER)],
        )
        self.assertIsNone(lr.check_start(policy.load_queue(self.queue_path), self.root))

    def test_a_slice_not_in_progress_keeps_its_status(self) -> None:
        lane = self.running(ReconcileTest.OTHER, "a2")  # queued in the example queue
        before = self.queue_path.read_bytes()
        lr.record_interrupted(self.queue_path, self.root, [lane], LATER)
        self.assertEqual(self.queue_path.read_bytes(), before)
        self.assertEqual(
            closure_of(only(lr.read_records(self.root))),
            lr.Closure(
                "queued", "The run was interrupted and its runner was stopped; the queue records queued.", "2026-11-27T09:35:15Z"
            ),
        )

    def test_the_record_on_disk_is_closed_not_the_older_one_in_memory(self) -> None:
        lane = self.running(ReconcileTest.SLICE, "a1")
        newer = lr.with_head(lane.record, HEAD2)
        lr.write_record(self.root, newer)
        lr.record_interrupted(self.queue_path, self.root, [lane], LATER)
        record = only(lr.read_records(self.root))
        self.assertEqual(record.recorded_head, HEAD2)
        self.assertFalse(record.is_open)

    def test_a_closed_record_is_left_alone(self) -> None:
        lane = self.running(ReconcileTest.SLICE, "a1")
        done = lr.closed(lane.record, "done", "landed", NOW)
        lr.write_record(self.root, done)
        before = self.queue_path.read_bytes()
        lr.record_interrupted(self.queue_path, self.root, [lane], LATER)
        self.assertEqual(lr.read_records(self.root), [done])
        self.assertEqual(self.queue_path.read_bytes(), before)

    def test_a_slice_gone_from_the_queue_closes_as_not_in_queue(self) -> None:
        lane = self.running("removed-slice", "a3")
        lr.record_interrupted(self.queue_path, self.root, [lane], LATER)
        self.assertEqual(closure_of(only(lr.read_records(self.root))).queue_status, lr.NOT_IN_QUEUE)


if __name__ == "__main__":
    unittest.main()
