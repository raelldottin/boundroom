from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from automation.benchmark import last_build
from automation.benchmark.strategies import AGENT_TIMEOUT_RETURNCODE, ARTIFACT_PHASE_MAY_CHANGE, EXCLUDED_FROM_SUBMISSION, RPI_DIR
from automation.tests.test_benchmark_lanes import FakeAgent, run_lane

GOOD = "int main(void){return 0;}\n"
BROKEN = "int main(void){return undeclared_yet;}\n"


class _WorkspaceCase(unittest.TestCase):
    """Each test gets a real temporary directory, removed when it ends."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="last-build-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _workspace(self) -> Path:
        return _make_workspace(self.tmp / "workspace")


def _make_workspace(workspace: Path) -> Path:
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (workspace / "executable").write_text("reference binary\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.c").write_text(GOOD, encoding="utf-8")
    _compile_sh(workspace, "exit 0")
    last_build.install(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)
    return workspace


def _compile_sh(workspace: Path, body: str) -> None:
    script = workspace / "compile.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)


def _build(workspace: Path) -> int:
    """Run the installed script the way the prompt tells the agent to: from the workspace root."""
    return subprocess.run(["./" + RPI_DIR + "/" + last_build.SCRIPT_NAME], cwd=workspace, capture_output=True).returncode


def _member_text(tar: tarfile.TarFile, name: str) -> str:
    member = tar.extractfile(name)
    assert member is not None, f"{name} is not a regular file in the archive"
    return member.read().decode()


def _recorded(workspace: Path) -> set[str]:
    with tarfile.open(workspace / RPI_DIR / last_build.SNAPSHOT_NAME) as tar:
        return {name.removeprefix("./") for name in tar.getnames()} - {".", ""}


class CheckpointedBuildTests(_WorkspaceCase):
    """The record is taken inside the build, so it can only ever hold a tree that built."""

    def test_a_successful_build_records_exactly_what_would_be_submitted(self) -> None:
        workspace = self._workspace()
        self.assertEqual(0, _build(workspace))
        self.assertEqual({"compile.sh", "src", "src/main.c"}, _recorded(workspace))

    def test_a_failed_build_records_nothing_and_keeps_the_previous_record(self) -> None:
        workspace = self._workspace()
        self.assertEqual(0, _build(workspace))
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")
        _compile_sh(workspace, "exit 1")
        self.assertNotEqual(0, _build(workspace))
        with tarfile.open(workspace / RPI_DIR / last_build.SNAPSHOT_NAME) as tar:
            self.assertEqual(GOOD, _member_text(tar, "./src/main.c"))
        self.assertFalse((workspace / RPI_DIR / (last_build.SNAPSHOT_NAME + ".part")).exists())

    def test_the_prompt_names_the_installed_script(self) -> None:
        self.assertEqual("./.rpi/build.sh", last_build.checkpointed_build_command("", RPI_DIR))
        self.assertEqual("cd /workspace && ./.rpi/build.sh", last_build.checkpointed_build_command("/workspace/", RPI_DIR))


class RestoreTests(_WorkspaceCase):
    def test_restore_puts_back_the_last_built_tree_and_nothing_else(self) -> None:
        workspace = self._workspace()
        _build(workspace)
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")
        (workspace / "probe.py").write_text("print(1)\n", encoding="utf-8")
        (workspace / "compile.sh").unlink()

        record = last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)

        self.assertEqual({"restored": True, "entries": ["compile.sh", "src"]}, record)
        self.assertEqual(GOOD, (workspace / "src" / "main.c").read_text(encoding="utf-8"))
        self.assertFalse((workspace / "probe.py").exists())
        self.assertTrue((workspace / "compile.sh").stat().st_mode & 0o100, "compile.sh must stay executable")
        # Never submitted, so never swapped: the reference binary and VCS metadata stay as they were.
        self.assertEqual("reference binary\n", (workspace / "executable").read_text(encoding="utf-8"))
        self.assertTrue((workspace / ".git" / "HEAD").is_file())
        self.assertFalse((workspace / RPI_DIR / "last-build.restore").exists())

    def test_no_record_leaves_the_workspace_as_the_agent_left_it(self) -> None:
        workspace = self._workspace()
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")
        record = last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)
        self.assertEqual({"restored": False, "reason": "no successful build was recorded"}, record)
        self.assertEqual(BROKEN, (workspace / "src" / "main.c").read_text(encoding="utf-8"))

    def test_a_record_that_escapes_the_workspace_is_refused_before_anything_is_removed(self) -> None:
        """The record is written from inside the agent's sandbox, so it is untrusted input."""
        workspace = self._workspace()
        payload = b"pwned\n"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(payload)
        with tarfile.open(workspace / RPI_DIR / last_build.SNAPSHOT_NAME, "w") as tar:
            tar.addfile(info, io.BytesIO(payload))

        record = last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)

        self.assertFalse(record["restored"])
        self.assertIn("unreadable", record["reason"])
        self.assertFalse((workspace.parent / "escaped.txt").exists())
        self.assertEqual(GOOD, (workspace / "src" / "main.c").read_text(encoding="utf-8"))

    def test_a_record_cannot_overwrite_what_is_never_submitted(self) -> None:
        workspace = self._workspace()
        payload = b"rewritten\n"
        info = tarfile.TarInfo(f"./{RPI_DIR}/plan.json")
        info.size = len(payload)
        with tarfile.open(workspace / RPI_DIR / last_build.SNAPSHOT_NAME, "w") as tar:
            tar.addfile(info, io.BytesIO(payload))
        last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)
        self.assertFalse((workspace / RPI_DIR / "plan.json").exists())


class PutBackTests(_WorkspaceCase):
    """What an artifact-only phase changed outside its artifact directory is undone."""

    def _hold(self, workspace: Path) -> tuple[Path, dict]:
        archive = self.tmp / "held" / "workspace-before.tar"
        return archive, last_build.hold(workspace, archive, ARTIFACT_PHASE_MAY_CHANGE)

    def test_a_program_written_outside_the_artifact_is_put_back_reference_binary_and_all(self) -> None:
        workspace = self._workspace()
        archive, held = self._hold(workspace)
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")
        (workspace / "cmatrix.c").write_text(GOOD, encoding="utf-8")
        (workspace / "executable").write_text("the plan's own build\n", encoding="utf-8")
        (workspace / "compile.sh").unlink()
        (workspace / RPI_DIR / "plan.json").write_text("{}\n", encoding="utf-8")

        changed = last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE)

        self.assertEqual(("cmatrix.c", "compile.sh", "executable", "src/main.c"), changed)
        self.assertEqual(GOOD, (workspace / "src" / "main.c").read_text(encoding="utf-8"))
        self.assertFalse((workspace / "cmatrix.c").exists())
        self.assertEqual("reference binary\n", (workspace / "executable").read_text(encoding="utf-8"))
        self.assertTrue((workspace / "compile.sh").stat().st_mode & 0o100, "compile.sh must stay executable")
        # The artifact directory and VCS metadata are the phase's to change.
        self.assertEqual("{}\n", (workspace / RPI_DIR / "plan.json").read_text(encoding="utf-8"))
        self.assertTrue((workspace / ".git" / "HEAD").is_file())

    def test_an_execute_only_reference_binary_is_held_and_put_back_with_its_mode(self) -> None:
        """Run 41177348090: ``docker cp`` keeps the image's mode, and the reference binary has no owner-read bit.

        The agent runs as root, which ignores it; the harness does not.
        """
        workspace = self._workspace()
        reference = workspace / "executable"
        reference.chmod(0o111)
        archive, held = self._hold(workspace)
        self.assertEqual(0o111, stat.S_IMODE(reference.stat().st_mode), "holding must not leave the mode changed")
        reference.chmod(0o755)
        reference.write_text("the plan's own build\n", encoding="utf-8")

        changed = last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE)

        self.assertEqual(("executable",), changed)
        self.assertEqual(0o111, stat.S_IMODE(reference.stat().st_mode))
        reference.chmod(0o755)
        self.assertEqual("reference binary\n", reference.read_text(encoding="utf-8"))

    def test_a_session_that_never_made_the_artifact_directory_is_still_put_back(self) -> None:
        """Nothing guarantees ``.rpi`` exists: a phase that writes no artifact may never create it."""
        workspace = self._workspace()
        shutil.rmtree(workspace / RPI_DIR)
        archive, held = self._hold(workspace)
        (workspace / "probe.py").write_text("print(1)\n", encoding="utf-8")
        self.assertEqual(("probe.py",), last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE))
        self.assertFalse((workspace / "probe.py").exists())

    def test_a_phase_that_wrote_only_its_artifact_changes_nothing(self) -> None:
        workspace = self._workspace()
        archive, held = self._hold(workspace)
        (workspace / RPI_DIR / "research.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual((), last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE))

    def test_a_phase_that_changed_nothing_does_not_need_the_archive(self) -> None:
        workspace = self._workspace()
        archive, held = self._hold(workspace)
        archive.unlink()
        self.assertEqual((), last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE))

    def test_a_changed_workspace_with_an_unreadable_archive_is_refused_and_left_as_it_is(self) -> None:
        workspace = self._workspace()
        archive, held = self._hold(workspace)
        archive.write_bytes(b"not a tar archive")
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")

        with self.assertRaises(RuntimeError) as refusal:
            last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE)

        self.assertIn("could not put the workspace back", str(refusal.exception))
        self.assertIn("recorded build unreadable", str(refusal.exception))
        self.assertEqual(BROKEN, (workspace / "src" / "main.c").read_text(encoding="utf-8"))

    def test_holding_twice_into_a_directory_that_did_not_exist(self) -> None:
        workspace = self._workspace()
        archive = self.tmp / "held" / "nested" / "workspace-before.tar"
        first = last_build.hold(workspace, archive, ARTIFACT_PHASE_MAY_CHANGE)
        second = last_build.hold(workspace, archive, ARTIFACT_PHASE_MAY_CHANGE)
        self.assertEqual(first, second)
        with tarfile.open(archive) as tar:
            self.assertEqual(GOOD, _member_text(tar, "src/main.c"))

    @unittest.skipIf(os.geteuid() == 0, "root reads a file without its read bit")
    def test_an_execute_only_binary_is_archived_whole_and_executable(self) -> None:
        """A size of 256 bytes has the same bit set as owner-read: the mode, not the size, decides."""
        workspace = self._workspace()
        payload = bytes(range(256))
        reference = workspace / "executable"
        reference.write_bytes(payload)
        reference.chmod(0o111)

        archive, _held = self._hold(workspace)

        self.assertEqual(0o111, stat.S_IMODE(reference.stat().st_mode))
        with tarfile.open(archive) as tar:
            member = tar.getmember("executable")
            self.assertEqual(0o111, member.mode & 0o111, "the archived binary lost its execute bits")
            extracted = tar.extractfile(member)
            assert extracted is not None
            self.assertEqual(payload, extracted.read())

    def test_a_held_dangling_symlink_is_put_back_without_being_followed(self) -> None:
        workspace = self._workspace()
        (workspace / "dangling").symlink_to("missing-target")
        archive, held = self._hold(workspace)
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")

        changed = last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE)

        self.assertEqual(("src/main.c",), changed)
        self.assertTrue((workspace / "dangling").is_symlink())
        self.assertEqual("missing-target", os.readlink(workspace / "dangling"))
        self.assertEqual(GOOD, (workspace / "src" / "main.c").read_text(encoding="utf-8"))

    def test_a_changed_file_is_reported_by_its_own_path_and_kept_names_count_only_at_the_root(self) -> None:
        workspace = self._workspace()
        nested = workspace / "src" / RPI_DIR
        nested.mkdir()
        (nested / "notes.txt").write_text("before\n", encoding="utf-8")
        archive, held = self._hold(workspace)
        (workspace / "src" / "main.c").write_text(BROKEN, encoding="utf-8")
        (workspace / "src" / "extra.c").write_text(GOOD, encoding="utf-8")
        (nested / "notes.txt").write_text("after, and longer\n", encoding="utf-8")
        (workspace / RPI_DIR / "research.json").write_text("{}\n", encoding="utf-8")

        changed = last_build.put_back(workspace, RPI_DIR, archive, held, ARTIFACT_PHASE_MAY_CHANGE)

        # Not "src": a directory changes when its mode does, not when a file inside it does.
        self.assertEqual((f"src/{RPI_DIR}/notes.txt", "src/extra.c", "src/main.c"), changed)
        self.assertEqual("before\n", (nested / "notes.txt").read_text(encoding="utf-8"))
        self.assertFalse((workspace / "src" / "extra.c").exists())


class InstallTests(_WorkspaceCase):
    def test_install_writes_an_executable_script_and_returns_where_it_is(self) -> None:
        workspace = self.tmp / "bare"
        workspace.mkdir()

        script = last_build.install(workspace, "a/b", frozenset({"zz", "aa"}))

        self.assertEqual(workspace / "a" / "b" / last_build.SCRIPT_NAME, script)
        self.assertEqual(0o755, stat.S_IMODE(script.stat().st_mode))
        text = script.read_text(encoding="utf-8")
        # What is left out, in a stable order, and the record written inside the rpi dir.
        self.assertIn(" --exclude=./aa --exclude=./zz ", text)
        self.assertIn("-cf a/b/last-build.tar.part .", text)
        self.assertIn("mv -f a/b/last-build.tar.part a/b/last-build.tar", text)
        self.assertIn("COPYFILE_DISABLE=1 tar", text)

    def test_installing_again_rewrites_the_script(self) -> None:
        workspace = self._workspace()
        script = last_build.install(workspace, RPI_DIR, frozenset())
        self.assertEqual(workspace / RPI_DIR / last_build.SCRIPT_NAME, script)
        self.assertNotIn("--exclude", script.read_text(encoding="utf-8"))


class RestoreEdgeTests(_WorkspaceCase):
    def test_an_explicit_snapshot_is_restored_instead_of_the_recorded_build(self) -> None:
        workspace = self._workspace()
        self.assertEqual(0, _build(workspace))
        other = self.tmp / "other.tar"
        payload = b"int main(void){return 7;}\n"
        info = tarfile.TarInfo("./src/main.c")
        info.size = len(payload)
        with tarfile.open(other, "w") as tar:
            tar.addfile(info, io.BytesIO(payload))

        record = last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION, other)

        self.assertEqual({"restored": True, "entries": ["src"]}, record)
        self.assertEqual(payload, (workspace / "src" / "main.c").read_bytes())
        self.assertFalse((workspace / "compile.sh").exists())

    def test_an_unreadable_record_leaves_the_workspace_and_no_staging_behind(self) -> None:
        workspace = self._workspace()
        (workspace / RPI_DIR / last_build.SNAPSHOT_NAME).write_bytes(b"\x00garbage, not a tar")

        record = last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)

        self.assertFalse(record["restored"])
        self.assertTrue(record["reason"].startswith("recorded build unreadable: "), record["reason"])
        self.assertFalse((workspace / RPI_DIR / "last-build.restore").exists())
        self.assertEqual(GOOD, (workspace / "src" / "main.c").read_text(encoding="utf-8"))

    def test_a_symlink_to_a_directory_outside_is_removed_and_its_target_kept(self) -> None:
        workspace = self._workspace()
        self.assertEqual(0, _build(workspace))
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not the workspace's\n", encoding="utf-8")
        (workspace / "linked").symlink_to(outside, target_is_directory=True)

        last_build.restore(workspace, RPI_DIR, EXCLUDED_FROM_SUBMISSION)

        self.assertFalse((workspace / "linked").is_symlink())
        self.assertEqual("not the workspace's\n", (outside / "keep.txt").read_text(encoding="utf-8"))


class PlansAProgram(FakeAgent):
    """Plan writes the program, as nemotron's plan phase did; implement records what it inherits."""

    def __call__(self, command, workspace, env, timeout):
        returncode = super().__call__(command, workspace, env, timeout)
        phase = self.sessions[-1]["phase"]
        if phase == "plan":
            (workspace / "cmatrix.c").write_text(GOOD, encoding="utf-8")
            (workspace / "compile.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        return returncode

    def _write_artifact(self, workspace, phase):
        if phase == "implement":
            self.inherited = sorted(entry.name for entry in workspace.iterdir())
        super()._write_artifact(workspace, phase)


class _LaneCase(unittest.TestCase):
    """A lane run makes its own directories and leaves them; keep them in this test's temp dir."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="last-build-lane-")
        self.addCleanup(tmp.cleanup)
        previous = tempfile.tempdir
        tempfile.tempdir = tmp.name
        self.addCleanup(setattr, tempfile, "tempdir", previous)


class LanePutBackTests(_LaneCase):
    def test_implement_inherits_the_plan_artifact_and_not_the_program_plan_wrote(self) -> None:
        agent, result, _ = run_lane("C", agent=PlansAProgram())
        self.assertNotIn("cmatrix.c", agent.inherited)
        self.assertNotIn("compile.sh", agent.inherited)
        phases = {phase["phase"]: phase for phase in result.strategy.to_dict()["phases"]}
        self.assertEqual(["cmatrix.c", "compile.sh"], phases["plan"]["discarded"])
        self.assertEqual([], phases["research"]["discarded"])
        self.assertEqual([], phases["implement"]["discarded"])


class BuildsThenBreaks(FakeAgent):
    """Implement builds a good tree through the prompt's own command, then breaks it."""

    def __call__(self, command, workspace, env, timeout):
        returncode = super().__call__(command, workspace, env, timeout)
        if self.sessions[-1]["phase"] != "implement":
            return returncode
        self.script_was_installed = (workspace / RPI_DIR / last_build.SCRIPT_NAME).stat().st_mode & 0o100 != 0
        (workspace / "compile.sh").chmod(0o755)
        self.build_returncode = _build(workspace)
        (workspace / "main.c").write_text(BROKEN, encoding="utf-8")
        return returncode


def _submitted_main(out_tar: Path) -> str:
    with tarfile.open(out_tar) as tar:
        return _member_text(tar, "main.c")


class LaneRestoreTests(_LaneCase):
    def test_a_killed_implement_is_graded_on_its_last_build(self) -> None:
        agent, result, out_tar = run_lane("C", agent=BuildsThenBreaks(returncode=AGENT_TIMEOUT_RETURNCODE))
        self.assertTrue(agent.script_was_installed)
        self.assertEqual(0, agent.build_returncode)
        self.assertEqual(GOOD, _submitted_main(out_tar))
        self.assertTrue(result.strategy.to_dict()["implement_restore"]["restored"])

    def test_an_implement_that_ends_on_its_own_is_graded_as_left(self) -> None:
        """Ending the session is the agent's call; only a kill overrides the tree."""
        _, result, out_tar = run_lane("C", agent=BuildsThenBreaks(returncode=0))
        self.assertEqual(BROKEN, _submitted_main(out_tar))
        self.assertIsNone(result.strategy.to_dict()["implement_restore"])

    def test_a_kill_with_no_recorded_build_says_so(self) -> None:
        _, result, _ = run_lane("C", agent=FakeAgent(returncode=AGENT_TIMEOUT_RETURNCODE))
        self.assertEqual(
            {"restored": False, "reason": "no successful build was recorded"},
            result.strategy.to_dict()["implement_restore"],
        )

    def test_single_session_controls_are_never_restored(self) -> None:
        for lane in ("A", "B"):
            with self.subTest(lane=lane):
                _, result, _ = run_lane(lane, agent=FakeAgent(returncode=AGENT_TIMEOUT_RETURNCODE))
                self.assertIsNone(result.strategy.to_dict()["implement_restore"])


if __name__ == "__main__":
    unittest.main()
