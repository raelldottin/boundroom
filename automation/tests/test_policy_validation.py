"""Which validations the supervisor trusts, which it replays, and how a replay is judged.

The replay runner is the one out-of-process dependency here, so the replay tests pass a small
function that answers from the argv it is given instead of starting a process. One test per
runner path still runs a real process, so the default runner stays covered.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automation.supervisor import policy

REPLAYED = "The supervisor replays this exact allowlisted command before continuation."
REPORT_ONLY = "This command is currently run-reported only and not replayed by the supervisor."
DOMAIN_TESTS = "Domain-targeted tests are currently run-reported and not replayed by the supervisor."
MANUAL = "Manual validation steps are never supervisor-owned and should not be replayed."
UI_LAUNCH = "UI-launch steps are never supervisor-owned and should not be replayed."


class RequiredValidationFailuresTest(unittest.TestCase):
    def test_a_required_command_fails_unless_it_passed_and_did_not_fail(self) -> None:
        cases = (
            ("all passed", ["a", "b"], ["a", "b"], [], []),
            ("not reported", ["a", "b"], ["a"], [], ["b"]),
            ("reported both passed and failed", ["a"], ["a"], ["a"], ["a"]),
            ("failed only", ["a"], [], ["a"], ["a"]),
            ("required order and duplicates are kept", ["b", "a", "b"], [], [], ["b", "a", "b"]),
            ("matching is exact", [" a"], ["a"], [], [" a"]),
            ("nothing required", [], [], ["x"], []),
        )
        for name, required, passed, failed, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.required_validation_failures(required, passed, failed))


class ValidationOwnershipTest(unittest.TestCase):
    def test_each_command_is_classified_by_the_allowlist_and_its_prefix(self) -> None:
        cases = (
            ("make architecture", "supervisor_replayable", REPLAYED),
            ("git diff --check", "supervisor_replayable", REPLAYED),
            ("  git diff --check  ", "supervisor_replayable", REPLAYED),
            ("make architecture --x", "run_report_only", REPORT_ONLY),
            ("manual check the screen", "never_supervisor_owned", MANUAL),
            ("manual", "run_report_only", REPORT_ONLY),
            ("open App.xcodeproj", "never_supervisor_owned", UI_LAUNCH),
            ("make test-domain today", "run_report_only", DOMAIN_TESTS),
            ("make test-domain", "run_report_only", REPORT_ONLY),
            ("pytest -q", "run_report_only", REPORT_ONLY),
            ("", "run_report_only", REPORT_ONLY),
        )
        for command, tier, reason in cases:
            with self.subTest(command=command):
                expected = policy.ValidationOwnership(command=command.strip(), tier=tier, reason=reason)
                self.assertEqual(expected, policy.classify_validation_ownership(command))

    def test_a_list_is_classified_once_per_stripped_command_in_first_seen_order(self) -> None:
        self.assertEqual(
            [
                policy.ValidationOwnership("pytest", "run_report_only", REPORT_ONLY),
                policy.ValidationOwnership("make architecture", "supervisor_replayable", REPLAYED),
            ],
            policy.classify_validation_ownerships(["pytest", "", "  ", "make architecture", " pytest "]),
        )

    def test_only_exact_allowlisted_commands_are_replayable_once_each_in_required_order(self) -> None:
        required = ["pytest", "git diff --check", " make architecture", "make architecture", "git diff --check"]
        self.assertEqual(["git diff --check", "make architecture"], policy.supervisor_replayable_validations(required))


class ReplayArgvTest(unittest.TestCase):
    def test_the_argv_for_each_allowlisted_command(self) -> None:
        cases = (
            ("make architecture", "", ["make", "architecture"]),
            ("make architecture", "abc", ["make", "architecture"]),
            ("git diff --check", "", ["git", "diff", "--check"]),
            ("git diff --check", "abc", ["git", "diff", "--check", "abc"]),
        )
        for command, base_sha, expected in cases:
            with self.subTest(command=command, base_sha=base_sha):
                self.assertEqual(expected, policy.replay_argv(command, base_sha))

    def test_an_unknown_command_raises(self) -> None:
        with self.assertRaises(KeyError) as raised:
            policy.replay_argv("rm -rf /")
        self.assertEqual("'rm -rf /'", str(raised.exception))

    def test_the_argv_is_a_copy(self) -> None:
        policy.replay_argv("git diff --check", "abc").append("--oops")
        self.assertEqual(["git", "diff", "--check"], policy.replay_argv("git diff --check"))


def exits_with(codes: dict[str, int]):
    """A replay runner that answers from the argv: the exit code for the command its argv starts with."""

    def run(repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, codes[" ".join(argv[:3])])

    return run


class ReplayValidationCommandsTest(unittest.TestCase):
    BOTH = ["git diff --check", "make architecture"]

    def replay(self, runner, required=None, passed=None, base_sha: str = ""):
        required = self.BOTH if required is None else required
        passed = self.BOTH if passed is None else passed
        return policy.replay_validation_commands(Path("/repo"), required, passed, runner, base_sha)

    def test_each_replayable_command_the_handoff_passed_is_replayed_and_judged(self) -> None:
        cases = (
            (
                "both pass",
                {"git diff --check": 0, "make architecture": 0},
                [
                    policy.ValidationReplayResult("git diff --check", True, 0, "Supervisor replay passed."),
                    policy.ValidationReplayResult("make architecture", True, 0, "Supervisor replay passed."),
                ],
            ),
            (
                "non-zero exits fail",
                {"git diff --check": 1, "make architecture": 2},
                [
                    policy.ValidationReplayResult("git diff --check", False, 1, "Supervisor replay exited with code 1."),
                    policy.ValidationReplayResult("make architecture", False, 2, "Supervisor replay exited with code 2."),
                ],
            ),
            (
                "a signal exit fails",
                {"git diff --check": -9, "make architecture": 0},
                [
                    policy.ValidationReplayResult("git diff --check", False, -9, "Supervisor replay exited with code -9."),
                    policy.ValidationReplayResult("make architecture", True, 0, "Supervisor replay passed."),
                ],
            ),
        )
        for name, codes, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, self.replay(exits_with(codes)))

    def test_commands_the_handoff_did_not_pass_or_that_are_not_replayable_are_skipped(self) -> None:
        runner = exits_with({"git diff --check": 0, "make architecture": 0})
        self.assertEqual(
            [policy.ValidationReplayResult("make architecture", True, 0, "Supervisor replay passed.")],
            self.replay(runner, required=["pytest", *self.BOTH], passed=["pytest", "make architecture"]),
        )
        self.assertEqual([], self.replay(runner, required=["pytest"], passed=["pytest"]))
        self.assertEqual([], self.replay(runner, passed=[]))

    def test_the_runner_gets_the_repository_and_the_base_commit(self) -> None:
        calls: list[tuple[Path, list[str]]] = []

        def recording(repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append((repo_root, argv))
            return subprocess.CompletedProcess(argv, 0)

        self.replay(recording, base_sha="abc")
        self.assertEqual([(Path("/repo"), ["git", "diff", "--check", "abc"]), (Path("/repo"), ["make", "architecture"])], calls)

    def test_a_command_that_cannot_start_fails_and_the_next_still_runs(self) -> None:
        def missing_git(repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[0] == "git":
                raise FileNotFoundError("no git here")
            return subprocess.CompletedProcess(argv, 0)

        self.assertEqual(
            [
                policy.ValidationReplayResult(
                    "git diff --check", False, None, "Supervisor replay could not execute: no git here"
                ),
                policy.ValidationReplayResult("make architecture", True, 0, "Supervisor replay passed."),
            ],
            self.replay(missing_git),
        )

    def test_an_error_that_is_not_an_os_error_propagates(self) -> None:
        def broken(repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
            raise RuntimeError("bug in the runner")

        with self.assertRaisesRegex(RuntimeError, "bug in the runner"):
            self.replay(broken)

    def test_without_a_runner_the_command_runs_in_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            environment = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            with mock.patch.dict(os.environ, environment):
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                (repo / "f.txt").write_text("clean\n", encoding="utf-8")
                passed = policy.replay_validation_commands(repo, ["git diff --check"], ["git diff --check"])
                subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
                (repo / "f.txt").write_text("trailing \n", encoding="utf-8")
                failed = policy.replay_validation_commands(repo, ["git diff --check"], ["git diff --check"])
        self.assertEqual([policy.ValidationReplayResult("git diff --check", True, 0, "Supervisor replay passed.")], passed)
        self.assertEqual(
            [policy.ValidationReplayResult("git diff --check", False, 2, "Supervisor replay exited with code 2.")], failed
        )


class DefaultValidationReplayRunnerTest(unittest.TestCase):
    def test_runs_the_argv_in_the_repository_and_captures_text_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir).resolve()
            script = "import os, sys; print(os.getcwd()); print('oops', file=sys.stderr); sys.exit(3)"
            result = policy.default_validation_replay_runner(repo, [sys.executable, "-c", script])
        self.assertEqual((3, f"{repo}\n", "oops\n"), (result.returncode, result.stdout, result.stderr))


class ValidationReplayFailuresTest(unittest.TestCase):
    def test_the_failed_commands_sorted_and_unique(self) -> None:
        ok = policy.ValidationReplayResult("make architecture", True, 0, "Supervisor replay passed.")
        bad_b = policy.ValidationReplayResult("make b", False, 1, "")
        bad_a = policy.ValidationReplayResult("make a", False, None, "")
        self.assertEqual(["make a", "make b"], policy.validation_replay_failures([bad_b, ok, bad_a, bad_b]))
        self.assertEqual([], policy.validation_replay_failures([ok]))
        self.assertEqual([], policy.validation_replay_failures([]))


if __name__ == "__main__":
    unittest.main()
