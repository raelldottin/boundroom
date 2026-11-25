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
rejected go into the task file's ``audit``. The scope fields stay empty: blinded subagents (the owner's
decision) fill them in from each pull request's description alone, without its diff, and record that.

Run it as a module, never as a script (a script's directory would shadow the standard library's
``select``)::

    python -m automation.benchmark.trackr.select --cache-dir /outside/the/repo/cache

It reads GitHub (``gh pr list`` and a mirror clone) and starts no model.
"""

from __future__ import annotations

import argparse
import copy
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
    DEFAULT_REQUIRED_VALIDATIONS,
    BASE_PATCH,
    SCOPE_FIELDS,
    SCOPE_PROVENANCE_FIELDS,
    CommandRunner,
    BasePatch,
    PatchError,
    TrackRError,
    clone_at,
    ensure_mirror,
    git,
    has_object,
    isolated_environment,
    name_status,
    numstat,
    prepare_setup,
    read_json,
    require_outside_repository,
    run_hidden_tests,
    run_validations,
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
        "the base patch applies at base_sha and head_sha and takes (amendment 2)",
        "hidden tests fail or error at base_sha+patch and pass at head_sha+patch",
        "green base: every required validation passes at base_sha+patch (amendment 1)",
    ],
    "base_patch": f"#279's handoff-rule fix ({BASE_PATCH.source_commit}) to {BASE_PATCH.path} and nothing else: the "
    "one rule-building line is rewritten so a handoff outside the repository gets Edit(//abs), and the patched "
    "slice_settings is called to check that an outside handoff gets the absolute rule and no git add rule, and an "
    "inside handoff is unchanged. A base that already has the fix is left as it is. Every session, oracle run and "
    "green-base check starts from base (or head) plus this patch, after uv sync.",
    "green_base": "in a clean clone at base_sha: uv sync --frozen, the base patch, commit that as the setup, and run "
    "every required validation. They are the task's own blinded required_validations, or for a candidate with no "
    "blinded scope yet: "
    + "; ".join(DEFAULT_REQUIRED_VALIDATIONS)
    + ". The pilot runs the same check at every session's setup commit, in both arms, before any model session.",
    "base_sha": "the first parent of the squash merge commit",
    "head_sha": "the squash merge commit",
    "hidden_tests": "automation/tests/test_*.py files the pull request added or changed",
    "oracle": "copy the hidden tests from head_sha into a clean clone, `uv sync --frozen`, then "
    "`uv run --frozen python -m pytest -q -p no:cacheprovider <hidden tests>`. Fails at base: pytest exits 1 or 2 "
    "(a failure, an error or an import error). Passes at head: pytest exits 0 having run at least one test.",
    "count": TASK_COUNT,
}

SCOPE_TEMPLATE: dict[str, Any] = {
    "task_statement": "",
    "allowed_paths": [],
    "required_validations": [],
    "max_files_changed": None,
    # The keys common.SCOPE_PROVENANCE_FIELDS requires; a blinded scope writer fills them in. ``inputs``
    # must say the scope came from the title and body only, without the diff.
    "scope_provenance": {name: "" for name in SCOPE_PROVENANCE_FIELDS},
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
    support: Sequence[str] = (),
    runner: CommandRunner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
    patch: Optional[BasePatch] = None,
) -> dict[str, Any]:
    """The hidden tests at base (expected to fail) and at head (expected to pass), each in a clean clone
    with the base patch applied, as every session's clone has it."""
    result: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="trackr-select-", dir=work_dir) as scratch:
        root = Path(scratch)
        env = isolated_environment(environ, root / "private")
        for label, sha in (("base", base_sha), ("head", head_sha)):
            clone = clone_at(mirror, sha, root / label, env)
            synced = runner(list(UV_SYNC), clone, env, SYNC_TIMEOUT_SECONDS)
            if synced.returncode != 0:
                result[label] = {
                    "uv_sync_exit_code": synced.returncode,
                    "uv_sync_output": tail(synced.stdout + synced.stderr, 15),
                }
                continue
            result[f"{label}_setup"] = commit_setup(clone, patch, env, runner)
            outcome = run_hidden_tests(runner, mirror, head_sha, hidden_tests, clone, env, PYTEST_TIMEOUT_SECONDS, support)
            outcome["output_tail"] = tail(outcome["output_tail"], 15 if label == "base" else 3)
            result[label] = {"uv_sync_exit_code": 0, **outcome}
    base, head = result.get("base", {}), result.get("head", {})
    result["fails_at_base"] = base.get("uv_sync_exit_code") == 0 and bool(base.get("failed_any"))
    result["passes_at_head"] = head.get("uv_sync_exit_code") == 0 and bool(head.get("passed_all"))
    return result


def commit_setup(clone: Path, patch: Optional[BasePatch], env: Mapping[str, str], runner: CommandRunner) -> dict[str, Any]:
    """The sessions' setup commit, minus arm S's queue: the base patch, checked."""
    prepared = prepare_setup(clone, patch, env, runner)
    git(["commit", "--quiet", "--allow-empty", "-m", "Track R setup"], clone, env)
    return {"patch": prepared["patch"]}


def green_base_check(
    mirror: Path,
    base_sha: str,
    validations: Sequence[str],
    *,
    runner: CommandRunner,
    environ: Mapping[str, str],
    work_dir: Optional[Path] = None,
    patch: Optional[BasePatch] = None,
) -> dict[str, Any]:
    """Every required validation at base+patch, before anything else changes.

    A validation that already fails there would fail whatever the agent does, and arm S's agent,
    seeing it fail at baseline, stops blocked: it biases the experiment against S.
    """
    with tempfile.TemporaryDirectory(prefix="trackr-green-", dir=work_dir) as scratch:
        root = Path(scratch)
        env = isolated_environment(environ, root / "private")
        clone = clone_at(mirror, base_sha, root / "base", env)
        synced = runner(list(UV_SYNC), clone, env, SYNC_TIMEOUT_SECONDS)
        if synced.returncode != 0:
            return {"green": False, "uv_sync_exit_code": synced.returncode, "validations": []}
        setup = commit_setup(clone, patch, env, runner)
        return {"setup": setup, **run_validations(validations, clone, env, runner)}


def oracle_reasons(oracle: Mapping[str, Any]) -> list[str]:
    reasons = []
    for label in ("base", "head"):
        if oracle.get(label, {}).get("uv_sync_exit_code", 0) != 0:
            reasons.append(f"uv sync --frozen failed at {label}_sha with the base patch, so the oracle could not run")
    if not oracle["fails_at_base"] and oracle.get("base", {}).get("uv_sync_exit_code") == 0:
        reasons.append("the hidden tests already pass at base_sha with the base patch")
    if not oracle["passes_at_head"] and oracle.get("head", {}).get("uv_sync_exit_code") == 0:
        reasons.append("the hidden tests do not pass at head_sha with the base patch")
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
    patch: Optional[BasePatch] = BASE_PATCH,
    scopes: Optional[Mapping[int, Mapping[str, Any]]] = None,
    green_base: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The first ``count`` candidates that pass every check, and the audit of every candidate.

    ``scopes`` are blinded scopes already written, by pull request number. A kept task keeps its
    scope unchanged, and the green-base check uses its ``required_validations``; a candidate with no
    scope yet is checked with ``DEFAULT_REQUIRED_VALIDATIONS`` and kept with empty scope fields.
    """
    scopes = scopes or {}
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
                try:
                    oracle = oracle_check(
                        mirror,
                        static["base_sha"],
                        static["head_sha"],
                        static["hidden_tests"],
                        support=static["hidden_test_support_files"],
                        runner=runner,
                        environ=environ,
                        work_dir=work_dir,
                        patch=patch,
                    )
                except PatchError as error:
                    # The base patch did not apply or did not take: the task can't start from base+patch.
                    entry["checks"]["base_patch_applies"] = False
                    entry["oracle"] = f"not run: {error}"
                    entry.update(decision="rejected", reasons=[f"the base patch fails here: {error}"], green_base="not run")
                    audit.append(entry)
                    continue
                entry["checks"]["base_patch_applies"] = True
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
            scope = scopes.get(pr["number"])
            green = None
            if not reasons and green_base:
                validations = list(scope["required_validations"]) if scope else list(DEFAULT_REQUIRED_VALIDATIONS)
                if progress:
                    progress(f"green-base check for #{pr['number']}")
                green = green_base_check(
                    mirror, static["base_sha"], validations, runner=runner, environ=environ, work_dir=work_dir, patch=patch
                )
                green["validations_from"] = "the task's blinded scope" if scope else "DEFAULT_REQUIRED_VALIDATIONS (no scope yet)"
                entry["checks"]["green_base"] = green["green"]
                entry["green_base"] = green
                if not green["green"]:
                    failing = [item["command"] for item in green["validations"] if not item["passed"]]
                    reasons.append("a required validation fails at base_sha+patch: " + ", ".join(failing or ["uv sync"]))
            elif not reasons:
                entry["green_base"] = "not run"
            else:
                entry["green_base"] = "not run: an earlier check already rejected it"
            entry.update(decision="rejected" if reasons else "kept", reasons=reasons or ["passes every check"])
            audit.append(entry)
            if reasons or oracle is None:
                continue
            kept_scope = (
                {field: copy.deepcopy(scope[field]) for field in (*SCOPE_FIELDS, "scope_provenance") if field in scope}
                if scope
                else copy.deepcopy(SCOPE_TEMPLATE)
            )
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
                    "oracle": {"base_patch": patch.id if patch else None, "base": oracle["base"], "head": oracle["head"]},
                    "green_base": green,
                    "scope_status": "kept from the earlier blinded scope" if scope else "new: needs a blinded scope",
                    **kept_scope,
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
        "model_sessions": REAL_SESSIONS["count"],
        "model_sessions_detail": REAL_SESSIONS,
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
    parser.add_argument(
        "--previous",
        type=Path,
        help="An earlier task file whose blinded scopes, amendments and history carry over (default: --out, if it exists).",
    )
    return parser


SCOPE_WRITERS = "Scopes are written by blinded subagents, one per task, by the owner's decision (2026-11-24), not by a person: each fills in task_statement, allowed_paths, required_validations and max_files_changed from the pull request's title and body only, without its diff or files, and records by, when, inputs and leakage_ignored in scope_provenance. The pilot refuses to start until every task has them."
# Real model sessions made from this task set so far, all from R1 pilot v1, before amendments 1 and 2.
REAL_SESSIONS = {
    "count": 3,
    "complete": 2,
    "interrupted": 1,
    "cost_usd_approx": 3.9,
    "results": "discarded",
    "note": "R1 pilot v1, run before amendments 1 and 2; stopped after 2 complete sessions and part of a third",
}


AMENDMENTS = [
    {
        "id": 0,
        "date": "2026-11-24",
        "made_after": "the R0 dry run, before any model session",
        "why": "the dry run showed that each base's own supervisor, from before #279, gives a handoff outside the "
        "repository the rule Edit(/abs), which Claude Code reads as relative to the settings file, so arm S could never "
        "write its handoff",
        "change": "every clone, in both arms, got automation/supervisor/, automation/schemas/ and automation/context/ "
        "replaced by their v0.5.3 contents (b193297), and the oracle check was redone with that overlay",
        "superseded_by": 2,
    },
    {
        "id": 1,
        "date": "2026-11-25",
        "made_after": "the first 2 real R1 sessions and part of a third, about $3.90; their results are discarded",
        "why": "with the overlay the base was red before the agent changed anything: `UV_PYTHON=3.12 make reusable-check` "
        "failed at base+overlay, first at `settings_view.py --docs --check`. Arm S's agent saw a required validation fail "
        "at baseline and stopped blocked with no commit. Every task would hit it, which biases the experiment against S.",
        "change": "a new check, green base, added after real sessions had run: a task is kept only when every required validation passes at "
        "its setup commit, and the pilot runs the same check at each session's setup, in both arms, before any model "
        "session. Blinded scopes of tasks that still qualify are kept unchanged; no task's required_validations changed.",
    },
    {
        "id": 2,
        "date": "2026-11-25",
        "made_after": "the same 2 real sessions, by the owner's decision, once the green-base check showed that with the "
        "overlay only one task in the window qualified",
        "why": "the overlay made every base's own tests disagree with the overlaid supervisor (the legacy-variable "
        "removal version, run_next's warnings and prompt errors, the telemetry exporter), so their bases could never be "
        "green, and a starting checkout with test files overlaid too would be one nobody ever shipped",
        "change": "the overlay is dropped. Each base gets only #279's runner_profile.py fix for a handoff outside the "
        "repository (Edit(//abs), no git add rule), checked by calling the patched slice_settings. Selection, with the "
        "oracle check and the green-base check on base+patch, was rerun over the same window in the same merge order.",
    },
]
# The driver also stopped setting BOUNDROOM_TELEMETRY=off in the environments it builds: that kill switch
# broke the repository's own telemetry tests in `make reusable-check`, in every arm, check and oracle run.


def previous_task_file(args: argparse.Namespace) -> Optional[dict[str, Any]]:
    path = args.previous or (args.out if args.out.exists() else None)
    return read_json(path) if path else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.work_dir:
        require_outside_repository(args.work_dir, "the work directory")
        args.work_dir.mkdir(parents=True, exist_ok=True)
    pull_requests = json.loads(args.pull_requests.read_text(encoding="utf-8")) if args.pull_requests else fetch_pull_requests()
    with tempfile.TemporaryDirectory(prefix="trackr-mirror-env-") as scratch:
        mirror = ensure_mirror(args.source, args.cache_dir, isolated_environment(os.environ, Path(scratch)))
    previous = previous_task_file(args)
    scopes = {task["number"]: task for task in (previous or {}).get("tasks", []) if task.get("task_statement")}
    tasks, audit = select_tasks(
        pull_requests,
        mirror,
        work_dir=args.work_dir,
        count=args.count,
        progress=lambda message: print(message, file=sys.stderr),
        scopes=scopes,
    )
    record = provenance(pull_requests, args.source)
    earlier = (previous or {}).get("provenance", {})
    record["amendments"] = AMENDMENTS
    record["earlier_selections"] = [
        *earlier.get("earlier_selections", []),
        *(
            [{"selected_at": earlier.get("selected_at"), "tasks": [task["number"] for task in previous["tasks"]]}]
            if previous and earlier.get("selected_at")
            else []
        ),
    ]
    previous_numbers = {task["number"] for task in (previous or {}).get("tasks", [])}
    record["task_changes"] = {
        "dropped": sorted(previous_numbers - {task["number"] for task in tasks}),
        "added": [task["number"] for task in tasks if task["number"] not in previous_numbers],
        "need_blinded_scope": [task["number"] for task in tasks if not task.get("task_statement")],
    }
    write_json(
        args.out,
        {
            "schema": "boundroom.trackr.tasks/1",
            "spec": "SPEC-012 Track R, phase R0",
            "selection_rule": SELECTION_RULE,
            "base_patch": {**BASE_PATCH.record(), "source": args.source, "replaces": "the v0.5.3 overlay (amendment 2)"},
            "provenance": record,
            "scope_instructions": SCOPE_WRITERS,
            "tasks": tasks,
            "audit": audit,
        },
    )
    kept = ", ".join(f"#{task['number']}" for task in tasks)
    print(f"selected {len(tasks)} of {len(audit)} candidates: {kept}; wrote {args.out}")
    return 0 if len(tasks) == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
