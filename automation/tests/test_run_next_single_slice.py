"""``run_next --single-slice`` runs at most one slice (SPEC-002).

Without the flag, the queue below lets the supervisor continue from the first slice into
the second. With it, only the first runs, and any ``--autonomous-limit`` is refused before
anything is claimed. The last test runs the exact command the run-next skill documents.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from automation.supervisor import run_next
from automation.tests import test_run_next_kanban
from automation.tests.test_run_next_kanban import AGENT

consumer = test_run_next_kanban.consumer

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "plugins/repo-automation/skills/repo-automation-run-next/SKILL.md"
FIRST, SECOND = "first-slice", "second-slice"


def slice_record(slice_id: str, priority: int) -> dict:
    return {
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


@pytest.fixture
def queue(consumer, tmp_path):
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT, encoding="utf-8")
    log = tmp_path / "agent-runs.log"
    command = (
        f"echo {{slice_id}} >> {shlex.quote(str(log))} && "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} 0"
    )
    data = {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 5,
            "handoff_timeout_seconds": 60,
            "agent_command_template": command,
            "supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"],
        },
        "slices": [slice_record(FIRST, 10), slice_record(SECOND, 20)],
    }
    path = consumer / "automation/queue/slices.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    git = ["git", "-C", str(consumer), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "queue"], check=True)
    return path, log


def supervise(monkeypatch, *flags: str) -> int:
    monkeypatch.setattr(sys, "argv", ["run_next", *flags])
    return run_next._cli_entry()


def runs(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def statuses(path: Path) -> dict[str, str]:
    return {item["slice_id"]: item["status"] for item in json.loads(path.read_text(encoding="utf-8"))["slices"]}


def test_without_the_flag_the_queue_runs_both_slices(queue, monkeypatch):
    _, log = queue

    assert supervise(monkeypatch) == 0

    assert runs(log) == [FIRST, SECOND]


def test_single_slice_runs_one_and_leaves_the_second_queued(queue, monkeypatch, capsys):
    path, log = queue

    assert supervise(monkeypatch, "--single-slice") == 0

    assert runs(log) == [FIRST]
    assert statuses(path) == {FIRST: "done", SECOND: "queued"}
    assert json.loads(capsys.readouterr().out)["autonomous_run_limit"] == 1


@pytest.mark.parametrize(
    "flags",
    [
        ("--single-slice", "--autonomous-limit", "2"),
        ("--single-slice", "--autonomous-limit=2"),
        ("--autonomous-limit", "2", "--single-slice"),
        ("--single-slice", "--autonomous-limit", "2", "--autonomous-limit", "1"),
        ("--single-slice", "--autonomous-limit", "1"),
    ],
)
def test_single_slice_refuses_any_autonomous_limit_before_claiming(queue, monkeypatch, flags):
    path, log = queue
    before = path.read_bytes()

    assert supervise(monkeypatch, *flags) == run_next.EX_USAGE

    assert runs(log) == []
    assert path.read_bytes() == before


def test_the_skill_documents_exactly_one_run_next_command(queue, monkeypatch):
    path, log = queue
    commands = re.findall(r"```bash\n(.*?)\n```", SKILL.read_text(encoding="utf-8"), re.DOTALL)
    assert len(commands) == 1, "the skill must document one supported invocation"
    argv = shlex.split(commands[0])
    assert argv[:2] == ["python3", "automation/supervisor/run_next.py"]

    assert supervise(monkeypatch, *argv[2:]) == 0

    assert runs(log) == [FIRST]
    assert statuses(path)[SECOND] == "queued"
