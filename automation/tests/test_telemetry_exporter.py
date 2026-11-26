"""The telemetry exporter: off costs nothing, on goes only where the user said (SPEC-006 RA-TEL-002).

Covers UD-1 (off imports, writes and connects to nothing), UD-3 (no built-in destination and
``send_to_logfire=False``), UD-4 (otlp connects only to the user's endpoint), UD-6 (ambient OTEL_*
and LOGFIRE_* change nothing), the missing-SDK warning or refusal, the file_dir-in-repository
refusal and the bounded flush.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

from automation.supervisor import telemetry, telemetry_metrics
from automation.supervisor.telemetry_settings import TelemetrySettings, resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_HOSTS = re.compile(
    r"logfire[-a-z]*\.pydantic\.dev|honeycomb\.io|datadoghq\.|newrelic\.com|grafana\.net|lightstep\.com|signoz\.cloud|sentry\.io"
)


class Sink:
    """A local OTLP/HTTP endpoint that records each request's path, headers and body.

    ``status`` answers the paths it names with that status instead of 200. ``on_request`` is
    called with each request's path before the request is answered.
    """

    def __init__(self, status: Optional[dict[str, int]] = None, on_request: Optional[Callable[[str], None]] = None) -> None:
        received: list[tuple[str, dict[str, str], bytes]] = []
        statuses = dict(status or {})

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                received.append((self.path, {key.lower(): value for key, value in self.headers.items()}, body))
                if on_request is not None:
                    on_request(self.path)
                self.send_response(statuses.get(self.path, 200))
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's name
                return None

        self.received = received
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ExporterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-exporter-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def settings(self, **flags: object):
        return resolve(flags=flags, environ={}, operator_file=None)

    def file_settings(self, **extra: object):
        return self.settings(telemetry="file", telemetry_file_dir=str(self.tmp / "spans"), **extra)


class OffTests(ExporterCase):
    def test_ud1_off_imports_writes_and_connects_to_nothing(self) -> None:
        code = """
import socket, sys
def refuse(*a, **k): raise AssertionError("telemetry opened a socket")
socket.socket.connect = refuse; socket.socket.connect_ex = refuse; socket.create_connection = refuse
from pathlib import Path
from automation.supervisor import telemetry
from automation.supervisor.telemetry_settings import resolve
handle = telemetry.configure(resolve(environ={}, operator_file=None), repo_root=Path(sys.argv[1]), run_id="r1")
with handle.span("anything", x=1):
    handle.metric("boundroom.runs", entry="run_next", outcome="ok")
    handle.record_run("run_next", "ok", 0.1)
assert handle.flush()
loaded = [m for m in sys.modules if m.startswith(("logfire", "opentelemetry", "urllib.request", "http.client"))]
assert not loaded, loaded
import os
assert "PYDANTIC_DISABLE_PLUGINS" not in os.environ, "the plugin switch leaked into the environment"
print("clean")
"""
        before = sorted(self.tmp.rglob("*"))
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.tmp)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.assertEqual("clean", result.stdout.strip(), result.stderr)
        self.assertEqual(before, sorted(self.tmp.rglob("*")))


class FileModeTests(ExporterCase):
    def test_spans_go_to_one_json_lines_file_per_run_with_their_nesting(self) -> None:
        handle = telemetry.configure(self.file_settings(), repo_root=REPO_ROOT, run_id="run-1")
        self.assertTrue(handle.enabled)
        with handle.span("run_next"):
            with handle.span("claim", slice_id="S-1"):
                pass
        self.assertTrue(handle.flush())
        records = [json.loads(line) for line in (self.tmp / "spans/run-1.spans.jsonl").read_text(encoding="utf-8").splitlines()]
        by_name = {record["name"]: record for record in records}
        self.assertEqual(by_name["run_next"]["span_id"], by_name["claim"]["parent_span_id"])
        self.assertEqual(by_name["run_next"]["trace_id"], by_name["claim"]["trace_id"])
        self.assertEqual("S-1", by_name["claim"]["attributes"]["slice_id"])

    def test_a_file_dir_inside_the_repository_is_refused(self) -> None:
        inside = self.settings(telemetry="file", telemetry_file_dir=str(REPO_ROOT / "spans"))
        handle = telemetry.configure(inside, repo_root=REPO_ROOT, run_id="r")
        self.assertFalse(handle.enabled)
        self.assertIn("outside the repository", handle.messages[0])
        required = self.settings(telemetry="file", telemetry_file_dir=str(REPO_ROOT / "spans"), telemetry_required=True)
        with self.assertRaises(telemetry.TelemetryRefused):
            telemetry.configure(required, repo_root=REPO_ROOT, run_id="r")
        self.assertFalse((REPO_ROOT / "spans").exists())

    def test_required_checks_the_file_dir_is_writable_before_starting(self) -> None:
        blocked = self.tmp / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        settings = self.settings(telemetry="file", telemetry_file_dir=str(blocked / "spans"), telemetry_required=True)
        with self.assertRaises(telemetry.TelemetryRefused):
            telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r")


class SdkConfigurationTests(ExporterCase):
    # How the SDK is configured is judged by what it writes, in test_telemetry_core: the exact
    # resource, nothing on stdout, no connection, and ambient OTEL_* settings that change nothing.

    def test_logfires_real_default_views_lose_only_the_exponential_histogram(self) -> None:
        import logfire  # noqa: PLC0415
        from opentelemetry.sdk.metrics.view import (  # noqa: PLC0415
            ExplicitBucketHistogramAggregation,
            ExponentialBucketHistogramAggregation,
        )

        defaults = logfire.MetricsOptions.DEFAULT_VIEWS
        views = telemetry._metric_views(logfire)
        self.assertFalse(any(isinstance(view._aggregation, ExponentialBucketHistogramAggregation) for view in views))
        others = [view for view in defaults if not isinstance(view._aggregation, ExponentialBucketHistogramAggregation)]
        self.assertEqual(others, views[:-1])
        explicit = views[-1]
        self.assertIsInstance(explicit._aggregation, ExplicitBucketHistogramAggregation)
        self.assertEqual("boundroom.*", explicit._instrument_name)
        self.assertEqual(telemetry_metrics.DURATION_BUCKETS_SECONDS, tuple(explicit._aggregation._boundaries))

    def test_without_the_sdk_a_warning_or_with_required_a_refusal(self) -> None:
        with mock.patch.dict(sys.modules, {"logfire": None}):
            handle = telemetry.configure(self.file_settings(), repo_root=REPO_ROOT, run_id="r")
            self.assertFalse(handle.enabled)
            self.assertIn("logfire package isn't installed", handle.messages[0])
            with self.assertRaises(telemetry.TelemetryRefused):
                telemetry.configure(self.file_settings(telemetry_required=True), repo_root=REPO_ROOT, run_id="r")

    def test_ud3_no_telemetry_vendor_host_appears_in_code_or_skills(self) -> None:
        for root in ("automation", "Tools", "plugins"):
            for path in (REPO_ROOT / root).rglob("*"):
                if path.suffix in {".py", ".sh", ".md", ".json"} and path.is_file() and path.name != Path(__file__).name:
                    with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                        self.assertIsNone(VENDOR_HOSTS.search(path.read_text(encoding="utf-8", errors="ignore")))

    def test_ud8_skill_text_names_no_telemetry_endpoint_host_or_token(self) -> None:
        allowed = {"https://github.com/raelldottin/boundroom.git"}
        for skill in (REPO_ROOT / "plugins/boundroom/skills").glob("*/SKILL.md"):
            text = skill.read_text(encoding="utf-8")
            with self.subTest(skill=skill.parent.name):
                self.assertEqual(set(), set(re.findall(r"https?://[^\s)`\"']+", text)) - allowed)
                self.assertNotRegex(text, r"(?i)bearer\s+\S|authorization=")

    def test_traces_url_adds_the_standard_path_to_a_bare_endpoint(self) -> None:
        self.assertEqual("http://127.0.0.1:4318/v1/traces", telemetry.traces_url("http://127.0.0.1:4318"))
        self.assertEqual("https://c.example.test/custom", telemetry.traces_url("https://c.example.test/custom"))


class EgressTests(ExporterCase):
    def setUp(self) -> None:
        super().setUp()
        self.mine = Sink()
        self.ambient = Sink()
        self.addCleanup(self.mine.close)
        self.addCleanup(self.ambient.close)

    def test_ud4_and_ud6_spans_reach_only_the_users_endpoint(self) -> None:
        connections: list[tuple[str, int]] = []
        real = socket.socket.connect

        def recording(sock: socket.socket, address, *args, **kwargs):  # type: ignore[no-untyped-def]
            connections.append((address[0], int(address[1])))
            return real(sock, address, *args, **kwargs)

        ambient = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.ambient.url,
            "OTEL_TRACES_EXPORTER": "otlp",
            "LOGFIRE_SEND_TO_LOGFIRE": "true",
        }
        settings = self.settings(telemetry="otlp", telemetry_endpoint=self.mine.url)
        with mock.patch.dict(os.environ, ambient), mock.patch.object(socket.socket, "connect", recording):
            handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r")
            self.assertEqual(self.ambient.url, os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"], "the environment is restored")
            with handle.span("run_next"):
                pass
            self.assertTrue(handle.flush())
        self.assertTrue(self.mine.received, "no spans reached the user's endpoint")
        self.assertEqual({"/v1/traces"}, {path for path, _, _ in self.mine.received})
        self.assertEqual([], self.ambient.received, "spans reached an endpoint set for another tool")
        port = int(self.mine.url.rsplit(":", 1)[1])
        self.assertEqual({("127.0.0.1", port)}, set(connections))

    def test_ud6_ambient_settings_with_telemetry_off_send_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": self.ambient.url, "OTEL_TRACES_EXPORTER": "otlp"}):
            handle = telemetry.configure(resolve(environ=dict(os.environ), operator_file=None), repo_root=REPO_ROOT, run_id="r")
            with handle.span("run_next"):
                pass
            handle.flush()
        self.assertEqual([], self.ambient.received)


class FakeClock:
    """The bounded flush's clock, moved only by the test."""

    def __init__(self) -> None:
        self.now = 1e9  # far from any real monotonic reading

    def __call__(self) -> float:
        return self.now


class BoundedFlushTests(ExporterCase):
    """Spans first; metrics get whatever time the spans left of the five seconds."""

    def flush_after_traces_take(self, seconds: float) -> tuple[bool, str, Sink]:
        clock = FakeClock()

        def slow_traces(path: str) -> None:
            if path == "/v1/traces":
                clock.now += seconds

        sink = Sink(on_request=slow_traces)
        self.addCleanup(sink.close)
        settings = self.settings(telemetry="otlp", telemetry_endpoint=sink.url)
        handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r", clock=clock)
        with handle.span("boundroom.run"):
            handle.record_run("run_next", "ok", 0.2)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            flushed = handle.flush()
        return flushed, stderr.getvalue(), sink

    def test_spans_that_take_all_five_seconds_leave_no_time_for_metrics(self) -> None:
        flushed, said, sink = self.flush_after_traces_take(5.0)
        self.assertIs(False, flushed)
        self.assertEqual(["telemetry: no time was left to send metrics; they were dropped"], said.splitlines())
        self.assertEqual(["/v1/traces"], [path for path, _, _ in sink.received])

    def test_spans_that_leave_a_second_let_the_metrics_go(self) -> None:
        flushed, said, sink = self.flush_after_traces_take(4.0)
        self.assertTrue(flushed)
        self.assertEqual("", said)
        self.assertEqual(["/v1/traces", "/v1/metrics"], [path for path, _, _ in sink.received])

    def test_an_endpoint_that_refuses_the_metrics_is_said(self) -> None:
        sink = Sink(status={"/v1/metrics": 400})
        self.addCleanup(sink.close)
        handle = telemetry.configure(
            self.settings(telemetry="otlp", telemetry_endpoint=sink.url), repo_root=REPO_ROOT, run_id="r"
        )
        with handle.span("boundroom.run"):
            handle.record_run("run_next", "ok", 0.2)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(handle.flush())
        self.assertIn("telemetry: the endpoint didn't take the metrics; they were dropped", stderr.getvalue().splitlines())


class StartupProbeTests(ExporterCase):
    """With ``required``, otlp proves the endpoint answers before anything is claimed."""

    def otlp(self, url: str, required: bool) -> TelemetrySettings:
        flags = {"telemetry": "otlp", "telemetry_endpoint": url, "telemetry_required": required}
        return resolve(flags=flags, environ={"BOUNDROOM_TELEMETRY_HEADERS": "authorization=Bearer probe"}, operator_file=None)

    def test_required_posts_an_empty_request_with_the_headers_to_the_traces_path(self) -> None:
        sink = Sink()
        self.addCleanup(sink.close)
        handle = telemetry.configure(self.otlp(sink.url, True), repo_root=REPO_ROOT, run_id="r")
        self.assertTrue(handle.enabled)
        ((path, headers, body),) = sink.received
        self.assertEqual(("/v1/traces", "Bearer probe", b""), (path, headers["authorization"], body))

    def test_an_endpoint_that_rejects_the_empty_request_still_answered(self) -> None:
        sink = Sink(status={"/v1/traces": 405})
        self.addCleanup(sink.close)
        handle = telemetry.configure(self.otlp(sink.url, True), repo_root=REPO_ROOT, run_id="r")
        self.assertTrue(handle.enabled)
        self.assertEqual(["/v1/traces"], [path for path, _, _ in sink.received])

    def test_an_endpoint_that_doesnt_answer_refuses_naming_only_its_host(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"http://127.0.0.1:{port}/otlp/v1/traces?tenant=abc"
        with self.assertRaises(telemetry.TelemetryRefused) as caught:
            telemetry.configure(self.otlp(url, True), repo_root=REPO_ROOT, run_id="r")
        self.assertEqual("telemetry: the endpoint at 127.0.0.1 didn't answer", str(caught.exception))

    def test_not_required_probes_nothing(self) -> None:
        sink = Sink()
        self.addCleanup(sink.close)
        handle = telemetry.configure(self.otlp(sink.url, False), repo_root=REPO_ROOT, run_id="r")
        self.assertTrue(handle.enabled)
        self.assertEqual([], sink.received)


if __name__ == "__main__":
    unittest.main()
