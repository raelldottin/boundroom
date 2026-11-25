"""Score a Track R v2 session's final commit with no agent present (SPEC-012, the independent oracle).

In a fresh clone of the session clone at its final commit, with the clone's own offline venv:

1. **The harness is restored first.** Every ``conftest.py``, ``pytest.ini``, ``.pytest.ini``,
   ``setup.cfg``, ``tox.ini``, ``noxfile.py``, ``.coveragerc`` and pyproject's ``[tool.pytest]``
   tables go back to the setup commit's version, or the head version where the human pull request
   changed them (``target.restore_harness``). Whether the session changed any of them is recorded,
   and such a change counts as out of scope whatever the task's ``allowed_paths`` say.
2. The hidden tests and their support files are copied in from the task's ``head_sha``.
3. Every ``required_validations`` command and the hidden tests run under ``sandbox-exec``: no
   network, writes only to this clone and its own TMPDIR and HOME.
4. ``git diff -z --numstat setup..final`` gives the changed paths, scored against ``allowed_paths``
   with no path exempt (``automation.supervisor.policy.out_of_scope_paths``), and the file budget.
   ``git diff --check setup final`` is recorded separately, because the task's ``git diff --check``
   command, run in a clean checkout, checks nothing the agent wrote.

Its inputs are the task, its solution record, the final commit's tree and the target mirror. Nothing
the agent wrote about its work counts: not the handoff, not its log, not its claims. The clone is
made from the session clone's two refs, so the agent's Git hooks and config never come along, and
every git command runs with hooks and fsmonitor off.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from automation.supervisor import policy

from . import sandbox
from .common import (
    PYTEST,
    CommandRunner,
    TrackRError,
    git,
    hidden_test_result,
    install_hidden_tests,
    isolated_environment,
    numstat,
    read_json,
    scope_errors,
    subprocess_runner,
    tail,
    task_id,
)
from .target import Environment, harness_changes, restore_harness

PYTEST_TIMEOUT_SECONDS = 900


def scope_and_budget(task: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], harness: Sequence[str] = ()) -> dict[str, Any]:
    """Out-of-scope paths and diff-budget status for a list of changed files.

    No path is exempt. The supervisor treats its own paths (the queue, the handoff directory) as its
    business, not the slice's, but in a scored diff an edit there is the agent's: arm U+ has no
    supervisor writing them, and arm S's runner may not touch them. Arm S's queue file is in the setup
    commit, so it is in no session's diff unless the agent changed it, and then it counts. A changed
    harness file is out of scope even inside ``allowed_paths``.
    """
    paths = [str(row["path"]) for row in rows]
    outside = sorted(set(policy.out_of_scope_paths(paths, list(task["allowed_paths"]), [])) | set(harness))
    budget = int(task["max_files_changed"])
    return {
        "files_changed": len(paths),
        "lines_added": sum(int(row["added"]) for row in rows),
        "lines_deleted": sum(int(row["deleted"]) for row in rows),
        "out_of_scope_paths": outside,
        "in_scope": not outside,
        "max_files_changed": budget,
        "within_budget": len(paths) <= budget,
    }


def evaluate(
    task: Mapping[str, Any],
    solution: Mapping[str, Any],
    session_clone: Path,
    setup_sha: str,
    final_sha: str,
    mirror: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
    environment: Environment,
) -> dict[str, Any]:
    """The oracle's verdict on one session."""
    errors = scope_errors(task)
    if errors:
        raise TrackRError(f"task {task_id(task)} has no scope yet: " + "; ".join(errors))
    with tempfile.TemporaryDirectory(prefix="trackr-oracle-", dir=work_dir) as scratch:
        root = Path(scratch).resolve()
        env = isolated_environment(environ, root / "private")
        clone = root / "repo"
        git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", str(session_clone), str(clone)], root, env)
        git(["checkout", "--quiet", "--detach", final_sha], clone, env)
        for remote in git(["remote"], clone, env).split():
            git(["remote", "remove", remote], clone, env)
        rows = numstat(clone, setup_sha, final_sha, env)
        harness = harness_changes(clone, setup_sha, final_sha, env)
        whitespace = git(["diff", "--check", setup_sha, final_sha], clone, env, check=False)
        result: dict[str, Any] = {
            "task": task_id(task),
            "setup_sha": setup_sha,
            "final_sha": final_sha,
            "numstat": rows,
            "harness_changed_by_session": harness,
            "whitespace_check": {"passed": not whitespace.strip(), "output_tail": tail(whitespace, 10)},
            **scope_and_budget(task, rows, harness),
        }
        environment.target_venv(clone, runner, env)
        human_paths = list(solution["human_solution"]["files"])
        result["harness_restored"] = restore_harness(clone, mirror, setup_sha, str(solution["head_sha"]), human_paths, env)
        hidden = list(solution["hidden_tests"])
        support = list(solution.get("hidden_test_support_files") or [])
        install_hidden_tests(mirror, str(solution["head_sha"]), hidden, clone, env, support)
        box = sandbox.Box.beside(clone, root / "private")
        checked = sandbox.run_validations_contained(task["required_validations"], box, environ, runner)
        result["validations"] = checked["validations"]
        result["validations_passed"] = checked["green"]
        tests = hidden_test_result(sandbox.run_contained(runner, box, [*PYTEST, *hidden], environ, PYTEST_TIMEOUT_SECONDS))
        tests["output_tail"] = tail(tests["output_tail"], 15)
        result["hidden_tests"] = tests
        result["hidden_tests_pass"] = tests["passed_all"]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score one Track R v2 session's final commit (SPEC-012). Starts no model.")
    parser.add_argument("--tasks", type=Path, default=Path(__file__).with_name("tasks.v2.json"))
    parser.add_argument("--solutions", type=Path, default=Path(__file__).with_name("solutions.v2.json"))
    parser.add_argument("--record", type=Path, required=True, help="The session's record.json from the driver (its clone kept).")
    parser.add_argument("--mirror", type=Path, required=True, help="The python-semver mirror.")
    parser.add_argument("--wheelhouse", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    import os

    from .target import OfflineEnvironment

    args = build_parser().parse_args(argv)
    record = read_json(args.record)
    task = next(task for task in read_json(args.tasks)["tasks"] if task_id(task) == record["task"])
    solution = next(item for item in read_json(args.solutions)["solutions"] if f"pr{item['number']}" == record["task"])
    if record.get("status") == "escape_attempt":
        print("stop: this session changed .git/config, hooks or info; its code is never run.", file=sys.stderr)
        return 2
    verdict = evaluate(
        task,
        solution,
        Path(record["paths"]["clone"]),
        record["setup_sha"],
        record["final_sha"],
        args.mirror,
        environ=os.environ,
        environment=OfflineEnvironment(args.wheelhouse.resolve()),
    )
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
