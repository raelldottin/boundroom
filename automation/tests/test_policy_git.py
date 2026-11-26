"""What the supervisor reads from Git: dirty paths, HEAD, the handoff commit and committed paths.

Git is the supervisor's own tool here, so these tests run real Git in tiny temporary
repositories, with the user's global and system Git configuration switched off.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automation.supervisor import policy


class GitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.repo = Path(temp_dir.name).resolve()
        environment = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, *args: str) -> str:
        command = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
        return subprocess.run(command, cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, name: str, text: str = "x\n") -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, *names: str, message: str = "c") -> str:
        for name in names:
            self.write(name, f"{name} {message}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def init(self) -> str:
        self.git("init", "-q")
        return self.commit("README.md", ".gitignore", message="base")


class GitDirtyPathsTest(GitTestCase):
    def test_unstaged_staged_and_untracked_paths_sorted_and_unique(self) -> None:
        self.init()
        self.write(".gitignore", "ignored.txt\n")
        self.git("add", ".gitignore")
        self.write("README.md", "changed\n")
        self.write("staged.txt")
        self.git("add", "staged.txt")
        self.write("staged.txt", "changed again\n")
        self.write("dir/untracked.txt")
        self.write("ignored.txt")
        self.assertEqual([".gitignore", "README.md", "dir/untracked.txt", "staged.txt"], policy.git_dirty_paths(self.repo))

    def test_a_clean_repository_has_none(self) -> None:
        self.init()
        self.assertEqual([], policy.git_dirty_paths(self.repo))

    def test_a_directory_that_is_not_a_repository(self) -> None:
        with self.assertRaises(policy.ConfigError) as raised:
            policy.git_dirty_paths(self.repo)
        self.assertEqual(
            f"not a Git repository: {self.repo}\n"
            "hint: run 'git init -b main' in this directory, commit the bootstrap, then re-run.",
            str(raised.exception),
        )

    def test_a_failing_git_command_in_a_repository(self) -> None:
        self.write(".git", "not a gitfile\n")
        with self.assertRaises(policy.ConfigError) as raised:
            policy.git_dirty_paths(self.repo)
        self.assertRegex(
            str(raised.exception),
            rf"\Agit command failed in {re.escape(str(self.repo))}: git diff --name-only --relative\nstderr: fatal: .+\Z",
        )

    def test_git_missing_from_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": ""}), self.assertRaises(policy.ConfigError) as raised:
            policy.git_dirty_paths(self.repo)
        self.assertEqual(
            "git executable not found on PATH: [Errno 2] No such file or directory: 'git'\n"
            "hint: install Git, or ensure git is on PATH for this process.",
            str(raised.exception),
        )


class GitHeadTest(GitTestCase):
    def test_the_commit_head_is_at(self) -> None:
        base = self.init()
        self.assertRegex(base, r"\A[0-9a-f]{40}\Z")
        self.assertEqual(base, policy.git_head(self.repo))

    def test_a_repository_without_commits(self) -> None:
        self.git("init", "-q")
        with self.assertRaises(policy.ConfigError) as raised:
            policy.git_head(self.repo)
        self.assertRegex(
            str(raised.exception),
            rf"\Acannot read HEAD in {re.escape(str(self.repo))}: .+\nhint: a slice needs a repository with at least one commit\.\Z",
        )


class GitHandoffCommitParentTest(GitTestCase):
    HANDOFF = "automation/handoffs/h.json"

    def test_the_parent_when_the_last_commit_changed_only_the_handoff(self) -> None:
        base = self.init()
        work = self.commit("docs/a.md", message="work")
        self.commit(self.HANDOFF, message="handoff")
        cases = (
            ("handoff-only commit", base, self.HANDOFF, work),
            ("the handoff named with ./", base, f"./{self.HANDOFF}", work),
            ("the parent itself as base", work, self.HANDOFF, work),
            ("no handoff", base, None, None),
            ("empty handoff", base, "", None),
            ("a different handoff", base, "automation/handoffs/other.json", None),
        )
        for name, base_sha, handoff, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.git_handoff_commit_parent(self.repo, base_sha, handoff))

    def test_none_when_the_last_commit_changed_more_or_precedes_the_base(self) -> None:
        base = self.init()
        self.commit(self.HANDOFF, "docs/a.md", message="both")
        self.assertIsNone(policy.git_handoff_commit_parent(self.repo, base, self.HANDOFF))

        head = self.commit(self.HANDOFF, message="handoff")
        self.assertIsNone(policy.git_handoff_commit_parent(self.repo, head, self.HANDOFF))

    def test_none_for_a_root_commit_or_an_unknown_base(self) -> None:
        self.git("init", "-q")
        root = self.commit(self.HANDOFF, message="root")
        self.assertIsNone(policy.git_handoff_commit_parent(self.repo, root, self.HANDOFF))
        self.commit(self.HANDOFF, message="again")
        self.assertIsNone(policy.git_handoff_commit_parent(self.repo, "f" * 40, self.HANDOFF))


class GitCommittedPathsTest(GitTestCase):
    def test_paths_changed_since_the_base_including_both_sides_of_a_rename(self) -> None:
        base = self.init()
        self.commit("docs/b.md", message="add")
        self.git("mv", "README.md", "docs/renamed.md")
        self.git("commit", "-q", "-m", "rename")
        self.write("dirty.txt")
        self.assertEqual(["README.md", "docs/b.md", "docs/renamed.md"], policy.git_committed_paths(self.repo, base))

    def test_nothing_committed_since_the_base(self) -> None:
        base = self.init()
        self.assertEqual([], policy.git_committed_paths(self.repo, base))

    def test_refuses_when_head_no_longer_descends_from_the_base(self) -> None:
        self.init()
        side = self.commit("side.txt", message="side")
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.commit("main.txt", message="main")
        with self.assertRaises(policy.ConfigError) as raised:
            policy.git_committed_paths(self.repo, side)
        self.assertEqual(
            f"HEAD no longer descends from the commit the slice started at ({side}): it is not an ancestor",
            str(raised.exception),
        )


if __name__ == "__main__":
    unittest.main()
