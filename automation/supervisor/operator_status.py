"""Report what Boundroom would do next in this repository, and from what (RA-PROD-101).

Usage::

    python3 -m automation.supervisor.operator_status [--json]
    python3 -m automation.supervisor.operator_status --board-dir <board-dir> --overlay <overlay.json> [--json]

It reports the Boundroom version, the consumer's sync pin, whether slices come from the JSON
queue or a board, the next eligible slice, the blocked and deferred slices, the reasons
``run_next.py`` would refuse to start, and the provenance of the report: the commit, whether
the worktree is clean, and every file it read.

The command is read-only. It writes nothing: no queue, board, handoff, lock or state file. Run
as a command, it caches no bytecode for the modules it imports; ``python3 -m`` caches the module
itself before it runs, so run it with ``PYTHONDONTWRITEBYTECODE=1`` to leave no ``__pycache__``
at all. It selects with the supervisor's own functions
(``policy.select_next_slice`` and ``policy.blocked_slice_reports``, as ``run_next.py --dry-run
--include-blocked`` does), and projects a board with ``kanban_projection``. Its only
subprocesses are the read-only ``git`` calls in ``policy.git_head`` and ``policy.git_dirty_paths``.
It never starts or advances a slice.

Exit codes: 0 when the report was printed; 1 when it was printed but the queue, the board or
git could not be read, so the report is incomplete; 2 for a usage error.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    # Before the imports below: a status check run as a command leaves no __pycache__ behind.
    sys.dont_write_bytecode = True

import argparse
import json
from pathlib import Path
from typing import Any, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import kanban_projection, local_board, policy, run_next
from automation.supervisor.handoff_summary import safe

SCHEMA = "boundroom.operator-status/1"
DEFAULT_QUEUE = "automation/queue/slices.json"
# The plugin manifest the release check reads first (Tools/check_plugin_version.py). A consumer's
# synced copy has no plugin directory, so there the sync pin is what identifies the copy.
VERSION_FILE = "plugins/boundroom/plugin.json"
# The consumer's lock, then the name a consumer that adopted before 0.3.0 keeps (SPEC-005).
LOCK_FILES = ("automation/boundroom.lock", "automation/repo-automation.lock")
LOCK_FIELDS = ("commit", "remote", "repository", "imported_at")
# Refusals that mean the report itself is incomplete, rather than that run_next would stop.
INCOMPLETE = frozenset({"queue_unreadable", "board_unreadable", "git_unavailable"})
EX_INCOMPLETE = 1


def build_parser() -> argparse.ArgumentParser:
    """The command line, shared with the settings registry's completeness tests (SPEC-008)."""
    parser = argparse.ArgumentParser(
        description="Report the next slice, refusals, sync pin and provenance without running anything. Writes nothing."
    )
    parser.add_argument("--queue", default=DEFAULT_QUEUE, help="Path to the live queue JSON file, from the repository root.")
    parser.add_argument("--board-dir", type=Path, help="Take slices from this Kanban board directory instead of --queue.")
    parser.add_argument("--overlay", type=Path, help="The approval overlay for --board-dir.")
    parser.add_argument("--json", action="store_true", help="Print one versioned JSON object instead of a summary.")
    return parser


class _Reader:
    """Records every file the report reads, for its provenance."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.paths: list[str] = []

    def display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def read(self, path: Path) -> None:
        if not path.exists():
            return
        shown = self.display(path)
        if shown not in self.paths:
            self.paths.append(shown)


def read_version(repo_root: Path, reader: _Reader) -> dict[str, Any]:
    path = repo_root / VERSION_FILE
    if not path.is_file():
        return {"value": None, "source": None}
    reader.read(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"value": None, "source": VERSION_FILE, "error": "no readable version"}
    return {"value": value if isinstance(value, str) else None, "source": VERSION_FILE}


def parse_lock(text: str) -> dict[str, Any]:
    """The lock's fields: a JSON object, or ``key = value`` / ``key: value`` lines."""
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict):
        return document
    fields: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        separator = min((index for index in (line.find("="), line.find(":")) if index > 0), default=-1)
        if separator > 0:
            fields[line[:separator].strip()] = line[separator + 1 :].strip().strip("\"'")
    return fields


def read_sync_pin(repo_root: Path, reader: _Reader) -> Optional[dict[str, Any]]:
    """The consumer's sync pin, or None when the repository keeps no lock (as Boundroom itself)."""
    for name in LOCK_FILES:
        path = repo_root / name
        if not path.is_file():
            continue
        reader.read(path)
        pin: dict[str, Any] = {"path": name, "legacy": name != LOCK_FILES[0]}
        try:
            fields = parse_lock(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            fields = {}
            pin["error"] = f"unreadable lock: {error}"
        for field in LOCK_FIELDS:
            value = fields.get(field)
            pin[field] = value if isinstance(value, str) and value else None
        if pin["commit"] is None and "error" not in pin:
            pin["error"] = "the lock names no commit"
        return pin
    return None


def _refusal(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def load_work(
    repo_root: Path,
    queue: str,
    board_dir: Optional[Path],
    overlay: Optional[Path],
    reader: _Reader,
    refusals: list[dict[str, str]],
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Where slices come from, and the queue document run_next would select from."""
    if board_dir is None or overlay is None:
        path = repo_root / queue
        mode: dict[str, Any] = {"kind": "queue", "queue": reader.display(path)}
        reader.read(path)
        try:
            return mode, policy.load_queue(path)
        except (ValueError, policy.ConfigError) as error:  # load_queue reports an invalid queue as ValueError
            refusals.append(_refusal("queue_unreadable", str(error)))
            return mode, None

    mode = {"kind": "board", "board_dir": str(board_dir), "overlay": str(overlay)}
    if run_next.board_inside_repository(board_dir, repo_root):
        refusals.append(
            _refusal("board_inside_repository", f"the board {board_dir} is inside the repository; keep boards outside it")
        )
    try:
        reader.read(overlay)
        loaded = kanban_projection.load_overlay(overlay)
        mode.update(backend=loaded.backend, board=loaded.board)
        database = local_board.DB_NAME if loaded.backend == "local" else "kanban.db"
        reader.read(board_dir / database)
        projection = kanban_projection.project(kanban_projection.read_snapshot(board_dir, loaded.backend), loaded)
    except (ValueError, policy.ConfigError) as error:  # ProjectionError is a ValueError
        refusals.append(_refusal("board_unreadable", str(error)))
        return mode, None
    mode["revision"] = projection.revision
    return mode, projection.queue


def _slice_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "slice_id": record["slice_id"],
        "title": record["title"],
        "priority": record["priority"],
        "allowed_paths": record["allowed_paths"],
        "required_validations": record["required_validations"],
    }


def collect(
    repo_root: Path, queue: str = DEFAULT_QUEUE, board_dir: Optional[Path] = None, overlay: Optional[Path] = None
) -> dict[str, Any]:
    """The operator status document. Reads files and runs read-only git; writes nothing."""
    reader = _Reader(repo_root)
    refusals: list[dict[str, str]] = []
    version = read_version(repo_root, reader)
    sync_pin = read_sync_pin(repo_root, reader)
    mode, queue_data = load_work(repo_root, queue, board_dir, overlay, reader, refusals)

    head: Optional[str] = None
    dirty: Optional[list[str]] = None
    try:
        head = policy.git_head(repo_root)
        dirty = policy.git_dirty_paths(repo_root)
    except policy.ConfigError as error:
        refusals.append(_refusal("git_unavailable", str(error)))

    next_slice: Optional[dict[str, Any]] = None
    blocked: list[dict[str, str]] = []
    waiting: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    if queue_data is not None:
        for record in queue_data["slices"]:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        active = policy.active_slice(queue_data)
        if active is not None:
            refusals.append(_refusal("active_slice", f"queue already has in_progress slice {active['slice_id']}"))
        selected = policy.select_next_slice(queue_data)
        if selected is None:
            refusals.append(_refusal("no_eligible_slice", "no eligible queued slice found."))
        else:
            next_slice = _slice_summary(selected)
            if dirty is not None:
                outside = policy.out_of_scope_paths(
                    dirty, selected["allowed_paths"], queue_data["policy"]["supervisor_owned_paths"]
                )
                if outside:
                    refusals.append(
                        _refusal("dirty_outside_scope", "repo is dirty outside the next slice scope: " + ", ".join(outside))
                    )
        if not queue_data["policy"]["agent_command_template"].strip():
            refusals.append(
                _refusal(
                    "no_agent_command",
                    "no agent command template configured. Pass --agent-cmd or set policy.agent_command_template.",
                )
            )
        blocked = [
            {
                "slice_id": report.slice_id,
                "status": report.status,
                "entry_condition": report.entry_condition,
                "recommended_unblocker": report.recommended_unblocker,
            }
            for report in policy.blocked_slice_reports(queue_data)
        ]
        for record in queue_data["slices"]:
            if record["status"] == "queued" and not policy.dependencies_satisfied(record, queue_data):
                unmet = [
                    dependency
                    for dependency in record["depends_on"]
                    if (policy.find_slice(queue_data, dependency) or {}).get("status") != "done"
                ]
                waiting.append({"slice_id": record["slice_id"], "waiting_on": unmet})

    return {
        "schema": SCHEMA,
        "read_only": True,
        "boundroom_version": version,
        "sync_pin": sync_pin,
        "mode": mode,
        "slice_counts": dict(sorted(counts.items())),
        "next_eligible_slice": next_slice,
        "blocked_slices": blocked,
        "waiting_on_dependencies": waiting,
        "refusals": refusals,
        "provenance": {
            "repo_root": str(repo_root),
            "git_head": head,
            "worktree_clean": None if dirty is None else not dirty,
            "dirty_paths": dirty,
            "files_read": reader.paths,
        },
    }


def render_text(status: dict[str, Any]) -> str:
    """A short summary for a person. Every value from a file or board is escaped for the terminal."""
    lines = ["Boundroom operator status (read-only)"]
    version = status["boundroom_version"]
    lines.append(
        f"version: {safe(version['value'])} ({version['source']})"
        if version["value"]
        else "version: unknown (no plugin manifest)"
    )
    pin = status["sync_pin"]
    if pin is None:
        lines.append(f"sync pin: none (no {' or '.join(LOCK_FILES)})")
    else:
        shown = ", ".join(f"{field} {safe(pin[field])}" for field in LOCK_FIELDS if pin[field])
        lines.append(f"sync pin: {pin['path']}{' (legacy name)' if pin['legacy'] else ''}: {shown or 'no fields'}")
        if pin.get("error"):
            lines.append(f"  problem: {safe(pin['error'])}")
    mode = status["mode"]
    if mode["kind"] == "queue":
        lines.append(f"mode: JSON queue {safe(mode['queue'])}")
    else:
        lines.append(
            f"mode: {safe(mode.get('backend', 'unknown'))} board {safe(mode['board_dir'])}, overlay {safe(mode['overlay'])}"
        )
    counts = status["slice_counts"]
    if counts:
        lines.append("slices: " + ", ".join(f"{count} {name}" for name, count in counts.items()))
    selected = status["next_eligible_slice"]
    lines.append(
        f"next eligible slice: {safe(selected['slice_id'])}: {safe(selected['title'])}"
        if selected
        else "next eligible slice: none"
    )
    if status["blocked_slices"]:
        lines.append("blocked/deferred slices:")
        for blocked in status["blocked_slices"]:
            lines.append(f"- {blocked['status']}: {safe(blocked['slice_id'])}")
            lines.append(f"  missing entry condition: {safe(blocked['entry_condition'])}")
            lines.append(f"  recommended unblocker: {safe(blocked['recommended_unblocker']) or '<none recorded>'}")
    for waiting in status["waiting_on_dependencies"]:
        lines.append(f"waiting: {safe(waiting['slice_id'])} on {', '.join(safe(item) for item in waiting['waiting_on'])}")
    if status["refusals"]:
        lines.append("run_next would refuse:")
        for refusal in status["refusals"]:
            first, *rest = refusal["message"].split("\n")
            lines.append(f"- {refusal['code']}: {safe(first)}")
            lines.extend(f"  {safe(line)}" for line in rest)
    else:
        lines.append("run_next would refuse: nothing")
    provenance = status["provenance"]
    clean = provenance["worktree_clean"]
    state = "unknown" if clean is None else "clean" if clean else f"dirty ({len(provenance['dirty_paths'])} paths)"
    lines.append(f"provenance: HEAD {provenance['git_head'] or 'unknown'}, worktree {state}")
    lines.append("read: " + (", ".join(safe(path) for path in provenance["files_read"]) or "nothing"))
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.board_dir is None) != (args.overlay is None):
        parser.error("--board-dir and --overlay go together")
    status = collect(REPO_ROOT, args.queue, args.board_dir, args.overlay)
    if args.json:
        # ensure_ascii escapes every control character, so board or queue text can't drive the terminal.
        print(json.dumps(status, indent=2, ensure_ascii=True))
    else:
        print(render_text(status), end="")
    return EX_INCOMPLETE if any(refusal["code"] in INCOMPLETE for refusal in status["refusals"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
