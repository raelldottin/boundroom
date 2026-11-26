"""The containers the agent got, not the container we asked for.

Every test drives the witness against a stand-in for Docker, because the fact under test is
what the witness *concludes* from what the daemon reports - and what it has to conclude
about a reused, bridged or wrongly mounted container is a refusal.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from automation.benchmark.cleanroom import CleanroomError
from automation.benchmark.witness import RELEASE_PREFIX, WITNESS_SUFFIX, SandboxWitness, container_record, witness_receipt

IMAGE = "programbench/abishekvashok_1776_cmatrix.5c082c6:task_cleanroom_v6"
IMAGE_ID = "sha256:4ef6d754"
CONTAINER = "c0ffee" * 10
PROBE_CONTAINER = "decade" * 10
SESSION_CONTAINER = "bedead" * 10
# What `container_persistent: false` keys an agent turn's own tool calls by.
SESSION_TASK_ID = "32c58fde-13c6-4448-bbb5-b90482b19290"


class FakeDocker:
    """A Docker whose container list the test changes while the session 'runs'."""

    def __init__(
        self,
        *,
        containers: list[str],
        inspect: dict,
        unremovable: tuple[str, ...] = (),
        reclaims: bool = True,
        drop_after_inspect: bool = False,
        ps: OSError | int | None = None,
        inspect_fails: tuple[str, ...] = (),
        rm_raises: bool = False,
    ) -> None:
        self.containers = containers
        # One record per container id, or one record for whatever is asked about.
        self.inspect = inspect
        # Containers `docker rm -f` cannot get rid of: a wedged runtime, a shutting-down
        # daemon. The cell has to notice rather than hand the next phase a busy runner.
        self.unremovable = unremovable
        # Whether the `docker run` that hands the workspace back to the harness succeeds.
        self.reclaims = reclaims
        # What `docker_persist_across_processes: false` does at exit, once the witness has
        # seen what it is there to see: the container is gone the moment it was inspected.
        self.drop_after_inspect = drop_after_inspect
        # `docker ps` that cannot answer: an exception to raise (no Docker), or a return code.
        self.ps = ps
        # Containers gone between the listing and the inspect.
        self.inspect_fails = inspect_fails
        # `docker rm -f` that cannot even be started.
        self.rm_raises = rm_raises
        self.calls: list[list[str]] = []
        # Notified after every call, so a 'session' can wait on what the witness has done
        # instead of sleeping until it has probably done it.
        self.called = threading.Condition()

    def create(self, *containers: str) -> None:
        with self.called:
            self.containers.extend(containers)

    def remove(self, container: str) -> None:
        with self.called:
            self.containers.remove(container)

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        with self.called:
            try:
                return self._answer(argv)
            finally:
                self.called.notify_all()

    def _answer(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[1] == "ps":
            if isinstance(self.ps, OSError):
                raise self.ps
            if self.ps is not None:
                # Whatever it printed before failing is not a listing to trust.
                listing = "".join(f"{container}\n" for container in self.containers)
                return self.ps, listing or "partial\n", "Cannot connect to the Docker daemon\n"
            return 0, "".join(f"{container}\n" for container in self.containers), ""
        if argv[1] == "inspect":
            if argv[-1] in self.inspect_fails:
                return 1, "", f"Error: No such object: {argv[-1]}\n"
            record = self.inspect.get(argv[-1], self.inspect) if self.inspect else {}
            if self.drop_after_inspect and argv[-1] in self.containers:
                self.containers.remove(argv[-1])
            return 0, json.dumps(record) + "\n", ""
        if argv[1] == "rm":
            if self.rm_raises:
                raise OSError("docker: exec format error")
            container = argv[-1]
            if container in self.unremovable:
                return 1, "", f"Error response from daemon: cannot remove {container}\n"
            self.containers.remove(container)
            return 0, f"{container}\n", ""
        if argv[1] == "run":
            return (0, "", "") if self.reclaims else (125, "", "docker: Error response from daemon: no such image\n")
        raise AssertionError(f"unexpected docker call: {argv}")


def inspected(workspace: Path, *, container: str = CONTAINER, task_id: str = "default", **overrides) -> dict:
    raw = {
        "container_id": container,
        "created_at": "2026-09-28T12:14:57.1Z",
        "image_ref": IMAGE,
        "image_id": IMAGE_ID,
        "network_mode": "none",
        "mounts": [{"Destination": "/workspace", "Source": str(workspace), "RW": True, "Type": "bind"}],
        "labels": {"hermes-agent": "1", "hermes-task-id": task_id, "org.opencontainers.image.title": "cmatrix"},
    }
    raw.update(overrides)
    return raw


class WitnessTests(unittest.TestCase):
    def watch(
        self,
        docker: FakeDocker,
        workspace: Path,
        sessions: Path,
        *,
        creates: tuple[str, ...] = (CONTAINER,),
    ) -> SandboxWitness:
        """Run one 'session' that creates its containers the way Hermes does: partway in.

        No waiting: whatever the polls missed, the witness's last look after the session
        ends still lists it, so every container created here is observed.
        """
        witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01)

        def session(command, workspace_arg, env, timeout) -> int:
            docker.create(*creates)
            return 0

        witness.wrap(session)("hermes", workspace, {}, 300)
        return witness

    def written(self, sessions: Path) -> dict:
        receipts = list(sessions.glob(f"*{WITNESS_SUFFIX}"))
        self.assertEqual(1, len(receipts), f"expected one witness receipt, found {receipts}")
        return json.loads(receipts[0].read_text(encoding="utf-8"))

    def test_the_container_the_agent_was_given_is_recorded_while_it_still_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "agent-sessions"
            workspace.mkdir()
            sessions.mkdir()
            (sessions / "20260928T121455Z-3087.controls.json").write_text("{}", encoding="utf-8")
            docker = FakeDocker(containers=[], inspect=inspected(workspace), drop_after_inspect=True)

            witness = self.watch(docker, workspace, sessions)

            self.assertEqual([], witness.violations)
            # Named for the session that created it: run_agent.sh writes its controls
            # report before it launches the agent, so the newest one names this session.
            receipt = json.loads((sessions / f"20260928T121455Z-3087{WITNESS_SUFFIX}").read_text(encoding="utf-8"))
            self.assertTrue(receipt["observed"])
            self.assertTrue(receipt["default_backend_verified"])
            self.assertTrue(receipt["default_backend_removed_after_exit"])
            record = receipt["containers"][0]
            self.assertEqual("default", record["task_id"])
            self.assertFalse(record["existed_in_baseline"])
            self.assertEqual(CONTAINER, record["container_id"])
            self.assertEqual(IMAGE_ID, record["image_id"])
            self.assertEqual("none", record["network_mode"])
            self.assertEqual({"source": str(workspace), "destination": "/workspace", "rw": True}, record["workspace_mount"])
            self.assertEqual({"hermes-agent": "1", "hermes-task-id": "default"}, record["labels"])

    def test_every_backend_the_session_built_is_recorded_not_just_the_first(self) -> None:
        # Hermes builds one container per task id: the system prompt's own probe gets
        # `prompt-backend-probe`, and the tools the model calls get `default`. The probe's
        # container appears first, so first-container-wins recorded the one that proves
        # nothing about the backend serving terminal, file and code_execution.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(
                containers=[],
                inspect={
                    PROBE_CONTAINER: inspected(workspace, container=PROBE_CONTAINER, task_id="prompt-backend-probe"),
                    CONTAINER: inspected(workspace),
                },
            )

            witness = self.watch(docker, workspace, sessions, creates=(PROBE_CONTAINER, CONTAINER))

            receipt = self.written(sessions)
            self.assertEqual(
                ["prompt-backend-probe", "default"], sorted((r["task_id"] for r in receipt["containers"]), reverse=True)
            )
            self.assertTrue(receipt["default_backend_verified"])
            self.assertEqual([], witness.violations)

    def test_a_session_that_only_built_the_prompt_probe_has_not_proved_the_default_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            probe = inspected(workspace, container=PROBE_CONTAINER, task_id="prompt-backend-probe")
            docker = FakeDocker(containers=[], inspect={PROBE_CONTAINER: probe})

            self.watch(docker, workspace, sessions, creates=(PROBE_CONTAINER,))

            receipt = self.written(sessions)
            self.assertTrue(receipt["observed"])
            self.assertFalse(receipt["default_backend_verified"])
            self.assertIsNone(receipt["default_backend_removed_after_exit"])

    def test_the_session_scoped_backend_is_scored_apart_from_the_default_one(self) -> None:
        # Under `container_persistent: false` a tool call inside an agent turn is keyed by
        # session id, not by "default", and reaches the mount logic by a different path.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(
                containers=[],
                inspect={
                    CONTAINER: inspected(workspace),
                    SESSION_CONTAINER: inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
                },
            )

            witness = self.watch(docker, workspace, sessions, creates=(CONTAINER, SESSION_CONTAINER))

            receipt = self.written(sessions)
            self.assertTrue(receipt["default_backend_verified"])
            self.assertTrue(receipt["session_backend_verified"])
            self.assertEqual([], witness.violations)

    def test_a_probe_backend_too_short_lived_to_poll_is_still_inspected(self) -> None:
        """The barrier run 37275443406 needed: the container waits to be inspected.

        A probe container lives for exactly one `docker exec` - 267ms for the
        session-scoped one in that run, against a 500ms poll - so sampling decided whether
        a sound cleanroom could be proved. Here each container is removed the instant it is
        released, which is far inside the poll interval below, and both are witnessed
        anyway. Without the release this test cannot pass: the probe never gets its marker
        and reports the barrier timeout instead.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            barrier = workspace / ".sandbox-probe-barrier"
            docker = FakeDocker(
                containers=[],
                inspect={
                    CONTAINER: inspected(workspace),
                    SESSION_CONTAINER: inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
                },
            )
            witness = SandboxWitness(
                sessions,
                workspace,
                IMAGE,
                IMAGE_ID,
                run=docker,
                # Deliberately slower than anything the fake probe does: the only reason a
                # container is still there to inspect is that it is waiting to be.
                poll_seconds=0.2,
                stem="sandbox-probe",
                release_dir=barrier,
            )
            recorded_at_release = []

            def probe(command, workspace_arg, env, timeout) -> int:
                """What `run_agent.sh --sandbox-probe` does: two backends, one exec each."""
                for release, container in enumerate((CONTAINER, SESSION_CONTAINER), start=1):
                    marker = barrier / f"{RELEASE_PREFIX}{release}"
                    self.assertFalse(marker.exists(), f"{marker.name} existed before the container it releases")
                    docker.create(container)
                    # The marker is written before the witness's next docker call, and every
                    # call wakes this; the cap is the probe's own bounded barrier.
                    with docker.called:
                        released = docker.called.wait_for(marker.exists, timeout=5)
                    if not released:
                        return 75  # the bounded barrier timeout, as the probe reports it
                    recorded_at_release.append(len(witness.containers))
                    docker.remove(container)
                return 0

            self.assertEqual(0, witness.wrap(probe)("hermes", workspace, {}, 300))

            # Released once inspected, never before: the nth marker appears only after the
            # nth container has been recorded with its `docker inspect` output.
            self.assertEqual([1, 2], recorded_at_release)
            receipt = self.written(sessions)
            self.assertTrue(receipt["default_backend_verified"])
            self.assertTrue(receipt["session_backend_verified"])
            self.assertEqual([], witness.violations)

    def test_a_session_backend_on_a_tmpfs_is_caught_while_the_default_one_looks_sound(self) -> None:
        # Run 36485906813 exactly: `docker_mount_cwd_to_workspace` bound /workspace for the
        # CLI parent's "default" backend, and `_resolve_task_host_cwd` refused to derive it
        # for the session-scoped one, which got an empty tmpfs instead.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            tmpfs = inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID, mounts=[])
            docker = FakeDocker(containers=[], inspect={CONTAINER: inspected(workspace), SESSION_CONTAINER: tmpfs})

            witness = self.watch(docker, workspace, sessions, creates=(CONTAINER, SESSION_CONTAINER))

            receipt = self.written(sessions)
            self.assertTrue(receipt["default_backend_verified"])
            self.assertFalse(receipt["session_backend_verified"])
            self.assertEqual(["the agent's container has nothing mounted at /workspace"], witness.violations)

    def test_a_container_that_outlives_its_process_is_reaped_without_losing_that_it_did(self) -> None:
        # Two facts, both kept. `removed_after_exit` is the runtime reading of
        # `docker_persist_across_processes: false` - a container the daemon still lists is
        # one the next process attaches to by label - and rewriting it to say the harness
        # tidied up would erase the only direct evidence there is.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))

            witness = self.watch(docker, workspace, sessions)

            receipt = self.written(sessions)
            self.assertFalse(receipt["default_backend_removed_after_exit"])
            self.assertEqual(
                {
                    "removed_after_exit": False,
                    "cleanup_attempted": True,
                    "removed_by_harness": True,
                    "removed_after_cleanup": True,
                },
                {
                    key: receipt["containers"][0][key]
                    for key in ("removed_after_exit", "cleanup_attempted", "removed_by_harness", "removed_after_cleanup")
                },
            )
            self.assertEqual([["docker", "rm", "-f", CONTAINER]], [c for c in docker.calls if c[1] == "rm"])
            self.assertEqual([], witness.violations)
            self.assertEqual([], docker.containers)

    def test_a_phase_is_reaped_at_its_own_exit_not_at_the_end_of_the_lane(self) -> None:
        # C-E run several agent invocations through one witness. A timed-out Research
        # container left running while Plan is measured on the same runner is the thing the
        # reap exists to stop, so it happens per invocation - and the second invocation must
        # not re-answer whether the first one's container outlived it.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(
                containers=[],
                inspect={
                    CONTAINER: inspected(workspace),
                    SESSION_CONTAINER: inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
                },
            )
            witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01)

            def phase(container: str):
                def session(command, workspace_arg, env, timeout) -> int:
                    docker.create(container)
                    return 0

                return witness.wrap(session)

            phase(CONTAINER)("hermes", workspace, {}, 300)
            self.assertEqual([], docker.containers, "the first phase's container is still running during the second")
            phase(SESSION_CONTAINER)("hermes", workspace, {}, 300)

            records = {record["container_id"]: record for record in self.written(sessions)["containers"]}
            self.assertEqual([False, False], [records[c]["removed_after_exit"] for c in (CONTAINER, SESSION_CONTAINER)])
            self.assertEqual([True, True], [records[c]["removed_after_cleanup"] for c in (CONTAINER, SESSION_CONTAINER)])
            self.assertEqual(2, len([call for call in docker.calls if call[1] == "rm"]))
            self.assertEqual([], witness.violations)

    def test_a_container_the_harness_cannot_remove_refuses_the_cell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace), unremovable=(CONTAINER,))

            witness = self.watch(docker, workspace, sessions)

            receipt = self.written(sessions)
            record = receipt["containers"][0]
            self.assertTrue(record["cleanup_attempted"])
            self.assertFalse(record["removed_by_harness"])
            self.assertFalse(record["removed_after_cleanup"])
            self.assertEqual([f"the container {CONTAINER} outlived its session and could not be removed"], witness.violations)

    def test_the_workspace_is_handed_back_to_the_harness_once_nothing_can_write_to_it(self) -> None:
        # The agent's tools run as the image's root, so on a Linux runner everything they
        # create in the bind mount is root's. Run 40784747377 died on the harness's next
        # write: research had made `.rpi`, and installing `.rpi/build.sh` before implement
        # raised PermissionError. After the reap, so no leaked container is still writing.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))

            witness = self.watch(docker, workspace, sessions)

            verbs = [call[1] for call in docker.calls if call[1] in ("rm", "run")]
            self.assertEqual(["rm", "run"], verbs)
            run = next(call for call in docker.calls if call[1] == "run")
            self.assertEqual(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "-v",
                    f"{workspace.resolve()}:/workspace",
                    "--entrypoint",
                    "chown",
                    IMAGE_ID,
                    "-R",
                    "-h",
                    f"{os.getuid()}:{os.getgid()}",
                    "/workspace",
                ],
                run,
            )
            self.assertEqual([], witness.violations)

    def test_a_workspace_the_harness_cannot_take_back_stops_the_cell_there(self) -> None:
        # Carrying on would only move the failure to the harness's next write, as a bare
        # PermissionError that says nothing about why.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace), drop_after_inspect=True, reclaims=False)

            with self.assertRaises(CleanroomError) as refusal:
                self.watch(docker, workspace, sessions)

            self.assertIn("could not hand the workspace back", str(refusal.exception))
            self.assertIn("no such image", str(refusal.exception))

    def test_a_session_that_ended_cleanly_is_not_reaped(self) -> None:
        # Hermes removed its own container, so there is nothing to clean up and the receipt
        # should not claim the harness did anything.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace), drop_after_inspect=True)

            self.watch(docker, workspace, sessions)

            record = self.written(sessions)["containers"][0]
            self.assertTrue(record["removed_after_exit"])
            self.assertFalse(record["cleanup_attempted"])
            self.assertIsNone(record["removed_after_cleanup"])

    def test_containers_that_were_here_before_the_cell_are_never_reaped(self) -> None:
        # The reap works off this witness's own ids. A `hermes-agent=1` sweep would also
        # take a container another cell is still working in.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[PROBE_CONTAINER], inspect=inspected(workspace))

            self.watch(docker, workspace, sessions)

            self.assertEqual([["docker", "rm", "-f", CONTAINER]], [c for c in docker.calls if c[1] == "rm"])
            self.assertIn(PROBE_CONTAINER, docker.containers)

    def test_the_container_environment_is_never_asked_for(self) -> None:
        # The provider key lives in the agent's environment. The witness reads the container
        # from outside, and a receipt beside the submission is the last place it may surface.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))
            self.watch(docker, workspace, sessions)
            inspect = next(argv for argv in docker.calls if argv[1] == "inspect")
            self.assertNotIn("Env", inspect[3])
            self.assertNotIn("Env", json.dumps(self.written(sessions)))

    def test_a_container_on_a_bridge_network_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace, network_mode="bridge"))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("NetworkMode='bridge'", witness.violations[0])
            receipt = self.written(sessions)
            self.assertTrue(receipt["observed"])
            self.assertFalse(receipt["default_backend_verified"])

    def test_a_container_running_another_image_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace, image_id="sha256:something-else"))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("not the cleanroom image", witness.violations[0])

    def test_a_workspace_from_somewhere_other_than_this_cell_is_a_hard_failure(self) -> None:
        # A container reused across cells keeps the *first* cell's bind mount, so the second
        # cell's agent would be writing into the first cell's workspace.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            stale = [{"Destination": "/workspace", "Source": "/tmp/pb-another-cell", "RW": True}]
            docker = FakeDocker(containers=[], inspect=inspected(workspace, mounts=stale))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("not this cell's workspace", witness.violations[0])

    def test_a_read_only_workspace_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            frozen = [{"Destination": "/workspace", "Source": str(workspace), "RW": False}]
            docker = FakeDocker(containers=[], inspect=inspected(workspace, mounts=frozen))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("read-only", witness.violations[0])

    def test_nothing_mounted_at_the_workspace_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace, mounts=[]))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("nothing mounted at /workspace", witness.violations[0])

    def test_a_session_that_created_no_container_says_so_rather_than_inventing_one(self) -> None:
        # An agent that never calls a tool never gets a container. That is a fact about the
        # model's turn, not about the sandbox, so it is recorded and not refused.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect={})
            witness = self.watch(docker, workspace, sessions, creates=())

            self.assertEqual([], witness.violations)
            self.assertEqual({"ps"}, {call[1] for call in docker.calls})
            self.assertEqual(
                {
                    "observed": False,
                    "default_backend_verified": False,
                    "session_backend_verified": False,
                    "default_backend_removed_after_exit": None,
                    "existing_containers": [],
                    "containers": [],
                },
                self.written(sessions),
            )

    def test_a_session_that_reused_an_existing_container_is_a_hard_failure(self) -> None:
        # Reuse leaves no new container, so it looks exactly like "no container" except for
        # what was already lying around - which is the only evidence there is.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=["leftover-from-an-earlier-process"], inspect={})
            witness = self.watch(docker, workspace, sessions, creates=())

            self.assertIn("no container was created and 1 Hermes container(s) were already present", witness.violations[0])
            self.assertEqual(["leftover-from-an-earlier-process"], self.written(sessions)["existing_containers"])

    def test_a_container_that_was_already_running_is_not_mistaken_for_this_cell_s(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=["someone-elses"], inspect=inspected(workspace))
            witness = self.watch(docker, workspace, sessions)

            receipt = self.written(sessions)
            self.assertEqual([CONTAINER], [record["container_id"] for record in receipt["containers"]])
            self.assertEqual([], witness.violations)

    def test_the_probe_names_its_own_receipt_rather_than_borrowing_a_session_s(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))
            witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01, stem="sandbox-probe")

            def session(command, workspace_arg, env, timeout) -> int:
                docker.create(CONTAINER)
                return 0

            witness.wrap(session)("probe", workspace, {}, 300)

            self.assertTrue((sessions / f"sandbox-probe{WITNESS_SUFFIX}").is_file())


def recorded(workspace: Path, *, container: object = CONTAINER, task_id: object = "default", **fields: object) -> dict:
    """The record the witness keeps for one sound container, with whatever the test changes."""
    record = {
        "task_id": task_id,
        "container_id": container,
        "created_at": "2026-09-28T12:14:57.1Z",
        "requested_image": IMAGE,
        "image_ref": IMAGE,
        "image_id": IMAGE_ID,
        "network_mode": "none",
        "workspace_mount": {"source": str(workspace), "destination": "/workspace", "rw": True},
        "labels": {"hermes-agent": "1", "hermes-task-id": task_id},
        "existed_in_baseline": False,
        "removed_after_exit": None,
        "cleanup_attempted": False,
        "removed_by_harness": False,
        "removed_after_cleanup": None,
        "violations": [],
    }
    record.update(fields)
    return record


class ContainerRecordTests(unittest.TestCase):
    """What one `docker inspect` answer becomes in the receipt, apart from any Docker."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def record(self, raw: dict) -> dict:
        return container_record(raw, requested_image=IMAGE, image_id=IMAGE_ID, workspace=self.workspace)

    def test_a_sound_container_is_recorded_field_by_field_in_receipt_order(self) -> None:
        record = self.record(inspected(self.workspace))
        self.assertEqual(recorded(self.workspace), record)
        self.assertEqual(list(recorded(self.workspace)), list(record))

    def test_each_thing_wrong_with_a_container_is_named_exactly(self) -> None:
        other_cell = [{"Destination": "/workspace", "Source": "/tmp/pb-another-cell", "RW": True}]
        ws = repr(str(self.workspace))
        cases: list[tuple[dict[str, Any], list[str]]] = [
            (
                {"image_id": "sha256:else"},
                [f"the agent's container runs sha256:else, not the cleanroom image {IMAGE_ID}"],
            ),
            (
                {"network_mode": "bridge"},
                ["the agent's container is on NetworkMode='bridge', not 'none'"],
            ),
            ({"mounts": []}, ["the agent's container has nothing mounted at /workspace"]),
            ({"mounts": None}, ["the agent's container has nothing mounted at /workspace"]),
            (
                {"mounts": [{"Destination": "/data", "Source": str(self.workspace), "RW": True}]},
                ["the agent's container has nothing mounted at /workspace"],
            ),
            ({"mounts": other_cell}, [f"/workspace comes from '/tmp/pb-another-cell', not this cell's workspace {ws}"]),
            (
                {"mounts": [{"Destination": "/workspace", "RW": True}]},
                [f"/workspace comes from None, not this cell's workspace {ws}"],
            ),
            (
                {"mounts": [{"Destination": "/workspace", "Source": str(self.workspace), "RW": False}]},
                ["/workspace is mounted read-only; the agent cannot produce a submission in it"],
            ),
            (
                {"mounts": [{"Destination": "/workspace", "Source": str(self.workspace)}]},
                ["/workspace is mounted read-only; the agent cannot produce a submission in it"],
            ),
            (
                {"mounts": [{"Destination": "/workspace", "Source": "/elsewhere", "RW": False}]},
                [
                    f"/workspace comes from '/elsewhere', not this cell's workspace {ws}",
                    "/workspace is mounted read-only; the agent cannot produce a submission in it",
                ],
            ),
            (
                # Nothing mounted ends the mount checks: there is no source or mode to judge.
                {"image_id": None, "network_mode": "host", "mounts": []},
                [
                    f"the agent's container runs None, not the cleanroom image {IMAGE_ID}",
                    "the agent's container is on NetworkMode='host', not 'none'",
                    "the agent's container has nothing mounted at /workspace",
                ],
            ),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(expected, self.record(inspected(self.workspace, **overrides))["violations"])

    def test_a_workspace_reached_through_a_symlink_is_this_cell_s_workspace(self) -> None:
        # Docker answers with the path it was handed; the harness's temp root is a symlink
        # on some hosts. The record keeps what Docker said, and the check resolves it.
        link = self.root / "link"
        link.symlink_to(self.workspace, target_is_directory=True)
        record = self.record(inspected(self.workspace, mounts=[{"Destination": "/workspace", "Source": str(link), "RW": True}]))
        self.assertEqual([], record["violations"])
        self.assertEqual({"source": str(link), "destination": "/workspace", "rw": True}, record["workspace_mount"])

    def test_a_workspace_handed_over_through_a_symlink_is_still_this_cell_s(self) -> None:
        link = self.root / "ws-link"
        link.symlink_to(self.workspace, target_is_directory=True)
        record = container_record(inspected(self.workspace), requested_image=IMAGE, image_id=IMAGE_ID, workspace=link)
        self.assertEqual([], record["violations"])

    def test_only_hermes_labels_are_kept_and_the_workspace_mount_is_picked_out(self) -> None:
        raw = inspected(
            self.workspace,
            labels={"hermes-agent": "1", "hermes-task-id": SESSION_TASK_ID, "com.docker.compose": "z"},
            mounts=[
                {"Destination": "/cache", "Source": "/var/cache", "RW": False},
                {"Destination": "/workspace", "Source": str(self.workspace), "RW": True, "Type": "bind"},
            ],
        )
        record = self.record(raw)
        self.assertEqual({"hermes-agent": "1", "hermes-task-id": SESSION_TASK_ID}, record["labels"])
        self.assertEqual(SESSION_TASK_ID, record["task_id"])
        self.assertEqual({"source": str(self.workspace), "destination": "/workspace", "rw": True}, record["workspace_mount"])

    def test_an_empty_inspect_answer_is_recorded_as_nothing_known(self) -> None:
        self.assertEqual(
            recorded(
                self.workspace,
                task_id=None,
                container_id=None,
                created_at=None,
                image_ref=None,
                image_id=None,
                network_mode=None,
                workspace_mount=None,
                labels={},
                violations=[
                    f"the agent's container runs None, not the cleanroom image {IMAGE_ID}",
                    "the agent's container is on NetworkMode=None, not 'none'",
                    "the agent's container has nothing mounted at /workspace",
                ],
            ),
            self.record({}),
        )


def backend(task_id: object, *, violations: tuple[str, ...] = (), removed: object = None) -> dict:
    """The fields of a record the receipt reads."""
    return {"task_id": task_id, "violations": list(violations), "removed_after_exit": removed}


class WitnessReceiptTests(unittest.TestCase):
    """The summary drawn from the records, apart from any Docker."""

    def test_a_witness_that_saw_nothing_says_so_in_receipt_order(self) -> None:
        receipt = witness_receipt([], set())
        self.assertEqual(
            {
                "observed": False,
                "default_backend_verified": False,
                "session_backend_verified": False,
                "default_backend_removed_after_exit": None,
                "existing_containers": [],
                "containers": [],
            },
            receipt,
        )
        self.assertEqual(
            [
                "observed",
                "default_backend_verified",
                "session_backend_verified",
                "default_backend_removed_after_exit",
                "existing_containers",
                "containers",
            ],
            list(receipt),
        )

    def test_the_containers_already_present_are_listed_sorted(self) -> None:
        self.assertEqual(["a", "b", "c"], witness_receipt([], {"c", "a", "b"})["existing_containers"])

    def test_the_default_backend_is_removed_after_exit_only_when_every_one_of_them_is(self) -> None:
        cases = [
            ([backend("default", removed=True), backend("default", removed=True)], True),
            ([backend("default", removed=True), backend("default", removed=False)], False),
            ([backend("default", removed=True), backend("default", removed=None)], None),
            ([backend("default", removed=False), backend("default", removed=None)], None),
            # Another backend's answer says nothing about the default one's.
            ([backend("prompt-backend-probe", removed=True), backend(SESSION_TASK_ID, removed=True)], None),
            ([backend("default", removed=False), backend(SESSION_TASK_ID, removed=None)], False),
        ]
        for containers, expected in cases:
            with self.subTest(containers=containers):
                self.assertEqual(expected, witness_receipt(containers, set())["default_backend_removed_after_exit"])

    def test_which_backends_are_verified_is_decided_per_backend(self) -> None:
        bad = ("wrong image",)
        cases = [
            ([backend("default")], (True, True, False)),
            ([backend("default"), backend("default", violations=bad)], (True, False, False)),
            ([backend("prompt-backend-probe")], (True, False, False)),
            ([backend(SESSION_TASK_ID)], (True, False, True)),
            ([backend(SESSION_TASK_ID), backend("other-session", violations=bad)], (True, False, False)),
            # A container with no task id, or an empty one, is not keyed by a session.
            ([backend(None)], (True, False, False)),
            ([backend("")], (True, False, False)),
            ([backend("default"), backend("prompt-backend-probe", violations=bad), backend(SESSION_TASK_ID)], (True, True, True)),
        ]
        for containers, expected in cases:
            with self.subTest(containers=containers):
                receipt = witness_receipt(containers, set())
                self.assertEqual(
                    expected, (receipt["observed"], receipt["default_backend_verified"], receipt["session_backend_verified"])
                )
                self.assertEqual(containers, receipt["containers"])


class WitnessEdgeTests(unittest.TestCase):
    """What the witness does when Docker, the filesystem or the session answers oddly."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.sessions = self.root / "agent-sessions"

    def witness(self, docker: FakeDocker, **options) -> SandboxWitness:
        return SandboxWitness(self.sessions, self.workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01, **options)

    def run_session(self, witness: SandboxWitness, docker: FakeDocker, *containers: str) -> int:
        def session(command, workspace_arg, env, timeout) -> int:
            docker.create(*containers)
            return 0

        return witness.wrap(session)("hermes", self.workspace, {}, 300)

    def receipt_files(self) -> list[str]:
        return sorted(path.name for path in self.sessions.glob(f"*{WITNESS_SUFFIX}"))

    def test_a_host_without_docker_witnesses_nothing_and_claims_nothing(self) -> None:
        for index, answer in enumerate((OSError("[Errno 2] No such file or directory: 'docker'"), 1)):
            with self.subTest(ps=answer):
                docker = FakeDocker(containers=[], inspect=inspected(self.workspace), ps=answer)
                witness = self.witness(docker, stem=f"no-docker-{index}")
                self.run_session(witness, docker, CONTAINER)

                self.assertEqual(witness_receipt([], set()), witness.receipt)
                self.assertEqual([], witness.violations)
                self.assertEqual([], [call for call in docker.calls if call[1] in {"rm", "run"}])

    def test_a_container_gone_before_its_inspect_is_not_claimed(self) -> None:
        docker = FakeDocker(containers=[], inspect=inspected(self.workspace), inspect_fails=(CONTAINER,))
        witness = self.witness(docker)

        def session(command, workspace_arg, env, timeout) -> int:
            docker.create(CONTAINER)
            # Ended only once the witness has tried it, so its last look comes after.
            with docker.called:
                docker.called.wait_for(lambda: any(call[1] == "inspect" for call in docker.calls), timeout=5)
            return 0

        witness.wrap(session)("hermes", self.workspace, {}, 300)

        self.assertEqual([], witness.containers)
        self.assertFalse(witness.receipt["observed"])

    def test_a_removal_that_cannot_be_started_is_not_claimed_and_refuses_the_cell(self) -> None:
        docker = FakeDocker(containers=[], inspect=inspected(self.workspace), rm_raises=True)
        witness = self.witness(docker)
        self.run_session(witness, docker, CONTAINER)

        record = witness.containers[0]
        self.assertEqual(
            (False, True, False, False),
            (
                record["removed_after_exit"],
                record["cleanup_attempted"],
                record["removed_by_harness"],
                record["removed_after_cleanup"],
            ),
        )
        self.assertEqual([f"the container {CONTAINER} outlived its session and could not be removed"], witness.violations)

    def test_the_newest_controls_report_names_the_receipt_not_the_last_by_name(self) -> None:
        self.sessions.mkdir()
        newest, oldest = self.sessions / "a.controls.json", self.sessions / "b.controls.json"
        for path, mtime in ((newest, 2_000_000_000), (oldest, 1_000_000_000)):
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (mtime, mtime))
        docker = FakeDocker(containers=[], inspect=inspected(self.workspace), drop_after_inspect=True)
        self.run_session(self.witness(docker), docker, CONTAINER)

        self.assertEqual([f"a{WITNESS_SUFFIX}"], self.receipt_files())

    def test_with_no_controls_report_the_receipt_is_unattributed_and_its_directory_is_made(self) -> None:
        self.sessions = self.root / "runs" / "1" / "agent-sessions"
        docker = FakeDocker(containers=[], inspect=inspected(self.workspace), drop_after_inspect=True)
        self.run_session(self.witness(docker), docker, CONTAINER)

        self.assertEqual([f"unattributed{WITNESS_SUFFIX}"], self.receipt_files())

    def test_each_recorded_container_releases_the_next_numbered_marker(self) -> None:
        release_dir = self.workspace / "probe" / ".sandbox-probe-barrier"
        docker = FakeDocker(
            containers=[],
            inspect={
                CONTAINER: inspected(self.workspace),
                SESSION_CONTAINER: inspected(self.workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
            },
            drop_after_inspect=True,
        )
        witness = self.witness(docker, stem="sandbox-probe", release_dir=release_dir)
        self.run_session(witness, docker, CONTAINER, SESSION_CONTAINER)

        self.assertEqual([f"{RELEASE_PREFIX}1", f"{RELEASE_PREFIX}2"], sorted(path.name for path in release_dir.iterdir()))
        self.assertEqual("", (release_dir / f"{RELEASE_PREFIX}1").read_text(encoding="utf-8"))

    def test_the_receipt_is_on_disk_before_a_failed_hand_back_stops_the_cell(self) -> None:
        docker = FakeDocker(containers=[], inspect=inspected(self.workspace), drop_after_inspect=True, reclaims=False)
        witness = self.witness(docker, stem="cell")

        with self.assertRaises(CleanroomError) as refusal:
            self.run_session(witness, docker, CONTAINER)

        self.assertEqual(
            "could not hand the workspace back to the harness (rc 125): docker: Error response from daemon: no such image",
            str(refusal.exception),
        )
        written = json.loads((self.sessions / f"cell{WITNESS_SUFFIX}").read_text(encoding="utf-8"))
        self.assertEqual(witness.receipt, written)
        self.assertTrue(written["default_backend_removed_after_exit"])

    def test_the_wrapped_runner_gets_its_arguments_in_order_and_its_answer_is_kept(self) -> None:
        docker = FakeDocker(containers=[], inspect={})
        seen = []

        def runner(command, workspace, env, timeout) -> int:
            seen.append((command, workspace, env, timeout))
            return 7

        answer = self.witness(docker).wrap(runner)("hermes chat", self.workspace, {"HOME": "/h"}, 900)

        self.assertEqual(7, answer)
        self.assertEqual([("hermes chat", self.workspace, {"HOME": "/h"}, 900)], seen)

    def test_two_leaked_backends_are_reaped_reclaimed_and_written_whole(self) -> None:
        # The longest path: two backends, neither removed by Hermes, both reaped by id,
        # then the workspace handed back by image id, and one receipt that says all of it.
        docker = FakeDocker(
            containers=[],
            inspect={
                CONTAINER: inspected(self.workspace),
                SESSION_CONTAINER: inspected(self.workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
            },
        )
        witness = self.witness(docker, stem="20260928T121455Z-3087")
        self.run_session(witness, docker, CONTAINER, SESSION_CONTAINER)

        reaped = {
            "removed_after_exit": False,
            "cleanup_attempted": True,
            "removed_by_harness": True,
            "removed_after_cleanup": True,
        }
        expected = {
            "observed": True,
            "default_backend_verified": True,
            "session_backend_verified": True,
            "default_backend_removed_after_exit": False,
            "existing_containers": [],
            # One listing showed both, and a listing is taken in id order.
            "containers": [
                recorded(self.workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID, **reaped),
                recorded(self.workspace, **reaped),
            ],
        }
        path = self.sessions / f"20260928T121455Z-3087{WITNESS_SUFFIX}"
        written = path.read_text(encoding="utf-8")
        self.assertEqual(json.dumps(expected, indent=2) + "\n", written)
        self.assertEqual([], [entry.name for entry in self.sessions.iterdir() if entry.name.endswith(".partial")])
        self.assertEqual(
            [
                ["docker", "rm", "-f", SESSION_CONTAINER],
                ["docker", "rm", "-f", CONTAINER],
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "-v",
                    f"{self.workspace.resolve()}:/workspace",
                    "--entrypoint",
                    "chown",
                    IMAGE_ID,
                    "-R",
                    "-h",
                    f"{os.getuid()}:{os.getgid()}",
                    "/workspace",
                ],
            ],  # fmt: skip
            [call for call in docker.calls if call[1] in ("rm", "run")],
        )
        self.assertEqual([], witness.violations)


if __name__ == "__main__":
    unittest.main()
