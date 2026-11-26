"""How the supervisor judges a finished run: ``policy.evaluate_completion``.

Every case starts from one run that succeeds and changes one input. The expected decision is the
success decision with the fields that case changes, compared whole, so a field nobody meant to
change can't drift unnoticed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from automation.supervisor import policy

OWNED = ["automation/handoffs/", "automation/queue/slices.json"]
SCOPE_REASON = "Files changed outside the slice's allowed_paths were detected."
VALIDATION_REASON = "Required validations were missing, failed, or did not pass supervisor replay."
PROOF_REASON = "Slice reported done, but required manual proof or proof level was missing."
HEAD = "a" * 40
PROOF_BYTES = b"proof"
PROOF_PATH = "automation/proofs/cur/shot.png"


def slice_record(slice_id: str = "cur", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "slice_id": slice_id,
        "status": "in_progress" if slice_id == "cur" else "queued",
        "priority": 1,
        "allowed_paths": ["docs"],
        "required_validations": ["git diff --check"],
        "depends_on": [],
        "max_files_changed": 2,
    }
    record.update(overrides)
    return record


def handoff(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "slice_id": "cur",
        "status": "done",
        "files_touched": ["docs/a.md"],
        "validations_passed": ["git diff --check"],
        "validations_failed": [],
        "proof_level": "doc-only",
        "recommended_next_slice": "",
        "dirty_paths_outside_scope": [],
    }
    document.update(overrides)
    return document


def evaluate(**keywords: Any) -> policy.CompletionDecision:
    """Evaluate the baseline run, changed by *keywords*.

    ``current``, ``others``, ``handoff_document``, ``before`` and ``after`` replace the current slice, the
    other queued slices, the handoff and the dirty paths before and after the run; every other keyword
    goes to ``evaluate_completion`` as it is.
    """
    current = keywords.pop("current", None) or slice_record()
    others = keywords.pop("others", None)
    others = [slice_record("next")] if others is None else others
    after = keywords.pop("after", None)
    queue_data = {"policy": {"supervisor_owned_paths": OWNED}, "slices": [current, *others]}
    keywords.setdefault("completed_autonomous_runs", 0)
    keywords.setdefault("run_limit", 3)
    return policy.evaluate_completion(
        queue_data=queue_data,
        slice_record=current,
        handoff=keywords.pop("handoff_document", None) or handoff(),
        dirty_paths_before_run=keywords.pop("before", None) or [],
        dirty_paths_after_run=["docs/a.md"] if after is None else after,
        **keywords,
    )


def success(**overrides: Any) -> policy.CompletionDecision:
    decision = policy.CompletionDecision(
        queue_status="done",
        decision="continue",
        should_continue=True,
        stop_reason="Continuation allowed into the highest-priority eligible queued slice.",
        next_slice_id="next",
        recommended_next_slice="",
        required_validation_failures=[],
        dirty_paths_outside_scope=[],
        files_touched_outside_scope=[],
        changed_file_count=1,
    )
    return dataclasses.replace(decision, **overrides)


def failed(stop_reason: str, **overrides: Any) -> policy.CompletionDecision:
    stop = {"queue_status": "failed", "decision": "stop_failed", "should_continue": False, "next_slice_id": None}
    return success(stop_reason=stop_reason, **{**stop, **overrides})


class BaselineTest(unittest.TestCase):
    """A run is charged with what it changed: paths dirty after it but not before, plus what it committed."""

    def test_the_run_is_charged_only_with_what_it_introduced_or_committed(self) -> None:
        cases = (
            ("the baseline run", {}, success()),
            ("dirt that predates the run is not the run's", {"before": ["src/x"], "after": ["src/x", "docs/a.md"]}, success()),
            ("predating dirt is compared normalized", {"before": ["./src/x"], "after": ["src/x", "docs/a.md"]}, success()),
            (
                "a predating path the run also committed is the run's",
                {"before": ["src/x"], "after": ["src/x", "docs/a.md"], "committed_paths": ["src/x"]},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["src/x"]),
            ),
            (
                "a committed path no longer dirty is the run's",
                {"after": [], "committed_paths": ["src/x", "docs/a.md"]},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["src/x"]),
            ),
            (
                "committed and dirty paths are combined",
                {"after": ["src/y"], "committed_paths": ["src/x"]},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["src/x", "src/y"]),
            ),
            ("a blank committed entry is dropped", {"after": [], "committed_paths": [" "]}, success()),
            ("no committed paths is the same as none given", {"committed_paths": None}, success()),
        )
        for name, keywords, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, evaluate(**keywords))


class ScopeTest(unittest.TestCase):
    def test_paths_outside_the_allowed_prefixes_fail_the_slice(self) -> None:
        cases = (
            (
                "a sibling of the allowed prefix",
                {"after": ["docs-private/x.md"]},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["docs-private/x.md"]),
            ),
            (
                "an allowed prefix with a trailing slash",
                {"current": slice_record(allowed_paths=["docs/"])},
                success(),
            ),
            (
                "the supervisor's own exact file",
                {"after": ["docs/a.md", "automation/queue/slices.json"]},
                success(),
            ),
            (
                "a neighbour of an owned exact file",
                {"after": ["automation/queue/slices.json.bak"]},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["automation/queue/slices.json.bak"]),
            ),
            (
                "a file the handoff says it touched outside scope",
                {"handoff_document": handoff(files_touched=["docs/a.md", "src/b.py"])},
                failed(SCOPE_REASON, files_touched_outside_scope=["src/b.py"], changed_file_count=2),
            ),
            (
                "a path the handoff reports outside scope",
                {"handoff_document": handoff(dirty_paths_outside_scope=["src/r"])},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["src/r"]),
            ),
            (
                "the supervisor's queue write, reported by the runner, is not held against it",
                {"handoff_document": handoff(dirty_paths_outside_scope=["automation/queue/slices.json"])},
                success(),
            ),
            (
                "found and reported paths are combined",
                {"after": ["docs/a.md", "src/a"], "handoff_document": handoff(dirty_paths_outside_scope=["src/b"])},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["src/a", "src/b"]),
            ),
        )
        for name, keywords, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, evaluate(**keywords))

    def test_the_runs_own_handoff_is_in_scope_but_another_handoff_beside_it_is_not(self) -> None:
        own = "automation/handoffs/20260101-cur.json"
        cases = (
            ("own handoff", [own], success()),
            (
                "another slice's handoff",
                [own, "automation/handoffs/20260101-next.json"],
                failed(SCOPE_REASON, dirty_paths_outside_scope=["automation/handoffs/20260101-next.json"]),
            ),
        )
        for name, extra, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, evaluate(after=["docs/a.md", *extra], handoff_path=own))


class FileBudgetTest(unittest.TestCase):
    def test_the_count_may_reach_max_files_changed_but_not_pass_it(self) -> None:
        def over(count: int) -> policy.CompletionDecision:
            return failed(f"Slice exceeded max_files_changed ({count} > 2).", changed_file_count=count)

        two = ["docs/a.md", "docs/b.md"]
        three = [*two, "docs/c.md"]
        cases = (
            (
                "exactly the maximum",
                {"after": two, "handoff_document": handoff(files_touched=two)},
                success(changed_file_count=2),
            ),
            ("one past the maximum", {"after": three, "handoff_document": handoff(files_touched=two)}, over(3)),
            ("counted from files_touched alone", {"after": [], "handoff_document": handoff(files_touched=three)}, over(3)),
            ("the larger count wins", {"after": three, "handoff_document": handoff(files_touched=["docs/a.md"])}, over(3)),
            (
                "an owned path left dirty is not counted",
                {"after": [*two, "automation/queue/slices.json"], "handoff_document": handoff(files_touched=two)},
                success(changed_file_count=2),
            ),
            (
                "an owned path in files_touched is counted",
                {"after": two, "handoff_document": handoff(files_touched=[*two, "automation/queue/slices.json"])},
                over(3),
            ),
            (
                "files_touched spellings of one path count once",
                {"after": two, "handoff_document": handoff(files_touched=["docs/a.md", "./docs/a.md", "docs/b.md"])},
                success(changed_file_count=2),
            ),
        )
        for name, keywords, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, evaluate(**keywords))


class GateOrderTest(unittest.TestCase):
    """The first gate that fails decides; each case trips two gates and expects the earlier one."""

    def test_the_earlier_gate_wins(self) -> None:
        outside = {"after": ["docs/a.md", "src/x"]}
        missing_validation = {"validations_passed": []}
        cases = (
            (
                "blocked beats a missing validation and an out-of-scope path",
                {**outside, "handoff_document": handoff(status="blocked", **missing_validation)},
                failed(
                    "Slice reported blocked status.",
                    queue_status="blocked",
                    decision="stop_blocked",
                    required_validation_failures=["git diff --check"],
                    dirty_paths_outside_scope=["src/x"],
                ),
            ),
            (
                "failed beats a missing validation and an out-of-scope path",
                {**outside, "handoff_document": handoff(status="failed", **missing_validation)},
                failed(
                    "Slice reported failed status.",
                    required_validation_failures=["git diff --check"],
                    dirty_paths_outside_scope=["src/x"],
                ),
            ),
            (
                "a missing validation beats an out-of-scope path",
                {**outside, "handoff_document": handoff(**missing_validation)},
                failed(VALIDATION_REASON, required_validation_failures=["git diff --check"], dirty_paths_outside_scope=["src/x"]),
            ),
            (
                "a validation the handoff lists as failed",
                {"handoff_document": handoff(validations_failed=["git diff --check"])},
                failed(VALIDATION_REASON, required_validation_failures=["git diff --check"]),
            ),
            (
                "scope beats the file budget",
                {"after": ["docs/a.md", "docs/b.md", "docs/c.md", "src/x"]},
                failed(SCOPE_REASON, dirty_paths_outside_scope=["src/x"], changed_file_count=3),
            ),
            (
                "the file budget beats a missing manual proof",
                {
                    "current": slice_record(manual_proof=["screenshot"], required_proof_level="screenshot-verified"),
                    "after": ["docs/a.md", "docs/b.md", "docs/c.md"],
                },
                failed("Slice exceeded max_files_changed (3 > 2).", changed_file_count=3),
            ),
            (
                "a blocked handoff's recommendation is reported stripped",
                {"handoff_document": handoff(status="blocked", recommended_next_slice=" next ")},
                failed(
                    "Slice reported blocked status.",
                    queue_status="blocked",
                    decision="stop_blocked",
                    recommended_next_slice="next",
                ),
            ),
        )
        for name, keywords, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, evaluate(**keywords))

    def test_stopping_gates_report_found_paths_or_else_reported_ones(self) -> None:
        # Current behaviour: blocked, failed and validation stops list the paths the supervisor found,
        # and the runner's own report only when it found none. The scope gate combines both.
        reported = handoff(dirty_paths_outside_scope=["src/reported"])
        for status, extra in (("blocked", {"queue_status": "blocked", "decision": "stop_blocked"}), ("failed", {})):
            reason = f"Slice reported {status} status."
            with self.subTest(status=status, found="both"):
                found_and_reported = evaluate(after=["docs/a.md", "src/found"], handoff_document={**reported, "status": status})
                self.assertEqual(failed(reason, dirty_paths_outside_scope=["src/found"], **extra), found_and_reported)
            with self.subTest(status=status, found="reported only"):
                reported_only = evaluate(handoff_document={**reported, "status": status})
                self.assertEqual(failed(reason, dirty_paths_outside_scope=["src/reported"], **extra), reported_only)


class ValidationReplayTest(unittest.TestCase):
    def test_a_failed_supervisor_replay_fails_a_validation_the_handoff_passed(self) -> None:
        passed = policy.ValidationReplayResult("git diff --check", True, 0, "Supervisor replay passed.")
        failing = policy.ValidationReplayResult("make architecture", False, 2, "Supervisor replay exited with code 2.")
        both = ["git diff --check", "make architecture"]
        current = slice_record(required_validations=both)
        cases = (
            ("a passing replay is carried on success", current, [passed], success(supervisor_validation_replays=[passed])),
            (
                "a failing replay fails the slice",
                current,
                [passed, failing],
                failed(
                    VALIDATION_REASON,
                    required_validation_failures=["make architecture"],
                    supervisor_validation_replays=[passed, failing],
                ),
            ),
            (
                "a failing replay counts even for a command the slice does not require",
                slice_record(),
                [failing],
                failed(
                    VALIDATION_REASON,
                    required_validation_failures=["make architecture"],
                    supervisor_validation_replays=[failing],
                ),
            ),
            ("no replays", current, None, success()),
        )
        for name, record, replays, expected in cases:
            with self.subTest(name):
                keywords = {} if replays is None else {"validation_replays": replays}
                decision = evaluate(current=record, handoff_document=handoff(validations_passed=both), **keywords)
                self.assertEqual(expected, decision)

    def test_validation_failures_are_sorted_and_unique(self) -> None:
        failing = policy.ValidationReplayResult("git diff --check", False, 1, "Supervisor replay exited with code 1.")
        record = slice_record(required_validations=["make z", "git diff --check", "make z"])
        decision = evaluate(current=record, validation_replays=[failing])
        self.assertEqual(
            failed(
                VALIDATION_REASON,
                required_validation_failures=["git diff --check", "make z"],
                supervisor_validation_replays=[failing],
            ),
            decision,
        )


class ProofGateTest(unittest.TestCase):
    """The proof gate, last before the queue rules, decides from observed proof files."""

    def proof_case(self, **handoff_overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        record = slice_record(manual_proof=["screenshot"], required_proof_level="flow-verified")
        proof = {
            "proof_type": "screenshot",
            "path": PROOF_PATH,
            "commit_sha": HEAD,
            "slice_id": "cur",
            "sha256": hashlib.sha256(PROOF_BYTES).hexdigest(),
        }
        document = handoff(proof_level="flow-verified", verified_commit_sha=HEAD, provided_proofs=[proof])
        document.update(handoff_overrides)
        return record, document

    def test_the_proof_gate_decides_from_the_facts_it_is_given(self) -> None:
        # The facts are used as given: the current directory has no such proof file.
        self.assertFalse(Path(PROOF_PATH).exists())
        good = {PROOF_PATH: policy.ProofFile(sha256=hashlib.sha256(PROOF_BYTES).hexdigest())}
        cases = (
            ("a matching file", {}, good, success()),
            (
                "a changed file",
                {},
                {PROOF_PATH: policy.ProofFile(sha256="0" * 64)},
                failed(PROOF_REASON, missing_manual_proofs=["screenshot (sha256 mismatch)"]),
            ),
            ("no fact for the path", {}, {}, failed(PROOF_REASON, missing_manual_proofs=["screenshot (missing file)"])),
            ("a proof level short", {"proof_level": "build-tested"}, good, failed(PROOF_REASON, missing_proof_level=True)),
        )
        for name, overrides, facts, expected in cases:
            with self.subTest(name):
                record, document = self.proof_case(**overrides)
                decision = evaluate(current=record, handoff_document=document, post_run_commit_sha=HEAD, proof_facts=facts)
                self.assertEqual(expected, decision)

    def test_without_proof_facts_the_current_directory_is_the_proof_root(self) -> None:
        cases = (
            ("the recorded file", PROOF_BYTES, success()),
            ("a changed file", b"changed", failed(PROOF_REASON, missing_manual_proofs=["screenshot (sha256 mismatch)"])),
        )
        for name, content, expected in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temp_dir:
                proof_file = Path(temp_dir) / PROOF_PATH
                proof_file.parent.mkdir(parents=True)
                proof_file.write_bytes(content)
                record, document = self.proof_case()
                with contextlib.chdir(temp_dir):
                    decision = evaluate(current=record, handoff_document=document, post_run_commit_sha=HEAD)
                self.assertEqual(expected, decision)


class StopRulesThroughCompletionTest(unittest.TestCase):
    """A run that passes every gate still stops when the queue says it should."""

    def test_the_queue_rules_decide_after_every_gate_passes(self) -> None:
        cases = (
            (
                "a recommended slice whose dependency is another unfinished slice",
                [slice_record("other", priority=5), slice_record("next", priority=0, depends_on=["other"])],
                {"handoff_document": handoff(recommended_next_slice="next")},
                success(
                    queue_status="done",
                    decision="stop_for_review",
                    should_continue=False,
                    stop_reason="The handoff recommended slice 'next', but its dependencies are not yet satisfied.",
                    next_slice_id=None,
                    recommended_next_slice="next",
                ),
            ),
            (
                "a recommended slice that depends on the current one",
                [slice_record("next", depends_on=["cur"])],
                {"handoff_document": handoff(recommended_next_slice="next")},
                success(
                    stop_reason="Continuation allowed into the recommended eligible next slice.",
                    recommended_next_slice="next",
                ),
            ),
            (
                "the run limit",
                None,
                {"completed_autonomous_runs": 3},
                success(
                    decision="stop_for_review",
                    should_continue=False,
                    stop_reason="Consecutive autonomous run limit reached; stop for human review.",
                    next_slice_id=None,
                ),
            ),
            (
                "an empty queue after this slice",
                [],
                {},
                success(
                    decision="stop",
                    should_continue=False,
                    stop_reason="No eligible queued next slice remains after the current slice completed.",
                    next_slice_id=None,
                ),
            ),
        )
        for name, others, keywords, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, evaluate(others=others, **keywords))


if __name__ == "__main__":
    unittest.main()
