"""``run_next --single-slice`` runs at most one slice (SPEC-002).

Without the flag, the queue below lets the supervisor continue from the first slice into
the second. With it, only the first runs, and any ``--autonomous-limit`` is refused before
anything is claimed. The last test runs the exact command the run-next skill documents.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from automation.supervisor import run_next
from automation.tests import test_run_next_kanban
from automation.tests.test_run_next_kanban import AGENT

consumer = test_run_next_kanban.consumer

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "plugins/boundroom/skills/boundroom-run-next/SKILL.md"
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


# Shared by the other run_next integration tests: the real file exporter as the record of reason codes.


@pytest.fixture
def traced(tmp_path, monkeypatch) -> Path:
    """A directory for the file exporter's spans, with no telemetry setting inherited from the caller."""
    for name in list(os.environ):
        if name.startswith(("BOUNDROOM_TELEMETRY", "OTEL_")):
            monkeypatch.delenv(name)
    return tmp_path / "spans"


def trace_flags(file_dir: Path) -> tuple[str, ...]:
    return ("--telemetry", "file", "--telemetry-file-dir", str(file_dir))


def spans(file_dir: Path) -> list[dict]:
    """Every span the run exported, in the order they started."""
    lines = [line for path in file_dir.glob("*.spans.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
    return sorted((json.loads(line) for line in lines), key=lambda record: record["start_time_unix_nano"])


def decision_codes(file_dir: Path) -> list[Optional[str]]:
    """The reason code on each ``boundroom.decision`` span, None for a verdict that isn't a failure."""
    return [
        record["status_description"] if record["status"] == "ERROR" else None
        for record in spans(file_dir)
        if record["name"] == "boundroom.decision"
    ]


git_fault = test_run_next_kanban.git_fault


def arm(trigger: Path, command: str) -> str:
    """An agent command that arms ``trigger`` and then runs ``command``."""
    return f"touch {shlex.quote(str(trigger))} && {command}"


def agent_command(log: Path, exit_code: int = 0) -> str:
    """The queue fixture's agent, as --agent-cmd would pass it, without the run log."""
    agent = log.parent / "agent.py"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} {exit_code}"


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


def test_a_failing_agent_under_single_slice_fails_one_and_leaves_the_second_queued(queue, monkeypatch, capsys, tmp_path):
    """The failure path of the one-slice smoke (RA-PROD-104): one run, one failed slice, nothing more."""
    path, log = queue
    agent = tmp_path / "agent.py"
    failing = (
        f"echo {{slice_id}} >> {shlex.quote(str(log))} && "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} 3"
    )

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", failing) != 0

    assert runs(log) == [FIRST]
    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
    report = json.loads(capsys.readouterr().out)
    assert (report["slice_id"], report["queue_status"], report["autonomous_run_limit"]) == (FIRST, "failed", 1)


def git_head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_the_report_names_the_commits_and_can_go_to_a_file(queue, monkeypatch, capsys, tmp_path):
    """RA-PROD-105: a reviewer gets both commit identities, and a clean JSON copy of the report."""
    path, _ = queue
    consumer = path.parents[2]
    base = git_head(consumer)
    report_file = tmp_path / "reports.jsonl"

    assert supervise(monkeypatch, "--single-slice", "--report-file", str(report_file)) == 0

    printed = json.loads(capsys.readouterr().out)
    (line,) = report_file.read_text(encoding="utf-8").splitlines()
    written = json.loads(line)
    assert written == printed
    assert line == json.dumps(written, sort_keys=True)  # one line, keys sorted, so equal reports diff clean
    assert (written["base_sha"], written["head_sha"]) == (base, git_head(consumer))


def test_a_failure_report_names_the_commits_too(queue, monkeypatch, capsys, tmp_path):
    path, log = queue
    consumer = path.parents[2]
    base = git_head(consumer)
    failing = f"{shlex.quote(sys.executable)} -c 'raise SystemExit(3)'"

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", failing) != 0

    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["base_sha"], report["head_sha"]) == ("failed", base, base)


def test_a_report_file_inside_the_repository_is_refused_before_claiming(queue, monkeypatch, capsys):
    path, log = queue
    before = path.read_bytes()

    assert supervise(monkeypatch, "--single-slice", "--report-file", str(path.parent / "report.jsonl")) == 2

    assert "outside the repository" in capsys.readouterr().err
    assert runs(log) == [] and path.read_bytes() == before


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

    assert supervise(monkeypatch, *flags) == 64  # EX_USAGE, as run_agent.sh exits for a bad runner

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


# The longest happy path: two slices that each commit a change, with git diff --check replayed.

COMMITTING_AGENT = """
import json, pathlib, subprocess, sys
handoff, slice_id = sys.argv[1], sys.argv[2]
path = pathlib.Path("docs") / (slice_id + ".md")
path.parent.mkdir(exist_ok=True)
path.write_text(slice_id + "\\n")
subprocess.run(["git", "add", str(path)], check=True)
subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.invalid", "commit", "-q", "-m", slice_id], check=True)
json.dump({"slice_id": slice_id, "status": "done", "summary": "Wrote the page.", "files_touched": [str(path)],
           "validations_passed": ["git diff --check"], "validations_failed": [], "proof_level": "doc-only",
           "missing_proof_levels": [], "contract_status_changes": [], "residual_risks": ["None known."],
           "recommended_next_slice": "", "recommended_next_reason": "", "repo_clean_status": "clean",
           "git_mirror_status": "not-checked", "dirty_paths_outside_scope": [], "timestamp": "2026-11-10T00:00:00Z"},
          open(handoff, "w"))
"""

ONE_SLICE = [
    "boundroom.select",
    "boundroom.context",
    "boundroom.claim",
    "boundroom.agent_session",
    "boundroom.handoff.evaluate",
    "boundroom.validation.replay",
    "boundroom.decision",
    "boundroom.board.record",
]


def printed_reports(printed: str) -> list[dict]:
    """Each JSON report on stdout, in order."""
    decoder, reports = json.JSONDecoder(), []
    while printed.strip():
        report, end = decoder.raw_decode(printed.lstrip())
        reports.append(report)
        printed = printed.lstrip()[end:]
    return reports


def test_two_committed_slices_run_back_to_back_with_their_validations_replayed(queue, traced, monkeypatch, capsys, tmp_path):
    path, _ = queue
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["slices"]:
        item["required_validations"] = ["git diff --check"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    consumer = path.parents[2]
    git = ["git", "-C", str(consumer), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "commit", "-qam", "validations"], check=True)
    agent = tmp_path / "committing_agent.py"
    agent.write_text(COMMITTING_AGENT, encoding="utf-8")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}}"
    base = git_head(consumer)

    assert supervise(monkeypatch, "--agent-cmd", command, *trace_flags(traced)) == 0

    reports = printed_reports(capsys.readouterr().out)
    assert [(r["slice_id"], r["decision"], r["queue_status"]) for r in reports] == [
        (FIRST, "continue", "done"),
        (SECOND, "stop", "done"),
    ]
    assert [r["completed_autonomous_runs"] for r in reports] == [1, 2]
    assert reports[0]["base_sha"] == base and reports[1]["base_sha"] == reports[0]["head_sha"] != base
    for report in reports:
        assert report["supervisor_validation_replays"] == [
            {"command": "git diff --check", "success": True, "exit_code": 0, "reason": "Supervisor replay passed."}
        ]
        assert report["changed_file_count"] == 1
    assert [r["reason"] for r in reports] == [
        "Continuation allowed into the highest-priority eligible queued slice.",
        "No eligible queued next slice remains after the current slice completed.",
    ]
    assert statuses(path) == {FIRST: "done", SECOND: "done"}
    (root,) = [record for record in spans(traced) if record["name"] == "boundroom.run"]
    children = [record["name"] for record in spans(traced) if record["parent_span_id"] == root["span_id"]]
    assert children == ONE_SLICE * 2
    assert decision_codes(traced) == [None, None]


def test_the_repository_is_the_one_passed_in_not_the_module_default(queue, monkeypatch, capsys, tmp_path):
    path, _ = queue
    monkeypatch.setattr(run_next, "REPO_ROOT", tmp_path / "not-a-repository")

    assert run_next.main(["--single-slice", "--dry-run"], repo_root=path.parents[2]) == 0

    assert json.loads(capsys.readouterr().out)["selected_slice"] == FIRST


def test_the_environment_is_the_one_passed_in(queue, monkeypatch, capsys):
    """A nested-run marker in the injected environment refuses the run; the process's own has none."""
    path, log = queue
    for name in ("BOUNDROOM_SUPERVISOR_SLICE_ID", "REPO_AUTOMATION_SUPERVISOR_SLICE_ID", "OWLORY_SUPERVISOR_SLICE_ID"):
        monkeypatch.delenv(name, raising=False)
    environ = {**os.environ, "BOUNDROOM_SUPERVISOR_SLICE_ID": "other-slice"}

    assert run_next.main(["--single-slice"], repo_root=path.parents[2], environ=environ) == 78  # EX_CONFIG

    assert "BOUNDROOM_SUPERVISOR_SLICE_ID" in capsys.readouterr().err
    assert runs(log) == [] and statuses(path) == {FIRST: "queued", SECOND: "queued"}


# What stops the run before anything is claimed, through the real command line.


def commit_all(repo: Path, message: str) -> None:
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True)


def test_with_no_agent_command_nothing_is_claimed(queue, monkeypatch, capsys):
    path, log = queue
    data = json.loads(path.read_text(encoding="utf-8"))
    data["policy"]["agent_command_template"] = ""
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    commit_all(path.parents[2], "no agent command")
    before = path.read_bytes()

    assert supervise(monkeypatch, "--single-slice") == 2

    captured = capsys.readouterr()
    assert captured.err == (
        "stop: no agent command template configured. Pass --agent-cmd or set policy.agent_command_template.\n"
    )
    assert captured.out == ""
    assert runs(log) == [] and path.read_bytes() == before


def test_a_worktree_dirty_outside_the_slice_scope_stops_before_the_claim(queue, monkeypatch, capsys):
    path, log = queue
    (path.parents[2] / "stray.txt").write_text("left behind\n", encoding="utf-8")
    before = path.read_bytes()

    assert supervise(monkeypatch, "--single-slice") == 2

    captured = capsys.readouterr()
    assert captured.err == "stop: repo is dirty outside the next slice scope:\n- stray.txt\n"
    assert captured.out == ""
    assert runs(log) == [] and path.read_bytes() == before


def test_a_dry_run_needs_no_run_lock(queue, monkeypatch, capsys, tmp_path):
    """A dry run claims nothing, so it goes ahead while another run holds the lock; a real run doesn't."""
    path, log = queue
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    lock = tmp_path / "state/boundroom/run.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert supervise(monkeypatch, "--dry-run") == 0
        assert json.loads(capsys.readouterr().out)["selected_slice"] == FIRST

        assert supervise(monkeypatch, "--single-slice") == 78
        assert f"another supervised run holds {lock}" in capsys.readouterr().err

    assert runs(log) == [] and statuses(path) == {FIRST: "queued", SECOND: "queued"}


def test_required_telemetry_that_cannot_start_refuses_the_run(queue, traced, monkeypatch, capsys, tmp_path):
    path, log = queue
    not_a_directory = tmp_path / "spans"
    not_a_directory.write_text("", encoding="utf-8")
    before = path.read_bytes()

    assert supervise(monkeypatch, "--single-slice", *trace_flags(not_a_directory), "--telemetry-required") == 78

    assert f"stop: telemetry: file_dir {not_a_directory} isn't writable" in capsys.readouterr().err
    assert runs(log) == [] and path.read_bytes() == before


# The loop and the verdict.


def test_an_autonomous_limit_of_two_runs_two_of_three_eligible_slices(queue, monkeypatch, capsys):
    path, log = queue
    data = json.loads(path.read_text(encoding="utf-8"))
    data["slices"].append(slice_record("third-slice", 30))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    commit_all(path.parents[2], "third slice")

    assert supervise(monkeypatch, "--autonomous-limit", "2") == 0

    assert runs(log) == [FIRST, SECOND]
    assert statuses(path) == {FIRST: "done", SECOND: "done", "third-slice": "queued"}
    reports = printed_reports(capsys.readouterr().out)
    assert [(r["completed_autonomous_runs"], r["autonomous_run_limit"]) for r in reports] == [(1, 2), (2, 2)]


HANDOFF_COMMITTING_AGENT = """
import json, subprocess, sys
handoff, slice_id = sys.argv[1], sys.argv[2]
json.dump({"slice_id": slice_id, "status": "done", "summary": "Nothing to change.", "files_touched": [],
           "validations_passed": [], "validations_failed": [], "proof_level": "doc-only", "missing_proof_levels": [],
           "contract_status_changes": [], "residual_risks": ["None known."], "recommended_next_slice": "",
           "recommended_next_reason": "", "repo_clean_status": "clean", "git_mirror_status": "not-checked",
           "dirty_paths_outside_scope": [], "timestamp": "2026-11-26T00:00:00Z"}, open(handoff, "w"))
subprocess.run(["git", "add", handoff], check=True)
subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@example.invalid", "commit", "-q", "-m", "handoff"], check=True)
"""


def test_a_handoff_committed_in_a_commit_of_its_own_is_the_slices_own_change(queue, monkeypatch, capsys, tmp_path):
    """#284: the handoff, here in a directory neither the slice nor the supervisor owns, is never out of scope."""
    path, _ = queue
    consumer = path.parents[2]
    agent = tmp_path / "handoff_committing_agent.py"
    agent.write_text(HANDOFF_COMMITTING_AGENT, encoding="utf-8")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}}"
    base = git_head(consumer)

    assert supervise(monkeypatch, "--single-slice", "--handoff-dir", "handoffs", "--agent-cmd", command) == 0

    report = json.loads(capsys.readouterr().out)
    assert (report["queue_status"], report["dirty_paths_outside_scope"]) == ("done", [])
    assert report["head_sha"] != base  # the handoff commit is there to be judged
    assert statuses(path)[FIRST] == "done"


def test_a_failure_that_cannot_be_reported_escapes_once(queue, traced, monkeypatch, capsys, tmp_path):
    """When reporting the failure itself breaks, the error escapes and the slice is not recorded twice."""
    path, log = queue
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    flags = ("--single-slice", "--agent-cmd", agent_command(log, exit_code=3), "--report-file", str(report_dir))

    with pytest.raises(IsADirectoryError):
        supervise(monkeypatch, *flags, *trace_flags(traced))

    assert [r["reason"] for r in printed_reports(capsys.readouterr().out)] == [
        f"Agent command exited with code 3 for slice {FIRST}."
    ]
    assert decision_codes(traced) == ["agent_exit_nonzero"]
    assert statuses(path) == {FIRST: "failed", SECOND: "queued"}
