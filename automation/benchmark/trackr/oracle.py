"""Score a Track R session's final commit with no agent present (SPEC-012, the independent oracle).

In a fresh clone at the session's final commit, the oracle:

1. runs every one of the task's ``required_validations``, not only the two the supervisor replays;
2. copies the hidden tests from the task's ``head_sha`` over the clone's copies and runs them;
3. computes ``git diff --numstat setup..final``, the paths outside the task's ``allowed_paths``
   (``automation.supervisor.policy.out_of_scope_paths``, with the example queue's supervisor-owned
   paths) and whether the changed-file count is within ``max_files_changed``.

Its inputs are the task file, the final commit's tree and the mirror's ``head_sha``. Nothing the
agent wrote about its work counts: not the handoff, not its log, not its claims. The clone is
made from the session clone's two refs, so the agent's Git hooks and config never come along.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from automation.supervisor import policy

from .common import (
    PYTEST,
    SUPERVISOR_OWNED_PATHS,
    UV_SYNC,
    CommandRunner,
    TrackRError,
    git,
    install_hidden_tests,
    isolated_environment,
    hidden_test_result,
    numstat,
    read_json,
    scope_errors,
    subprocess_runner,
    tail,
    task_id,
)

VALIDATION_TIMEOUT_SECONDS = 1800
SYNC_TIMEOUT_SECONDS = 900
PYTEST_TIMEOUT_SECONDS = 900
# Files that can change how the hidden tests are collected or whether they run at all.
TEST_HARNESS_FILES = ("automation/tests/conftest.py", "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py")


def scope_and_budget(task: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Out-of-scope paths and diff-budget status for a list of changed files, as the gates would see them."""
    paths = [str(row["path"]) for row in rows]
    outside = policy.out_of_scope_paths(paths, list(task["allowed_paths"]), list(SUPERVISOR_OWNED_PATHS))
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
    session_clone: Path,
    setup_sha: str,
    final_sha: str,
    mirror: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """The oracle's verdict on one session."""
    errors = scope_errors(task)
    if errors:
        raise TrackRError(f"task {task_id(task)} has no scope yet: " + "; ".join(errors))
    with tempfile.TemporaryDirectory(prefix="trackr-oracle-", dir=work_dir) as scratch:
        root = Path(scratch)
        env = isolated_environment(environ, root / "private")
        clone = root / "repo"
        git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", str(session_clone), str(clone)], root, env)
        git(["checkout", "--quiet", "--detach", final_sha], clone, env)
        for remote in git(["remote"], clone, env).split():
            git(["remote", "remove", remote], clone, env)
        rows = numstat(clone, setup_sha, final_sha, env)
        result: dict[str, Any] = {
            "task": task_id(task),
            "setup_sha": setup_sha,
            "final_sha": final_sha,
            "numstat": rows,
            **scope_and_budget(task, rows),
            "test_harness_changed": sorted(str(row["path"]) for row in rows if row["path"] in TEST_HARNESS_FILES),
        }
        synced = runner(list(UV_SYNC), clone, env, SYNC_TIMEOUT_SECONDS)
        result["uv_sync_exit_code"] = synced.returncode
        if synced.returncode != 0:
            result.update(
                validations=[],
                validations_passed=False,
                hidden_tests={"passed_all": False, "error": "uv sync --frozen failed", "output_tail": tail(synced.stderr, 15)},
                hidden_tests_pass=False,
            )
            return result
        validations = []
        for command in task["required_validations"]:
            completed = runner(["/bin/sh", "-c", command], clone, env, VALIDATION_TIMEOUT_SECONDS)
            validations.append(
                {
                    "command": command,
                    "exit_code": completed.returncode,
                    "timed_out": completed.timed_out,
                    "passed": completed.returncode == 0,
                    "output_tail": tail(completed.stdout + completed.stderr, 15),
                }
            )
        result["validations"] = validations
        result["validations_passed"] = all(item["passed"] for item in validations)
        hidden = list(task["hidden_tests"])
        install_hidden_tests(mirror, str(task["head_sha"]), hidden, clone, env)
        tests = hidden_test_result(runner([*PYTEST, *hidden], clone, env, PYTEST_TIMEOUT_SECONDS))
        tests["output_tail"] = tail(tests["output_tail"], 15)
        result["hidden_tests"] = tests
        result["hidden_tests_pass"] = tests["passed_all"]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score one Track R session's final commit (SPEC-012). Starts no model.")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True, help="The session's record.json from the driver.")
    parser.add_argument("--mirror", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    import os

    args = build_parser().parse_args(argv)
    record = read_json(args.record)
    task = next(task for task in read_json(args.tasks)["tasks"] if task_id(task) == record["task"])
    verdict = evaluate(
        task, Path(record["paths"]["clone"]), record["setup_sha"], record["final_sha"], args.mirror, environ=os.environ
    )
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
