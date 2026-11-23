#!/usr/bin/env python3
"""Watch the Boundroom supervisor reject a handoff that its git diff contradicts.

The demo needs no agent and no API key. In a temporary git repository it queues one slice
that may change one file under ``docs/workflows/``, then makes a scripted "agent" commit that
changes three files, one of them ``README.md``. The handoff it writes claims the slice is done
and that it touched one file. The supervisor's ``evaluate_handoff.py --dry-run`` compares that
claim with the diff it computes itself, and rejects it.

Run it from the repository root::

    make demo-reject                                   # in Boundroom
    uv run python automation/examples/demo_reject.py   # anywhere Boundroom is synced

The supervisor it runs is this checkout's: the demo copies ``automation/supervisor``,
``automation/schemas`` and ``automation/context`` into the temporary repository, because
``evaluate_handoff.py`` treats the repository it sits in as the one it evaluates. Nothing in
this checkout changes. The temporary directory is removed afterwards unless ``--keep`` is given.

Exit status: 0 when the supervisor rejected the handoff for the out-of-scope file, as expected;
1 when it decided anything else; 2 when the demo itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_DIRS = ("automation/supervisor", "automation/schemas", "automation/context")
EVALUATE_HANDOFF = "automation/supervisor/evaluate_handoff.py"
QUEUE = "automation/queue/slices.json"
SLICE_ID = "demo-workflow-guide"
EXPECTED_OUT_OF_SCOPE = ["README.md"]

SLICE: dict[str, Any] = {
    "slice_id": SLICE_ID,
    "title": "Clarify the workflow guide",
    "status": "queued",
    "priority": 10,
    "domain": "docs",
    "allowed_paths": ["docs/workflows/"],
    "required_validations": ["git diff --check"],
    "depends_on": [],
    "max_files_changed": 1,
    "notes": "Demo slice: one file under docs/workflows/.",
    "required_proof_level": "doc-only",
}

QUEUE_DATA: dict[str, Any] = {
    "version": 1,
    "policy": {
        "consecutive_autonomous_limit": 1,
        "handoff_timeout_seconds": 1800,
        "agent_command_template": "automation/supervisor/run_agent.sh --repo-root {repo_root} --prompt-file {prompt_file} "
        "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}",
        "supervisor_owned_paths": [QUEUE, "automation/handoffs/"],
    },
    "slices": [SLICE],
}

BASE_FILES = {
    "README.md": "# Demo project\n",
    "docs/workflows/guide.md": "# Workflow guide\n\nStep one.\n",
}

# The scripted agent stays in scope for two files and strays into README.md for the third.
AGENT_FILES = {
    "docs/workflows/guide.md": "# Workflow guide\n\nStep one.\nStep two.\n",
    "docs/workflows/faq.md": "# Workflow FAQ\n",
    "README.md": "# Demo project\n\nSee docs/workflows/guide.md.\n",
}

CLAIMED_FILES = ["docs/workflows/guide.md"]


class DemoError(Exception):
    """The demo could not stage or run its scenario."""


@dataclass
class DemoResult:
    base_sha: str
    head_sha: str
    handoff: dict[str, Any]
    diff_files: list[str]
    report: dict[str, Any]
    supervisor_exit: int


def isolated_env() -> dict[str, str]:
    """The environment for git and the supervisor, free of the caller's git state and config.

    A git hook exports GIT_DIR and friends, which would point every git call back at the
    caller's repository, and a global config may sign commits or run hooks.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Boundroom demo",
        GIT_AUTHOR_EMAIL="demo@example.invalid",
        GIT_COMMITTER_NAME="Boundroom demo",
        GIT_COMMITTER_EMAIL="demo@example.invalid",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, env=isolated_env(), capture_output=True, text=True)
    if result.returncode != 0:
        raise DemoError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def write_files(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def copy_supervisor(source_root: Path, repo: Path) -> None:
    for relative in SUPERVISOR_DIRS:
        source = source_root / relative
        if not source.is_dir():
            raise DemoError(f"{relative}/ is missing from {source_root}; run the demo from a checkout with Boundroom synced.")
        shutil.copytree(source, repo / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def build_handoff(head_sha: str) -> dict[str, Any]:
    """A handoff valid against automation/schemas/handoff.schema.json that understates the work."""
    return {
        "slice_id": SLICE_ID,
        "status": "done",
        "summary": "Added step two to the workflow guide.",
        "files_touched": list(CLAIMED_FILES),
        "validations_passed": ["git diff --check"],
        "validations_failed": [],
        "proof_level": "doc-only",
        "missing_proof_levels": [],
        "contract_status_changes": [],
        "residual_risks": ["None: the guide change is small."],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "repo_clean_status": "clean",
        "git_mirror_status": "not-checked",
        "dirty_paths_outside_scope": [],
        "timestamp": "2026-01-01T00:00:00Z",
        "verified_commit_sha": head_sha,
    }


def run_demo(workdir: Path, source_root: Path = SOURCE_ROOT) -> DemoResult:
    """Stage the scenario under *workdir* and return what the supervisor decided."""
    repo = workdir / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    copy_supervisor(source_root, repo)
    write_files(repo, {QUEUE: json.dumps(QUEUE_DATA, indent=2) + "\n", **BASE_FILES})
    base_sha = commit_all(repo, "Queue the demo slice")

    write_files(repo, AGENT_FILES)
    head_sha = commit_all(repo, "Scripted agent: update the workflow guide")
    diff_files = git(repo, "diff", "--name-only", base_sha, head_sha).splitlines()

    # Outside the repository, so the handoff is not part of the diff it describes.
    handoff = build_handoff(head_sha)
    handoff_path = workdir / "handoff.json"
    handoff_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")

    command = [
        sys.executable,
        str(repo / EVALUATE_HANDOFF),
        "--slice-id",
        SLICE_ID,
        "--handoff",
        str(handoff_path),
        "--base-sha",
        base_sha,
        "--head-sha",
        head_sha,
        "--dry-run",
        "--no-telemetry",
    ]
    completed = subprocess.run(command, cwd=repo, env=isolated_env(), capture_output=True, text=True)
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        raise DemoError(
            f"evaluate_handoff.py exited {completed.returncode} without a JSON report.\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        ) from None
    if not isinstance(report, dict):
        raise DemoError(f"evaluate_handoff.py printed {type(report).__name__}, not a report object.")
    return DemoResult(base_sha, head_sha, handoff, diff_files, report, completed.returncode)


def rejected_as_expected(result: DemoResult) -> bool:
    return (
        result.report.get("decision") == "reject"
        and result.report.get("out_of_scope_files") == EXPECTED_OUT_OF_SCOPE
        and result.supervisor_exit == 1
    )


def render(result: DemoResult) -> str:
    report = result.report
    out_of_scope = set(report.get("out_of_scope_files") or [])
    claimed = list(result.handoff["files_touched"])
    shown = [f"{name}  <- outside allowed_paths" if name in out_of_scope else name for name in result.diff_files]
    left_width = max(len("Handoff claims"), *(len(name) for name in claimed)) + 4

    def row(label: str, left: str, right: str) -> str:
        return f"  {label:<15}{left:<{left_width}}{right}".rstrip()

    lines = [
        "Boundroom demo: does the handoff match the diff?",
        "",
        f"Slice {SLICE_ID}",
        f"  allowed_paths      {', '.join(SLICE['allowed_paths'])}",
        f"  max_files_changed  {SLICE['max_files_changed']}",
        f"  proof level        {SLICE['required_proof_level']}",
        f"  base..head         {result.base_sha[:12]}..{result.head_sha[:12]}",
        "",
        row("", "Handoff claims", "Git diff shows"),
        row("status", result.handoff["status"], ""),
        row("files changed", str(len(claimed)), str(len(result.diff_files))),
    ]
    for index in range(max(len(claimed), len(shown))):
        lines.append(row("", claimed[index] if index < len(claimed) else "", shown[index] if index < len(shown) else ""))
    lines += [
        "",
        "Supervisor: evaluate_handoff.py --dry-run",
        f"  decision           {report.get('decision')}",
        f"  reason             {report.get('reason')}",
        f"  changed_file_count {report.get('changed_file_count')}",
        f"  out_of_scope_files {json.dumps(report.get('out_of_scope_files'))}",
        f"  exit code          {result.supervisor_exit}",
        "",
    ]
    if rejected_as_expected(result):
        lines.append("Rejected as expected: the diff touches README.md, outside allowed_paths. --dry-run changed no queue.")
    else:
        lines.append("UNEXPECTED: the supervisor did not reject the handoff for README.md. Its report:")
        lines.append(json.dumps(report, indent=2))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--keep", action="store_true", help="Keep the temporary repository and print where it is.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="boundroom-demo-"))
    try:
        result = run_demo(workdir)
    except (DemoError, OSError) as error:
        print(f"demo failed: {error}", file=sys.stderr)
        return 2
    finally:
        if args.keep:
            print(f"Kept the temporary repository: {workdir / 'repo'} (handoff: {workdir / 'handoff.json'})")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    print(render(result))
    return 0 if rejected_as_expected(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
