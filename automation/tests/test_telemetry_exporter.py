"""The telemetry exporter: off costs nothing, on goes only where the user said (SPEC-006 RA-TEL-002).

Covers UD-1 (off imports, writes and connects to nothing), UD-3 (no built-in destination and
``send_to_logfire=False``), UD-4 (otlp connects only to the user's endpoint), UD-6 (ambient OTEL_*
and LOGFIRE_* change nothing), the missing-SDK warning or refusal, the file_dir-in-repository
refusal and the bounded flush.
"""

from __future__ import annotations

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
from unittest import mock

from automation.supervisor import telemetry
from automation.supervisor.telemetry_settings import resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_HOSTS = re.compile(
    r"logfire[-a-z]*\.pydantic\.dev|honeycomb\.io|datadoghq\.|newrelic\.com|grafana\.net|lightstep\.com|signoz\.cloud|sentry\.io"
)


class Sink:
    """A local OTLP/HTTP endpoint that records what reaches it."""

    def __init__(self) -> None:
        received: list[tuple[str, bytes]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                received.append((self.path, body))
                self.send_response(200)
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
    def test_ud3_the_sdk_is_configured_only_from_boundrooms_settings(self) -> None:
        fake = mock.Mock()
        with mock.patch.object(telemetry, "_import_logfire", return_value=fake):
            telemetry.configure(self.file_settings(), repo_root=REPO_ROOT, run_id="r", service_version="0.3.0")
        kwargs = fake.configure.call_args.kwargs
        self.assertIs(False, kwargs["send_to_logfire"])
        self.assertIs(False, kwargs["console"])
        # Metrics go only to Boundroom's own in-memory reader, collected once at flush (RA-TEL-010).
        self.assertIs(fake.MetricsOptions.return_value, kwargs["metrics"])
        self.assertEqual({"additional_readers"}, set(fake.MetricsOptions.call_args.kwargs))
        self.assertEqual(1, len(fake.MetricsOptions.call_args.kwargs["additional_readers"]))
        self.assertIs(False, kwargs["inspect_arguments"])
        self.assertIs(True, kwargs["local"])
        self.assertEqual("boundroom", kwargs["service_name"])
        self.assertFalse(telemetry._inside(Path(kwargs["data_dir"]), REPO_ROOT))
        self.assertEqual(1, len(kwargs["additional_span_processors"]))

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

    def test_the_bounded_flush_says_when_it_dropped_spans(self) -> None:
        handle = telemetry.Telemetry(settings=self.file_settings(), run_id="r")
        handle._processor = mock.Mock(**{"force_flush.return_value": False})
        with mock.patch("sys.stderr") as stderr:
            self.assertFalse(handle.flush())
        self.assertIn("dropped", "".join(str(call) for call in stderr.write.call_args_list))

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
        self.assertEqual({"/v1/traces"}, {path for path, _ in self.mine.received})
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


if __name__ == "__main__":
    unittest.main()
