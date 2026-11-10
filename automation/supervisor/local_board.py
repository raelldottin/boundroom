"""A local Kanban board with no Hermes dependency (SPEC-004, RA-BD-002).

``docs/architecture/agent-agnostic-board.md`` specifies it. A board is one SQLite file,
``<board_dir>/board.db``, read and written with the standard library only: no Hermes, no daemon
and no network. The board directory's name is its slug, as for Hermes boards.

``LocalBoard`` is a ``board_backend.BoardBackend``, so the supervisor reaches it exactly as it
reaches a Hermes board, and it keeps the same write-back contract (``kanban-cutover.md``):

- ``snapshot`` is read-only, and its revision is built by ``kanban_projection.build_snapshot``,
  so equal boards have equal revisions whichever backend read them.
- ``claim`` checks the revision **inside** the write transaction (``BEGIN IMMEDIATE``), so the
  check and the write are atomic across processes, which the Hermes CLI can't offer.
- ``record`` and ``renew`` write only on the supervisor's own current run. A supervisor ``done``
  becomes ``review``; only ``approve`` moves a task to ``done``.
- A claim belongs to its run, not to a process, so ``renew`` can extend it. An expired claim is
  released by the next write to the board (or by ``expire``), and its run ends ``expired``.

People and agents use the command line (``python -m automation.supervisor.local_board``) to add,
link and prioritize tasks, approve reviewed ones and unblock blocked ones. ``claim``, ``record``
and ``renew`` are library calls for the supervisor only. Nothing here enforces who approves:
every event records its ``actor``, which SPEC-004 decision 3 accepts as enough for now.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Generator, Optional, Sequence

from automation.supervisor import kanban_projection as kp
from automation.supervisor.board_backend import Claim

DB_NAME = "board.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_SECONDS = 30
SUPERVISOR_ACTOR = "repo-automation-supervisor"
EXPIRY_ACTOR = "repo-automation-board"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    assignee       TEXT,
    current_run_id INTEGER
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL REFERENCES tasks(id),
    child_id  TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    status           TEXT NOT NULL,
    outcome          TEXT,
    claimed_by       TEXT NOT NULL,
    claimed_at       INTEGER NOT NULL,
    claim_expires_at REAL NOT NULL,
    ended_at         INTEGER,
    metadata         TEXT
);
CREATE TABLE task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    run_id  INTEGER,
    kind    TEXT NOT NULL,
    actor   TEXT NOT NULL,
    at      INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""

# Supervisor verdict -> (task status, run outcome, block kind). The same mapping as Hermes's
# ``request-review`` and ``block --kind`` in ``kanban_writer.VERDICTS``.
VERDICTS: dict[str, tuple[str, str, str]] = {
    "done": ("review", "review_requested", ""),
    "blocked": ("blocked", "blocked", "needs_input"),
    "failed": ("blocked", "blocked", "transient"),
}
OPEN_TO_LINK = frozenset({"todo", "ready", "blocked"})


class LocalBoardError(kp.ProjectionError):
    """The local board refused an operation, or its file isn't a board this code can read."""


def init_board(board_dir: Path, actor: str, clock: Callable[[], float] = time.time) -> LocalBoard:
    """Create an empty board at ``<board_dir>/board.db``. Refuses an existing board."""
    slug = kp.board_slug(board_dir)
    db = board_dir / DB_NAME
    if db.exists():
        raise LocalBoardError(f"board already exists: {db}")
    board_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        # executescript commits any open transaction first, so the script opens its own and
        # leaves it open for the parameterized inserts; the board appears whole or not at all.
        conn.executescript(f"BEGIN IMMEDIATE;{SCHEMA}PRAGMA user_version = {SCHEMA_VERSION};")
        conn.execute("INSERT INTO meta (key, value) VALUES ('slug', ?)", (slug,))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, actor, at, payload) VALUES (NULL, NULL, 'board_created', ?, ?, '{}')",
            (actor, int(clock())),
        )
        conn.execute("COMMIT")
    return LocalBoard(board_dir, clock=clock, actor=actor)


class LocalBoard:
    """One local board. ``actor`` names whoever this instance writes as; the supervisor keeps the default."""

    def __init__(self, board_dir: Path, clock: Callable[[], float] = time.time, actor: str = SUPERVISOR_ACTOR) -> None:
        self.board_dir = board_dir
        self.clock = clock
        self.actor = actor

    @property
    def slug(self) -> str:
        return kp.board_slug(self.board_dir)

    # The supervisor's interface (board_backend.BoardBackend).

    def snapshot(self) -> kp.BoardSnapshot:
        with self._connect() as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            try:
                return self._snapshot(conn)
            finally:
                conn.rollback()

    def claim(self, task_id: str, expected_revision: str, ttl_seconds: int) -> Claim:
        if ttl_seconds <= 0:
            raise LocalBoardError(f"a claim needs a positive TTL, not {ttl_seconds}")
        with self._write() as conn:
            kp.require_revision(self._snapshot(conn), expected_revision)
            task = self._task(conn, task_id)
            if task["status"] != "ready":
                raise LocalBoardError(f"task {task_id} is {task['status']}, not ready")
            if task["assignee"]:
                raise LocalBoardError(f"task {task_id} is assigned to {task['assignee']!r}")
            now = self.clock()
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, claimed_by, claimed_at, claim_expires_at)"
                " VALUES (?, 'running', ?, ?, ?)",
                (task_id, self.actor, int(now), now + ttl_seconds),
            ).lastrowid
            assert run_id is not None
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, task_id))
            self._event(conn, task_id, run_id, "claimed", {"ttl_seconds": ttl_seconds})
        return Claim(self.slug, task_id, run_id)

    def renew(self, held: Claim, ttl_seconds: int) -> Claim:
        """Extend the supervisor's claim from now. Refuses a run that is no longer current."""
        if ttl_seconds <= 0:
            raise LocalBoardError(f"a claim needs a positive TTL, not {ttl_seconds}")
        self._require_board(held)
        with self._write() as conn:
            self._require_current(conn, held)
            conn.execute("UPDATE task_runs SET claim_expires_at = ? WHERE id = ?", (self.clock() + ttl_seconds, held.run_id))
            self._event(conn, held.task_id, held.run_id, "renewed", {"ttl_seconds": ttl_seconds})
        return held

    def record(self, held: Claim, verdict: str, reason: str, handoff_path: Optional[Path]) -> bool:
        """Record a verdict on the claimed run. Returns False when it was already recorded."""
        if verdict not in VERDICTS:
            raise LocalBoardError(f"no Kanban transition for verdict {verdict!r}")
        self._require_board(held)
        status, outcome, block_kind = VERDICTS[verdict]
        with self._write() as conn:
            if self._run(conn, held)["outcome"] == outcome:
                return False
            self._require_current(conn, held)
            # A run can fail before the agent wrote a handoff; the record then says so instead of a digest.
            digest = _handoff_digest(handoff_path)
            if verdict == "done" and digest == "none":
                raise LocalBoardError(f"a done verdict for {held.task_id} needs its handoff")
            metadata = {"slice_id": kp.slice_id_for(held.board, held.task_id), "handoff_sha256": digest, "reason": reason}
            if block_kind:
                metadata["block_kind"] = block_kind
            conn.execute(
                "UPDATE task_runs SET status = 'ended', outcome = ?, ended_at = ?, metadata = ? WHERE id = ?",
                (outcome, int(self.clock()), json.dumps(metadata, sort_keys=True), held.run_id),
            )
            conn.execute("UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ?", (status, held.task_id))
            self._event(conn, held.task_id, held.run_id, outcome, metadata)
        return True

    # What people do, through the command line.

    def add(self, title: str, priority: int = 0, parents: Sequence[str] = (), expected_revision: Optional[str] = None) -> str:
        if not title.strip():
            raise LocalBoardError("a task needs a title")
        with self._write(expected_revision) as conn:
            for parent_id in parents:
                self._task(conn, parent_id)
            task_id = self._new_task_id(conn)
            status = "todo" if self._open_parents(conn, parents) else "ready"
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, title.strip(), status, priority, int(self.clock())),
            )
            conn.executemany("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", [(p, task_id) for p in parents])
            self._event(conn, task_id, None, "created", {"title": title.strip(), "parents": sorted(parents)})
        return task_id

    def link(self, parent_id: str, child_id: str, expected_revision: Optional[str] = None) -> None:
        if parent_id == child_id:
            raise LocalBoardError(f"task {child_id} can't depend on itself")
        with self._write(expected_revision) as conn:
            self._task(conn, parent_id)
            child = self._task(conn, child_id)
            if child["status"] not in OPEN_TO_LINK:
                raise LocalBoardError(f"task {child_id} is {child['status']}; only todo, ready or blocked tasks take new parents")
            if child_id in self._ancestors_of(conn, parent_id):
                raise LocalBoardError(f"linking {parent_id} before {child_id} would make a cycle")
            if conn.execute("SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id)).fetchone():
                raise LocalBoardError(f"{parent_id} is already a parent of {child_id}")
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent_id, child_id))
            if child["status"] == "ready" and self._open_parents(conn, [parent_id]):
                conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child_id,))
            self._event(conn, child_id, None, "linked", {"parent_id": parent_id})

    def set_priority(self, task_id: str, priority: int, expected_revision: Optional[str] = None) -> None:
        with self._write(expected_revision) as conn:
            if self._task(conn, task_id)["status"] == "archived":
                raise LocalBoardError(f"task {task_id} is archived")
            conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id))
            self._event(conn, task_id, None, "prioritized", {"priority": priority})

    def approve(self, task_id: str, expected_revision: Optional[str] = None) -> list[str]:
        """Move a reviewed task to ``done`` and return the children that became ready."""
        return self._finish(task_id, "review", "done", "approved", expected_revision)

    def archive(self, task_id: str, expected_revision: Optional[str] = None) -> list[str]:
        """Archive a task that isn't running and return the children that became ready."""
        status = self.task(task_id)["status"]
        if status in {"running", "archived"}:
            raise LocalBoardError(f"task {task_id} is {status} and can't be archived")
        return self._finish(task_id, status, "archived", "archived", expected_revision)

    def unblock(self, task_id: str, expected_revision: Optional[str] = None) -> str:
        with self._write(expected_revision) as conn:
            if self._task(conn, task_id)["status"] != "blocked":
                raise LocalBoardError(f"task {task_id} is not blocked")
            status = "todo" if self._open_parents(conn, self._parents_of(conn, task_id)) else "ready"
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            self._event(conn, task_id, None, "unblocked", {"status": status})
        return status

    def expire(self) -> None:
        """Release every expired claim now. Every other write does this first anyway."""
        with self._write():
            pass

    def task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._task(conn, task_id)

    def events(self, task_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, run_id, kind, actor, at, payload FROM task_events"
                " WHERE (? IS NULL OR task_id = ?) ORDER BY id",
                (task_id, task_id),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    # Internals.

    @contextlib.contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        db = self.board_dir / DB_NAME
        if not db.is_file() or db.is_symlink():
            raise LocalBoardError(f"missing board database: {db}")
        conn = sqlite3.connect(db, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise LocalBoardError(f"board {db} has schema version {version}; this code reads version {SCHEMA_VERSION}")
            row = conn.execute("SELECT value FROM meta WHERE key = 'slug'").fetchone()
            if row is None or row[0] != self.slug:
                raise LocalBoardError(f"board database {db} is board {row[0] if row else None!r}, not {self.slug!r}")
            yield conn
        except sqlite3.Error as error:
            raise LocalBoardError(f"board {db}: {error}") from error
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self, expected_revision: Optional[str] = None) -> Generator[sqlite3.Connection]:
        """One write transaction, after releasing expired claims in a transaction of its own."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_claims(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if expected_revision is not None:
                    kp.require_revision(self._snapshot(conn), expected_revision)
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def _snapshot(self, conn: sqlite3.Connection) -> kp.BoardSnapshot:
        tasks = tuple(dict(row) for row in conn.execute("SELECT id, title, status, priority, created_at FROM tasks ORDER BY id"))
        links = tuple(
            (row[0], row[1]) for row in conn.execute("SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id")
        )
        created = [
            (row[0], row[1])
            for row in conn.execute("SELECT task_id, payload FROM task_events WHERE kind = 'created' ORDER BY id")
        ]
        watermark = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        return kp.build_snapshot(self.slug, tasks, links, created, int(watermark))

    def _expire_claims(self, conn: sqlite3.Connection) -> None:
        now = self.clock()
        expired = conn.execute(
            "SELECT id, task_id FROM task_runs WHERE status = 'running' AND claim_expires_at <= ?", (now,)
        ).fetchall()
        for run_id, task_id in expired:
            conn.execute(
                "UPDATE task_runs SET status = 'ended', outcome = 'expired', ended_at = ? WHERE id = ?", (int(now), run_id)
            )
            status = "todo" if self._open_parents(conn, self._parents_of(conn, task_id)) else "ready"
            conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ? AND current_run_id = ?",
                (status, task_id, run_id),
            )
            self._event(conn, task_id, run_id, "expired", {"status": status}, actor=EXPIRY_ACTOR)

    def _finish(self, task_id: str, from_status: str, to_status: str, kind: str, expected_revision: Optional[str]) -> list[str]:
        with self._write(expected_revision) as conn:
            status = self._task(conn, task_id)["status"]
            if status != from_status:
                raise LocalBoardError(f"task {task_id} is {status}, not {from_status}")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (to_status, task_id))
            self._event(conn, task_id, None, kind, {})
            promoted = []
            for (child_id,) in conn.execute(
                "SELECT child_id FROM task_links JOIN tasks ON tasks.id = child_id"
                " WHERE parent_id = ? AND tasks.status = 'todo' ORDER BY child_id",
                (task_id,),
            ).fetchall():
                if not self._open_parents(conn, self._parents_of(conn, child_id)):
                    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child_id,))
                    self._event(conn, child_id, None, "promoted", {"after": task_id})
                    promoted.append(child_id)
        return promoted

    def _require_board(self, held: Claim) -> None:
        if held.board != self.slug:
            raise LocalBoardError(f"claim is for board {held.board!r}, not {self.slug!r}")

    def _require_current(self, conn: sqlite3.Connection, held: Claim) -> None:
        run = self._run(conn, held)
        if run["status"] != "running" or self._task(conn, held.task_id)["current_run_id"] != held.run_id:
            raise LocalBoardError(f"run {held.run_id} of {held.task_id} is {run['outcome'] or run['status']}; not ours to close")

    def _task(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT id, title, status, priority, created_at, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise LocalBoardError(f"no task {task_id} on board {self.slug!r}")
        return dict(row)

    def _run(self, conn: sqlite3.Connection, held: Claim) -> dict[str, Any]:
        row = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ? AND task_id = ?", (held.run_id, held.task_id)
        ).fetchone()
        if row is None:
            raise LocalBoardError(f"run {held.run_id} of {held.task_id} is not on board {held.board!r}")
        return dict(row)

    def _parents_of(self, conn: sqlite3.Connection, task_id: str) -> list[str]:
        return [row[0] for row in conn.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))]

    def _ancestors_of(self, conn: sqlite3.Connection, task_id: str) -> set[str]:
        seen: set[str] = set()
        frontier = [task_id]
        while frontier:
            for parent_id in self._parents_of(conn, frontier.pop()):
                if parent_id not in seen:
                    seen.add(parent_id)
                    frontier.append(parent_id)
        return seen

    def _open_parents(self, conn: sqlite3.Connection, parents: Sequence[str]) -> list[str]:
        return [p for p in parents if self._task(conn, p)["status"] not in kp.TERMINAL_STATUSES]

    def _new_task_id(self, conn: sqlite3.Connection) -> str:
        while True:
            task_id = f"t_{secrets.token_hex(4)}"
            if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                return task_id

    def _event(
        self,
        conn: sqlite3.Connection,
        task_id: Optional[str],
        run_id: Optional[int],
        kind: str,
        payload: dict[str, Any],
        actor: Optional[str] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, actor, at, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, run_id, kind, actor or self.actor, int(self.clock()), json.dumps(payload, sort_keys=True)),
        )


def _handoff_digest(handoff_path: Optional[Path]) -> str:
    if handoff_path and handoff_path.is_file():
        return hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    return "none"


def snapshot_document(snapshot: kp.BoardSnapshot) -> dict[str, Any]:
    """The ``snapshot --json`` document: the board as the projection and the wiki read it."""
    return {
        "board": snapshot.board,
        "revision": snapshot.revision,
        "event_watermark": snapshot.event_watermark,
        "tasks": list(snapshot.tasks),
        "links": [list(link) for link in snapshot.links],
        "creators": snapshot.creators,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m automation.supervisor.local_board",
        description="Edit or read a local Kanban board. Claims and run records are the supervisor's alone.",
    )
    parser.add_argument("--board", type=Path, required=True, help="The board directory; its name is the board's slug.")
    parser.add_argument("--actor", default=None, help="Who this write is recorded as (default: the OS user).")
    parser.add_argument("--expect-revision", default=None, help="Refuse the write unless the board is at this revision.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the board.")
    add = commands.add_parser("add", help="Add a task: ready, or todo while it has open parents.")
    add.add_argument("title")
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--parent", action="append", default=[], help="A task this one depends on; repeatable.")
    link = commands.add_parser("link", help="Make PARENT a dependency of CHILD.")
    link.add_argument("parent")
    link.add_argument("child")
    priority = commands.add_parser("priority", help="Set a task's priority; higher runs first.")
    priority.add_argument("task")
    priority.add_argument("priority", type=int)
    for name, text in (
        ("approve", "Move a reviewed task to done and promote its ready children."),
        ("archive", "Archive a task that isn't running."),
        ("unblock", "Move a blocked task back to ready (or todo while parents are open)."),
        ("show", "Print one task and its events."),
    ):
        commands.add_parser(name, help=text).add_argument("task")
    commands.add_parser("expire", help="Release expired claims now.")
    commands.add_parser("list", help="Print every task.")
    commands.add_parser("snapshot", help="Print the board as JSON, with its revision.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    actor = args.actor or getpass.getuser()
    expected = args.expect_revision
    try:
        if args.command == "init":
            init_board(args.board, actor)
            print(f"created board {kp.board_slug(args.board)!r} at {args.board / DB_NAME}")
            return 0
        board = LocalBoard(args.board, actor=actor)
        if args.command == "add":
            print(board.add(args.title, args.priority, args.parent, expected))
        elif args.command == "link":
            board.link(args.parent, args.child, expected)
        elif args.command == "priority":
            board.set_priority(args.task, args.priority, expected)
        elif args.command in {"approve", "archive"}:
            promoted = getattr(board, args.command)(args.task, expected)
            print("\n".join(f"ready: {task_id}" for task_id in promoted) or "no children became ready")
        elif args.command == "unblock":
            print(board.unblock(args.task, expected))
        elif args.command == "expire":
            board.expire()
        elif args.command == "list":
            for task in board.snapshot().tasks:
                print(f"{task['id']}  {task['status']:<8}  p{task['priority']:<3}  {task['title']}")
        elif args.command == "show":
            print(json.dumps({"task": board.task(args.task), "events": board.events(args.task)}, indent=2, sort_keys=True))
        else:
            print(json.dumps(snapshot_document(board.snapshot()), indent=2, sort_keys=True))
    except kp.ProjectionError as error:
        print(f"stop: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
