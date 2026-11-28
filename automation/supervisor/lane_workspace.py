"""One lane's Git worktree: where it lives, how it is made and provisioned, how it ends (SPEC-014 RA-LANE-002).

This delivers RA-SYM-002 (per-slice workspaces) as a library. ``run_lanes`` (RA-LANE-005) runs
each slice in a lane through it; ``run_next`` keeps running its slice in the repository's own
worktree.

- **Where.** A lane's worktree lives under the lane root, by default
  ``$XDG_STATE_HOME/boundroom/lanes/<repository id>/``, or under ``~/.local/state`` when that is
  unset (a relative ``XDG_STATE_HOME`` is ignored, as the XDG spec says). The root must be outside
  every worktree of the repository and its Git directory, and must not be reached through a
  symlink, so a symlinked home needs ``XDG_STATE_HOME`` set to its canonical path.
- **Made.** ``git worktree add -b boundroom/lane/<slice_id>/<attempt_id> <root>/<attempt_id> <base sha>``.
- **Provisioned.** The queue's setup commands run in the new worktree before the runner starts. A
  failing, unstartable or timed-out command raises ``LaneSetupFailed``.
- **Ended.** ``lane_uncommitted_changes`` lists what the runner left uncommitted, apart from its
  handoff, and ``lane_head`` reads the commit the controller integrates. ``retain_or_remove``
  removes the worktree and its branch only when the slice is done and integrated; anything else
  keeps it.
- **One at a time.** A repository has at most one lane worktree. A kept one blocks the next until it
  is removed. Several at once is RA-LANE-006. The caller holds ``runner_profile.run_lock``, as the
  controller does for a whole run, so no other run makes a lane between the check and the add.

What this does not cover is unchanged: the ``.git`` directory is shared by every worktree, so a
commit in a lane can write any ref. That is why the controller records the lane's head commit and
integrates that SHA, never the branch name.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Callable, Mapping, Optional, Sequence

from automation.supervisor import policy

LANE_BRANCH_PREFIX = "boundroom/lane/"
DEFAULT_SETUP_TIMEOUT_SECONDS = 1800
# What a setup failure keeps of the command's output, from the end, for the decision report.
SETUP_OUTPUT_TAIL_CHARS = 4000
# How often a stop checks whether a group that got SIGTERM has ended.
GROUP_POLL_SECONDS = 0.02

# One path component and one ref component: no separators, no leading dot or dash, nothing Git's
# ref rules forbid (``..``, a trailing ``.lock`` or ``.``, ``@{``, control characters, spaces).
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class LaneWorkspaceError(Exception):
    """A lane workspace could not be made, provisioned, read or removed."""


class LaneRefused(LaneWorkspaceError):
    """The request is unsafe or not allowed: an unsafe id or root, a second lane, or a removal outside the root."""


class SetupFailureKind(StrEnum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    NOT_STARTED = "not_started"


class LaneSetupFailed(LaneWorkspaceError):
    """A ``lane_setup`` command failed. Later commands didn't run, and the worktree is kept."""

    def __init__(self, command: str, kind: SetupFailureKind, exit_code: Optional[int], output: str) -> None:
        self.command = command
        self.kind = kind
        self.exit_code = exit_code
        self.output = output[-SETUP_OUTPUT_TAIL_CHARS:]
        detail = {
            SetupFailureKind.EXITED: f"exited with code {exit_code}",
            SetupFailureKind.TIMED_OUT: "timed out",
            SetupFailureKind.NOT_STARTED: "could not start",
        }[kind]
        super().__init__(f"lane setup command {command!r} {detail}")


@dataclass(frozen=True)
class LaneWorkspace:
    """What the controller records about a lane when it makes the worktree."""

    slice_id: str
    attempt_id: str
    base_sha: str
    path: Path
    branch: str


@dataclass(frozen=True)
class LaneWorktree:
    """A lane worktree Git knows about, as ``git worktree list`` reports it."""

    path: Path
    branch: str
    head: str


@dataclass(frozen=True)
class LaneEnd:
    removed: bool
    path: Path
    branch: str


# The seam provisioning runs through: (worktree, command, environment, timeout in seconds). It
# returns the completed process, raises ``subprocess.TimeoutExpired`` on a timeout and ``OSError``
# when the command can't start.
SetupRunner = Callable[[Path, str, Mapping[str, str], float], subprocess.CompletedProcess[str]]


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def check_component(value: str, what: str) -> str:
    """Return *value* if it is safe as one path component and one ref component, else refuse."""
    if not _COMPONENT.fullmatch(value) or ".." in value or value.endswith((".", ".lock")):
        raise LaneRefused(
            f"{what} {value!r} is not safe as a lane path and branch component: use 1 to 100 letters, digits, "
            "'.', '_' or '-', starting with a letter or digit, without '..' and not ending in '.' or '.lock'"
        )
    return value


def lane_branch(slice_id: str, attempt_id: str) -> str:
    return f"{LANE_BRANCH_PREFIX}{check_component(slice_id, 'slice id')}/{check_component(attempt_id, 'attempt id')}"


def check_full_sha(value: str, what: str = "base") -> str:
    if not _FULL_SHA.fullmatch(value):
        raise LaneRefused(f"{what} {value!r} is not a full commit SHA: lanes use commits, never branch names")
    return value


def state_home(environ: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME``, or ``$HOME/.local/state``. A relative value is ignored, as the XDG spec says."""
    state = environ.get("XDG_STATE_HOME", "")
    if os.path.isabs(state):
        return Path(state)
    home = environ.get("HOME", "")
    if not os.path.isabs(home):
        raise LaneRefused("cannot place the lane root: set XDG_STATE_HOME or HOME to an absolute path")
    return Path(home) / ".local" / "state"


def repository_id(common_git_dir: Path) -> str:
    """A stable id for a repository: a hash of the canonical path of its common Git directory.

    Every worktree of a repository shares that directory, so the repository's own worktree and its
    lanes get the same id, and one lane root holds all of its lanes, which the one-at-a-time rule
    needs. Two clones have different directories and so different ids, even when they share a
    remote, and a repository without a remote still has one. The path is passed in canonical form.
    """
    return hashlib.sha256(os.fsencode(common_git_dir)).hexdigest()[:16]


def root_refusal(given: Path, canonical: Path, worktrees: Sequence[Path], common_git_dir: Path) -> Optional[str]:
    """Why *given* can't be a lane root, or None. All other paths are canonical."""
    if Path(os.path.normpath(given)) != canonical:
        return f"lane root {given} is reached through a symlink (it resolves to {canonical}); use its canonical path"
    for inside in (*worktrees, common_git_dir):
        if canonical.is_relative_to(inside):
            return f"lane root {canonical} is inside {inside}, which belongs to the repository; it must be outside it"
    return None


def without_handoff(dirty_paths: Sequence[str], handoff_path: str) -> list[str]:
    handoff = policy.normalize_repo_path(handoff_path)
    return [path for path in dirty_paths if path != handoff]


def parse_worktree_list(porcelain: str) -> list[tuple[Path, str, str]]:
    """(path, branch ref or '', head SHA or '') for each record of ``git worktree list --porcelain``."""
    records: list[tuple[Path, str, str]] = []
    for block in porcelain.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" in fields:
            records.append((Path(fields["worktree"]), fields.get("branch", ""), fields.get("HEAD", "")))
    return records


# ---------------------------------------------------------------------------
# Git and the file system
# ---------------------------------------------------------------------------


def _run_git(cwd: Path, environ: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    if not cwd.is_dir():
        raise LaneWorkspaceError(f"cannot run git {' '.join(args)}: {cwd} is not a directory")
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=dict(environ), check=False, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise LaneWorkspaceError(f"git executable not found on PATH: {error}") from error


def _git(cwd: Path, environ: Mapping[str, str], *args: str) -> str:
    result = _run_git(cwd, environ, *args)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "(empty)"
        raise LaneWorkspaceError(f"git {' '.join(args)} failed in {cwd} with code {result.returncode}\nstderr: {stderr}")
    return result.stdout.strip()


def common_git_dir(repo_root: Path, environ: Mapping[str, str]) -> Path:
    return (repo_root / _git(repo_root, environ, "rev-parse", "--git-common-dir")).resolve()


def worktrees(repo_root: Path, environ: Mapping[str, str]) -> list[tuple[Path, str, str]]:
    """(canonical path, branch ref or '', head SHA) of each worktree. Git lists the repository's own first."""
    return [
        (path.resolve(), branch, head)
        for path, branch, head in parse_worktree_list(_git(repo_root, environ, "worktree", "list", "--porcelain"))
    ]


def _root_and_worktrees(repo_root: Path, environ: Mapping[str, str]) -> tuple[Path, list[tuple[Path, str, str]]]:
    common = common_git_dir(repo_root, environ)
    listed = worktrees(repo_root, environ)
    given = state_home(environ) / "boundroom" / "lanes" / repository_id(common)
    canonical = given.resolve()
    refusal = root_refusal(given, canonical, [path for path, _, _ in listed], common)
    if refusal:
        raise LaneRefused(refusal)
    return canonical, listed


def lane_root(repo_root: Path, environ: Mapping[str, str]) -> Path:
    """The canonical lane root for *repo_root*, after refusing an unsafe one. It isn't created here."""
    return _root_and_worktrees(repo_root, environ)[0]


def lanes_in(root: Path, listed: Sequence[tuple[Path, str, str]]) -> list[LaneWorktree]:
    """The lane worktrees among *listed*: under *root*, or on a lane branch. The repository's own worktree never is one."""
    return [
        LaneWorktree(path=path, branch=branch.removeprefix("refs/heads/"), head=head)
        for path, branch, head in listed[1:]
        if branch.startswith(f"refs/heads/{LANE_BRANCH_PREFIX}") or path.is_relative_to(root)
    ]


def live_lanes(repo_root: Path, environ: Mapping[str, str]) -> list[LaneWorktree]:
    """Every lane worktree of the repository, kept or running."""
    return lanes_in(*_root_and_worktrees(repo_root, environ))


def make_private_dirs(path: Path) -> None:
    """Create *path* and any missing parents with mode 0700, as the XDG spec asks of state directories."""
    missing: list[Path] = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def create(repo_root: Path, slice_id: str, attempt_id: str, base_sha: str, environ: Mapping[str, str]) -> LaneWorkspace:
    """Make the lane's worktree on a new lane branch at *base_sha*. Refuses while another lane exists.

    The check and the ``git worktree add`` aren't atomic: the caller holds ``runner_profile.run_lock``,
    as SPEC-014 requires of the controller, so no second run makes a lane in between.
    """
    branch = lane_branch(slice_id, attempt_id)
    check_full_sha(base_sha)
    root, listed = _root_and_worktrees(repo_root, environ)
    existing = lanes_in(root, listed)
    if existing:
        lane = existing[0]
        raise LaneRefused(
            f"a lane workspace already exists at {lane.path} (branch {lane.branch or 'none'}); this version runs one "
            "lane at a time. Once its work is recovered, remove it with 'git worktree remove' and delete its branch"
        )
    path = root / attempt_id
    if path.exists() or path.is_symlink():
        raise LaneRefused(f"{path} already exists; each attempt needs its own id")
    _git(repo_root, environ, "cat-file", "-e", f"{base_sha}^{{commit}}")
    make_private_dirs(root)
    if root.resolve() != root:
        raise LaneRefused(f"lane root {root} changed into a symlink while it was being created")
    _git(repo_root, environ, "worktree", "add", "-b", branch, str(path), base_sha)
    return LaneWorkspace(slice_id=slice_id, attempt_id=attempt_id, base_sha=base_sha, path=path, branch=branch)


def signal_group(pid: int, signum: int) -> None:
    """Send *signum* to the process group *pid* leads. A group that already ended is no error."""
    try:
        os.killpg(pid, signum)
    except OSError:
        pass  # The group already ended, or only unreaped members are left.


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # A member we may not signal is still a member.
    return True


def wait_for_group(
    process: subprocess.Popen[Any],
    deadline: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until *deadline*, by *clock*, for the group *process* leads to end: its leader and every other member."""
    try:
        process.wait(timeout=max(0.0, deadline - clock()))
    except subprocess.TimeoutExpired:
        return
    while clock() < deadline and _group_alive(process.pid):
        sleep(GROUP_POLL_SECONDS)


def stop_process_group(
    process: subprocess.Popen[Any],
    grace_seconds: float = 0.0,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Stop the whole process group *process* leads, which it must, by ``start_new_session=True``.

    With a grace period the group gets ``SIGTERM`` first, and ``SIGKILL`` once the period has passed
    with any member still alive. With none it gets ``SIGKILL`` at once. Either way the leader is
    reaped before this returns, so no zombie is left.
    """
    if grace_seconds > 0:
        signal_group(process.pid, signal.SIGTERM)
        wait_for_group(process, clock() + grace_seconds, clock=clock, sleep=sleep)
    signal_group(process.pid, signal.SIGKILL)
    process.wait()


def _tail(handle: IO[bytes]) -> str:
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - SETUP_OUTPUT_TAIL_CHARS * 4))
    return handle.read().decode("utf-8", errors="replace")[-SETUP_OUTPUT_TAIL_CHARS:]


def default_setup_runner(
    worktree: Path, command: str, environ: Mapping[str, str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Run *command* with the shell in its own process group and its output in a temporary file.

    The command ends when its shell exits, so a background child it leaves doesn't hold it open.
    On a timeout, or if anything interrupts the wait, the whole group is killed. Only the end of
    the output is kept.
    """
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command,
            cwd=worktree,
            env=dict(environ),
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            stop_process_group(process)
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=_tail(output)) from None
        except BaseException:
            stop_process_group(process)
            raise
        return subprocess.CompletedProcess(command, returncode, _tail(output), None)


def provision(
    workspace: LaneWorkspace,
    setup_commands: Sequence[str],
    environ: Mapping[str, str],
    runner: Optional[SetupRunner] = None,
    timeout_seconds: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
) -> None:
    """Run the setup commands in order in the lane's worktree, each with *timeout_seconds*.

    The first that fails raises ``LaneSetupFailed``, and the rest don't run.
    """
    execute = runner or default_setup_runner
    for command in setup_commands:
        try:
            result = execute(workspace.path, command, environ, timeout_seconds)
        except subprocess.TimeoutExpired as error:
            output = error.output if isinstance(error.output, str) else ""
            raise LaneSetupFailed(command, SetupFailureKind.TIMED_OUT, None, output) from error
        except OSError as error:
            raise LaneSetupFailed(command, SetupFailureKind.NOT_STARTED, None, str(error)) from error
        if result.returncode != 0:
            raise LaneSetupFailed(command, SetupFailureKind.EXITED, result.returncode, result.stdout or "")


def lane_uncommitted_changes(workspace: LaneWorkspace, handoff_path: str, environ: Mapping[str, str]) -> list[str]:
    """The uncommitted paths in the lane's worktree, apart from its handoff, sorted and repository-relative.

    Unstaged, staged and untracked paths count, as in ``policy.git_dirty_paths``. Ignored files,
    such as what provisioning installed, don't. The verdict is the caller's.
    """
    dirty: set[str] = set()
    for args in (
        ("diff", "--name-only", "--relative"),
        ("diff", "--name-only", "--relative", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        dirty.update(policy.normalize_repo_path(line) for line in _git(workspace.path, environ, *args).splitlines() if line)
    return without_handoff(sorted(dirty), handoff_path)


def is_ancestor(repo_root: Path, commit: str, ref: str, environ: Mapping[str, str]) -> bool:
    """Whether *ref* contains *commit*. Raises ``LaneWorkspaceError`` when Git can't tell, such as for an unknown commit."""
    result = _run_git(repo_root, environ, "merge-base", "--is-ancestor", commit, ref)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise LaneWorkspaceError(f"git merge-base --is-ancestor failed in {repo_root}\nstderr: {result.stderr.strip()}")


def lane_head(workspace: LaneWorkspace, environ: Mapping[str, str]) -> str:
    """The commit the lane's worktree is at: the SHA the controller records and integrates."""
    return _git(workspace.path, environ, "rev-parse", "--verify", "HEAD^{commit}")


def retain_or_remove(
    repo_root: Path,
    workspace: LaneWorkspace,
    environ: Mapping[str, str],
    *,
    done_and_integrated: bool,
    recorded_head: str = "",
) -> LaneEnd:
    """Remove the lane's worktree and branch when its slice is done and integrated; otherwise keep both.

    Removal needs the *recorded_head* the controller integrated, and refuses unless the worktree
    is directly under the lane root, Git lists it on the lane's own branch, that branch is still
    at the recorded head, and the repository's own ``HEAD`` contains it.
    """
    if not done_and_integrated:
        return LaneEnd(removed=False, path=workspace.path, branch=workspace.branch)
    check_full_sha(recorded_head, "recorded head")
    root, listed = _root_and_worktrees(repo_root, environ)
    path = Path(os.path.normpath(workspace.path))
    if path.resolve() != path or path.parent != root:
        raise LaneRefused(f"refusing to remove {workspace.path}: it is not a lane worktree directly under {root}")
    if workspace.branch != lane_branch(workspace.slice_id, workspace.attempt_id):
        raise LaneRefused(f"refusing to delete branch {workspace.branch!r}: it is not this lane's branch")
    heads = {(p, b): h for p, b, h in listed}
    head = heads.get((path, f"refs/heads/{workspace.branch}"))
    if head is None:
        raise LaneRefused(f"refusing to remove {path}: Git doesn't list it as a worktree on {workspace.branch}")
    if head != recorded_head:
        raise LaneRefused(f"refusing to remove {path}: {workspace.branch} is at {head}, not the recorded head {recorded_head}")
    if not is_ancestor(repo_root, recorded_head, "HEAD", environ):
        raise LaneRefused(
            f"refusing to remove {path}: the repository's HEAD doesn't contain {recorded_head}, so it isn't integrated"
        )
    _git(repo_root, environ, "worktree", "remove", str(path))
    _git(repo_root, environ, "branch", "-D", workspace.branch)
    return LaneEnd(removed=True, path=path, branch=workspace.branch)
