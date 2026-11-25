"""What the Track R selection, driver, oracle and pilot share: clones, commands and the environment.

Track R v2 runs against an external target, python-semver, with Boundroom v0.5.3 synced beside its
code. Every clone made here is disposable, lives outside any repository and has no remote. A
session's clone holds no object from after its base commit, so the human solution can't be found
with ``git log --all`` or ``git cat-file``. Every git command the driver runs disables hooks and
fsmonitor, so nothing an agent planted under ``.git`` runs with the driver's network access.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
TASKS_FILE = HERE / "tasks.v2.json"
SOLUTIONS_FILE = HERE / "solutions.v2.json"
V1_TASKS_FILE = HERE / "tasks.pilot.json"  # void; kept as the record of v1

# The target: python-semver (BSD-3-Clause), chosen by the owner on 2026-11-25.
TARGET_UPSTREAM = "https://github.com/python-semver/python-semver.git"
TARGET_REPOSITORY = "python-semver/python-semver"
TARGET_MIRROR = "python-semver.git"
# The supervisor beside it: Boundroom's v0.5.3 release commit, synced by its own sync script.
SUPERVISOR_UPSTREAM = "https://github.com/raelldottin/boundroom.git"
SUPERVISOR_MIRROR = "boundroom.git"
SUPERVISOR_RELEASE = "b193297829c68d2243e5e32d1517cf0b51568813"
MODEL = "claude-opus-5-5"

TIMEOUT_RETURNCODE = 124  # timeout(1)'s code, as the benchmark and run_next read it
TERMINATION_GRACE_SECONDS = 5

# The fields a blinded scope writer fills in from the linked issue or the pull request's title alone.
SCOPE_FIELDS = ("task_statement", "allowed_paths", "required_validations", "max_files_changed")
# The example queue's supervisor-owned paths, used only in arm S's queue policy. The oracle exempts none.
SUPERVISOR_OWNED_PATHS = ("automation/queue/slices.json", "automation/handoffs/")

# The hidden-test command, inside the sandbox, with the clone's own offline venv.
PYTEST = (".venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider")
TEST_FILE = re.compile(r"^tests/test_[^/]*\.py$")
# The checks a candidate's base must pass when it has no blinded scope yet: the commands the
# target's own CI runs, in the clone's venv. A blinded scope's own required_validations replace them.
DEFAULT_REQUIRED_VALIDATIONS = ("git diff --check", ".venv/bin/python -m pytest -q")

# Every git command the driver runs: no hooks, no fsmonitor, so a planted .git/config or hook runs nothing.
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

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


class Interrupted(BaseException):
    """The pilot or driver got SIGTERM, SIGINT or SIGHUP. A BaseException, so no ``except Exception`` swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by {signal.Signals(signum).name}")
        self.signum = signum


# Process groups started by subprocess_runner and not yet collected, so an exit path can kill them.
ACTIVE_GROUPS: set[int] = set()
INTERRUPT_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def _raise_interrupted(signum: int, frame: Any) -> None:
    raise Interrupted(signum)


def kill_active_groups() -> None:
    """SIGTERM every live session group, then SIGKILL after the grace period. Safe to call twice."""
    groups = list(ACTIVE_GROUPS)
    for pid in groups:
        _kill_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while groups and time.monotonic() < deadline:
        groups = [pid for pid in groups if _group_alive(pid)]
        time.sleep(0.1)
    for pid in groups:
        _kill_group(pid, signal.SIGKILL)


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def install_interrupt_handlers() -> None:
    """Turn SIGTERM, SIGINT and SIGHUP into ``Interrupted``, and kill live session groups at any exit.

    Without this, SIGTERM ends the pilot at once and the session it started (``run_next.py`` and the
    ``claude`` under it, in their own process group) keeps running, unwatched and still spending.
    """
    for signum in INTERRUPT_SIGNALS:
        signal.signal(signum, _raise_interrupted)
    atexit.register(kill_active_groups)


def subprocess_runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Completed:
    """Run a command in its own process group, and treat running out of time as a result.

    Output goes to temporary files and the wait is on the process itself, not on its pipes: a
    background child that inherited stdout or stderr (a tool a session left running) would otherwise
    hold a pipe open and make a finished command look like a timeout. Once the command exits, or
    runs out of time, the whole group gets SIGTERM, then SIGKILL after a grace period, so no process
    it started outlives it. On ``Interrupted`` (or any other exception) the group gets the same, so
    Claude Code can flush its transcript, and the exception goes on to the caller.
    """
    started = time.monotonic()
    timed_out = False
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err,
    ):
        process = subprocess.Popen(
            list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True
        )
        ACTIVE_GROUPS.add(process.pid)
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _kill_group(process.pid, signal.SIGKILL)
                    process.wait()
        except BaseException:
            kill_active_groups()
            process.wait()
            raise
        finally:
            # Whatever the command left running in its group goes too.
            _kill_group(process.pid, signal.SIGTERM)
            _kill_group(process.pid, signal.SIGKILL)
            ACTIVE_GROUPS.discard(process.pid)
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(), err.read()
    return Completed(
        returncode=TIMEOUT_RETURNCODE if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        wall_seconds=round(time.monotonic() - started, 3),
    )


# --- environment ----------------------------------------------------------------------------------


def isolated_environment(environ: Mapping[str, str], private_dir: Path) -> dict[str, str]:
    """``environ`` without the stripped names, with Git and gh pointed at empty, private config.

    ``private_dir`` must be outside the clone. Git reads an empty global file and no system file,
    so no credential helper, hook path or identity of the operator's applies; gh reads an empty
    config directory, and its token variables are stripped, so it has no account.

    No variable is added beyond those. Telemetry is off by default once every ``BOUNDROOM_*`` name
    is stripped. Setting the ``BOUNDROOM_TELEMETRY=off`` kill switch here, as an earlier version
    did, broke the repository's own telemetry tests inside ``make reusable-check`` for the agent,
    the green-base check and the oracle alike.
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
        }
    )
    return env


def recorded_env_names(env: Mapping[str, str]) -> list[str]:
    return sorted(name for name in env if name.startswith(RECORDED_ENV_PREFIXES))


# --- git ---------------------------------------------------------------------------------------


def git(args: Sequence[str], cwd: Path, env: Optional[Mapping[str, str]] = None, check: bool = True) -> str:
    """One git command with hooks and fsmonitor off (``SAFE_GIT``)."""
    result = subprocess.run(
        ["git", *SAFE_GIT, *args], cwd=cwd, env=None if env is None else dict(env), capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise TrackRError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def git_bytes(args: Sequence[str], cwd: Path, env: Optional[Mapping[str, str]] = None) -> bytes:
    result = subprocess.run(["git", *SAFE_GIT, *args], cwd=cwd, env=None if env is None else dict(env), capture_output=True)
    if result.returncode != 0:
        raise TrackRError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def has_object(repo: Path, sha: str, env: Optional[Mapping[str, str]] = None) -> bool:
    result = subprocess.run(["git", *SAFE_GIT, "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, env=env, capture_output=True)
    return result.returncode == 0


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


def ensure_mirror(source: str, cache_dir: Path, env: Mapping[str, str], name: str = TARGET_MIRROR) -> Path:
    """A bare mirror of ``source`` under ``cache_dir``, fetched once and refreshed after (read-only)."""
    require_outside_repository(cache_dir, "the clone cache")
    mirror = cache_dir / name
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


def numstat(repo: Path, base: str, head: str, env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """``git diff -z --numstat base head``: every path verbatim, binaries as 0 lines, no rename pairing."""
    fields = git_bytes(["diff", "-z", "--numstat", "--no-renames", base, head], repo, env).decode("utf-8", "surrogateescape")
    rows = []
    for record in fields.split("\0"):
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        rows.append(
            {"path": path, "added": int(added) if added.isdigit() else 0, "deleted": int(deleted) if deleted.isdigit() else 0}
        )
    return rows


def name_status(repo: Path, base: str, head: str, env: Optional[Mapping[str, str]] = None) -> list[tuple[str, str]]:
    """``git diff -z --name-status base head``: (status letter, path) with every path verbatim."""
    fields = git_bytes(["diff", "-z", "--name-status", "--no-renames", base, head], repo, env).decode("utf-8", "surrogateescape")
    parts = [part for part in fields.split("\0")]
    rows = []
    index = 0
    while index + 1 < len(parts) and parts[index]:
        rows.append((parts[index][0], parts[index + 1]))
        index += 2
    return rows


def status_paths(repo: Path, env: Optional[Mapping[str, str]] = None) -> list[str]:
    """``git status -z --porcelain --untracked-files=all``: every dirty path verbatim."""
    fields = git_bytes(["status", "-z", "--porcelain", "--untracked-files=all"], repo, env).decode("utf-8", "surrogateescape")
    paths = []
    records = fields.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        paths.append(record[3:])
        if record[0] in "RC":  # a rename or copy is followed by its old path
            index += 1
    return paths


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


def install_hidden_tests(
    mirror: Path, head_sha: str, paths: Sequence[str], clone: Path, env: Mapping[str, str], support: Sequence[str] = ()
) -> None:
    """Copy each hidden test file, and each support file the tests need (fixtures, helpers), from
    ``head_sha`` in the mirror over the clone's copy, if any."""
    for path in [*support, *paths]:
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git_bytes(["show", f"{head_sha}:{path}"], mirror, env))


# --- files ----------------------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# The scope's provenance, as the blinded scope writers record it. ``owner_decision`` is optional.
SCOPE_PROVENANCE_FIELDS = ("by", "when", "inputs", "leakage_ignored")
SCOPE_PROVENANCE_OPTIONAL = ("owner_decision", "rerun")


def scope_errors(task: Mapping[str, Any]) -> list[str]:
    """What is missing from a task's scope, which a blinded person fills in later.

    The scope counts only with its provenance: who wrote it, when, from what, and what they set
    aside as leakage. A scope whose inputs say the diff or the files were seen is refused.
    """
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
    errors += provenance_errors(task.get("scope_provenance"))
    return errors


def provenance_errors(provenance: Any) -> list[str]:
    if not isinstance(provenance, Mapping):
        return ["scope_provenance is missing"]
    errors = [
        f"scope_provenance.{name} is empty" for name in SCOPE_PROVENANCE_FIELDS if not str(provenance.get(name) or "").strip()
    ]
    unknown = sorted(set(provenance) - set(SCOPE_PROVENANCE_FIELDS) - set(SCOPE_PROVENANCE_OPTIONAL))
    if unknown:
        errors.append("scope_provenance has unknown keys: " + ", ".join(unknown))
    inputs = str(provenance.get("inputs") or "").lower()
    if inputs and not re.search(r"\bno (repository, )?diff\b|without (its |the )?diff", inputs):
        errors.append("scope_provenance.inputs must say the scope was written without the diff (for example: no diff)")
    if re.search(r"\b(saw|seen|read|looked at|used) (the |its )?(diff|files changed|changed files)\b", inputs) and not re.search(
        r"\bno(t)? .{0,20}(diff|files)", inputs
    ):
        errors.append("scope_provenance.inputs says the diff was seen, so the scope is not blind")
    return errors


def task_id(task: Mapping[str, Any]) -> str:
    return f"pr{task['number']}"
