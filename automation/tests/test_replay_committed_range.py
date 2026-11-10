"""The supervisor checks what a slice committed, not only what it left uncommitted.

The runner prompt asks the runner to commit and push before it hands off. The RA-BD-004 Claude Code
canary showed the consequence: the supervisor's ``git diff --check`` replay saw an empty diff, and
its scope check and file count saw only uncommitted paths plus the agent's own report. These tests
pin the fix: both are measured from the commit the slice started at.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from automation.supervisor import local_board as lb
from automation.supervisor import policy, run_next
from automation.tests import test_run_next_kanban, test_run_next_local_board

consumer = test_run_next_kanban.consumer
no_hermes = test_run_next_local_board.no_hermes

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_POLICY = json.loads((REPO_ROOT / "automation/examples/kanban-overlay.json").read_text(encoding="utf-8"))["policy"]


def git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def replay(repo: Path, base_sha: str = "") -> policy.ValidationReplayResult:
    [result] = policy.replay_validation_commands(repo, ["git diff --check"], ["git diff --check"], base_sha=base_sha)
    return result


# The replay.


def test_the_replay_compares_with_the_base_commit_when_it_has_one():
    assert policy.replay_argv("git diff --check", "abc123") == ["git", "diff", "--check", "abc123"]
    assert policy.replay_argv("git diff --check") == ["git", "diff", "--check"]
    assert policy.replay_argv("make architecture", "abc123") == ["make", "architecture"]


def test_a_committed_whitespace_error_fails_the_replay(repo):
    base = policy.git_head(repo)
    (repo / "loop.py").write_text("x = 1 \n", encoding="utf-8")  # trailing whitespace
    git(repo, "add", "loop.py")
    git(repo, "commit", "-q", "-m", "slice")

    assert not replay(repo, base).success
    assert replay(repo).success  # what the supervisor did before: an empty diff passes


def test_a_clean_commit_and_an_uncommitted_error_are_both_judged(repo):
    base = policy.git_head(repo)
    (repo / "loop.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "loop.py")
    git(repo, "commit", "-q", "-m", "slice")
    assert replay(repo, base).success

    (repo / "loop.py").write_text("x = 2 \n", encoding="utf-8")  # left uncommitted
    assert not replay(repo, base).success


# The slice's committed paths.


def test_committed_paths_list_both_sides_of_a_rename(repo):
    base = policy.git_head(repo)
    git(repo, "mv", "README.md", "NOTES.md")
    (repo / "src").mkdir()
    (repo / "src/loop.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "slice")

    assert policy.git_committed_paths(repo, base) == ["NOTES.md", "README.md", "src/loop.py"]


def test_a_head_that_no_longer_descends_from_the_base_is_refused(repo):
    (repo / "loop.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "loop.py")
    git(repo, "commit", "-q", "-m", "second")
    base = policy.git_head(repo)
    git(repo, "reset", "-q", "--hard", "HEAD~1")

    with pytest.raises(policy.ConfigError, match="no longer descends"):
        policy.git_committed_paths(repo, base)


def test_a_repository_without_commits_has_no_base(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")

    with pytest.raises(policy.ConfigError, match="at least one commit"):
        policy.git_head(empty)


# End to end: run_next on a local board, with an agent that commits, as the runner prompt asks.

AGENT = """
import json, subprocess, sys
handoff, slice_id, mode = sys.argv[1], sys.argv[2], sys.argv[3]
def write(path, text):
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").write(text)
write("src/loop.py", "x = 1 \\n" if mode == "whitespace" else "x = 1\\n")
if mode == "out-of-scope":
    write("docs/unrelated.md", "not this slice's file\\n")
subprocess.run(["git", "add", "src/loop.py"] + (["docs/unrelated.md"] if mode == "out-of-scope" else []), check=True)
amend = ["--amend", "--no-edit"] if mode == "amend" else ["-m", "slice"]  # amend rewrites the base commit
subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.invalid", "commit", "-q", *amend], check=True)
json.dump({"slice_id": slice_id, "status": "done", "summary": "Changed the loop.", "files_touched": ["src/loop.py"],
           "validations_passed": ["git diff --check"], "validations_failed": [], "proof_level": "doc-only",
           "missing_proof_levels": [], "contract_status_changes": [], "residual_risks": ["None known."],
           "recommended_next_slice": "", "recommended_next_reason": "", "repo_clean_status": "clean",
           "git_mirror_status": "not-checked", "dirty_paths_outside_scope": [], "timestamp": "2026-11-10T00:00:00Z"},
          open(handoff, "w"))
"""


def supervise(monkeypatch, tmp_path: Path, mode: str) -> tuple[int, str]:
    board_dir = tmp_path / "boards" / "replay-board"
    local = lb.init_board(board_dir, actor="maintainer")
    task = local.add("Speed up the hot loop")
    overlay = tmp_path / "overlay.json"
    approval = {
        "domain": "performance",
        "allowed_paths": ["src/loop.py"],
        "required_validations": ["git diff --check"],
        "max_files_changed": 1,
    }
    overlay.write_text(
        json.dumps(
            {"version": 1, "backend": "local", "board": "replay-board", "policy": EXAMPLE_POLICY, "approved": {task: approval}}
        ),
        encoding="utf-8",
    )
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT, encoding="utf-8")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} {mode}"
    argv = ["run_next", "--board", str(board_dir), "--kanban-overlay", str(overlay), "--agent-cmd", command, "--single-slice"]
    monkeypatch.setattr(sys, "argv", argv)
    return run_next._cli_entry(), local.task(task)["status"]


def test_a_clean_committed_slice_still_goes_to_review(consumer, no_hermes, monkeypatch, capsys, tmp_path):
    code, status = supervise(monkeypatch, tmp_path, "clean")

    report = json.loads(capsys.readouterr().out)
    assert (code, status, report["queue_status"]) == (0, "review", "done")
    assert report["supervisor_validation_replays"][0]["success"] is True
    assert report["changed_file_count"] == 1


def test_a_committed_whitespace_error_fails_the_slice(consumer, no_hermes, monkeypatch, capsys, tmp_path):
    code, status = supervise(monkeypatch, tmp_path, "whitespace")

    report = json.loads(capsys.readouterr().out)
    assert (status, report["queue_status"]) == ("blocked", "failed")
    assert report["required_validation_failures"] == ["git diff --check"]


def test_a_committed_file_outside_the_slice_fails_it_even_unreported(consumer, no_hermes, monkeypatch, capsys, tmp_path):
    code, status = supervise(monkeypatch, tmp_path, "out-of-scope")

    report = json.loads(capsys.readouterr().out)
    assert (status, report["queue_status"]) == ("blocked", "failed")
    assert report["dirty_paths_outside_scope"] == ["docs/unrelated.md"]
    assert report["files_touched_outside_scope"] == []  # the agent didn't report it; the supervisor saw it


def test_a_slice_that_rewrites_the_base_commit_is_failed(consumer, no_hermes, monkeypatch, capsys, tmp_path):
    code, status = supervise(monkeypatch, tmp_path, "amend")

    report = json.loads(capsys.readouterr().out)
    assert (code, status, report["queue_status"]) == (2, "blocked", "failed")
    assert "no longer descends" in report["reason"]
