"""Every setting the Boundroom supervisor reads, declared once (SPEC-008, RA-SET-001).

Each entry records where a setting can come from (flags, environment variables, the repository's
policy files), its default, its surfacing class and what the repository may do with it. The
registry describes today's behavior and changes none of it: entry points still read their own
flags and variables. The resolver (RA-SET-002) and the views (RA-SET-003) are built on it.

``automation/tests/test_settings_registry.py`` keeps it complete: every parser option, every
variable the code reads, every policy-model field and every flag a skill mentions must be here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

EntryPoint = Literal["run_next", "evaluate_handoff", "kanban_projection", "operator_status", "sync"]
SettingClass = Literal["secret", "internal", "security", "essential", "advanced", "outside"]
RepositoryRule = Literal["any", "narrow-only", "never"]
Area = Literal["runner", "work source", "policy", "sync", "memory", "telemetry", "evaluate", "projection", "status", "internal"]

ENTRY_POINTS: tuple[EntryPoint, ...] = ("run_next", "evaluate_handoff", "kanban_projection", "operator_status", "sync")
CLASSES: tuple[SettingClass, ...] = ("secret", "internal", "security", "essential", "advanced", "outside")
REPOSITORY_RULES: tuple[RepositoryRule, ...] = ("any", "narrow-only", "never")

# A value that is computed at run time rather than fixed, such as the interpreter running a tool.
COMPUTED = "<computed>"


@dataclass(frozen=True)
class Flag:
    entry_point: EntryPoint
    options: tuple[str, ...]  # every spelling, such as ("--kanban-board", "--board")


@dataclass(frozen=True)
class Setting:
    id: str
    area: Area
    klass: SettingClass
    description: str
    default: Any = None
    choices: Optional[tuple[str, ...]] = None
    flags: tuple[Flag, ...] = ()
    env: Optional[str] = None
    # Older names still read when ``env`` is unset (SPEC-005), newest first.
    legacy_env: tuple[str, ...] = ()
    # Dotted paths into the repository's files: ``queue.policy.<field>``, ``overlay.<field>``,
    # ``overlay.policy.<field>`` or ``memory.<field>``.
    repository_fields: tuple[str, ...] = ()
    repository_rule: RepositoryRule = "never"
    # A key in one of the operator's files under ~/.config/boundroom/, as ``<file>:<key>``.
    operator_file: Optional[str] = None
    # Shown in the quick view only when this other setting has this value.
    only_when: Optional[tuple[str, str]] = None
    # A boolean setting that forces this one to ``off_value`` whatever else is set, and the value
    # of ``env`` that does the same. Telemetry's kill switch is the one use.
    kill_switch: Optional[str] = None
    off_value: Any = None

    @property
    def secret(self) -> bool:
        return self.klass == "secret"

    @property
    def env_names(self) -> tuple[str, ...]:
        return ((self.env,) if self.env else ()) + self.legacy_env


def _flag(entry_point: EntryPoint, *options: str) -> Flag:
    return Flag(entry_point, options)


def _legacy(suffix: str) -> tuple[str, ...]:
    return (f"REPO_AUTOMATION_{suffix}",)


_POLICY = ("queue.policy", "overlay.policy")


def _policy_fields(field: str) -> tuple[str, ...]:
    return tuple(f"{source}.{field}" for source in _POLICY)


SETTINGS: tuple[Setting, ...] = (
    # --- runner (run_agent.sh) ---------------------------------------------------------------
    Setting(
        "runner",
        "runner",
        "security",
        "Which agent harness runs the slice; auto means the one the call came from.",
        default="auto",
        choices=("auto", "codex", "claude", "hermes"),
        env="BOUNDROOM_AGENT_RUNNER",
        legacy_env=_legacy("AGENT_RUNNER"),
    ),
    Setting(
        "claude_permission_mode",
        "runner",
        "security",
        "The permission mode a Claude Code runner starts in.",
        default="bypassPermissions",
        env="BOUNDROOM_CLAUDE_PERMISSION_MODE",
        legacy_env=_legacy("CLAUDE_PERMISSION_MODE"),
    ),
    Setting(
        "claude_profile",
        "runner",
        "security",
        "The Claude Code runner profile. Unset keeps claude_permission_mode's behavior until RA-SET-010.",
        choices=("parent", "bounded", "edits", "unbounded", "custom"),
        flags=(_flag("run_next", "--runner-profile"),),
        env="BOUNDROOM_CLAUDE_PROFILE",
    ),
    Setting(
        "claude_profile_ceiling",
        "runner",
        "security",
        "The most a Claude runner may get; edits by default, unbounded when the machine is declared isolated.",
        default=COMPUTED,
        choices=("bounded", "edits", "unbounded"),
        env="BOUNDROOM_CLAUDE_PROFILE_CEILING",
    ),
    Setting(
        "claude_isolated",
        "runner",
        "security",
        "1 declares this run is in a container or VM, which lets the ceiling admit unbounded.",
        env="BOUNDROOM_CLAUDE_ISOLATED",
    ),
    Setting(
        "claude_settings",
        "runner",
        "security",
        "The operator's own Claude Code settings file, for the custom profile.",
        env="BOUNDROOM_CLAUDE_SETTINGS",
    ),
    Setting(
        "codex_bin",
        "runner",
        "advanced",
        "The Codex executable.",
        default="codex",
        env="BOUNDROOM_CODEX_BIN",
        legacy_env=_legacy("CODEX_BIN") + ("OWLORY_CODEX_BIN",),
    ),
    Setting(
        "claude_bin",
        "runner",
        "advanced",
        "The Claude Code executable.",
        default="claude",
        env="BOUNDROOM_CLAUDE_BIN",
        legacy_env=_legacy("CLAUDE_BIN"),
    ),
    Setting(
        "hermes_bin",
        "runner",
        "advanced",
        "The Hermes executable.",
        default="hermes",
        env="BOUNDROOM_HERMES_BIN",
        legacy_env=_legacy("HERMES_BIN"),
    ),
    Setting(
        "hermes_transport",
        "runner",
        "advanced",
        "How run_agent.sh talks to Hermes.",
        default="oneshot",
        choices=("oneshot", "stream-json"),
        env="BOUNDROOM_HERMES_TRANSPORT",
        legacy_env=_legacy("HERMES_TRANSPORT"),
    ),
    Setting(
        "hermes_max_turns",
        "runner",
        "advanced",
        "A turn limit for a Hermes session; honoured on the stream-json transport only.",
        env="BOUNDROOM_HERMES_MAX_TURNS",
        legacy_env=_legacy("HERMES_MAX_TURNS"),
    ),
    Setting(
        "hermes_python",
        "runner",
        "advanced",
        "The Hermes interpreter for the sandbox probe; defaults to the one beside the Hermes executable.",
        default=COMPUTED,
        env="BOUNDROOM_HERMES_PYTHON",
        legacy_env=_legacy("HERMES_PYTHON"),
    ),
    Setting(
        "hermes_usage_dir",
        "runner",
        "advanced",
        "Where a Hermes session's usage and controls report is written.",
        env="BOUNDROOM_HERMES_USAGE_DIR",
        legacy_env=_legacy("HERMES_USAGE_DIR"),
    ),
    Setting(
        "hermes_sandbox_image",
        "runner",
        "security",
        "The container image Hermes runs its tools in.",
        env="BOUNDROOM_HERMES_SANDBOX_IMAGE",
        legacy_env=_legacy("HERMES_SANDBOX_IMAGE"),
        only_when=("runner", "hermes"),
    ),
    # --- work source --------------------------------------------------------------------------
    Setting(
        "queue",
        "work source",
        "essential",
        "The JSON queue slices come from, when no board is given.",
        default="automation/queue/slices.json",
        flags=(_flag("run_next", "--queue"), _flag("evaluate_handoff", "--queue"), _flag("operator_status", "--queue")),
    ),
    Setting(
        "board",
        "work source",
        "essential",
        "The Kanban board directory slices come from, instead of the queue.",
        flags=(_flag("run_next", "--kanban-board", "--board"),),
        repository_fields=("overlay.board",),
        repository_rule="any",
    ),
    Setting(
        "kanban_overlay",
        "work source",
        "essential",
        "The approval overlay for the board.",
        flags=(_flag("run_next", "--kanban-overlay"),),
    ),
    Setting(
        "board_backend",
        "work source",
        "essential",
        "The board's kind; defaults to the overlay's backend and must match it.",
        default="hermes",
        choices=("hermes", "local"),
        flags=(_flag("run_next", "--board-backend"),),
        repository_fields=("overlay.backend",),
        repository_rule="any",
    ),
    Setting(
        "hermes_command",
        "work source",
        "advanced",
        "The command that runs the Hermes CLI, for writes to a Hermes board.",
        default="hermes",
        flags=(_flag("run_next", "--hermes"),),
    ),
    Setting(
        "handoff_dir",
        "work source",
        "advanced",
        "Where handoff artifacts are expected.",
        default="automation/handoffs",
        flags=(_flag("run_next", "--handoff-dir"),),
    ),
    Setting(
        "include_blocked",
        "work source",
        "advanced",
        "Report blocked and deferred slices when none is eligible.",
        default=False,
        flags=(_flag("run_next", "--include-blocked"),),
    ),
    Setting(
        "dry_run",
        "work source",
        "advanced",
        "Decide without launching an agent or changing the queue.",
        default=False,
        flags=(_flag("run_next", "--dry-run"), _flag("evaluate_handoff", "--dry-run")),
    ),
    # --- policy -------------------------------------------------------------------------------
    Setting(
        "single_slice",
        "policy",
        "security",
        "Run at most one slice, refusing any autonomous limit.",
        default=False,
        flags=(_flag("run_next", "--single-slice"),),
    ),
    Setting(
        "autonomous_limit",
        "policy",
        "security",
        "How many slices may run back to back unattended; the policy value is a default a caller can override.",
        flags=(_flag("run_next", "--autonomous-limit"),),
        repository_fields=_policy_fields("consecutive_autonomous_limit"),
        repository_rule="any",
    ),
    Setting(
        "agent_command",
        "policy",
        "security",
        "The shell command template that launches the agent.",
        flags=(_flag("run_next", "--agent-cmd"),),
        repository_fields=_policy_fields("agent_command_template"),
        repository_rule="any",
    ),
    Setting(
        "supervisor_owned_paths",
        "policy",
        "security",
        "Files no slice may change.",
        repository_fields=_policy_fields("supervisor_owned_paths"),
        repository_rule="any",
    ),
    Setting(
        "handoff_timeout_seconds",
        "policy",
        "advanced",
        "How long the supervisor waits for an agent's handoff.",
        repository_fields=_policy_fields("handoff_timeout_seconds"),
        repository_rule="any",
    ),
    # --- memory ---------------------------------------------------------------------------------
    Setting(
        "memory_config",
        "memory",
        "advanced",
        "The operator-reviewed Hindsight project-to-bank map to recall from.",
        flags=(_flag("run_next", "--memory-config"),),
    ),
    Setting(
        "memory_project",
        "memory",
        "advanced",
        "The project whose granted bank each context bundle recalls from.",
        flags=(_flag("run_next", "--memory-project"),),
    ),
    Setting(
        "memory_endpoint",
        "memory",
        "security",
        "The Hindsight server; data leaves the machine when it is set.",
        repository_fields=("memory.endpoint",),
        repository_rule="any",
    ),
    Setting(
        "memory_api_key_env",
        "memory",
        "secret",
        "The variable holding the Hindsight credential. Only its name and whether it is set are shown.",
        repository_fields=("memory.api_key_env",),
        repository_rule="any",
    ),
    Setting(
        "memory_timeout_seconds",
        "memory",
        "advanced",
        "The Hindsight request timeout.",
        repository_fields=("memory.timeout_seconds",),
        repository_rule="any",
    ),
    *(
        Setting(
            f"memory_budget_{name}",
            "memory",
            "advanced",
            f"Memory budget: {name.replace('_', ' ')}.",
            repository_fields=(f"memory.budgets.{name}",),
            repository_rule="any",
        )
        for name in (
            "max_request_bytes",
            "max_response_bytes",
            "recall_max_tokens",
            "reflect_max_tokens",
            "max_results",
            "max_result_chars",
            "max_total_chars",
            "max_snapshot_memories",
        )
    ),
    Setting(
        "memory_projects",
        "memory",
        "advanced",
        "Which bank each project may use, and for which operations.",
        repository_fields=("memory.projects",),
        repository_rule="any",
    ),
    # --- telemetry (SPEC-006) ------------------------------------------------------------------
    Setting(
        "telemetry_kill_switch",
        "telemetry",
        "security",
        "Turn telemetry off whatever any other source says; BOUNDROOM_TELEMETRY=off does the same.",
        default=False,
        flags=(_flag("run_next", "--no-telemetry"), _flag("evaluate_handoff", "--no-telemetry")),
    ),
    Setting(
        "telemetry_mode",
        "telemetry",
        "security",
        "off, file or otlp. Telemetry is on only with a mode and a destination.",
        default="off",
        choices=("off", "file", "otlp"),
        flags=(_flag("run_next", "--telemetry"), _flag("evaluate_handoff", "--telemetry")),
        env="BOUNDROOM_TELEMETRY",
        repository_fields=_policy_fields("telemetry.mode"),
        repository_rule="narrow-only",
        operator_file="telemetry.json:mode",
        kill_switch="telemetry_kill_switch",
        off_value="off",
    ),
    *(
        Setting(
            f"telemetry_{name}",
            "telemetry",
            "security",
            description,
            flags=(
                _flag("run_next", f"--telemetry-{name.replace('_', '-')}"),
                _flag("evaluate_handoff", f"--telemetry-{name.replace('_', '-')}"),
            ),
            env=f"BOUNDROOM_TELEMETRY_{name.upper()}",
            operator_file=f"telemetry.json:{name}",
        )
        for name, description in (
            ("file_dir", "Where file mode writes its JSON Lines; an absolute directory outside the repository."),
            ("endpoint", "Your OTLP endpoint; https://, or http:// to localhost only."),
        )
    ),
    Setting(
        "telemetry_headers",
        "telemetry",
        "secret",
        "Headers sent only to the endpoint, such as a token. Never a flag, never printed.",
        default={},
        env="BOUNDROOM_TELEMETRY_HEADERS",
        operator_file="telemetry.json:headers",
    ),
    Setting(
        "telemetry_capture",
        "telemetry",
        "security",
        "metadata, or content as well.",
        default="metadata",
        choices=("metadata", "content"),
        flags=(_flag("run_next", "--telemetry-capture"), _flag("evaluate_handoff", "--telemetry-capture")),
        env="BOUNDROOM_TELEMETRY_CAPTURE",
        repository_fields=_policy_fields("telemetry.capture"),
        repository_rule="narrow-only",
        operator_file="telemetry.json:capture",
    ),
    Setting(
        "telemetry_runner",
        "telemetry",
        "security",
        "Whether the runner's own telemetry joins the trace.",
        default="off",
        choices=("off", "metadata", "content"),
        flags=(_flag("run_next", "--telemetry-runner"), _flag("evaluate_handoff", "--telemetry-runner")),
        env="BOUNDROOM_TELEMETRY_RUNNER",
        repository_fields=_policy_fields("telemetry.runner"),
        repository_rule="narrow-only",
        operator_file="telemetry.json:runner",
    ),
    Setting(
        "telemetry_required",
        "telemetry",
        "advanced",
        "Refuse to start a run whose telemetry can't be enabled.",
        default=False,
        flags=(_flag("run_next", "--telemetry-required"), _flag("evaluate_handoff", "--telemetry-required")),
        env="BOUNDROOM_TELEMETRY_REQUIRED",
        operator_file="telemetry.json:required",
    ),
    Setting(
        "telemetry_environment",
        "telemetry",
        "advanced",
        "A short name recorded on every span, such as local, ci or benchmark.",
        default="local",
        env="BOUNDROOM_TELEMETRY_ENVIRONMENT",
        repository_fields=_policy_fields("telemetry.environment"),
        repository_rule="any",
        operator_file="telemetry.json:environment",
    ),
    # --- evaluate_handoff ------------------------------------------------------------------------
    Setting(
        "evaluate_slice_id", "evaluate", "advanced", "The slice to evaluate.", flags=(_flag("evaluate_handoff", "--slice-id"),)
    ),
    Setting(
        "evaluate_handoff", "evaluate", "advanced", "The handoff to evaluate.", flags=(_flag("evaluate_handoff", "--handoff"),)
    ),
    Setting(
        "evaluate_base_sha", "evaluate", "advanced", "Git SHA before the work.", flags=(_flag("evaluate_handoff", "--base-sha"),)
    ),
    Setting(
        "evaluate_head_sha",
        "evaluate",
        "advanced",
        "Git SHA of the verified work.",
        flags=(_flag("evaluate_handoff", "--head-sha"),),
    ),
    # --- kanban_projection -----------------------------------------------------------------------
    Setting(
        "projection_board_dir",
        "projection",
        "advanced",
        "The board directory to project.",
        flags=(_flag("kanban_projection", "--board-dir"), _flag("operator_status", "--board-dir")),
    ),
    Setting(
        "projection_overlay",
        "projection",
        "advanced",
        "The approval overlay to project with.",
        flags=(_flag("kanban_projection", "--overlay"), _flag("operator_status", "--overlay")),
    ),
    Setting(
        "projection_shadow_queue",
        "projection",
        "advanced",
        "A JSON queue to compare the projection with.",
        flags=(_flag("kanban_projection", "--shadow-queue"),),
    ),
    # --- operator_status -------------------------------------------------------------------------
    Setting(
        "status_json",
        "status",
        "advanced",
        "Print the operator status as one versioned JSON object instead of a summary.",
        default=False,
        flags=(_flag("operator_status", "--json"),),
    ),
    # --- sync (Tools/boundroom-sync.sh) ---------------------------------------------------------
    Setting(
        "sync_check", "sync", "advanced", "Report drift without changing files.", default=False, flags=(_flag("sync", "--check"),)
    ),
    Setting(
        "sync_sync",
        "sync",
        "advanced",
        "Update the consumer to match the manifest.",
        default=False,
        flags=(_flag("sync", "--sync"),),
    ),
    Setting("sync_target", "sync", "advanced", "The consumer folder to write into.", flags=(_flag("sync", "--target"),)),
    Setting(
        "sync_source",
        "sync",
        "advanced",
        "The canonical source checkout; defaults to the one running the tool.",
        default=COMPUTED,
        flags=(_flag("sync", "--source"),),
    ),
    Setting("sync_manifest", "sync", "advanced", "The manifest to sync from.", flags=(_flag("sync", "--manifest"),)),
    Setting("sync_pin", "sync", "security", "The full commit the source must be at.", flags=(_flag("sync", "--pin"),)),
    Setting(
        "sync_expect_remote",
        "sync",
        "security",
        "The URL the source's origin must match.",
        flags=(_flag("sync", "--expect-remote"),),
    ),
    Setting(
        "sync_quality_command",
        "sync",
        "advanced",
        "The quality gate the source must pass.",
        default="make reusable-check",
        flags=(_flag("sync", "--quality-command"),),
    ),
    Setting(
        "sync_runtime_python",
        "sync",
        "advanced",
        "The consumer's interpreter, checked against the manifest's runtime dependencies.",
        default=COMPUTED,
        flags=(_flag("sync", "--runtime-python"),),
    ),
    Setting(
        "sync_allow_unverified_source",
        "sync",
        "security",
        "Development only: skip the source checks. AGENTS.md forbids agents from using it.",
        default=False,
        flags=(_flag("sync", "--allow-unverified-source"),),
    ),
    Setting(
        "sync_force_templates",
        "sync",
        "security",
        "Overwrite a consumer's local changes to template files.",
        default=False,
        flags=(_flag("sync", "--force-templates"),),
    ),
    # --- set by Boundroom, not by you ---------------------------------------------------------
    Setting(
        "supervisor_context_file",
        "internal",
        "internal",
        "Exported to the agent: its context bundle.",
        env="BOUNDROOM_SUPERVISOR_CONTEXT_FILE",
        legacy_env=_legacy("SUPERVISOR_CONTEXT_FILE") + ("OWLORY_SUPERVISOR_CONTEXT_FILE",),
    ),
    Setting(
        "supervisor_handoff_file",
        "internal",
        "internal",
        "Exported to the agent: where it writes its handoff.",
        env="BOUNDROOM_SUPERVISOR_HANDOFF_FILE",
        legacy_env=_legacy("SUPERVISOR_HANDOFF_FILE") + ("OWLORY_SUPERVISOR_HANDOFF_FILE",),
    ),
    Setting(
        "supervisor_slice_id",
        "internal",
        "internal",
        "Exported to the agent: the slice it is running.",
        env="BOUNDROOM_SUPERVISOR_SLICE_ID",
        legacy_env=_legacy("SUPERVISOR_SLICE_ID") + ("OWLORY_SUPERVISOR_SLICE_ID",),
    ),
    Setting(
        "claude_runner_mode",
        "internal",
        "internal",
        "Set for run_agent.sh: the permission mode the chosen profile resolved to.",
        env="BOUNDROOM_CLAUDE_RUNNER_MODE",
    ),
    Setting(
        "claude_runner_settings",
        "internal",
        "internal",
        "Set for run_agent.sh: the per-slice settings file, outside the repository.",
        env="BOUNDROOM_CLAUDE_RUNNER_SETTINGS",
    ),
    Setting(
        "claude_runner_budget_usd",
        "internal",
        "internal",
        "Set for run_agent.sh: the per-slice budget cap.",
        default="5",
        env="BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD",
    ),
    Setting(
        "sandbox_probe_command",
        "internal",
        "internal",
        "Diagnostic: the command the sandbox probe runs.",
        default="pwd && test -x ./executable && test -d .git",
        env="BOUNDROOM_SANDBOX_PROBE_COMMAND",
        legacy_env=_legacy("SANDBOX_PROBE_COMMAND"),
    ),
    Setting(
        "sandbox_probe_barrier",
        "internal",
        "internal",
        "Diagnostic: the sandbox probe's barrier directory.",
        default="",
        env="BOUNDROOM_SANDBOX_PROBE_BARRIER",
    ),
    Setting(
        "sandbox_probe_barrier_seconds",
        "internal",
        "internal",
        "Diagnostic: how long the sandbox probe waits at its barrier.",
        default="15",
        env="BOUNDROOM_SANDBOX_PROBE_BARRIER_SECONDS",
    ),
    Setting(
        "unlisted_sentinel",
        "internal",
        "internal",
        "Test hook: a variable the benchmark must not pass to an agent.",
        env="BOUNDROOM_UNLISTED_SENTINEL",
    ),
)

# Fields of the repository's files that are not settings, and why.
NOT_SETTINGS: dict[str, str] = {
    "queue.version": "schema version",
    "queue.slices": "work items",
    "overlay.version": "schema version",
    "overlay.policy": "a container; its fields are listed one by one",
    "queue.policy.telemetry": "a container; its fields are listed one by one",
    "overlay.policy.telemetry": "a container; its fields are listed one by one",
    "overlay.approved": "work items",
    "memory.version": "schema version",
    "memory.budgets": "a container; its fields are listed one by one",
}

BY_ID: dict[str, Setting] = {setting.id: setting for setting in SETTINGS}


def flags_for(entry_point: EntryPoint) -> dict[str, Setting]:
    """Every option spelling an entry point accepts, mapped to its setting."""
    return {
        option: setting
        for setting in SETTINGS
        for flag in setting.flags
        if flag.entry_point == entry_point
        for option in flag.options
    }


def env_names() -> dict[str, Setting]:
    """Every variable name, new or legacy, mapped to its setting."""
    return {name: setting for setting in SETTINGS for name in setting.env_names}


def repository_fields() -> dict[str, Setting]:
    return {field: setting for setting in SETTINGS for field in setting.repository_fields}


# --- the resolver (RA-SET-002) -------------------------------------------------------------------


@dataclass(frozen=True)
class Sources:
    """What one invocation can read a setting from. Every mapping is optional.

    ``flags`` is the parsed command line (``vars(args)``), ``repository`` maps the dotted
    paths in ``Setting.repository_fields`` to values (see ``repository_values``), and
    ``operator`` maps ``<file>:<key>`` to values from the operator's files.
    """

    entry_point: Optional[EntryPoint] = None
    flags: Mapping[str, Any] = field(default_factory=dict)
    environ: Mapping[str, str] = field(default_factory=dict)
    operator: Mapping[str, Any] = field(default_factory=dict)
    repository: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    """One source in a setting's precedence chain, whether it was set, and what happened to it."""

    source: str  # kill switch, flag, env, legacy env, operator file, repository or default
    name: str  # the option, variable, file key or field
    is_set: bool
    value: Any = None
    note: str = ""


@dataclass(frozen=True)
class Resolution:
    setting: Setting
    value: Any
    source: str
    chain: tuple[Step, ...]
    refused: tuple[str, ...] = ()
    legacy_in_effect: Optional[str] = None

    @property
    def is_default(self) -> bool:
        return self.source == "default"


def _dest(option: str) -> str:
    return option.lstrip("-").replace("-", "_")


def _is_set(value: Any) -> bool:
    # argparse gives None for an absent option and False for an absent store_true. An empty string
    # counts as absent too, as `args.x or fallback` and the shell's ${X:-default} both treat it.
    return value is not None and value is not False and value != ""


def repository_values(
    queue_policy: Optional[Mapping[str, Any]] = None,
    overlay: Optional[Mapping[str, Any]] = None,
    memory: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Flatten the repository's files into the dotted paths the registry names."""
    roots: dict[str, Any] = {"queue": {"policy": queue_policy or {}}, "overlay": overlay or {}, "memory": memory or {}}
    values: dict[str, Any] = {}
    for path in repository_fields():
        node: Any = roots
        for part in path.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        if node is not None:
            values[path] = node
    return values


def _narrower(setting: Setting, candidate: Any, current: Any) -> bool:
    """Whether ``candidate`` is a strictly lower level than ``current`` in the setting's choices."""
    if not setting.choices or candidate not in setting.choices or current not in setting.choices:
        return False
    return setting.choices.index(candidate) < setting.choices.index(current)


def resolve(setting_id: str, sources: Sources) -> Resolution:
    """A setting's effective value, the source that decided it, and its whole precedence chain.

    Kill switch, then flags, then the ``BOUNDROOM_*`` variable, then its legacy names (read only
    when the new name is unset, SPEC-005), then the operator's file, then the repository, then
    the default. A ``narrow-only`` repository value is applied last, as a cap: it wins only when
    it is lower than what the other sources chose, and is refused otherwise.
    """
    setting = BY_ID[setting_id]
    chain: list[Step] = []
    candidates: list[tuple[str, Any]] = []
    legacy_in_effect: Optional[str] = None

    for flag in setting.flags:
        if sources.entry_point is not None and flag.entry_point != sources.entry_point:
            continue
        value = sources.flags.get(_dest(flag.options[0]))
        chain.append(Step("flag", flag.options[0], _is_set(value), value if _is_set(value) else None))
        if _is_set(value):
            candidates.append(("flag", value))
    if setting.env:
        value = sources.environ.get(setting.env)
        chain.append(Step("env", setting.env, _is_set(value), value, "empty: treated as unset" if value == "" else ""))
        if _is_set(value):
            candidates.append(("env", value))
    new_is_set = bool(setting.env) and setting.env in sources.environ
    for name in setting.legacy_env:
        value = sources.environ.get(name)
        if not _is_set(value):
            chain.append(Step("legacy env", name, False))
        elif new_is_set or legacy_in_effect:
            chain.append(Step("legacy env", name, True, value, "ignored: a newer name is set"))
        else:
            chain.append(Step("legacy env", name, True, value, f"use {setting.env}" if setting.env else ""))
            candidates.append(("legacy env", value))
            legacy_in_effect = name
    if setting.operator_file:
        value = sources.operator.get(setting.operator_file)
        chain.append(Step("operator file", setting.operator_file, value is not None, value))
        if value is not None:
            candidates.append(("operator file", value))
    repository: list[tuple[str, Any]] = [
        (path, sources.repository[path]) for path in setting.repository_fields if path in sources.repository
    ]
    if setting.repository_rule == "any":
        for path, value in repository:
            chain.append(Step("repository", path, True, value))
            candidates.append(("repository", value))
    chain.append(Step("default", "default", True, setting.default))

    source, value = candidates[0] if candidates else ("default", setting.default)
    if candidates and legacy_in_effect and source != "legacy env":
        legacy_in_effect = None

    refused: list[str] = []
    if setting.repository_rule == "narrow-only":
        for path, candidate in repository:
            if _narrower(setting, candidate, value):
                chain.append(Step("repository", path, True, candidate, "applied: the repository may lower this"))
                source, value = "repository", candidate
            elif candidate == value:
                chain.append(Step("repository", path, True, candidate, "same as the effective value"))
            else:
                reason = f"{path}={candidate!r} refused: the repository may only lower {setting.id}"
                chain.append(Step("repository", path, True, candidate, reason))
                refused.append(reason)

    if setting.kill_switch:
        kill = resolve(setting.kill_switch, sources)
        env_off = setting.env is not None and sources.environ.get(setting.env) == setting.off_value
        if kill.value is True or env_off:
            name = kill.chain[0].name if kill.value is True else f"{setting.env}={setting.off_value}"
            chain.insert(0, Step("kill switch", name, True, setting.off_value, "always wins"))
            source, value = "kill switch", setting.off_value

    return Resolution(setting, value, source, tuple(chain), tuple(refused), legacy_in_effect)
