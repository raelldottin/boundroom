"""Freeze the Track R pilot task set: task set R-b, Boundroom's own merged pull requests (SPEC-012 R0).

The rule is preregistered and mechanical. Candidates are the pull requests merged into
raelldottin/boundroom from 2026-11-13T00:00:00Z (v0.4.0, when the bounded runner profile became
the default) to 2026-11-23T23:59:59Z, in merge order. A candidate is kept when:

1. its files include at least one ``automation/tests/test_*.py``;
2. it changed 15 or fewer files;
3. it is not documentation only;
4. it touches nothing under ``automation/supervisor/``, ``automation/schemas/``,
   ``automation/context/`` or ``automation/queue/``, because ``run_next.py`` judges its own checkout;
5. its merge commit is one squash commit whose diff from its first parent is the pull request's
   file list, so ``base_sha`` (the first parent) and ``head_sha`` (the merge commit) bracket it;
6. the oracle agrees: the hidden tests (the test files it added or changed), copied from
   ``head_sha`` into a clean clone at ``base_sha``, fail or error there, and pass at ``head_sha``.

The first six kept are the pilot set. Every candidate's checks and the reason it was kept or
rejected go into the task file's ``audit``. The scope fields stay empty: a person fills them in
from each pull request's description alone, without its diff, and records that.

Run it as a module, never as a script (a script's directory would shadow the standard library's
``select``)::

    python -m automation.benchmark.trackr.select --cache-dir /outside/the/repo/cache

It reads GitHub (``gh pr list`` and a mirror clone) and starts no model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .common import (
    REPO_ROOT,
    TASKS_FILE,
    TEST_FILE,
    UPSTREAM,
    UPSTREAM_REPOSITORY,
    UV_SYNC,
    OVERLAY,
    CommandRunner,
    Overlay,
    TrackRError,
    apply_overlay,
    clone_at,
    ensure_mirror,
    git,
    has_object,
    isolated_environment,
    name_status,
    numstat,
    require_outside_repository,
    run_hidden_tests,
    subprocess_runner,
    tail,
    write_json,
)

WINDOW_START = "2026-11-13T00:00:00Z"
WINDOW_END = "2026-11-23T23:59:59Z"
MAX_FILES_CHANGED = 15
TASK_COUNT = 6
EXCLUDED_PREFIXES = ("automation/supervisor/", "automation/schemas/", "automation/context/", "automation/queue/")
GH_FIELDS = "number,title,body,mergedAt,mergeCommit,files"
GH_COMMAND = ("gh", "pr", "list", "--repo", UPSTREAM_REPOSITORY, "--state", "merged", "--json", GH_FIELDS, "--limit", "300")
SYNC_TIMEOUT_SECONDS = 900
PYTEST_TIMEOUT_SECONDS = 900

SELECTION_RULE = {
    "task_set": "R-b: Boundroom's own merged pull requests",
    "window": {"merged_from": WINDOW_START, "merged_to": WINDOW_END, "order": "mergedAt, then number"},
    "keep_if": [
        "files include at least one automation/tests/test_*.py",
        f"{MAX_FILES_CHANGED} or fewer files changed",
        "not documentation only (some file is neither under docs/ nor a .md file nor mkdocs.yml)",
        "nothing under " + ", ".join(EXCLUDED_PREFIXES),
        "the merge commit's diff from its first parent is exactly the pull request's file list",
        "hidden tests fail or error at base_sha+overlay and pass at head_sha+overlay",
    ],
    "base_sha": "the first parent of the squash merge commit",
    "head_sha": "the squash merge commit",
    "hidden_tests": "automation/tests/test_*.py files the pull request added or changed",
    "oracle": "copy the hidden tests from head_sha into a clean clone, `uv sync --frozen`, then "
    "`uv run --frozen python -m pytest -q -p no:cacheprovider <hidden tests>`. Fails at base: pytest exits 1 or 2 "
    "(a failure, an error or an import error). Passes at head: pytest exits 0 having run at least one test.",
    "count": TASK_COUNT,
    "overlay": "both oracle runs, like every session, replace "
    + ", ".join(OVERLAY.paths)
    + f" with their contents at {OVERLAY.commit} (v0.5.3) before uv sync, so base means base+overlay and head means "
    "head+overlay. A pull request that needs its own supervisor to pass is already excluded by the path rule.",
}

SCOPE_TEMPLATE: dict[str, Any] = {
    "task_statement": "",
    "allowed_paths": [],
    "required_validations": [],
    "max_files_changed": None,
    "scope_provenance": {
        "written_by": "",
        "written_at": "",
        "source": "the pull request's title and description only, without its diff",
        "diff_seen": None,
    },
}


def fetch_pull_requests() -> list[dict[str, Any]]:
    """The merged pull requests, from ``gh`` (read-only)."""
    result = subprocess.run(list(GH_COMMAND), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise TrackRError(f"gh pr list failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def in_window(pull_requests: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    chosen = [pr for pr in pull_requests if WINDOW_START <= pr["mergedAt"] <= WINDOW_END]
    return sorted(chosen, key=lambda pr: (pr["mergedAt"], pr["number"]))


def documentation_only(paths: Sequence[str]) -> bool:
    return bool(paths) and all(path.startswith("docs/") or path.endswith(".md") or path == "mkdocs.yml" for path in paths)


def static_checks(pr: Mapping[str, Any], mirror: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Every check but the oracle, all of them computed so the audit shows each one."""
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
    support = [path for status, path in statuses if path.startswith("automation/tests/") and not TEST_FILE.match(path)]
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
        },
    )
    excluded = [path for path in paths if path.startswith(EXCLUDED_PREFIXES)]
    checks = {
        "merge_commit_present": True,
        "single_parent": len(parents) == 1,
        "has_test_file": any(TEST_FILE.match(path) for path in paths),
        "files_changed_at_most_15": len(paths) <= MAX_FILES_CHANGED,
        "not_documentation_only": not documentation_only(paths),
        "no_supervisor_owned_paths": not excluded,
        "squash_matches_pull_request": sorted(paths) == pr_paths,
    }
    record["checks"] = checks
    reasons = record["reasons"]
    if not checks["single_parent"]:
        reasons.append(f"its merge commit has {len(parents)} parents, not one")
    if not checks["has_test_file"]:
        reasons.append("no automation/tests/test_*.py file in its files")
    if not checks["files_changed_at_most_15"]:
        reasons.append(f"{len(paths)} files changed, more than {MAX_FILES_CHANGED}")
    if not checks["not_documentation_only"]:
        reasons.append("documentation only")
    if excluded:
        reasons.append("touches " + ", ".join(sorted(excluded)))
    if not checks["squash_matches_pull_request"]:
        reasons.append("the merge commit's diff is not the pull request's file list (not one squash commit)")
    if checks["has_test_file"] and not hidden:
        reasons.append("its only test-file changes are deletions, so there is no hidden test")
    return record


def oracle_check(
    mirror: Path,
    base_sha: str,
    head_sha: str,
    hidden_tests: Sequence[str],
    *,
    runner: CommandRunner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
    overlay: Optional[Overlay] = None,
) -> dict[str, Any]:
    """The hidden tests at base (expected to fail) and at head (expected to pass), each in a clean clone
    with the overlay applied, as every session's clone has it."""
    result: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="trackr-select-", dir=work_dir) as scratch:
        root = Path(scratch)
        env = isolated_environment(environ, root / "private")
        for label, sha in (("base", base_sha), ("head", head_sha)):
            clone = clone_at(mirror, sha, root / label, env)
            if overlay is not None:
                result[f"{label}_overlay_files"] = len(apply_overlay(mirror, clone, overlay, env))
            synced = runner(list(UV_SYNC), clone, env, SYNC_TIMEOUT_SECONDS)
            if synced.returncode != 0:
                result[label] = {
                    "uv_sync_exit_code": synced.returncode,
                    "uv_sync_output": tail(synced.stdout + synced.stderr, 15),
                }
                continue
            outcome = run_hidden_tests(runner, mirror, head_sha, hidden_tests, clone, env, PYTEST_TIMEOUT_SECONDS)
            outcome["output_tail"] = tail(outcome["output_tail"], 15 if label == "base" else 3)
            result[label] = {"uv_sync_exit_code": 0, **outcome}
    base, head = result.get("base", {}), result.get("head", {})
    result["fails_at_base"] = base.get("uv_sync_exit_code") == 0 and bool(base.get("failed_any"))
    result["passes_at_head"] = head.get("uv_sync_exit_code") == 0 and bool(head.get("passed_all"))
    return result


def oracle_reasons(oracle: Mapping[str, Any]) -> list[str]:
    reasons = []
    for label in ("base", "head"):
        if oracle.get(label, {}).get("uv_sync_exit_code", 0) != 0:
            reasons.append(f"uv sync --frozen failed at {label}_sha with the overlay, so the oracle could not run")
    if not oracle["fails_at_base"] and oracle.get("base", {}).get("uv_sync_exit_code") == 0:
        reasons.append("the hidden tests already pass at base_sha with the overlay")
    if not oracle["passes_at_head"] and oracle.get("head", {}).get("uv_sync_exit_code") == 0:
        reasons.append("the hidden tests do not pass at head_sha with the overlay")
    return reasons


def select_tasks(
    pull_requests: Sequence[Mapping[str, Any]],
    mirror: Path,
    *,
    runner: CommandRunner = subprocess_runner,
    environ: Mapping[str, str] = os.environ,
    work_dir: Optional[Path] = None,
    count: int = TASK_COUNT,
    progress: Any = None,
    overlay: Optional[Overlay] = OVERLAY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The first ``count`` candidates that pass every check, and the audit of every candidate."""
    tasks: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="trackr-select-env-", dir=work_dir) as scratch:
        env = isolated_environment(environ, Path(scratch))
        for pr in in_window(pull_requests):
            entry: dict[str, Any] = {"number": pr["number"], "title": pr["title"], "merged_at": pr["mergedAt"]}
            if len(tasks) >= count:
                entry.update(decision="not_examined", reasons=[f"the first {count} tasks were already selected"])
                audit.append(entry)
                continue
            static = static_checks(pr, mirror, env)
            entry.update(base_sha=static["base_sha"], head_sha=static["head_sha"], checks=dict(static["checks"]))
            reasons = list(static["reasons"])
            oracle = None
            if not reasons:
                if progress:
                    progress(f"oracle check for #{pr['number']} {pr['title']}")
                oracle = oracle_check(
                    mirror,
                    static["base_sha"],
                    static["head_sha"],
                    static["hidden_tests"],
                    runner=runner,
                    environ=environ,
                    work_dir=work_dir,
                    overlay=overlay,
                )
                entry["checks"].update(
                    oracle_fails_at_base=oracle["fails_at_base"], oracle_passes_at_head=oracle["passes_at_head"]
                )
                reasons = oracle_reasons(oracle)
                # Enough of each run to audit a rejection without rerunning it.
                entry["oracle"] = {
                    label: {
                        key: oracle[label].get(key)
                        for key in ("uv_sync_exit_code", "exit_code", "passed", "failed", "errors")
                        if key in oracle[label]
                    }
                    | {"output_tail": tail(str(oracle[label].get("output_tail") or oracle[label].get("uv_sync_output") or ""), 6)}
                    for label in ("base", "head")
                    if label in oracle
                }
            else:
                entry["oracle"] = "not run: a cheaper check already rejected it"
            entry.update(decision="rejected" if reasons else "kept", reasons=reasons or ["passes every check"])
            audit.append(entry)
            if reasons or oracle is None:
                continue
            tasks.append(
                {
                    "id": f"pr{pr['number']}",
                    "number": pr["number"],
                    "title": pr["title"],
                    "body": pr.get("body") or "",
                    "merged_at": pr["mergedAt"],
                    "base_sha": static["base_sha"],
                    "head_sha": static["head_sha"],
                    "hidden_tests": static["hidden_tests"],
                    "hidden_test_support_files": static["hidden_test_support_files"],
                    "human_solution": static["human_solution"],
                    "checks": entry["checks"],
                    "oracle": {"overlay": overlay.commit if overlay else None, "base": oracle["base"], "head": oracle["head"]},
                    **json.loads(json.dumps(SCOPE_TEMPLATE)),
                }
            )
    return tasks, audit


def provenance(pull_requests: Sequence[Mapping[str, Any]], source: str) -> dict[str, Any]:
    window = in_window(pull_requests)
    canonical = json.dumps(window, sort_keys=True, ensure_ascii=False).encode("utf-8")
    tool = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=False).stdout.strip()
    return {
        "repository": UPSTREAM_REPOSITORY,
        "clone_source": source,
        "gh_command": " ".join(GH_COMMAND),
        "pull_requests_listed": len(pull_requests),
        "candidates_in_window": len(window),
        "candidates_sha256": hashlib.sha256(canonical).hexdigest(),
        "selected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "selected_by": "automation/benchmark/trackr/select.py",
        "selector_commit": git(["rev-parse", "HEAD"], REPO_ROOT).strip(),
        "python": platform.python_version(),
        "uv": tool,
        "platform": f"{platform.system()} {platform.machine()}",
        "model_sessions": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze the Track R pilot task set (SPEC-012 R0). Starts no model.")
    parser.add_argument(
        "--cache-dir", type=Path, required=True, help="Where the read-only mirror clone lives, outside any repository."
    )
    parser.add_argument("--work-dir", type=Path, help="Where the disposable clones go (default: the system temporary directory).")
    parser.add_argument("--source", default=UPSTREAM, help="The repository to mirror (default: %(default)s).")
    parser.add_argument("--pull-requests", type=Path, help="A saved `gh pr list` JSON to use instead of calling gh.")
    parser.add_argument("--out", type=Path, default=TASKS_FILE, help="The task file to write (default: %(default)s).")
    parser.add_argument("--count", type=int, default=TASK_COUNT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.work_dir:
        require_outside_repository(args.work_dir, "the work directory")
        args.work_dir.mkdir(parents=True, exist_ok=True)
    pull_requests = json.loads(args.pull_requests.read_text(encoding="utf-8")) if args.pull_requests else fetch_pull_requests()
    with tempfile.TemporaryDirectory(prefix="trackr-mirror-env-") as scratch:
        mirror = ensure_mirror(args.source, args.cache_dir, isolated_environment(os.environ, Path(scratch)))
    tasks, audit = select_tasks(
        pull_requests,
        mirror,
        work_dir=args.work_dir,
        count=args.count,
        progress=lambda message: print(message, file=sys.stderr),
    )
    write_json(
        args.out,
        {
            "schema": "boundroom.trackr.tasks/1",
            "spec": "SPEC-012 Track R, phase R0",
            "selection_rule": SELECTION_RULE,
            "overlay": {**OVERLAY.record(), "source": args.source, "release": "v0.5.3"},
            "provenance": provenance(pull_requests, args.source),
            "scope_instructions": (
                "A person fills in task_statement, allowed_paths, required_validations and max_files_changed for each task "
                "from its title and body only, without looking at its diff or files, and records who did it and when in "
                "scope_provenance. The pilot refuses to start until every task has them."
            ),
            "tasks": tasks,
            "audit": audit,
        },
    )
    kept = ", ".join(f"#{task['number']}" for task in tasks)
    print(f"selected {len(tasks)} of {len(audit)} candidates: {kept}; wrote {args.out}")
    return 0 if len(tasks) == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
