"""Which telemetry settings apply to a run, and where each came from (SPEC-006, RA-TEL-001).

Nothing here imports a telemetry SDK, opens a connection or writes a file: it only decides. The
exporter (RA-TEL-002) reads the result.

The order is the spec's:

1. the kill switch, ``--no-telemetry`` or ``BOUNDROOM_TELEMETRY=off``, which always wins;
2. flags;
3. ``BOUNDROOM_TELEMETRY_*`` variables;
4. the operator's file, ``~/.config/boundroom/telemetry.json``;
5. the default: off, with no destination.

The repository's ``telemetry`` block can only reduce, so it is applied last as a cap: it can turn
telemetry off, lower ``capture`` and ``runner``, or name the ``environment``, and nothing more. Its
model refuses anything else when the queue is loaded. ``OTEL_*`` and ``LOGFIRE_*`` variables are
never read, so a setting someone made for another tool can't send Boundroom's traces anywhere.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Optional
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator

from automation.schemas.models import Contract, Omittable

Mode = Literal["off", "file", "otlp"]
Capture = Literal["metadata", "content"]
RunnerLevel = Literal["off", "metadata", "content"]

MODES: tuple[str, ...] = ("off", "file", "otlp")
CAPTURES: tuple[str, ...] = ("metadata", "content")
RUNNER_LEVELS: tuple[str, ...] = ("off", "metadata", "content")
DEFAULT_OPERATOR_FILE = Path("~/.config/boundroom/telemetry.json")
LOOPBACK_HOSTS = {"localhost"}

ENV = {
    "mode": "BOUNDROOM_TELEMETRY",
    "file_dir": "BOUNDROOM_TELEMETRY_FILE_DIR",
    "endpoint": "BOUNDROOM_TELEMETRY_ENDPOINT",
    "headers": "BOUNDROOM_TELEMETRY_HEADERS",
    "capture": "BOUNDROOM_TELEMETRY_CAPTURE",
    "runner": "BOUNDROOM_TELEMETRY_RUNNER",
    "required": "BOUNDROOM_TELEMETRY_REQUIRED",
    "environment": "BOUNDROOM_TELEMETRY_ENVIRONMENT",
}
DEFAULTS: dict[str, Any] = {
    "mode": "off",
    "file_dir": None,
    "endpoint": None,
    "headers": {},
    "capture": "metadata",
    "runner": "off",
    "required": False,
    "environment": "local",
}
SETTINGS = tuple(DEFAULTS)
_RANK = {level: index for index, level in enumerate(RUNNER_LEVELS)}


def check_endpoint(url: str) -> str:
    """``https://``, or ``http://`` only to the user's own machine (UD-10)."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("telemetry endpoint must be an https:// URL")
    if parts.username or parts.password:
        raise ValueError("telemetry endpoint must not carry credentials; use headers")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ValueError("telemetry endpoint must use https:// unless it is localhost, 127.0.0.1 or [::1]")
    return url


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_headers(raw: str) -> dict[str, str]:
    """``name=value`` pairs separated by commas, as in ``BOUNDROOM_TELEMETRY_HEADERS``."""
    headers: dict[str, str] = {}
    for pair in filter(None, (part.strip() for part in raw.split(","))):
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError("telemetry headers must be name=value pairs")
        headers[name.strip()] = value.strip()
    return headers


class OperatorTelemetry(Contract):
    """``~/.config/boundroom/telemetry.json``, the operator's own choice."""

    mode: Annotated[Mode, Omittable]
    file_dir: Annotated[str, Field(min_length=1), Omittable]
    endpoint: Annotated[str, Omittable]
    headers: Annotated[dict[str, str], Omittable]
    capture: Annotated[Capture, Omittable]
    runner: Annotated[RunnerLevel, Omittable]
    required: Annotated[bool, Omittable]
    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$"), Omittable]

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        return check_endpoint(value)


@dataclass(frozen=True)
class TelemetrySettings:
    """The resolved settings. ``headers`` are secrets: ``repr`` and ``report`` never show them."""

    mode: str
    file_dir: Optional[str]
    # Paths and queries can carry tenant ids or tokens, so the endpoint stays out of repr too.
    endpoint: Optional[str] = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    capture: str
    runner: str
    required: bool
    environment: str
    sources: dict[str, str]
    messages: tuple[str, ...] = ()
    # Set when the run must not start: telemetry was required but can't be enabled.
    refusal: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def report(self, trace_id: Optional[str] = None) -> dict[str, Any]:
        """The decision report's ``telemetry`` block: no path, query, header or token."""
        destination = None
        if self.mode == "file":
            destination = "file"
        elif self.mode == "otlp" and self.endpoint:
            parts = urlsplit(self.endpoint)
            destination = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        return {
            "mode": self.mode,
            "destination": destination,
            "capture": self.capture,
            "runner": self.runner,
            "environment": self.environment,
            "trace_id": trace_id,
            "source": self.sources["mode"],
        }


def load_operator_file(path: Path) -> tuple[Optional[OperatorTelemetry], Optional[str]]:
    """The operator's file, or None and a message saying why it was ignored. Absent is not a problem."""
    path = path.expanduser()
    if not path.exists():
        return None, None
    try:
        return OperatorTelemetry.model_validate(json.loads(path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError, ValidationError) as error:
        first = str(error).splitlines()[0]
        return None, f"telemetry: ignoring {path}: {first}"


def _from_env(environ: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    problems: list[str] = []
    for setting, name in ENV.items():
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            values[setting] = _parse(setting, raw)
        except ValueError as error:
            problems.append(f"telemetry: ignoring {name}: {error}")
    return values, problems


def _parse(setting: str, raw: str) -> Any:
    choices = {"mode": MODES, "capture": CAPTURES, "runner": RUNNER_LEVELS}.get(setting)
    if choices and raw not in choices:
        raise ValueError(f"must be one of {', '.join(choices)}")
    if setting == "headers":
        return parse_headers(raw)
    if setting == "required":
        if raw not in ("true", "false"):
            raise ValueError("must be true or false")
        return raw == "true"
    if setting == "endpoint":
        return check_endpoint(raw)
    return raw


def _from_flags(flags: Mapping[str, Any]) -> dict[str, Any]:
    names = {
        "mode": "telemetry",
        "file_dir": "telemetry_file_dir",
        "endpoint": "telemetry_endpoint",
        "capture": "telemetry_capture",
        "runner": "telemetry_runner",
    }
    values = {setting: flags[name] for setting, name in names.items() if flags.get(name) is not None}
    if "endpoint" in values:
        values["endpoint"] = check_endpoint(values["endpoint"])
    if flags.get("telemetry_required"):
        values["required"] = True
    return values


def resolve(
    *,
    flags: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    operator_file: Optional[Path] = DEFAULT_OPERATOR_FILE,
    repository: Optional[Mapping[str, Any]] = None,
) -> TelemetrySettings:
    """Resolve the settings for one run. ``repository`` is the queue policy's validated block."""
    flags = flags or {}
    environ = environ or {}
    messages: list[str] = []

    layers: list[tuple[str, dict[str, Any]]] = [("flag", _from_flags(flags))]
    env_values, env_problems = _from_env(environ)
    messages += env_problems
    layers.append(("env", env_values))
    if operator_file is not None:
        operator, problem = load_operator_file(operator_file)
        if problem:
            messages.append(problem)
        if operator is not None:
            layers.append(("operator file", {key: value for key, value in operator.model_dump().items() if value is not None}))

    values: dict[str, Any] = dict(DEFAULTS)
    sources = {setting: "default" for setting in SETTINGS}
    for setting in SETTINGS:
        for source, layer in layers:
            if setting in layer:
                values[setting], sources[setting] = layer[setting], source
                break

    for setting, value in (repository or {}).items():
        if value is None:
            continue
        if setting == "environment":
            if sources["environment"] == "default":
                values["environment"], sources["environment"] = value, "repository"
        elif setting == "mode" or (setting == "capture" and values["capture"] != value):
            values[setting], sources[setting] = value, "repository"
        elif setting == "runner" and _RANK[value] < _RANK[values["runner"]]:
            values["runner"], sources["runner"] = value, "repository"

    if flags.get("no_telemetry") or environ.get(ENV["mode"]) == "off":
        values["mode"], sources["mode"] = "off", "kill switch"

    refusal = None
    missing = {"file": "file_dir", "otlp": "endpoint"}.get(values["mode"])
    if missing and not values[missing]:
        message = f"telemetry: {values['mode']} mode needs {missing}; telemetry is off"
        messages.append(message)
        if values["required"]:
            refusal = message.replace("; telemetry is off", "; refusing to start because telemetry is required")
        values["mode"], sources["mode"] = "off", f"missing {missing}"
    elif values["required"] and values["mode"] == "off" and sources["mode"] in ("repository", "kill switch"):
        # The operator asked for telemetry to be required, and something turned it off. Starting
        # anyway would run unobserved, which is what required exists to prevent.
        refusal = f"telemetry: required, but turned off by the {sources['mode']}; refusing to start"

    return TelemetrySettings(
        mode=values["mode"],
        file_dir=values["file_dir"],
        endpoint=values["endpoint"],
        headers=dict(values["headers"]),
        capture=values["capture"],
        runner=values["runner"],
        required=values["required"],
        environment=values["environment"],
        sources=sources,
        messages=tuple(messages),
        refusal=refusal,
    )


def add_flags(parser: Any) -> None:
    """The telemetry flags ``run_next.py``, ``evaluate_handoff.py`` and the sync tool share. Headers have none."""
    group = parser.add_argument_group("telemetry (SPEC-006; off unless you choose a mode and a destination)")
    group.add_argument("--telemetry", choices=MODES, help="off, file or otlp.")
    group.add_argument("--no-telemetry", action="store_true", help="Turn telemetry off, whatever any other source says.")
    group.add_argument("--telemetry-file-dir", help="Absolute directory outside the repository, for file mode.")
    group.add_argument("--telemetry-endpoint", help="Your OTLP endpoint: https://, or http:// to localhost only.")
    group.add_argument("--telemetry-capture", choices=CAPTURES, help="metadata (default) or content.")
    group.add_argument("--telemetry-runner", choices=RUNNER_LEVELS, help="Whether the runner's telemetry joins the trace.")
    group.add_argument("--telemetry-required", action="store_true", help="Refuse to start if telemetry can't be enabled.")
