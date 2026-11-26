"""Freeze the Track R v2 task set: python-semver pull requests, chosen by the owner (SPEC-012).

The owner chose the target and the candidates on 2026-11-25: six python-semver pull requests merged
after the model's training cutoff, found by a research agent (#516, #508, #507, #489, #485, #469).
This rule doesn't widen that pool. Each candidate, in merge order, is kept when:

1. its files include at least one ``tests/test_*.py``;
2. it changed 15 or fewer files;
3. it is not documentation only;
4. its merge commit is one squash commit whose diff from its first parent is the pull request's
   file list, so ``base_sha`` (the first parent) and ``head_sha`` (the merge commit) bracket it;
5. the oracle agrees, under ``sandbox-exec``: with the setup applied (Boundroom synced in, the
   offline venv) and the harness restored, the hidden tests (the test files it added or changed,
   with their support files) fail or error at base and pass at head;
6. the base is green, under ``sandbox-exec``: every required check passes at the setup commit.

The task file holds only what a blinded scope writer may read: the linked issue, or the pull
request's title where there is none, and the base. The solution (head, files, numstat, hidden tests)
goes into a separate ``solutions.v2.json``, which scope writers never see. Every candidate's checks
and the reason it was kept or rejected go into the task file's ``audit``.

    python -m automation.benchmark.trackr.select --cache-dir /outside/cache --work-dir /outside/work --wheelhouse /outside/wheels

It reads GitHub (``gh pr view``, ``gh issue view`` and mirror clones), runs the release's own sync
once, and starts no model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import sandbox
from .common import (
    SUPERVISOR_LABEL,
    SUPERVISOR_RELEASE,
    DEFAULT_REQUIRED_VALIDATIONS,
    MODEL,
    PYTEST,
    REPO_ROOT,
    SCOPE_PROVENANCE_FIELDS,
    SOLUTIONS_FILE,
    TARGET_REPOSITORY,
    TARGET_UPSTREAM,
    TASKS_FILE,
    TEST_FILE,
    CommandRunner,
    TrackRError,
    clone_at,
    git,
    has_object,
    hidden_test_result,
    install_hidden_tests,
    isolated_environment,
    name_status,
    numstat,
    read_json,
    scope_errors,
    subprocess_runner,
    tail,
    write_json,
)
from .target import SYNC_OMIT, Environment, SyncStage, apply_sync, is_harness, restore_harness

OWNER_CANDIDATES = (516, 508, 507, 489, 485, 469)
MAX_FILES_CHANGED = 15
TASK_COUNT = 6
PYTEST_TIMEOUT_SECONDS = 900
GH_FIELDS = "number,title,body,mergedAt,mergeCommit,files,closingIssuesReferences,url"

SELECTION_RULE = {
    "task_set": "v2: python-semver pull requests merged after the model's training cutoff",
    "candidates": list(OWNER_CANDIDATES),
    "candidates_chosen_by": "the owner, from a research agent's list (2026-11-25); the rule does not widen the pool",
    "order": "mergedAt, then number",
    "keep_if": [
        "files include at least one tests/test_*.py",
        f"{MAX_FILES_CHANGED} or fewer files changed",
        "not documentation only (some file is neither under docs/ nor a .rst/.md file nor under changelog.d/)",
        "the merge commit's diff from its first parent is exactly the pull request's file list",
        "under sandbox-exec, with the setup applied and the harness restored: hidden tests fail or error at "
        "base+setup and pass at head+setup",
        "under sandbox-exec: every required check passes at the setup commit (green base)",
    ],
    "setup": f"Boundroom {SUPERVISOR_RELEASE} ({SUPERVISOR_LABEL}) synced in by its own Tools/boundroom-sync.sh "
    f"(--omit {' --omit '.join(SYNC_OMIT)}), plus an offline .venv from wheelhouse.v2.json",
    "base_sha": "the first parent of the squash merge commit",
    "head_sha": "the squash merge commit",
    "hidden_tests": "tests/test_*.py files the pull request added or changed; other files it changed under tests/ "
    "are installed with them as support files",
    "oracle": "copy the hidden tests and support files from head_sha into the clone, restore the harness, then, "
    "under sandbox-exec: " + " ".join(PYTEST) + " <hidden tests>. Fails at base: pytest exits 1 or 2. Passes at head: "
    "pytest exits 0 having run at least one test.",
    "green_base_checks_without_a_scope": list(DEFAULT_REQUIRED_VALIDATIONS),
    "count": TASK_COUNT,
}

SCOPE_WRITERS = (
    "Scopes are written by blinded agents, one per task, by the owner's decision (2026-11-25), not by a person. "
    "Each reads only the task's linked issue, or the pull request's title where no issue is linked, never the "
    "solution or solutions.v2.json, and fills in task_statement, allowed_paths, required_validations and "
    "max_files_changed. The checks available in the target are: " + "; ".join(DEFAULT_REQUIRED_VALIDATIONS) + ". "
    "The writer records by, when, inputs (saying no diff was seen) and leakage_ignored in scope_provenance."
)

SCOPE_TEMPLATE: dict[str, Any] = {
    "task_statement": "",
    "allowed_paths": [],
    "required_validations": [],
    "max_files_changed": None,
    "scope_provenance": {name: "" for name in SCOPE_PROVENANCE_FIELDS},
}


def fetch(number: int) -> dict[str, Any]:
    """One merged pull request, and its linked issue if any, from ``gh`` (read-only)."""
    pr = json.loads(_gh(["pr", "view", str(number), "--repo", TARGET_REPOSITORY, "--json", GH_FIELDS]))
    issue_number, link = linked_issue(pr)
    if issue_number:
        pr["linked_issue"] = json.loads(
            _gh(["issue", "view", str(issue_number), "--repo", TARGET_REPOSITORY, "--json", "number,title,body,url"])
        ) | {"link": link}
    return pr


def _gh(args: Sequence[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise TrackRError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def linked_issue(pr: Mapping[str, Any]) -> tuple[Optional[int], str]:
    """The issue a pull request resolves: GitHub's closing reference, else ``Fix #N`` in its title, else
    ``Relates to #N`` in its body."""
    closing = pr.get("closingIssuesReferences") or []
    if closing:
        return int(closing[0]["number"]), "GitHub closing reference"
    title = re.search(r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)", pr.get("title") or "", re.I)
    if title:
        return int(title.group(1)), "the pull request's title"
    body = re.search(r"\brelates? to #(\d+)", pr.get("body") or "", re.I)
    if body:
        return int(body.group(1)), "'Relates to' in the pull request's body"
    return None, ""


def documentation_only(paths: Sequence[str]) -> bool:
    return bool(paths) and all(path.startswith(("docs/", "changelog.d/")) or path.endswith((".rst", ".md")) for path in paths)


def static_checks(pr: Mapping[str, Any], mirror: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Every check but the sandboxed ones, all of them computed so the audit shows each one."""
    head = (pr.get("mergeCommit") or {}).get("oid") or ""
    record: dict[str, Any] = {"head_sha": head, "base_sha": "", "checks": {}, "reasons": []}
    if not head or not has_object(mirror, head, env):
        record["reasons"].append("its merge commit is not in the repository's history")
        record["checks"]["merge_commit_present"] = False
        return record
    parents = git(["rev-list", "--parents", "-n", "1", head], mirror, env).split()[1:]
    record["base_sha"] = parents[0] if parents else ""
    statuses = name_status(mirror, record["base_sha"], head, env)
    paths = [path for _, path in statuses]
    pr_paths = sorted(file["path"] for file in pr.get("files") or [])
    hidden = [path for status, path in statuses if status != "D" and TEST_FILE.match(path)]
    support = [path for status, path in statuses if status != "D" and path.startswith("tests/") and not TEST_FILE.match(path)]
    rows = numstat(mirror, record["base_sha"], head, env)
    record.update(
        hidden_tests=hidden,
        hidden_test_support_files=support,
        human_solution={
            "files": paths,
            "numstat": rows,
            "files_changed": len(paths),
            "lines_added": sum(row["added"] for row in rows),
            "lines_deleted": sum(row["deleted"] for row in rows),
            "harness_files": [path for path in paths if is_harness(path)],
        },
    )
    checks = {
        "merge_commit_present": True,
        "single_parent": len(parents) == 1,
        "has_test_file": bool(hidden),
        "files_changed_at_most_15": len(paths) <= MAX_FILES_CHANGED,
        "not_documentation_only": not documentation_only(paths),
        "squash_matches_pull_request": sorted(paths) == pr_paths,
    }
    record["checks"] = checks
    reasons = record["reasons"]
    if not checks["single_parent"]:
        reasons.append(f"its merge commit has {len(parents)} parents, not one")
    if not checks["has_test_file"]:
        reasons.append("no tests/test_*.py file added or changed")
    if not checks["files_changed_at_most_15"]:
        reasons.append(f"{len(paths)} files changed, more than {MAX_FILES_CHANGED}")
    if not checks["not_documentation_only"]:
        reasons.append("documentation only")
    if not checks["squash_matches_pull_request"]:
        reasons.append("the merge commit's diff is not the pull request's file list (not one squash commit)")
    return record


def setup_clone(
    mirror: Path,
    sha: str,
    dest: Path,
    stage: SyncStage,
    environment: Environment,
    supervisor_python: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> str:
    """A clone at ``sha`` with the sessions' setup committed (venv ignored, sync in). Returns the setup sha."""
    clone = clone_at(mirror, sha, dest, env)
    (clone / ".git" / "info" / "exclude").open("a").write("\n/.venv/\n")
    environment.target_venv(clone, runner, env)
    apply_sync(stage, clone, runner, {**env, "TRACKR_SUPERVISOR_PYTHON": str(supervisor_python)})
    git(["add", "-A"], clone, env)
    git(["commit", "--quiet", "--allow-empty", "-m", "Track R setup"], clone, env)
    return git(["rev-parse", "HEAD"], clone, env).strip()


def oracle_check(
    mirror: Path,
    static: Mapping[str, Any],
    stage: SyncStage,
    environment: Environment,
    supervisor_python: Path,
    *,
    runner: CommandRunner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Under the sandbox: the hidden tests at base+setup (expected to fail) and head+setup (expected to pass)."""
    result: dict[str, Any] = {}
    head = str(static["head_sha"])
    with tempfile.TemporaryDirectory(prefix="trackr-select-", dir=work_dir) as scratch:
        root = Path(scratch).resolve()
        env = isolated_environment(environ, root / "private")
        for label, sha in (("base", static["base_sha"]), ("head", head)):
            clone = root / label
            setup = setup_clone(mirror, sha, clone, stage, environment, supervisor_python, runner, env)
            restored = restore_harness(clone, mirror, setup, head, static["human_solution"]["files"], env)
            install_hidden_tests(mirror, head, static["hidden_tests"], clone, env, static["hidden_test_support_files"])
            box = sandbox.Box.beside(clone, root / f"private-{label}")
            outcome = hidden_test_result(
                sandbox.run_contained(runner, box, [*PYTEST, *static["hidden_tests"]], environ, PYTEST_TIMEOUT_SECONDS)
            )
            outcome["output_tail"] = tail(outcome["output_tail"], 15 if label == "base" else 4)
            result[label] = {"setup_sha": setup, "harness_restored": restored, "contained": True, **outcome}
    result["fails_at_base"] = bool(result["base"]["failed_any"])
    result["passes_at_head"] = bool(result["head"]["passed_all"])
    return result


def green_base_check(
    mirror: Path,
    base_sha: str,
    validations: Sequence[str],
    stage: SyncStage,
    environment: Environment,
    supervisor_python: Path,
    *,
    runner: CommandRunner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Every required check at base+setup, under the sandbox, before anything else changes."""
    with tempfile.TemporaryDirectory(prefix="trackr-green-", dir=work_dir) as scratch:
        root = Path(scratch).resolve()
        env = isolated_environment(environ, root / "private")
        clone = root / "base"
        setup = setup_clone(mirror, base_sha, clone, stage, environment, supervisor_python, runner, env)
        box = sandbox.Box.beside(clone, root / "private-box")
        return {"setup_sha": setup, **sandbox.run_validations_contained(validations, box, environ, runner)}


def select_tasks(
    pull_requests: Sequence[Mapping[str, Any]],
    mirror: Path,
    stage: SyncStage,
    environment: Environment,
    supervisor_python: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    environ: Mapping[str, str] = os.environ,
    work_dir: Optional[Path] = None,
    count: int = TASK_COUNT,
    scopes: Optional[Mapping[int, Mapping[str, Any]]] = None,
    progress: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The tasks, their solutions, and the audit of every candidate, in merge order."""
    scopes = scopes or {}
    tasks: list[dict[str, Any]] = []
    solutions: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="trackr-select-env-", dir=work_dir) as scratch:
        env = isolated_environment(environ, Path(scratch))
        for pr in sorted(pull_requests, key=lambda item: (item["mergedAt"], item["number"])):
            entry: dict[str, Any] = {"number": pr["number"], "title": pr["title"], "merged_at": pr["mergedAt"]}
            if len(tasks) >= count:
                entry.update(decision="not_examined", reasons=[f"the first {count} tasks were already selected"])
                audit.append(entry)
                continue
            static = static_checks(pr, mirror, env)
            entry.update(base_sha=static["base_sha"], head_sha=static["head_sha"], checks=dict(static["checks"]))
            reasons = list(static["reasons"])
            oracle = green = None
            if not reasons:
                if progress:
                    progress(f"sandboxed oracle check for #{pr['number']}")
                oracle = oracle_check(
                    mirror, static, stage, environment, supervisor_python, runner=runner, environ=environ, work_dir=work_dir
                )
                entry["checks"].update(
                    oracle_fails_at_base=oracle["fails_at_base"], oracle_passes_at_head=oracle["passes_at_head"]
                )
                entry["oracle"] = {
                    label: {key: oracle[label].get(key) for key in ("exit_code", "passed", "failed", "errors", "output_tail")}
                    for label in ("base", "head")
                }
                if not oracle["fails_at_base"]:
                    reasons.append("the hidden tests already pass at base+setup")
                if not oracle["passes_at_head"]:
                    reasons.append("the hidden tests do not pass at head+setup")
            if not reasons:
                scope = scopes.get(pr["number"])
                validations = list(scope["required_validations"]) if scope else list(DEFAULT_REQUIRED_VALIDATIONS)
                if progress:
                    progress(f"sandboxed green-base check for #{pr['number']}")
                green = green_base_check(
                    mirror,
                    static["base_sha"],
                    validations,
                    stage,
                    environment,
                    supervisor_python,
                    runner=runner,
                    environ=environ,
                    work_dir=work_dir,
                )
                green["validations_from"] = "the task's blinded scope" if scope else "DEFAULT_REQUIRED_VALIDATIONS (no scope yet)"
                entry["checks"]["green_base"] = green["green"]
                entry["green_base"] = green
                if not green["green"]:
                    failing = [item["command"] for item in green["validations"] if not item["passed"]]
                    reasons.append("a required check fails at base+setup: " + ", ".join(failing))
            entry.update(decision="rejected" if reasons else "kept", reasons=reasons or ["passes every check"])
            audit.append(entry)
            if reasons or oracle is None:
                continue
            scope = scopes.get(pr["number"])
            issue = pr.get("linked_issue")
            tasks.append(
                {
                    "id": f"pr{pr['number']}",
                    "number": pr["number"],
                    "pr_title": pr["title"],
                    "linked_issue": {key: issue[key] for key in ("number", "title", "body", "url", "link")} if issue else None,
                    "scope_source": "the linked issue" if issue else "the pull request's title only",
                    "base_sha": static["base_sha"],
                    "scope_status": "kept from the earlier blinded scope" if scope else "new: needs a blinded scope",
                    **{field: json.loads(json.dumps((scope or SCOPE_TEMPLATE)[field])) for field in SCOPE_TEMPLATE},
                }
            )
            solutions.append(
                {
                    "id": f"pr{pr['number']}",
                    "number": pr["number"],
                    "url": pr.get("url"),
                    "merged_at": pr["mergedAt"],
                    "base_sha": static["base_sha"],
                    "head_sha": static["head_sha"],
                    "hidden_tests": static["hidden_tests"],
                    "hidden_test_support_files": static["hidden_test_support_files"],
                    "human_solution": static["human_solution"],
                    "checks": entry["checks"],
                    "oracle": {"base": oracle["base"], "head": oracle["head"]},
                    "green_base": green,
                }
            )
    return tasks, solutions, audit


def false_positive_baseline(task: Mapping[str, Any], solution: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the human solution fits its own task's scope: the S gate's false-positive baseline.

    The same scope and file-budget check the oracle applies, on the human solution's files. A human
    solution outside its scope is one the supervisor's scope or budget gate would stop.
    """
    from .oracle import scope_and_budget

    verdict = scope_and_budget(task, solution["human_solution"]["numstat"])
    return {
        "files_changed": verdict["files_changed"],
        "max_files_changed": verdict["max_files_changed"],
        "out_of_scope_paths": verdict["out_of_scope_paths"],
        "within_budget": verdict["within_budget"],
        "s_gate_would_reject": not (verdict["in_scope"] and verdict["within_budget"]),
    }


def recheck(
    tasks: Sequence[Mapping[str, Any]],
    solutions: Sequence[dict[str, Any]],
    mirror: Path,
    stage: SyncStage,
    environment: Environment,
    supervisor_python: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    environ: Mapping[str, str] = os.environ,
    work_dir: Optional[Path] = None,
    progress: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rerun the sandboxed oracle and green-base checks with each task's own scope, scopes untouched.

    Returns the updated solutions and the tasks that no longer qualify. The task file is only read.
    """
    by_id = {str(solution["id"]): solution for solution in solutions}
    failures: list[str] = []
    updated: list[dict[str, Any]] = []
    for task in tasks:
        errors = scope_errors(task)
        if errors:
            raise TrackRError(f"{task['id']} has no complete scope: " + "; ".join(errors))
        solution = dict(by_id[str(task["id"])])
        if progress:
            progress(f"sandboxed oracle and green-base recheck for {task['id']}")
        oracle = oracle_check(
            mirror, solution, stage, environment, supervisor_python, runner=runner, environ=environ, work_dir=work_dir
        )
        green = green_base_check(
            mirror,
            str(task["base_sha"]),
            list(task["required_validations"]),
            stage,
            environment,
            supervisor_python,
            runner=runner,
            environ=environ,
            work_dir=work_dir,
        )
        green["validations_from"] = "the task's blinded scope"
        solution["oracle"] = {"base": oracle["base"], "head": oracle["head"]}
        solution["green_base"] = green
        solution["checks"] = {
            **{key: value for key, value in solution["checks"].items() if not key.startswith(("oracle_", "green_base"))},
            "oracle_fails_at_base": oracle["fails_at_base"],
            "oracle_passes_at_head": oracle["passes_at_head"],
            "green_base": green["green"],
        }
        solution["false_positive_baseline"] = false_positive_baseline(task, solution)
        if not (oracle["fails_at_base"] and oracle["passes_at_head"] and green["green"]):
            failures.append(str(task["id"]))
        updated.append(solution)
    return updated, failures


def freeze(tasks_file: Path, when: str) -> None:
    """Mark the task file frozen, changing only its top-level status; refuses while a scope is missing."""
    data = read_json(tasks_file)
    missing = {str(task["id"]): scope_errors(task) for task in data["tasks"] if scope_errors(task)}
    if missing:
        raise TrackRError(f"refusing to freeze with scopes missing: {missing}")
    data["status"] = "frozen"
    data["frozen_at"] = when
    write_json(tasks_file, data)


def public_audit(audit: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The audit a scope writer may read: which candidates were kept or rejected, and why, nothing more."""
    return [{key: entry[key] for key in ("number", "decision", "reasons") if key in entry} for entry in audit]


def provenance(pull_requests: Sequence[Mapping[str, Any]], stage: SyncStage) -> dict[str, Any]:
    canonical = json.dumps(sorted(pull_requests, key=lambda pr: pr["number"]), sort_keys=True, ensure_ascii=False).encode("utf-8")
    tool = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=False).stdout.strip()
    return {
        "target": TARGET_UPSTREAM,
        "target_license": "BSD-3-Clause",
        "candidates_sha256": hashlib.sha256(canonical).hexdigest(),
        "selected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "selected_by": "automation/benchmark/trackr/select.py",
        "selector_commit": git(["rev-parse", "HEAD"], REPO_ROOT).strip(),
        "sync": stage.record(),
        "model": MODEL,
        "python": platform.python_version(),
        "uv": tool,
        "platform": f"{platform.system()} {platform.machine()}",
        "containment": "/usr/bin/sandbox-exec (see sandbox.py)",
        "model_sessions": 0,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    from .driver import prepare

    parser = argparse.ArgumentParser(description="Freeze the Track R v2 task set (SPEC-012). Starts no model.")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--pull-requests", type=Path, help="Saved `gh pr view` JSON files' directory, instead of calling gh.")
    parser.add_argument("--tasks-out", type=Path, default=TASKS_FILE)
    parser.add_argument("--solutions-out", type=Path, default=SOLUTIONS_FILE)
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="Rerun the sandboxed oracle and green-base checks with each task's own scope; write only solutions.v2.json.",
    )
    parser.add_argument(
        "--freeze", metavar="DATE", help="With --recheck: mark tasks.v2.json frozen on DATE if every task passes."
    )
    args = parser.parse_args(argv)
    if not sandbox.available():
        print("stop: sandbox-exec is not available, and the checks refuse to run uncontained.", file=sys.stderr)
        return 2
    work = args.work_dir.resolve()
    prepared = prepare(args.cache_dir, work, args.wheelhouse, subprocess_runner, os.environ)
    if args.recheck:
        tasks_data, solutions_data = read_json(args.tasks_out), read_json(args.solutions_out)
        updated, failures = recheck(
            tasks_data["tasks"],
            solutions_data["solutions"],
            prepared.target_mirror,
            prepared.sync,
            prepared.environment,
            prepared.supervisor_python,
            work_dir=work,
            progress=lambda message: print(message, file=sys.stderr),
        )
        solutions_data["solutions"] = updated
        solutions_data["rechecked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        solutions_data["rechecked_with"] = {"sync": prepared.sync.record(), "validations": "each task's own required_validations"}
        write_json(args.solutions_out, solutions_data)
        rejected = [s["id"] for s in updated if s["false_positive_baseline"]["s_gate_would_reject"]]
        print(f"recheck: {len(updated) - len(failures)} of {len(updated)} pass; failing: {failures or 'none'}")
        print(f"S gate would reject the human solution: {len(rejected)}/{len(updated)} ({', '.join(rejected) or 'none'})")
        if failures:
            return 1
        if args.freeze:
            freeze(args.tasks_out, args.freeze)
            print(f"froze {args.tasks_out} on {args.freeze}")
        return 0
    if args.pull_requests:
        pull_requests = [json.loads((args.pull_requests / f"pr{number}.json").read_text()) for number in OWNER_CANDIDATES]
        for pr in pull_requests:
            issue_number, link = linked_issue(pr)
            saved = args.pull_requests / f"issue{issue_number}.json"
            if issue_number and saved.exists():
                pr["linked_issue"] = json.loads(saved.read_text()) | {"link": link}
    else:
        pull_requests = [fetch(number) for number in OWNER_CANDIDATES]
    tasks, solutions, audit = select_tasks(
        pull_requests,
        prepared.target_mirror,
        prepared.sync,
        prepared.environment,
        prepared.supervisor_python,
        work_dir=work,
        progress=lambda message: print(message, file=sys.stderr),
    )
    write_json(
        args.tasks_out,
        {
            "schema": "boundroom.trackr.tasks/2",
            "spec": "SPEC-012 Track R, pilot v2",
            "status": "frozen design, awaiting blinded scopes and the owner's approval; no session may start before both",
            "selection_rule": SELECTION_RULE,
            "arms": {
                "U+": "the task statement plus the allowed paths, required checks and file budget in S's exact words; "
                "arm U's settings; no gates",
                "S": "the supervisor (run_next.py --single-slice, bounded profile)",
            },
            "model": MODEL,
            "provenance": provenance(pull_requests, prepared.sync),
            "scope_instructions": SCOPE_WRITERS,
            "tasks": tasks,
            # Only the decision and the reasons: head shas and test output stay in solutions.v2.json.
            "audit": public_audit(audit),
        },
    )
    write_json(
        args.solutions_out,
        {
            "schema": "boundroom.trackr.solutions/2",
            "note": "Never shown to scope writers.",
            "solutions": solutions,
            "audit": audit,
        },
    )
    kept = ", ".join("#" + str(task["number"]) for task in tasks)
    print(f"selected {len(tasks)} of {len(audit)} candidates: {kept}")
    return 0 if len(tasks) == TASK_COUNT else 1


if __name__ == "__main__":
    sys.exit(main())
