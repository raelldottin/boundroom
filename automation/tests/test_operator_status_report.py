"""The operator status report as whole values: the JSON document, its readers and the summary.

``test_operator_status.py`` drives the command over scenarios. These tests pin each output in
full: ``collect`` as one dictionary, ``render_text`` and ``render_telemetry`` as whole strings
from hand-built documents, and the readers on the lock and manifest edge cases. The clock plays
no part; the environment and the operator's telemetry file are always passed in.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from automation.supervisor import operator_status

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_QUEUE = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
QUEUE = "automation/queue/slices.json"
SHA = "0123456789abcdef0123456789abcdef01234567"
TELEMETRY_OFF = {
    "mode": "off",
    "destination_kind": "off",
    "source": "default",
    "capture": "metadata",
    "runner": "off",
    "required": False,
    "environment": "local",
    "offline": False,
    "resource_attribute_keys": [],
    "operator_file": "absent",
    "refusal": None,
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def write_queue(repo: Path, queue: dict[str, Any]) -> None:
    path = repo / QUEUE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")


def commit(repo: Path) -> None:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "fixture",
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "consumer"
    path.mkdir()
    git(path, "init", "-q")
    write_queue(path, EXAMPLE_QUEUE)
    commit(path)
    return path


def collect(repo: Path, **kwargs: Any) -> dict[str, Any]:
    return operator_status.collect(repo, environ={}, operator_file=repo.parent / "absent.json", **kwargs)


# --- collect -------------------------------------------------------------------------------------


def test_the_report_on_the_example_queue(repo):
    selected = EXAMPLE_QUEUE["slices"][1]

    assert collect(repo) == {
        "schema": "boundroom.operator-status/1",
        "read_only": True,
        "boundroom_version": {"value": None, "source": None},
        "sync_pin": None,
        "mode": {"kind": "queue", "queue": QUEUE},
        "slice_counts": {"done": 1, "queued": 2},
        "next_eligible_slice": {
            "slice_id": "today-continue-ui-regression-coverage",
            "title": selected["title"],
            "priority": selected["priority"],
            "allowed_paths": selected["allowed_paths"],
            "required_validations": selected["required_validations"],
        },
        "blocked_slices": [],
        "waiting_on_dependencies": [
            {"slice_id": "today-continue-copy-proofread", "waiting_on": ["today-continue-ui-regression-coverage"]}
        ],
        "refusals": [],
        "telemetry": TELEMETRY_OFF,
        "provenance": {
            "repo_root": str(repo),
            "git_head": git(repo, "rev-parse", "HEAD"),
            "worktree_clean": True,
            "dirty_paths": [],
            "files_read": [QUEUE],
        },
    }


def test_a_slice_waits_only_on_dependencies_that_are_not_done(repo):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["slices"][2]["depends_on"] = ["today-nonfocus-add-to-focus", "today-continue-ui-regression-coverage"]
    write_queue(repo, queue)
    commit(repo)

    assert collect(repo)["waiting_on_dependencies"] == [
        {"slice_id": "today-continue-copy-proofread", "waiting_on": ["today-continue-ui-regression-coverage"]}
    ]


def test_the_slice_counts_are_listed_by_status_name(repo):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["slices"].reverse()
    write_queue(repo, queue)
    commit(repo)

    assert list(collect(repo)["slice_counts"].items()) == [("done", 1), ("queued", 2)]


def test_a_board_dir_without_an_overlay_reads_the_queue(repo):
    status = collect(repo, board_dir=repo.parent / "board", overlay=None)

    assert (status["mode"], status["refusals"]) == ({"kind": "queue", "queue": QUEUE}, [])


def test_a_file_read_twice_is_listed_once(repo):
    (repo / "automation/boundroom.lock").write_text(json.dumps(EXAMPLE_QUEUE), encoding="utf-8")

    status = collect(repo, queue="automation/boundroom.lock")

    assert status["provenance"]["files_read"] == ["automation/boundroom.lock"]


def test_the_repository_can_turn_telemetry_off_over_the_environment(repo):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["policy"]["telemetry"] = {"mode": "off"}
    write_queue(repo, queue)
    commit(repo)
    environ = {"BOUNDROOM_TELEMETRY": "otlp", "BOUNDROOM_TELEMETRY_ENDPOINT": "https://collector.example.test"}

    status = operator_status.collect(repo, environ=environ, operator_file=None)

    assert (status["telemetry"]["mode"], status["telemetry"]["source"]) == ("off", "repository")


# --- the readers, seen through the report --------------------------------------------------------


def manifest(repo: Path, text: str) -> None:
    path = repo / operator_status.VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"version": "9.8.7"}', {"value": "9.8.7", "source": "plugins/boundroom/plugin.json"}),
        ('{"version": 3}', {"value": None, "source": "plugins/boundroom/plugin.json"}),
        ("{not json", {"value": None, "source": "plugins/boundroom/plugin.json", "error": "no readable version"}),
        ('{"name": "boundroom"}', {"value": None, "source": "plugins/boundroom/plugin.json", "error": "no readable version"}),
        ('["9.8.7"]', {"value": None, "source": "plugins/boundroom/plugin.json", "error": "no readable version"}),
    ],
)
def test_the_version_is_read_from_the_plugin_manifest(repo, text, expected):
    manifest(repo, text)
    commit(repo)

    status = collect(repo)

    assert (status["boundroom_version"], status["provenance"]["files_read"]) == (
        expected,
        ["plugins/boundroom/plugin.json", QUEUE],
    )


def lock(repo: Path, name: str, data: bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    commit(repo)


def test_a_legacy_lock_alone_is_read_and_named_legacy(repo):
    lock(repo, "automation/repo-automation.lock", json.dumps({"commit": SHA, "remote": "", "repository": 7}).encode())

    status = collect(repo)

    assert (status["sync_pin"], status["provenance"]["files_read"]) == (
        {
            "path": "automation/repo-automation.lock",
            "legacy": True,
            "commit": SHA,
            "remote": None,
            "repository": None,
            "imported_at": None,
        },
        ["automation/repo-automation.lock", QUEUE],
    )


def test_a_lock_that_is_not_text_is_reported_unreadable(repo):
    lock(repo, "automation/boundroom.lock", b"\xff\xfe")

    assert collect(repo)["sync_pin"] == {
        "path": "automation/boundroom.lock",
        "legacy": False,
        "error": "unreadable lock: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        "commit": None,
        "remote": None,
        "repository": None,
        "imported_at": None,
    }


def test_a_lock_with_an_empty_commit_names_no_commit(repo):
    lock(repo, "automation/boundroom.lock", json.dumps({"commit": "", "remote": "r"}).encode())

    assert collect(repo)["sync_pin"] == {
        "path": "automation/boundroom.lock",
        "legacy": False,
        "commit": None,
        "remote": "r",
        "repository": None,
        "imported_at": None,
        "error": "the lock names no commit",
    }


def test_files_outside_the_repository_are_listed_in_full(repo, tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{}", encoding="utf-8")

    status = collect(repo, board_dir=tmp_path / "board", overlay=overlay)

    assert status["provenance"]["files_read"] == [str(overlay)]


def test_a_file_that_does_not_exist_is_not_listed_as_read(repo):
    status = collect(repo, queue="nope.json")

    assert (status["mode"], status["provenance"]["files_read"]) == ({"kind": "queue", "queue": "nope.json"}, [])


# --- the summary ---------------------------------------------------------------------------------


def full_status() -> dict[str, Any]:
    return {
        "boundroom_version": {"value": "1.2.3", "source": "plugins/boundroom/plugin.json"},
        "sync_pin": {
            "path": "automation/repo-automation.lock",
            "legacy": True,
            "commit": SHA,
            "remote": None,
            "repository": "owner/repo",
            "imported_at": None,
            "error": "bad\x1b",
        },
        "mode": {"kind": "board", "board_dir": "/boards/b", "overlay": "/o.json", "backend": "local"},
        "slice_counts": {"done": 1, "queued": 2},
        "next_eligible_slice": {"slice_id": "S1", "title": "First"},
        "blocked_slices": [
            {"slice_id": "B1", "status": "blocked", "entry_condition": "needs X", "recommended_unblocker": ""},
            {"slice_id": "B2", "status": "deferred", "entry_condition": "later", "recommended_unblocker": "S1"},
        ],
        "waiting_on_dependencies": [{"slice_id": "W1", "waiting_on": ["S1", "ghost"]}],
        "refusals": [
            {"code": "queue_unreadable", "message": "queue file not found\n  hint: copy it"},
            {"code": "active_slice", "message": "busy"},
        ],
        "telemetry": {
            "mode": "otlp",
            "destination_kind": "otlp",
            "source": "env",
            "capture": "metadata",
            "runner": "metadata",
            "offline": False,
            "resource_attribute_keys": ["service.team", "team.name"],
            "refusal": "no endpoint",
        },
        "provenance": {"git_head": SHA, "worktree_clean": False, "dirty_paths": ["a", "b"], "files_read": ["q.json", "lock"]},
    }


def test_the_summary_of_a_full_report():
    assert operator_status.render_text(full_status()) == (
        "Boundroom operator status (read-only)\n"
        "version: 1.2.3 (plugins/boundroom/plugin.json)\n"
        f"sync pin: automation/repo-automation.lock (legacy name): commit {SHA}, repository owner/repo\n"
        "  problem: bad\\x1b\n"
        "mode: local board /boards/b, overlay /o.json\n"
        "slices: 1 done, 2 queued\n"
        "next eligible slice: S1: First\n"
        "blocked/deferred slices:\n"
        "- blocked: B1\n"
        "  missing entry condition: needs X\n"
        "  recommended unblocker: <none recorded>\n"
        "- deferred: B2\n"
        "  missing entry condition: later\n"
        "  recommended unblocker: S1\n"
        "waiting: W1 on S1, ghost\n"
        "run_next would refuse:\n"
        "- queue_unreadable: queue file not found\n"
        "    hint: copy it\n"
        "- active_slice: busy\n"
        "telemetry: otlp to a otlp destination, capture metadata, runner metadata, offline off, "
        "resource keys service.team, team.name; would refuse: no endpoint\n"
        f"provenance: HEAD {SHA}, worktree dirty (2 paths)\n"
        "read: q.json, lock\n"
    )


def test_the_summary_of_an_empty_report():
    status = {
        "boundroom_version": {"value": None, "source": None},
        "sync_pin": None,
        "mode": {"kind": "queue", "queue": QUEUE},
        "slice_counts": {},
        "next_eligible_slice": None,
        "blocked_slices": [],
        "waiting_on_dependencies": [],
        "refusals": [],
        "telemetry": {**TELEMETRY_OFF, "offline": True},
        "provenance": {"git_head": None, "worktree_clean": None, "dirty_paths": None, "files_read": []},
    }

    assert operator_status.render_text(status) == (
        "Boundroom operator status (read-only)\n"
        "version: unknown (no plugin manifest)\n"
        "sync pin: none (no automation/boundroom.lock or automation/repo-automation.lock)\n"
        f"mode: JSON queue {QUEUE}\n"
        "next eligible slice: none\n"
        "run_next would refuse: nothing\n"
        "telemetry: off (default), offline on\n"
        "provenance: HEAD unknown, worktree unknown\n"
        "read: nothing\n"
    )


def test_the_summary_of_a_lock_with_no_fields_and_a_clean_worktree():
    status = full_status()
    status["sync_pin"] = {
        "path": "automation/boundroom.lock",
        "legacy": False,
        "commit": None,
        "remote": None,
        "repository": None,
        "imported_at": None,
    }
    status["mode"] = {"kind": "board", "board_dir": "/b", "overlay": "/o.json"}
    status["provenance"] = {"git_head": SHA, "worktree_clean": True, "dirty_paths": [], "files_read": ["q.json"]}

    lines = operator_status.render_text(status).splitlines()

    assert lines[2:4] == ["sync pin: automation/boundroom.lock: no fields", "mode: unknown board /b, overlay /o.json"]
    assert lines[-2:] == [f"provenance: HEAD {SHA}, worktree clean", "read: q.json"]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ({**TELEMETRY_OFF, "source": "offline", "offline": True}, "telemetry: off (offline), offline on"),
        (
            {**TELEMETRY_OFF, "mode": "file", "destination_kind": "file", "source": "operator", "runner": "off"},
            "telemetry: file to a file destination, capture metadata, runner off, offline off",
        ),
        (
            {**TELEMETRY_OFF, "resource_attribute_keys": ["k\x1b"], "refusal": "required but off"},
            "telemetry: off (default), offline off, resource keys k\\x1b; would refuse: required but off",
        ),
    ],
)
def test_the_telemetry_line(block, expected):
    assert operator_status.render_telemetry(block) == expected


# --- the command ---------------------------------------------------------------------------------


def test_the_module_runs_as_a_command_and_caches_no_bytecode(tmp_path):
    cache = tmp_path / "pycache"
    environ = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONPYCACHEPREFIX": str(cache),
    }

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "automation/supervisor/operator_status.py"), "--json", "--queue", "no-such-queue.json"],
        capture_output=True,
        text=True,
        env=environ,
        cwd=tmp_path,
    )

    assert result.returncode == 1
    status = json.loads(result.stdout)
    assert [refusal["code"] for refusal in status["refusals"]] == ["queue_unreadable"]
    assert status["provenance"]["repo_root"] == str(REPO_ROOT)
    assert status["telemetry"]["operator_file"] == "absent"
    # The interpreter caches what it imports before the module's first line runs; nothing after.
    assert [path for path in cache.rglob("*.pyc") if "automation" in path.parts] == []
