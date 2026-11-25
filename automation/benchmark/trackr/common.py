"""What the Track R selection, driver, oracle and pilot share: clones, commands and the environment.

Every clone made here is disposable, lives outside any repository and has no remote. A
session's clone also holds no object from after its base commit, so the human solution can't
be found with ``git log --all`` or ``git cat-file``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKS_FILE = Path(__file__).with_name("tasks.pilot.json")
UPSTREAM = "https://github.com/raelldottin/boundroom.git"
UPSTREAM_REPOSITORY = "raelldottin/boundroom"

TIMEOUT_RETURNCODE = 124  # timeout(1)'s code, as the benchmark and run_next read it
TERMINATION_GRACE_SECONDS = 5

# The fields a person fills in from the pull request's description alone, without its diff.
SCOPE_FIELDS = ("task_statement", "allowed_paths", "required_validations", "max_files_changed")
# The example queue's supervisor-owned paths. Both arms are scored against the same list.
SUPERVISOR_OWNED_PATHS = ("automation/queue/slices.json", "automation/handoffs/")

UV_SYNC = ("uv", "sync", "--frozen")
# The preregistered hidden-test command. ``no:cacheprovider`` only keeps .pytest_cache out of the tree.
PYTEST = ("uv", "run", "--frozen", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider")

TEST_FILE = re.compile(r"^automation/tests/test_[^/]*\.py$")

# Removed from every environment a session, the oracle or the selection inherits. Credentials
# that could reach GitHub, interpreter state that would point uv at another project, Claude Code's
# own markers (a nested ``claude`` may refuse to start under them), and every Boundroom or Git
# variable, so an operator's runner profile, runner choice or GIT_DIR can't leak into an arm.
STRIPPED_NAMES = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
        "SSH_AUTH_SOCK",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "UV_PROJECT_ENVIRONMENT",
        "CLAUDECODE",
        "CLAUDE_CODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ID",
    }
)
STRIPPED_PREFIXES = ("BOUNDROOM_", "REPO_AUTOMATION_", "OWLORY_", "GIT_")
# Names (never values) recorded with each session, because they can change the model or its route.
RECORDED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "AWS_BEDROCK", "CLOUD_ML_", "OTEL_", "HTTPS_PROXY", "HTTP_PROXY")


class TrackRError(Exception):
    """A step of the selection, a session or the oracle could not be carried out."""


@dataclass
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    wall_seconds: float = 0.0


# (argv, cwd, env, timeout_seconds) -> Completed. The one seam every agent session, uv and pytest
# call goes through, so tests drive the driver and the oracle with a fake (as adapter.CommandRunner).
CommandRunner = Callable[[Sequence[str], Path, Mapping[str, str], float], Completed]


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def subprocess_runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Completed:
    """Run a command in its own process group, and treat running out of time as a result.

    On timeout the whole group gets SIGTERM, then SIGKILL after a grace period, so a session's tool
    processes can't outlive it. A finished command's leftover children are collected the same way.
    """
    started = time.monotonic()
    timed_out = False
    with subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _kill_group(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
        finally:
            _kill_group(process.pid, signal.SIGKILL)
    return Completed(
        returncode=TIMEOUT_RETURNCODE if timed_out else process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        wall_seconds=round(time.monotonic() - started, 3),
    )


# --- environment ----------------------------------------------------------------------------------


def isolated_environment(environ: Mapping[str, str], private_dir: Path) -> dict[str, str]:
    """``environ`` without the stripped names, with Git and gh pointed at empty, private config.

    ``private_dir`` must be outside the clone. Git reads an empty global file and no system file,
    so no credential helper, hook path or identity of the operator's applies; gh reads an empty
    config directory, and its token variables are stripped, so it has no account.
    """
    private_dir.mkdir(parents=True, exist_ok=True)
    gitconfig = private_dir / "gitconfig"
    gitconfig.touch()
    gh_dir = private_dir / "gh"
    gh_dir.mkdir(exist_ok=True)
    env = {
        name: value for name, value in environ.items() if name not in STRIPPED_NAMES and not name.startswith(STRIPPED_PREFIXES)
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GH_CONFIG_DIR": str(gh_dir),
            "BOUNDROOM_TELEMETRY": "off",
        }
    )
    return env


def recorded_env_names(env: Mapping[str, str]) -> list[str]:
    return sorted(name for name in env if name.startswith(RECORDED_ENV_PREFIXES))


# --- git ------------------------------------------------------------------------------------------


def git(args: Sequence[str], cwd: Path, env: Optional[Mapping[str, str]] = None, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, env=None if env is None else dict(env), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise TrackRError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def has_object(repo: Path, sha: str, env: Optional[Mapping[str, str]] = None) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, env=env, capture_output=True).returncode == 0


def inside_repository(path: Path) -> Optional[Path]:
    """The nearest enclosing directory holding ``.git``, if any."""
    for candidate in (path.resolve(), *path.resolve().parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def require_outside_repository(path: Path, what: str) -> None:
    enclosing = inside_repository(path)
    if enclosing is not None:
        raise TrackRError(f"{what} {path} is inside the repository {enclosing}; choose a directory outside any repository")


def ensure_mirror(source: str, cache_dir: Path, env: Mapping[str, str]) -> Path:
    """A bare mirror of ``source`` under ``cache_dir``, fetched once and refreshed after (read-only)."""
    require_outside_repository(cache_dir, "the clone cache")
    mirror = cache_dir / "boundroom.git"
    if mirror.exists():
        git(["fetch", "--quiet", "--prune", "origin"], mirror, env)
    else:
        cache_dir.mkdir(parents=True, exist_ok=True)
        git(["clone", "--quiet", "--mirror", source, str(mirror)], cache_dir, env)
    return mirror


def clone_at(source: Path, sha: str, dest: Path, env: Mapping[str, str], *, history_only: bool = True) -> Path:
    """A clone of ``source`` with ``sha`` checked out, no remote and the driver's identity.

    With ``history_only`` every ref is deleted and unreachable objects are pruned, so the clone holds
    ``sha`` and its ancestors and nothing later: not the human solution, not a later tag.
    """
    git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", str(source), str(dest)], dest.parent, env)
    git(["checkout", "--quiet", "--detach", sha], dest, env)
    for remote in git(["remote"], dest, env).split():
        git(["remote", "remove", remote], dest, env)
    if history_only:
        for ref in git(["for-each-ref", "--format=%(refname)"], dest, env).split():
            git(["update-ref", "-d", ref], dest, env)
        git(["reflog", "expire", "--expire=now", "--all"], dest, env)
        git(["gc", "--quiet", "--prune=now"], dest, env)
    git(["config", "user.name", "Track R"], dest, env)
    git(["config", "user.email", "trackr@example.invalid"], dest, env)
    git(["config", "commit.gpgsign", "false"], dest, env)
    return dest


@dataclass(frozen=True)
class Overlay:
    """Paths replaced, in every clone, by their contents at one commit: the supervisor under test.

    ``run_next.py`` judges its own checkout, so without an overlay each task's session would run
    the supervisor that task's base shipped with, bugs included. The overlay is applied to both
    arms alike, so their starting checkouts stay identical.
    """

    commit: str
    paths: tuple[str, ...]

    def record(self) -> dict[str, Any]:
        return {"commit": self.commit, "paths": list(self.paths)}

    @classmethod
    def from_record(cls, data: Optional[Mapping[str, Any]]) -> Optional["Overlay"]:
        return cls(str(data["commit"]), tuple(data["paths"])) if data else None


# The v0.5.3 release commit, by the owner's decision: the pilot measures today's supervisor.
OVERLAY = Overlay(
    "b193297829c68d2243e5e32d1517cf0b51568813", ("automation/supervisor", "automation/schemas", "automation/context")
)


def apply_overlay(mirror: Path, clone: Path, overlay: Overlay, env: Mapping[str, str]) -> list[str]:
    """Replace the overlay's paths in ``clone`` with their contents at ``overlay.commit`` and stage it.

    Files come from ``git archive`` of the mirror into the working tree, so no object from after
    the clone's base is added to its history until the setup commit records the files themselves.
    Returns the staged paths the overlay changed.
    """
    if not has_object(mirror, overlay.commit, env):
        raise TrackRError(f"the overlay commit {overlay.commit} is not in {mirror}")
    for path in overlay.paths:
        target = clone / path
        if target.is_dir():
            subprocess.run(["rm", "-rf", str(target)], check=True)
        elif target.exists():
            target.unlink()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", overlay.commit, *overlay.paths],
        cwd=mirror,
        env=dict(env),
        capture_output=True,
        check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-f", "-", "-C", str(clone)], input=archive, check=True)
    git(["add", "-A", "--", *overlay.paths], clone, env)
    return git(["diff", "--cached", "--name-only"], clone, env).split()


def numstat(repo: Path, base: str, head: str, env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """``git diff --numstat base head``, with renames shown as their new path and binaries as 0 lines."""
    rows = []
    for line in git(["diff", "--numstat", "--no-renames", base, head], repo, env).splitlines():
        added, deleted, path = line.split("\t", 2)
        rows.append(
            {"path": path, "added": int(added) if added.isdigit() else 0, "deleted": int(deleted) if deleted.isdigit() else 0}
        )
    return rows


def name_status(repo: Path, base: str, head: str, env: Optional[Mapping[str, str]] = None) -> list[tuple[str, str]]:
    rows = []
    for line in git(["diff", "--name-status", "--no-renames", base, head], repo, env).splitlines():
        status, path = line.split("\t", 1)
        rows.append((status[0], path))
    return rows


# --- pytest ---------------------------------------------------------------------------------------

_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_SUMMARY = re.compile(r"^\d+ [a-z]+.* in [\d.]+s")


def pytest_counts(output: str) -> dict[str, int]:
    """The counts in pytest's last summary line (``2 failed, 10 passed, 1 error in 1.2s``)."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    summary = ""
    for line in output.splitlines():
        stripped = line.strip("= ").strip()
        if _SUMMARY.match(stripped):
            summary = stripped
    for number, kind in _COUNT.findall(summary):
        key = "errors" if kind.startswith("error") else kind
        if key in counts:
            counts[key] = int(number)
    return counts


def hidden_test_result(completed: Completed) -> dict[str, Any]:
    """One hidden-test run: ``passed`` only when pytest exits 0 having run at least one test."""
    counts = pytest_counts(completed.stdout + "\n" + completed.stderr)
    return {
        "exit_code": completed.returncode,
        "timed_out": completed.timed_out,
        **counts,
        "passed_all": completed.returncode == 0 and counts["passed"] > 0,
        # Fails at base: a failure or error, including an import or collection error (pytest exit 1 or 2).
        "failed_any": completed.returncode in (1, 2) or counts["failed"] > 0 or counts["errors"] > 0,
        "output_tail": tail(completed.stdout + completed.stderr),
    }


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


def install_hidden_tests(mirror: Path, head_sha: str, paths: Sequence[str], clone: Path, env: Mapping[str, str]) -> None:
    """Copy each hidden test file from ``head_sha`` in the mirror over the clone's copy, if any."""
    for path in paths:
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.run(
                ["git", "show", f"{head_sha}:{path}"], cwd=mirror, env=dict(env), capture_output=True, check=True
            ).stdout
        )


def run_hidden_tests(
    runner: CommandRunner, mirror: Path, head_sha: str, paths: Sequence[str], clone: Path, env: Mapping[str, str], timeout: float
) -> dict[str, Any]:
    install_hidden_tests(mirror, head_sha, paths, clone, env)
    return hidden_test_result(runner([*PYTEST, *paths], clone, env, timeout))


# --- files ----------------------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def scope_errors(task: Mapping[str, Any]) -> list[str]:
    """What is missing from a task's scope fields, which a blinded person fills in later."""
    errors = []
    if not str(task.get("task_statement") or "").strip():
        errors.append("task_statement is empty")
    allowed = task.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(p, str) and p.strip() for p in allowed):
        errors.append("allowed_paths needs at least one path")
    validations = task.get("required_validations")
    if not isinstance(validations, list) or not validations or not all(isinstance(c, str) and c.strip() for c in validations):
        errors.append("required_validations needs at least one command")
    budget = task.get("max_files_changed")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        errors.append("max_files_changed must be a whole number of at least 1")
    return errors


def task_id(task: Mapping[str, Any]) -> str:
    return f"pr{task['number']}"
