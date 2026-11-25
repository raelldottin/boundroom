"""What the Track R selection, driver, oracle and pilot share: clones, commands and the environment.

Every clone made here is disposable, lives outside any repository and has no remote. A
session's clone also holds no object from after its base commit, so the human solution can't
be found with ``git log --all`` or ``git cat-file``.
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
TASKS_FILE = Path(__file__).with_name("tasks.pilot.json")
UPSTREAM = "https://github.com/raelldottin/boundroom.git"
UPSTREAM_REPOSITORY = "raelldottin/boundroom"

TIMEOUT_RETURNCODE = 124  # timeout(1)'s code, as the benchmark and run_next read it
TERMINATION_GRACE_SECONDS = 5

# The fields a blinded scope writer fills in from the pull request's description alone, without its diff.
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
class BasePatch:
    """A minimal fix applied to every clone's base, in both arms, so the supervisor can run at all.

    ``run_next.py`` judges its own checkout, so each session runs the supervisor its task's base
    shipped with. Before #279, that supervisor gave a handoff outside the repository the rule
    ``Edit(/abs/path)``, which Claude Code reads as relative to the settings file, so the runner
    could never write its handoff. The patch is #279's runner_profile.py change and nothing else: no
    test, no changelog, no other supervisor code. That keeps each base's own tests consistent with
    its own supervisor (a full v0.5.3 overlay did not, see the README's amendments).
    """

    id: str
    source_commit: str
    path: str

    def record(self) -> dict[str, Any]:
        return {"id": self.id, "source_commit": self.source_commit, "path": self.path}

    @classmethod
    def from_record(cls, data: Optional[Mapping[str, Any]]) -> Optional["BasePatch"]:
        return cls(str(data["id"]), str(data["source_commit"]), str(data["path"])) if data else None


# #279's merge commit: "let a bounded runner write a handoff outside the repository".
BASE_PATCH = BasePatch(
    "pr279-outside-handoff-rule", "30ab82bb1854079776e98b1499f2816e49a3d5f4", "automation/supervisor/runner_profile.py"
)

_UNPATCHED_RULE = 'allow.append(f"Edit({handoff})")'
_PATCHED_RULE = 'allow.append(f"Edit(/{handoff})" if os.path.isabs(handoff) else f"Edit({handoff})")'
_FIXED_MARKER = 'f"Edit(/{handoff})"'


class PatchError(TrackRError):
    """The base patch could not be applied to a base, or did not take."""


def patch_runner_profile(text: str) -> tuple[str, str]:
    """#279's handoff-rule fix applied to one base's runner_profile.py. Returns (text, status).

    Deterministic and minimal: the one rule-building line is rewritten so an absolute handoff gets
    ``Edit(//abs)``. #279's other half, no ``git add`` rule for an outside handoff, needs no edit:
    a base either has no ``git add`` rule for the handoff at all or already guards it against an
    absolute path. ``verify_patch`` checks both halves by calling the patched code.
    """
    if _FIXED_MARKER in text:
        return text, "already present"
    if text.count(_UNPATCHED_RULE) != 1:
        raise PatchError(
            f"#279's handoff rule: expected one {_UNPATCHED_RULE!r} in runner_profile.py, found {text.count(_UNPATCHED_RULE)}"
        )
    if not re.search(r"^import os$", text, re.M):
        raise PatchError("#279's handoff rule needs os.path.isabs, but runner_profile.py does not import os")
    return text.replace(_UNPATCHED_RULE, _PATCHED_RULE), "applied"


# Run in the clone after uv sync: what the patched slice_settings gives a handoff inside and outside.
VERIFY_SCRIPT = r"""
import inspect, json, os, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())
from automation.supervisor import runner_profile as rp
profile = rp.choose({"runner_profile": "bounded"}, {})
record = {"allowed_paths": ["src/"], "required_validations": ["true"]}
policy = {"supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"]}
out = {}
for name, handoff in (("outside", "/trackr-verify/handoffs/x.json"), ("inside", "automation/handoffs/x.json")):
    kwargs = {"handoff": handoff}
    if "repo_root" in inspect.signature(rp.slice_settings).parameters:
        kwargs["repo_root"] = Path(os.getcwd())
    out[name] = rp.slice_settings(record, policy, profile, **kwargs)["permissions"]["allow"]
print(json.dumps(out))
"""
VERIFY_ARGV = ("uv", "run", "--frozen", "python", "-c", VERIFY_SCRIPT)


def verify_patch(clone: Path, env: Mapping[str, str], runner: CommandRunner) -> dict[str, Any]:
    """Check the patch took, by calling the clone's own slice_settings. Raises PatchError if not."""
    # No bytecode: the check must leave the setup tree exactly as the patch left it.
    completed = runner(list(VERIFY_ARGV), clone, {**env, "PYTHONDONTWRITEBYTECODE": "1"}, 120)
    try:
        rules = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise PatchError(f"could not check #279's handoff rule: {tail(completed.stdout + completed.stderr, 8)}") from error
    outside, inside = "/trackr-verify/handoffs/x.json", "automation/handoffs/x.json"
    checks = {
        "outside_handoff_is_absolute": f"Edit(/{outside})" in rules["outside"],
        "outside_handoff_has_no_single_slash_rule": f"Edit({outside})" not in rules["outside"],
        "outside_handoff_has_no_git_add": f"Bash(git add {outside})" not in rules["outside"],
        "inside_handoff_unchanged": f"Edit({inside})" in rules["inside"],
    }
    if not all(checks.values()):
        raise PatchError("#279's handoff rule did not take: " + ", ".join(name for name, ok in checks.items() if not ok))
    return checks


def apply_base_patch(clone: Path, patch: BasePatch, env: Mapping[str, str]) -> str:
    """Rewrite the patched file in the working tree and stage it. Returns the patch status."""
    target = clone / patch.path
    if not target.is_file():
        raise PatchError(f"{patch.path} is missing at this base, so #279's handoff fix cannot be applied")
    text, status = patch_runner_profile(target.read_text(encoding="utf-8"))
    target.write_text(text, encoding="utf-8")
    git(["add", "--", patch.path], clone, env)
    return status


# The validations every blinded scope written so far chose. The green-base check uses a task's own
# required_validations once a blinded scope exists, and these for a candidate that has none yet.
DEFAULT_REQUIRED_VALIDATIONS = (
    "git diff --check",
    "UV_PYTHON=3.12 make reusable-check",
    "NO_MKDOCS_2_WARNING=1 uv run mkdocs build --strict",
)
VALIDATION_TIMEOUT_SECONDS = 1800


def prepare_setup(clone: Path, patch: Optional[BasePatch], env: Mapping[str, str], runner: CommandRunner) -> dict[str, Any]:
    """Apply the base patch, check it took, and stage it. Needs ``uv sync`` first.

    Every clone a session, the oracle check or the green-base check starts from is made this way, so
    they all start from the same tree. Nothing is regenerated: the patch touches no file any other
    file is generated from. Returns what was done, for the record.
    """
    if patch is None:
        return {"patch": None, "staged": git(["diff", "--cached", "--name-only"], clone, env).split()}
    status = apply_base_patch(clone, patch, env)
    checks = verify_patch(clone, env, runner)
    return {
        "patch": {**patch.record(), "status": status, "verified": checks},
        "staged": git(["diff", "--cached", "--name-only"], clone, env).split(),
    }


def run_validations(commands: Sequence[str], clone: Path, env: Mapping[str, str], runner: CommandRunner) -> dict[str, Any]:
    """Run each command at the clone's committed setup, with nothing else changed. Starts no model."""
    results = []
    for command in commands:
        completed = runner(["/bin/sh", "-c", command], clone, env, VALIDATION_TIMEOUT_SECONDS)
        output = completed.stdout + completed.stderr
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "timed_out": completed.timed_out,
                "passed": completed.returncode == 0,
                "wall_seconds": completed.wall_seconds,
                "failures": [line for line in output.splitlines() if line.startswith(("FAILED", "ERROR"))][:30],
                "output_tail": tail(output, 12),
            }
        )
    return {"green": all(item["passed"] for item in results), "validations": results}


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


def install_hidden_tests(
    mirror: Path, head_sha: str, paths: Sequence[str], clone: Path, env: Mapping[str, str], support: Sequence[str] = ()
) -> None:
    """Copy each hidden test file, and each support file the tests need (fixtures, helpers), from
    ``head_sha`` in the mirror over the clone's copy, if any."""
    for path in [*support, *paths]:
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.run(
                ["git", "show", f"{head_sha}:{path}"], cwd=mirror, env=dict(env), capture_output=True, check=True
            ).stdout
        )


def run_hidden_tests(
    runner: CommandRunner,
    mirror: Path,
    head_sha: str,
    paths: Sequence[str],
    clone: Path,
    env: Mapping[str, str],
    timeout: float,
    support: Sequence[str] = (),
) -> dict[str, Any]:
    install_hidden_tests(mirror, head_sha, paths, clone, env, support)
    return hidden_test_result(runner([*PYTEST, *paths], clone, env, timeout))


# --- files ----------------------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# The scope's provenance, as the blinded scope writers record it. ``owner_decision`` is optional.
SCOPE_PROVENANCE_FIELDS = ("by", "when", "inputs", "leakage_ignored")
SCOPE_PROVENANCE_OPTIONAL = ("owner_decision",)


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
    if inputs and not re.search(r"\bno (repository, )?diff\b|without (its |the )?diff|title and body only", inputs):
        errors.append("scope_provenance.inputs must say the scope was written from the title and body only, without the diff")
    if re.search(r"\b(saw|seen|read|looked at|used) (the |its )?(diff|files changed|changed files)\b", inputs) and not re.search(
        r"\bno(t)? .{0,20}(diff|files)", inputs
    ):
        errors.append("scope_provenance.inputs says the diff was seen, so the scope is not blind")
    return errors


def task_id(task: Mapping[str, Any]) -> str:
    return f"pr{task['number']}"
