"""Which slice runs next, and when the supervisor stops for a person.

These are the queue rules: slice selection, dependency checks, the stop rules of
``next_slice_eligibility`` and the queue integrity checks. Every decision is compared whole.
"""

from __future__ import annotations

import copy
import dataclasses
import unittest
from typing import Any

from automation.supervisor import policy


def slice_record(slice_id: str, status: str = "queued", priority: int = 1, depends_on: tuple[str, ...] = (), **extra: Any):
    record = {"slice_id": slice_id, "status": status, "priority": priority, "depends_on": list(depends_on)}
    record.update(extra)
    return record


def queue(*slices: dict[str, Any]) -> dict[str, Any]:
    return {"policy": {"supervisor_owned_paths": ["automation/queue/slices.json"]}, "slices": list(slices)}


def stop_for_review(stop_reason: str, recommended: str) -> policy.CompletionDecision:
    return policy.CompletionDecision(
        queue_status="done",
        decision="stop_for_review",
        should_continue=False,
        stop_reason=stop_reason,
        next_slice_id=None,
        recommended_next_slice=recommended,
        required_validation_failures=[],
        dirty_paths_outside_scope=[],
        files_touched_outside_scope=[],
        changed_file_count=0,
    )


def continue_into(next_slice_id: str, recommended: str) -> policy.CompletionDecision:
    reason = (
        "Continuation allowed into the recommended eligible next slice."
        if recommended
        else "Continuation allowed into the highest-priority eligible queued slice."
    )
    return dataclasses.replace(
        stop_for_review(reason, recommended), decision="continue", should_continue=True, next_slice_id=next_slice_id
    )


NO_NEXT_SLICE = dataclasses.replace(
    stop_for_review("No eligible queued next slice remains after the current slice completed.", ""), decision="stop"
)


class SelectNextSliceTest(unittest.TestCase):
    def test_the_lowest_priority_number_among_eligible_queued_slices_wins(self) -> None:
        cases = (
            ("lower priority number wins", [slice_record("a", priority=2), slice_record("b", priority=1)], "b"),
            ("a tie goes to the earlier slice", [slice_record("a", priority=1), slice_record("b", priority=1)], "a"),
            (
                "a tie goes to the earlier slice, reversed ids",
                [slice_record("z", priority=1), slice_record("a", priority=1)],
                "z",
            ),
            (
                "slices that are not queued are skipped",
                [
                    slice_record(f"s-{status}", status=status, priority=0)
                    for status in ("in_progress", "done", "failed", "blocked", "deferred")
                ]
                + [slice_record("q", priority=9)],
                "q",
            ),
            (
                "unmet dependencies block a slice",
                [
                    slice_record("dep", status="in_progress"),
                    slice_record("a", priority=0, depends_on=("dep",)),
                    slice_record("b", priority=5),
                ],
                "b",
            ),
            (
                "met dependencies do not",
                [
                    slice_record("dep", status="done"),
                    slice_record("a", priority=0, depends_on=("dep",)),
                    slice_record("b", priority=5),
                ],
                "a",
            ),
        )
        for name, slices, expected in cases:
            with self.subTest(name):
                selected = policy.select_next_slice(queue(*slices))
                self.assertEqual(expected, selected["slice_id"] if selected else None)

    def test_none_when_nothing_is_eligible(self) -> None:
        cases = (
            ("empty queue", []),
            ("only done slices", [slice_record("a", status="done")]),
            ("only blocked dependencies", [slice_record("dep", status="failed"), slice_record("a", depends_on=("dep",))]),
        )
        for name, slices in cases:
            with self.subTest(name):
                self.assertIsNone(policy.select_next_slice(queue(*slices)))


class DependenciesSatisfiedTest(unittest.TestCase):
    def test_every_dependency_must_be_done(self) -> None:
        cases = (
            ("no dependencies", (), True),
            ("done dependency", ("done-slice",), True),
            ("unknown dependency", ("missing",), False),
            ("one done, one queued", ("done-slice", "queued-slice"), False),
        )
        statuses = ("failed", "blocked", "in_progress", "queued", "deferred")
        dependencies = [slice_record("done-slice", status="done"), slice_record("queued-slice")]
        dependencies += [slice_record(f"{status}-slice", status=status) for status in statuses]
        cases += tuple((f"{status} dependency", (f"{status}-slice",), False) for status in statuses)
        for name, depends_on, expected in cases:
            with self.subTest(name):
                record = slice_record("a", depends_on=depends_on)
                self.assertEqual(expected, policy.dependencies_satisfied(record, queue(*dependencies, record)))


class NextSliceEligibilityTest(unittest.TestCase):
    def decide(self, slices: list[dict[str, Any]], recommended: str, completed: int = 0, limit: int = 3):
        return policy.next_slice_eligibility(queue(*slices), "cur", recommended, completed, limit)

    def test_the_run_limit_stops_before_anything_else(self) -> None:
        reason = "Consecutive autonomous run limit reached; stop for human review."
        slices = [slice_record("cur", status="in_progress"), slice_record("next")]
        cases = (
            ("limit reached", 3, 3, "next", stop_for_review(reason, "next")),
            ("past the limit", 4, 3, "", stop_for_review(reason, "")),
            ("a zero limit stops at once", 0, 0, "", stop_for_review(reason, "")),
            ("one run below the limit continues", 2, 3, "next", continue_into("next", "next")),
            ("an invalid recommendation still reports the limit", 3, 3, "cur", stop_for_review(reason, "cur")),
            ("a whitespace recommendation is reported stripped", 3, 3, "  ", stop_for_review(reason, "")),
        )
        for name, completed, limit, recommended, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, self.decide(slices, recommended, completed, limit))

    def test_a_recommendation_must_be_the_highest_priority_eligible_queued_slice(self) -> None:
        current = slice_record("cur", status="in_progress")
        cases = (
            (
                "recommending the current slice",
                [current, slice_record("next")],
                "cur",
                stop_for_review("The handoff recommended the current slice again instead of a queued successor.", "cur"),
            ),
            (
                "recommending an unknown slice",
                [current, slice_record("next")],
                "nope",
                stop_for_review(
                    "The handoff recommended a next slice that is not present in automation/queue/slices.json.", "nope"
                ),
            ),
            (
                "ids are case-sensitive",
                [current, slice_record("next")],
                "Next",
                stop_for_review(
                    "The handoff recommended a next slice that is not present in automation/queue/slices.json.", "Next"
                ),
            ),
            (
                "recommending a queued slice whose dependency is another unfinished slice",
                [current, slice_record("other"), slice_record("next", priority=0, depends_on=("other",))],
                "next",
                stop_for_review("The handoff recommended slice 'next', but its dependencies are not yet satisfied.", "next"),
            ),
            (
                "recommending a slice that depends on the current one",
                [current, slice_record("next", depends_on=("cur",))],
                "next",
                continue_into("next", "next"),
            ),
            (
                "recommending a lower-priority slice",
                [current, slice_record("first", priority=1), slice_record("next", priority=2)],
                "next",
                stop_for_review(
                    "The handoff recommended slice 'next', but the highest-priority eligible queued slice is 'first'.", "next"
                ),
            ),
            (
                "recommending the later of two equal-priority slices",
                [current, slice_record("first"), slice_record("next")],
                "next",
                stop_for_review(
                    "The handoff recommended slice 'next', but the highest-priority eligible queued slice is 'first'.", "next"
                ),
            ),
            (
                "a padded recommendation is stripped",
                [current, slice_record("next")],
                "  next \n",
                continue_into("next", "next"),
            ),
        )
        for name, slices, recommended, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, self.decide(slices, recommended))

    def test_a_recommended_slice_that_is_not_queued_stops(self) -> None:
        for status in ("in_progress", "done", "failed", "blocked", "deferred"):
            with self.subTest(status=status):
                slices = [slice_record("cur", status="in_progress"), slice_record("next", status=status)]
                expected = stop_for_review(
                    f"The handoff recommended slice 'next', but its queue status is {status!r} instead of 'queued'.", "next"
                )
                self.assertEqual(expected, self.decide(slices, "next"))

    def test_without_a_recommendation_the_highest_priority_slice_follows(self) -> None:
        current = slice_record("cur", status="in_progress")
        cases = (
            (
                "a next slice exists",
                [current, slice_record("b", priority=2), slice_record("a", priority=1)],
                "",
                continue_into("a", ""),
            ),
            ("a whitespace recommendation counts as none", [current, slice_record("a")], "   ", continue_into("a", "")),
            ("nothing left", [current, slice_record("done", status="done")], "", NO_NEXT_SLICE),
            ("nothing eligible", [current, slice_record("a", depends_on=("gone",))], "", NO_NEXT_SLICE),
        )
        for name, slices, recommended, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, self.decide(slices, recommended))

    def test_an_unknown_current_slice_raises(self) -> None:
        with self.assertRaises(KeyError) as raised:
            policy.next_slice_eligibility(queue(slice_record("a")), "nope", "", 0, 3)
        self.assertEqual("'Unknown slice_id: nope'", str(raised.exception))

    def test_the_queue_passed_in_is_not_changed(self) -> None:
        queue_data = queue(slice_record("cur", status="in_progress"), slice_record("next", depends_on=("cur",)))
        original = copy.deepcopy(queue_data)
        policy.next_slice_eligibility(queue_data, "cur", "next", 0, 3)
        self.assertEqual(original, queue_data)


class ValidateQueueIntegrityTest(unittest.TestCase):
    def test_errors_are_reported_in_order(self) -> None:
        cases = (
            ("a valid queue", [slice_record("a"), slice_record("b", depends_on=("a",))], []),
            ("duplicate ids", [slice_record("a"), slice_record("a")], ["slice_id values must be unique"]),
            ("one in_progress slice is fine", [slice_record("a", status="in_progress"), slice_record("b")], []),
            (
                "two in_progress slices",
                [slice_record("a", status="in_progress"), slice_record("b", status="in_progress")],
                ["at most one slice may be in_progress"],
            ),
            (
                "unknown dependencies, in declared order",
                [slice_record("a", depends_on=("y", "x"))],
                ["slice 'a' depends on unknown slice IDs: ['y', 'x']"],
            ),
            (
                "a blocked slice needs an entry condition",
                [slice_record("a", status="blocked", entry_condition="  ")],
                ["slice 'a' is 'blocked' but is missing an explicit entry_condition"],
            ),
            (
                "a deferred slice without the key",
                [slice_record("a", status="deferred")],
                ["slice 'a' is 'deferred' but is missing an explicit entry_condition"],
            ),
            ("other statuses need none", [slice_record("a", status="failed")], []),
            (
                "an unknown unblocker, stripped",
                [slice_record("a", status="blocked", entry_condition="when b", recommended_unblocker=" zzz ")],
                ["slice 'a' recommends unknown unblocker 'zzz'"],
            ),
            (
                "a whitespace unblocker is ignored",
                [slice_record("a", status="blocked", entry_condition="when b", recommended_unblocker="  ")],
                [],
            ),
            (
                "a known unblocker is fine",
                [slice_record("a", status="blocked", entry_condition="when b", recommended_unblocker="b"), slice_record("b")],
                [],
            ),
            (
                "an unblocker on a queued slice is not checked",
                [slice_record("a", recommended_unblocker="zzz")],
                [],
            ),
            (
                "everything at once",
                [
                    slice_record("a", status="in_progress"),
                    slice_record("a", status="in_progress", depends_on=("x",)),
                    slice_record("b", status="blocked", recommended_unblocker="zzz"),
                ],
                [
                    "slice_id values must be unique",
                    "at most one slice may be in_progress",
                    "slice 'a' depends on unknown slice IDs: ['x']",
                    "slice 'b' is 'blocked' but is missing an explicit entry_condition",
                    "slice 'b' recommends unknown unblocker 'zzz'",
                ],
            ),
        )
        for name, slices, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.validate_queue_integrity(queue(*slices)))


class QueueLookupTest(unittest.TestCase):
    def test_set_slice_status_returns_a_changed_copy(self) -> None:
        queue_data = queue(slice_record("a"), slice_record("b"))
        original = copy.deepcopy(queue_data)
        updated = policy.set_slice_status(queue_data, "b", "done")
        self.assertEqual(queue(slice_record("a"), slice_record("b", status="done")), updated)
        self.assertEqual(original, queue_data)

    def test_set_slice_status_changes_only_the_first_duplicate(self) -> None:
        updated = policy.set_slice_status(queue(slice_record("a"), slice_record("a")), "a", "anything")
        self.assertEqual(queue(slice_record("a", status="anything"), slice_record("a")), updated)

    def test_set_slice_status_refuses_an_unknown_id(self) -> None:
        with self.assertRaises(KeyError) as raised:
            policy.set_slice_status(queue(slice_record("a")), "nope", "done")
        self.assertEqual("'Unknown slice_id: nope'", str(raised.exception))

    def test_find_slice_and_active_slice_return_the_first_match(self) -> None:
        first = slice_record("a", status="in_progress", priority=1)
        second = slice_record("a", status="in_progress", priority=2)
        queue_data = queue(slice_record("q"), first, second)
        self.assertIs(first, policy.find_slice(queue_data, "a"))
        self.assertIs(first, policy.active_slice(queue_data))
        self.assertIsNone(policy.find_slice(queue_data, "nope"))
        self.assertIsNone(policy.active_slice(queue(slice_record("q"))))

    def test_blocked_slice_reports_list_blocked_and_deferred_slices_in_order(self) -> None:
        queue_data = queue(
            slice_record("q"),
            slice_record("d", status="deferred"),
            slice_record("b", status="blocked", entry_condition=" when x ", recommended_unblocker=" x ", notes=" n "),
            slice_record("f", status="failed"),
        )
        self.assertEqual(
            [
                policy.BlockedSliceReport("d", "deferred", "", "", ""),
                policy.BlockedSliceReport("b", "blocked", "when x", "x", "n"),
            ],
            policy.blocked_slice_reports(queue_data),
        )


if __name__ == "__main__":
    unittest.main()
