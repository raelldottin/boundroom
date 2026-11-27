"""How many lanes a queue would run at once, at each lane ceiling (SPEC-014 "How often this helps").

Usage::

    python3 Tools/lane_admission_report.py [--queue automation/examples/example-slices.json] [--max-ceiling 8]

It replays the queue with ``automation/supervisor/lane_admission.admit``, the rule the lane
controller will use, and assumes every lane ends ``done``. A slice the queue marks
``in_progress`` is a lane already running. Each step admits what the rule allows, then the oldest
running lane finishes, in the order the lanes started. The report prints, for each ceiling, how
many lanes were running at each step. Slices that are blocked, deferred or failed, or that wait on
one, never become eligible and are listed as never admitted.

The autonomous budget is left out: it bounds one run, and the question here is how much of the
queue could run side by side at all. The script reads the queue file and prints to stdout. It
runs nothing and writes nothing.

Exit codes: 0 when the report was printed; 2 when the queue is missing, unreadable or invalid.
"""

from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import lane_admission, policy  # noqa: E402

DEFAULT_QUEUE = REPO_ROOT / "automation/examples/example-slices.json"
# SPEC-014 caps a queue's max_lanes at 8. The schema gains the field in RA-LANE-005.
SPEC_MAX_LANES = 8


@dataclass(frozen=True)
class Replay:
    """One ceiling's replay: the lanes running at each step, and the slices never admitted."""

    ceiling: int
    lanes_per_step: tuple[int, ...]
    never_admitted: tuple[str, ...]


def replay(queue_data: dict[str, Any], ceiling: int) -> Replay:
    queue = copy.deepcopy(queue_data)
    limits = lane_admission.Limits(ceiling=ceiling, budget=len(queue["slices"]))
    by_id = {slice_record["slice_id"]: slice_record for slice_record in queue["slices"]}
    running = lane_admission.lanes_in_flight(queue, [])
    lanes_per_step: list[int] = []
    started = {lane.slice_id for lane in running}
    while True:
        admission = lane_admission.admit(queue, running, limits)
        running.extend(lane_admission.Lane.for_slice(by_id[slice_id]) for slice_id in admission.start)
        started.update(admission.start)
        if not running:
            break
        lanes_per_step.append(len(running))
        finished = running.pop(0)
        by_id[finished.slice_id]["status"] = "done"
    never = tuple(
        slice_record["slice_id"]
        for slice_record in queue_data["slices"]
        if slice_record["status"] != "done" and slice_record["slice_id"] not in started
    )
    return Replay(ceiling=ceiling, lanes_per_step=tuple(lanes_per_step), never_admitted=never)


def render(queue_label: str, queue_data: dict[str, Any], replays: Sequence[Replay]) -> str:
    statuses: dict[str, int] = {}
    for slice_record in queue_data["slices"]:
        statuses[slice_record["status"]] = statuses.get(slice_record["status"], 0) + 1
    counts = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
    lines = [
        f"Queue: {queue_label} ({len(queue_data['slices'])} slices: {counts or 'none'})",
        "",
        "| Ceiling | Steps | Most lanes at once | Lanes running at each step |",
        "| --- | --- | --- | --- |",
    ]
    for result in replays:
        per_step = " ".join(str(count) for count in result.lanes_per_step) or "-"
        most = max(result.lanes_per_step, default=0)
        lines.append(f"| {result.ceiling} | {len(result.lanes_per_step)} | {most} | {per_step} |")
    never = replays[0].never_admitted if replays else ()
    lines.append("")
    lines.append(f"Never admitted: {', '.join(never) if never else 'none'}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="How many lanes a queue would run at once, at each lane ceiling.")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="the slice queue to replay")
    parser.add_argument(
        "--max-ceiling",
        type=int,
        choices=range(1, SPEC_MAX_LANES + 1),
        default=SPEC_MAX_LANES,
        metavar=f"1..{SPEC_MAX_LANES}",
        help="the highest ceiling to replay",
    )
    args = parser.parse_args(argv)
    try:
        queue_data = policy.load_queue(args.queue)
    except (policy.ConfigError, ValueError, OSError) as error:
        print(f"lane_admission_report: {error}", file=sys.stderr)
        return 2
    replays = [replay(queue_data, ceiling) for ceiling in range(1, args.max_ceiling + 1)]
    resolved = args.queue.resolve()
    label = str(resolved.relative_to(REPO_ROOT)) if resolved.is_relative_to(REPO_ROOT) else str(args.queue)
    sys.stdout.write(render(label, queue_data, replays))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
