"""Shared by the run_lanes tests: a real repository, a scripted agent, and the two supervisors run over it.

The agent is the one unmanaged dependency, so it is a script. Everything else is real: Git, the
queue file, the lane worktrees and the attempt records. Git's global and system configuration are
switched off and every commit's author, committer and date are fixed, so two repositories built
the same way, and the same agent run in each, give the same commit SHAs. That lets the parity tests
compare ``base_sha`` and ``head_sha`` as they are.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from automation.supervisor import run_lanes, run_next

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE = "automation/queue/slices.json"
FIXED_DATE = "2026-11-27T00:00:00+00:00"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
}

# The scripted agent: argv is the handoff file, the slice id, the behaviours file, the queue file
# and the directory it copies the queue into when it starts. Its working directory is the worktree
# the supervisor runs it in.
AGENT = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path

handoff, slice_id, behaviours, queue, snapshots = sys.argv[1:6]
behaviour = json.loads(Path(behaviours).read_text()).get(slice_id, "done")
Path(snapshots).mkdir(parents=True, exist_ok=True)
Path(snapshots, slice_id + ".json").write_text(Path(queue).read_text())
Path(snapshots, slice_id + ".cwd").write_text(os.getcwd())
attempts = Path.cwd().parent / ".attempts" / (Path.cwd().name + ".json")
if attempts.exists():
    Path(snapshots, slice_id + ".record").write_text(attempts.read_text())
setup_marker = Path("setup-ran.marker")
Path(snapshots, slice_id + ".setup").write_text("yes" if setup_marker.exists() else "no")
Path(snapshots, slice_id + ".session").write_text(str(os.getsid(0)))
repository = Path(queue).parents[2]

def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)

def commit(paths, text="change\n"):
    for path in paths:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    git("add", "--", *paths)
    git("commit", "-q", "-m", slice_id)

if behaviour.startswith("exit:"):
    sys.exit(int(behaviour.split(":")[1]))
if behaviour.startswith("signal:"):
    os.kill(int(behaviour.split(":")[1]), signal.SIGTERM)
    time.sleep(60)
if behaviour == "sleep":
    time.sleep(60)

record = next(item for item in json.loads(Path(queue).read_text())["slices"] if item["slice_id"] == slice_id)
own = "docs/" + slice_id + ".md"
touched = [own]
status = "done"
if behaviour in ("done", "commit-handoff", "edit-handoff", "failed", "blocked", "nohandoff"):
    commit([own])
    status = {"failed": "failed", "blocked": "blocked"}.get(behaviour, "done")
elif behaviour == "outside":
    touched = [own, "src/outside.md"]
    commit(touched)
elif behaviour == "whitespace":
    commit([own], "trailing   \n")
elif behaviour == "toomany":
    touched = [own, "docs/two.md", "docs/three.md"]
    commit(touched)
elif behaviour == "uncommitted":
    Path(own).parent.mkdir(parents=True, exist_ok=True)
    Path(own).write_text("left behind\n")
elif behaviour == "next-other":
    commit([own])
elif behaviour == "move-main":
    commit([own])
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "--allow-empty", "-m", "moved"], check=True)
elif behaviour == "switch-branch":
    commit([own])
    subprocess.run(["git", "-C", str(repository), "checkout", "-q", "-b", "elsewhere"], check=True)
elif behaviour == "block-ff":
    commit([own])
    Path(repository, own).write_text("an untracked file the fast-forward would overwrite\n")
if behaviour == "nohandoff":
    sys.exit(0)
recommended = "someone-else" if behaviour == "next-other" else ""
Path(handoff).parent.mkdir(parents=True, exist_ok=True)
Path(handoff).write_text(json.dumps({
    "slice_id": slice_id, "status": status, "summary": "Scripted.", "files_touched": touched,
    "validations_passed": record["required_validations"], "validations_failed": [], "proof_level": "doc-only",
    "missing_proof_levels": [], "contract_status_changes": [], "residual_risks": ["None known."],
    "recommended_next_slice": recommended, "recommended_next_reason": "", "repo_clean_status": "clean",
    "git_mirror_status": "not-checked", "dirty_paths_outside_scope": [], "timestamp": "2026-11-27T00:00:00Z"}))
if behaviour in ("commit-handoff", "edit-handoff"):
    git("add", "-f", "--", handoff)
    git("commit", "-q", "-m", slice_id + " handoff")
if behaviour == "edit-handoff":
    edited = json.loads(Path(handoff).read_text())
    edited["summary"] = "Edited after the commit."
    Path(handoff).write_text(json.dumps(edited))
"""


def slice_record(slice_id: str, priority: int, **fields: Any) -> dict:
    record = {
        "slice_id": slice_id,
        "title": slice_id,
        "status": "queued",
        "priority": priority,
        "domain": "example",
        "allowed_paths": ["docs/"],
        "required_validations": [],
        "depends_on": [],
        "max_files_changed": 1,
        "notes": "",
    }
    return record | fields


def queue_document(slices: list[dict], **policy_fields: Any) -> dict:
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 5,
            "handoff_timeout_seconds": 60,
            "agent_command_template": "",
            "supervisor_owned_paths": [QUEUE, "automation/handoffs/"],
        }
        | policy_fields,
        "slices": slices,
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def isolate_environment(monkeypatch: Any, state: Path) -> None:
    """No inherited Boundroom, telemetry or Git setting; fixed commit identity and dates; a canonical state directory."""
    for name in list(os.environ):
        if name.startswith(("BOUNDROOM_", "REPO_AUTOMATION_", "OWLORY_", "OTEL_", "GIT_")):
            monkeypatch.delenv(name)
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state.resolve()))


@dataclass
class Repo:
    """One repository for one supervisor run, and where its agent leaves what it saw."""

    root: Path
    work: Path  # the agent script, its behaviours and its snapshots live here, outside the repository

    @property
    def queue(self) -> Path:
        return self.root / QUEUE

    @property
    def snapshots(self) -> Path:
        return self.work / "snapshots"

    def statuses(self) -> dict[str, str]:
        return {item["slice_id"]: item["status"] for item in json.loads(self.queue.read_text(encoding="utf-8"))["slices"]}

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD")

    def agent_command(self) -> str:
        return " ".join(
            [
                shlex.quote(sys.executable),
                shlex.quote(str(self.work / "agent.py")),
                "{handoff_file}",
                "{slice_id}",
                shlex.quote(str(self.work / "behaviours.json")),
                shlex.quote(str(self.queue)),
                shlex.quote(str(self.snapshots)),
            ]
        )

    def behave(self, behaviours: dict[str, str]) -> None:
        (self.work / "behaviours.json").write_text(json.dumps(behaviours), encoding="utf-8")


def make_repo(base: Path, document: dict, *, ignore_handoffs: bool = True, extra_files: Optional[dict[str, str]] = None) -> Repo:
    """A committed repository holding the queue and the prompt fragments, as a consumer's sync leaves them."""
    root = (base / "repo").resolve()
    work = (base / "work").resolve()
    root.mkdir(parents=True)
    work.mkdir(parents=True)
    (work / "agent.py").write_text(AGENT, encoding="utf-8")
    shutil.copytree(REPO_ROOT / "automation/prompts", root / "automation/prompts")
    ignored = ["automation/handoffs/"] if ignore_handoffs else []
    (root / ".gitignore").write_text("".join(f"{line}\n" for line in [*ignored, "*.marker"]), encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/README.md").write_text("docs\n", encoding="utf-8")
    for relative, text in (extra_files or {}).items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    repo = Repo(root, work)
    repo.queue.parent.mkdir(parents=True)
    repo.queue.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    repo.behave({})
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return repo


def split_printed(stdout: str) -> tuple[list[dict], list[str]]:
    """The reports a run printed, as JSON, and every other line it printed."""
    decoder = json.JSONDecoder()
    reports: list[dict] = []
    others: list[str] = []
    position = 0
    while position < len(stdout):
        end = stdout.find("\n", position)
        end = len(stdout) if end < 0 else end
        if stdout.startswith("{", position):
            report, position = decoder.raw_decode(stdout, position)
            reports.append(report)
            continue
        line = stdout[position:end]
        if line.strip():
            others.append(line)
        position = end + 1
    return reports, others


@dataclass
class Outcome:
    exit_code: int
    stdout: str
    stderr: str
    reports: list[dict]
    queue_text: str
    snapshots: dict[str, str]
    head: str


def run(entry: Callable[..., int], module: Any, repo: Repo, flags: list[str], monkeypatch: Any) -> Outcome:
    """Run one supervisor's command-line entry over *repo*, as a person would, and collect what it left."""
    report_file = repo.work / "reports.jsonl"
    report_file.unlink(missing_ok=True)
    monkeypatch.setattr(module, "REPO_ROOT", repo.root)
    out, err = io.StringIO(), io.StringIO()
    argv = ["--queue", QUEUE, "--agent-cmd", repo.agent_command(), "--report-file", str(report_file), *flags]
    with redirect_stdout(out), redirect_stderr(err):
        code = entry(argv)
    reports = [json.loads(line) for line in report_file.read_text(encoding="utf-8").splitlines()] if report_file.exists() else []
    snapshots = (
        {path.name: path.read_text(encoding="utf-8") for path in sorted(repo.snapshots.glob("*.json"))}
        if repo.snapshots.exists()
        else {}
    )
    return Outcome(code, out.getvalue(), err.getvalue(), reports, repo.queue.read_text(encoding="utf-8"), snapshots, repo.head())


def run_next_over(repo: Repo, flags: list[str], monkeypatch: Any) -> Outcome:
    return run(run_next._cli_entry, run_next, repo, flags, monkeypatch)


def run_lanes_over(repo: Repo, flags: list[str], monkeypatch: Any) -> Outcome:
    return run(run_lanes._cli_entry, run_lanes, repo, flags, monkeypatch)


class _FixedClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _FixedClock:  # run_next asks for UTC, and the fixed time is UTC
        return cls(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


def freeze_run_next_clock(monkeypatch: Any) -> None:
    """Fix the clock run_next names handoffs by, so two runs name them alike and commit identical trees."""
    monkeypatch.setattr(run_next, "datetime", _FixedClock)


def lane_root(state: Path) -> Path:
    """The one lane root under *state*: the repository id is a hash, so it is found, not computed."""
    roots = list((state / "boundroom" / "lanes").iterdir())
    assert len(roots) == 1, roots
    return roots[0]


def normalize(value: Any, repositories: list[Path], lane_roots: list[Path], *, drop: tuple[str, ...] = ("lane",)) -> Any:
    """*value* with the worktree a slice ran in replaced by ``<worktree>``, and the *drop* keys left out.

    These are the fields SPEC-014 invariant 6 allows to differ: the worktree, so the handoff path
    under it, and the report's ``lane`` block that names it. A lane worktree is its lane root and
    attempt id; run_next's is the repository. *repositories* also holds each run's agent directory,
    which the test's agent command names.
    """
    patterns = [re.compile(re.escape(str(root)) + r"/[A-Za-z0-9][A-Za-z0-9._-]*") for root in lane_roots]
    patterns += [re.compile(re.escape(str(path))) for path in sorted(repositories, key=lambda path: -len(str(path)))]
    return _replace(value, patterns, drop)


def _replace(value: Any, patterns: list[re.Pattern[str]], drop: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _replace(item, patterns, drop) for key, item in value.items() if key not in drop}
    if isinstance(value, list):
        return [_replace(item, patterns, drop) for item in value]
    if isinstance(value, str):
        for pattern in patterns:
            value = pattern.sub("<worktree>", value)
        # subprocess reports the time left on its timer, such as 4.99991 or 5, so two runs of
        # run_next itself differ here. This is the one normalized field that doesn't name a worktree.
        return TIMED_OUT.sub(r"\1<seconds> seconds", value)
    return value


TIMED_OUT = re.compile(r"(timed out after )[0-9.]+ seconds?")
