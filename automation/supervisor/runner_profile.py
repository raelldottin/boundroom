"""Claude Code runner profiles, the ceiling, the slice allowlist and the nested-run guard (SPEC-008 RA-SET-008).

A profile chooses the permission mode a supervised Claude Code runner starts in and what it may do
without asking. Profiles apply only when one is chosen, with ``--runner-profile`` or
``BOUNDROOM_CLAUDE_PROFILE``. Without one, ``run_agent.sh`` keeps today's behavior,
``BOUNDROOM_CLAUDE_PERMISSION_MODE`` defaulting to ``bypassPermissions``, until RA-SET-010
switches the default to ``parent``.

The ceiling caps what any profile asks for or resolves to. The default ceiling is ``edits``, or
``unbounded`` on a machine the operator declares isolated with ``BOUNDROOM_CLAUDE_ISOLATED=1``.

Every guard here is honest about its limit: an ``unbounded`` runner can unset a variable or
remove a lock file, which is why the default ceiling admits ``unbounded`` only on a declared
isolated machine.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Mapping, Optional

PROFILES = ("parent", "bounded", "edits", "unbounded", "custom")
CEILINGS = ("bounded", "edits", "unbounded")
# Ordered by what runs without anyone's approval.
_RANK = {"bounded": 0, "edits": 1, "unbounded": 2}
MODES = {"bounded": "dontAsk", "edits": "acceptEdits", "unbounded": "bypassPermissions"}
DEFAULT_BUDGET_USD = 5

PROFILE_ENV = "BOUNDROOM_CLAUDE_PROFILE"
CEILING_ENV = "BOUNDROOM_CLAUDE_PROFILE_CEILING"
ISOLATED_ENV = "BOUNDROOM_CLAUDE_ISOLATED"
CUSTOM_SETTINGS_ENV = "BOUNDROOM_CLAUDE_SETTINGS"
# Set by the supervisor for run_agent.sh, never by a user.
RUNNER_MODE_ENV = "BOUNDROOM_CLAUDE_RUNNER_MODE"
RUNNER_SETTINGS_ENV = "BOUNDROOM_CLAUDE_RUNNER_SETTINGS"
RUNNER_BUDGET_ENV = "BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD"
# Any of these means this process was started by a supervised run's runner.
NESTED_RUN_ENV = ("BOUNDROOM_SUPERVISOR_SLICE_ID", "REPO_AUTOMATION_SUPERVISOR_SLICE_ID", "OWLORY_SUPERVISOR_SLICE_ID")


class ProfileRefused(Exception):
    """The run must not start; raised before anything is claimed."""


@dataclass(frozen=True)
class RunnerProfile:
    requested: str
    requested_source: str
    parent_mode: Optional[str]  # the nearest Claude Code ancestor's launch mode, or None
    resolved: str
    ceiling: str
    ceiling_source: str
    effective: str
    # The permission mode run_agent.sh passes; None for custom, whose file decides.
    mode: Optional[str]
    sandbox: bool
    reasons: tuple[str, ...]

    def record(self) -> dict[str, Any]:
        """The decision report's ``runner_profile`` block (PS-21)."""
        return {
            "requested": self.requested,
            "requested_source": self.requested_source,
            "parent_mode": self.parent_mode,
            "resolved": self.resolved,
            "ceiling": self.ceiling,
            "ceiling_source": self.ceiling_source,
            "effective": self.effective,
            "permission_mode": self.mode,
            "sandbox": self.sandbox,
            "reasons": list(self.reasons),
        }

    def chain(self) -> str:
        """One line for the quick view, such as ``parent → unbounded → edits (ceiling)``."""
        steps = [self.requested]
        if self.resolved != self.requested:
            steps.append(self.resolved)
        if self.effective != self.resolved:
            steps.append(f"{self.effective} (ceiling)")
        return " → ".join(steps)


# --- the parent's launch mode ---------------------------------------------------------------------


def _ps(field: str, pid: str) -> str:
    result = subprocess.run(["ps", "-o", f"{field}=", "-p", pid], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _harness(comm: str, args: str) -> Optional[str]:
    """The harness a process is, by the same rule as ``run_agent.sh``'s ``harness_named_by``."""
    words = args.split()
    first = words[0].rsplit("/", 1)[-1] if words else ""
    second = ""
    if len(words) > 1 and (first in ("node", "nodejs", "bun", "deno", "python", "python3") or first.startswith("python3.")):
        second = words[1].rsplit("/", 1)[-1]
    for candidate in (comm.rsplit("/", 1)[-1], first, second):
        if candidate in ("codex", "claude", "hermes"):
            return candidate
    return None


def launch_mode(args: str) -> str:
    """The permission mode a Claude Code command line asks for. Only two flags are read (PS-16)."""
    words = args.split()
    mode = "default"
    for index, word in enumerate(words):
        if word == "--dangerously-skip-permissions":
            return "bypassPermissions"
        if word == "--permission-mode" and index + 1 < len(words):
            mode = words[index + 1]
        elif word.startswith("--permission-mode="):
            mode = word.split("=", 1)[1]
    return mode


def parent_launch_mode(start_pid: Optional[int] = None) -> Optional[str]:
    """The nearest enclosing harness's launch mode if it is Claude Code, else None.

    Nearest, as ``run_agent.sh`` picks the runner: Claude Code driving Hermes driving this is
    Hermes's call, and Hermes has no Claude permission mode to match.
    """
    pid = str(start_pid if start_pid is not None else os.getppid())
    for _ in range(64):  # bounded, so a ps that reports a cycle can't hang the supervisor
        if not pid or pid == "0":
            return None
        args = _ps("args", pid)
        harness = _harness(_ps("comm", pid), args)
        if harness == "claude":
            return launch_mode(args)
        if harness is not None:
            return None
        pid = _ps("ppid", pid)
    return None


# --- choosing the profile -------------------------------------------------------------------------


def configured(flags: Mapping[str, Any], environ: Mapping[str, str]) -> bool:
    """Whether the operator chose a profile at all. Without one, nothing changes (PS-10)."""
    return bool(flags.get("runner_profile") or environ.get(PROFILE_ENV))


def choose(flags: Mapping[str, Any], environ: Mapping[str, str], parent_mode: Optional[str]) -> RunnerProfile:
    """Requested profile, parent resolution, then the ceiling. Raises ProfileRefused (PS-13, PS-19)."""
    reasons: list[str] = []
    if flags.get("runner_profile"):
        requested, requested_source = flags["runner_profile"], "flag --runner-profile"
    else:
        requested, requested_source = environ.get(PROFILE_ENV, ""), f"env {PROFILE_ENV}"
    if requested not in PROFILES:
        raise ProfileRefused(f"unknown Claude runner profile {requested!r}; choose one of {', '.join(PROFILES)}")

    isolated = environ.get(ISOLATED_ENV) == "1"
    explicit_ceiling = environ.get(CEILING_ENV, "")
    if explicit_ceiling:
        if explicit_ceiling not in CEILINGS:
            raise ProfileRefused(f"unknown {CEILING_ENV} {explicit_ceiling!r}; choose one of {', '.join(CEILINGS)}")
        ceiling, ceiling_source = explicit_ceiling, f"env {CEILING_ENV}"
    elif isolated:
        ceiling, ceiling_source = "unbounded", f"default with {ISOLATED_ENV}=1"
    else:
        ceiling, ceiling_source = "edits", "default"

    sandbox = False
    if requested == "parent":
        resolved = _resolve_parent(parent_mode, reasons)
        sandbox = resolved == "bounded" and parent_mode is None
    else:
        resolved = requested
        sandbox = requested == "bounded"
        # Asking for unbounded with no isolation declaration and no ceiling of one's own is refused
        # rather than silently lowered (PS-13); an explicit lower ceiling lowers it (PS-18).
        if requested == "unbounded" and not isolated and not explicit_ceiling:
            raise ProfileRefused(
                f"the unbounded profile needs {ISOLATED_ENV}=1, the declaration that this run is in a container or VM, "
                f"or {CEILING_ENV}=unbounded"
            )

    if resolved == "custom":
        settings = environ.get(CUSTOM_SETTINGS_ENV, "")
        if not settings:
            raise ProfileRefused(f"the custom profile needs {CUSTOM_SETTINGS_ENV}, the operator's own --settings file")
        effective, mode = "custom", None
        reasons.append("custom is the operator's own settings file and isn't capped")
    else:
        effective = resolved if _RANK[resolved] <= _RANK[ceiling] else ceiling
        if effective != resolved:
            reasons.append(f"the ceiling ({ceiling}, {ceiling_source}) lowered {resolved} to {effective}")
        mode = MODES[effective]
        sandbox = sandbox and effective == "bounded"
    return RunnerProfile(
        requested=requested,
        requested_source=requested_source,
        parent_mode=parent_mode,
        resolved=resolved,
        ceiling=ceiling,
        ceiling_source=ceiling_source,
        effective=effective,
        mode=mode,
        sandbox=sandbox,
        reasons=tuple(reasons),
    )


def _resolve_parent(parent_mode: Optional[str], reasons: list[str]) -> str:
    if parent_mode is None:
        reasons.append("no Claude Code parent, or its flags couldn't be read: the fallback profile, bounded")
        return "bounded"
    if parent_mode == "plan":
        raise ProfileRefused("the parent Claude Code session is in plan mode, which means no edits; refusing to start a slice")
    if parent_mode == "bypassPermissions":
        reasons.append("the parent runs bypassPermissions")
        return "unbounded"
    if parent_mode == "acceptEdits":
        reasons.append("the parent runs acceptEdits")
        return "edits"
    if parent_mode in ("default", "dontAsk"):
        reasons.append(f"the parent runs {parent_mode}: headless, so every action is pre-approved or denied")
        return "bounded"
    if parent_mode == "auto":
        reasons.append("the parent runs auto; whether -p accepts it is unverified until RA-SET-009, so edits")
        return "edits"
    reasons.append(f"the parent's mode {parent_mode!r} isn't known: the fallback profile, bounded")
    return "bounded"


# --- the slice allowlist --------------------------------------------------------------------------


def slice_settings(
    slice_record: Mapping[str, Any], policy: Mapping[str, Any], profile: RunnerProfile, handoff: Optional[str] = None
) -> dict[str, Any]:
    """The per-slice ``--settings`` file: a pure function of the slice record, the policy and the
    handoff path the supervisor chose (PS-11). Nothing an agent or a repository file writes adds a rule.

    Deny rules win over allow rules in Claude Code, so the supervisor-owned paths, ``.git`` and
    ``.claude`` stay denied even inside an allowed path (PS-12). Claude Code applies only ``Edit``
    rules to its file tools, and ``Edit`` covers every one of them, so there are no ``Write`` rules.
    """
    allowed = list(slice_record["allowed_paths"])
    allow = [f"Edit({path})" for path in allowed]
    if handoff:
        # The handoff is the one file every slice must write, wherever its allowed paths are.
        allow.append(f"Edit({handoff})")
    allow += [f"Bash({command})" for command in slice_record.get("required_validations", [])]
    allow += ["Bash(git status)", "Bash(git diff:*)", "Bash(git rev-list:*)", "Bash(git commit:*)", "Bash(git push)"]
    allow += [f"Bash(git add {path})" for path in allowed]
    owned = list(policy.get("supervisor_owned_paths", []))
    deny = [f"Edit({path})" for path in (*owned, ".git/**", ".claude/**")]
    deny += ["Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/**)", "WebFetch", "WebSearch"]
    permissions: dict[str, Any] = {"allow": allow, "deny": deny}
    if profile.mode:
        permissions["defaultMode"] = profile.mode
    settings: dict[str, Any] = {"permissions": permissions}
    if profile.sandbox:
        settings["sandbox"] = {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            # The sandbox turns the .git deny rule into a write block, so git add and commit run
            # outside it; the allowlist above still limits them to the slice's paths (RA-SET-009).
            # An entry without a wildcard matches only the bare command, so these carry " *".
            "excludedCommands": ["git add *", "git commit *", "git push"],
            "network": {"allowedDomains": []},
        }
    return settings


def runner_notes(settings: Mapping[str, Any]) -> str:
    """The runner prompt's "What you may run" section, from the same settings file (SPEC-008)."""
    lines = [
        "## What you may run",
        "",
        "This run allows only these calls. Anything else is denied, not asked about.",
        "",
        *(f"- `{rule}`" for rule in settings["permissions"]["allow"]),
        "",
        "Run `git add`, `git commit` and `git push` as separate commands. Give the commit message as "
        '`-m "..."` with a plain quoted string. A command joined with `&&` or `;`, or one using `$(...)`, '
        "a heredoc or a redirect, is matched as a whole and may be denied.",
    ]
    if "sandbox" in settings:
        lines.append(
            "Those three git commands run outside this run's sandbox only in that plain form. Inside it, "
            "`.git` can't be written, so a commit fails."
        )
    return "\n".join(lines) + "\n"


@contextmanager
def runner_environment(
    slice_record: Mapping[str, Any],
    policy: Mapping[str, Any],
    profile: RunnerProfile,
    environ: Mapping[str, str],
    handoff: Optional[str] = None,
) -> Generator[dict[str, str], None, None]:
    """The runner's environment, with the slice settings file in a temporary directory.

    The file lives outside the repository and is removed afterwards, so the worktree never
    sees it (PS-15).
    """
    directory = Path(tempfile.mkdtemp(prefix="boundroom-runner-"))
    try:
        env = dict(environ)
        env[RUNNER_BUDGET_ENV] = str(DEFAULT_BUDGET_USD)
        if profile.effective == "custom":
            env[RUNNER_SETTINGS_ENV] = environ[CUSTOM_SETTINGS_ENV]
            env.pop(RUNNER_MODE_ENV, None)
        else:
            path = directory / "settings.json"
            path.write_text(json.dumps(slice_settings(slice_record, policy, profile, handoff), indent=2) + "\n", encoding="utf-8")
            env[RUNNER_SETTINGS_ENV] = str(path)
            env[RUNNER_MODE_ENV] = str(profile.mode)
        yield env
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --- no nested runs -------------------------------------------------------------------------------


def nested_run(environ: Mapping[str, str]) -> Optional[str]:
    """The variable that shows this process runs inside a supervised slice, if any (PS-20)."""
    return next((name for name in NESTED_RUN_ENV if environ.get(name)), None)


def refuse_if_nested(environ: Mapping[str, str], what: str) -> None:
    name = nested_run(environ)
    if name:
        raise ProfileRefused(f"{what} refused inside a supervised slice ({name} is set): no worker may start another run")


def lock_path(environ: Mapping[str, str]) -> Path:
    state = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "boundroom" / "run.lock"


@contextmanager
def run_lock(environ: Mapping[str, str]) -> Generator[None, None, None]:
    """One supervised run per user at a time, held for the whole run, outside any repository."""
    path = lock_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProfileRefused(f"another supervised run holds {path}; refusing to start a nested or parallel run") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
