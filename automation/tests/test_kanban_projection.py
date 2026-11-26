"""Contract tests for the read-only Kanban projection (RA-CP-002).

The projection core (``build_snapshot``, ``project``, ``shadow_diff``) is pure, so most tests build
their board in memory with ``kp.build_snapshot`` and check its output. ``TASKS``, ``LINKS`` and
``CREATED`` mirror the synthetic ``automation/examples/kanban-board.sql``, and one test checks that
reading that file gives the same snapshot. Only the tests of ``read_board`` and the command line read
a real ``kanban.db``, built from that file under ``tmp_path``, so no test touches ``~/.hermes``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from automation.supervisor import kanban_projection as kp
from automation.supervisor import local_board as lb
from automation.supervisor import policy

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_SQL = REPO_ROOT / "automation/examples/kanban-board.sql"
OVERLAY = REPO_ROOT / "automation/examples/kanban-overlay.json"
BOARD = "example-board"

# The example board, as read_board reads it: tasks by id, links in order, ``created`` payloads by event id.
TASKS: tuple[dict[str, Any], ...] = (
    {"id": "t_000000a1", "title": "Add the parser", "status": "done", "priority": 0, "created_at": 100},
    {"id": "t_000000a2", "title": "Wire the parser into CLI", "status": "ready", "priority": 0, "created_at": 200},
    {"id": "t_000000a3", "title": "Speed up the hot loop", "status": "ready", "priority": 1, "created_at": 300},
    {"id": "t_000000a4", "title": "Document the CLI", "status": "todo", "priority": 0, "created_at": 400},
    {"id": "t_000000a5", "title": "Retire the old flag", "status": "archived", "priority": 0, "created_at": 500},
    {"id": "t_000000a6", "title": "Unapproved spike", "status": "ready", "priority": 5, "created_at": 600},
    {"id": "t_000000a7", "title": "Release notes", "status": "ready", "priority": 0, "created_at": 700},
    {"id": "t_000000a8", "title": "Wait for the vendor fix", "status": "scheduled", "priority": 0, "created_at": 800},
)
LINKS: tuple[tuple[str, str], ...] = (
    ("t_000000a1", "t_000000a2"),
    ("t_000000a2", "t_000000a4"),
    ("t_000000a5", "t_000000a2"),
    ("t_000000a6", "t_000000a7"),
)
CREATED: tuple[tuple[str, Optional[str]], ...] = (
    ("t_000000a1", '{"parents": [], "creator_task_id": null}'),
    ("t_000000a2", '{"parents": ["t_000000a1"], "creator_task_id": "t_000000a1"}'),
    ("t_000000a3", '{"parents": [], "creator_task_id": "t_000000a1"}'),
    ("t_000000a4", '{"parents": ["t_000000a2"]}'),
    ("t_000000a5", "{}"),
    ("t_000000a6", "{}"),
    ("t_000000a7", '{"parents": ["t_000000a6"]}'),
    ("t_000000a8", "not json"),
)
WATERMARK = 9  # eight ``created`` events and one ``completed``


def sid(task_id: str) -> str:
    return kp.slice_id_for(BOARD, task_id)


def snapshot(
    tasks: tuple[dict[str, Any], ...] = TASKS,
    links: tuple[tuple[str, str], ...] = LINKS,
    created: tuple[tuple[str, Optional[str]], ...] = CREATED,
) -> kp.BoardSnapshot:
    return kp.build_snapshot(BOARD, tasks, links, created, WATERMARK)


def edited(*task_ids: str, **fields: object) -> tuple[dict[str, Any], ...]:
    """The example board's tasks with ``fields`` changed on ``task_ids``."""
    return tuple({**task, **fields} if task["id"] in task_ids else task for task in TASKS)


def with_link(parent_id: str, child_id: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((*LINKS, (parent_id, child_id))))


def task(task_id: str, status: str = "ready", priority: int = 0, created_at: int = 100) -> dict[str, Any]:
    return {"id": task_id, "title": f"Task {task_id}", "status": status, "priority": priority, "created_at": created_at}


def board_of(*tasks: dict[str, Any], links: tuple[tuple[str, str], ...] = ()) -> kp.BoardSnapshot:
    """A small board of its own, for behaviours the example board doesn't show."""
    return kp.build_snapshot(BOARD, tuple(sorted(tasks, key=lambda t: t["id"])), tuple(sorted(links)), (), 0)


def approval(**changes: object) -> dict[str, Any]:
    return {"domain": "cli", "allowed_paths": ["src/app.py"], "required_validations": [], "max_files_changed": 1, **changes}


def approving(*task_ids: str) -> kp.KanbanOverlay:
    """The example overlay's policy, approving ``task_ids`` in that order with a plain approval each."""
    return overlay_with(approved={task_id: approval() for task_id in task_ids})


def overlay_with(**changes: object) -> kp.KanbanOverlay:
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document.update(changes)
    return kp.KanbanOverlay.model_validate(document)


def by_id(projection: kp.Projection) -> dict[str, dict]:
    return {record["slice_id"]: record for record in projection.queue["slices"]}


def make_board(root: Path, extra_sql: str = "", board: str = BOARD) -> Path:
    board_dir = root / board
    board_dir.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(board_dir / "kanban.db")) as conn, conn:
        conn.executescript(BOARD_SQL.read_text(encoding="utf-8") + extra_sql)
    return board_dir


def execute(board_dir: Path, sql: str) -> None:
    with contextlib.closing(sqlite3.connect(board_dir / "kanban.db")) as conn, conn:
        conn.executescript(sql)


# Slice ids.


def test_slice_id_traces_back_to_board_and_task():
    assert kp.parse_slice_id(sid("t_000000a2")) == (BOARD, "t_000000a2")
    assert kp.parse_slice_id(kp.slice_id_for(BOARD, "t_000000a2", "board")) == (BOARD, "t_000000a2")
    assert kp.parse_slice_id("today-continue-ui-regression-coverage") is None
    assert kp.parse_slice_id("kanban.Bad Board.t_000000a2") is None
    assert kp.parse_slice_id("kanban.example-board.t_xyz") is None


@pytest.mark.parametrize(
    "slice_id",
    ["other.example-board.t_000000a2", "kanban.example-board.t_000000a2.x", "kanban.t_000000a2"],
)
def test_a_slice_id_with_an_unknown_prefix_or_extra_parts_is_not_a_board_slice(slice_id):
    assert kp.parse_slice_id(slice_id) is None


@pytest.mark.parametrize(
    ("slice_id", "expected"),
    [
        ("kanban.example-board.t_000000A2", None),  # Hermes writes lower-case hex
        ("kanban.example-board.t_000000a20", None),  # nine hex digits
        ("kanban.example-board.t_000000a", None),  # seven
        (f"kanban.{'b' * 64}.t_000000a2", ("b" * 64, "t_000000a2")),
        (f"kanban.{'b' * 65}.t_000000a2", None),
        ("kanban.-board.t_000000a2", None),
    ],
)
def test_task_ids_and_board_slugs_must_match_hermes_formats(slice_id, expected):
    assert kp.parse_slice_id(slice_id) == expected


# Snapshots and revisions.


def test_creator_is_provenance_not_a_dependency():
    projection = kp.project(snapshot(), kp.load_overlay(OVERLAY))

    assert projection.provenance[sid("t_000000a3")]["spawned_by"] == "t_000000a1"
    assert by_id(projection)[sid("t_000000a3")]["depends_on"] == []
    # A malformed payload yields no provenance rather than an error.
    assert projection.provenance[sid("t_000000a8")]["spawned_by"] == ""


@pytest.mark.parametrize(
    ("payload", "creators"),
    [
        ('{"creator_task_id": "t_000000a9"}', {"t_000000a1": "t_000000a9"}),
        ('{"creator_task_id": 5}', {}),
        ('{"creator_task_id": ""}', {}),
        ('{"creator_task_id": ["t_000000a9"]}', {}),
        ('{"creator_task_id": null}', {}),
        ('["t_000000a9"]', {}),  # JSON, but not an object
        ("not json", {}),
        ("", {}),
        (None, {}),
    ],
)
def test_only_a_non_empty_string_creator_counts(payload, creators):
    built = kp.build_snapshot(BOARD, (task("t_000000a1"),), (), [("t_000000a1", payload)], 1)

    assert built.creators == creators


def test_equal_boards_get_equal_revisions_whatever_order_a_backend_lists_columns_in():
    reordered = tuple(dict(reversed(list(row.items()))) for row in TASKS)

    assert snapshot(tasks=reordered).revision == snapshot().revision


GOLDEN_TASKS: tuple[dict[str, Any], ...] = (
    {"id": "t_000000a1", "title": "Café", "status": "ready", "priority": 2, "created_at": 100},
    {"id": "t_000000a2", "title": "Ship it", "status": "todo", "priority": 0, "created_at": 200},
)
GOLDEN_CANONICAL = (
    '{"board":"example-board","creators":{"t_000000a2":"t_000000a1"},"links":[["t_000000a1","t_000000a2"]],'
    '"tasks":[{"created_at":100,"id":"t_000000a1","priority":2,"status":"ready","title":"Café"},'
    '{"created_at":200,"id":"t_000000a2","priority":0,"status":"todo","title":"Ship it"}]}'
)
GOLDEN_REVISION = "d2ef9909fe7f0c4e5f1a8f3756559e0590383961a2da3310a94b6ff044f65c2f"


def test_the_revision_is_the_sha256_of_the_boards_canonical_json():
    """Sorted keys, no spaces, UTF-8 left unescaped: the revision any backend must reproduce."""
    built = kp.build_snapshot(
        BOARD, GOLDEN_TASKS, (("t_000000a1", "t_000000a2"),), [("t_000000a2", '{"creator_task_id": "t_000000a1"}')], 3
    )

    assert built.revision == GOLDEN_REVISION
    assert hashlib.sha256(GOLDEN_CANONICAL.encode("utf-8")).hexdigest() == GOLDEN_REVISION


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"tasks": edited("t_000000a3", title="Speed up the cold loop")}, id="title"),
        pytest.param({"tasks": edited("t_000000a3", status="running")}, id="status"),
        pytest.param({"tasks": edited("t_000000a3", priority=2)}, id="priority"),
        pytest.param({"tasks": edited("t_000000a3", created_at=301)}, id="created_at"),
        pytest.param({"links": with_link("t_000000a3", "t_000000a7")}, id="link"),
        pytest.param({"created": (*CREATED, ("t_000000a7", '{"creator_task_id": "t_000000a3"}'))}, id="creator"),
    ],
)
def test_every_field_the_projection_reads_moves_the_revision(changed):
    assert snapshot(**changed).revision != snapshot().revision


def test_a_snapshot_or_projection_cannot_be_altered():
    board = snapshot()
    projection = kp.project(board, kp.load_overlay(OVERLAY))

    with pytest.raises(dataclasses.FrozenInstanceError):
        board.revision = "0" * 64  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        projection.revision = "0" * 64  # ty: ignore[invalid-assignment]


def test_stale_revision_is_refused():
    expected = snapshot().revision
    moved = snapshot(tasks=edited("t_000000a3", status="running"))

    kp.require_revision(snapshot(), expected)  # an unchanged board passes
    with pytest.raises(kp.StaleRevisionError, match=f"board 'example-board' is at revision {moved.revision}, not the expected"):
        kp.require_revision(moved, expected)


# Projection.


def test_projection_maps_status_priority_and_dependencies():
    projection = kp.project(snapshot(), kp.load_overlay(OVERLAY))
    slices = by_id(projection)

    assert [record["slice_id"] for record in projection.queue["slices"]] == [
        sid("t_000000a3"),
        sid("t_000000a1"),
        sid("t_000000a2"),
        sid("t_000000a4"),
        sid("t_000000a7"),
        sid("t_000000a8"),
    ]
    assert {key: record["status"] for key, record in slices.items()} == {
        sid("t_000000a1"): "done",
        sid("t_000000a2"): "queued",
        sid("t_000000a3"): "queued",
        sid("t_000000a4"): "blocked",
        sid("t_000000a7"): "blocked",
        sid("t_000000a8"): "deferred",
    }
    # Kanban's highest priority becomes the queue's lowest number.
    assert slices[sid("t_000000a3")]["priority"] == 0
    assert slices[sid("t_000000a2")]["priority"] == 1
    # The archived parent counts as satisfied and drops out, as recompute_ready treats it.
    assert slices[sid("t_000000a2")]["depends_on"] == [sid("t_000000a1")]
    assert slices[sid("t_000000a4")]["entry_condition"] == "Kanban task is waiting on open parents (todo)."
    assert slices[sid("t_000000a8")]["entry_condition"] == "Vendor ships the upstream fix."
    assert slices[sid("t_000000a2")]["required_proof_level"] == "domain-tested"
    assert "required_proof_level" not in slices[sid("t_000000a3")]


@pytest.mark.parametrize(
    ("kanban_status", "slice_status", "entry_condition"),
    [
        ("ready", "queued", ""),
        ("running", "in_progress", ""),
        ("done", "done", ""),
        ("blocked", "blocked", "Kanban task is blocked."),
        ("todo", "blocked", "Kanban task is waiting on open parents (todo)."),
        ("review", "blocked", "Kanban task is waiting for review."),
        ("triage", "deferred", "Kanban task is still in triage."),
        ("scheduled", "deferred", "Kanban task is scheduled for later."),
    ],
)
def test_every_kanban_status_maps_to_its_slice_status_and_entry_condition(kanban_status, slice_status, entry_condition):
    projection = kp.project(board_of(task("t_000000b1", kanban_status)), approving("t_000000b1"))

    [record] = projection.queue["slices"]
    assert (record["status"], record["entry_condition"]) == (slice_status, entry_condition)


def test_kanban_priority_is_inverted_so_the_highest_priority_runs_first():
    board = board_of(
        task("t_000000b1", priority=3, created_at=100),
        task("t_000000b2", priority=0, created_at=200),
        task("t_000000b3", priority=-2, created_at=300),
    )

    projection = kp.project(board, approving("t_000000b3", "t_000000b2", "t_000000b1"))

    assert [(record["slice_id"], record["priority"]) for record in projection.queue["slices"]] == [
        (sid("t_000000b1"), 0),
        (sid("t_000000b2"), 3),
        (sid("t_000000b3"), 5),
    ]


def test_queue_picks_the_task_kanban_would_dispatch_first():
    projection = kp.project(snapshot(), kp.load_overlay(OVERLAY))

    selected = policy.select_next_slice(projection.queue)

    assert selected is not None and selected["slice_id"] == sid("t_000000a3")


def test_equal_priorities_dispatch_oldest_first_then_by_id():
    board = board_of(
        task("t_000000b1", created_at=200),
        task("t_000000b2", created_at=100),
        task("t_000000b3", created_at=100),
    )

    projection = kp.project(board, approving("t_000000b3", "t_000000b1", "t_000000b2"))

    assert [record["slice_id"] for record in projection.queue["slices"]] == [
        sid("t_000000b2"),
        sid("t_000000b3"),
        sid("t_000000b1"),
    ]


def test_an_archived_parent_counts_as_satisfied_and_drops_out():
    projection = kp.project(snapshot(), kp.load_overlay(OVERLAY))  # approves the archived t_000000a5
    slices = by_id(projection)

    assert sid("t_000000a5") not in slices
    assert sid("t_000000a5") not in projection.provenance
    assert slices[sid("t_000000a2")]["status"] == "queued"
    assert slices[sid("t_000000a2")]["depends_on"] == [sid("t_000000a1")]


def test_unapproved_card_is_never_projected():
    projection = kp.project(snapshot(), kp.load_overlay(OVERLAY))

    assert sid("t_000000a6") not in by_id(projection)
    # Its approved child is held instead of being left runnable behind an invisible parent.
    child = by_id(projection)[sid("t_000000a7")]
    assert child["status"] == "blocked"
    assert child["depends_on"] == []
    assert "t_000000a6" in child["entry_condition"]


def test_an_open_unapproved_parent_holds_only_a_queued_child():
    board = board_of(
        task("t_000000b0"),
        task("t_000000b1", "todo"),
        task("t_000000b2", "running"),
        task("t_000000b3", "ready"),
        links=(("t_000000b0", "t_000000b1"), ("t_000000b0", "t_000000b2"), ("t_000000b0", "t_000000b3")),
    )

    slices = by_id(kp.project(board, approving("t_000000b1", "t_000000b2", "t_000000b3")))

    assert {key: (record["status"], record["entry_condition"]) for key, record in slices.items()} == {
        sid("t_000000b1"): ("blocked", "Kanban task is waiting on open parents (todo)."),
        sid("t_000000b2"): ("in_progress", ""),
        sid("t_000000b3"): ("blocked", "Kanban parent t_000000b0 is open and not approved for projection."),
    }


def test_a_done_unapproved_parent_does_not_hold_its_child():
    board = board_of(task("t_000000b0", "done"), task("t_000000b1"), links=(("t_000000b0", "t_000000b1"),))

    [child] = kp.project(board, approving("t_000000b1")).queue["slices"]

    assert (child["status"], child["depends_on"]) == ("queued", [])


def test_a_link_to_a_task_missing_from_the_board_holds_its_child():
    board = board_of(task("t_000000b1"), links=(("t_0000dead", "t_000000b1"),))

    [child] = kp.project(board, approving("t_000000b1")).queue["slices"]

    assert child["status"] == "blocked"
    assert child["entry_condition"] == "Kanban parent t_0000dead is open and not approved for projection."
    assert child["depends_on"] == []


def test_an_approval_entry_condition_replaces_the_status_default_unless_blank():
    board = board_of(task("t_000000b1", "blocked"), task("t_000000b2", "blocked"), task("t_000000b3", "ready"))
    overlay = overlay_with(
        approved={
            "t_000000b1": approval(entry_condition="Legal signs off."),
            "t_000000b2": approval(entry_condition="   "),
            "t_000000b3": approval(entry_condition="Only after the freeze."),
        }
    )

    slices = by_id(kp.project(board, overlay))

    assert slices[sid("t_000000b1")]["entry_condition"] == "Legal signs off."
    assert slices[sid("t_000000b2")]["entry_condition"] == "Kanban task is blocked."
    assert slices[sid("t_000000b3")]["entry_condition"] == "Only after the freeze."


def test_optional_policy_fields_pass_through_only_when_set():
    overlay = mapped_overlay()

    projection = kp.project(snapshot(), overlay)
    slices = by_id(projection)

    assert projection.queue["version"] == 1
    assert projection.queue["policy"] == json.loads(OVERLAY.read_text(encoding="utf-8"))["policy"]
    assert slices[sid("t_000000a2")]["required_proof_level"] == "domain-tested"
    assert "required_proof_level" not in slices[sid("t_000000a3")]
    assert all("queue_slice_id" not in record for record in slices.values())  # for the shadow diff only


@pytest.mark.parametrize(("backend", "prefix"), [("hermes", "kanban"), ("local", "board")])
def test_provenance_names_backend_board_task_status_priority_and_creator(backend, prefix):
    projection = kp.project(snapshot(), overlay_with(backend=backend))

    assert projection.provenance[f"{prefix}.{BOARD}.t_000000a3"] == {
        "backend": backend,
        "board": BOARD,
        "task_id": "t_000000a3",
        "kanban_status": "ready",
        "kanban_priority": 1,
        "spawned_by": "t_000000a1",
    }
    assert sorted(projection.provenance) == sorted(record["slice_id"] for record in projection.queue["slices"])
    assert all(record["slice_id"].startswith(f"{prefix}.{BOARD}.") for record in projection.queue["slices"])
    assert (projection.revision, projection.event_watermark) == (snapshot().revision, WATERMARK)


def test_repeat_projection_is_byte_identical():
    first = kp.project(snapshot(), kp.load_overlay(OVERLAY))
    second = kp.project(snapshot(), kp.load_overlay(OVERLAY))

    assert json.dumps(first.queue, sort_keys=True) == json.dumps(second.queue, sort_keys=True)
    assert first.provenance == second.provenance
    assert first.revision == second.revision


def test_projected_queue_round_trips_through_load_queue(tmp_path):
    projection = kp.project(snapshot(), kp.load_overlay(OVERLAY))
    queue_path = tmp_path / "slices.json"
    policy.write_json(queue_path, projection.queue)

    assert policy.load_queue(queue_path) == projection.queue


def test_unknown_kanban_status_is_refused():
    with pytest.raises(kp.ProjectionError, match="unknown Kanban status 'failed'"):
        kp.project(snapshot(tasks=edited("t_000000a3", status="failed")), kp.load_overlay(OVERLAY))


def test_two_running_tasks_cannot_become_one_queue():
    board = snapshot(tasks=edited("t_000000a2", "t_000000a3", status="running"))

    with pytest.raises(kp.ProjectionError, match="at most one slice may be in_progress"):
        kp.project(board, kp.load_overlay(OVERLAY))


def test_overlay_for_a_foreign_board_is_refused():
    with pytest.raises(kp.ProjectionError, match="not 'example-board'"):
        kp.project(snapshot(), overlay_with(board="other-board"))


def test_overlay_approving_a_task_from_another_board_is_refused():
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document["approved"]["t_0000ffff"] = document["approved"]["t_000000a1"]

    with pytest.raises(kp.ProjectionError, match="not on board"):
        kp.project(snapshot(), kp.KanbanOverlay.model_validate(document))


def test_an_overlay_that_approves_nothing_projects_an_empty_queue():
    projection = kp.project(snapshot(), overlay_with(approved={}))

    assert projection.queue["slices"] == []
    assert projection.provenance == {}


def test_overlay_must_carry_the_policy_fields(tmp_path):
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    document["approved"]["t_000000a1"]["allowed_paths"] = []
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(kp.ProjectionError, match="allowed_paths"):
        kp.load_overlay(path)


def test_an_overlay_without_a_backend_is_a_hermes_overlay():
    overlay = kp.load_overlay(OVERLAY)  # written before the local board, so it has no backend field

    assert overlay.backend == "hermes"
    assert all(record["slice_id"].startswith("kanban.") for record in kp.project(snapshot(), overlay).queue["slices"])


# Reading a Hermes board's SQLite file.


def test_reading_the_example_board_gives_the_in_memory_snapshot(tmp_path):
    assert kp.read_board(make_board(tmp_path)) == snapshot()


def test_concurrent_edit_leaves_the_snapshot_intact_and_moves_the_revision(tmp_path):
    board_dir = make_board(tmp_path)
    before = kp.read_board(board_dir)
    execute(board_dir, "INSERT INTO task_links VALUES ('t_000000a3', 't_000000a7');")
    after = kp.read_board(board_dir)

    assert ("t_000000a3", "t_000000a7") not in before.links
    assert ("t_000000a3", "t_000000a7") in after.links
    assert before.revision != after.revision
    kp.require_revision(after, after.revision)


def test_heartbeat_style_events_do_not_move_the_revision(tmp_path):
    board_dir = make_board(tmp_path)
    before = kp.read_board(board_dir)
    execute(
        board_dir, "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_000000a3', 'heartbeat', '{}', 900);"
    )
    after = kp.read_board(board_dir)

    assert after.event_watermark == before.event_watermark + 1
    assert after.revision == before.revision


def test_a_missing_priority_reads_as_zero(tmp_path):
    board_dir = make_board(tmp_path, "UPDATE tasks SET priority = NULL WHERE id = 't_000000a3';")

    assert kp.read_board(board_dir) == snapshot(tasks=edited("t_000000a3", priority=0))


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        pytest.param("ALTER TABLE tasks DROP COLUMN priority;", "table 'tasks' lacks columns \\['priority'\\]", id="tasks"),
        pytest.param(
            "ALTER TABLE task_events DROP COLUMN payload;", "table 'task_events' lacks columns \\['payload'\\]", id="task_events"
        ),
        pytest.param(
            "DROP TABLE task_links; CREATE TABLE task_links (parent_id TEXT NOT NULL);",
            "table 'task_links' lacks columns \\['child_id'\\]",
            id="task_links",
        ),
    ],
)
def test_missing_schema_column_is_refused(tmp_path, sql, message):
    board_dir = make_board(tmp_path, sql)

    with pytest.raises(kp.ProjectionError, match=message):
        kp.read_board(board_dir)


def test_board_directory_must_be_a_slug(tmp_path):
    with pytest.raises(kp.ProjectionError, match="not a Kanban board slug"):
        kp.read_board(make_board(tmp_path, board="Not A Slug"))


def test_a_symlinked_or_missing_board_database_is_refused(tmp_path):
    missing = tmp_path / "missing" / BOARD
    missing.mkdir(parents=True)
    real = make_board(tmp_path / "real")
    linked = tmp_path / "linked" / BOARD
    linked.mkdir(parents=True)
    (linked / "kanban.db").symlink_to(real / "kanban.db")

    for board_dir in (missing, linked):
        with pytest.raises(kp.ProjectionError, match="missing board database"):
            kp.read_board(board_dir)


def test_reading_a_board_writes_nothing(tmp_path):
    board_dir = make_board(tmp_path)
    db = board_dir / "kanban.db"
    contents = db.read_bytes()
    # A board Hermes owns may be read-only to us. Root ignores file modes, so this half proves
    # something only for an ordinary user; the checks below hold either way.
    db.chmod(0o444)
    board_dir.chmod(0o555)
    try:
        kp.project(kp.read_board(board_dir), kp.load_overlay(OVERLAY))
        with kp.read_only(board_dir) as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM tasks")
    finally:
        board_dir.chmod(0o755)
        db.chmod(0o644)

    assert db.read_bytes() == contents
    assert sorted(os.listdir(board_dir)) == ["kanban.db"]


def test_read_snapshot_reads_either_kind_of_board(tmp_path):
    local = lb.init_board(tmp_path / "local-board", "tester", clock=lambda: 1000.0)
    task_id = local.add("Write the docs")

    local_snapshot = kp.read_snapshot(tmp_path / "local-board", "local")

    assert local_snapshot.board == "local-board"
    assert [(row["id"], row["title"]) for row in local_snapshot.tasks] == [(task_id, "Write the docs")]
    assert kp.read_snapshot(make_board(tmp_path), "hermes") == snapshot()


# The shadow diff.


def mapped_overlay(**changes: object) -> kp.KanbanOverlay:
    """The example overlay with every approved task mapped to queue slice ``q.<task>``."""
    document = json.loads(OVERLAY.read_text(encoding="utf-8"))
    for task_id, approved in document["approved"].items():
        approved["queue_slice_id"] = f"q.{task_id}"
    document.update(changes)
    return kp.KanbanOverlay.model_validate(document)


def unmapping(overlay: kp.KanbanOverlay, task_id: str) -> kp.KanbanOverlay:
    unmapped = overlay.approved[task_id].model_copy(update={"queue_slice_id": None})
    return overlay.model_copy(update={"approved": {**overlay.approved, task_id: unmapped}})


def mirror(projection: kp.Projection) -> dict:
    """The JSON queue the projection stands for, under the ``q.<task>`` ids."""
    rename = {slice_id: f"q.{slice_id.rsplit('.', 1)[1]}" for slice_id in by_id(projection)}
    slices = [
        {**copy.deepcopy(record), "slice_id": rename[record["slice_id"]], "depends_on": [rename[d] for d in record["depends_on"]]}
        for record in projection.queue["slices"]
    ]
    return {**projection.queue, "slices": slices}


def test_a_matching_queue_has_an_empty_shadow_diff():
    overlay = mapped_overlay()
    projection = kp.project(snapshot(), overlay)

    diff = kp.shadow_diff(projection, overlay, mirror(projection))

    assert all("queue_slice_id" not in record for record in projection.queue["slices"])  # never projected
    assert diff["mismatches"] == [] and diff["unmapped_tasks"] == diff["missing_from_queue"] == diff["queue_only"] == []
    assert diff["next"]["queue"] == diff["next"]["kanban"] is not None


def test_the_shadow_diff_reports_every_difference():
    overlay = unmapping(mapped_overlay(), "t_000000a7")
    projection = kp.project(snapshot(), overlay)
    queue = mirror(projection)
    slices = {record["slice_id"]: record for record in queue["slices"]}
    slices["q.t_000000a1"]["status"] = "blocked"
    slices["q.t_000000a1"]["entry_condition"] = "Waiting."
    slices["q.t_000000a1"]["allowed_paths"].reverse()  # order alone is not a difference
    slices["q.t_000000a2"]["required_validations"] = []
    del slices["q.t_000000a4"]
    slices["q.extra"] = {**slices["q.t_000000a3"], "slice_id": "q.extra", "depends_on": []}
    queue = {**queue, "slices": list(slices.values())}

    diff = kp.shadow_diff(projection, overlay, queue)

    assert sorted((m["slice"], m["field"]) for m in diff["mismatches"]) == [
        ("q.t_000000a1", "status"),
        ("q.t_000000a2", "required_validations"),
    ]
    assert diff["unmapped_tasks"] == [sid("t_000000a7")]
    assert diff["missing_from_queue"] == ["q.t_000000a4"]
    assert "q.extra" in diff["queue_only"] and "q.t_000000a7" in diff["queue_only"]


def test_two_tasks_mapped_to_one_queue_slice_are_refused():
    overlay = mapped_overlay()
    twin = overlay.approved["t_000000a2"].model_copy(update={"queue_slice_id": "q.t_000000a1"})
    overlay = overlay.model_copy(update={"approved": {**overlay.approved, "t_000000a2": twin}})
    projection = kp.project(snapshot(), overlay)

    with pytest.raises(kp.ProjectionError, match="more than one task"):
        kp.shadow_diff(projection, overlay, mirror(projection))


def test_a_dependency_on_an_unmapped_task_is_compared_as_is():
    overlay = unmapping(mapped_overlay(), "t_000000a1")
    projection = kp.project(snapshot(), overlay)
    queue = mirror(projection)
    for record in queue["slices"]:
        if record["slice_id"] == "q.t_000000a2":
            record["depends_on"] = [sid("t_000000a1")]  # the queue names the unmapped task by its Kanban id

    diff = kp.shadow_diff(projection, overlay, queue)

    assert diff["mismatches"] == []
    assert diff["unmapped_tasks"] == [sid("t_000000a1")]


def test_the_queue_side_dependencies_are_not_translated():
    overlay = mapped_overlay()
    projection = kp.project(snapshot(), overlay)
    queue = mirror(projection)
    for record in queue["slices"]:
        if record["slice_id"] == "q.t_000000a2":
            record["depends_on"] = [sid("t_000000a1")]  # a Kanban id in the JSON queue is a difference

    diff = kp.shadow_diff(projection, overlay, queue)

    assert diff["mismatches"] == [
        {
            "slice": "q.t_000000a2",
            "kanban_slice": sid("t_000000a2"),
            "field": "depends_on",
            "queue": [sid("t_000000a1")],
            "kanban": ["q.t_000000a1"],
        }
    ]


def test_next_reports_the_queue_id_or_the_raw_kanban_id_or_none():
    mapped = mapped_overlay()
    projection = kp.project(snapshot(), mapped)
    queue = mirror(projection)
    unmapped = unmapping(mapped, "t_000000a3")
    nothing_runnable = mapped.model_copy(update={"approved": {"t_000000a1": mapped.approved["t_000000a1"]}})
    empty_queue = {**queue, "slices": []}

    assert kp.shadow_diff(projection, mapped, queue)["next"] == {"queue": "q.t_000000a3", "kanban": "q.t_000000a3"}
    assert kp.shadow_diff(kp.project(snapshot(), unmapped), unmapped, queue)["next"] == {
        "queue": "q.t_000000a3",
        "kanban": sid("t_000000a3"),
    }
    assert kp.shadow_diff(kp.project(snapshot(), nothing_runnable), nothing_runnable, empty_queue)["next"] == {
        "queue": None,
        "kanban": None,
    }


# The command line.


def test_cli_prints_the_projection_and_fails_cleanly(tmp_path, capsys):
    board_dir = make_board(tmp_path)
    expected = kp.project(snapshot(), kp.load_overlay(OVERLAY))

    assert kp.main(["--board-dir", str(board_dir), "--overlay", str(OVERLAY)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "revision": snapshot().revision,
        "event_watermark": WATERMARK,
        "provenance": expected.provenance,
        "queue": expected.queue,
    }

    assert kp.main(["--board-dir", str(tmp_path / "missing"), "--overlay", str(OVERLAY)]) == 1
    assert "missing board database" in capsys.readouterr().err


def test_the_cli_prints_the_shadow_diff(tmp_path, capsys):
    overlay_path = tmp_path / "overlay.json"
    overlay_path.write_text(mapped_overlay().model_dump_json(exclude_none=True), encoding="utf-8")
    board_dir = make_board(tmp_path)
    queue_path = tmp_path / "slices.json"
    queue_path.write_text(json.dumps(mirror(kp.project(kp.read_board(board_dir), mapped_overlay()))), encoding="utf-8")

    assert kp.main(["--board-dir", str(board_dir), "--overlay", str(overlay_path), "--shadow-queue", str(queue_path)]) == 0
    assert json.loads(capsys.readouterr().out)["shadow"]["mismatches"] == []


@pytest.mark.parametrize(
    ("overlay_text", "queue_text", "message"),
    [
        pytest.param('{"version": 1}', None, "error: Invalid Kanban overlay", id="invalid-overlay"),
        pytest.param(None, '{"version": 1, "slices": "none"}', "error: Invalid queue file", id="invalid-queue"),
        pytest.param(None, "", "error: queue file not found", id="missing-queue"),
    ],
)
def test_the_cli_exits_1_on_an_invalid_overlay_or_queue(tmp_path, capsys, overlay_text, queue_text, message):
    overlay_path = OVERLAY
    if overlay_text is not None:
        overlay_path = tmp_path / "overlay.json"
        overlay_path.write_text(overlay_text, encoding="utf-8")
    argv = ["--board-dir", str(make_board(tmp_path)), "--overlay", str(overlay_path)]
    if queue_text is not None:
        queue_path = tmp_path / "slices.json"
        if queue_text:
            queue_path.write_text(queue_text, encoding="utf-8")
        argv += ["--shadow-queue", str(queue_path)]

    assert kp.main(argv) == 1
    assert capsys.readouterr().err.startswith(message)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--overlay", str(OVERLAY)], id="no-board-dir"),
        pytest.param(["--board-dir", "example-board"], id="no-overlay"),
    ],
)
def test_the_cli_requires_the_board_and_the_overlay(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        kp.main(argv)

    assert exit_info.value.code == 2
    assert "the following arguments are required" in capsys.readouterr().err


def test_the_module_runs_as_a_command(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "automation.supervisor.kanban_projection",
            "--board-dir",
            str(tmp_path / BOARD),
            "--overlay",
            str(OVERLAY),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1
    assert "error: missing board database" in result.stderr
