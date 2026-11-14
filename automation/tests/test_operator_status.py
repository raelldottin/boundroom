"""The operator status contract (RA-PROD-101): what Boundroom would do next, read-only.

Each test builds a small consumer repository in a temporary git checkout, with the example
queue, and reads it the way the ``boundroom-status`` skill does.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from automation.supervisor import local_board as lb
from automation.supervisor import operator_status, run_next

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_QUEUE = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
EXAMPLE_OVERLAY = json.loads((REPO_ROOT / "automation/examples/kanban-overlay.json").read_text(encoding="utf-8"))
QUEUE = "automation/queue/slices.json"
SHA = "0123456789abcdef0123456789abcdef01234567"
TOP_LEVEL_KEYS = {
    "schema",
    "read_only",
    "boundroom_version",
    "sync_pin",
    "mode",
    "slice_counts",
    "next_eligible_slice",
    "blocked_slices",
    "waiting_on_dependencies",
    "refusals",
    "telemetry",
    "provenance",
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
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "fixture")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A consumer with the example queue committed and a clean worktree."""
    path = tmp_path / "consumer"
    path.mkdir()
    git(path, "init", "-q")
    write_queue(path, EXAMPLE_QUEUE)
    commit(path)
    monkeypatch.setattr(operator_status, "REPO_ROOT", path)
    return path


def status_json(capsys, *argv: str) -> tuple[int, dict[str, Any]]:
    code = operator_status.main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)


def status_text(capsys, *argv: str) -> tuple[int, str]:
    code = operator_status.main(list(argv))
    return code, capsys.readouterr().out


def snapshot(*roots: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under the roots, .git included, with its modification time and contents."""
    files: dict[str, tuple[int, bytes]] = {}
    for root in roots:
        for path in [root] if root.is_file() else sorted(root.rglob("*")):
            if path.is_file():
                files[str(path)] = (path.stat().st_mtime_ns, path.read_bytes())
    return files


# --- the JSON contract ----------------------------------------------------------------------


def test_the_json_report_is_one_versioned_object(repo, capsys):
    code, status = status_json(capsys)

    assert code == 0
    assert set(status) == TOP_LEVEL_KEYS
    assert status["schema"] == "boundroom.operator-status/1"
    assert status["read_only"] is True
    assert status["mode"] == {"kind": "queue", "queue": QUEUE}
    assert status["slice_counts"] == {"done": 1, "queued": 2}
    assert status["next_eligible_slice"]["slice_id"] == "today-continue-ui-regression-coverage"
    assert set(status["next_eligible_slice"]) == {"slice_id", "title", "priority", "allowed_paths", "required_validations"}
    assert status["waiting_on_dependencies"] == [
        {"slice_id": "today-continue-copy-proofread", "waiting_on": ["today-continue-ui-regression-coverage"]}
    ]
    assert status["refusals"] == []
    assert status["provenance"] == {
        "repo_root": str(repo),
        "git_head": git(repo, "rev-parse", "HEAD"),
        "worktree_clean": True,
        "dirty_paths": [],
        "files_read": [QUEUE],
    }


def test_the_next_slice_is_the_one_run_next_dry_run_selects(repo, capsys, monkeypatch):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["slices"][2]["depends_on"] = []
    queue["slices"][2]["priority"] = 10  # now ahead of the other queued slice
    write_queue(repo, queue)
    commit(repo)

    _, status = status_json(capsys)
    monkeypatch.setattr(run_next, "REPO_ROOT", repo)
    monkeypatch.setattr(sys, "argv", ["run_next", "--dry-run", "--include-blocked"])
    assert run_next._cli_entry() == 0
    dry_run = json.loads(capsys.readouterr().out)

    assert status["next_eligible_slice"]["slice_id"] == dry_run["selected_slice"] == "today-continue-copy-proofread"
    assert status["blocked_slices"] == dry_run["blocked_slices"]


def test_the_version_comes_from_the_plugin_manifest_when_there_is_one(repo, capsys):
    manifest = repo / operator_status.VERSION_FILE
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "boundroom", "version": "9.8.7"}), encoding="utf-8")

    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["boundroom_version"] == {"value": "9.8.7", "source": operator_status.VERSION_FILE}
    assert operator_status.VERSION_FILE in status["provenance"]["files_read"]
    assert "version: 9.8.7 (plugins/boundroom/plugin.json)" in text


def test_a_synced_copy_without_a_manifest_has_no_version(repo, capsys):
    _, status = status_json(capsys)

    assert status["boundroom_version"] == {"value": None, "source": None}


def test_boundroom_itself_reports_its_release_version():
    status = operator_status.collect(REPO_ROOT, queue="automation/examples/example-slices.json")
    manifest = json.loads((REPO_ROOT / operator_status.VERSION_FILE).read_text(encoding="utf-8"))

    assert status["boundroom_version"]["value"] == manifest["version"]
    assert status["sync_pin"] is None


# --- the sync pin ---------------------------------------------------------------------------


def test_no_lock_means_no_sync_pin(repo, capsys):
    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["sync_pin"] is None
    assert "sync pin: none (no automation/boundroom.lock or automation/repo-automation.lock)" in text


def test_a_boundroom_lock_reports_its_pin(repo, capsys):
    lock = {"repository": "raelldottin/boundroom", "remote": "https://example.invalid/boundroom.git", "commit": SHA}
    lock["imported_at"] = "2026-11-12T00:00:00Z"
    (repo / "automation/boundroom.lock").write_text(json.dumps(lock), encoding="utf-8")
    commit(repo)

    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["sync_pin"] == {"path": "automation/boundroom.lock", "legacy": False, **lock}
    assert "automation/boundroom.lock" in status["provenance"]["files_read"]
    assert f"sync pin: automation/boundroom.lock: commit {SHA}" in text


def test_a_legacy_lock_is_read_and_named_legacy(repo, capsys):
    (repo / "automation/repo-automation.lock").write_text(
        f"# pinned by the owner\ncommit={SHA}\nremote = https://example.invalid/boundroom.git\nimported_at: 2026-11-01\n",
        encoding="utf-8",
    )
    commit(repo)

    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["sync_pin"] == {
        "path": "automation/repo-automation.lock",
        "legacy": True,
        "commit": SHA,
        "remote": "https://example.invalid/boundroom.git",
        "repository": None,
        "imported_at": "2026-11-01",
    }
    assert "(legacy name)" in text


def test_the_new_lock_name_wins_over_the_legacy_one(repo, capsys):
    (repo / "automation/boundroom.lock").write_text(json.dumps({"commit": SHA}), encoding="utf-8")
    (repo / "automation/repo-automation.lock").write_text(json.dumps({"commit": "f" * 40}), encoding="utf-8")

    _, status = status_json(capsys)

    assert (status["sync_pin"]["path"], status["sync_pin"]["commit"]) == ("automation/boundroom.lock", SHA)


def test_a_lock_without_a_commit_is_reported_as_a_problem(repo, capsys):
    (repo / "automation/boundroom.lock").write_text("{}", encoding="utf-8")

    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["sync_pin"]["commit"] is None
    assert status["sync_pin"]["error"] == "the lock names no commit"
    assert "problem: the lock names no commit" in text


# --- the queue and the refusals --------------------------------------------------------------


def test_an_empty_queue_has_nothing_eligible(repo, capsys):
    write_queue(repo, {**EXAMPLE_QUEUE, "slices": []})
    commit(repo)

    code, status = status_json(capsys)
    _, text = status_text(capsys)

    assert code == 0
    assert status["next_eligible_slice"] is None
    assert status["slice_counts"] == {}
    assert status["refusals"] == [{"code": "no_eligible_slice", "message": "no eligible queued slice found."}]
    assert "next eligible slice: none" in text
    assert "- no_eligible_slice: no eligible queued slice found." in text


def test_a_blocked_slice_is_reported_with_its_reason_and_unblocker(repo, capsys):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["slices"][1].update(
        status="blocked",
        entry_condition="Needs the UI test target from the platform team.",
        recommended_unblocker="today-nonfocus-add-to-focus",
    )
    write_queue(repo, queue)
    commit(repo)

    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["blocked_slices"] == [
        {
            "slice_id": "today-continue-ui-regression-coverage",
            "status": "blocked",
            "entry_condition": "Needs the UI test target from the platform team.",
            "recommended_unblocker": "today-nonfocus-add-to-focus",
        }
    ]
    assert status["next_eligible_slice"] is None
    assert "- blocked: today-continue-ui-regression-coverage" in text
    assert "missing entry condition: Needs the UI test target from the platform team." in text
    assert "recommended unblocker: today-nonfocus-add-to-focus" in text


def test_an_in_progress_slice_is_a_refusal(repo, capsys):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["slices"][1]["status"] = "in_progress"
    write_queue(repo, queue)
    commit(repo)

    _, status = status_json(capsys)

    assert {"code": "active_slice", "message": "queue already has in_progress slice today-continue-ui-regression-coverage"} in (
        status["refusals"]
    )


def test_a_dirty_path_outside_the_next_slice_is_a_refusal(repo, capsys):
    (repo / "stray.txt").write_text("not in scope\n", encoding="utf-8")

    _, status = status_json(capsys)
    _, text = status_text(capsys)

    assert status["provenance"]["worktree_clean"] is False
    assert status["provenance"]["dirty_paths"] == ["stray.txt"]
    assert status["refusals"] == [
        {"code": "dirty_outside_scope", "message": "repo is dirty outside the next slice scope: stray.txt"}
    ]
    assert "worktree dirty (1 paths)" in text


def test_a_queue_without_an_agent_command_is_a_refusal(repo, capsys):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["policy"]["agent_command_template"] = " "
    write_queue(repo, queue)
    commit(repo)

    _, status = status_json(capsys)

    assert [refusal["code"] for refusal in status["refusals"]] == ["no_agent_command"]


def test_a_missing_queue_is_reported_and_the_exit_code_says_so(repo, capsys):
    (repo / QUEUE).unlink()
    commit(repo)

    code, status = status_json(capsys)
    text_code, text = status_text(capsys)

    assert code == text_code == operator_status.EX_INCOMPLETE
    assert [refusal["code"] for refusal in status["refusals"]] == [
        "queue_unreadable",
    ]
    assert status["provenance"]["files_read"] == []
    assert "- queue_unreadable: queue file not found:" in text
    assert "\n  hint: copy automation/examples/example-slices.json" in text


def test_outside_git_the_provenance_is_unknown(tmp_path, capsys, monkeypatch):
    write_queue(tmp_path, EXAMPLE_QUEUE)
    monkeypatch.setattr(operator_status, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    code, status = status_json(capsys)
    _, text = status_text(capsys)

    assert code == operator_status.EX_INCOMPLETE
    assert status["provenance"]["git_head"] is None and status["provenance"]["worktree_clean"] is None
    assert status["refusals"][0]["code"] == "git_unavailable"
    assert "provenance: HEAD unknown, worktree unknown" in text


def test_queue_text_cannot_drive_the_terminal(repo, capsys):
    queue = copy.deepcopy(EXAMPLE_QUEUE)
    queue["slices"][1]["title"] = "Coverage \x1b[2Jcleared"
    write_queue(repo, queue)
    commit(repo)

    _, text = status_text(capsys)
    code = operator_status.main(["--json"])
    raw = capsys.readouterr().out

    assert code == 0
    assert "\x1b" not in text and "\x1b" not in raw
    assert "Coverage \\x1b[2Jcleared" in text


# --- board mode -----------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path) -> tuple[Path, Path, str]:
    """A local board outside the repository with one approved, ready task."""
    board_dir = tmp_path / "boards" / "local-board"
    local = lb.init_board(board_dir, actor="maintainer")
    task_id = local.add("Board task")
    overlay = tmp_path / "overlay.json"
    approval = EXAMPLE_OVERLAY["approved"]["t_000000a3"]
    document = {"version": 1, "backend": "local", "board": "local-board", "policy": EXAMPLE_OVERLAY["policy"]}
    overlay.write_text(json.dumps({**document, "approved": {task_id: approval}}), encoding="utf-8")
    return board_dir, overlay, task_id


def test_board_mode_reports_the_projection(repo, board, capsys):
    board_dir, overlay, task_id = board

    code, status = status_json(capsys, "--board-dir", str(board_dir), "--overlay", str(overlay))
    _, text = status_text(capsys, "--board-dir", str(board_dir), "--overlay", str(overlay))

    assert code == 0
    assert status["mode"]["kind"] == "board"
    assert (status["mode"]["backend"], status["mode"]["board"]) == ("local", "local-board")
    assert len(status["mode"]["revision"]) == 64
    assert status["next_eligible_slice"]["slice_id"] == f"board.local-board.{task_id}"
    assert status["provenance"]["files_read"] == [str(overlay), str(board_dir / lb.DB_NAME)]
    assert f"mode: local board {board_dir}" in text


def test_a_board_inside_the_repository_is_a_refusal(repo, capsys):
    board_dir = repo / "boards" / "local-board"
    local = lb.init_board(board_dir, actor="maintainer")
    task_id = local.add("Board task")
    overlay = repo / "overlay.json"
    approval = EXAMPLE_OVERLAY["approved"]["t_000000a3"]
    document = {"version": 1, "backend": "local", "board": "local-board", "policy": EXAMPLE_OVERLAY["policy"]}
    overlay.write_text(json.dumps({**document, "approved": {task_id: approval}}), encoding="utf-8")

    _, status = status_json(capsys, "--board-dir", str(board_dir), "--overlay", str(overlay))

    assert status["refusals"][0]["code"] == "board_inside_repository"


def test_an_unreadable_board_is_reported(repo, board, capsys):
    board_dir, overlay, _ = board

    code, status = status_json(capsys, "--board-dir", str(board_dir.parent / "missing"), "--overlay", str(overlay))

    assert code == operator_status.EX_INCOMPLETE
    assert status["refusals"][0]["code"] == "board_unreadable"
    assert status["next_eligible_slice"] is None


def test_board_dir_and_overlay_go_together(repo, capsys):
    with pytest.raises(SystemExit) as raised:
        operator_status.main(["--board-dir", "somewhere"])

    assert raised.value.code == 2
    assert "--board-dir and --overlay go together" in capsys.readouterr().err


# --- read-only ------------------------------------------------------------------------------


def test_the_status_writes_nothing(repo, board, capsys):
    board_dir, overlay, _ = board
    (repo / "automation/boundroom.lock").write_text(json.dumps({"commit": SHA}), encoding="utf-8")
    (repo / "automation/handoffs").mkdir()
    (repo / "automation/handoffs/.keep").write_text("", encoding="utf-8")
    commit(repo)
    before = snapshot(repo, board_dir, overlay)
    assert before

    operator_status.main([])
    operator_status.main(["--json"])
    operator_status.main(["--board-dir", str(board_dir), "--overlay", str(overlay)])
    capsys.readouterr()

    assert snapshot(repo, board_dir, overlay) == before


# --- the telemetry block (SPEC-006 RA-TEL-013) ----------------------------------------------------

ENDPOINT = "https://collector.example.test/tenant-path?token=query-secret"
HEADER_SECRET = "Bearer header-secret-value"
LABEL_VALUE = "payments-label-value"


def test_telemetry_is_reported_off_by_default_with_nothing_read(tmp_path):
    status = operator_status.collect(
        REPO_ROOT, queue="automation/examples/example-slices.json", environ={}, operator_file=tmp_path / "absent.json"
    )
    assert status["telemetry"] == {
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
    assert "telemetry: off (default), offline off" in operator_status.render_text(status)


def test_telemetry_block_shows_kinds_and_keys_never_endpoint_headers_or_values(tmp_path):
    operator = tmp_path / "telemetry.json"
    operator.write_text(json.dumps({"headers": {"authorization": HEADER_SECRET}}), encoding="utf-8")
    environ = {
        "BOUNDROOM_TELEMETRY": "otlp",
        "BOUNDROOM_TELEMETRY_ENDPOINT": ENDPOINT,
        "BOUNDROOM_TELEMETRY_RESOURCE_ATTRIBUTES": f"team.name={LABEL_VALUE}",
    }
    status = operator_status.collect(
        REPO_ROOT, queue="automation/examples/example-slices.json", environ=environ, operator_file=operator
    )
    block = status["telemetry"]
    assert (block["mode"], block["destination_kind"], block["source"]) == ("otlp", "otlp", "env")
    assert (block["resource_attribute_keys"], block["operator_file"], block["offline"]) == (["team.name"], "read", False)

    shown = json.dumps(status) + operator_status.render_text(status)
    for secret in ("collector.example.test", "tenant-path", "query-secret", "header-secret-value", LABEL_VALUE, str(tmp_path)):
        assert secret not in shown


def test_telemetry_block_reports_offline_and_the_refusal():
    environ = {
        "BOUNDROOM_OFFLINE": "1",
        "BOUNDROOM_TELEMETRY": "otlp",
        "BOUNDROOM_TELEMETRY_ENDPOINT": ENDPOINT,
        "BOUNDROOM_TELEMETRY_REQUIRED": "true",
    }
    status = operator_status.collect(
        REPO_ROOT, queue="automation/examples/example-slices.json", environ=environ, operator_file=None
    )
    block = status["telemetry"]
    assert (block["mode"], block["source"], block["offline"]) == ("off", "offline", True)
    assert "refusing to start" in block["refusal"]
    text = operator_status.render_text(status)
    assert "offline on" in text and "would refuse" in text
    assert "collector.example.test" not in json.dumps(status) + text


def test_an_invalid_operator_file_is_reported_as_ignored(tmp_path):
    operator = tmp_path / "telemetry.json"
    operator.write_text("{not json", encoding="utf-8")
    block = operator_status.telemetry_status(None, {}, operator)
    assert block["operator_file"] == "ignored"
    assert block["mode"] == "off"
