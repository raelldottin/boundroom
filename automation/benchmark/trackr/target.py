"""The target's setup: Boundroom synced beside python-semver, offline venvs, and the test harness.

**The sync.** Boundroom's own ``Tools/boundroom-sync.sh --sync`` runs once per pilot or selection,
from a clean checkout of the pinned supervisor commit (SUPERVISOR_RELEASE), into a staging directory. It checks the source's
provenance (the pin, the remote, a clean tree) and runs the source's quality gate,
``make reusable-check``, which takes minutes, so it runs once and not per clone. The staged result is
then copied into each clone, and ``boundroom-sync.sh --check`` (an unpinned drift report) confirms
the clone matches the manifest. The optional ``benchmark`` component is left out (``--omit
benchmark``): it is the ProgramBench harness, irrelevant to a supervised slice in semver.

**The venvs.** semver's tests run in the clone's own ``.venv`` (ignored by semver's ``.gitignore``),
installed offline from the frozen wheelhouse. The supervisor runs under its own interpreter, outside
every clone, with pydantic from the same wheelhouse.

**The harness.** Files that decide how tests are collected or whether they run at all. The checker
restores them to the base version, or the head version where the human pull request changed them,
before it runs anything, and an agent's change to one counts as out of scope.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from . import wheelhouse as wheels
from .common import (
    SUPERVISOR_RELEASE,
    SUPERVISOR_UPSTREAM,
    CommandRunner,
    TrackRError,
    clone_at,
    git,
    git_bytes,
    tail,
)

SYNC_SCRIPT = "Tools/boundroom-sync.sh"
SYNC_OMIT = ("benchmark",)
SYNC_TIMEOUT_SECONDS = 1800
VENV_TIMEOUT_SECONDS = 600
# What the sync writes, by its own allow list (ALLOWED_DESTINATION_PREFIXES and _FILES in the script).
SYNC_DESTINATIONS = (
    "automation/",
    "docs/workflows/",
    "THIRD-PARTY-NOTICES.md",
    "Tools/boundroom-sync.sh",
    "Tools/repo-automation-sync.sh",
)

# Collection and run configuration pytest reads, by file name anywhere in the tree.
HARNESS_NAMES = ("conftest.py", "pytest.ini", ".pytest.ini", "setup.cfg", "tox.ini", "noxfile.py", ".coveragerc")
# pyproject.toml counts only for its pytest tables.
PYPROJECT = "pyproject.toml"
PYTEST_TABLE = re.compile(r"^\[(tool\.pytest[^\]]*)\]\s*$")


# --- the sync -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncStage:
    """What one verified sync of the release commit wrote, ready to copy into clones."""

    path: Path
    source: Path
    files: tuple[str, ...]
    digest: str
    log: str

    def record(self) -> dict[str, Any]:
        return {
            "release": SUPERVISOR_RELEASE,
            "script": SYNC_SCRIPT,
            "omit": list(SYNC_OMIT),
            "files": len(self.files),
            "sha256": self.digest,
        }


def tree_digest(root: Path) -> tuple[tuple[str, ...], str]:
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink())
    digest = hashlib.sha256()
    for relative in files:
        digest.update(relative.encode() + b"\0" + (root / relative).read_bytes() + b"\0")
    return tuple(files), digest.hexdigest()


def stage_sync(
    boundroom_mirror: Path, work: Path, supervisor_python: Path, runner: CommandRunner, env: Mapping[str, str]
) -> SyncStage:
    """Run the release's own sync, fully verified, into ``work/stage``. Starts no model."""
    source = work / "boundroom-supervisor"
    clone_at(boundroom_mirror, SUPERVISOR_RELEASE, source, env, history_only=False)
    git(["remote", "add", "origin", SUPERVISOR_UPSTREAM], source, env)  # its identity, for the sync's check
    stage = work / "stage"
    argv = ["bash", SYNC_SCRIPT, "--sync", "--target", str(stage), "--pin", SUPERVISOR_RELEASE]
    argv += ["--expect-remote", SUPERVISOR_UPSTREAM, "--runtime-python", str(supervisor_python)]
    for component in SYNC_OMIT:
        argv += ["--omit", component]
    completed = runner(argv, source, env, SYNC_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise TrackRError(f"boundroom-sync.sh --sync failed: {tail(completed.stdout + completed.stderr, 25)}")
    files, digest = tree_digest(stage)
    outside = [path for path in files if not path.startswith(SYNC_DESTINATIONS)]
    if outside:
        raise TrackRError(f"the sync wrote outside its allowed destinations: {outside[:5]}")
    return SyncStage(stage, source, files, digest, tail(completed.stdout, 15))


def apply_sync(stage: SyncStage, clone: Path, runner: CommandRunner, env: Mapping[str, str]) -> dict[str, Any]:
    """Copy the staged sync into a clone and confirm, with the script's own drift report, that it matches."""
    clashes = [path for path in stage.files if (clone / path).exists()]
    if clashes:
        raise TrackRError(f"the target already has files the sync would write: {clashes[:5]}")
    shutil.copytree(stage.path, clone, dirs_exist_ok=True, symlinks=True)
    argv = ["bash", str(stage.source / SYNC_SCRIPT), "--check", "--target", str(clone), "--source", str(stage.source)]
    for component in SYNC_OMIT:
        argv += ["--omit", component]
    argv += ["--runtime-python", str(env.get("TRACKR_SUPERVISOR_PYTHON", "python3"))]
    completed = runner(argv, clone, env, SYNC_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise TrackRError(f"the synced clone drifts from the manifest: {tail(completed.stdout + completed.stderr, 15)}")
    return {**stage.record(), "check": "no drift"}


# --- the venvs ------------------------------------------------------------------------------------


class Environment(Protocol):
    """How a clone's test venv and the supervisor's interpreter are made: offline, or faked in tests."""

    def target_venv(self, clone: Path, runner: CommandRunner, env: Mapping[str, str]) -> dict[str, Any]: ...

    def supervisor_python(self, directory: Path, runner: CommandRunner, env: Mapping[str, str]) -> Path: ...


@dataclass(frozen=True)
class OfflineEnvironment:
    """Venvs installed with ``uv`` from the frozen wheelhouse, offline, with every hash required."""

    wheelhouse: Path

    def _venv(self, venv: Path, group: str, runner: CommandRunner, env: Mapping[str, str]) -> None:
        problems = wheels.verify(self.wheelhouse)
        if problems:
            raise TrackRError("the wheelhouse doesn't match wheelhouse.v2.json: " + "; ".join(problems))
        made = runner(
            ["uv", "venv", "--quiet", "--offline", "--python", wheels.PYTHON, str(venv)], venv.parent, env, VENV_TIMEOUT_SECONDS
        )
        if made.returncode != 0:
            raise TrackRError(f"uv venv failed: {tail(made.stdout + made.stderr, 10)}")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write(wheels.requirements(group))
        installed = runner(
            wheels.install_argv(venv / "bin" / "python", self.wheelhouse, Path(handle.name)),
            venv.parent,
            env,
            VENV_TIMEOUT_SECONDS,
        )
        Path(handle.name).unlink(missing_ok=True)
        if installed.returncode != 0:
            raise TrackRError(f"offline install of {group} failed: {tail(installed.stdout + installed.stderr, 10)}")

    def target_venv(self, clone: Path, runner: CommandRunner, env: Mapping[str, str]) -> dict[str, Any]:
        self._venv(clone / ".venv", "target", runner, env)
        return {"venv": ".venv", "group": "target", "pins": wheels.requirements("target").split()[::2]}

    def supervisor_python(self, directory: Path, runner: CommandRunner, env: Mapping[str, str]) -> Path:
        venv = directory / "supervisor-venv"
        if not (venv / "bin" / "python").exists():
            directory.mkdir(parents=True, exist_ok=True)
            self._venv(venv, "supervisor", runner, env)
        return venv / "bin" / "python"


# --- the harness ----------------------------------------------------------------------------------


def is_harness(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in HARNESS_NAMES or path == PYPROJECT or fnmatch.fnmatch(name, "conftest.py")


def tracked(repo: Path, sha: str, env: Mapping[str, str]) -> list[str]:
    listing = git_bytes(["ls-tree", "-r", "-z", "--name-only", sha], repo, env).decode("utf-8", "surrogateescape")
    return [path for path in listing.split("\0") if path]


def pytest_tables(text: str) -> str:
    """Only the ``[tool.pytest...]`` tables of a pyproject.toml, verbatim, in order."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        header = re.match(r"^\[\[?([^\]]+)\]\]?\s*$", line)
        if header:
            inside = bool(PYTEST_TABLE.match(line))
        if inside:
            kept.append(line)
    return "".join(kept)


def without_pytest_tables(text: str) -> str:
    kept: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        header = re.match(r"^\[\[?([^\]]+)\]\]?\s*$", line)
        if header:
            inside = bool(PYTEST_TABLE.match(line))
        if not inside:
            kept.append(line)
    return "".join(kept)


def harness_changes(clone: Path, setup_sha: str, final_sha: str, env: Mapping[str, str]) -> list[str]:
    """Harness files the session added, removed or changed (for pyproject.toml, only its pytest tables)."""
    before = {path for path in tracked(clone, setup_sha, env) if is_harness(path)}
    after = {path for path in tracked(clone, final_sha, env) if is_harness(path)}
    changed = sorted(before ^ after)
    for path in sorted(before & after):
        old = git_bytes(["show", f"{setup_sha}:{path}"], clone, env)
        new = git_bytes(["show", f"{final_sha}:{path}"], clone, env)
        if path == PYPROJECT:
            if pytest_tables(old.decode("utf-8", "replace")) != pytest_tables(new.decode("utf-8", "replace")):
                changed.append(path)
        elif old != new:
            changed.append(path)
    return sorted(changed)


def restore_harness(
    clone: Path, mirror: Path, setup_sha: str, head_sha: str, human_paths: Sequence[str], env: Mapping[str, str]
) -> dict[str, list[str]]:
    """Put every harness file back as the reference has it, before anything runs in ``clone``.

    The reference is the setup commit (the task's base plus the sync), except where the human pull
    request changed a harness file: then it is the head version, or nothing if the human deleted it.
    A harness file the reference lacks is deleted; one it has is written; pyproject.toml keeps
    everything but its pytest tables, which come from the reference. Returns what was done.
    """
    human = set(human_paths)
    reference: dict[str, tuple[Path, str]] = {}
    for path in tracked(clone, setup_sha, env):
        if is_harness(path) and path not in human:
            reference[path] = (clone, setup_sha)
    for path in tracked(mirror, head_sha, env):
        if is_harness(path) and path in human:
            reference[path] = (mirror, head_sha)
    present = {
        path.relative_to(clone).as_posix()
        for path in clone.rglob("*")
        if path.is_file() and not ({".git", ".venv"} & set(path.relative_to(clone).parts))
    }
    done: dict[str, list[str]] = {"restored": [], "removed": [], "pyproject_tables": []}
    for path in sorted(p for p in present if is_harness(p) and p != PYPROJECT and p not in reference):
        (clone / path).unlink()
        done["removed"].append(path)
    for path, (repo, sha) in sorted(reference.items()):
        content = git_bytes(["show", f"{sha}:{path}"], repo, env)
        target = clone / path
        if path == PYPROJECT:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            wanted = pytest_tables(content.decode("utf-8"))
            if pytest_tables(current) != wanted:
                target.write_text((without_pytest_tables(current).rstrip("\n") + "\n" + wanted).lstrip("\n"), encoding="utf-8")
                done["pyproject_tables"].append(path)
            continue
        if not target.exists() or target.read_bytes() != content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            done["restored"].append(path)
    return done


@dataclass
class FakeEnvironment:
    """For tests: a ``.venv/bin/python`` that is the test interpreter, and the same as the supervisor's."""

    python: str
    calls: list[str] = field(default_factory=list)

    def target_venv(self, clone: Path, runner: CommandRunner, env: Mapping[str, str]) -> dict[str, Any]:
        (clone / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        link = clone / ".venv" / "bin" / "python"
        if not link.exists():
            link.symlink_to(self.python)
        self.calls.append(f"target_venv {clone}")
        return {"venv": ".venv", "group": "fake"}

    def supervisor_python(self, directory: Path, runner: CommandRunner, env: Mapping[str, str]) -> Path:
        self.calls.append(f"supervisor_python {directory}")
        return Path(self.python)


def fake_stage(root: Path, files: Mapping[str, str], source: Optional[Path] = None) -> SyncStage:
    """For tests: a staged sync with the given files, as if the release's sync had written them."""
    stage = root / "stage"
    for path, text in files.items():
        (stage / path).parent.mkdir(parents=True, exist_ok=True)
        (stage / path).write_text(text)
    names, digest = tree_digest(stage)
    return SyncStage(stage, source or root, names, digest, "fake sync")
