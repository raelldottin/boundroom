"""The manual-proof rules, as a pure function of the handoff and what the supervisor saw on disk.

``check_manual_proofs`` decides from facts that ``observe_proof_files`` gathered, so these tests
state the facts directly and touch no file. The trust-boundary tests in
``test_manual_proof_trust_boundary.py`` cover gathering them from a real proof directory.
"""

from __future__ import annotations

import copy
import itertools
import unittest
from typing import Any, get_args

from automation.schemas.models import ProofLevel
from automation.supervisor import policy
from automation.supervisor.policy import ProofFile

HEAD = "a" * 40
PARENT = "b" * 40
OTHER = "c" * 40
SLICE_ID = "prove-it"
DIGEST = "d" * 64
LEVELS = (
    "doc-only",
    "domain-tested",
    "build-tested",
    "running-app-smoke",
    "flow-verified",
    "screenshot-verified",
    "device-verified",
    "testflight-verified",
)
MISSING_VERIFIED_SHA = "Missing verified_commit_sha (required when the slice requires manual proof)"
INVALID_VERIFIED_SHA = "Invalid verified_commit_sha (does not match supervisor-observed HEAD)"


class MeetsProofLevelTest(unittest.TestCase):
    def test_the_ladder_ranks_exactly_the_levels_the_schemas_accept(self) -> None:
        self.assertEqual(list(get_args(ProofLevel)), list(policy.PROOF_LADDER))

    def test_a_level_meets_itself_and_every_level_below_it(self) -> None:
        for (achieved_rank, achieved), (required_rank, required) in itertools.product(enumerate(LEVELS), repeat=2):
            with self.subTest(achieved=achieved, required=required):
                self.assertEqual(achieved_rank >= required_rank, policy.meets_proof_level(achieved, required))

    def test_empty_and_unknown_levels(self) -> None:
        cases = (
            ("nothing required", "", "", True),
            ("nothing required, something achieved", "doc-only", "", True),
            ("nothing achieved", "", "doc-only", False),
            ("an unknown achieved level", "bogus", "doc-only", False),
            ("achieved levels are matched exactly", " doc-only", "doc-only", False),
            ("achieved levels are case-sensitive", "Doc-Only", "doc-only", False),
            ("unknown achieved and required levels", "bogus", "bogus", False),
            # Current behaviour, fail-open: an unknown required level ranks as doc-only. The schema's
            # Literal keeps an unknown level out of a validated queue.
            ("an unknown required level", "doc-only", "bogus", True),
        )
        for name, achieved, required, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.meets_proof_level(achieved, required))


class VerifiedCommitsTest(unittest.TestCase):
    def test_head_and_the_handoff_commit_parent_when_there_is_one(self) -> None:
        cases = (
            (HEAD, PARENT, {HEAD, PARENT}),
            (HEAD, None, {HEAD}),
            (HEAD, "", {HEAD}),
            ("", None, {""}),
        )
        for head, parent, expected in cases:
            with self.subTest(head=head, parent=parent):
                self.assertEqual(expected, policy.verified_commits(head, parent))


def proof(proof_type: Any = "screenshot", path: Any = "p/shot.png", **overrides: Any) -> dict[str, Any]:
    record = {"proof_type": proof_type, "path": path, "commit_sha": HEAD, "slice_id": SLICE_ID, "sha256": DIGEST}
    record.update(overrides)
    return record


def slice_record(manual_proof: tuple[str, ...] = ("screenshot",), level: str = "") -> dict[str, Any]:
    record: dict[str, Any] = {"slice_id": SLICE_ID, "manual_proof": list(manual_proof)}
    if level:
        record["required_proof_level"] = level
    return record


def handoff(*proofs: dict[str, Any], level: Any = "doc-only", **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {"proof_level": level, "verified_commit_sha": HEAD, "provided_proofs": list(proofs)}
    document.update(overrides)
    return document


FACTS = {"p/shot.png": ProofFile(sha256=DIGEST), "p/video.mov": ProofFile(sha256="e" * 64)}


class CheckManualProofsTest(unittest.TestCase):
    def check(self, record, document, facts=None, head: str = HEAD, parent: str | None = None):
        return policy.check_manual_proofs(record, document, head, parent, FACTS if facts is None else facts)

    def test_a_valid_proof_passes(self) -> None:
        self.assertEqual(([], False), self.check(slice_record(), handoff(proof())))

    def test_the_verified_commit_sha(self) -> None:
        cases = (
            ("absent", {"verified_commit_sha": None}, [MISSING_VERIFIED_SHA]),
            ("empty", {"verified_commit_sha": ""}, [MISSING_VERIFIED_SHA]),
            ("another commit", {"verified_commit_sha": OTHER}, [INVALID_VERIFIED_SHA]),
        )
        for name, overrides, expected in cases:
            with self.subTest(name):
                document = handoff(proof(), **overrides)
                self.assertEqual((expected, False), self.check(slice_record(), document))

        without_key = handoff(proof())
        del without_key["verified_commit_sha"]
        self.assertEqual(([MISSING_VERIFIED_SHA], False), self.check(slice_record(), without_key))

    def test_the_handoff_commit_parent_is_a_verified_commit(self) -> None:
        handoff_commit = "f" * 40
        document = handoff(proof(commit_sha=PARENT), verified_commit_sha=PARENT)
        self.assertEqual(([], False), self.check(slice_record(), document, head=handoff_commit, parent=PARENT))
        self.assertEqual(([INVALID_VERIFIED_SHA, "screenshot"], False), self.check(slice_record(), document, head=handoff_commit))

    def test_without_required_proofs_the_verified_commit_sha_is_not_needed(self) -> None:
        for sha in (None, "", OTHER):
            with self.subTest(verified_commit_sha=sha):
                document = handoff(verified_commit_sha=sha)
                self.assertEqual(([], False), self.check(slice_record(manual_proof=()), document))

    def test_which_provided_proof_counts(self) -> None:
        cases = (
            ("not provided", [], ["screenshot"]),
            ("provided for another slice", [proof(slice_id="other-slice")], ["screenshot"]),
            ("provided for another commit", [proof(commit_sha=OTHER)], ["screenshot"]),
            ("provided without a commit", [{k: v for k, v in proof().items() if k != "commit_sha"}], ["screenshot"]),
            ("a different proof type", [proof(proof_type="video", path="p/video.mov")], ["screenshot"]),
            ("a later proof for another slice does not replace a valid one", [proof(), proof(slice_id="x", path="bad")], []),
            ("a later proof for another commit does not replace a valid one", [proof(), proof(commit_sha=OTHER, path="bad")], []),
            ("the last valid proof of a type counts", [proof(), proof(path="p/missing.png")], ["screenshot (missing file)"]),
            ("the last valid proof of a type counts, reversed", [proof(path="p/missing.png"), proof()], []),
        )
        for name, proofs, expected in cases:
            with self.subTest(name):
                self.assertEqual((expected, False), self.check(slice_record(), handoff(*proofs)))

    def test_each_problem_with_a_proof_file(self) -> None:
        cases = (
            ("an invalid path", {"p/shot.png": ProofFile(problem="invalid path")}, {}, "screenshot (invalid path)"),
            (
                "outside the approved proof root",
                {"p/shot.png": ProofFile(problem="outside approved proof root")},
                {},
                "screenshot (outside approved proof root)",
            ),
            ("a missing file", {"p/shot.png": ProofFile(problem="missing file")}, {}, "screenshot (missing file)"),
            ("no fact for the path fails closed", {}, {}, "screenshot (missing file)"),
            ("an unreadable file", {"p/shot.png": ProofFile()}, {}, "screenshot (could not read file for sha256)"),
            ("a different digest", {"p/shot.png": ProofFile(sha256="0" * 64)}, {}, "screenshot (sha256 mismatch)"),
            ("an uppercase digest", FACTS, {"sha256": DIGEST.upper()}, "screenshot (sha256 mismatch)"),
            ("no digest in the metadata", FACTS, {"sha256": ""}, "screenshot (missing sha256 in metadata)"),
            ("a null digest in the metadata", FACTS, {"sha256": None}, "screenshot (missing sha256 in metadata)"),
            ("a path that is not a string", FACTS, {"path": None}, "screenshot (invalid path)"),
            ("a path that is a number", FACTS, {"path": 7}, "screenshot (invalid path)"),
            (
                "a location problem wins over a missing digest",
                {"p/shot.png": ProofFile(problem="outside approved proof root")},
                {"sha256": ""},
                "screenshot (outside approved proof root)",
            ),
            (
                "a missing digest wins over an unreadable file",
                {"p/shot.png": ProofFile()},
                {"sha256": ""},
                "screenshot (missing sha256 in metadata)",
            ),
        )
        for name, facts, overrides, message in cases:
            with self.subTest(name):
                self.assertEqual(([message], False), self.check(slice_record(), handoff(proof(**overrides)), facts))

    def test_proof_without_a_path_is_looked_up_as_the_empty_path(self) -> None:
        pathless = {k: v for k, v in proof().items() if k != "path"}
        facts = {"": ProofFile(problem="outside approved proof root")}
        self.assertEqual(
            (["screenshot (outside approved proof root)"], False), self.check(slice_record(), handoff(pathless), facts)
        )

    def test_messages_follow_the_required_order(self) -> None:
        record = slice_record(manual_proof=("video", "screenshot", "log", "screenshot"))
        document = handoff(proof(proof_type="video", path="p/video.mov", sha256="0" * 64), proof(), verified_commit_sha=OTHER)
        self.assertEqual(
            ([INVALID_VERIFIED_SHA, "video (sha256 mismatch)", "log"], False),
            self.check(record, document),
        )

    def test_a_duplicated_requirement_is_reported_twice(self) -> None:
        record = slice_record(manual_proof=("screenshot", "screenshot"))
        self.assertEqual((["screenshot", "screenshot"], False), self.check(record, handoff()))

    def test_the_proof_level_is_checked_independently(self) -> None:
        cases = (
            ("level met", slice_record(level="flow-verified"), handoff(proof(), level="flow-verified"), ([], False)),
            ("level above", slice_record(level="flow-verified"), handoff(proof(), level="device-verified"), ([], False)),
            ("level short", slice_record(level="flow-verified"), handoff(proof(), level="running-app-smoke"), ([], True)),
            (
                "level short without manual proofs",
                slice_record(manual_proof=(), level="build-tested"),
                handoff(level="doc-only"),
                ([], True),
            ),
            (
                "no level in the handoff",
                slice_record(manual_proof=(), level="doc-only"),
                {k: v for k, v in handoff().items() if k != "proof_level"},
                ([], True),
            ),
            ("both short", slice_record(level="flow-verified"), handoff(level="doc-only"), (["screenshot"], True)),
        )
        for name, record, document, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, self.check(record, document))

    def test_inputs_are_not_changed(self) -> None:
        record, document, facts = slice_record(level="flow-verified"), handoff(proof(), proof(path="x")), dict(FACTS)
        originals = copy.deepcopy((record, document, facts))
        self.check(record, document, facts)
        self.assertEqual(originals, (record, document, facts))


if __name__ == "__main__":
    unittest.main()
