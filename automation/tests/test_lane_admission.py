"""Which queued slices start a lane now: SPEC-014 "How many lanes run" (RA-LANE-003).

``lane_admission.admit`` is pure, so every test here is output-based: a queue, the lanes in
flight and the limits go in, and the whole ``Admission`` is compared.
"""

from __future__ import annotations

import random
import unittest
from typing import Any

from automation.supervisor import policy
from automation.supervisor.lane_admission import (
    Admission,
    Hold,
    Lane,
    Limits,
    admit,
    eligible_slices,
    lanes_in_flight,
    paths_overlap,
)


def slice_record(
    slice_id: str,
    paths: tuple[str, ...] = (),
    *,
    status: str = "queued",
    priority: int = 10,
    depends_on: tuple[str, ...] = (),
    exclusive: bool | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "slice_id": slice_id,
        "status": status,
        "priority": priority,
        "depends_on": list(depends_on),
        "allowed_paths": list(paths),
    }
    if exclusive is not None:
        record["exclusive"] = exclusive
    return record


def queue(*slices: dict[str, Any]) -> dict[str, Any]:
    return {"policy": {}, "slices": list(slices)}


def lane(slice_id: str, *paths: str, exclusive: bool = False) -> Lane:
    return Lane(slice_id=slice_id, allowed_paths=paths, exclusive=exclusive)


ROOMY = Limits(ceiling=8, budget=8)


class AdmissionTableTest(unittest.TestCase):
    """Each row: a queue, the lanes running, the limits, and the admission expected."""

    CASES: list[tuple[str, dict[str, Any], tuple[Lane, ...], Limits, Admission]] = [
        ("an empty queue admits nothing", queue(), (), ROOMY, Admission(start=(), held=())),
        (
            "disjoint ready slices all start",
            queue(slice_record("a", ("src/a/",)), slice_record("b", ("src/b/",))),
            (),
            ROOMY,
            Admission(start=("a", "b"), held=()),
        ),
        (
            "slices that are not ready are neither started nor held",
            queue(
                slice_record("done", ("x/",), status="done"),
                slice_record("blocked", ("y/",), status="blocked"),
                slice_record("deferred", ("y/",), status="deferred"),
                slice_record("failed", ("y/",), status="failed"),
                slice_record("waits", ("z/",), depends_on=("blocked",)),
                slice_record("ready", ("w/",), depends_on=("done",)),
            ),
            (),
            ROOMY,
            Admission(start=("ready",), held=()),
        ),
        (
            "a slice that depends on a running slice waits for it to be done",
            queue(slice_record("a", ("src/a/",), status="in_progress"), slice_record("b", ("src/b/",), depends_on=("a",))),
            (lane("a", "src/a/"),),
            ROOMY,
            Admission(start=(), held=()),
        ),
        (
            "a running slice still marked queued is not started twice",
            queue(slice_record("a", ("src/a/",)), slice_record("b", ("src/b/",))),
            (lane("a", "src/a/"),),
            ROOMY,
            Admission(start=("b",), held=()),
        ),
        (
            "lower priority numbers go first, and a tie keeps queue order",
            queue(
                slice_record("late", ("src/",), priority=30),
                slice_record("first", ("src/a/",), priority=10),
                slice_record("tie", ("src/a/x",), priority=10),
            ),
            (),
            ROOMY,
            Admission(start=("first",), held=(Hold("tie", "overlap", "first"), Hold("late", "overlap", "first"))),
        ),
        (
            "a path inside a running lane's prefix overlaps it",
            queue(slice_record("b", ("src/lib/",))),
            (lane("a", "src/"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"),)),
        ),
        (
            "a running lane inside the slice's prefix overlaps it too",
            queue(slice_record("b", ("src",))),
            (lane("a", "src/lib/x.py"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"),)),
        ),
        (
            "a shared name prefix is not a shared path",
            queue(slice_record("b", ("src2/",)), slice_record("c", ("src.md",))),
            (lane("a", "src"),),
            ROOMY,
            Admission(start=("b", "c"), held=()),
        ),
        (
            "paths are compared after normalization",
            queue(slice_record("b", (" ./src//lib/ ",))),
            (lane("a", "src/lib"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"),)),
        ),
        (
            "a held slice reserves its paths, so a later slice cannot start on them",
            queue(
                slice_record("b", ("src/a/", "src/b/")),
                slice_record("c", ("src/b/",)),
                slice_record("d", ("docs/",)),
            ),
            (lane("a", "src/a/"),),
            ROOMY,
            Admission(start=("d",), held=(Hold("b", "overlap", "a"), Hold("c", "held", "b"))),
        ),
        (
            "holding is transitive: a slice held behind a held slice reserves its own paths",
            queue(
                slice_record("b", ("src/a/", "src/b/")),
                slice_record("c", ("src/b/", "src/c/")),
                slice_record("d", ("src/c/",)),
            ),
            (lane("a", "src/a/"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"), Hold("c", "held", "b"), Hold("d", "held", "c"))),
        ),
        (
            "a whole-repository slice waits for running lanes and holds every later slice",
            queue(slice_record("b", (".",)), slice_record("c", ("docs/",))),
            (lane("a", "src/"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"), Hold("c", "held", "b"))),
        ),
        (
            "an empty path list claims the whole repository",
            queue(slice_record("b", ()), slice_record("c", ("docs/",))),
            (lane("a", "src/"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"), Hold("c", "held", "b"))),
        ),
        (
            "with nothing running, a whole-repository slice runs alone",
            queue(slice_record("b", ("./",)), slice_record("c", ("docs/",))),
            (),
            ROOMY,
            Admission(start=("b",), held=(Hold("c", "overlap", "b"),)),
        ),
        (
            "a running lane that claims the whole repository blocks every slice",
            queue(slice_record("b", ("docs/",))),
            (lane("a"),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "overlap", "a"),)),
        ),
        (
            "nothing starts beside a running exclusive lane",
            queue(slice_record("b", ("docs/",))),
            (lane("a", "src/", exclusive=True),),
            ROOMY,
            Admission(start=(), held=(Hold("b", "exclusive", "a"),)),
        ),
        (
            "an exclusive slice waits for running lanes and holds every later slice",
            queue(slice_record("x", ("src/x/",), exclusive=True), slice_record("c", ("docs/",))),
            (lane("a", "src/a/"),),
            ROOMY,
            Admission(start=(), held=(Hold("x", "exclusive", "a"), Hold("c", "held", "x"))),
        ),
        (
            "with nothing running, an exclusive slice starts alone",
            queue(slice_record("x", ("src/x/",), exclusive=True), slice_record("c", ("docs/",))),
            (),
            ROOMY,
            Admission(start=("x",), held=(Hold("c", "exclusive", "x"),)),
        ),
        (
            "an exclusive slice after one that starts now waits for it",
            queue(
                slice_record("a", ("src/a/",), priority=1),
                slice_record("x", ("src/x/",), priority=2, exclusive=True),
                slice_record("c", ("docs/",), priority=3),
            ),
            (),
            ROOMY,
            Admission(start=("a",), held=(Hold("x", "exclusive", "a"), Hold("c", "held", "x"))),
        ),
        (
            "exclusive false is an ordinary slice",
            queue(slice_record("b", ("docs/",), exclusive=False)),
            (lane("a", "src/"),),
            ROOMY,
            Admission(start=("b",), held=()),
        ),
        (
            "a slice the queue marks in_progress keeps its paths though the caller didn't pass it",
            queue(
                slice_record("x", ("src/",), status="in_progress"), slice_record("y", ("src/a/",)), slice_record("z", ("docs/",))
            ),
            (),
            ROOMY,
            Admission(start=("z",), held=(Hold("y", "overlap", "x"),)),
        ),
        (
            "a slice the queue marks in_progress takes a lane under the ceiling",
            queue(slice_record("x", ("src/",), status="in_progress"), slice_record("y", ("docs/",))),
            (),
            Limits(ceiling=1, budget=8),
            Admission(start=(), held=(Hold("y", "ceiling"),)),
        ),
        (
            "a lane passed in and marked in_progress takes one lane, not two",
            queue(slice_record("x", ("src/",), status="in_progress"), slice_record("y", ("docs/",))),
            (lane("x", "src/"),),
            Limits(ceiling=2, budget=8),
            Admission(start=("y",), held=()),
        ),
        (
            "the ceiling counts running lanes",
            queue(slice_record("b", ("src/b/",)), slice_record("c", ("src/c/",))),
            (lane("a", "src/a/"),),
            Limits(ceiling=2, budget=8),
            Admission(start=("b",), held=(Hold("c", "ceiling"),)),
        ),
        (
            "a ceiling of one starts one lane",
            queue(slice_record("a", ("src/a/",)), slice_record("b", ("src/b/",))),
            (),
            Limits(ceiling=1, budget=8),
            Admission(start=("a",), held=(Hold("b", "ceiling"),)),
        ),
        (
            "more lanes running than the ceiling allows starts none",
            queue(slice_record("c", ("src/c/",))),
            (lane("a", "src/a/"), lane("b", "src/b/")),
            Limits(ceiling=1, budget=8),
            Admission(start=(), held=(Hold("c", "ceiling"),)),
        ),
        (
            "the budget caps lanes started, whatever the ceiling",
            queue(slice_record("a", ("src/a/",)), slice_record("b", ("src/b/",)), slice_record("c", ("src/c/",))),
            (),
            Limits(ceiling=8, budget=2),
            Admission(start=("a", "b"), held=(Hold("c", "budget"),)),
        ),
        (
            "a spent budget starts nothing and says so before the ceiling",
            queue(slice_record("b", ("src/b/",))),
            (lane("a", "src/a/"),),
            Limits(ceiling=1, budget=0),
            Admission(start=(), held=(Hold("b", "budget"),)),
        ),
        (
            "an overlap is reported before a full ceiling",
            queue(slice_record("b", ("src/a/x",)), slice_record("c", ("src/c/",))),
            (lane("a", "src/a/"),),
            Limits(ceiling=1, budget=8),
            Admission(start=(), held=(Hold("b", "overlap", "a"), Hold("c", "ceiling"))),
        ),
        (
            "a slice that misses only for room reserves nothing",
            queue(slice_record("a", ("src/a/",)), slice_record("b", ("src/b/",)), slice_record("c", ("src/b/x",))),
            (),
            Limits(ceiling=1, budget=8),
            Admission(start=("a",), held=(Hold("b", "ceiling"), Hold("c", "ceiling"))),
        ),
    ]

    def test_each_case_admits_what_the_spec_says(self) -> None:
        for name, queue_data, running, limits, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(expected, admit(queue_data, running, limits))

    def test_admission_leaves_the_queue_unchanged(self) -> None:
        queue_data = queue(slice_record("a", ("src/a/",), priority=5), slice_record("b", ("src/a/",), priority=1))
        before = repr(queue_data)
        admit(queue_data, (), ROOMY)
        self.assertEqual(before, repr(queue_data))


class LaneTest(unittest.TestCase):
    def test_a_slice_without_allowed_paths_is_refused_rather_than_given_the_repository(self) -> None:
        with self.assertRaises(KeyError):
            Lane.for_slice({"slice_id": "a", "status": "queued", "priority": 1, "depends_on": []})


class LimitsTest(unittest.TestCase):
    def test_a_ceiling_below_one_is_refused(self) -> None:
        for ceiling in (0, -1):
            with self.subTest(ceiling=ceiling), self.assertRaisesRegex(ValueError, "ceiling must be at least 1"):
                Limits(ceiling=ceiling, budget=1)

    def test_a_negative_budget_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "budget cannot be negative"):
            Limits(ceiling=1, budget=-1)


class OverlapTest(unittest.TestCase):
    """Overlap is containment either way, by the rule ``policy.path_matches_prefix`` uses."""

    def test_overlap_agrees_with_path_matches_prefix_in_both_directions(self) -> None:
        paths = ["src", "src/", "src/a", "src/a/b.py", "src2", "./src/a/", "docs/x.md", "docs", "a b/c", "src.md"]
        for one in paths:
            for other in paths:
                with self.subTest(one=one, other=other):
                    expected = policy.path_matches_prefix(one, other) or policy.path_matches_prefix(other, one)
                    self.assertEqual(expected, paths_overlap([one], [other]))

    def test_any_overlapping_pair_is_an_overlap(self) -> None:
        self.assertTrue(paths_overlap(["docs/", "src/a/"], ["tests/", "src/"]))
        self.assertFalse(paths_overlap(["docs/", "src/a/"], ["tests/", "src/b/"]))

    def test_an_empty_list_or_dot_overlaps_everything(self) -> None:
        for whole in ([], ["."], ["./"], ["docs/", "."]):
            with self.subTest(whole=whole):
                self.assertTrue(paths_overlap(whole, ["anything/at/all"]))
                self.assertTrue(paths_overlap(["anything/at/all"], whole))


def random_queue(rng: random.Random, *, in_progress: bool = True) -> dict[str, Any]:
    roots = ["src", "src/a", "src/a/x.py", "src/b", "docs", "docs/a.md", "tests/a", "src2", "lib", "bin/", "web/app", "./web"]
    statuses = ["queued", "queued", "queued", "queued", "done", "blocked", "deferred", "failed"]
    if in_progress:
        statuses.append("in_progress")
    slices = []
    for index in range(rng.randint(0, 9)):
        earlier = [record["slice_id"] for record in slices]
        whole = rng.random()
        paths = () if whole < 0.03 else (".",) if whole < 0.06 else tuple(rng.sample(roots, rng.randint(1, 2)))
        slices.append(
            slice_record(
                f"s{index}",
                paths,
                status=rng.choice(statuses),
                priority=rng.randint(1, 4),
                depends_on=tuple(rng.sample(earlier, min(len(earlier), rng.randint(0, 2)))),
                exclusive=rng.random() < 0.08 or None,
            )
        )
    return queue(*slices)


class AdmissionPropertyTest(unittest.TestCase):
    """Properties over many seeded random queues, so a counterexample is reproducible."""

    SEEDS = range(2000)

    def test_with_nothing_running_the_first_slice_admitted_is_select_next_slice(self) -> None:
        for seed in self.SEEDS:
            rng = random.Random(seed)
            queue_data = random_queue(rng, in_progress=False)
            limits = Limits(ceiling=rng.randint(1, 8), budget=rng.randint(1, 8))
            expected = policy.select_next_slice(queue_data)
            admission = admit(queue_data, (), limits)
            with self.subTest(seed=seed):
                self.assertEqual(expected["slice_id"] if expected else None, admission.start[0] if admission.start else None)

    def test_admitted_lanes_never_collide_and_respect_the_limits(self) -> None:
        for seed in self.SEEDS:
            rng = random.Random(seed)
            queue_data = random_queue(rng)
            by_id = {record["slice_id"]: record for record in queue_data["slices"]}
            running_ids = rng.sample(sorted(by_id), min(len(by_id), rng.randint(0, 2)))
            passed = tuple(Lane.for_slice(by_id[slice_id]) for slice_id in running_ids)
            running = lanes_in_flight(queue_data, passed)
            running_ids = [lane.slice_id for lane in running]
            limits = Limits(ceiling=rng.randint(1, 8), budget=rng.randint(0, 8))
            admission = admit(queue_data, passed, limits)
            started = [Lane.for_slice(by_id[slice_id]) for slice_id in admission.start]
            with self.subTest(seed=seed):
                self.assertLessEqual(len(started), min(max(limits.ceiling - len(running), 0), limits.budget))
                lanes = [*running, *started]
                for position, one in enumerate(started):
                    for other in lanes[: len(running) + position]:
                        self.assertFalse(one.exclusive or other.exclusive, (one, other))
                        self.assertFalse(paths_overlap(one.allowed_paths, other.allowed_paths), (one, other))
                eligible = {
                    record["slice_id"]
                    for record in queue_data["slices"]
                    if policy.slice_is_eligible(record, queue_data) and record["slice_id"] not in running_ids
                }
                reported = [*admission.start, *(hold.slice_id for hold in admission.held)]
                self.assertEqual(sorted(eligible), sorted(reported))
                self.assertEqual(len(reported), len(set(reported)))

    def test_no_slice_starts_on_paths_an_earlier_held_slice_needs(self) -> None:
        for seed in self.SEEDS:
            rng = random.Random(seed)
            queue_data = random_queue(rng)
            by_id = {record["slice_id"]: record for record in queue_data["slices"]}
            running_ids = rng.sample(sorted(by_id), min(len(by_id), rng.randint(0, 2)))
            passed = tuple(Lane.for_slice(by_id[slice_id]) for slice_id in running_ids)
            running_ids = [lane.slice_id for lane in lanes_in_flight(queue_data, passed)]
            admission = admit(queue_data, passed, ROOMY)
            order = [record["slice_id"] for record in eligible_slices(queue_data, running_ids)]
            blocked = [hold.slice_id for hold in admission.held if hold.reason in {"overlap", "held", "exclusive"}]
            with self.subTest(seed=seed):
                for held_id in blocked:
                    held_lane = Lane.for_slice(by_id[held_id])
                    for started_id in admission.start:
                        if order.index(started_id) > order.index(held_id):
                            started_lane = Lane.for_slice(by_id[started_id])
                            self.assertFalse(held_lane.exclusive or started_lane.exclusive, (held_id, started_id))
                            self.assertFalse(paths_overlap(held_lane.allowed_paths, started_lane.allowed_paths))


if __name__ == "__main__":
    unittest.main()
