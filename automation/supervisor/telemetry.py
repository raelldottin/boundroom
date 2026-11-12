"""The telemetry exporter: a no-op unless the resolved settings turn it on (SPEC-006 RA-TEL-002).

``configure`` takes the settings ``telemetry_settings.resolve`` decided and returns a handle whose
``span`` is free when telemetry is off. Off means no SDK import, no file and no connection (UD-1).

When it is on, the Logfire SDK is imported lazily and configured only from those settings:
``send_to_logfire=False`` always (UD-3), no console, no metrics, no argument inspection, its
``data_dir`` outside the repository, and spans go only to Boundroom's own exporter for the user's
destination. Logfire reads ``OTEL_EXPORTER_OTLP_*`` from the environment while it configures and
would add an exporter for them, so those variables, and ``LOGFIRE_*``, are hidden from it for the
duration (UD-6). A setting someone made for another tool must not send Boundroom's traces anywhere.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager, Generator, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

from automation.supervisor.telemetry_settings import TelemetrySettings

SERVICE_NAME = "boundroom-supervisor"
FLUSH_TIMEOUT_SECONDS = 5
STARTUP_TIMEOUT_SECONDS = 5
AMBIENT_PREFIXES = ("OTEL_", "LOGFIRE_")


class TelemetryRefused(Exception):
    """Telemetry was required and can't be enabled; the run must stop before the claim."""


@dataclass
class Telemetry:
    """A handle for one run's telemetry. ``span`` costs nothing when telemetry is off."""

    settings: TelemetrySettings
    run_id: str
    _logfire: Any = None
    _processor: Any = None
    messages: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self._logfire is not None

    def span(self, name: str, **attributes: Any) -> ContextManager[Any]:
        if self._logfire is None:
            return nullcontext()
        return self._logfire.span(name, **attributes)

    def mark(self, name: str, **attributes: Any) -> None:
        """A span for a step that is over as soon as it is recorded: a selection, a claim, a verdict."""
        if self._logfire is not None:
            with self._logfire.span(name, **attributes):
                pass

    def annotate(self, span: Any, **attributes: Any) -> None:
        if span is not None and self._logfire is not None:
            for key, value in attributes.items():
                span.set_attribute(key, value)

    def flush(self) -> bool:
        """Flush for at most five seconds, then drop what's left and say so (bounded flush)."""
        if self._processor is None:
            return True
        flushed = bool(self._processor.force_flush(timeout_millis=FLUSH_TIMEOUT_SECONDS * 1000))
        if not flushed:
            print(f"telemetry: flush did not finish within {FLUSH_TIMEOUT_SECONDS}s; the rest was dropped", file=sys.stderr)
        self._processor.shutdown()
        self._processor = None
        return flushed


def configure(
    settings: TelemetrySettings,
    *,
    repo_root: Path,
    run_id: str,
    service_version: str = "",
    outside: Sequence[Path] = (),
) -> Telemetry:
    """Build the run's telemetry handle. Raises TelemetryRefused only when ``required`` is set.

    ``outside`` names more checkouts ``file_dir`` must stay out of. The sync tool writes into a
    consumer but reads a canonical source that must stay clean for its provenance check, so a
    spans file in either would break the next run.
    """
    handle = Telemetry(settings=settings, run_id=run_id)
    if not settings.enabled:
        return handle
    try:
        processor, data_dir = _exporter(settings, (repo_root, *outside), run_id)
        if settings.required:
            _startup_check(settings)
        logfire = _import_logfire()
        with _hidden_ambient_settings():
            handle._logfire = logfire.configure(
                local=True,
                send_to_logfire=False,
                console=False,
                metrics=False,
                inspect_arguments=False,
                service_name=SERVICE_NAME,
                service_version=service_version or None,
                environment=settings.environment,
                data_dir=data_dir,
                additional_span_processors=[processor],
                distributed_tracing=False,
            )
        handle._processor = processor
    except TelemetryRefused:
        if settings.required:
            raise
        handle.messages.append("telemetry: could not be enabled; continuing with telemetry off")
    except _Unavailable as error:
        if settings.required:
            raise TelemetryRefused(f"{error}; refusing to start because telemetry is required") from error
        handle.messages.append(f"{error}; continuing with telemetry off")
    return handle


class _Unavailable(Exception):
    pass


def _import_logfire() -> Any:
    try:
        import logfire  # noqa: PLC0415 - imported only when telemetry is on (UD-1)
    except ImportError as error:
        raise _Unavailable("telemetry: the logfire package isn't installed (pip install 'boundroom[telemetry]')") from error
    return logfire


@contextmanager
def _hidden_ambient_settings() -> Generator[None, None, None]:
    hidden = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith(AMBIENT_PREFIXES)}
    try:
        yield
    finally:
        os.environ.update(hidden)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _exporter(settings: TelemetrySettings, roots: Sequence[Path], run_id: str) -> tuple[Any, str]:
    """The span processor for the user's destination, and a Logfire data_dir outside the repository."""
    try:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except ImportError as error:
        raise _Unavailable("telemetry: the opentelemetry SDK isn't installed (pip install 'boundroom[telemetry]')") from error
    if settings.mode == "file":
        file_dir = Path(str(settings.file_dir))
        if not file_dir.is_absolute():
            raise _Unavailable(f"telemetry: file_dir must be an absolute path, not {file_dir}")
        if any(_inside(file_dir, root) for root in roots):
            # A write there would dirty the worktree and break the clean stop.
            raise _Unavailable("telemetry: file_dir must be outside the repository")
        return BatchSpanProcessor(json_lines_exporter(file_dir / f"{run_id}.spans.jsonl")), str(file_dir / ".logfire")
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415

    exporter = OTLPSpanExporter(endpoint=traces_url(str(settings.endpoint)), headers=dict(settings.headers))
    return BatchSpanProcessor(exporter), tempfile.mkdtemp(prefix="boundroom-logfire-")


def traces_url(endpoint: str) -> str:
    """OTLP over HTTP takes the traces path; a bare host gets the standard one."""
    parts = urlsplit(endpoint)
    if parts.path in ("", "/"):
        return urlunsplit((parts.scheme, parts.netloc, "/v1/traces", parts.query, ""))
    return endpoint


def _startup_check(settings: TelemetrySettings) -> None:
    """With ``required``, prove the destination works before anything is claimed."""
    if settings.mode == "file":
        file_dir = Path(str(settings.file_dir))
        try:
            file_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=file_dir):
                pass
        except OSError as error:
            raise TelemetryRefused(f"telemetry: file_dir {file_dir} isn't writable: {error.strerror}") from error
        return
    import urllib.error  # noqa: PLC0415 - only a required otlp run probes its endpoint
    import urllib.request  # noqa: PLC0415

    request = urllib.request.Request(traces_url(str(settings.endpoint)), method="POST", data=b"", headers=dict(settings.headers))
    try:
        urllib.request.urlopen(request, timeout=STARTUP_TIMEOUT_SECONDS).close()
    except urllib.error.HTTPError:
        return  # it answered; an empty request may well be rejected
    except (urllib.error.URLError, OSError) as error:
        host = urlsplit(str(settings.endpoint)).hostname
        raise TelemetryRefused(f"telemetry: the endpoint at {host} didn't answer") from error


def json_lines_exporter(path: Path) -> Any:
    """A span exporter that writes each finished span as one JSON line, to the user's file only.

    The class is built here rather than at module level, so importing this module loads no
    OpenTelemetry code while telemetry is off (UD-1).
    """
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: PLC0415

    class JsonLinesExporter(SpanExporter):
        def export(self, spans: Sequence[Any]) -> SpanExportResult:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    for span in spans:
                        handle.write(json.dumps(_span_record(span), sort_keys=True, default=str) + "\n")
            except OSError:
                return SpanExportResult.FAILURE
            return SpanExportResult.SUCCESS

    return JsonLinesExporter()


def _span_record(span: Any) -> dict[str, Any]:
    context = span.get_span_context()
    parent = span.parent
    return {
        "name": span.name,
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
    }


def new_run_id() -> str:
    """A run id that sorts by time and can't collide between two runs started in the same second."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def version(repo_root: Path) -> str:
    """The Boundroom version from the repository's pyproject, when it has one; consumers may not."""
    try:
        text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def trace_id(span: Any) -> Optional[str]:
    """The trace id of the run's root span, for the decision report; None when telemetry is off."""
    if span is None:
        return None
    return f"{span.get_span_context().trace_id:032x}"


# The variables Claude Code reads to export its own telemetry. If the user exported any of them,
# they chose their runner's telemetry themselves: Boundroom then sets none of these, so it never
# adds to or redirects a choice the user made (UD-9).
RUNNER_DESTINATION_VARIABLES = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "OTEL_TRACES_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
)
# Exports the whole conversation. Boundroom never sets it, whatever the capture level.
NEVER_SET = "OTEL_LOG_RAW_API_BODIES"


@dataclass(frozen=True)
class RunnerTelemetry:
    variables: dict[str, str]  # what Boundroom adds to the runner's environment
    passthrough: tuple[str, ...]  # runner telemetry variables the user had already exported
    note: str = ""

    def report(self) -> dict[str, Any]:
        """Names only, never values: the headers among them are secrets."""
        return {
            "runner_variables": sorted(self.variables),
            "runner_passthrough": list(self.passthrough),
            "runner_note": self.note,
        }


def runner_environment(settings: TelemetrySettings, environ: Any, session: Any, run_id: str, slice_id: str) -> RunnerTelemetry:
    """What the runner gets, for its spans to nest under ``boundroom.agent_session`` (RA-TEL-004).

    Nothing at all while Boundroom telemetry is off (UD-1). With it on, the trace context always;
    the runner's own exporter only when ``runner`` isn't off and the destination is the user's
    OTLP endpoint (UD-9).
    """
    if not settings.enabled or session is None:
        return RunnerTelemetry({}, ())
    context = session.get_span_context()
    variables = {"TRACEPARENT": f"00-{context.trace_id:032x}-{context.span_id:016x}-01"}
    if "OTEL_RESOURCE_ATTRIBUTES" not in environ:
        variables["OTEL_RESOURCE_ATTRIBUTES"] = f"boundroom.run_id={run_id},boundroom.slice_id={slice_id}"
    passthrough = tuple(name for name in (*RUNNER_DESTINATION_VARIABLES, NEVER_SET) if name in environ)
    if settings.runner == "off":
        return RunnerTelemetry(variables, passthrough)
    if passthrough:
        return RunnerTelemetry(variables, passthrough, "the user set the runner's telemetry; Boundroom left it as it is")
    if settings.mode != "otlp":
        return RunnerTelemetry(
            variables, passthrough, "runner telemetry needs an otlp destination; file mode traces the supervisor only"
        )
    variables |= {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": str(settings.endpoint),
    }
    if settings.headers:
        variables["OTEL_EXPORTER_OTLP_HEADERS"] = ",".join(f"{name}={value}" for name, value in settings.headers.items())
    if settings.runner == "content":
        variables |= {"OTEL_LOG_USER_PROMPTS": "1", "OTEL_LOG_TOOL_DETAILS": "1"}
    return RunnerTelemetry(variables, passthrough)
