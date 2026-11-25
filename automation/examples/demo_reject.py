#!/usr/bin/env python3
"""Watch the Boundroom supervisor reject a handoff from the diff, not from what the handoff claims.

The demo needs no agent and no API key. In a temporary git repository it queues one slice
that may change one file under ``docs/workflows/``, then makes a scripted "agent" commit that
changes three files, one of them ``README.md``. The handoff it writes claims the slice is done
and that it touched one file. The supervisor's ``evaluate_handoff.py --dry-run`` computes the
diff itself and rejects the slice, because the diff touches ``README.md``, outside the slice's
``allowed_paths``.

What it shows, precisely: the verdict comes from the diff. The supervisor doesn't compare the
handoff's ``files_touched`` with the diff, so it would reject a handoff that listed all three
files the same way, and it would not notice a handoff that understated a diff that stayed in
scope and within the file budget.

Run it from the repository root::

    make demo-reject                                   # in Boundroom
    uv run python automation/examples/demo_reject.py   # anywhere Boundroom is synced

The supervisor it runs is this checkout's: the demo copies ``automation/supervisor``,
``automation/schemas`` and ``automation/context`` into the temporary repository, because
``evaluate_handoff.py`` treats the repository it sits in as the one it evaluates. Nothing in
this checkout changes. The temporary directory is removed afterwards unless ``--keep`` is given.

A consumer's copy of this demo is a template the sync never updates, while the supervisor it
runs is. So before running the supervisor the demo checks that it still speaks the demo's
contract: the queue and handoff it writes must satisfy the copied ``automation/schemas``, and
``evaluate_handoff.py --help`` must list every flag the demo passes. If not, the demo says it is
out of date and exits 2, rather than reporting a decision about inputs that supervisor no longer
accepts.

Exit status: 0 when the supervisor rejected the handoff for the out-of-scope file, as expected;
1 when it decided anything else; 2 when the demo itself failed, including when it is older than
the supervisor it found.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_DIRS = ("automation/supervisor", "automation/schemas", "automation/context")
EVALUATE_HANDOFF = "automation/supervisor/evaluate_handoff.py"
HANDOFF_SCHEMA = "automation/schemas/handoff.schema.json"
QUEUE_SCHEMA = "automation/schemas/slice.schema.json"
# Every flag run_demo passes to evaluate_handoff.py.
EVALUATE_HANDOFF_FLAGS = ("--slice-id", "--handoff", "--base-sha", "--head-sha", "--dry-run", "--no-telemetry")
STALE_DEMO_MESSAGE = "this demo is older than your synced supervisor; copy the current one from the release"
QUEUE = "automation/queue/slices.json"
SLICE_ID = "demo-workflow-guide"
EXPECTED_OUT_OF_SCOPE = ["README.md"]

SLICE: dict[str, Any] = {
    "slice_id": SLICE_ID,
    "title": "Clarify the workflow guide",
    "status": "queued",
    "priority": 10,
    "domain": "docs",
    "allowed_paths": ["docs/workflows/"],
    "required_validations": ["git diff --check"],
    "depends_on": [],
    "max_files_changed": 1,
    "notes": "Demo slice: one file under docs/workflows/.",
    "required_proof_level": "doc-only",
}

QUEUE_DATA: dict[str, Any] = {
    "version": 1,
    "policy": {
        "consecutive_autonomous_limit": 1,
        "handoff_timeout_seconds": 1800,
        "agent_command_template": "automation/supervisor/run_agent.sh --repo-root {repo_root} --prompt-file {prompt_file} "
        "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}",
        "supervisor_owned_paths": [QUEUE, "automation/handoffs/"],
    },
    "slices": [SLICE],
}

BASE_FILES = {
    "README.md": "# Demo project\n",
    "docs/workflows/guide.md": "# Workflow guide\n\nStep one.\n",
}

# The scripted agent stays in scope for two files and strays into README.md for the third.
AGENT_FILES = {
    "docs/workflows/guide.md": "# Workflow guide\n\nStep one.\nStep two.\n",
    "docs/workflows/faq.md": "# Workflow FAQ\n",
    "README.md": "# Demo project\n\nSee docs/workflows/guide.md.\n",
}

CLAIMED_FILES = ["docs/workflows/guide.md"]


# The supervisor finishes in about a second; this only stops a hang from hanging the test suite.
SUPERVISOR_TIMEOUT_SECONDS = 120


class DemoError(Exception):
    """The demo could not stage or run its scenario."""


class StaleDemoError(DemoError):
    """The supervisor the demo found no longer accepts the queue, handoff or flags the demo uses."""


@dataclass
class DemoResult:
    base_sha: str
    head_sha: str
    handoff: dict[str, Any]
    diff_files: list[str]
    report: dict[str, Any]
    supervisor_exit: int


def isolated_env() -> dict[str, str]:
    """The environment for git and the supervisor, free of the caller's git state and config.

    A git hook exports GIT_DIR and friends, which would point every git call back at the
    caller's repository, and a global config may sign commits or run hooks.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Boundroom demo",
        GIT_AUTHOR_EMAIL="demo@example.invalid",
        GIT_COMMITTER_NAME="Boundroom demo",
        GIT_COMMITTER_EMAIL="demo@example.invalid",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, env=isolated_env(), capture_output=True, text=True)
    if result.returncode != 0:
        raise DemoError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def write_files(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def copy_supervisor(source_root: Path, repo: Path) -> None:
    for relative in SUPERVISOR_DIRS:
        source = source_root / relative
        if not source.is_dir():
            raise DemoError(f"{relative}/ is missing from {source_root}; run the demo from a checkout with Boundroom synced.")
        shutil.copytree(source, repo / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def schema_problems(document: Any, schema: Any, root: Any = None, where: str = "$") -> list[str]:
    """Where *document* breaks *schema*, for the JSON Schema keywords the supervisor's schemas use.

    Not a general validator: it checks ``$ref`` into ``$defs``, ``anyOf``, ``type``, ``enum``,
    ``required``, ``additionalProperties: false``, ``properties``, ``items``, ``minLength``,
    ``minItems`` and ``minimum``, and ignores any other keyword. The standard library is all the
    demo may assume a consumer has.
    """
    root = schema if root is None else root
    if not isinstance(schema, dict):
        return []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target: Any = root
        for part in ref.removeprefix("#/").split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return [f"{where}: the schema's {ref} is missing"]
        return schema_problems(document, target, root, where)
    if isinstance(schema.get("anyOf"), list):
        branches = [schema_problems(document, branch, root, where) for branch in schema["anyOf"]]
        if all(branches):
            return [f"{where}: matches none of anyOf ({'; '.join(problem for branch in branches for problem in branch)})"]
    problems: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _JSON_TYPES:
        is_type = isinstance(document, _JSON_TYPES[expected]) and not (
            expected in ("integer", "number") and isinstance(document, bool)
        )
        if not is_type:
            return [f"{where}: expected {expected}, got {type(document).__name__}"]
    if "enum" in schema and document not in schema["enum"]:
        problems.append(f"{where}: {document!r} is not one of {schema['enum']}")
    if isinstance(document, str) and len(document) < schema.get("minLength", 0):
        problems.append(f"{where}: shorter than {schema['minLength']}")
    if (
        isinstance(document, (int, float))
        and not isinstance(document, bool)
        and "minimum" in schema
        and document < schema["minimum"]
    ):
        problems.append(f"{where}: below {schema['minimum']}")
    if isinstance(document, list):
        if len(document) < schema.get("minItems", 0):
            problems.append(f"{where}: fewer than {schema['minItems']} items")
        for index, item in enumerate(document):
            problems += schema_problems(item, schema.get("items"), root, f"{where}[{index}]")
    if isinstance(document, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key in schema.get("required", []):
            if key not in document:
                problems.append(f"{where}: missing required {key!r}")
        for key, value in document.items():
            if key in properties:
                problems += schema_problems(value, properties[key], root, f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{where}: {key!r} is not allowed")
    return problems


def supervisor_contract_problems(repo: Path) -> list[str]:
    """Why the supervisor copied into *repo* won't accept what the demo gives it; empty when it will."""
    problems: list[str] = []
    for relative, document in ((QUEUE_SCHEMA, QUEUE_DATA), (HANDOFF_SCHEMA, build_handoff("0" * 40))):
        try:
            schema = json.loads((repo / relative).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.append(f"{relative}: unreadable ({error})")
            continue
        problems += [f"{relative}: {problem}" for problem in schema_problems(document, schema)]
    try:
        completed = subprocess.run(
            [sys.executable, str(repo / EVALUATE_HANDOFF), "--help"],
            cwd=repo,
            env=isolated_env(),
            capture_output=True,
            text=True,
            timeout=SUPERVISOR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return problems + [f"{EVALUATE_HANDOFF} --help did not finish within {SUPERVISOR_TIMEOUT_SECONDS} seconds"]
    if completed.returncode != 0:
        return problems + [f"{EVALUATE_HANDOFF} --help exited {completed.returncode}: {completed.stderr.strip()}"]
    listed = set(completed.stdout.replace(",", " ").replace("[", " ").replace("]", " ").split())
    missing = [flag for flag in EVALUATE_HANDOFF_FLAGS if flag not in listed]
    if missing:
        problems.append(f"{EVALUATE_HANDOFF} --help does not list {', '.join(missing)}")
    return problems


def check_supervisor_contract(repo: Path) -> None:
    """Raise ``StaleDemoError`` when the copied supervisor no longer accepts the demo's inputs."""
    problems = supervisor_contract_problems(repo)
    if problems:
        raise StaleDemoError(STALE_DEMO_MESSAGE + ".\n  - " + "\n  - ".join(problems))


def build_handoff(head_sha: str) -> dict[str, Any]:
    """A handoff valid against automation/schemas/handoff.schema.json that understates the work."""
    return {
        "slice_id": SLICE_ID,
        "status": "done",
        "summary": "Added step two to the workflow guide.",
        "files_touched": list(CLAIMED_FILES),
        "validations_passed": ["git diff --check"],
        "validations_failed": [],
        "proof_level": "doc-only",
        "missing_proof_levels": [],
        "contract_status_changes": [],
        "residual_risks": ["None: the guide change is small."],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "repo_clean_status": "clean",
        "git_mirror_status": "not-checked",
        "dirty_paths_outside_scope": [],
        "timestamp": "2026-01-01T00:00:00Z",
        "verified_commit_sha": head_sha,
    }


def run_demo(workdir: Path, source_root: Optional[Path] = None) -> DemoResult:
    """Stage the scenario under *workdir* and return what the supervisor decided."""
    repo = workdir / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    copy_supervisor(SOURCE_ROOT if source_root is None else source_root, repo)
    check_supervisor_contract(repo)
    write_files(repo, {QUEUE: json.dumps(QUEUE_DATA, indent=2) + "\n", **BASE_FILES})
    base_sha = commit_all(repo, "Queue the demo slice")

    write_files(repo, AGENT_FILES)
    head_sha = commit_all(repo, "Scripted agent: update the workflow guide")
    diff_files = git(repo, "diff", "--name-only", base_sha, head_sha).splitlines()

    # Outside the repository, so the handoff is not part of the diff it describes.
    handoff = build_handoff(head_sha)
    handoff_path = workdir / "handoff.json"
    handoff_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")

    command = [
        sys.executable,
        str(repo / EVALUATE_HANDOFF),
        "--slice-id",
        SLICE_ID,
        "--handoff",
        str(handoff_path),
        "--base-sha",
        base_sha,
        "--head-sha",
        head_sha,
        "--dry-run",
        "--no-telemetry",
    ]
    try:
        completed = subprocess.run(
            command, cwd=repo, env=isolated_env(), capture_output=True, text=True, timeout=SUPERVISOR_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise DemoError(f"evaluate_handoff.py did not finish within {SUPERVISOR_TIMEOUT_SECONDS} seconds.") from None
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        raise DemoError(
            f"evaluate_handoff.py exited {completed.returncode} without a JSON report.\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        ) from None
    if not isinstance(report, dict):
        raise DemoError(f"evaluate_handoff.py printed {type(report).__name__}, not a report object.")
    return DemoResult(base_sha, head_sha, handoff, diff_files, report, completed.returncode)


def rejected_as_expected(result: DemoResult) -> bool:
    return (
        result.report.get("decision") == "reject"
        and result.report.get("out_of_scope_files") == EXPECTED_OUT_OF_SCOPE
        and result.supervisor_exit == 1
    )


def render(result: DemoResult) -> str:
    report = result.report
    out_of_scope = set(report.get("out_of_scope_files") or [])
    claimed = list(result.handoff["files_touched"])
    shown = [f"{name}  <- outside allowed_paths" if name in out_of_scope else name for name in result.diff_files]
    left_width = max(len("Handoff claims"), *(len(name) for name in claimed)) + 4

    def row(label: str, left: str, right: str) -> str:
        return f"  {label:<15}{left:<{left_width}}{right}".rstrip()

    lines = [
        "Boundroom demo: the supervisor judges the diff, not the handoff's claims",
        "",
        f"Slice {SLICE_ID}",
        f"  allowed_paths      {', '.join(SLICE['allowed_paths'])}",
        f"  max_files_changed  {SLICE['max_files_changed']}",
        f"  proof level        {SLICE['required_proof_level']}",
        f"  base..head         {result.base_sha[:12]}..{result.head_sha[:12]}",
        "",
        row("", "Handoff claims", "Git diff shows"),
        row("status", result.handoff["status"], ""),
        row("files changed", str(len(claimed)), str(len(result.diff_files))),
    ]
    for index in range(max(len(claimed), len(shown))):
        lines.append(row("", claimed[index] if index < len(claimed) else "", shown[index] if index < len(shown) else ""))
    lines += [
        "",
        "Supervisor: evaluate_handoff.py --dry-run",
        f"  decision           {report.get('decision')}",
        f"  reason             {report.get('reason')}",
        f"  changed_file_count {report.get('changed_file_count')}",
        f"  out_of_scope_files {json.dumps(report.get('out_of_scope_files'))}",
        f"  exit code          {result.supervisor_exit}",
        "",
    ]
    if rejected_as_expected(result):
        lines.append("Rejected as expected: the diff touches README.md, outside allowed_paths. --dry-run changed no queue.")
        lines.append("The verdict comes from the diff alone: the supervisor doesn't compare files_touched with it, so it would")
        lines.append("not notice an understated handoff whose diff stayed in scope and within the file budget.")
    else:
        lines.append("UNEXPECTED: the supervisor did not reject the handoff for README.md. Its report:")
        lines.append(json.dumps(report, indent=2))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--keep", action="store_true", help="Keep the temporary repository and print where it is.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workdir = Path(tempfile.mkdtemp(prefix="boundroom-demo-"))
    except OSError as error:
        print(f"demo failed: {error}", file=sys.stderr)
        return 2
    try:
        result = run_demo(workdir)
        text = render(result)
        expected = rejected_as_expected(result)
    except Exception as error:
        # Exit 1 is reserved for a supervisor that decided something else; anything that went
        # wrong in the demo itself is 2, whatever it raised.
        if isinstance(error, (DemoError, OSError)):
            print(f"demo failed: {error}", file=sys.stderr)
        else:
            print("demo failed with an unexpected error:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        if args.keep:
            print(f"Kept the partial temporary directory: {workdir}", file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    print(text)
    if args.keep:
        print(f"\nKept the temporary repository: {workdir / 'repo'} (handoff: {workdir / 'handoff.json'})")
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
