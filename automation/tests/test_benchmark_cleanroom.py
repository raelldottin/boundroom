"""The cleanroom has to be proved, not configured.

Every test here drives ``cleanroom`` against a stand-in for Docker, because the thing
under test is what the module *concludes* from what a container reports - and the
conclusion that matters is the refusal. A run that scores a lane on a workspace with no
reference executable, or with a working network, is not a ProgramBench inference result,
and the only place that can be caught is before the first agent turn.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from automation.benchmark import cleanroom
from automation.benchmark.cleanroom import (
    CleanroomError,
    CleanroomReceipt,
    image_for,
    materialize,
    prepare,
    verify,
)

INSTANCE = "abishekvashok__cmatrix.5c082c6"
REPOSITORY = "abishekvashok/cmatrix"

# What a healthy cleanroom's probe container prints: a reference binary, a writable
# workspace, and four egress attempts that each got nowhere.
HEALTHY_PROBE = """reference_executable=yes
workspace_writable=yes
git_worktree=yes
egress=yes|6|curl -sS -m 5 https://github.com
egress=no||wget -q -T 5 -O - https://github.com
egress=yes|128|git ls-remote https://github.com/abishekvashok/cmatrix.git
egress=yes|2|getent hosts github.com
"""


class FakeDocker:
    """A Docker that reports whatever the test needs it to report."""

    def __init__(
        self,
        *,
        contents: dict[str, str],
        probe: str = HEALTHY_PROBE,
        network_mode: str = "none",
        executable: bool = True,
        # What a cache miss narrates on stderr. It both precedes and follows the container
        # id on stdout, so no line-picking rule can separate them from one merged stream.
        pull_noise: str = (
            "Unable to find image locally\n"
            "task_cleanroom_v6: Pulling from programbench/x\n"
            "Status: Downloaded newer image for programbench/x:task_cleanroom_v6\n"
        ),
        # Calls that fail, by name: "create" (the copy container), "probe" (the probe
        # container's create), or any other verb ("cp", "image", "start", "inspect", "rm").
        fail: dict[str, tuple[int, str, str]] | None = None,
    ) -> None:
        self.contents = contents
        self.probe = probe
        self.network_mode = network_mode
        self.pull_noise = pull_noise
        self.executable = executable
        self.fail = fail or {}
        self.calls: list[list[str]] = []
        self._workspace: Path | None = None

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        name = "probe" if argv[1] == "create" and "--network=none" in argv else argv[1]
        if name in self.fail:
            return self.fail[name]
        if argv[1] == "create":
            container = "probe-container" if "--network=none" in argv else "copy-container"
            return 0, f"{container}\n", self.pull_noise
        if argv[1] == "cp":
            destination = Path(argv[3])
            for name, body in self.contents.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            if self.executable:
                (destination / "executable").chmod(0o755)
            return 0, "", ""
        if argv[1] == "image":
            # Answers only the id query for the image the copy container was made from.
            created = next((call[2] for call in self.calls if call[1] == "create" and "--network=none" not in call), None)
            if argv == ["docker", "image", "inspect", "--format", "{{.Id}}", created]:
                return 0, "sha256:d1ge57\n", ""
            return 1, "", f"unexpected image query: {argv}"
        if argv[1] == "start":
            return 0, self.probe, ""
        if argv[1] == "inspect":
            return 0, self.network_mode + "\n", ""
        if argv[1] == "rm":
            return 0, "", ""
        raise AssertionError(f"unexpected docker call: {argv}")

    def call(self, verb: str) -> list[str]:
        return next(argv for argv in self.calls if argv[1] == verb)


def cleanroom_contents() -> dict[str, str]:
    # `.git` is part of what the official image ships: its /workspace is a one-commit
    # repository, built by cloning upstream and then replacing the history wholesale.
    return {
        "executable": "ELF",
        "README.md": "cmatrix - terminal rain",
        "doc/cmatrix.1": ".TH CMATRIX 1",
        ".git/HEAD": "ref: refs/heads/master\n",
    }


class ImageNamingTests(unittest.TestCase):
    def test_the_instance_id_becomes_the_official_image_name(self) -> None:
        # ProgramBench's own rule: Docker repository names cannot hold `__`.
        self.assertEqual(
            "programbench/abishekvashok_1776_cmatrix.5c082c6:task_cleanroom_v6",
            image_for(INSTANCE),
        )


class PrepareTests(unittest.TestCase):
    def prepare(self, docker: FakeDocker, workspace: Path):
        return prepare(INSTANCE, workspace, REPOSITORY, run=docker)

    def test_the_reference_executable_and_its_documentation_arrive_before_the_agent_does(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            receipt = self.prepare(docker, workspace)

            self.assertTrue((workspace / "executable").is_file())
            self.assertTrue(receipt.reference_executable)
            self.assertEqual(["README.md", "doc"], receipt.documentation)
            self.assertEqual("sha256:d1ge57", receipt.image_id)
            self.assertEqual("none", receipt.network_mode)
            # The whole image workspace, not a curated subset: what the image ships is the
            # specification the model is allowed to read.
            self.assertEqual([".git", "README.md", "doc", "executable"], receipt.workspace_entries)
            self.assertTrue(receipt.git_worktree)

    def test_a_pull_on_the_way_in_is_not_mistaken_for_the_container_id(self) -> None:
        # Runs 36414921665 and 36419783072: the image is never cached on a fresh runner, so
        # `docker create` pulls, and its narration on stderr was read as the container id.
        # Every later call then addressed a container that does not exist.
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            self.prepare(docker, Path(temp_dir) / "ws")
        self.assertEqual("copy-container:/workspace/.", docker.call("cp")[2])
        self.assertEqual("probe-container", docker.call("inspect")[-1])

    def test_the_probe_runs_air_gapped_in_the_same_image_and_the_same_workspace(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            self.prepare(docker, workspace)

            created = [argv for argv in docker.calls if argv[1] == "create"][-1]
            self.assertIn("--network=none", created)
            self.assertIn(f"{workspace.resolve()}:/workspace", created)
            self.assertIn(image_for(INSTANCE), created)
            # Created and inspected rather than `run --rm`: the network mode has to be read
            # off the container that ran the probes, not off the flags we believe we passed.
            self.assertEqual(
                ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", "probe-container"],
                docker.call("inspect"),
            )

    def test_a_reachable_network_is_refused(self) -> None:
        docker = FakeDocker(
            contents=cleanroom_contents(),
            probe=HEALTHY_PROBE.replace("egress=yes|6|curl", "egress=yes|0|curl"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("network egress succeeded", str(refusal.exception))

    def test_a_container_that_is_not_air_gapped_is_refused_even_when_nothing_reached_out(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), network_mode="bridge")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("NetworkMode='bridge'", str(refusal.exception))

    def test_an_image_too_thin_to_test_is_not_a_tested_cleanroom(self) -> None:
        # Every probe binary missing proves nothing about the network. Passing here would
        # mean the air gap was never checked, only assumed.
        absent = "\n".join(
            line.replace("=yes|6|", "=no||").replace("=yes|128|", "=no||").replace("=yes|2|", "=no||")
            for line in HEALTHY_PROBE.splitlines()
        )
        docker = FakeDocker(contents=cleanroom_contents(), probe=absent)
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("no egress probe could run", str(refusal.exception))

    def test_a_workspace_without_the_reference_binary_is_refused(self) -> None:
        docker = FakeDocker(
            contents={"README.md": "docs only", ".git/HEAD": "ref: x\n"},
            probe=HEALTHY_PROBE.replace("reference_executable=yes", "reference_executable=no"),
            executable=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("no executable reference", str(refusal.exception))

    def test_the_harness_never_creates_the_workspace_it_is_supposed_to_be_measuring(self) -> None:
        # A `git init` here, or a leftover from an earlier cell, would make the cell's
        # environment partly the harness's invention. run_agent.sh needs a worktree; the
        # image ships one, and that is the only one a cleanroom cell may run on.
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            (workspace / ".git").mkdir()
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, workspace)
        self.assertIn("is not empty", str(refusal.exception))
        self.assertEqual([], [argv for argv in docker.calls if argv[1] == "cp"])

    def test_the_worktree_the_image_ships_has_its_remotes_read_and_left_alone(self) -> None:
        # The upstream identity this benchmark leaks is not all the harness's to remove.
        # The prompt is ours and no longer names the program; the image's own worktree is
        # ProgramBench's, and sanitizing it would make this a different benchmark. So a
        # remote is recorded as evidence and nothing is written back.
        probe = HEALTHY_PROBE + "git_remote=remote.origin.url|https://github.com/abishekvashok/cmatrix.git\n"
        docker = FakeDocker(contents=cleanroom_contents(), probe=probe)
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt = prepare(INSTANCE, Path(temp_dir), REPOSITORY, run=docker)

            self.assertEqual(
                [{"name": "remote.origin.url", "url": "https://github.com/abishekvashok/cmatrix.git"}],
                receipt.git_remotes,
            )
            self.assertEqual([], receipt.violations())
            self.assertEqual([], [argv for argv in docker.calls if "remote" in argv or "config" in argv])

    def test_a_worktree_with_no_remote_records_none_rather_than_guessing(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt = prepare(INSTANCE, Path(temp_dir), REPOSITORY, run=docker)
            self.assertEqual([], receipt.git_remotes)

    def test_an_image_that_ships_no_worktree_is_refused_rather_than_git_inited(self) -> None:
        docker = FakeDocker(
            contents={"executable": "ELF", "README.md": "docs"},
            probe=HEALTHY_PROBE.replace("git_worktree=yes", "git_worktree=no"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("ships no Git worktree", str(refusal.exception))

    def test_a_workspace_without_documentation_is_refused_and_says_what_it_found(self) -> None:
        # The task is "rebuild from the executable and the bundled documentation". Half of
        # that being absent changes the task, so it stops the run instead of scoring it.
        docker = FakeDocker(contents={"executable": "ELF", "data.bin": "\0", ".git/HEAD": "ref: x\n"})
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("no bundled documentation", str(refusal.exception))
        self.assertIn(".git, data.bin, executable", str(refusal.exception))

    def test_a_refused_cleanroom_still_reports_everything_it_observed(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), network_mode="bridge")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        receipt = refusal.exception.receipt
        assert receipt is not None
        self.assertTrue(receipt.reference_executable)
        self.assertEqual(4, len(receipt.egress_probes))
        self.assertEqual("bridge", receipt.to_dict()["network_mode"])


IMAGE = image_for(INSTANCE)


def healthy_receipt(**overrides) -> CleanroomReceipt:
    """A receipt that passes every acceptance item, with whatever the test changes."""
    fields: dict = {
        "instance_id": INSTANCE,
        "image": IMAGE,
        "image_id": "sha256:d1ge57",
        "network_mode": "none",
        "reference_executable": True,
        "workspace_writable": True,
        "git_worktree": True,
        "workspace_entries": ["README.md", "executable"],
        "documentation": ["README.md"],
        "egress_probes": [{"command": "curl -sS -m 5 https://github.com", "present": True, "returncode": 6}],
    }
    fields.update(overrides)
    return CleanroomReceipt(**fields)


class ImageNamingTableTests(unittest.TestCase):
    def test_every_double_underscore_is_replaced_and_nothing_else_is(self) -> None:
        cases = {
            "fixture__tool.0000000": "programbench/fixture_1776_tool.0000000:task_cleanroom_v6",
            "a__b__c.1": "programbench/a_1776_b_1776_c.1:task_cleanroom_v6",
            "plain": "programbench/plain:task_cleanroom_v6",
            "one_underscore": "programbench/one_underscore:task_cleanroom_v6",
        }
        self.assertEqual(cases, {instance: image_for(instance) for instance in cases})


class ReceiptTests(unittest.TestCase):
    """What a receipt concludes from what was observed, apart from Docker."""

    def test_a_receipt_that_observed_nothing_fails_every_item_in_contract_order(self) -> None:
        # The defaults are "not observed": a receipt is never sound until a probe says so.
        self.assertEqual(
            [
                "model-facing tools are not air-gapped: NetworkMode='', want 'none'",
                "no executable reference at /workspace/executable before the first agent turn",
                "no bundled documentation in the image workspace; it holds only: (nothing)",
                "/workspace is not writable by the tools that have to rebuild in it",
                "the image ships no Git worktree at /workspace; the harness does not create one",
                "no egress probe could run: none of curl, wget, git or getent exist in the image",
            ],
            CleanroomReceipt(instance_id="i", image="img").violations(),
        )

    def test_the_default_receipt_serializes_every_field_in_contract_order(self) -> None:
        self.assertEqual(
            {
                "instance_id": "i",
                "image": "img",
                "image_id": "",
                "network_mode": "",
                "reference_executable": False,
                "workspace_writable": False,
                "git_worktree": False,
                "workspace_entries": [],
                "documentation": [],
                "git_remotes": [],
                "egress_probes": [],
                "probe_argv": [],
            },
            CleanroomReceipt(instance_id="i", image="img").to_dict(),
        )
        self.assertEqual(
            [
                "instance_id",
                "image",
                "image_id",
                "network_mode",
                "reference_executable",
                "workspace_writable",
                "git_worktree",
                "workspace_entries",
                "documentation",
                "git_remotes",
                "egress_probes",
                "probe_argv",
            ],
            list(healthy_receipt().to_dict()),
        )

    def test_a_healthy_receipt_has_no_violations(self) -> None:
        self.assertEqual([], healthy_receipt().violations())

    def test_each_failed_item_is_named_on_its_own(self) -> None:
        reached = {"command": "curl -sS -m 5 https://github.com", "present": True, "returncode": 0}
        absent = {"command": "wget -q", "present": False, "returncode": None}
        cases = [
            ({"network_mode": "bridge"}, ["model-facing tools are not air-gapped: NetworkMode='bridge', want 'none'"]),
            ({"reference_executable": False}, ["no executable reference at /workspace/executable before the first agent turn"]),
            (
                {"documentation": []},
                ["no bundled documentation in the image workspace; it holds only: README.md, executable"],
            ),
            (
                {"documentation": [], "workspace_entries": []},
                ["no bundled documentation in the image workspace; it holds only: (nothing)"],
            ),
            ({"workspace_writable": False}, ["/workspace is not writable by the tools that have to rebuild in it"]),
            ({"git_worktree": False}, ["the image ships no Git worktree at /workspace; the harness does not create one"]),
            (
                {"egress_probes": [reached, absent]},
                ["network egress succeeded from inside the sandbox: ['curl -sS -m 5 https://github.com']"],
            ),
            (
                {"egress_probes": [absent]},
                ["no egress probe could run: none of curl, wget, git or getent exist in the image"],
            ),
            (
                {"egress_probes": []},
                ["no egress probe could run: none of curl, wget, git or getent exist in the image"],
            ),
            # A probe that ran but whose exit status was unreadable reached nothing.
            ({"egress_probes": [{"command": "git ls-remote", "present": True, "returncode": None}]}, []),
            ({"egress_probes": [{"command": "git ls-remote", "present": True}]}, []),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(expected, healthy_receipt(**overrides).violations())

    def test_every_probe_that_reached_out_is_listed_in_probe_order(self) -> None:
        probes = [
            {"command": "curl a", "present": True, "returncode": 0},
            {"command": "wget b", "present": True, "returncode": 4},
            {"command": "git c", "present": True, "returncode": 0},
        ]
        self.assertEqual(
            ["network egress succeeded from inside the sandbox: ['curl a', 'git c']"],
            healthy_receipt(egress_probes=probes).violations(),
        )

    def test_several_failures_are_reported_together_in_contract_order(self) -> None:
        receipt = healthy_receipt(
            network_mode="host",
            workspace_writable=False,
            egress_probes=[{"command": "getent hosts github.com", "present": True, "returncode": 0}],
        )
        self.assertEqual(
            [
                "model-facing tools are not air-gapped: NetworkMode='host', want 'none'",
                "/workspace is not writable by the tools that have to rebuild in it",
                "network egress succeeded from inside the sandbox: ['getent hosts github.com']",
            ],
            receipt.violations(),
        )


class DocumentationTests(unittest.TestCase):
    """What counts as a specification the agent may read, read off a real workspace."""

    def test_documentation_is_told_apart_by_name_and_by_whether_it_is_a_directory(self) -> None:
        files = [
            "foo.1",  # a man page
            "foo.9",
            "foo.10",  # not a man page section
            ".1",  # a suffix with no name in front of it
            "NOTES.TXT",  # suffixes compare case-blind
            "guide.html",
            "README",  # conventional extensionless names
            "ChangeLog",
            "Makefile",
            "main.c",
            "docs",  # a file named like a doc directory is just a file
            "readme.c",
        ]
        directories = ["doc", "share", "MAN", "src", "help"]  # a doc stem, but as a directory
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for name in files:
                (workspace / name).write_text("x", encoding="utf-8")
            for name in directories:
                (workspace / name).mkdir()

            receipt = verify(INSTANCE, workspace, IMAGE, "sha256:d1ge57", REPOSITORY, run=FakeDocker(contents={}))

        self.assertEqual(
            ["ChangeLog", "MAN", "NOTES.TXT", "README", "doc", "foo.1", "foo.9", "guide.html", "share"], receipt.documentation
        )
        self.assertEqual(sorted(files + directories), receipt.workspace_entries)


class ProbeParsingTests(unittest.TestCase):
    """The probe container's `key=value` lines, read the way verify reads them."""

    def verified(self, probe: str) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README").write_text("x", encoding="utf-8")
            receipt = verify(INSTANCE, workspace, IMAGE, "sha256:d1ge57", REPOSITORY, run=FakeDocker(contents={}, probe=probe))
            return receipt.to_dict()

    def test_padded_unknown_and_odd_lines_are_read_for_exactly_what_they_say(self) -> None:
        probe = (
            "  reference_executable=yes  \n"
            "workspace_writable=YES\n"  # only `yes` is yes
            "git_worktree=yes\n"
            "no_such_key=yes\n"
            "not a key value line\n"
            "\n"
            "git_remote=origin|https://example.test/a|b.git\n"
            "git_remote=bare\n"
            "egress=yes|6|curl -sS -m 5 https://github.com\n"
            "egress=no||wget -q -T 5 -O - https://github.com\n"
            "egress=yes|x1|git ls-remote https://github.com/a/b.git\n"
            "egress=yes|-1|getent hosts github.com\n"
            "egress=yes|7|sh -c a|b\n"
        )
        observed = self.verified(probe)
        self.assertEqual(
            {
                "reference_executable": True,
                "workspace_writable": False,
                "git_worktree": True,
                "git_remotes": [{"name": "origin", "url": "https://example.test/a|b.git"}, {"name": "bare", "url": ""}],
                "egress_probes": [
                    {"command": "curl -sS -m 5 https://github.com", "present": True, "returncode": 6},
                    {"command": "wget -q -T 5 -O - https://github.com", "present": False, "returncode": None},
                    {"command": "git ls-remote https://github.com/a/b.git", "present": True, "returncode": None},
                    {"command": "getent hosts github.com", "present": True, "returncode": None},
                    {"command": "sh -c a|b", "present": True, "returncode": 7},
                ],
            },
            {
                key: observed[key]
                for key in ("reference_executable", "workspace_writable", "git_worktree", "git_remotes", "egress_probes")
            },
        )

    def test_an_empty_probe_output_observes_nothing(self) -> None:
        observed = self.verified("")
        self.assertEqual(
            (False, False, False, [], []),
            (
                observed["reference_executable"],
                observed["workspace_writable"],
                observed["git_worktree"],
                observed["git_remotes"],
                observed["egress_probes"],
            ),
        )

    def test_the_probe_container_is_created_air_gapped_over_the_resolved_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "a" / ".." / "ws"
            (Path(temp_dir) / "a").mkdir()
            (Path(temp_dir) / "ws").mkdir()
            docker = FakeDocker(contents={})
            receipt = verify(INSTANCE, workspace, IMAGE, "sha256:d1ge57", REPOSITORY, run=docker)
            resolved = (Path(temp_dir) / "ws").resolve()

        self.assertEqual(
            ["docker", "create", "--network=none", "-v", f"{resolved}:/workspace", "-w", "/workspace", IMAGE, "sh", "-c"],
            receipt.probe_argv[:-1],
        )
        script = receipt.probe_argv[-1]
        self.assertIn("cd /workspace || exit 70", script)
        self.assertIn("if [ -x ./executable ]", script)
        self.assertIn("try git git ls-remote https://github.com/abishekvashok/cmatrix.git", script)
        self.assertEqual(
            [
                receipt.probe_argv,
                ["docker", "start", "--attach", "probe-container"],
                ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", "probe-container"],
                ["docker", "rm", "-f", "probe-container"],
            ],
            docker.calls,
        )
        self.assertEqual(
            (INSTANCE, IMAGE, "sha256:d1ge57", "none"),
            (receipt.instance_id, receipt.image, receipt.image_id, receipt.network_mode),
        )


class DockerFailureTests(unittest.TestCase):
    """A docker step that fails stops the cleanroom, says which step, and still cleans up."""

    def test_the_copy_container_that_cannot_be_created_stops_before_anything_is_copied(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), fail={"create": (125, "", "Error: pull access denied\n")})
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                materialize(INSTANCE, Path(temp_dir) / "ws", run=docker)
        self.assertEqual(f"docker create {IMAGE} failed (rc 125): Error: pull access denied", str(refusal.exception))
        self.assertIsNone(refusal.exception.receipt)
        self.assertEqual([["docker", "create", IMAGE, "true"]], docker.calls)

    def test_a_failed_copy_still_removes_the_copy_container(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), fail={"cp": (1, "partial\n", "no space left on device\n")})
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            with self.assertRaises(CleanroomError) as refusal:
                materialize(INSTANCE, workspace, run=docker)
        self.assertEqual(
            "docker cp copy-container:/workspace/. failed (rc 1): partial\nno space left on device", str(refusal.exception)
        )
        self.assertIn(["docker", "rm", "-f", "copy-container"], docker.calls)

    def test_an_image_whose_id_cannot_be_read_is_refused(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), fail={"image": (1, "", "Error: No such image\n")})
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                materialize(INSTANCE, Path(temp_dir) / "ws", run=docker)
        self.assertEqual("docker image inspect failed (rc 1): Error: No such image", str(refusal.exception))

    def test_materialize_answers_the_image_and_its_id_into_a_workspace_it_creates(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            # Parents too: a cell's workspace sits under directories nobody has made yet.
            workspace = Path(temp_dir) / "cells" / "a" / "ws"
            self.assertEqual((IMAGE, "sha256:d1ge57"), materialize(INSTANCE, workspace, run=docker))
            self.assertEqual([".git", "README.md", "doc", "executable"], sorted(entry.name for entry in workspace.iterdir()))

    def test_a_non_empty_workspace_is_named_with_what_it_holds(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "b").write_text("", encoding="utf-8")
            (workspace / "a").mkdir()
            with self.assertRaises(CleanroomError) as refusal:
                materialize(INSTANCE, workspace, run=docker)
        self.assertEqual(
            f"{workspace} is not empty; the cleanroom is not merged into existing state: ['a', 'b']", str(refusal.exception)
        )
        self.assertEqual([], docker.calls)

    def test_a_probe_container_that_cannot_be_created_stops_the_check(self) -> None:
        docker = FakeDocker(contents={}, fail={"probe": (1, "", "Error: invalid mount\n")})
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                verify(INSTANCE, Path(temp_dir), IMAGE, "sha256:d1ge57", REPOSITORY, run=docker)
        self.assertEqual("docker create --network=none failed (rc 1): Error: invalid mount", str(refusal.exception))
        self.assertEqual(1, len(docker.calls))

    def test_a_network_mode_that_cannot_be_read_fails_and_still_removes_the_probe_container(self) -> None:
        docker = FakeDocker(contents={}, fail={"inspect": (1, "", "Error: No such container\n")})
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                verify(INSTANCE, Path(temp_dir), IMAGE, "sha256:d1ge57", REPOSITORY, run=docker)
        self.assertEqual("docker inspect --format failed (rc 1): Error: No such container", str(refusal.exception))
        self.assertEqual(["docker", "rm", "-f", "probe-container"], docker.calls[-1])

    def test_a_probe_run_that_exits_nonzero_is_judged_on_what_it_printed(self) -> None:
        # An unreadable workspace makes the probe exit non-zero; what it printed is still the
        # observation, and the receipt decides from it.
        docker = FakeDocker(contents={}, fail={"start": (70, "", "sh: cd: /workspace: No such file\n")})
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt = verify(INSTANCE, Path(temp_dir), IMAGE, "sha256:d1ge57", REPOSITORY, run=docker)
        self.assertFalse(receipt.reference_executable)
        self.assertEqual([], receipt.egress_probes)
        self.assertEqual("none", receipt.network_mode)


CLI_INSTANCE = "fixture__tool.0000000"
CLI_IMAGE = "programbench/fixture_1776_tool.0000000:task_cleanroom_v6"


class MainTests(unittest.TestCase):
    """The command the workflow runs, with Docker stood in for at its one default."""

    def main(self, docker: FakeDocker, argv: list[str]) -> tuple[object, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cleanroom, "_subprocess_docker", docker), redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = cleanroom.main(argv)
        return returncode, stdout.getvalue(), stderr.getvalue()

    def test_a_sound_cleanroom_exits_zero_prints_what_it_proved_and_writes_the_receipt(self) -> None:
        probe = HEALTHY_PROBE + "git_remote=remote.origin.url|https://example.test/fixture/tool.git\n"
        docker = FakeDocker(contents=cleanroom_contents(), probe=probe)
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, receipt_path = Path(temp_dir) / "ws", Path(temp_dir) / "receipt.json"
            returncode, stdout, stderr = self.main(
                docker, ["--instance", CLI_INSTANCE, "--workspace", str(workspace), "--receipt", str(receipt_path)]
            )
            written = receipt_path.read_text(encoding="utf-8")

        self.assertEqual(0, returncode)
        self.assertEqual("", stderr)
        self.assertEqual(
            f"cleanroom ok   {CLI_IMAGE}\n"
            "  image id     sha256:d1ge57\n"
            "  network      none\n"
            "  reference    ./executable executable\n"
            "  git          worktree shipped by the image, not created here; https://example.test/fixture/tool.git\n"
            "  workspace    4 entries, writable\n"
            "  docs         README.md, doc\n"
            "  egress       curl rc 6; wget absent; git rc 128; getent rc 2\n",
            stdout,
        )
        receipt = json.loads(written)
        self.assertEqual(json.dumps(receipt, indent=2) + "\n", written)
        self.assertEqual("fixture__tool.0000000", receipt["instance_id"])
        self.assertEqual(CLI_IMAGE, receipt["image"])
        self.assertIn("try git git ls-remote https://github.com/fixture/tool.git", receipt["probe_argv"][-1])
        self.assertEqual([], CleanroomReceipt(**receipt).violations())

    def test_without_a_receipt_path_nothing_is_written_and_no_remote_says_so(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            returncode, stdout, _ = self.main(docker, ["--instance", CLI_INSTANCE, "--workspace", str(Path(temp_dir) / "ws")])
            self.assertEqual(["ws"], sorted(entry.name for entry in Path(temp_dir).iterdir()))
        self.assertEqual(0, returncode)
        self.assertIn("  git          worktree shipped by the image, not created here; no remotes\n", stdout)

    def test_a_refused_cleanroom_exits_one_says_why_and_still_writes_what_it_saw(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), network_mode="bridge")
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = Path(temp_dir) / "receipt.json"
            returncode, stdout, stderr = self.main(
                docker, ["--instance", CLI_INSTANCE, "--workspace", str(Path(temp_dir) / "ws"), "--receipt", str(receipt_path)]
            )
            written = receipt_path.read_text(encoding="utf-8")

        self.assertEqual(1, returncode)
        self.assertEqual("", stdout)
        self.assertEqual(
            "cleanroom preflight FAILED: the inference environment is not a ProgramBench cleanroom:\n"
            "  - model-facing tools are not air-gapped: NetworkMode='bridge', want 'none'\n",
            stderr,
        )
        receipt = json.loads(written)
        self.assertEqual(json.dumps(receipt, indent=2) + "\n", written)
        self.assertEqual("bridge", receipt["network_mode"])

    def test_a_refusal_with_nothing_observed_writes_no_receipt(self) -> None:
        # A workspace that is not empty fails before any container exists: there is no
        # observation to record, so no receipt file claims one.
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, receipt_path = Path(temp_dir) / "ws", Path(temp_dir) / "receipt.json"
            workspace.mkdir()
            (workspace / "leftover").write_text("", encoding="utf-8")
            returncode, stdout, stderr = self.main(
                docker, ["--instance", CLI_INSTANCE, "--workspace", str(workspace), "--receipt", str(receipt_path)]
            )
            self.assertFalse(receipt_path.exists())

        self.assertEqual(1, returncode)
        self.assertEqual("", stdout)
        self.assertEqual(
            f"cleanroom preflight FAILED: {workspace} is not empty; the cleanroom is not merged into existing state: ['leftover']\n",
            stderr,
        )
        self.assertEqual([], docker.calls)

    def test_a_refusal_without_a_receipt_path_writes_nothing(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), network_mode="bridge")
        with tempfile.TemporaryDirectory() as temp_dir:
            returncode, _, _ = self.main(docker, ["--instance", CLI_INSTANCE, "--workspace", str(Path(temp_dir) / "ws")])
            self.assertEqual(["ws"], sorted(entry.name for entry in Path(temp_dir).iterdir()))
        self.assertEqual(1, returncode)

    def test_the_instance_and_the_workspace_are_both_required(self) -> None:
        for argv in (["--workspace", "ws"], ["--instance", CLI_INSTANCE], []):
            with self.subTest(argv=argv):
                docker = FakeDocker(contents={})
                with self.assertRaises(SystemExit) as exit_:
                    self.main(docker, argv)
                self.assertEqual(2, exit_.exception.code)
                self.assertEqual([], docker.calls)


if __name__ == "__main__":
    unittest.main()
