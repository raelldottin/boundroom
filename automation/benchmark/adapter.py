"""Adapters that turn one ProgramBench task into a ``submission.tar.gz``.

The production adapter, ``SupervisorAgentAdapter``, exercises the *real* harness: it
models the rebuild as a single supervisor slice, renders the harness prompt with the
existing ``run_next.render_prompt`` (base + slice fragments), and launches the harness
agent runner (``run_agent.sh``) in a fresh local workspace. Nothing here uses a
container; the agent rebuilds on the local filesystem and the workspace is archived as
the submission. ProgramBench's cleanroom is only used later, by the eval step.

The agent invocation is a single injectable seam (``runner``) so tests drive the whole
adapter without an LLM or any external process. *How* the agent is driven is a second
seam (``strategy``): the default is ``SliceContextStrategy`` - the shipped harness
behaviour, and lane B of the effectiveness experiment - while other lanes vary context
and workflow only. Workspace setup, archiving and budget stay here so they are identical
across lanes.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from automation.context import build_context

from .cleanroom import WORKSPACE_DIR, CleanroomError, CleanroomReceipt
from .cleanroom import prepare as prepare_cleanroom
from .diagnosis import controls_stems, snapshot_activity
from .witness import SandboxWitness
from .instances import TaskSpec
from .strategies import (
    AGENT_TIMEOUT_RETURNCODE,
    EXCLUDED_FROM_SUBMISSION,
    RPI_DIR,
    ExecutionContext,
    ExecutionStrategy,
    SliceContextStrategy,
    StrategyResult,
    agent_workspace_for,
)

DEFAULT_TIMEOUT_SECONDS = 1800
# How long a session gets to shut down after its budget kill, before the process group is
# destroyed. A SIGKILL takes the session's buffered stdout with it: every phase killed at
# its ceiling in runs 35715428932 and 35736569601 archived a 0-byte log, which is also the
# evidence the provider-failure classifier reads. Short, because this is flush-and-exit
# time, not working time - it is recorded separately and never charged to the phase.
AGENT_TERMINATION_GRACE_SECONDS = 5
# Allow the agent to touch the whole rebuild workspace.
WORKSPACE_ALLOWED_PATH = "./"
# Effectively unbounded diff budget: a from-scratch rebuild is not a bounded slice.
REBUILD_DIFF_BUDGET = 1_000_000

# (formatted_command, workspace, env, timeout_seconds) -> return code
CommandRunner = Callable[[str, Path, Mapping[str, str], int], int]

# Exactly what ``run_agent.sh`` reads, by literal name, and nothing else.
#
# A cell's whole job is to run model-authored commands, and it used to be handed
# ``dict(os.environ)``: the operator's provider keys, GitHub token, cloud credentials and
# SSH agent socket, none of which rebuilds a C repository. The list is names, not patterns,
# because a pattern fails open - ``BOUNDROOM_*`` ships the next variable somebody adds
# under that prefix, whatever ends up in it, and the property worth having is that a variable
# nobody considered is absent. Anything else a session genuinely needs (a provider key, a base
# URL) is authorized deliberately through ``env=`` / ``--agent-env``.
#
# ``TERMINAL_CWD`` and ``HERMES_HOME`` are deliberately absent: the runner sets both itself,
# per invocation, and inheriting either would point a session at the previous cell's state.
INHERITED_ENV_NAMES = (
    "PATH",  # find any agent CLI at all
    "HOME",  # the CLI's own config and credential store
    "TMPDIR",  # run_agent.sh mktemp -d's the throwaway HERMES_HOME under it
    "LANG",
    "LC_ALL",
    "BOUNDROOM_AGENT_RUNNER",
    "BOUNDROOM_CODEX_BIN",
    "BOUNDROOM_CLAUDE_BIN",
    "BOUNDROOM_CLAUDE_PERMISSION_MODE",
    "BOUNDROOM_HERMES_BIN",
    # Names from before the 0.3.0 rename (SPEC-005), which run_agent.sh still honours.
    "REPO_AUTOMATION_AGENT_RUNNER",
    "REPO_AUTOMATION_CODEX_BIN",
    "REPO_AUTOMATION_CLAUDE_BIN",
    "REPO_AUTOMATION_CLAUDE_PERMISSION_MODE",
    "REPO_AUTOMATION_HERMES_BIN",
    "OWLORY_CODEX_BIN",  # legacy alias run_agent.sh still honours
    "HERMES_REVISION",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_INFERENCE_MODEL",
    "CLAUDECODE",  # the three markers run_agent.sh auto-detects Claude Code by
    "CLAUDE_CODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


def audited_inherited_environment() -> dict[str, str]:
    """The launcher variables an agent session inherits, selected by exact name."""
    return {name: os.environ[name] for name in INHERITED_ENV_NAMES if name in os.environ}


def agent_environment(env: Optional[Mapping[str, str]]) -> dict[str, str]:
    """An agent session's environment: the audited inherited variables, then ``env`` over them.

    Explicit configuration wins, and is never filtered through the inherited list: an env=
    mapping is the caller's authorization, not an accident of the launcher. Every lane and the
    stop gate's judge build theirs here, so the judge is the agent the lanes are.
    """
    environment = audited_inherited_environment()
    if env:
        environment.update(env)
    return environment


@dataclass
class SubmissionResult:
    instance_id: str
    tar_path: Path
    workspace: Path
    returncode: int
    strategy: Optional[StrategyResult] = None
    cleanroom: Optional[CleanroomReceipt] = None


class AgentAdapter(Protocol):
    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult: ...


def build_slice_record(task: TaskSpec) -> dict:
    """Model a ProgramBench rebuild as a schema-valid supervisor slice."""
    return {
        # Both reach the agent: `summarize_slice` puts them in the rendered prompt. The
        # instance id survives only where the agent never reads - run directories, the
        # benchmark receipts and scoring, and resolving the cleanroom image.
        "slice_id": task.public_id,
        "title": "Rebuild the program in this workspace from scratch",
        "status": "queued",
        "priority": 0,
        "domain": "programbench",
        "allowed_paths": [WORKSPACE_ALLOWED_PATH],
        "required_validations": ["sh compile.sh"],
        "depends_on": [],
        "max_files_changed": REBUILD_DIFF_BUDGET,
        "notes": task.objective,
    }


def build_queue_data(slice_record: dict, agent_command_template: str, timeout_seconds: int) -> dict:
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 1,
            "handoff_timeout_seconds": timeout_seconds,
            "agent_command_template": agent_command_template,
            "supervisor_owned_paths": ["automation/"],
        },
        "slices": [slice_record],
    }


def build_context_bundle(queue_data: dict, slice_record: dict) -> dict:
    """Assemble the minimal bundle ``run_next.render_prompt`` consumes, reusing helpers."""
    return {
        "policy_sentence": build_context.POLICY_SENTENCE,
        "slice": build_context.summarize_slice(slice_record),
        "queue": build_context.build_queue_metadata(queue_data, slice_record),
        "validation_ownership": build_context.summarize_validation_ownership(slice_record),
        "previous_handoff": None,
        "previous_handoff_summary": build_context.render_previous_handoff_summary(None),
        "execution_constraints": build_context.build_execution_constraints(slice_record),
        "handoff_template": build_context.build_handoff_template(slice_record),
        "documents": [],
    }


SUPERVISOR_SCRIPT = "automation/supervisor/run_agent.sh"
# The no-model default-backend probe: its witness receipt and its log are named for it
# rather than for a session, because it is not one.
SANDBOX_PROBE_STEM = "sandbox-probe"
# One container start and one `docker exec`. Generous for that, and short enough that a
# wedged daemon does not eat the cell it is protecting.
SANDBOX_PROBE_TIMEOUT_SECONDS = 300
# Where the probe and the witness meet, inside the workspace: the container sees it at
# /workspace, the witness at the host path, and neither needs a second channel. Removed
# again before the agent's turn - it is scaffolding, not part of the submission.
SANDBOX_PROBE_BARRIER_DIR = ".sandbox-probe-barrier"
# How long a probe container holds itself open waiting to be inspected. Room for a 0.5s
# poll plus a `docker inspect` on a loaded runner, and bounded so a witness that never
# releases costs the cell seconds rather than its budget.
SANDBOX_PROBE_BARRIER_SECONDS = 15


def _observed_task_ids(observation: Mapping[str, Any]) -> str:
    containers = observation.get("containers") or []
    return ", ".join(str(record.get("task_id")) for record in containers) or "none"


def _default_command_template(repo_root: Path) -> str:
    """run_agent.sh referenced by absolute path: the agent's cwd is the workspace."""
    script = repo_root / SUPERVISOR_SCRIPT
    return (
        f"{shlex.quote(str(script))} --repo-root {{repo_root}} --prompt-file {{prompt_file}} "
        "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}"
    )


def _subprocess_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
    """Run one agent session, and treat exhausting its budget as a result, not a crash.

    The budget exists to bound a cell. If running it out raised, one slow cell would take
    the rest of the matrix with it and the run would report nothing at all - including for
    the lanes that finished. A timed-out session is a failed phase like any other.
    """
    # Its own process group, so the timeout can collect the whole session: the agent spawns
    # tool subprocesses, and one left behind would spend the next cell's wall clock too.
    sessions_dir = Path(env["BOUNDROOM_HERMES_USAGE_DIR"]) if env.get("BOUNDROOM_HERMES_USAGE_DIR") else None
    announced = controls_stems(sessions_dir) if sessions_dir is not None else frozenset()
    with subprocess.Popen(command, cwd=workspace, shell=True, env=dict(env), start_new_session=True) as agent:
        try:
            return agent.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Ask before compelling. SIGTERM lets the session flush what it has written and
            # the tee drain the pipe, so a censored phase still leaves its checkpointed
            # artifact and a readable log; SIGKILL after the grace so a session that ignores
            # the signal still cannot outlive its budget. The activity snapshot comes first: the
            # turn Hermes unwinds on SIGTERM clears the label that says what it was doing.
            if sessions_dir is not None:
                snapshot_activity(sessions_dir, announced)
            os.killpg(agent.pid, signal.SIGTERM)
            try:
                agent.wait(timeout=AGENT_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(agent.pid, signal.SIGKILL)
            return AGENT_TIMEOUT_RETURNCODE


# Per-session agent reports, written next to the submission rather than into it.
AGENT_SESSIONS_DIR = "agent-sessions"
# What the sandbox was proved to be, written next to the submission it produced.
CLEANROOM_RECEIPT_FILENAME = "cleanroom.json"
# Read by run_agent.sh: the image the model-facing tools run inside, or nothing at all.
SANDBOX_IMAGE_ENV = "BOUNDROOM_HERMES_SANDBOX_IMAGE"
# What was actually graded, kept when the tarball itself is not.
SUBMISSION_MANIFEST_FILENAME = "submission.files.txt"


def _archive_workspace(workspace: Path, out_tar: Path) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    manifest = out_tar.with_name(SUBMISSION_MANIFEST_FILENAME)
    # touch(exist_ok=False), for the reason _save_phase_artifacts uses mkdir(exist_ok=False)
    # below: creating the path is the single operation that decides who owns it. These two
    # writes used to truncate unconditionally, so a rerun into a run directory holding a
    # finished cell overwrote the graded submission and the record of what was graded - the
    # same destruction the .rpi copy beside them refuses. Both paths are claimed before either
    # is written, so refusing the second does not strand a submission from the first.
    claimed: list[Path] = []
    try:
        for path in (out_tar, manifest):
            try:
                path.touch(exist_ok=False)
            except FileExistsError as collision:
                raise FileExistsError(f"refusing to overwrite pre-existing submission output: {path}") from collision
            claimed.append(path)
        with tarfile.open(out_tar, "w:gz") as tar:
            for entry in sorted(workspace.iterdir()):
                if entry.name in EXCLUDED_FROM_SUBMISSION:
                    continue
                tar.add(entry, arcname=entry.name)
            members = tar.getnames()
        # The tarball itself is too large to keep, so the graded contents are unprovable after
        # the job ends: whether the cell submitted anything, and whether a lane's own phase
        # artifacts leaked into what was scored. List what went in, next to what came out.
        # ponytail: one name per line, so a workspace filename containing a newline reads as two
        # entries. Nothing decides anything from this file - two tests split() it and CI uploads
        # it as evidence - so it stays plainly readable rather than escaped. Escape it when
        # something starts parsing it.
        manifest.write_text("".join(f"{name}\n" for name in members), encoding="utf-8")
    except BaseException:
        # Only ever the paths this call created, and only because it created them: a
        # half-written submission reads as a complete one.
        for path in claimed:
            path.unlink(missing_ok=True)
        raise


# Directories an agent-written tree may nest before the copy stops descending. Each level holds
# one descriptor open while its children are copied, so this also bounds the descriptors and the
# recursion one copy can use.
MAX_COPY_DEPTH = 64
# Written next to what was copied: what was left out, and why, so an audit can tell a planted link
# from nothing written. Never inside the copy, where an agent-written file could have the name.
REFUSED_FILENAME = "refused.json"


class NotARegularFile(Exception):
    """An agent-written path the harness refuses to read: a link, a special file, or too large.

    ``reason`` is ``symlink``, ``hard_link``, ``special`` or ``too_large``.
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.reason = reason


class NotARealDirectory(Exception):
    """An agent-written path that should be a directory and is a symlink (``symlink``) or something else (``not_a_directory``)."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.reason = reason


def _refusal_reason(info: os.stat_result) -> Optional[str]:
    """Why an opened entry that isn't a directory is refused, or None for a single-name regular file."""
    if not stat.S_ISREG(info.st_mode):
        return "special"
    if info.st_nlink != 1:
        return "hard_link"
    return None


def read_agent_file(path: Path, limit: int) -> bytes:
    """An agent-written file's bytes, refusing a symlink, a hard link, a FIFO or device, or more than ``limit`` bytes.

    An agent session writes the file and the harness reads it outside the session's sandbox, so
    a link must not lead the harness to read, and record, what the session could not. A hard
    link passes every test but the link count, so a regular file with more than one name is
    refused too. Opened with ``O_NOFOLLOW`` and checked with ``fstat``, so nothing can be
    swapped in between the check and the read. Raises ``NotARegularFile`` for a refusal and
    ``OSError`` (``FileNotFoundError`` included) when the file can't be opened.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise NotARegularFile(str(path), "symlink") from error
        raise
    with os.fdopen(descriptor, "rb") as handle:
        reason = _refusal_reason(os.fstat(handle.fileno()))
        if reason is not None:
            raise NotARegularFile(str(path), reason)
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise NotARegularFile(str(path), "too_large")
    return data


def open_real_dir(path: Path) -> int:
    """A read-only descriptor for an agent-written directory, never through a symlink.

    Opened once with ``O_NOFOLLOW | O_DIRECTORY`` and checked with ``fstat``, so a caller that
    copies from the descriptor copies what was checked, whatever is swapped in at the path
    afterwards. Raises ``NotARealDirectory`` for a symlink or a non-directory and ``OSError``
    (``FileNotFoundError`` included) when nothing can be opened.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            # Some systems report a symlink opened with O_DIRECTORY as ENOTDIR, not ELOOP.
            try:
                reason = "symlink" if stat.S_ISLNK(os.lstat(path).st_mode) else "not_a_directory"
            except OSError:
                raise error from None
            raise NotARealDirectory(str(path), reason) from error
        raise
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise NotARealDirectory(str(path), "not_a_directory")
    return descriptor


def copy_regular_tree(source: Path, destination: Path) -> list[dict[str, str]]:
    """Copy an agent-written tree, leaving out symlinks, hard links, FIFOs, sockets and devices.

    Returns what was left out, as ``{"path", "reason"}`` with the path relative to ``source``.
    A source that doesn't exist copies nothing and refuses nothing; a source that is itself a
    symlink, or isn't a directory, copies nothing and is refused with path ``"."``.
    See ``copy_regular_tree_from`` for what is refused and why.
    """
    try:
        root = open_real_dir(source)
    except FileNotFoundError:
        return []
    except NotARealDirectory as refusal:
        return [{"path": ".", "reason": refusal.reason}]
    try:
        return copy_regular_tree_from(root, Path(destination))
    finally:
        os.close(root)


def copy_regular_tree_from(root: int, destination: Path) -> list[dict[str, str]]:
    """Copy the tree under an open directory descriptor into ``destination``; return what was refused.

    An agent session writes these trees, and the harness copies them outside the session's
    sandbox: following a link would let the session have the harness copy a file it was
    never allowed to read, such as a key, into the run directory. A hard link is a regular file
    to every other test, so a regular file with more than one name is left out too; probe.py's
    evidence collector refuses it for the same reason.

    Every entry is opened with ``O_NOFOLLOW`` relative to its parent's descriptor and checked
    with ``fstat`` on what was opened, so a process the session left running can't swap a file
    or directory for a link between the check and the copy. ``shutil.copytree`` can't promise
    that: it decides from the ``lstat`` it took when listing, then opens by path. Directories
    nested deeper than ``MAX_COPY_DEPTH`` are not descended into.

    Each refusal's reason is ``symlink``, ``hard_link``, ``special`` (a FIFO, socket or device),
    ``too_deep`` or ``unreadable``. Files keep their permission bits, less setuid, setgid and
    sticky, and their access and modification times, as ``shutil.copy2`` keeps them.
    """
    refused: list[dict[str, str]] = []
    _copy_regular_entries(root, Path(destination), "", 0, refused)
    return refused


def _copy_regular_entries(directory: int, destination: Path, prefix: str, depth: int, refused: list[dict[str, str]]) -> None:
    """Copy the real directories and single-name regular files under an open directory descriptor."""
    destination.mkdir(parents=True, exist_ok=True)
    names = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_symlink():
                refused.append({"path": prefix + entry.name, "reason": "symlink"})
            elif entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False):
                names.append(entry.name)
            else:
                refused.append({"path": prefix + entry.name, "reason": "special"})
    for name in sorted(names):
        relative = prefix + name
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            continue  # Gone since the listing: nothing to copy, nothing refused.
        except OSError as error:
            if error.errno == errno.ELOOP:
                refused.append({"path": relative, "reason": "symlink"})  # Swapped for a link since the listing.
            elif error.errno in (errno.EMFILE, errno.ENFILE):
                raise
            else:
                refused.append({"path": relative, "reason": "unreadable"})
            continue
        try:
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 > MAX_COPY_DEPTH:
                    refused.append({"path": relative, "reason": "too_deep"})
                else:
                    _copy_regular_entries(descriptor, destination / name, relative + "/", depth + 1, refused)
                    _copy_times_and_mode(info, destination / name, directory=True)
                continue
            reason = _refusal_reason(info)
            if reason is not None:
                refused.append({"path": relative, "reason": reason})
                continue
            target = destination / name
            with os.fdopen(os.dup(descriptor), "rb") as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            _copy_times_and_mode(info, target, directory=False)
        finally:
            os.close(descriptor)


def _copy_times_and_mode(info: os.stat_result, target: Path, directory: bool) -> None:
    """Give a copy its source's permission bits and times, as ``shutil.copy2`` and ``copystat`` do.

    Setuid, setgid and sticky bits are dropped: the source is agent-written. A directory always
    keeps its owner's read, write and search bits, so the harness can still clean up its record.
    """
    mode = stat.S_IMODE(info.st_mode) & 0o777
    if directory:
        mode |= stat.S_IRWXU
    os.chmod(target, mode)
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def write_refused(path: Path, refused: list[dict[str, str]], error: Optional[str] = None) -> None:
    """Record what a copy of agent-written output left out, and the error that stopped it, if one did."""
    record = {"refused": sorted(refused, key=lambda entry: entry["path"]), "error": error}
    Path(path).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def describe_copy_error(error: BaseException) -> str:
    """A copy failure as one line for the record: its type, and its message."""
    return f"{type(error).__name__}: {error}"


def _save_phase_artifacts(workspace: Path, out_dir: Path) -> None:
    """Keep phase artifacts next to the submission so a result can be reproduced.

    What the copy refused, and an error that stopped it, are written to ``refused.json`` beside
    ``rpi/``. Running out of descriptors or recursion while copying is recorded there, not
    raised: it says what this cell's artifacts are, and doesn't stop the run.
    """
    record = Path(out_dir) / REFUSED_FILENAME
    destination = Path(out_dir) / "rpi"
    # Opened once, before the destination is claimed, and copied from that descriptor: a session
    # that makes `.rpi` a link, before or after this point, gets neither the link's target copied
    # out nor an empty directory claimed for it.
    try:
        root = open_real_dir(workspace / RPI_DIR)
    except FileNotFoundError:
        return
    except NotARealDirectory as refusal:
        write_refused(record, [{"path": "rpi", "reason": refusal.reason}])
        return
    except OSError as error:
        write_refused(record, [], describe_copy_error(error))
        return
    try:
        # mkdir, not exists()-then-copy: claiming the directory is how ownership is decided, so it
        # has to be the single operation that decides it. The copy used to open with an
        # unconditional rmtree, which meant a rerun into a run directory holding a finished cell's
        # phase artifacts deleted them - the evidence of the attempt being investigated, destroyed
        # by the attempt investigating it. A retry does not get to decide it owns someone's output.
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except FileExistsError as collision:
            raise FileExistsError(f"refusing to overwrite pre-existing phase artifact directory: {destination}") from collision
        try:
            refused = copy_regular_tree_from(root, destination)
        except (OSError, RecursionError) as error:
            # Only ever the directory this call created, and only because it created it: a
            # half-copied artifact set reads as a complete one.
            shutil.rmtree(destination, ignore_errors=True)
            write_refused(record, [], describe_copy_error(error))
            return
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        write_refused(record, [{**entry, "path": f"rpi/{entry['path']}"} for entry in refused])
    finally:
        os.close(root)


def probe_sandbox(
    repo_root: Path,
    runner: CommandRunner,
    workspace: Path,
    environment: Mapping[str, str],
    sessions_dir: Path,
    receipt: CleanroomReceipt,
) -> None:
    """Prove the containers the model's tools would get, before spending the budget.

    The preflight proves the image, and the witness proves whichever containers a
    session happens to create - but the containers that serve terminal, file and
    code_execution are made by the model's first tool call, and a cell that times out
    before that one leaves the question open. This asks it directly, with no model in
    the loop: one terminal call on each of the two backends an agent turn uses, through
    the same composed config and the same sandbox image the session will get.

    Refusing here costs two container starts. Refusing after the session costs the
    budget, which is how run 36485906813 spent 300 seconds to discover a tmpfs.

    The two sides are synchronised rather than sampled. A probe container exists for one
    `docker exec` and no longer, so whether the witness happens to see it is a race
    against its own poll interval - run 37275443406 lost that race by 233ms and refused a
    cleanroom whose own probe had just reported both backends sound. Each probe command
    therefore holds its container open until the witness has inspected and recorded it.
    What is proved is unchanged: the real `container_persistent: false` backend, its real
    image, network and workspace mount. Only the order of inspection and removal is.
    """
    barrier = workspace / SANDBOX_PROBE_BARRIER_DIR
    # Creating it is what gives the harness the right to delete it. The workspace is
    # ProgramBench's input, and clearing a path out of the way first would mean the harness
    # modifying the benchmark before the agent ever sees it - so a collision fails the cell
    # instead. No shipped image is known to carry this name; a reusable cleanroom harness
    # should not be the thing that finds out by overwriting one.
    try:
        barrier.mkdir(exist_ok=False)
    except FileExistsError as collision:
        raise CleanroomError(f"the sandbox-probe barrier already exists in the cleanroom: {barrier}") from collision
    witness = SandboxWitness(
        sessions_dir, workspace, receipt.image, receipt.image_id, stem=SANDBOX_PROBE_STEM, release_dir=barrier
    )
    script = shlex.quote(str(repo_root / SUPERVISOR_SCRIPT))
    command = f"{script} --repo-root {shlex.quote(str(workspace))} --sandbox-probe"
    environment = {
        **environment,
        # The container's view of the barrier, which is the only view the waiting command
        # has: /workspace is where the cleanroom is mounted.
        "BOUNDROOM_SANDBOX_PROBE_BARRIER": f"{WORKSPACE_DIR}/{SANDBOX_PROBE_BARRIER_DIR}",
        "BOUNDROOM_SANDBOX_PROBE_BARRIER_SECONDS": str(SANDBOX_PROBE_BARRIER_SECONDS),
    }
    # Guarding the probe only, deliberately: a `finally` around the `mkdir` above would
    # delete the very directory the collision check exists to protect.
    try:
        with witness.watching():
            returncode = runner(command, workspace, environment, SANDBOX_PROBE_TIMEOUT_SECONDS)
    finally:
        # Before the agent's turn whatever happened, including a refusal: the workspace is
        # archived as the submission, and a cell must not inherit the previous phase's
        # releases either.
        shutil.rmtree(barrier)
    observation = witness.receipt
    failures = list(witness.violations)
    if returncode != 0:
        failures.append(f"the sandbox probe exited {returncode}; see {SANDBOX_PROBE_STEM}.log")
    for backend, verified in (
        ("default", observation["default_backend_verified"]),
        ("session-scoped", observation["session_backend_verified"]),
    ):
        if not verified:
            failures.append(
                f"no sound {backend} container was observed, so the backend the model's tools "
                f"would have used is unproved (task ids seen: {_observed_task_ids(observation)})"
            )
    # `docker_persist_across_processes: false` read off the daemon rather than off the
    # config file: a container the daemon still lists is one the next process attaches
    # to by label, carrying this cell's mounts into the next.
    outlived = [record["container_id"] for record in observation["containers"] if not record["removed_after_exit"]]
    if outlived:
        failures.append(f"{len(outlived)} probe container(s) outlived the process that made them: {outlived}")
    if failures:
        raise CleanroomError("the agent's sandbox was not the cleanroom it was given:\n  - " + "\n  - ".join(failures))


class SupervisorAgentAdapter:
    """Drive the Boundroom supervisor loop to produce a submission, locally."""

    def __init__(
        self,
        repo_root: Path,
        agent_command_template: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        env: Optional[Mapping[str, str]] = None,
        runner: Optional[CommandRunner] = None,
        strategy: Optional[ExecutionStrategy] = None,
        cleanroom: bool = False,
        telemetry: Optional[Any] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._template = agent_command_template or _default_command_template(self._repo_root)
        self._timeout = timeout_seconds
        self._env = dict(env) if env is not None else None
        self._runner = runner or _subprocess_runner
        self._strategy = strategy or SliceContextStrategy()
        self._cleanroom = cleanroom
        self._telemetry = telemetry

    @property
    def lane(self) -> str:
        return self._strategy.name

    def _run_environment(self) -> dict[str, str]:
        return agent_environment(self._env)

    def _probe_sandbox(
        self, workspace: Path, environment: Mapping[str, str], sessions_dir: Path, receipt: CleanroomReceipt
    ) -> None:
        probe_sandbox(self._repo_root, self._runner, workspace, environment, sessions_dir, receipt)

    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
        out_tar = Path(out_tar)

        # Named for the public id: outside a cleanroom cell this path is the agent's own
        # working directory, and `pb-abishekvashok__cmatrix.5c082c6-x` in a shell prompt
        # tells it everything the envelope no longer does.
        workspace = Path(tempfile.mkdtemp(prefix=f"pb-{task.public_id}-"))
        if not self._cleanroom:
            # run_agent.sh refuses a non-git repo root; the workspace is the agent's repo.
            # A cleanroom cell gets its worktree from the image instead - see below.
            subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
        control_dir = Path(tempfile.mkdtemp(prefix=f"pb-control-{task.public_id}-"))

        environment = self._run_environment()
        # The inference environment, before the first agent turn and proved rather than
        # configured: the reference ``./executable`` and the image's bundled documentation
        # land in the workspace, and the image the tools run inside is named to run_agent.sh.
        # A cleanroom that fails its own preflight raises - an agent that can reach the
        # upstream source is not attempting this benchmark, whatever it scores.
        #
        # The workspace is the image's ``/workspace`` and nothing else: it must be empty
        # when the copy starts, and the Git worktree the harness needs is the one-commit
        # repository the image already ships. Creating either here would make the cell's
        # environment partly the harness's invention rather than ProgramBench's.
        # Beside the submission, never inside it: one usage + controls report per agent
        # session, so a result says which model answered and what it was allowed to do.
        # Absolute, because the agent runs with the workspace as its working directory and
        # --run-dir is usually relative: a relative path here wrote the reports into the
        # workspace, which archived them into the submission and left run.json with none.
        sessions_dir = (out_tar.parent / AGENT_SESSIONS_DIR).resolve()
        environment["BOUNDROOM_HERMES_USAGE_DIR"] = str(sessions_dir)

        receipt = None
        if self._cleanroom:
            receipt = prepare_cleanroom(task.instance_id, workspace, task.repository)
            environment[SANDBOX_IMAGE_ENV] = receipt.image
            out_tar.parent.mkdir(parents=True, exist_ok=True)
            (out_tar.parent / CLEANROOM_RECEIPT_FILENAME).write_text(
                json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
            self._probe_sandbox(workspace, environment, sessions_dir, receipt)

        # The preflight proves the image; this proves the container the agent's tools were
        # actually given, from outside, while the session still holds it. Reuse of a
        # container from an earlier process is the failure it exists for: nothing in the
        # receipt, the config or the session's own output would show it.
        runner = self._runner
        witness = None
        if self._cleanroom and receipt is not None:
            witness = SandboxWitness(sessions_dir, workspace, receipt.image, receipt.image_id)
            runner = witness.wrap(self._runner)

        strategy_result = self._strategy.execute(
            ExecutionContext(
                repo_root=self._repo_root,
                task=task,
                workspace=workspace,
                control_dir=control_dir,
                command_template=self._template,
                env=environment,
                timeout_seconds=self._timeout,
                runner=runner,
                agent_workspace=agent_workspace_for(self._cleanroom),
                telemetry=self._telemetry,
            )
        )
        if witness is not None and witness.violations:
            # Same refusal as a failed preflight, for the same reason: what ran was not a
            # ProgramBench cleanroom, so its score is not a ProgramBench result. The
            # witness receipts are already on disk beside the submission path.
            raise CleanroomError(
                "the agent's sandbox was not the cleanroom it was given:\n  - " + "\n  - ".join(witness.violations)
            )

        _save_phase_artifacts(workspace, out_tar.parent)
        _archive_workspace(workspace, out_tar)
        return SubmissionResult(
            instance_id=task.instance_id,
            tar_path=out_tar,
            workspace=workspace,
            returncode=strategy_result.returncode,
            strategy=strategy_result,
            cleanroom=receipt,
        )
