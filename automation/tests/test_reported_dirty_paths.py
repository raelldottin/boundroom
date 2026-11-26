"""What a handoff reports in dirty_paths_outside_scope, as evaluate_completion judges it.

Claiming a slice rewrites the queue file, so a runner that lists everything `git status` shows
outside its scope, as base.md asks, also lists the supervisor's own queue write. That report must
not fail the slice. A path outside the slice's scope that the supervisor doesn't own still must.
"""

from __future__ import annotations

import unittest
from typing import Any

from automation.supervisor import policy

QUEUE = "automation/queue/slices.json"
HANDOFF = "automation/handoffs/20261126T000000Z-fix.json"
OWNED = [QUEUE, "automation/handoffs/"]


def decide(reported: list[str], status: str = "done") -> policy.CompletionDecision:
    slice_record: dict[str, Any] = {
        "slice_id": "fix",
        "manual_proof": [],
        "required_proof_level": "domain-tested",
        "allowed_paths": ["src/semver/version.py", "tests/"],
        "required_validations": ["git diff --check"],
        "max_files_changed": 4,
    }
    handoff: dict[str, Any] = {
        "slice_id": "fix",
        "status": status,
        "proof_level": "domain-tested",
        "provided_proofs": [],
        "validations_passed": ["git diff --check"],
        "validations_failed": [],
        "files_touched": ["src/semver/version.py", "tests/test_fix.py"],
        "dirty_paths_outside_scope": reported,
        "recommended_next_slice": "",
        "recommended_next_reason": "",
    }
    return policy.evaluate_completion(
        queue_data={"policy": {"supervisor_owned_paths": OWNED}, "slices": [slice_record]},
        slice_record=slice_record,
        handoff=handoff,
        dirty_paths_before_run=[],
        dirty_paths_after_run=[QUEUE, HANDOFF, "src/semver/version.py", "tests/test_fix.py"],
        completed_autonomous_runs=1,
        run_limit=2,
        post_run_commit_sha="a" * 40,
        handoff_path=HANDOFF,
    )


class ReportedDirtyPathsTest(unittest.TestCase):
    def test_nothing_reported_is_done(self) -> None:
        # Control: the slice below passes every other gate.
        decision = decide([])
        self.assertEqual(("done", []), (decision.queue_status, decision.dirty_paths_outside_scope))

    def test_reporting_the_supervisors_own_queue_write_is_done(self) -> None:
        decision = decide([QUEUE])
        self.assertEqual("done", decision.queue_status, decision.stop_reason)
        self.assertEqual([], decision.dirty_paths_outside_scope)

    def test_reporting_its_own_handoff_or_an_allowed_path_is_done(self) -> None:
        decision = decide([HANDOFF, "./tests/test_fix.py"])
        self.assertEqual("done", decision.queue_status, decision.stop_reason)
        self.assertEqual([], decision.dirty_paths_outside_scope)

    def test_reporting_a_path_outside_scope_still_fails(self) -> None:
        decision = decide([QUEUE, "README.md"])
        self.assertEqual(("failed", "stop_failed"), (decision.queue_status, decision.decision))
        self.assertEqual(["README.md"], decision.dirty_paths_outside_scope)

    def test_reporting_another_slices_handoff_still_fails(self) -> None:
        # The handoffs directory is owned, but a runner may write only its own handoff there.
        other = "automation/handoffs/another-slice.json"
        decision = decide([other])
        self.assertEqual("failed", decision.queue_status)
        self.assertEqual([other], decision.dirty_paths_outside_scope)

    def test_a_blocked_slice_lists_only_paths_outside_scope(self) -> None:
        decision = decide([QUEUE, "README.md"], status="blocked")
        self.assertEqual(("blocked", ["README.md"]), (decision.queue_status, decision.dirty_paths_outside_scope))


if __name__ == "__main__":
    unittest.main()
