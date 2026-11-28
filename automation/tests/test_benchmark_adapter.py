"""The adapter's own behaviour: what an agent session is handed, and what of its output the harness keeps.

Agent sessions and Docker are the unmanaged dependencies here, so they are the only things
stood in for: a runner function in place of ``run_agent.sh``, and a scripted Docker behind the
cleanroom and the witness. The filesystem is real, links and FIFOs included.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Mapping

from automation.benchmark import adapter, cleanroom, witness
from automation.benchmark.adapter import (
    NotARegularFile,
    SupervisorAgentAdapter,
    agent_environment,
    build_queue_data,
    build_slice_record,
    copy_regular_tree,
    entry_open_refusal,
    read_agent_file,
)
from automation.benchmark.instances import TaskSpec
from automation.tests.test_benchmark_witness import IMAGE_ID, FakeDocker, inspected

TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")
REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_AS_ROOT = unittest.skipIf(os.geteuid() == 0, "root opens a file whatever its mode")


class AgentEnvironmentTests(unittest.TestCase):
    LAUNCHER = {
        "PATH": "/usr/bin",
        "HOME": "/home/op",
        "CLAUDECODE": "1",
        "REPO_AUTOMATION_AGENT_RUNNER": "codex",
        "GITHUB_TOKEN": "ghp_secret",
        "ANTHROPIC_API_KEY": "sk-secret",
        "BOUNDROOM_SOMETHING_NEW": "x",
        "TERMINAL_CWD": "/previous/cell",
        "HERMES_HOME": "/previous/home",
    }

    def setUp(self) -> None:
        patcher = unittest.mock.patch.dict(os.environ, self.LAUNCHER, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_session_inherits_only_the_launcher_variables_named_for_it(self) -> None:
        self.assertEqual(
            {"PATH": "/usr/bin", "HOME": "/home/op", "CLAUDECODE": "1", "REPO_AUTOMATION_AGENT_RUNNER": "codex"},
            agent_environment(None),
        )

    def test_explicit_configuration_wins_and_is_never_filtered(self) -> None:
        environment = agent_environment({"PATH": "/opt/bin", "PROVIDER_KEY": "authorized"})
        self.assertEqual("/opt/bin", environment["PATH"])
        self.assertEqual("authorized", environment["PROVIDER_KEY"])
        self.assertEqual("/home/op", environment["HOME"])


class SliceRecordTests(unittest.TestCase):
    """The rebuild as the supervisor's prompt renderer sees it: one schema-valid slice."""

    def test_a_rebuild_is_one_queued_slice_over_the_whole_workspace(self) -> None:
        self.assertEqual(
            {
                "slice_id": TASK.public_id,
                "title": "Rebuild the program in this workspace from scratch",
                "status": "queued",
                "priority": 0,
                "domain": "programbench",
                "allowed_paths": ["./"],
                "required_validations": ["sh compile.sh"],
                "depends_on": [],
                "max_files_changed": 1_000_000,
                "notes": TASK.objective,
            },
            build_slice_record(TASK),
        )

    def test_the_queue_holds_that_slice_alone_and_allows_one_autonomous_step(self) -> None:
        record = build_slice_record(TASK)
        self.assertEqual(
            {
                "version": 1,
                "policy": {
                    "consecutive_autonomous_limit": 1,
                    "handoff_timeout_seconds": 1800,
                    "agent_command_template": "cmd {repo_root}",
                    "supervisor_owned_paths": ["automation/"],
                },
                "slices": [record],
            },
            build_queue_data(record, "cmd {repo_root}", 1800),
        )


class ReadAgentFileTests(unittest.TestCase):
    """An agent-written file is read whole, or refused with the reason; never through a link."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def refusal(self, path: Path, limit: int = 64) -> str:
        with self.assertRaises(NotARegularFile) as raised:
            read_agent_file(path, limit)
        return raised.exception.reason

    def test_a_file_of_exactly_the_limit_is_read_whole(self) -> None:
        path = self.root / "out.json"
        path.write_bytes(b"x" * 64)
        self.assertEqual(b"x" * 64, read_agent_file(path, 64))

    def test_one_byte_over_the_limit_is_too_large(self) -> None:
        path = self.root / "out.json"
        path.write_bytes(b"x" * 65)
        self.assertEqual("too_large", self.refusal(path))

    def test_links_and_special_files_are_refused_by_kind(self) -> None:
        target = self.root / "secret"
        target.write_bytes(b"PRIVATE")
        (self.root / "symlink").symlink_to(target)
        os.link(target, self.root / "hard")
        os.mkfifo(self.root / "fifo")
        self.assertEqual("symlink", self.refusal(self.root / "symlink"))
        self.assertEqual("hard_link", self.refusal(self.root / "hard"))
        self.assertEqual("special", self.refusal(self.root / "fifo"))

    @SKIP_AS_ROOT
    def test_a_file_that_cannot_be_opened_is_an_error_not_a_refusal(self) -> None:
        path = self.root / "out.json"
        path.write_bytes(b"{}")
        path.chmod(0)
        with self.assertRaises(PermissionError):
            read_agent_file(path, 64)

    def test_a_missing_file_is_an_error_not_a_refusal(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_agent_file(self.root / "missing.json", 64)


class CopyRegularTreeRootTests(unittest.TestCase):
    """What a copy does with a source that is not a plain directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_missing_source_copies_and_refuses_nothing(self) -> None:
        self.assertEqual([], copy_regular_tree(self.root / "missing", self.root / "copy"))
        self.assertFalse((self.root / "copy").exists())

    def test_a_source_that_is_a_file_or_a_link_is_refused_whole(self) -> None:
        (self.root / "file").write_text("x", encoding="utf-8")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "key").write_text("PRIVATE", encoding="utf-8")
        (self.root / "dir-link").symlink_to(self.root / "secrets", target_is_directory=True)
        (self.root / "file-link").symlink_to(self.root / "file")
        cases = {"file": "not_a_directory", "dir-link": "symlink", "file-link": "symlink"}
        for name, reason in cases.items():
            with self.subTest(source=name):
                destination = self.root / f"copy-of-{name}"
                self.assertEqual([{"path": ".", "reason": reason}], copy_regular_tree(self.root / name, destination))
                self.assertFalse(destination.exists())

    def test_a_source_below_a_file_is_an_error_not_a_refusal(self) -> None:
        (self.root / "file").write_text("x", encoding="utf-8")
        with self.assertRaises(NotADirectoryError):
            copy_regular_tree(self.root / "file" / "sessions", self.root / "copy")


class CopyRegularTreeTests(unittest.TestCase):
    """Only real directories and single-name regular files leave an agent-written tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.copy = self.root / "copy"
        self.addCleanup(self._tmp.cleanup)

    def copied(self) -> dict[str, str]:
        return {
            path.relative_to(self.copy).as_posix(): (path.read_text(encoding="utf-8") if path.is_file() else "<dir>")
            for path in sorted(self.copy.rglob("*"))
        }

    def test_everything_but_plain_files_and_directories_is_left_out_and_named(self) -> None:
        secret = self.root / "id_ed25519"
        secret.write_text("PRIVATE", encoding="utf-8")
        (self.source / "report.json").write_text("{}", encoding="utf-8")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "log.txt").write_text("log", encoding="utf-8")
        (self.source / "nested" / "stolen").symlink_to(secret)
        (self.source / "linked-dir").symlink_to(self.root, target_is_directory=True)
        os.link(secret, self.source / "hard")
        os.mkfifo(self.source / "pipe")

        refused = copy_regular_tree(self.source, self.copy)

        self.assertEqual(
            [
                {"path": "hard", "reason": "hard_link"},
                {"path": "linked-dir", "reason": "symlink"},
                {"path": "nested/stolen", "reason": "symlink"},
                {"path": "pipe", "reason": "special"},
            ],
            sorted(refused, key=lambda entry: entry["path"]),
        )
        self.assertEqual({"nested": "<dir>", "nested/log.txt": "log", "report.json": "{}"}, self.copied())

    @SKIP_AS_ROOT
    def test_entries_the_harness_cannot_open_are_left_out_as_unreadable(self) -> None:
        (self.source / "report.json").write_text("{}", encoding="utf-8")
        (self.source / "locked.txt").write_text("locked", encoding="utf-8")
        (self.source / "locked.txt").chmod(0)
        (self.source / "locked-dir").mkdir()
        (self.source / "locked-dir").chmod(0)
        self.addCleanup((self.source / "locked-dir").chmod, 0o700)

        refused = copy_regular_tree(self.source, self.copy)

        self.assertEqual(
            [{"path": "locked-dir", "reason": "unreadable"}, {"path": "locked.txt", "reason": "unreadable"}],
            sorted(refused, key=lambda entry: entry["path"]),
        )
        self.assertEqual({"report.json": "{}"}, self.copied())

    def test_a_tree_far_deeper_than_the_limit_is_refused_once_at_the_limit(self) -> None:
        self.source.joinpath(*["d"] * 74).mkdir(parents=True)
        self.assertEqual([{"path": "/".join(["d"] * 65), "reason": "too_deep"}], copy_regular_tree(self.source, self.copy))

    def test_directories_are_copied_sixty_four_deep_and_no_deeper(self) -> None:
        deepest = self.source.joinpath(*["d"] * 65)
        deepest.mkdir(parents=True)
        (deepest.parent / "at-64.txt").write_text("kept", encoding="utf-8")
        (deepest / "at-65.txt").write_text("left out", encoding="utf-8")

        refused = copy_regular_tree(self.source, self.copy)

        self.assertEqual([{"path": "/".join(["d"] * 65), "reason": "too_deep"}], refused)
        self.assertEqual("kept", self.copy.joinpath(*["d"] * 64, "at-64.txt").read_text(encoding="utf-8"))
        self.assertFalse(self.copy.joinpath(*["d"] * 65).exists())

    def test_copies_keep_permission_bits_less_setuid_and_keep_their_times(self) -> None:
        for name, mode in (("read-only", 0o444), ("setuid", 0o4755)):
            path = self.source / name
            path.write_text(name, encoding="utf-8")
            path.chmod(mode)
            os.utime(path, ns=(1_000_000_000_000_000_000, 1_100_000_000_000_000_000))
        self.addCleanup((self.source / "read-only").chmod, 0o644)
        if not (self.source / "setuid").stat().st_mode & stat.S_ISUID:
            self.skipTest("this filesystem does not keep the setuid bit")

        copy_regular_tree(self.source, self.copy)

        self.assertEqual(0o444, stat.S_IMODE((self.copy / "read-only").stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE((self.copy / "setuid").stat().st_mode))
        for name in ("read-only", "setuid"):
            copied = (self.copy / name).stat()
            self.assertEqual((1_000_000_000_000_000_000, 1_100_000_000_000_000_000), (copied.st_atime_ns, copied.st_mtime_ns))

    def test_a_directory_copy_stays_the_harness_s_to_clean_up_and_keeps_its_times(self) -> None:
        sealed = self.source / "sealed"
        sealed.mkdir()
        (sealed / "kept.txt").write_text("kept", encoding="utf-8")
        sealed.chmod(0o500)
        os.utime(sealed, ns=(1_000_000_000_000_000_000, 1_100_000_000_000_000_000))
        self.addCleanup(sealed.chmod, 0o700)

        copy_regular_tree(self.source, self.copy)

        self.assertEqual(0o700, stat.S_IMODE((self.copy / "sealed").stat().st_mode))
        self.assertEqual(1_100_000_000_000_000_000, (self.copy / "sealed").stat().st_mtime_ns)
        (self.copy / "sealed" / "kept.txt").unlink()  # the harness can still clean up its record


class EntryOpenRefusalTests(unittest.TestCase):
    """Why an entry that was listed and then could not be opened is left out of a copy."""

    def test_an_entry_gone_since_the_listing_is_neither_copied_nor_refused(self) -> None:
        self.assertIsNone(entry_open_refusal(FileNotFoundError(errno.ENOENT, "gone")))

    def test_an_entry_swapped_for_a_link_since_the_listing_is_a_symlink(self) -> None:
        self.assertEqual("symlink", entry_open_refusal(OSError(errno.ELOOP, "too many levels of symbolic links")))

    def test_any_other_failure_to_open_is_unreadable(self) -> None:
        for code in (errno.EACCES, errno.EIO, errno.ENXIO):
            with self.subTest(errno=errno.errorcode[code]):
                self.assertEqual("unreadable", entry_open_refusal(OSError(code, os.strerror(code))))

    def test_running_out_of_descriptors_is_the_harness_s_failure_and_is_raised(self) -> None:
        for code in (errno.EMFILE, errno.ENFILE):
            failure = OSError(code, os.strerror(code))
            with self.subTest(errno=errno.errorcode[code]), self.assertRaises(OSError) as raised:
                entry_open_refusal(failure)
            self.assertIs(failure, raised.exception)


class CleanroomDocker(FakeDocker):
    """Docker for one cleanroom cell: the image's workspace, its preflight, and the witness's view.

    The containers the probe and the session create are the runner's to announce, through
    ``start``, the way Hermes creates them partway into a session.
    """

    def __init__(self, *, image_id: str = IMAGE_ID, network_mode: str = "none") -> None:
        super().__init__(containers=[], inspect={}, drop_after_inspect=True)
        self.image_id = image_id
        self.network_mode = network_mode
        self.created = 0
        # What the workspace held when the image's own was copied in: it must be nothing.
        self.workspace_before_copy: list[str] | None = None

    def start(self, workspace: Path, *task_ids: str) -> None:
        for task_id in task_ids:
            self.created += 1
            container = f"{self.created:064x}"
            self.inspect[container] = inspected(workspace, container=container, task_id=task_id, network_mode=self.network_mode)
            self.create(container)

    def _answer(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[1] == "create":
            self.calls.append(argv)
            return 0, "preflight-container\n", ""
        if argv[1] == "cp":
            self.calls.append(argv)
            workspace = Path(argv[-1])
            self.workspace_before_copy = sorted(entry.name for entry in workspace.iterdir())
            (workspace / "executable").write_bytes(b"\x7fELF")
            (workspace / "README.md").write_text("the specification\n", encoding="utf-8")
            (workspace / ".git").mkdir()
            return 0, "", ""
        if argv[1] == "image":
            self.calls.append(argv)
            return 0, f"{self.image_id}\n", ""
        if argv[1] == "start":
            self.calls.append(argv)
            return (
                0,
                "reference_executable=yes\ngit_worktree=yes\nworkspace_writable=yes\negress=yes|6|curl -sS -m 5 https://github.com\n",
                "",
            )
        if argv[1] == "inspect" and argv[3] == "{{.HostConfig.NetworkMode}}":
            self.calls.append(argv)
            return 0, "none\n", ""
        if argv[1] == "rm" and argv[-1] == "preflight-container":
            self.calls.append(argv)
            return 0, "", ""
        return super()._answer(argv)


class CleanroomCellTests(unittest.TestCase):
    """One cleanroom cell end to end, with Docker and the agent session stood in for."""

    def run_cell(self, docker: CleanroomDocker, out_tar: Path, sessions: list[tuple[str, str]]) -> adapter.SubmissionResult:
        """Run one cell; ``sessions`` collects each session's command and sandbox image, even when the cell is refused."""

        def runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
            sessions.append((command, env.get(adapter.SANDBOX_IMAGE_ENV, "")))
            if "--sandbox-probe" in command:
                docker.start(workspace, "default", "probe-a1b2")
            else:
                docker.start(workspace, "default")
                (workspace / "compile.sh").write_text("#!/bin/sh\ncc main.c\n", encoding="utf-8")
                (workspace / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            return 0

        with (
            unittest.mock.patch.object(cleanroom, "_subprocess_docker", docker),
            unittest.mock.patch.object(witness, "_subprocess_docker", docker),
        ):
            result = SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=runner, cleanroom=True).produce_submission(TASK, out_tar)
        return result

    def test_a_sound_cleanroom_cell_is_proved_before_and_during_the_session(self) -> None:
        docker = CleanroomDocker()
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp) / "B" / "r1" / TASK.instance_id
            sessions: list[tuple[str, str]] = []
            result = self.run_cell(docker, cell_dir / "submission.tar.gz", sessions)
            receipt = json.loads((cell_dir / "cleanroom.json").read_text(encoding="utf-8"))
            probe_receipt = json.loads((cell_dir / "agent-sessions" / "sandbox-probe.sandbox.json").read_text(encoding="utf-8"))
            with tarfile.open(cell_dir / "submission.tar.gz", "r:gz") as tar:
                submitted = sorted(tar.getnames())

        assert result.cleanroom is not None
        image = cleanroom.image_for(TASK.instance_id)
        self.assertEqual(result.cleanroom.to_dict(), receipt)
        self.assertEqual((image, IMAGE_ID), (result.cleanroom.image, result.cleanroom.image_id))
        # Nothing of the harness's own, not even a `git init`, was in the workspace before the
        # image's: the cell runs on the worktree the image ships.
        self.assertEqual([], docker.workspace_before_copy)
        self.assertTrue(any(f"https://github.com/{TASK.repository}.git" in " ".join(call) for call in docker.calls))
        self.assertTrue(probe_receipt["default_backend_verified"])
        self.assertTrue(probe_receipt["session_backend_verified"])
        self.assertEqual(2, len(sessions))
        self.assertIn("--sandbox-probe", sessions[0][0])
        self.assertIn("--prompt-file", sessions[1][0])
        self.assertEqual(image, sessions[1][1])
        # The workspace was handed back after each session by the image the cell was proved
        # against, by id, never by whatever its tag names now.
        reclaims = [call for call in docker.calls if call[1] == "run"]
        self.assertEqual(2, len(reclaims))
        self.assertTrue(all(IMAGE_ID in call for call in reclaims))
        # The reference executable is the specification, never part of the answer; the
        # documentation the agent read from stays in.
        self.assertEqual(["README.md", "compile.sh", "main.c"], submitted)

    def test_a_probe_container_off_the_air_gap_refuses_the_cell_before_the_session(self) -> None:
        docker = CleanroomDocker(network_mode="bridge")
        with tempfile.TemporaryDirectory() as tmp:
            cell_dir = Path(tmp) / "B" / "r1" / TASK.instance_id
            sessions: list[tuple[str, str]] = []
            with self.assertRaises(cleanroom.CleanroomError) as refusal:
                self.run_cell(docker, cell_dir / "submission.tar.gz", sessions)
            receipt_kept = (cell_dir / "cleanroom.json").is_file()
        self.assertIn("NetworkMode='bridge'", str(refusal.exception))
        self.assertIn("no sound default container", str(refusal.exception))
        self.assertTrue(receipt_kept)
        self.assertEqual(1, len(sessions))
        self.assertIn("--sandbox-probe", sessions[0][0])


if __name__ == "__main__":
    unittest.main()
