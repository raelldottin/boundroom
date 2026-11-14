"""Telemetry for the A-E experiment (SPEC-006 RA-TEL-005): one setting for every lane, unseen by the agent.

The experiment is traced as ``boundroom.benchmark.experiment``, one ``cell`` per (lane, repeat,
instance), one ``phase`` per agent session with its ``agent_session`` inside, and one ``score``
per scored ``<lane>/r<repeat>`` directory. Spans are metadata only. Nothing here writes to the
prompt, the context file, the command, the agent's environment or the workspace, so a lane's
treatment is byte-for-byte the same with telemetry on or off.

Off is the default and costs nothing: no SDK import, no file and no digest is computed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, ContextManager, Generator, Mapping, Optional

from automation.supervisor import egress
from automation.supervisor.telemetry import Telemetry
from automation.supervisor.telemetry_settings import ENV, TelemetrySettings, resolve

ENVIRONMENT = "benchmark"
RUNNER_REFUSAL = (
    "telemetry: the benchmark keeps runner telemetry off, because it would add variables and an "
    "exporter to the agent session being measured"
)


def benchmark_settings(flags: Mapping[str, Any], environ: Mapping[str, str]) -> TelemetrySettings:
    """The settings this experiment's own command line chose, and nothing a live run set up.

    The operator file and ``BOUNDROOM_TELEMETRY_*`` configure live runs. Reading them here would
    turn telemetry on for a benchmark a person approved without it, and send its spans to a live
    destination, which AGENTS.md's isolation rule forbids. Two variables still apply: the kill
    switch, which can only turn telemetry off, and the headers, which have no flag so that a token
    never sits in a process list, and which go only to the endpoint the command line named.

    The environment is always ``benchmark``, and the runner's own telemetry stays off: it would
    change the agent's environment between a traced and an untraced run.
    """
    passed: dict[str, str] = {}
    if environ.get(ENV["mode"]) == "off":
        passed[ENV["mode"]] = "off"
    if environ.get(ENV["headers"]):
        passed[ENV["headers"]] = environ[ENV["headers"]]
    # The offline switch can only refuse a destination, so it applies here too (RA-TEL-011).
    if egress.OFFLINE_ENV in environ:
        passed[egress.OFFLINE_ENV] = environ[egress.OFFLINE_ENV]
    settings = resolve(flags=flags, environ=passed, operator_file=None)
    refusal = settings.refusal
    if flags.get("telemetry_runner") not in (None, "off"):
        refusal = RUNNER_REFUSAL
    return dataclasses.replace(
        settings,
        runner="off",
        environment=ENVIRONMENT,
        sources={**settings.sources, "runner": ENVIRONMENT, "environment": ENVIRONMENT},
        refusal=refusal,
    )


def untraced() -> Telemetry:
    """The handle an experiment gets when nobody passed one: off, and recorded as off."""
    return Telemetry(settings=benchmark_settings({}, {}), run_id="")


def enabled(handle: Optional[Telemetry]) -> bool:
    return handle is not None and handle.enabled


def span(handle: Optional[Telemetry], name: str, **attributes: Any) -> ContextManager[Any]:
    """A span, or nothing at all. None-valued attributes are left out: OpenTelemetry has no null."""
    if handle is None:
        return nullcontext()
    return handle.span(name, **{key: value for key, value in attributes.items() if value is not None})


def annotate(handle: Optional[Telemetry], target: Any, **attributes: Any) -> None:
    if handle is not None:
        handle.annotate(target, **{key: value for key, value in attributes.items() if value is not None})


def fail(handle: Optional[Telemetry], target: Any, code: str) -> None:
    """The ERROR status on a span, described by a reason code."""
    if handle is not None:
        handle.fail(target, code)


def metric(handle: Optional[Telemetry], name: str, value: float = 1, **attributes: Any) -> None:
    """One data point for a catalogued instrument (SPEC-006 RA-TEL-010), or nothing at all."""
    if handle is not None:
        handle.metric(name, value, **attributes)


@contextmanager
def recorded_run(handle: Optional[Telemetry]) -> Generator[None, None, None]:
    """The experiment's run counter and duration: ok when it returns, error when it raises."""
    started = time.monotonic()
    outcome = "error"
    try:
        yield
        outcome = "ok"
    finally:
        if handle is not None:
            handle.record_run("benchmark", outcome, time.monotonic() - started)


def file_sha256(path: Path) -> Optional[str]:
    """The digest of a record a span describes, so an auditor can check the one against the other."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


__all__ = [
    "ENVIRONMENT",
    "RUNNER_REFUSAL",
    "annotate",
    "benchmark_settings",
    "enabled",
    "fail",
    "file_sha256",
    "metric",
    "recorded_run",
    "span",
    "untraced",
]
