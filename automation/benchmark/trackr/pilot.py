"""The Track R pilot, R1: 6 tasks x 2 arms x 1 repeat, sequential (SPEC-012).

Arm order rotates per task: even-indexed tasks run U first, odd-indexed tasks run S first. Before
each session the pilot stops if what was spent so far plus the cap would exceed ``--budget-total``
(a session whose cost could not be measured counts as the full cap). After each session the
oracle scores its final commit, and its diff is exported for blind review under a random id.

A real run starts model sessions, which is spending a person must approve, so it needs ``--yes``.
``--dry-run`` replaces Claude Code with a fake that starts no model: everything else runs for real,
the clones, the setup commits, ``run_next.py`` with all its gates, the oracle and the export. With
``--fake human`` (the default) the fake applies each task's human solution, which checks the oracle
and shows what the gates do to the human solution; ``--fake noop`` changes nothing.

Run it as a module::

    python -m automation.benchmark.trackr.pilot --dry-run --out /outside/the/repo/pilot --cache-dir /outside/cache
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .common import (
    TASKS_FILE,
    UPSTREAM,
    Overlay,
    TrackRError,
    ensure_mirror,
    isolated_environment,
    read_json,
    require_outside_repository,
    scope_errors,
    task_id,
    write_json,
)
from .driver import (
    ARMS,
    DEFAULT_CAP_USD,
    DEFAULT_TIMEOUT_SECONDS,
    QUEUE_PATH,
    DriverConfig,
    claude_preflight,
    claude_version,
    cleanup_session,
    run_session,
    write_fake_claude,
)
from .oracle import evaluate, scope_and_budget

DEFAULT_BUDGET_TOTAL = 60.0
# Paths that never reach the reviewer: Claude Code settings, and the supervisor's own queue and handoffs.
REVIEW_EXCLUDED = (".claude", QUEUE_PATH, "automation/handoffs")
NEUTRAL_HEADER = (
    "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
    "From: Track R review <review@example.invalid>\n"
    "Date: Thu, 1 Jan 1970 00:00:00 +0000\n"
    "Subject: [PATCH] Proposed change\n\n---\n"
)

SessionFn = Callable[..., dict[str, Any]]
OracleFn = Callable[..., dict[str, Any]]


def arm_order(index: int) -> tuple[str, str]:
    return ("U", "S") if index % 2 == 0 else ("S", "U")


def plan(tasks: Sequence[Mapping[str, Any]]) -> list[tuple[int, Mapping[str, Any], str]]:
    return [(index, task, arm) for index, task in enumerate(tasks) for arm in arm_order(index)]


def charged(record: Mapping[str, Any], cap_usd: float) -> float:
    """What a session counts against the total: its measured cost, or the full cap when unknown."""
    if record.get("status") == "setup_failed":
        return 0.0
    cost = record.get("cost_usd")
    return float(cost) if cost is not None else cap_usd


# --- blinding -------------------------------------------------------------------------------------


def review_patch(clone: Path, setup_sha: str, final_sha: str, env: Mapping[str, str]) -> str:
    """The session's change as one patch with a neutral header: no commit message, author, handoff or .claude/."""
    excludes = [f":(exclude){path}" for path in REVIEW_EXCLUDED]
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-color", setup_sha, final_sha, "--", ".", *excludes],
        cwd=clone,
        env=dict(env),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return NEUTRAL_HEADER + (diff if diff else "(no change)\n")


def export_for_review(
    out_dir: Path, task: Mapping[str, Any], record: Mapping[str, Any], env: Mapping[str, str], rng: random.Random
) -> Optional[str]:
    """Write one session's normalized diff under a random id. Returns the id, or None with no commit to show."""
    if record.get("status") != "ran":
        return None
    review_id = "".join(rng.choice("0123456789abcdef") for _ in range(12))
    directory = out_dir / "review" / review_id
    directory.mkdir(parents=True, exist_ok=False)
    patch = review_patch(Path(record["paths"]["clone"]), record["setup_sha"], record["final_sha"], env)
    (directory / "change.patch").write_text(patch, encoding="utf-8")
    (directory / "task.md").write_text(f"# Task\n\n{str(task['task_statement']).strip()}\n", encoding="utf-8")
    return review_id


def write_review_index(out_dir: Path, mapping: Mapping[str, Mapping[str, Any]], rng: random.Random) -> None:
    """The reviewer's queue, in random order, and the arm mapping, kept apart from it."""
    ids = sorted(mapping)
    rng.shuffle(ids)
    write_json(
        out_dir / "review" / "index.json",
        {
            "instructions": "Review each change against its task. Record a verdict, the lines you would edit, minutes "
            "spent, reasons, and your guess of which arm produced it.",
            "queue": [{"id": review_id, "task": mapping[review_id]["task"]} for review_id in ids],
        },
    )
    write_json(out_dir / "mapping.json", {"note": "Keep away from the reviewer until the review is done.", "sessions": mapping})


# --- the summary ----------------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 3) if denominator else None


def arm_summary(results: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    rows = [row for row in results if row["record"]["arm"] == arm and row["record"].get("status") == "ran"]
    costs = [row["record"]["cost_usd"] for row in rows if row["record"].get("cost_usd") is not None]
    oracle = [row["oracle"] for row in rows if row.get("oracle")]
    out_of_scope = [row for row in rows if row.get("oracle") and not row["oracle"]["in_scope"]]
    over_budget = [row for row in rows if row.get("oracle") and not row["oracle"]["within_budget"]]
    presented = [row for row in rows if row["record"].get("presented_as_done")]
    bad_presented = [
        row for row in presented if row.get("oracle") and not (row["oracle"]["hidden_tests_pass"] and row["oracle"]["in_scope"])
    ]
    capped = [row for row in rows if row["record"].get("cap_hit")]
    timed_out = [row for row in rows if row["record"].get("timed_out")]
    summary: dict[str, Any] = {
        "sessions": len(rows),
        "setup_failed": sum(
            1 for row in results if row["record"]["arm"] == arm and row["record"].get("status") == "setup_failed"
        ),
        "mean_cost_usd": round(sum(costs) / len(costs), 4) if costs else None,
        "total_cost_usd": round(sum(costs), 4),
        "cost_unknown": len(rows) - len(costs),
        "cap_hit": len(capped),
        "timed_out": len(timed_out),
        "cap_or_timeout": len({id(row) for row in capped + timed_out}),
        "hidden_tests_pass": sum(1 for verdict in oracle if verdict["hidden_tests_pass"]),
        "hidden_test_pass_rate": _rate(sum(1 for verdict in oracle if verdict["hidden_tests_pass"]), len(oracle)),
        "validations_all_pass": sum(1 for verdict in oracle if verdict.get("validations_passed")),
        "out_of_scope": len(out_of_scope),
        "over_budget": len(over_budget),
        "presented_as_done": len(presented),
        "bad_presented_as_done": len(bad_presented),
    }
    if arm == "S":
        supervised = [row["record"].get("supervised") or {} for row in rows]
        summary["decisions"] = dict(Counter(str(item.get("decision")) for item in supervised))
        summary["gates"] = dict(Counter(str(item.get("gate")) for item in supervised))
        summary["caught_out_of_scope"] = sum(1 for row in out_of_scope if not row["record"].get("presented_as_done"))
        summary["caught_over_budget"] = sum(1 for row in over_budget if not row["record"].get("presented_as_done"))
        summary["slipped_through"] = len(bad_presented)
        summary["unearned_done"] = sum(1 for row in presented if row.get("oracle") and not row["oracle"]["hidden_tests_pass"])
        summary["handoff_rules"] = dict(Counter(str(item.get("handoff_rule")) for item in supervised))
        summary["supervisor_budget_args"] = dict(
            Counter(" ".join(item.get("supervisor_budget_args") or []) for item in supervised)
        )
    else:
        # What the supervisor's scope and diff-budget gates would have stopped, had U been supervised.
        summary["would_be_caught_out_of_scope"] = len(out_of_scope)
        summary["would_be_caught_over_budget"] = len(over_budget)
    return summary


def human_baseline(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Which human solutions break their own task's scope or diff budget: the gates' false-positive baseline."""
    rows = []
    for task in tasks:
        if scope_errors(task):
            rows.append({"task": task_id(task), "status": "scope not filled in"})
            continue
        verdict = scope_and_budget(task, task["human_solution"]["numstat"])
        rows.append(
            {"task": task_id(task), **{key: verdict[key] for key in ("files_changed", "out_of_scope_paths", "within_budget")}}
        )
    return rows


def stop_conditions(
    tasks: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    summaries: Mapping[str, Mapping[str, Any]],
    out_dir: Path,
) -> list[dict[str, Any]]:
    """The spec's pilot stops, each with whether it fired."""
    conditions = []
    for arm in ARMS:
        summary = summaries[arm]
        fired = summary["sessions"] > 0 and summary["cap_or_timeout"] * 2 > summary["sessions"]
        conditions.append(
            {
                "condition": f"more than half of arm {arm}'s sessions hit the cap or the timeout",
                "fired": fired,
                "detail": f"{summary['cap_or_timeout']} of {summary['sessions']}",
            }
        )
    disagreements = [
        task_id(task)
        for task in tasks
        if not (task["checks"].get("oracle_fails_at_base") and task["checks"].get("oracle_passes_at_head"))
    ]
    conditions.append(
        {
            "condition": "the oracle disagrees with the human solution on any task",
            "fired": bool(disagreements),
            "detail": ", ".join(disagreements) or "none at selection",
        }
    )
    verdicts_file = out_dir / "review" / "verdicts.json"
    mapping_file = out_dir / "mapping.json"
    if verdicts_file.exists() and mapping_file.exists():
        mapping = read_json(mapping_file)["sessions"]
        guesses = [item for item in read_json(verdicts_file) if item.get("id") in mapping and item.get("arm_guess")]
        right = sum(1 for item in guesses if item["arm_guess"] == mapping[item["id"]]["arm"])
        conditions.append(
            {
                "condition": "the reviewer guesses the arm correctly in more than 75% of cases",
                "fired": bool(guesses) and right * 4 > len(guesses) * 3,
                "detail": f"{right} of {len(guesses)}",
            }
        )
    else:
        conditions.append(
            {
                "condition": "the reviewer guesses the arm correctly in more than 75% of cases",
                "fired": None,
                "detail": "not measurable yet: needs review/verdicts.json with arm_guess per id",
            }
        )
    return conditions


def summary_table(summaries: Mapping[str, Mapping[str, Any]]) -> str:
    rows = [
        ("sessions", "sessions"),
        ("mean $/session", "mean_cost_usd"),
        ("cost unknown", "cost_unknown"),
        ("hit the cap", "cap_hit"),
        ("timed out", "timed_out"),
        ("hidden-test pass rate", "hidden_test_pass_rate"),
        ("presented as done", "presented_as_done"),
        ("presented as done but bad", "bad_presented_as_done"),
        ("out of scope (oracle)", "out_of_scope"),
        ("over the diff budget (oracle)", "over_budget"),
    ]
    lines = ["| Measure | U | S |", "|---|---:|---:|"]
    for label, key in rows:
        lines.append(f"| {label} | {summaries['U'].get(key)} | {summaries['S'].get(key)} |")
    lines.append(
        f"| S gate stopped out-of-scope / over-budget | | {summaries['S'].get('caught_out_of_scope')} / {summaries['S'].get('caught_over_budget')} |"
    )
    lines.append(
        f"| U would be stopped for out-of-scope / over-budget | {summaries['U'].get('would_be_caught_out_of_scope')} / "
        f"{summaries['U'].get('would_be_caught_over_budget')} | |"
    )
    lines.append(f"| S decisions | | {json.dumps(summaries['S'].get('decisions'))} |")
    lines.append(f"| S gates | | {json.dumps(summaries['S'].get('gates'))} |")
    lines.append(f"| S runner's handoff rule | | {json.dumps(summaries['S'].get('handoff_rules'))} |")
    lines.append(
        f"| S cap run_next asked for (replaced by the driver's) | | {json.dumps(summaries['S'].get('supervisor_budget_args'))} |"
    )
    return "\n".join(lines) + "\n"


# --- the run --------------------------------------------------------------------------------------


def run_pilot(
    tasks: Sequence[Mapping[str, Any]],
    mirror: Path,
    out_dir: Path,
    config: DriverConfig,
    *,
    budget_total: float = DEFAULT_BUDGET_TOTAL,
    session_fn: SessionFn = run_session,
    oracle_fn: OracleFn = evaluate,
    rng: Optional[random.Random] = None,
    progress: Callable[[str], None] = lambda message: None,
    keep_clones: bool = False,
) -> dict[str, Any]:
    rng = rng or random.SystemRandom()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    mapping: dict[str, dict[str, Any]] = {}
    spent = 0.0
    stopped = None
    results_file = out_dir / "results.jsonl"
    for index, task, arm in plan(tasks):
        if spent + config.cap_usd > budget_total:
            stopped = (
                f"budget: {spent:.2f} spent + {config.cap_usd:.2f} cap > {budget_total:.2f} before {task_id(task)} arm {arm}"
            )
            progress(f"stop: {stopped}")
            break
        progress(f"session {len(results) + 1}: {task_id(task)} arm {arm}")
        session_dir = out_dir / "sessions" / f"{task_id(task)}-{arm}"
        record = session_fn(task, arm, mirror, session_dir, config)
        spent += charged(record, config.cap_usd)
        verdict = None
        try:
            if record.get("status") == "ran":
                verdict = oracle_fn(
                    task,
                    Path(record["paths"]["clone"]),
                    record["setup_sha"],
                    record["final_sha"],
                    mirror,
                    runner=config.runner,
                    environ=config.environ,
                    work_dir=config.work_dir,
                )
                env = isolated_environment(config.environ, Path(record["paths"]["root"]) / "private")
                review_id = export_for_review(out_dir, task, record, env, rng)
                if review_id:
                    mapping[review_id] = {
                        "task": task_id(task),
                        "arm": arm,
                        "session_dir": str(session_dir),
                        "order": len(results),
                    }
        finally:
            if not keep_clones:
                cleanup_session(record)
        row = {"task": task_id(task), "index": index, "arm": arm, "record": record, "oracle": verdict}
        results.append(row)
        with results_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    write_review_index(out_dir, mapping, rng)
    summaries = {arm: arm_summary(results, arm) for arm in ARMS}
    summary = {
        "sessions_run": len(results),
        "spent_usd": round(spent, 4),
        "budget_total_usd": budget_total,
        "stopped": stopped,
        "arms": summaries,
        "human_solution_baseline": human_baseline(tasks),
        "stop_conditions": stop_conditions(tasks, results, summaries, out_dir),
    }
    write_json(out_dir / "summary.json", summary)
    (out_dir / "summary.md").write_text(summary_table(summaries), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Track R pilot (SPEC-012 R1). A real run is spending.")
    parser.add_argument("--tasks", type=Path, default=TASKS_FILE)
    parser.add_argument("--out", type=Path, required=True, help="Results directory, outside any repository.")
    parser.add_argument("--cache-dir", type=Path, required=True, help="Where the mirror clone lives, outside any repository.")
    parser.add_argument("--work-dir", type=Path, help="Where the disposable clones go (default: the system temporary directory).")
    parser.add_argument("--source", default=UPSTREAM)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Use the fake Claude Code: no model, no spending.")
    mode.add_argument("--yes", action="store_true", help="Start real model sessions. Needs a person's spending approval.")
    parser.add_argument("--fake", choices=("human", "noop"), default="human", help="What the fake does in a dry run.")
    parser.add_argument("--budget-total", type=float, default=DEFAULT_BUDGET_TOTAL)
    parser.add_argument("--cap-usd", type=float, default=DEFAULT_CAP_USD)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--keep-clones", action="store_true")
    parser.add_argument("--no-isolate-user-config", action="store_true", help="Let both arms load user settings and MCP servers.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = args.out.resolve()
    require_outside_repository(out_dir, "--out")
    if out_dir.exists() and any(out_dir.iterdir()):
        print(f"stop: {out_dir} is not empty; give each pilot its own directory.", file=sys.stderr)
        return 2
    if args.work_dir:
        require_outside_repository(args.work_dir, "--work-dir")
        args.work_dir.mkdir(parents=True, exist_ok=True)
    data = read_json(args.tasks)
    tasks = data["tasks"]
    overlay = Overlay.from_record(data.get("overlay"))
    if overlay is None:
        print(
            "stop: the task file records no overlay; its oracle check wasn't made with the supervisor under test.",
            file=sys.stderr,
        )
        return 2
    missing = {task_id(task): scope_errors(task) for task in tasks if scope_errors(task)}
    if missing:
        for name, errors in missing.items():
            print(f"stop: {name}: {'; '.join(errors)}", file=sys.stderr)
        print("stop: a person fills in each task's scope from its description alone before the pilot runs.", file=sys.stderr)
        return 2
    config = DriverConfig(
        cap_usd=args.cap_usd,
        timeout_seconds=args.timeout,
        claude_bin=args.claude_bin,
        work_dir=args.work_dir,
        isolate_user_config=not args.no_isolate_user_config,
        overlay=overlay,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        config.claude_bin = str(write_fake_claude(out_dir / "fake-claude-bin", args.fake))
        config.stage_human_patch = args.fake == "human"
    env = isolated_environment(os.environ, out_dir / "private")
    missing_flags = claude_preflight(config.runner, config.claude_bin, env, out_dir)
    if missing_flags:
        print(f"stop: {config.claude_bin} --help doesn't list {', '.join(missing_flags)}", file=sys.stderr)
        return 2
    mirror = ensure_mirror(args.source, args.cache_dir, env)
    write_json(
        out_dir / "pilot.json",
        {
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": f"dry run, fake {args.fake}" if args.dry_run else "real",
            "claude_bin": config.claude_bin,
            "claude_code_version": claude_version(config.runner, config.claude_bin, env, out_dir),
            "cap_usd": config.cap_usd,
            "timeout_seconds": config.timeout_seconds,
            "budget_total_usd": args.budget_total,
            "isolate_user_config": config.isolate_user_config,
            "overlay": overlay.record(),
            "tasks": [task_id(task) for task in tasks],
            "plan": [{"task": task_id(task), "arm": arm} for _, task, arm in plan(tasks)],
        },
    )
    summary = run_pilot(
        tasks,
        mirror,
        out_dir,
        config,
        budget_total=args.budget_total,
        progress=lambda message: print(message, file=sys.stderr),
        keep_clones=args.keep_clones,
    )
    print((out_dir / "summary.md").read_text(encoding="utf-8"))
    print(json.dumps({"stopped": summary["stopped"], "stop_conditions": summary["stop_conditions"]}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TrackRError as error:
        print(f"stop: {error}", file=sys.stderr)
        sys.exit(2)
