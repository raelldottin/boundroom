"""A lane's workspace (SPEC-014 RA-LANE-002): its root, its worktree, provisioning, the clean end, retention.

The rules about ids, SHAs and paths are pure and tested by their output. Git and the file system
are the module's own tools, so the rest runs real Git in temporary repositories, with the user's
global and system Git configuration switched off and ``XDG_STATE_HOME`` in a temporary directory,
both passed through the ``environ`` parameter.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from automation.supervisor import lane_workspace as lw
from automation.supervisor import policy

SHA = "0123456789abcdef0123456789abcdef01234567"

# Inside a Git hook (a consumer's pre-push hook runs the suite), Git exports GIT_DIR, GIT_INDEX_FILE
# and friends. They would point the fixture's git calls at the outer repository, so the environment
# given to the module and the fixture leaves them out.
_NO_GIT_ENV = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
# policy.replay_validation_commands runs with the process's own environment.
needs_own_git = unittest.skipIf(
    any(name in os.environ for name in ("GIT_DIR", "GIT_WORK_TREE")), "GIT_DIR or GIT_WORK_TREE redirects every git call"
)


class ComponentTest(unittest.TestCase):
    def test_accepts_slice_and_attempt_ids_the_queue_uses(self) -> None:
        for value in ("RA-LANE-002", "today-nonfocus-add-to-focus", "a1.b_c", "7", "x" * 100):
            with self.subTest(value=value):
                self.assertEqual(lw.check_component(value, "slice id"), value)

    def test_refuses_what_could_leave_the_root_or_break_a_ref(self) -> None:
        for value in ("", ".", "..", "../x", "a/b", "a\\b", ".hidden", "-x", "a..b", "x.lock", "x.", "a b", "a@{b", "a\nb"):
            with self.subTest(value=value), self.assertRaises(lw.LaneRefused):
                lw.check_component(value, "slice id")

    def test_refuses_an_id_longer_than_100_characters(self) -> None:
        with self.assertRaises(lw.LaneRefused):
            lw.check_component("x" * 101, "attempt id")

    def test_lane_branch_names_slice_then_attempt(self) -> None:
        self.assertEqual(lw.lane_branch("RA-LANE-002", "a1"), "boundroom/lane/RA-LANE-002/a1")

    def test_lane_branch_refuses_an_unsafe_attempt(self) -> None:
        with self.assertRaises(lw.LaneRefused):
            lw.lane_branch("RA-LANE-002", "../a1")


class FullShaTest(unittest.TestCase):
    def test_accepts_full_sha1_and_sha256(self) -> None:
        for value in (SHA, "a" * 64):
            with self.subTest(value=value):
                self.assertEqual(lw.check_full_sha(value), value)

    def test_refuses_a_branch_name_a_short_sha_or_upper_case(self) -> None:
        for value in ("main", SHA[:12], SHA.upper(), SHA + "0", "HEAD"):
            with self.subTest(value=value), self.assertRaises(lw.LaneRefused):
                lw.check_full_sha(value)


class StateHomeTest(unittest.TestCase):
    def test_xdg_state_home_wins(self) -> None:
        self.assertEqual(lw.state_home({"XDG_STATE_HOME": "/s", "HOME": "/h"}), Path("/s"))

    def test_falls_back_to_home_local_state(self) -> None:
        self.assertEqual(lw.state_home({"HOME": "/h"}), Path("/h/.local/state"))

    def test_a_relative_xdg_state_home_is_ignored(self) -> None:
        self.assertEqual(lw.state_home({"XDG_STATE_HOME": "rel", "HOME": "/h"}), Path("/h/.local/state"))

    def test_refuses_without_an_absolute_home(self) -> None:
        for environ in ({}, {"HOME": "rel"}):
            with self.subTest(environ=environ), self.assertRaises(lw.LaneRefused):
                lw.state_home(environ)


class RepositoryIdTest(unittest.TestCase):
    def test_is_stable_short_hex_and_differs_per_directory(self) -> None:
        first = lw.repository_id(Path("/r/a/.git"))
        self.assertEqual(first, lw.repository_id(Path("/r/a/.git")))
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertNotEqual(first, lw.repository_id(Path("/r/b/.git")))


class RootRefusalTest(unittest.TestCase):
    worktrees = [Path("/repo"), Path("/elsewhere/lane")]
    common = Path("/git/common")

    def refusal(self, given: str, canonical: str = "") -> str | None:
        return lw.root_refusal(Path(given), Path(canonical or given), self.worktrees, self.common)

    def test_a_root_outside_the_repository_is_accepted(self) -> None:
        self.assertIsNone(self.refusal("/state/boundroom/lanes/id"))

    def test_a_sibling_whose_name_starts_like_a_worktree_is_outside_it(self) -> None:
        self.assertIsNone(self.refusal("/repo2/lanes"))

    def test_refuses_a_root_inside_or_equal_to_any_worktree(self) -> None:
        for given in ("/repo", "/repo/state/lanes", "/elsewhere/lane/x"):
            with self.subTest(given=given):
                self.assertIn("inside", self.refusal(given) or "")

    def test_refuses_a_root_inside_the_common_git_directory(self) -> None:
        self.assertIn("inside /git/common", self.refusal("/git/common/lanes") or "")

    def test_refuses_a_root_reached_through_a_symlink(self) -> None:
        self.assertIn("symlink", self.refusal("/link/lanes", "/state/lanes") or "")


class PurePartsTest(unittest.TestCase):
    def test_without_handoff_drops_only_the_normalized_handoff(self) -> None:
        dirty = ["automation/handoffs/s.json", "src/a.py", "automation/handoffs/other.json"]
        self.assertEqual(
            lw.without_handoff(dirty, "./automation/handoffs/s.json"), ["src/a.py", "automation/handoffs/other.json"]
        )

    def test_parse_worktree_list_reads_path_branch_and_head(self) -> None:
        porcelain = (
            "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /lanes/a1\nHEAD bbb\nbranch refs/heads/boundroom/lane/s/a1\n\n"
            "worktree /tmp/x\nHEAD ccc\ndetached\n"
        )
        self.assertEqual(
            lw.parse_worktree_list(porcelain),
            [
                (Path("/repo"), "refs/heads/main", "aaa"),
                (Path("/lanes/a1"), "refs/heads/boundroom/lane/s/a1", "bbb"),
                (Path("/tmp/x"), "", "ccc"),
            ],
        )


class SetupFailureTest(unittest.TestCase):
    def test_keeps_the_end_of_long_output(self) -> None:
        failure = lw.LaneSetupFailed("make", lw.SetupFailureKind.EXITED, 2, "a" * 10 + "b" * lw.SETUP_OUTPUT_TAIL_CHARS)
        self.assertEqual(failure.output, "b" * lw.SETUP_OUTPUT_TAIL_CHARS)
        self.assertEqual(str(failure), "lane setup command 'make' exited with code 2")


class LaneRepoTestCase(unittest.TestCase):
    """A real repository and a real state directory, both canonical, in temporary directories."""

    def setUp(self) -> None:
        self.repo = self.temp_dir()
        self.state = self.temp_dir()
        self.environ = {
            **_NO_GIT_ENV,
            "XDG_STATE_HOME": str(self.state),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")
        self.write(".gitignore", ".lane-env/\n")
        # The example queue's replayed validations are 'make architecture' and 'git diff --check'.
        # Here 'make architecture' needs what only provisioning creates, as a virtual environment would be.
        self.write("Makefile", "architecture:\n\ttest -f .lane-env/ready\n")
        self.write("src/app.py", "print('hi')\n")
        self.base = self.commit("base")

    def temp_dir(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name).resolve()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        command = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
        result = subprocess.run(command, cwd=cwd or self.repo, env=self.environ, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def write(self, name: str, text: str, root: Path | None = None) -> None:
        path = (root or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str, cwd: Path | None = None) -> str:
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", message, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def create(self, slice_id: str = "RA-LANE-002", attempt_id: str = "a1") -> lw.LaneWorkspace:
        return lw.create(self.repo, slice_id, attempt_id, self.base, self.environ)

    def expected_root(self) -> Path:
        common = (self.repo / ".git").resolve()
        return self.state / "boundroom" / "lanes" / lw.repository_id(common)

    def branches(self) -> list[str]:
        return self.git("branch", "--format=%(refname:short)").splitlines()


class CreateTest(LaneRepoTestCase):
    def test_makes_a_worktree_on_the_lane_branch_at_the_base(self) -> None:
        lane = self.create()
        self.assertEqual(
            lane,
            lw.LaneWorkspace(
                slice_id="RA-LANE-002",
                attempt_id="a1",
                base_sha=self.base,
                path=self.expected_root() / "a1",
                branch="boundroom/lane/RA-LANE-002/a1",
            ),
        )
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=lane.path), self.base)
        self.assertEqual(self.git("branch", "--show-current", cwd=lane.path), lane.branch)
        self.assertEqual((lane.path / "src/app.py").read_text(encoding="utf-8"), "print('hi')\n")

    def test_every_worktree_of_the_repository_has_the_same_lane_root(self) -> None:
        lane = self.create()
        self.assertEqual(lw.lane_root(lane.path, self.environ), lw.lane_root(self.repo, self.environ))

    def test_lists_the_lane_as_live(self) -> None:
        lane = self.create()
        self.assertEqual(lw.live_lanes(self.repo, self.environ), [lw.LaneWorktree(lane.path, lane.branch, self.base)])

    def test_refuses_a_second_lane_while_one_exists(self) -> None:
        first = self.create()
        with self.assertRaisesRegex(lw.LaneRefused, "already exists at " + str(first.path)):
            self.create("RA-LANE-003", "a2")
        self.assertNotIn("boundroom/lane/RA-LANE-003/a2", self.branches())
        self.assertFalse((self.expected_root() / "a2").exists())

    def test_refuses_a_second_lane_kept_outside_the_root_on_a_lane_branch(self) -> None:
        elsewhere = self.temp_dir() / "lane"
        self.git("worktree", "add", "-q", "-b", "boundroom/lane/old/a0", str(elsewhere), self.base)
        with self.assertRaisesRegex(lw.LaneRefused, "already exists"):
            self.create()

    def test_a_new_lane_can_start_once_the_last_is_removed(self) -> None:
        first = self.create()
        lw.retain_or_remove(self.repo, first, self.environ, done_and_integrated=True, recorded_head=self.base)
        self.assertEqual(self.create("RA-LANE-003", "a2").path, self.expected_root() / "a2")

    def test_the_repositorys_own_worktree_on_a_lane_branch_is_not_a_lane(self) -> None:
        self.git("checkout", "-q", "-b", "boundroom/lane/inspect/a0")
        self.assertEqual(lw.live_lanes(self.repo, self.environ), [])
        self.assertEqual(self.create().path, self.expected_root() / "a1")

    def test_refuses_an_attempt_whose_directory_already_exists(self) -> None:
        (self.expected_root() / "a1").mkdir(parents=True)
        with self.assertRaisesRegex(lw.LaneRefused, "already exists; each attempt"):
            self.create()
        self.assertEqual(self.branches(), ["main"])

    def test_creates_the_state_directories_private(self) -> None:
        self.create()
        for directory in (self.state / "boundroom", self.state / "boundroom" / "lanes", self.expected_root()):
            with self.subTest(directory=directory):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_refuses_an_unsafe_id_before_touching_git(self) -> None:
        with self.assertRaises(lw.LaneRefused):
            self.create(attempt_id="../escape")
        self.assertEqual(self.branches(), ["main"])
        self.assertFalse((self.state / "boundroom").exists())

    def test_refuses_a_branch_name_as_the_base(self) -> None:
        with self.assertRaises(lw.LaneRefused):
            lw.create(self.repo, "s", "a1", "main", self.environ)
        self.assertEqual(self.branches(), ["main"])

    def test_refuses_a_base_that_is_not_a_commit_here(self) -> None:
        with self.assertRaisesRegex(lw.LaneWorkspaceError, "cat-file"):
            lw.create(self.repo, "s", "a1", SHA, self.environ)
        self.assertFalse((self.state / "boundroom").exists())

    def test_refuses_a_lane_root_inside_the_repository(self) -> None:
        environ = {**self.environ, "XDG_STATE_HOME": str(self.repo / "state")}
        with self.assertRaisesRegex(lw.LaneRefused, "inside " + str(self.repo)):
            lw.create(self.repo, "s", "a1", self.base, environ)
        self.assertFalse((self.repo / "state").exists())
        self.assertEqual(self.branches(), ["main"])

    def test_refuses_a_lane_root_reached_through_a_symlink(self) -> None:
        link = self.temp_dir() / "state-link"
        link.symlink_to(self.state, target_is_directory=True)
        environ = {**self.environ, "XDG_STATE_HOME": str(link)}
        with self.assertRaisesRegex(lw.LaneRefused, "symlink"):
            lw.create(self.repo, "s", "a1", self.base, environ)
        self.assertFalse((self.state / "boundroom").exists())


class ProvisionTest(LaneRepoTestCase):
    VALIDATIONS = ["make architecture", "git diff --check"]

    def replay(self, lane: lw.LaneWorkspace) -> list[policy.ValidationReplayResult]:
        return policy.replay_validation_commands(lane.path, self.VALIDATIONS, self.VALIDATIONS, base_sha=lane.base_sha)

    @needs_own_git
    def test_the_example_validations_pass_in_a_fresh_lane_after_provisioning(self) -> None:
        lane = self.create()
        self.assertEqual(policy.validation_replay_failures(self.replay(lane)), ["make architecture"])

        lw.provision(lane, ["mkdir -p .lane-env", "touch .lane-env/ready"], self.environ)

        self.assertEqual([(r.command, r.success) for r in self.replay(lane)], [(c, True) for c in self.VALIDATIONS])
        self.assertEqual(lw.lane_uncommitted_changes(lane, "automation/handoffs/RA-LANE-002.json", self.environ), [])
        self.assertFalse((self.repo / ".lane-env").exists())

    def test_setup_commands_get_the_given_environment(self) -> None:
        lane = self.create()
        lw.provision(lane, ['printf %s "$LANE_SETUP_PROBE" > .lane-env-probe'], {**self.environ, "LANE_SETUP_PROBE": "given"})
        self.assertEqual((lane.path / ".lane-env-probe").read_text(encoding="utf-8"), "given")

    def test_a_failing_command_is_typed_and_stops_the_rest(self) -> None:
        lane = self.create()
        with self.assertRaises(lw.LaneSetupFailed) as caught:
            lw.provision(lane, ["echo boom; exit 3", "touch later"], self.environ)
        self.assertEqual(
            (caught.exception.command, caught.exception.kind, caught.exception.exit_code),
            ("echo boom; exit 3", lw.SetupFailureKind.EXITED, 3),
        )
        self.assertIn("boom", caught.exception.output)
        self.assertFalse((lane.path / "later").exists())

    def test_a_command_past_its_timeout_is_stopped_and_typed(self) -> None:
        lane = self.create()
        with self.assertRaises(lw.LaneSetupFailed) as caught:
            lw.provision(lane, ["echo started; sleep 10; touch finished"], self.environ, timeout_seconds=1)
        self.assertEqual((caught.exception.kind, caught.exception.exit_code), (lw.SetupFailureKind.TIMED_OUT, None))
        self.assertIn("started", caught.exception.output)
        self.assertFalse((lane.path / "finished").exists())

    def test_a_command_ends_when_its_shell_exits_even_if_it_leaves_a_background_child(self) -> None:
        lane = self.create()
        lw.provision(lane, ["sleep 3 & echo ok > .lane-env-done"], self.environ, timeout_seconds=1)
        self.assertEqual((lane.path / ".lane-env-done").read_text(encoding="utf-8"), "ok\n")

    def test_a_command_that_cannot_start_is_typed(self) -> None:
        def cannot_start(worktree: Path, command: str, environ: object, timeout: float) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(2, "No such file or directory", "sh")

        lane = self.create()
        with self.assertRaises(lw.LaneSetupFailed) as caught:
            lw.provision(lane, ["uv sync --frozen"], self.environ, runner=cannot_start)
        self.assertEqual((caught.exception.kind, caught.exception.exit_code), (lw.SetupFailureKind.NOT_STARTED, None))

    def test_the_runner_seam_gets_the_worktree_command_environment_and_timeout(self) -> None:
        calls: list[tuple[Path, str, object, float]] = []

        def record(worktree: Path, command: str, environ: object, timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append((worktree, command, environ, timeout))
            return subprocess.CompletedProcess(command, 0, "", None)

        lane = self.create()
        lw.provision(lane, ["one", "two"], self.environ, runner=record, timeout_seconds=7)
        self.assertEqual(calls, [(lane.path, "one", self.environ, 7), (lane.path, "two", self.environ, 7)])


class CleanEndTest(LaneRepoTestCase):
    def test_reports_uncommitted_changes_except_the_handoff(self) -> None:
        lane = self.create()
        self.write("src/app.py", "changed\n", lane.path)
        self.write("staged.txt", "s\n", lane.path)
        self.git("add", "staged.txt", cwd=lane.path)
        self.write("new/untracked.txt", "u\n", lane.path)
        self.write("automation/handoffs/RA-LANE-002.json", "{}\n", lane.path)
        self.write(".lane-env/ignored", "i\n", lane.path)
        self.assertEqual(
            lw.lane_uncommitted_changes(lane, "automation/handoffs/RA-LANE-002.json", self.environ),
            ["new/untracked.txt", "src/app.py", "staged.txt"],
        )

    def test_a_lane_that_committed_its_work_ends_clean_at_its_new_head(self) -> None:
        lane = self.create()
        self.write("src/app.py", "changed\n", lane.path)
        head = self.commit("lane work", cwd=lane.path)
        self.assertEqual(lw.lane_uncommitted_changes(lane, "automation/handoffs/RA-LANE-002.json", self.environ), [])
        self.assertEqual(lw.lane_head(lane, self.environ), head)
        self.assertNotEqual(head, self.base)


class MissingWorktreeTest(LaneRepoTestCase):
    def test_a_missing_worktree_is_reported_as_missing_not_as_missing_git(self) -> None:
        lane = replace(self.create(), path=self.state / "gone")
        with self.assertRaisesRegex(lw.LaneWorkspaceError, "is not a directory"):
            lw.lane_head(lane, self.environ)


class RetentionTest(LaneRepoTestCase):
    def integrated_lane(self) -> lw.LaneWorkspace:
        lane = self.create()
        lw.provision(lane, ["mkdir -p .lane-env", "touch .lane-env/ready"], self.environ)
        self.write("src/app.py", "changed\n", lane.path)
        self.head = self.commit("lane work", cwd=lane.path)
        self.git("merge", "-q", "--ff-only", self.head)
        return lane

    def remove(self, lane: lw.LaneWorkspace, recorded_head: str = "") -> lw.LaneEnd:
        return lw.retain_or_remove(
            self.repo, lane, self.environ, done_and_integrated=True, recorded_head=recorded_head or self.head
        )

    def test_keeps_the_worktree_and_branch_unless_done_and_integrated(self) -> None:
        lane = self.create()
        end = lw.retain_or_remove(self.repo, lane, self.environ, done_and_integrated=False)
        self.assertEqual(end, lw.LaneEnd(removed=False, path=lane.path, branch=lane.branch))
        self.assertTrue(lane.path.is_dir())
        self.assertIn(lane.branch, self.branches())

    def test_removes_the_worktree_and_branch_when_done_and_integrated(self) -> None:
        lane = self.integrated_lane()
        end = self.remove(lane)
        self.assertEqual(end, lw.LaneEnd(removed=True, path=lane.path, branch=lane.branch))
        self.assertFalse(lane.path.exists())
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(lw.live_lanes(self.repo, self.environ), [])

    def test_refuses_to_remove_a_lane_whose_head_is_not_integrated(self) -> None:
        lane = self.create()
        self.write("src/app.py", "changed\n", lane.path)
        head = self.commit("lane work", cwd=lane.path)
        with self.assertRaisesRegex(lw.LaneRefused, "isn't integrated"):
            self.remove(lane, head)
        self.assertTrue(lane.path.is_dir())
        self.assertIn(lane.branch, self.branches())

    def test_refuses_to_remove_a_path_outside_the_lane_root(self) -> None:
        lane = self.integrated_lane()
        for path in (self.repo, self.expected_root(), self.expected_root() / "a1" / "src"):
            with self.subTest(path=path), self.assertRaisesRegex(lw.LaneRefused, "not a lane worktree"):
                self.remove(replace(lane, path=path))
        self.assertTrue(lane.path.is_dir())
        self.assertTrue((self.repo / "src/app.py").exists())

    def test_refuses_a_path_that_is_a_symlink_under_the_root(self) -> None:
        lane = self.integrated_lane()
        for name, target in (("a1-link", lane.path), ("repo-link", self.repo)):
            link = self.expected_root() / name
            link.symlink_to(target, target_is_directory=True)
            with self.subTest(name=name), self.assertRaisesRegex(lw.LaneRefused, "not a lane worktree"):
                self.remove(replace(lane, path=link))
        self.assertTrue(lane.path.is_dir())
        self.assertTrue((self.repo / "src/app.py").exists())

    def test_refuses_to_delete_a_branch_that_is_not_the_lanes(self) -> None:
        lane = self.integrated_lane()
        with self.assertRaisesRegex(lw.LaneRefused, "not this lane's branch"):
            self.remove(replace(lane, branch="main"))
        self.assertIn("main", self.branches())
        self.assertTrue(lane.path.is_dir())

    def test_refuses_a_directory_under_the_root_that_git_does_not_list(self) -> None:
        lane = self.integrated_lane()
        stray = self.expected_root() / "a9"
        stray.mkdir()
        record = replace(lane, path=stray, attempt_id="a9", branch="boundroom/lane/RA-LANE-002/a9")
        with self.assertRaisesRegex(lw.LaneRefused, "doesn't list it"):
            self.remove(record)
        self.assertTrue(stray.is_dir())

    def test_refuses_without_a_recorded_head(self) -> None:
        lane = self.integrated_lane()
        with self.assertRaisesRegex(lw.LaneRefused, "recorded head .* is not a full commit SHA"):
            lw.retain_or_remove(self.repo, lane, self.environ, done_and_integrated=True)
        self.assertTrue(lane.path.is_dir())

    def test_refuses_when_the_lane_branch_moved_after_its_head_was_recorded(self) -> None:
        lane = self.integrated_lane()
        self.git("reset", "-q", "--hard", self.base, cwd=lane.path)
        with self.assertRaisesRegex(lw.LaneRefused, "not the recorded head"):
            self.remove(lane)
        self.assertIn(lane.branch, self.branches())
        self.assertTrue(lane.path.is_dir())


if __name__ == "__main__":
    unittest.main()
