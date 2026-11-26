"""Trust-boundary regressions for the manual-proof check against a real proof directory.

Every case starts from one known-good manual proof and mutates exactly one
property, so a failure names the property that stopped being enforced. The proof
root is passed explicitly (``proof_root=``); one test keeps the legacy behaviour,
where the root is the current directory, equal to the explicit one.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from automation.supervisor import policy

HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40
SLICE_ID = "today-prove-the-thing"
PROOF_BYTES = b"screenshot bytes"
MISSING_VERIFIED_SHA = "Missing verified_commit_sha (required when the slice requires manual proof)"
INVALID_VERIFIED_SHA = "Invalid verified_commit_sha (does not match supervisor-observed HEAD)"

Mutate = Callable[[dict[str, Any], dict[str, Any], Path], None]


@contextlib.contextmanager
def workspace():
    """A repo root containing one valid proof. The current directory is left alone."""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir).resolve()
        proof_dir = root / "automation" / "proofs" / SLICE_ID
        proof_dir.mkdir(parents=True)
        proof_file = proof_dir / "screenshot.png"
        proof_file.write_bytes(PROOF_BYTES)

        slice_record = {
            "slice_id": SLICE_ID,
            "manual_proof": ["screenshot"],
            "required_proof_level": "screenshot-verified",
            "allowed_paths": ["docs/product/domains/today.md"],
            "required_validations": ["git diff --check"],
            "max_files_changed": 5,
        }
        handoff = {
            "slice_id": SLICE_ID,
            "proof_level": "screenshot-verified",
            "verified_commit_sha": HEAD_SHA,
            "provided_proofs": [
                {
                    "proof_type": "screenshot",
                    "path": str(proof_file),
                    "commit_sha": HEAD_SHA,
                    "created_at": "2026-09-14T00:00:00Z",
                    "slice_id": SLICE_ID,
                    "sha256": hashlib.sha256(PROOF_BYTES).hexdigest(),
                }
            ],
        }
        yield root, slice_record, handoff


def completion_fields() -> dict[str, Any]:
    """The non-proof handoff keys evaluate_completion reads."""
    return {
        "status": "done",
        "validations_passed": ["git diff --check"],
        "validations_failed": [],
        "files_touched": ["docs/product/domains/today.md"],
        "dirty_paths_outside_scope": [],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
    }


def set_path(handoff: dict, path: Any) -> None:
    handoff["provided_proofs"][0]["path"] = path


def drop_proof(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"] = []


def wrong_slice_id(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["slice_id"] = "today-some-other-slice"


def stale_commit_sha(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["commit_sha"] = OTHER_SHA


def path_outside_proof_root(slice_record: dict, handoff: dict, root: Path) -> None:
    stray = root / "screenshot.png"
    stray.write_bytes(PROOF_BYTES)
    set_path(handoff, str(stray))


def path_in_another_slices_proof_dir(slice_record: dict, handoff: dict, root: Path) -> None:
    other = root / "automation" / "proofs" / "today-some-other-slice" / "screenshot.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(PROOF_BYTES)
    set_path(handoff, str(other))


def path_traversal(slice_record: dict, handoff: dict, root: Path) -> None:
    set_path(handoff, str(root / "automation" / "proofs" / SLICE_ID / ".." / ".." / ".." / "screenshot.png"))


def symlink_escaping_proof_root(slice_record: dict, handoff: dict, root: Path) -> None:
    outside = root / "outside.png"
    outside.write_bytes(PROOF_BYTES)
    link = root / "automation" / "proofs" / SLICE_ID / "linked.png"
    link.symlink_to(outside)
    set_path(handoff, str(link))


def symlinked_proofs_directory(slice_record: dict, handoff: dict, root: Path) -> None:
    # automation/proofs itself is a link to a directory elsewhere: the approved root is not followed.
    shutil.move(str(root / "automation" / "proofs"), str(root / "elsewhere"))
    (root / "automation" / "proofs").symlink_to(root / "elsewhere")


def path_is_the_proof_directory(slice_record: dict, handoff: dict, root: Path) -> None:
    set_path(handoff, str(root / "automation" / "proofs" / SLICE_ID))


def empty_path(slice_record: dict, handoff: dict, root: Path) -> None:
    set_path(handoff, "")


def path_with_nul_byte(slice_record: dict, handoff: dict, root: Path) -> None:
    set_path(handoff, str(root / "automation" / "proofs" / SLICE_ID / "shot\x00.png"))


def path_not_a_string(slice_record: dict, handoff: dict, root: Path) -> None:
    set_path(handoff, None)


def relative_path(slice_record: dict, handoff: dict, root: Path) -> None:
    set_path(handoff, f"automation/proofs/{SLICE_ID}/screenshot.png")


def outside_and_no_sha256(slice_record: dict, handoff: dict, root: Path) -> None:
    path_outside_proof_root(slice_record, handoff, root)
    sha256_absent(slice_record, handoff, root)


def file_absent(slice_record: dict, handoff: dict, root: Path) -> None:
    (root / "automation" / "proofs" / SLICE_ID / "screenshot.png").unlink()


def sha256_mismatch(slice_record: dict, handoff: dict, root: Path) -> None:
    (root / "automation" / "proofs" / SLICE_ID / "screenshot.png").write_bytes(b"swapped after recording")


def sha256_absent(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["sha256"] = ""


def proof_level_below_required(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["proof_level"] = "flow-verified"


def proof_level_above_required(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["proof_level"] = "device-verified"


def verified_sha_absent(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff.pop("verified_commit_sha")


def verified_sha_null(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["verified_commit_sha"] = None


def verified_sha_mismatched(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["verified_commit_sha"] = OTHER_SHA


# name, mutation, expected missing_manual_proofs, expected missing_proof_level
CASES: tuple[tuple[str, Mutate | None, list[str], bool], ...] = (
    ("baseline_valid_proof", None, [], False),
    ("relative_path_resolves_against_the_proof_root", relative_path, [], False),
    ("required_proof_not_provided", drop_proof, ["screenshot"], False),
    ("proof_recorded_for_another_slice", wrong_slice_id, ["screenshot"], False),
    ("proof_recorded_against_stale_commit", stale_commit_sha, ["screenshot"], False),
    ("proof_path_outside_proof_root", path_outside_proof_root, ["screenshot (outside approved proof root)"], False),
    (
        "proof_path_in_another_slices_proof_dir",
        path_in_another_slices_proof_dir,
        ["screenshot (outside approved proof root)"],
        False,
    ),
    ("proof_path_traverses_out_of_proof_root", path_traversal, ["screenshot (outside approved proof root)"], False),
    ("proof_path_symlinks_out_of_proof_root", symlink_escaping_proof_root, ["screenshot (outside approved proof root)"], False),
    ("proofs_directory_is_a_symlink", symlinked_proofs_directory, ["screenshot (outside approved proof root)"], False),
    ("proof_path_is_empty", empty_path, ["screenshot (outside approved proof root)"], False),
    ("proof_path_has_a_nul_byte", path_with_nul_byte, ["screenshot (invalid path)"], False),
    ("proof_path_is_not_a_string", path_not_a_string, ["screenshot (invalid path)"], False),
    ("proof_path_is_the_proof_directory", path_is_the_proof_directory, ["screenshot (could not read file for sha256)"], False),
    ("location_problem_wins_over_missing_sha256", outside_and_no_sha256, ["screenshot (outside approved proof root)"], False),
    ("proof_file_missing_on_disk", file_absent, ["screenshot (missing file)"], False),
    ("proof_file_changed_after_recording", sha256_mismatch, ["screenshot (sha256 mismatch)"], False),
    ("proof_metadata_has_no_sha256", sha256_absent, ["screenshot (missing sha256 in metadata)"], False),
    ("proof_level_below_required", proof_level_below_required, [], True),
    ("proof_level_above_required", proof_level_above_required, [], False),
    ("verified_commit_sha_absent", verified_sha_absent, [MISSING_VERIFIED_SHA], False),
    ("verified_commit_sha_null", verified_sha_null, [MISSING_VERIFIED_SHA], False),
    ("verified_commit_sha_mismatched", verified_sha_mismatched, [INVALID_VERIFIED_SHA], False),
)


class ManualProofTrustBoundaryTest(unittest.TestCase):
    def mutated(self, mutate: Mutate | None):
        """A workspace with *mutate* applied to copies of its records."""
        stack = contextlib.ExitStack()
        root, slice_record, handoff = stack.enter_context(workspace())
        self.addCleanup(stack.close)
        slice_record, handoff = copy.deepcopy(slice_record), copy.deepcopy(handoff)
        if mutate is not None:
            mutate(slice_record, handoff, root)
        return root, slice_record, handoff

    def test_one_mutation_per_case(self) -> None:
        for name, mutate, expected_proofs, expects_missing_level in CASES:
            with self.subTest(case=name):
                root, slice_record, handoff = self.mutated(mutate)
                result = policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA, proof_root=root)
                self.assertEqual((expected_proofs, expects_missing_level), result)

    def test_the_current_directory_is_the_proof_root_when_none_is_given(self) -> None:
        for name, mutate, _, _ in CASES:
            with self.subTest(case=name):
                root, slice_record, handoff = self.mutated(mutate)
                explicit = policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA, proof_root=root)
                with contextlib.chdir(root):
                    legacy = policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA)
                self.assertEqual(explicit, legacy)

    def test_an_unresolved_proof_root_is_resolved_before_the_check(self) -> None:
        root, slice_record, handoff = self.mutated(None)
        with tempfile.TemporaryDirectory() as temp_dir:
            alias = Path(temp_dir) / "alias"
            alias.symlink_to(root)
            handoff["provided_proofs"][0]["path"] = str(alias / "automation" / "proofs" / SLICE_ID / "screenshot.png")
            self.assertEqual(([], False), policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA, proof_root=alias))
            self.assertEqual(
                ([], False),
                policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA, proof_root=Path(os.path.relpath(alias))),
            )

    def test_only_the_file_of_each_proof_that_counts_is_observed(self) -> None:
        root, slice_record, handoff = self.mutated(None)
        valid = handoff["provided_proofs"][0]
        recording = f"automation/proofs/{SLICE_ID}/recording.mov"
        (root / recording).write_bytes(b"recording bytes")
        slice_record["manual_proof"] = ["screenshot", "recording"]
        handoff["provided_proofs"] = [
            {**valid, "path": str(root / "superseded.png")},  # an earlier screenshot the next one replaces
            valid,
            {**valid, "slice_id": "today-some-other-slice", "path": str(root / "another-slice.png")},
            {**valid, "commit_sha": OTHER_SHA, "path": str(root / "stale.png")},
            {**valid, "proof_type": "video", "path": str(root / "not-required.mov")},
            # Located but not read: there is no recorded digest to compare the bytes with.
            {**valid, "proof_type": "recording", "path": recording, "sha256": ""},
        ]
        self.assertEqual(
            {
                valid["path"]: policy.ProofFile(sha256=hashlib.sha256(PROOF_BYTES).hexdigest()),
                recording: policy.ProofFile(),
            },
            policy.observe_proof_files(root, slice_record, handoff, HEAD_SHA),
        )
        self.assertEqual(
            ({}, {}),
            (
                policy.observe_proof_files(
                    root, slice_record, {**handoff, "provided_proofs": [{**valid, "path": None}]}, HEAD_SHA
                ),
                policy.observe_proof_files(root, slice_record, handoff, "c" * 40),
            ),
        )

    def test_a_proof_for_the_commit_before_the_handoff_commit_is_accepted(self) -> None:
        # A runner that commits its handoff moves HEAD one commit past what the proof names, and a
        # handoff can't name the commit that contains it. The supervisor passes that parent only
        # when the last commit changed nothing but the handoff (git_handoff_commit_parent).
        handoff_commit = "f" * 40
        root, slice_record, handoff = self.mutated(None)
        accepted = policy.verify_manual_proofs(slice_record, handoff, handoff_commit, HEAD_SHA, proof_root=root)
        refused = policy.verify_manual_proofs(slice_record, handoff, handoff_commit, proof_root=root)
        self.assertEqual(([], False), accepted)
        self.assertEqual(([INVALID_VERIFIED_SHA, "screenshot"], False), refused)

    def test_verified_commit_sha_not_required_when_no_manual_proofs(self) -> None:
        root, slice_record, handoff = self.mutated(verified_sha_absent)
        slice_record["manual_proof"] = []
        self.assertEqual(([], False), policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA, proof_root=root))

    def completion_decision(self, root: Path, slice_record: dict, handoff: dict) -> policy.CompletionDecision:
        """Run the same proof through the supervisor completion boundary."""
        handoff.update(completion_fields())
        queue_data = {
            "policy": {"supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"]},
            "slices": [slice_record],
        }
        return policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=2,
            post_run_commit_sha=HEAD_SHA,
            proof_facts=policy.observe_proof_files(root, slice_record, handoff, HEAD_SHA),
        )

    ACCEPTED = policy.CompletionDecision(
        queue_status="done",
        decision="stop",
        should_continue=False,
        stop_reason="No eligible queued next slice remains after the current slice completed.",
        next_slice_id=None,
        recommended_next_slice="",
        required_validation_failures=[],
        dirty_paths_outside_scope=[],
        files_touched_outside_scope=[],
        changed_file_count=1,
    )

    def rejected(self, missing_manual_proofs: list[str], missing_proof_level: bool = False) -> policy.CompletionDecision:
        return dataclasses.replace(
            self.ACCEPTED,
            queue_status="failed",
            decision="stop_failed",
            stop_reason="Slice reported done, but required manual proof or proof level was missing.",
            missing_manual_proofs=missing_manual_proofs,
            missing_proof_level=missing_proof_level,
        )

    def test_completion_rejects_each_proof_problem_the_check_reports(self) -> None:
        for name, mutate, expected_proofs, expects_missing_level in CASES:
            if not (expected_proofs or expects_missing_level):
                continue
            with self.subTest(case=name):
                root, slice_record, handoff = self.mutated(mutate)
                decision = self.completion_decision(root, slice_record, handoff)
                self.assertEqual(self.rejected(expected_proofs, expects_missing_level), decision)

    def test_completion_accepts_done_handoff_with_valid_proof(self) -> None:
        """Control: the rejections above must come from the proof, not the rest of the handoff."""
        root, slice_record, handoff = self.mutated(None)
        self.assertEqual(self.ACCEPTED, self.completion_decision(root, slice_record, handoff))


if __name__ == "__main__":
    unittest.main()
