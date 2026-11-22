"""evaluate_handoff on a handoff the runner committed in a commit of its own.

A handoff can't name the commit that contains it, so run_next accepts the commit before a
handoff-only commit as the verified one (v0.5.2). evaluate_handoff must accept the same handoff.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from automation.supervisor import evaluate_handoff

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE = "automation/queue/slices.json"
HANDOFF = "automation/handoffs/S-1.json"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def commit(root: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", *files)
    git(root, "commit", "-q", "-m", ", ".join(files))
    return git(root, "rev-parse", "HEAD")


def handoff_text(verified_sha: str) -> str:
    handoff = json.loads((REPO_ROOT / "automation/examples/example-handoff.json").read_text(encoding="utf-8"))
    handoff.update(
        slice_id="S-1",
        files_touched=["docs/a.md"],
        validations_passed=["git diff --check"],
        verified_commit_sha=verified_sha,
    )
    return json.dumps(handoff, indent=2) + "\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A repository with the slice queued at base and its work committed after it."""
    # The user's global git config (signing, hooks) stays out of the test repositories, for these
    # git calls and for the ones evaluate_handoff makes, such as the replay's git worktree add.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    queue = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
    queue["slices"] = [
        {
            **queue["slices"][0],
            "slice_id": "S-1",
            "status": "queued",
            "allowed_paths": ["docs/a.md"],
            "required_validations": ["git diff --check"],
            "depends_on": [],
            "max_files_changed": 3,
        }
    ]
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "test")
    base = commit(tmp_path, {QUEUE: json.dumps(queue, indent=2) + "\n"})
    work = commit(tmp_path, {"docs/a.md": "work\n"})
    return tmp_path, base, work


def evaluate(root: Path, base: str, head: str) -> evaluate_handoff.EvaluationReport:
    return evaluate_handoff.evaluate(
        repo_root=root,
        queue_path=root / QUEUE,
        handoff_path=root / HANDOFF,
        slice_id="S-1",
        base_sha=base,
        head_sha=head,
        dry_run=True,
    )


def test_a_handoff_naming_the_commit_before_its_own_commit_is_accepted(repo: tuple[Path, str, str]) -> None:
    root, base, work = repo
    head = commit(root, {HANDOFF: handoff_text(work)})
    report = evaluate(root, base, head)
    assert (report.decision, report.reason) == ("accept", "Historical handoff evaluation passed all checks.")


def test_the_parent_is_not_accepted_when_the_head_commit_changes_more_than_the_handoff(
    repo: tuple[Path, str, str],
) -> None:
    root, base, work = repo
    head = commit(root, {HANDOFF: handoff_text(work), "docs/a.md": "changed after verifying\n"})
    report = evaluate(root, base, head)
    assert report.decision != "accept"
    assert "does not match --head-sha" in report.reason


def test_a_handoff_naming_an_unrelated_commit_is_refused(repo: tuple[Path, str, str]) -> None:
    root, base, _ = repo
    head = commit(root, {HANDOFF: handoff_text(base)})
    report = evaluate(root, base, head)
    assert report.decision != "accept"
    assert "does not match --head-sha" in report.reason
