"""Run one Track R session: one task, one arm, in a disposable clone (SPEC-012).

Each session gets a fresh clone at the task's ``base_sha`` in a temporary directory outside any
repository. The clone has no remote and no object from after ``base_sha``; Git reads an empty
global config and no system config, gh an empty config directory, and the GitHub token variables
and SSH agent socket are removed, so nothing in the session can push or reach GitHub with the
operator's credentials. The driver runs ``uv sync --frozen`` (so the agent never needs the network
for dependencies), makes one setup commit, runs the arm, and records the session's final commit.

The setup commit replaces ``automation/supervisor/``, ``automation/schemas/`` and
``automation/context/`` with their contents at the overlay commit (v0.5.3), in both arms, so the
starting checkouts stay identical and arm S runs today's supervisor rather than the one the task's
base shipped with. Arm S's setup commit also adds its queue.

**Arm U, unbounded.** ``claude -p "<task_statement>\\n\\nCommit your change when you are done."
--output-format json --max-budget-usd <cap>`` with ``--permission-mode acceptEdits`` and a settings
file that turns Claude Code's Bash sandbox on, fail-closed, with Bash auto-allowed inside it. File
edits are accepted inside the clone (the working directory); anything that would need a prompt,
such as an edit outside it, is denied in a headless run.

**Arm S, supervised.** The setup commit adds ``automation/queue/slices.json``: one queued slice
built from the task's scope fields, and the example queue's policy, whose
``agent_command_template`` runs the clone's own ``automation/supervisor/run_agent.sh``. The driver
then runs ``run_next.py --single-slice`` with its default runner profile (``bounded``), the
Claude runner chosen explicitly (``BOUNDROOM_AGENT_RUNNER=claude``) and the report and handoffs
outside the clone. ``run_agent.sh`` launches ``BOUNDROOM_CLAUDE_BIN``, which the driver points at a
small wrapper outside the clone that records usage (see ``usage_wrapper``).

Both arms get the same cap, wall-clock limit, inherited environment (apart from what an arm
needs), Claude Code version and isolation flags.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from automation.supervisor import runner_profile

from .common import (
    OVERLAY,
    SUPERVISOR_OWNED_PATHS,
    UV_SYNC,
    CommandRunner,
    Completed,
    TrackRError,
    Overlay,
    apply_overlay,
    clone_at,
    git,
    has_object,
    isolated_environment,
    read_json,
    recorded_env_names,
    require_outside_repository,
    scope_errors,
    subprocess_runner,
    tail,
    task_id,
    write_json,
)

ARMS = ("U", "S")
DEFAULT_CAP_USD = 4.50
DEFAULT_TIMEOUT_SECONDS = 1800
# run_next replays validations and records its verdict after the session; the driver's own guard
# allows for that before it kills run_next. The session itself is limited by the queue policy.
SUPERVISOR_MARGIN_SECONDS = 900
SYNC_TIMEOUT_SECONDS = 900
U_SUFFIX = "\n\nCommit your change when you are done."
SLICE_ID = "trackr-task"
QUEUE_PATH = "automation/queue/slices.json"
RUN_AGENT = "automation/supervisor/run_agent.sh"
# The example queue's command template (automation/examples/example-slices.json).
AGENT_COMMAND_TEMPLATE = (
    f"{RUN_AGENT} --repo-root {{repo_root}} --prompt-file {{prompt_file}} "
    "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}"
)
# Both arms load only the clone's project settings plus the arm's own --settings file, and no MCP
# server: the operator's user settings (plugins, hooks, a model choice) and connectors stay out.
# Managed (organization) settings still load, in both arms alike.
ISOLATION_ARGS = ("--setting-sources", "project,local", "--strict-mcp-config")
# Every flag the arms rely on; the preflight refuses a Claude Code whose --help lacks one.
REQUIRED_CLAUDE_FLAGS = (
    "--print",
    "--output-format",
    "--max-budget-usd",
    "--settings",
    "--permission-mode",
    "--setting-sources",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--input-format",
    "--add-dir",
)
AGENT_UV_CACHE = ".venv/.trackr-uv-cache"


@dataclass
class DriverConfig:
    cap_usd: float = DEFAULT_CAP_USD
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    claude_bin: str = "claude"
    runner: CommandRunner = subprocess_runner
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    work_dir: Optional[Path] = None
    # Both arms load only project settings and no MCP server (ISOLATION_ARGS).
    isolate_user_config: bool = True
    # The agent's uv cache inside the clone's .venv (ignored by Git), which a sandboxed command can
    # write; the operator's own uv cache is outside the clone, where the sandbox may refuse writes.
    agent_uv_cache_in_clone: bool = True
    # The S wrapper drops run_agent.sh's --no-session-persistence so S leaves a transcript, as U does.
    s_session_transcript: bool = True
    # Dry runs: stage the human solution next to the clone for the fake Claude Code to apply.
    stage_human_patch: bool = False
    # Today's supervisor, schemas and context in both arms' setup commit (the owner's decision).
    overlay: Optional[Overlay] = OVERLAY


# --- arm U ----------------------------------------------------------------------------------------


def unbounded_settings() -> dict[str, Any]:
    """Arm U's --settings: the Bash sandbox on and fail-closed, with no per-slice allowlist.

    The shape is ``runner_profile.slice_settings``'s sandbox block, minus the slice allowlist and
    plus ``autoAllowBashIfSandboxed``, so a sandboxed Bash command needs no allow rule. ``git add``
    and ``git commit`` run outside the sandbox, as in the supervised profile, so a write block on
    ``.git`` can't stop the commit the task asks for; they are the only allow rules. The network is
    closed to sandboxed commands and the web tools are denied in both arms, because the human
    solution is public on GitHub.
    """
    return {
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": ["Bash(git add:*)", "Bash(git commit:*)"],
            "deny": ["Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/**)", "WebFetch", "WebSearch"],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "excludedCommands": ["git add *", "git commit *"],
            "network": {"allowedDomains": []},
        },
    }


def unbounded_prompt(task: Mapping[str, Any]) -> str:
    return f"{str(task['task_statement']).rstrip()}{U_SUFFIX}"


def unbounded_argv(
    claude_bin: str, task: Mapping[str, Any], cap_usd: float, settings_path: Path, isolate: bool = True
) -> list[str]:
    return [
        claude_bin,
        "-p",
        unbounded_prompt(task),
        "--output-format",
        "json",
        "--max-budget-usd",
        format_usd(cap_usd),
        "--settings",
        str(settings_path),
        "--permission-mode",
        "acceptEdits",
        *(ISOLATION_ARGS if isolate else ()),
    ]


def format_usd(value: float) -> str:
    return f"{value:.2f}"


# --- arm S ----------------------------------------------------------------------------------------


def slice_record(task: Mapping[str, Any]) -> dict[str, Any]:
    """The one queued slice, from the task's scope fields only."""
    statement = str(task["task_statement"]).strip()
    title = next((line.strip() for line in statement.splitlines() if line.strip()), statement)[:120]
    return {
        "slice_id": SLICE_ID,
        "title": title,
        "status": "queued",
        "priority": 0,
        "domain": "trackr",
        "allowed_paths": list(task["allowed_paths"]),
        "required_validations": list(task["required_validations"]),
        "depends_on": [],
        "max_files_changed": int(task["max_files_changed"]),
        "notes": statement,
    }


def queue_data(task: Mapping[str, Any], timeout_seconds: int) -> dict[str, Any]:
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 1,
            "handoff_timeout_seconds": timeout_seconds,
            "agent_command_template": AGENT_COMMAND_TEMPLATE,
            "supervisor_owned_paths": list(SUPERVISOR_OWNED_PATHS),
        },
        "slices": [slice_record(task)],
    }


def supervised_argv(clone: Path, report_file: Path, handoff_dir: Path) -> list[str]:
    return [
        str(clone / ".venv" / "bin" / "python"),
        "automation/supervisor/run_next.py",
        "--single-slice",
        "--report-file",
        str(report_file),
        "--handoff-dir",
        str(handoff_dir),
    ]


def usage_wrapper(real_claude: str, session_dir: Path, cap_usd: float, *, isolate: bool, keep_transcript: bool) -> str:
    """The script ``BOUNDROOM_CLAUDE_BIN`` names in arm S.

    ``run_agent.sh`` runs Claude Code in text mode with ``--no-session-persistence``, so its cost and
    tokens are otherwise lost, and ``runner_profile.runner_environment`` always sets the cap to its
    default ($5), whatever the caller exported. The wrapper changes only what the comparison needs and
    records each change: it keeps the per-slice settings file, permission mode and every other
    argument; replaces the cap with the driver's (the same as arm U's); adds the JSON output and the
    isolation flags arm U also gets; and, unless told not to, drops ``--no-session-persistence`` so
    Claude Code writes a transcript as in arm U. It keeps a copy of the per-slice settings file,
    which ``run_next`` deletes after the run. It ``exec``s Claude Code, so a timeout's signal
    reaches Claude Code itself.
    """
    quote = shlex.quote
    drop = "shift" if keep_transcript else 'args+=("$1"); shift'
    extra = " ".join(quote(arg) for arg in (("--output-format", "json") + (ISOLATION_ARGS if isolate else ())))
    return f"""#!/usr/bin/env bash
# Track R arm S usage wrapper (SPEC-012), launched by run_agent.sh as BOUNDROOM_CLAUDE_BIN.
set -euo pipefail
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-budget-usd)
      printf '%s\\n' "${{2:-}}" >> {quote(str(session_dir / "supervisor-budget-arg.txt"))}
      args+=(--max-budget-usd {quote(format_usd(cap_usd))})
      shift 2
      ;;
    --settings)
      cp "${{2:-}}" {quote(str(session_dir / "runner-settings.json"))} 2>/dev/null || true
      args+=("$1" "${{2:-}}")
      shift 2
      ;;
    --no-session-persistence)
      {drop}
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done
printf '%q ' {quote(real_claude)} "${{args[@]}}" {extra} > {quote(str(session_dir / "runner-argv.txt"))}
exec {quote(real_claude)} "${{args[@]}}" {extra} > {quote(str(session_dir / "claude-output.json"))}
"""


def handoff_rule_status(settings: Mapping[str, Any], handoff_dir: Path) -> str:
    """Whether a per-slice settings file lets the runner write a handoff under ``handoff_dir``.

    Claude Code reads ``Edit(/x)`` as relative to the settings file and ``Edit(//x)`` as absolute
    (#279), so before that fix a handoff outside the repository could not be written.
    """
    allow = settings.get("permissions", {}).get("allow", [])
    absolute = str(handoff_dir.resolve())
    candidates = [rule for rule in allow if rule.startswith("Edit(") and absolute.lstrip("/") in rule]
    if any(rule.startswith("Edit(//") for rule in candidates):
        return "absolute"
    if candidates:
        return "single-slash: read relative to the settings file, so the handoff write is denied"
    return "missing: no allow rule names the handoff"


# --- usage ----------------------------------------------------------------------------------------


def claude_config_dir(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("CLAUDE_CONFIG_DIR") or Path(environ.get("HOME", str(Path.home()))) / ".claude")


def encoded_project(path: Path) -> str:
    """Claude Code's transcript directory name for a working directory: every other character a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def transcript_dirs(clone: Path, environ: Mapping[str, str]) -> list[Path]:
    projects = claude_config_dir(environ) / "projects"
    names = {encoded_project(clone), encoded_project(clone.resolve())}
    return sorted(projects / name for name in names if (projects / name).is_dir())


def transcript_usage(directories: Sequence[Path]) -> Optional[dict[str, Any]]:
    """Usage from Claude Code's own session transcripts, or None when there are none.

    Tokens are summed per assistant message (a message split over several lines repeats its usage,
    so each message id counts once). Cost comes from the transcript's last ``cost-state`` record per
    session, which Claude Code 2.1.311 writes; a version without it leaves the cost unknown here.
    """
    files = sorted(path for directory in directories for path in directory.rglob("*.jsonl"))
    if not files:
        return None
    messages: dict[str, dict[str, Any]] = {}
    models: set[str] = set()
    cost_states: dict[str, dict[str, Any]] = {}
    versions: set[str] = set()
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("version"):
                versions.add(str(record["version"]))
            if record.get("type") == "cost-state":
                cost_states[str(record.get("sessionId"))] = record
            message = record.get("message")
            if record.get("type") == "assistant" and isinstance(message, dict) and isinstance(message.get("usage"), dict):
                key = str(message.get("id") or record.get("uuid") or len(messages))
                messages[key] = message
                if message.get("model") and message["model"] != "<synthetic>":
                    models.add(str(message["model"]))
    tokens = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    for message in messages.values():
        for name in tokens:
            value = message["usage"].get(name)
            tokens[name] += value if isinstance(value, int) else 0
    cost = None
    if cost_states:
        cost = round(sum(float(state.get("totalCostUSD") or 0) for state in cost_states.values()), 6)
        for state in cost_states.values():
            models.update((state.get("modelUsage") or {}).keys())
    return {
        "files": [str(path) for path in files],
        "assistant_messages": len(messages),
        "tokens": tokens,
        "models": sorted(models),
        "cost_usd": cost,
        "claude_code_versions": sorted(versions),
    }


def json_usage(text: str) -> Optional[dict[str, Any]]:
    """Usage from ``claude -p --output-format json``: the last JSON object of type ``result``."""
    result = None
    for candidate in [text.strip(), *reversed(text.strip().splitlines())]:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            result = parsed
            break
    if result is None:
        return None
    usage = result.get("usage") or {}
    names = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {
        "cost_usd": result.get("total_cost_usd"),
        "subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "num_turns": result.get("num_turns"),
        "session_id": result.get("session_id"),
        "tokens": {name: usage.get(name, 0) for name in names},
        "models": sorted((result.get("modelUsage") or {}).keys()),
    }


def session_usage(transcript: Optional[Mapping[str, Any]], output: Optional[Mapping[str, Any]], cap_usd: float) -> dict[str, Any]:
    """One usage record per session: the transcript first, the JSON result as the cross-check."""
    cost, source = None, "unknown"
    if transcript and transcript.get("cost_usd") is not None:
        cost, source = transcript["cost_usd"], "transcript cost-state"
    elif output and output.get("cost_usd") is not None:
        cost, source = output["cost_usd"], "claude --output-format json"
    tokens = (
        (transcript or {}).get("tokens") if transcript and transcript.get("assistant_messages") else (output or {}).get("tokens")
    )
    models = sorted(set((transcript or {}).get("models", [])) | set((output or {}).get("models", [])))
    subtype = str((output or {}).get("subtype") or "")
    return {
        "cost_usd": cost,
        "cost_source": source,
        "cost_cross_check_usd": (output or {}).get("cost_usd") if source.startswith("transcript") else None,
        "tokens": tokens,
        "models": models,
        "result_subtype": subtype or None,
        "cap_hit": "budget" in subtype or (cost is not None and cost >= cap_usd),
        "transcript": dict(transcript) if transcript else None,
    }


def claude_version(runner: CommandRunner, claude_bin: str, env: Mapping[str, str], cwd: Path) -> str:
    completed = runner([claude_bin, "--version"], cwd, env, 60)
    return completed.stdout.strip() or f"unknown (exit {completed.returncode})"


def claude_preflight(runner: CommandRunner, claude_bin: str, env: Mapping[str, str], cwd: Path) -> list[str]:
    """The required flags this Claude Code's --help does not mention. Starts no model."""
    completed = runner([claude_bin, "--help"], cwd, env, 60)
    text = completed.stdout + completed.stderr
    return [flag for flag in REQUIRED_CLAUDE_FLAGS if flag not in text]


# --- the decision report --------------------------------------------------------------------------


def gate_of(report: Optional[Mapping[str, Any]], stderr: str = "") -> str:
    """A bounded name for what decided an S session, from run_next's report (SPEC-012 item 4's values)."""
    if report is None:
        return "refused" if "stop:" in stderr else "no_report"
    if report.get("queue_status") == "done":
        return "none"
    reason = str(report.get("reason") or "")
    for needle, gate in (
        ("timed out", "timeout"),
        ("exited with code 124", "timeout"),
        ("Agent command exited", "agent"),
        ("reported blocked", "agent"),
        ("reported failed", "agent"),
        ("handoff", "handoff"),
        ("Handoff", "handoff"),
        ("Required validations", "validation"),
        ("outside the slice's allowed_paths", "scope"),
        ("max_files_changed", "diff_budget"),
        ("manual proof", "proof"),
    ):
        if needle in reason:
            return gate
    return "other"


def last_report(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else None


# --- the fake Claude Code for dry runs ------------------------------------------------------------

FAKE_CLAUDE = '''#!{python} -I
"""A stand-in for Claude Code in Track R dry runs. It starts no model and costs nothing.

It records how it was called next to the clone (never inside it). In "human" mode it applies the
task's human solution, staged by the driver, and commits it; in "noop" mode it changes nothing.
Under run_agent.sh it writes a schema-shaped handoff, "done" with the human solution, else "blocked".
"""
import datetime, json, os, shutil, subprocess, sys, uuid

MODE = {mode!r}
argv = sys.argv[1:]
if "--version" in argv:
    print("0.0.0-trackr-fake (Claude Code)")
    sys.exit(0)
if "--help" in argv:
    print(" ".join({flags!r}))
    sys.exit(0)
cwd = os.getcwd()
outside = os.path.dirname(cwd)
record = os.path.join(outside, "fake-claude")
os.makedirs(record, exist_ok=True)
prompt = None
if "-p" in argv and argv.index("-p") + 1 < len(argv) and not argv[argv.index("-p") + 1].startswith("-"):
    prompt = argv[argv.index("-p") + 1]
if prompt is None:
    prompt = sys.stdin.read()
call = os.path.join(record, "call-%d" % len(os.listdir(record)))
os.makedirs(call)
names = ("BOUNDROOM_", "UV_", "GIT_", "GH_", "CLAUDE")
with open(os.path.join(call, "invocation.json"), "w") as handle:
    json.dump({{"argv": argv, "cwd": cwd, "env": {{k: os.environ[k] for k in sorted(os.environ) if k.startswith(names)}}}}, handle, indent=2)
with open(os.path.join(call, "prompt.txt"), "w") as handle:
    handle.write(prompt)
if "--settings" in argv:
    shutil.copy(argv[argv.index("--settings") + 1], os.path.join(call, "settings.json"))
changed = []
if MODE == "human":
    patch = os.path.join(outside, "fake-claude-input", "human.patch")
    subprocess.run(["git", "apply", "--index", "--whitespace=nowarn", patch], check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "Apply the change"], check=True)
    changed = subprocess.run(["git", "diff", "--name-only", "HEAD~1", "HEAD"], capture_output=True, text=True, check=True).stdout.split()
handoff = os.environ.get("BOUNDROOM_SUPERVISOR_HANDOFF_FILE")
if handoff:
    with open(os.environ["BOUNDROOM_SUPERVISOR_CONTEXT_FILE"]) as handle:
        template = json.load(handle)["handoff_template"]
    template.update(
        status="done" if MODE == "human" else "blocked",
        summary="Track R dry run: the fake Claude Code " + ("applied the human solution." if MODE == "human" else "changed nothing."),
        files_touched=changed,
        validations_failed=[],
        proof_level="build-tested",
        missing_proof_levels=[],
        contract_status_changes=[],
        residual_risks=["A Track R dry run: no agent worked on this."],
        repo_clean_status="clean",
        git_mirror_status="not-checked",
        timestamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if MODE != "human":
        template["validations_passed"] = []
    with open(handoff, "w") as handle:
        json.dump(template, handle, indent=2)
print(json.dumps({{
    "type": "result", "subtype": "success", "is_error": False, "result": "dry run", "num_turns": 0,
    "session_id": str(uuid.uuid4()), "total_cost_usd": 0.0,
    "usage": {{"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    "modelUsage": {{"trackr-fake": {{"inputTokens": 0, "outputTokens": 0, "costUSD": 0.0}}}},
}}))
'''


def write_fake_claude(path: Path, mode: str = "human") -> Path:
    if mode not in ("human", "noop"):
        raise TrackRError(f"unknown fake mode {mode!r}; choose human or noop")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLAUDE.format(python=sys.executable, mode=mode, flags=list(REQUIRED_CLAUDE_FLAGS)), encoding="utf-8")
    path.chmod(0o755)
    return path


# --- one session ----------------------------------------------------------------------------------


def session_environment(
    base: Mapping[str, str], arm: str, clone: Path, config: DriverConfig, wrapper: Optional[Path]
) -> dict[str, str]:
    """The arm's environment: the shared base, plus only what the arm needs."""
    env = dict(base)
    if config.agent_uv_cache_in_clone:
        env["UV_CACHE_DIR"] = str(clone / AGENT_UV_CACHE)
    if arm == "S":
        assert wrapper is not None
        env["BOUNDROOM_AGENT_RUNNER"] = "claude"
        env["BOUNDROOM_CLAUDE_BIN"] = str(wrapper)
        # Exported as asked, though runner_environment replaces it with its default; the wrapper
        # applies the cap (see usage_wrapper).
        env["BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD"] = format_usd(config.cap_usd)
    return env


def setup_commit(
    clone: Path, arm: str, task: Mapping[str, Any], config: DriverConfig, env: Mapping[str, str], mirror: Path
) -> tuple[str, list[str]]:
    """The one setup commit: the overlay in both arms, and arm S's queue. Returns its sha and the overlay's files."""
    overlaid = apply_overlay(mirror, clone, config.overlay, env) if config.overlay else []
    if arm == "S":
        write_json(clone / QUEUE_PATH, queue_data(task, config.timeout_seconds))
        git(["add", QUEUE_PATH], clone, env)
    git(["commit", "--quiet", "--allow-empty", "-m", "Track R setup"], clone, env)
    return git(["rev-parse", "HEAD"], clone, env).strip(), overlaid


def run_session(
    task: Mapping[str, Any], arm: str, mirror: Path, session_dir: Path, config: Optional[DriverConfig] = None
) -> dict[str, Any]:
    """Run one (task, arm) session and return its record. The clone is kept for the oracle;
    ``cleanup_session`` removes it."""
    config = config or DriverConfig()
    if arm not in ARMS:
        raise TrackRError(f"unknown arm {arm!r}; choose U or S")
    # The arms' environments drop every BOUNDROOM_* variable, so check the nested-run guard here,
    # before they do: a worker inside a supervised slice may not start any run (SPEC-008 PS-20).
    nested = runner_profile.nested_run(config.environ)
    if nested:
        raise TrackRError(f"refused inside a supervised slice ({nested} is set): no worker may start another run")
    errors = scope_errors(task)
    if errors:
        raise TrackRError(f"task {task_id(task)} has no scope yet: " + "; ".join(errors))
    session_dir = session_dir.resolve()
    require_outside_repository(session_dir, "the session directory")
    session_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="trackr-session-", dir=config.work_dir)).resolve()
    require_outside_repository(root, "the session clone's directory")
    base_env = isolated_environment(config.environ, root / "private")
    clone = clone_at(mirror, str(task["base_sha"]), root / "repo", base_env)
    record: dict[str, Any] = {
        "task": task_id(task),
        "number": task["number"],
        "arm": arm,
        "base_sha": task["base_sha"],
        "cap_usd": config.cap_usd,
        "timeout_seconds": config.timeout_seconds,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paths": {"session_dir": str(session_dir), "root": str(root), "clone": str(clone)},
        "remotes": git(["remote"], clone, base_env).split(),
        # The human solution must not be reachable from the agent's clone.
        "solution_absent_from_clone": not has_object(clone, str(task["head_sha"]), base_env),
        "isolation_args": list(ISOLATION_ARGS) if config.isolate_user_config else [],
    }
    if not record["solution_absent_from_clone"]:
        raise TrackRError(f"the clone for {task_id(task)} still holds head_sha {task['head_sha']}")
    synced = config.runner(list(UV_SYNC), clone, base_env, SYNC_TIMEOUT_SECONDS)
    record["uv_sync_exit_code"] = synced.returncode
    if synced.returncode != 0:
        record.update(status="setup_failed", error=tail(synced.stdout + synced.stderr, 20))
        write_json(session_dir / "record.json", record)
        return record
    if config.stage_human_patch:
        staged = root / "fake-claude-input" / "human.patch"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            git(["diff", "--binary", str(task["base_sha"]), str(task["head_sha"])], mirror, base_env), encoding="utf-8"
        )
    record["setup_sha"], overlaid = setup_commit(clone, arm, task, config, base_env, mirror)
    record["overlay"] = {**config.overlay.record(), "files_changed": len(overlaid)} if config.overlay else None

    wrapper = None
    if arm == "S":
        wrapper = session_dir / "claude-wrapper.sh"
        wrapper.write_text(
            usage_wrapper(
                config.claude_bin,
                session_dir,
                config.cap_usd,
                isolate=config.isolate_user_config,
                keep_transcript=config.s_session_transcript,
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    env = session_environment(base_env, arm, clone, config, wrapper)
    record["env_names_recorded"] = recorded_env_names(env)
    record["arm_env"] = {name: env[name] for name in sorted(env) if base_env.get(name) != env[name]}
    record["claude_code_version"] = claude_version(config.runner, config.claude_bin, env, clone)

    if arm == "U":
        settings_path = session_dir / "settings.json"
        write_json(settings_path, unbounded_settings())
        argv = unbounded_argv(config.claude_bin, task, config.cap_usd, settings_path, config.isolate_user_config)
        completed = config.runner(argv, clone, env, config.timeout_seconds)
        output_text = completed.stdout
    else:
        report_file = session_dir / "report.jsonl"
        handoff_dir = session_dir / "handoffs"
        handoff_dir.mkdir(exist_ok=True)
        argv = supervised_argv(clone, report_file, handoff_dir)
        completed = config.runner(argv, clone, env, config.timeout_seconds + SUPERVISOR_MARGIN_SECONDS)
        output_file = session_dir / "claude-output.json"
        output_text = output_file.read_text(encoding="utf-8", errors="replace") if output_file.exists() else ""
    (session_dir / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (session_dir / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    record.update(_outcome(task, arm, clone, record["setup_sha"], completed, output_text, session_dir, config, base_env, argv))
    _keep_fake_record(root, session_dir)
    write_json(session_dir / "record.json", record)
    return record


def _outcome(
    task: Mapping[str, Any],
    arm: str,
    clone: Path,
    setup_sha: str,
    completed: Completed,
    output_text: str,
    session_dir: Path,
    config: DriverConfig,
    env: Mapping[str, str],
    argv: Sequence[str],
) -> dict[str, Any]:
    final_sha = git(["rev-parse", "HEAD"], clone, env).strip()
    dirty = [line[3:] for line in git(["status", "--porcelain", "--untracked-files=all"], clone, env).splitlines()]
    # Refs the oracle clones from; set after the session, so the agent never saw them.
    git(["update-ref", "refs/heads/trackr-final", final_sha], clone, env)
    git(["update-ref", "refs/heads/trackr-setup", setup_sha], clone, env)
    rewritten = is_ancestor(clone, setup_sha, final_sha, env) is False
    usage = session_usage(transcript_usage(transcript_dirs(clone, config.environ)), json_usage(output_text), config.cap_usd)
    outcome: dict[str, Any] = {
        "status": "ran",
        "command": list(argv),
        "exit_code": completed.returncode,
        "wall_seconds": completed.wall_seconds,
        "final_sha": final_sha,
        "changed": final_sha != setup_sha,
        "history_rewritten": rewritten,
        "dirty_paths_at_end": sorted(path for path in dirty if path != QUEUE_PATH),
        "usage": usage,
        "cost_usd": usage["cost_usd"],
        "cap_hit": usage["cap_hit"],
    }
    if arm == "U":
        outcome["timed_out"] = completed.timed_out
        outcome["presented_as_done"] = completed.returncode == 0 and final_sha != setup_sha
        return outcome
    report = last_report(session_dir / "report.jsonl")
    gate = gate_of(report, completed.stderr)
    requested = session_dir / "supervisor-budget-arg.txt"
    settings_copy = session_dir / "runner-settings.json"
    outcome.update(
        timed_out=completed.timed_out or gate == "timeout",
        presented_as_done=bool(report) and report.get("queue_status") == "done",
        supervised={
            "run_next_exit_code": completed.returncode,
            "decision": (report or {}).get("decision"),
            "queue_status": (report or {}).get("queue_status"),
            "reason": (report or {}).get("reason") or tail(completed.stderr, 5),
            "gate": gate,
            "changed_file_count": (report or {}).get("changed_file_count"),
            "files_touched_outside_scope": (report or {}).get("files_touched_outside_scope"),
            "dirty_paths_outside_scope": (report or {}).get("dirty_paths_outside_scope"),
            "required_validation_failures": (report or {}).get("required_validation_failures"),
            "runner_profile": (report or {}).get("runner_profile"),
            "handoffs": sorted(str(path) for path in (session_dir / "handoffs").glob("*.json")),
            "supervisor_budget_args": requested.read_text(encoding="utf-8").split() if requested.exists() else [],
            "effective_cap_usd": config.cap_usd,
            "handoff_rule": handoff_rule_status(read_json(settings_copy), session_dir / "handoffs")
            if settings_copy.exists()
            else None,
        },
    )
    return outcome


def is_ancestor(repo: Path, ancestor: str, descendant: str, env: Mapping[str, str]) -> Optional[bool]:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, env=dict(env), capture_output=True
    )
    return {0: True, 1: False}.get(result.returncode)


def _keep_fake_record(root: Path, session_dir: Path) -> None:
    fake = root / "fake-claude"
    if fake.is_dir():
        shutil.copytree(fake, session_dir / "fake-claude", dirs_exist_ok=True)
        for call in sorted((session_dir / "fake-claude").glob("call-*")):
            settings = call / "settings.json"
            if settings.exists():
                status = handoff_rule_status(read_json(settings), session_dir / "handoffs")
                (call / "handoff-rule.txt").write_text(status + "\n", encoding="utf-8")


def cleanup_session(record: Mapping[str, Any]) -> None:
    root = record.get("paths", {}).get("root")
    if root and Path(root).name.startswith("trackr-session-"):
        shutil.rmtree(root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Track R session (SPEC-012). A real run is spending.")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--task", required=True, help="The task id, such as pr243.")
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--mirror", type=Path, required=True, help="The mirror clone select.py made.")
    parser.add_argument("--session-dir", type=Path, required=True, help="Outside any repository.")
    parser.add_argument("--cap-usd", type=float, default=DEFAULT_CAP_USD)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--fake", choices=("human", "noop"), help="Use the fake Claude Code instead of a real session.")
    parser.add_argument("--yes", action="store_true", help="Required for a real session, which is spending.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.fake and not args.yes:
        print("stop: a real session is spending; pass --yes once a person has approved it, or --fake.", file=sys.stderr)
        return 2
    tasks = {task_id(task): task for task in read_json(args.tasks)["tasks"]}
    data = read_json(args.tasks)
    config = DriverConfig(cap_usd=args.cap_usd, timeout_seconds=args.timeout, overlay=Overlay.from_record(data.get("overlay")))
    if args.fake:
        config.claude_bin = str(write_fake_claude(args.session_dir / "fake-claude-bin", args.fake))
        config.stage_human_patch = args.fake == "human"
    record = run_session(tasks[args.task], args.arm, args.mirror, args.session_dir, config)
    print(json.dumps(record, indent=2))
    cleanup_session(record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
