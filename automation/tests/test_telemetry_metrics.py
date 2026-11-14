"""Metrics, from one catalogue (SPEC-006 RA-TEL-010).

Every instrument is declared once in ``telemetry_metrics.CATALOGUE`` with a UCUM unit and a fixed
vocabulary for each attribute, so a value outside it becomes ``other`` and the number of series
stays bounded. Metrics are recorded only when telemetry is on. File mode writes them beside the
spans as ``<run_id>.metrics.jsonl``; otlp mode sends them once, at flush, to the user's endpoint
with the same headers as the traces.
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

from automation.supervisor import telemetry, telemetry_metrics
from automation.supervisor.telemetry_settings import resolve
from automation.tests import test_run_next_local_board as local
from automation.tests.test_telemetry_spans import spans

consumer = local.consumer
agent = local.agent
no_hermes = local.no_hermes

REPO_ROOT = Path(__file__).resolve().parents[2]
UCUM_UNITS = {"1", "s"}
# Where code names an instrument: telemetry.metric / tracing.metric calls, and record_run.
METRIC_CALL = re.compile(r"\bmetric\(\s*(?:[a-z_]+,\s*)?\"(boundroom\.[a-z_.]+)\"")


def file_handle(tmp_path: Path, run_id: str = "run-1") -> telemetry.Telemetry:
    settings = resolve(flags={"telemetry": "file", "telemetry_file_dir": str(tmp_path / "out")}, environ={}, operator_file=None)
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id=run_id, service_version="0.4.0")
    assert handle.enabled
    return handle


def metric_lines(directory: Path) -> list[dict]:
    (path,) = directory.glob("*.metrics.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestCatalogue:
    def test_every_instrument_is_well_formed(self):
        names = [instrument.name for instrument in telemetry_metrics.CATALOGUE]
        assert len(names) == len(set(names))
        for instrument in telemetry_metrics.CATALOGUE:
            assert instrument.name.startswith("boundroom.")
            assert instrument.kind in ("counter", "histogram")
            assert instrument.unit in UCUM_UNITS
            assert instrument.description.strip()
            assert instrument.attributes, "every instrument has at least one bounded attribute"
            for key, vocabulary in instrument.attributes.items():
                assert re.fullmatch(r"[a-z_]+", key)
                assert vocabulary and len(vocabulary) == len(set(vocabulary))
                assert telemetry_metrics.OTHER not in vocabulary
        assert {i.unit for i in telemetry_metrics.CATALOGUE if i.kind == "histogram"} == {"s"}

    def test_unknown_values_become_other_and_unknown_keys_are_dropped(self):
        decisions = telemetry_metrics.BY_NAME["boundroom.slice.decisions"]
        bounded = telemetry_metrics.bounded(
            decisions, {"decision": "made-up", "queue_status": "done", "slice_id": "S-1", "reason": "free text"}
        )
        assert bounded == {"decision": "other", "queue_status": "done"}
        replays = telemetry_metrics.BY_NAME["boundroom.validation.replays"]
        assert telemetry_metrics.bounded(replays, {"success": True, "entry": None}) == {"success": "true"}
        assert telemetry_metrics.bounded(replays, {"success": False}) == {"success": "false"}

    def test_every_instrument_the_code_records_is_in_the_catalogue_and_every_entry_is_used(self):
        used: set[str] = set()
        for root in (REPO_ROOT / "automation", REPO_ROOT / "Tools"):
            for path in root.rglob("*"):
                if path.suffix in {".py", ".sh"} and path.is_file() and "tests" not in path.parts:
                    used |= set(METRIC_CALL.findall(path.read_text(encoding="utf-8")))
        used |= {"boundroom.runs", "boundroom.run.duration"}  # through record_run
        assert used - set(telemetry_metrics.BY_NAME) == set(), "instruments missing from the catalogue"
        assert set(telemetry_metrics.BY_NAME) - used == set(), "catalogued instruments nothing records"

    def test_outcomes(self):
        assert [telemetry_metrics.session_outcome(code) for code in (0, 1, 124)] == ["ok", "nonzero", "timeout"]
        assert [telemetry_metrics.run_outcome(code) for code in (0, 2)] == ["ok", "failed"]


class TestRecording:
    def test_file_mode_writes_one_line_per_data_point_beside_the_spans(self, tmp_path):
        handle = file_handle(tmp_path)
        with handle.span("boundroom.run"):
            handle.metric("boundroom.slice.decisions", decision="stop_for_review", queue_status="done")
            handle.metric("boundroom.slice.decisions", decision="invented", queue_status="done", slice_id="S-9")
            handle.record_run("run_next", "ok", 1.5)
        assert handle.flush()

        trace = {record["trace_id"] for record in spans(tmp_path / "out")}
        lines = metric_lines(tmp_path / "out")
        assert {line["run_id"] for line in lines} == {"run-1"}
        assert {line["trace_id"] for line in lines} == trace
        assert all(line["resource"]["service.name"] == "boundroom" for line in lines)
        decisions = {line["attributes"]["decision"]: line for line in lines if line["name"] == "boundroom.slice.decisions"}
        assert set(decisions) == {"stop_for_review", "other"}
        assert decisions["other"]["attributes"] == {"decision": "other", "queue_status": "done"}
        assert (decisions["stop_for_review"]["kind"], decisions["stop_for_review"]["unit"]) == ("counter", "1")
        assert decisions["stop_for_review"]["value"] == 1
        (duration,) = [line for line in lines if line["name"] == "boundroom.run.duration"]
        assert (duration["unit"], duration["attributes"]) == ("s", {"entry": "run_next", "outcome": "ok"})
        assert duration["histogram"] == {"count": 1, "sum": 1.5, "min": 1.5, "max": 1.5}
        assert all(isinstance(line["time_unix_nano"], int) for line in lines)

    def test_off_records_nothing_and_writes_no_file(self, tmp_path):
        handle = telemetry.configure(resolve(environ={}, operator_file=None), repo_root=REPO_ROOT, run_id="r")
        handle.metric("boundroom.runs", entry="run_next", outcome="ok")
        handle.record_run("run_next", "ok", 0.1)
        assert handle.flush()
        assert list(tmp_path.iterdir()) == []

    def test_no_data_point_writes_no_metrics_file(self, tmp_path):
        handle = file_handle(tmp_path)
        with handle.span("boundroom.run"):
            pass
        assert handle.flush()
        assert list((tmp_path / "out").glob("*.metrics.jsonl")) == []

    def test_a_metric_that_cant_be_recorded_or_written_never_raises(self, tmp_path, capsys):
        handle = file_handle(tmp_path)
        handle.metric("boundroom.not_catalogued")
        assert "metric boundroom.not_catalogued was not recorded" in capsys.readouterr().err
        handle.metric("boundroom.runs", entry="run_next", outcome="ok")
        with mock.patch.object(handle._metric_reader, "get_metrics_data", side_effect=OSError("disk full")):
            assert handle.flush() is False
        assert "metrics could not be written" in capsys.readouterr().err

    def test_metrics_url_sits_beside_the_traces_url(self):
        assert telemetry.metrics_url("http://127.0.0.1:4318") == "http://127.0.0.1:4318/v1/metrics"
        assert telemetry.metrics_url("https://c.example.test/") == "https://c.example.test/v1/metrics"
        assert telemetry.metrics_url("https://c.example.test/otlp/v1/traces") == "https://c.example.test/otlp/v1/metrics"
        assert telemetry.metrics_url("https://c.example.test/custom") is None


class Sink:
    """A local OTLP/HTTP endpoint that records each request's path, headers and body."""

    def __init__(self) -> None:
        received: list[tuple[str, dict[str, str], bytes]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                received.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's name
                return None

        self.received = received
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sink():
    server = Sink()
    yield server
    server.close()


def metric_names(body: bytes) -> set[str]:
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

    request = ExportMetricsServiceRequest()
    request.ParseFromString(body)
    return {
        metric.name
        for resource_metrics in request.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_otlp_sends_metrics_once_at_flush_with_the_traces_headers(sink):
    settings = resolve(
        flags={"telemetry": "otlp", "telemetry_endpoint": sink.url},
        environ={"BOUNDROOM_TELEMETRY_HEADERS": "authorization=Bearer user-token"},
        operator_file=None,
    )
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r")
    with handle.span("boundroom.run"):
        handle.record_run("run_next", "ok", 0.2)
    assert not [path for path, _, _ in sink.received if path == "/v1/metrics"], "nothing is sent before the flush"
    assert handle.flush()

    metrics = [(headers, body) for path, headers, body in sink.received if path == "/v1/metrics"]
    assert len(metrics) == 1
    headers, body = metrics[0]
    assert headers["authorization"] == "Bearer user-token"
    assert metric_names(body) == {"boundroom.runs", "boundroom.run.duration"}


def test_ud6_ambient_otlp_headers_never_reach_the_users_endpoint(sink):
    ambient = {"OTEL_EXPORTER_OTLP_HEADERS": "x-ambient=leak", "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "x-ambient-metrics=leak"}
    with mock.patch.dict(os.environ, ambient):
        settings = resolve(flags={"telemetry": "otlp", "telemetry_endpoint": sink.url}, environ={}, operator_file=None)
        handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r")
        with handle.span("boundroom.run"):
            handle.record_run("run_next", "ok", 0.2)
        assert handle.flush()
    assert {path for path, _, _ in sink.received} == {"/v1/traces", "/v1/metrics"}
    for _, headers, _ in sink.received:
        assert "x-ambient" not in headers and "x-ambient-metrics" not in headers


def test_an_endpoint_with_a_custom_traces_path_sends_no_metrics_and_says_so(sink):
    settings = resolve(flags={"telemetry": "otlp", "telemetry_endpoint": f"{sink.url}/custom"}, environ={}, operator_file=None)
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r")
    assert any("metrics aren't sent" in message for message in handle.messages)
    with handle.span("boundroom.run"):
        handle.record_run("run_next", "ok", 0.2)
    assert handle.flush()
    assert {path for path, _, _ in sink.received} == {"/custom"}


def test_a_supervised_run_records_its_run_session_and_decision(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    file_dir = tmp_path / "out"
    flags = ("--telemetry", "file", "--telemetry-file-dir", str(file_dir))
    assert local.supervise(monkeypatch, board, agent, extra=flags) == 0
    report = json.loads(capsys.readouterr().out)

    lines = metric_lines(file_dir)
    by_name: dict[str, list[dict]] = {}
    for line in lines:
        by_name.setdefault(line["name"], []).append(line)
    assert {line["trace_id"] for line in lines} == {report["telemetry"]["trace_id"]}
    (runs,) = by_name["boundroom.runs"]
    assert (runs["attributes"], runs["value"]) == ({"entry": "run_next", "outcome": "ok"}, 1)
    (decision,) = by_name["boundroom.slice.decisions"]
    assert decision["attributes"] == {"decision": report["decision"], "queue_status": report["queue_status"]}
    (session,) = by_name["boundroom.agent_session.duration"]
    assert session["attributes"]["entry"] == "run_next" and session["attributes"]["outcome"] == "ok"
    assert session["attributes"]["runner"] in telemetry_metrics.RUNNERS + (telemetry_metrics.OTHER,)
    assert session["histogram"]["count"] == 1
    (duration,) = by_name["boundroom.run.duration"]
    assert duration["histogram"]["count"] == 1


def test_a_failed_run_records_a_failed_outcome(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    file_dir = tmp_path / "out"
    flags = ("--telemetry", "file", "--telemetry-file-dir", str(file_dir), "--agent-cmd", "true")
    assert local.supervise(monkeypatch, board, agent, extra=flags) == 2
    capsys.readouterr()
    by_name = {line["name"]: line for line in metric_lines(file_dir)}
    assert by_name["boundroom.runs"]["attributes"] == {"entry": "run_next", "outcome": "failed"}
    assert by_name["boundroom.slice.decisions"]["attributes"] == {"decision": "failed", "queue_status": "failed"}
