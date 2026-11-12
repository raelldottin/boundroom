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
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ContextManager, Generator, Sequence
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


def configure(settings: TelemetrySettings, *, repo_root: Path, run_id: str, service_version: str = "") -> Telemetry:
    """Build the run's telemetry handle. Raises TelemetryRefused only when ``required`` is set."""
    handle = Telemetry(settings=settings, run_id=run_id)
    if not settings.enabled:
        return handle
    try:
        processor, data_dir = _exporter(settings, repo_root, run_id)
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


def _exporter(settings: TelemetrySettings, repo_root: Path, run_id: str) -> tuple[Any, str]:
    """The span processor for the user's destination, and a Logfire data_dir outside the repository."""
    try:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except ImportError as error:
        raise _Unavailable("telemetry: the opentelemetry SDK isn't installed (pip install 'boundroom[telemetry]')") from error
    if settings.mode == "file":
        file_dir = Path(str(settings.file_dir))
        if not file_dir.is_absolute():
            raise _Unavailable(f"telemetry: file_dir must be an absolute path, not {file_dir}")
        if _inside(file_dir, repo_root):
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
