"""Read-only views of every setting: quick, ``--all``, ``--json`` and ``--explain`` (SPEC-008 RA-SET-003).

    python3 automation/supervisor/settings_view.py            the quick view
    python3 automation/supervisor/settings_view.py --all      every setting, grouped by area
    python3 automation/supervisor/settings_view.py --json     the same data, for tools
    python3 automation/supervisor/settings_view.py --explain runner

Nothing here writes, connects to the network, or prints a secret: a secret shows only as set or
not set with the name that holds it, and a URL shows only its scheme and host. A source that can't
be read is reported in the view, not fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import runner_profile, settings, telemetry_settings

QUICK = (
    ("runner", "runner"),
    ("claude profile", "claude_profile"),
    ("slices per run", "autonomous_limit"),
    ("agent command", "agent_command"),
    ("work source", "board"),
    ("memory", "memory_endpoint"),
    ("telemetry", "telemetry_mode"),
)
URL_SETTINGS = {"telemetry_endpoint", "memory_endpoint"}
AREA_ORDER = ("runner", "work source", "policy", "sync", "memory", "telemetry", "evaluate", "projection", "status", "internal")


def _host_only(value: Any) -> Any:
    if not isinstance(value, str) or "://" not in value:
        return value
    parts = urlsplit(value)
    return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")


def shown(setting: settings.Setting, value: Any, environ: Mapping[str, str]) -> Any:
    """What a view may print for a value. Secrets and URLs are reduced here, in one place."""
    if setting.secret:
        if setting.id == "memory_api_key_env":
            # The value is the variable's name, which may be shown; its contents may not.
            return None if value is None else f"{value} ({'set' if environ.get(str(value)) else 'not set'})"
        return "set" if value not in (None, {}, "") else "not set"
    if setting.id in URL_SETTINGS:
        return _host_only(value)
    return value


def gather(
    repo_root: Path, environ: Mapping[str, str], overlay: Optional[Path], memory_config: Optional[Path]
) -> tuple[settings.Sources, list[str]]:
    """Every source a view reads, and a note for each one that couldn't be read."""
    notes: list[str] = []
    queue_policy: Optional[dict[str, Any]] = None
    queue_path = repo_root / "automation/queue/slices.json"
    if queue_path.exists():
        try:
            queue_policy = json.loads(queue_path.read_text(encoding="utf-8")).get("policy")
        except (OSError, ValueError) as error:
            notes.append(f"could not read {queue_path}: {error}")
    overlay_data = _read_json(overlay, notes) if overlay else None
    memory_data = _read_json(memory_config, notes) if memory_config else None
    operator: dict[str, Any] = {}
    path = telemetry_settings.DEFAULT_OPERATOR_FILE.expanduser()
    loaded, problem = telemetry_settings.load_operator_file(path)
    if problem:
        notes.append(problem)
    if loaded is not None:
        operator = {f"telemetry.json:{key}": value for key, value in loaded.model_dump().items() if value is not None}
    repository = settings.repository_values(queue_policy, overlay_data, memory_data)
    return settings.Sources(environ=dict(environ), operator=operator, repository=repository), notes


def _read_json(path: Path, notes: list[str]) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        notes.append(f"could not read {path}: {error}")
        return None


def warnings(resolutions: Mapping[str, settings.Resolution], environ: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    for resolution in resolutions.values():
        if resolution.legacy_in_effect:
            found.append(f"{resolution.legacy_in_effect} is set; use {resolution.setting.env}")
        found += resolution.refused
    if resolutions["sync_allow_unverified_source"].value:
        found.append("--allow-unverified-source skips every source check; AGENTS.md forbids agents from using it")
    profile = resolutions["claude_profile"]
    if profile.value is None and resolutions["claude_permission_mode"].value == "bypassPermissions":
        found.append("a Claude runner starts in bypassPermissions; choose a profile with BOUNDROOM_CLAUDE_PROFILE")
    elif profile.value is not None:
        try:
            chosen = runner_profile.choose({}, environ, runner_profile.parent_launch_mode())
            if chosen.effective == "unbounded":
                found.append("the Claude runner profile is unbounded: permission checks are bypassed")
        except runner_profile.ProfileRefused as error:
            found.append(f"the Claude runner profile would be refused: {error}")
    memory_key = resolutions["memory_api_key_env"].value
    if resolutions["memory_endpoint"].value and memory_key and not environ.get(str(memory_key)):
        found.append(f"memory is configured but {memory_key} is not set")
    if resolutions["telemetry_mode"].value == "otlp" and not resolutions["telemetry_endpoint"].value:
        found.append("telemetry otlp mode has no endpoint, so telemetry is off")
    if resolutions["telemetry_mode"].value == "file" and not resolutions["telemetry_file_dir"].value:
        found.append("telemetry file mode has no file_dir, so telemetry is off")
    return found


def resolve_all(sources: settings.Sources) -> dict[str, settings.Resolution]:
    return {setting.id: settings.resolve(setting.id, sources) for setting in settings.SETTINGS}


def row(resolution: settings.Resolution, environ: Mapping[str, str]) -> dict[str, Any]:
    setting = resolution.setting
    return {
        "id": setting.id,
        "area": setting.area,
        "class": setting.klass,
        "value": shown(setting, resolution.value, environ),
        "source": resolution.source,
        "default": shown(setting, setting.default, environ),
        "choices": list(setting.choices) if setting.choices else None,
        "repository_rule": setting.repository_rule,
        "description": setting.description,
    }


def explain(resolution: settings.Resolution, environ: Mapping[str, str]) -> dict[str, Any]:
    setting = resolution.setting
    steps = [
        {
            "source": step.source,
            "name": step.name,
            "set": step.is_set,
            "value": shown(setting, step.value, environ) if step.is_set else None,
            "note": step.note,
        }
        for step in resolution.chain
    ]
    return row(resolution, environ) | {"chain": steps, "refused": list(resolution.refused)}


def quick_view(resolutions: Mapping[str, settings.Resolution], environ: Mapping[str, str], notes: list[str]) -> str:
    lines = ["Boundroom settings (quick view)          run with --all for everything"]

    def add(label: str, value: Any, source: str) -> None:
        text = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
        lines.append(f"  {label:<24} {text:<34} {source}")

    for label, setting_id in QUICK:
        resolution = resolutions[setting_id]
        value = shown(resolution.setting, resolution.value, environ)
        if setting_id == "claude_profile":
            if resolution.value is None:
                mode = resolutions["claude_permission_mode"]
                add(label, f"none: {mode.value}", _source(mode))
            else:
                try:
                    add(
                        label,
                        runner_profile.choose({}, environ, runner_profile.parent_launch_mode()).chain(),
                        _source(resolution),
                    )
                except runner_profile.ProfileRefused:
                    add(label, f"{resolution.value} (refused)", _source(resolution))
            continue
        if setting_id == "agent_command":
            value = "custom" if resolution.source in ("flag", "env") else ("from policy" if resolution.value else "default")
        if value is None:
            value = "not set"
        if setting_id == "board":
            value = f"queue {resolutions['queue'].value}" if value == "not set" else value
        if setting_id in ("memory_endpoint", "telemetry_mode") and value in ("not set", "off"):
            value = "off"
        add(label, value, _source(resolution))

    shown_ids = {setting_id for _, setting_id in QUICK} | {"claude_permission_mode", "queue"}
    extra = [
        resolution
        for setting_id, resolution in resolutions.items()
        if setting_id not in shown_ids and not resolution.is_default and resolution.setting.klass != "internal"
    ]
    for resolution in extra:
        add(resolution.setting.id.replace("_", " "), shown(resolution.setting, resolution.value, environ), _source(resolution))

    found = warnings(resolutions, environ) + notes
    if found:
        lines.append("Warnings")
        lines += [f"  ! {warning}" for warning in found]
    return "\n".join(lines) + "\n"


def _source(resolution: settings.Resolution) -> str:
    if resolution.is_default:
        return "default"
    winner = next((step for step in resolution.chain if step.source == resolution.source and step.is_set), None)
    return f"{resolution.source} {winner.name}" if winner else resolution.source


def full_view(resolutions: Mapping[str, settings.Resolution], environ: Mapping[str, str], notes: list[str]) -> str:
    lines: list[str] = []
    for area in AREA_ORDER:
        rows = [row(r, environ) for r in resolutions.values() if r.setting.area == area]
        if not rows:
            continue
        lines.append("Set by Boundroom, not by you" if area == "internal" else area.capitalize())
        for item in rows:
            lines.append(f"  {item['id']:<32} {str(item['value']):<28} {item['source']:<14} {item['class']}")
    found = warnings(resolutions, environ) + notes
    if found:
        lines.append("Warnings")
        lines += [f"  ! {warning}" for warning in found]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show every Boundroom setting, its value and where it came from. Read-only.")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--all", action="store_true", help="Every setting, grouped by area.")
    view.add_argument("--json", action="store_true", help="Every setting as JSON.")
    view.add_argument("--explain", metavar="ID", help="One setting's full precedence chain.")
    parser.add_argument("--kanban-overlay", type=Path, help="Read board settings from this overlay.")
    parser.add_argument("--memory-config", type=Path, help="Read memory settings from this configuration.")
    return parser


def main(argv: Optional[list[str]] = None, environ: Optional[Mapping[str, str]] = None, repo_root: Path = REPO_ROOT) -> int:
    args = build_parser().parse_args(argv)
    environ = dict(os.environ if environ is None else environ)
    sources, notes = gather(repo_root, environ, args.kanban_overlay, args.memory_config)
    resolutions = resolve_all(sources)
    if args.explain:
        if args.explain not in resolutions:
            print(f"unknown setting {args.explain!r}; --all lists them", file=sys.stderr)
            return 64
        print(json.dumps(explain(resolutions[args.explain], environ), indent=2, default=str))
    elif args.json:
        document = {
            "settings": [row(r, environ) for r in resolutions.values()],
            "warnings": warnings(resolutions, environ) + notes,
        }
        print(json.dumps(document, indent=2, default=str))
    elif args.all:
        print(full_view(resolutions, environ, notes), end="")
    else:
        print(quick_view(resolutions, environ, notes), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
