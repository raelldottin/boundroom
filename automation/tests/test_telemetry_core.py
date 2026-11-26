"""The telemetry handle, judged by what it writes (SPEC-006 RA-TEL-002, RA-TEL-009, RA-TEL-010).

In file mode the observable output is the run's ``<run_id>.spans.jsonl`` and
``<run_id>.metrics.jsonl``, ``handle.messages``, stderr and the return values. These tests read
those, and touch the handle's inside only where noted: the exact resource on every span, ambient ``OTEL_*``
settings that change nothing, span statuses, an escaping exception recorded by its type only,
the refusals and the bounded flush's messages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

import pytest

from automation.supervisor import telemetry
from automation.supervisor.telemetry_settings import resolve
from automation.tests.test_telemetry_exporter import Sink

RUN_ID = "core-1"
OPERATOR_LABELS = "team.name=core,cost.centre=42"
EXPECTED_RESOURCE = {
    "service.name": "boundroom",
    "service.version": "1.2.3",
    "deployment.environment.name": "ci",
    "cost.centre": "42",
    "team.name": "core",
}
SDK_MISSING = (
    "telemetry: the opentelemetry SDK isn't installed (pip install 'boundroom[telemetry]'); continuing with telemetry off"
)


class Planted(Exception):
    pass


def explode(error: BaseException) -> None:
    raise error


def file_settings(file_dir: Path, *, environ: Optional[dict[str, str]] = None, **flags: object) -> Any:
    environ = {
        "BOUNDROOM_TELEMETRY_ENVIRONMENT": "ci",
        "BOUNDROOM_TELEMETRY_RESOURCE_ATTRIBUTES": OPERATOR_LABELS,
        **(environ or {}),
    }
    return resolve(flags={"telemetry": "file", "telemetry_file_dir": str(file_dir), **flags}, environ=environ, operator_file=None)


def file_handle(tmp_path: Path, run_id: str = RUN_ID) -> telemetry.Telemetry:
    handle = telemetry.configure(
        file_settings(tmp_path / "out"), repo_root=repo(tmp_path), run_id=run_id, service_version="1.2.3"
    )
    assert handle.enabled, handle.messages
    return handle


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    if not root.exists():
        (root / "automation").mkdir(parents=True)
        (root / "automation/slices.json").write_text("{}\n", encoding="utf-8")
    return root


def tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def records(file_dir: Path, run_id: str = RUN_ID) -> list[dict[str, Any]]:
    lines = (file_dir / f"{run_id}.spans.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


class TestTheFileModeResource:
    def test_every_span_carries_exactly_boundrooms_resource_and_nothing_leaves_the_file(self, tmp_path, monkeypatch, capsys):
        connections: list[Any] = []

        def refuse(sock: socket.socket, address: Any, *args: Any, **kwargs: Any) -> None:
            connections.append(address)
            raise OSError("this test allows no connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        root = repo(tmp_path)
        before = tree(root)

        handle = file_handle(tmp_path)
        with handle.span("boundroom.run"):
            handle.mark("boundroom.claim", slice_id="S-1")
        assert handle.flush()

        spans = records(tmp_path / "out")
        assert [span["name"] for span in spans] == ["boundroom.claim", "boundroom.run"]
        assert [span["resource"] for span in spans] == [EXPECTED_RESOURCE, EXPECTED_RESOURCE]
        assert capsys.readouterr().out == ""
        assert connections == []
        assert tree(root) == before


class TestAmbientSettings:
    AMBIENT = {"OTEL_RESOURCE_ATTRIBUTES": "team.name=leak", "OTEL_SERVICE_NAME": "leak"}

    def test_ambient_otel_settings_change_nothing_in_the_file_and_send_nothing(self, tmp_path):
        ambient_sink = Sink()
        try:
            with mock.patch.dict(os.environ, {**self.AMBIENT, "OTEL_EXPORTER_OTLP_ENDPOINT": ambient_sink.url}):
                before = dict(os.environ)
                handle = file_handle(tmp_path)
                with handle.span("boundroom.run"):
                    handle.metric("boundroom.runs", entry="run_next", outcome="ok")
                assert handle.flush()
                assert dict(os.environ) == before
        finally:
            ambient_sink.close()
        (span,) = records(tmp_path / "out")
        assert span["resource"] == EXPECTED_RESOURCE
        assert "leak" not in (tmp_path / "out" / f"{RUN_ID}.spans.jsonl").read_text(encoding="utf-8")
        assert "leak" not in (tmp_path / "out" / f"{RUN_ID}.metrics.jsonl").read_text(encoding="utf-8")
        assert ambient_sink.received == []

    def test_ambient_resource_settings_never_reach_the_users_endpoint(self, tmp_path):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: PLC0415

        mine = Sink()
        try:
            with mock.patch.dict(os.environ, self.AMBIENT):
                settings = resolve(flags={"telemetry": "otlp", "telemetry_endpoint": mine.url}, environ={}, operator_file=None)
                handle = telemetry.configure(settings, repo_root=repo(tmp_path), run_id=RUN_ID)
                with handle.span("boundroom.run"):
                    pass
                assert handle.flush()
        finally:
            mine.close()
        (body,) = [body for path, _, body in mine.received if path == "/v1/traces"]
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        (resource_spans,) = request.resource_spans
        resource = {attribute.key: attribute.value.string_value for attribute in resource_spans.resource.attributes}
        assert resource["service.name"] == "boundroom"
        assert "team.name" not in resource
        assert "leak" not in resource.values()

    def test_the_environment_comes_back_when_configure_gives_up(self, tmp_path):
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9", "LOGFIRE_TOKEN": "t"}):
            before = dict(os.environ)
            handle = telemetry.configure(file_settings(Path("relative-out")), repo_root=repo(tmp_path), run_id=RUN_ID)
            assert dict(os.environ) == before
        assert not handle.enabled


class TestSpanStatus:
    def test_mark_sets_the_status_from_a_reason_code_only(self, tmp_path):
        handle = file_handle(tmp_path)
        handle.mark("bad", error_code="Bad Code! /home/someone")
        handle.mark("good", error_code="stop_failed")
        handle.mark("fine", decision="continue")
        assert handle.flush()
        by_name = {span["name"]: span for span in records(tmp_path / "out")}
        assert (by_name["bad"]["status"], by_name["bad"]["status_description"]) == ("ERROR", "error")
        assert (by_name["good"]["status"], by_name["good"]["status_description"]) == ("ERROR", "stop_failed")
        assert (by_name["fine"]["status"], by_name["fine"]["status_description"]) == ("UNSET", None)
        assert by_name["fine"]["attributes"]["decision"] == "continue"

    def test_an_escaping_exception_is_raised_unchanged_and_recorded_by_its_type_only(self, tmp_path):
        handle = file_handle(tmp_path)
        error = Planted("a message naming /home/someone/secret-path and a token")
        with pytest.raises(Planted) as caught:
            with handle.span("boundroom.run"):
                explode(error)
        assert caught.value is error
        assert handle.flush()

        (span,) = records(tmp_path / "out")
        assert (span["status"], span["status_description"]) == ("ERROR", "exception:Planted")
        assert span["events"] == [
            {
                "name": "exception",
                "attributes": {"exception.type": f"{__name__}.Planted", "boundroom.exception.where": f"{__name__}.explode"},
            }
        ]
        text = (tmp_path / "out" / f"{RUN_ID}.spans.jsonl").read_text(encoding="utf-8")
        assert "secret-path" not in text and "token" not in text

    def test_annotate_and_fail_without_a_span_or_with_telemetry_off_do_nothing(self, tmp_path):
        handle = file_handle(tmp_path)
        off = telemetry.configure(resolve(environ={}, operator_file=None), repo_root=repo(tmp_path), run_id="off")
        handle.annotate(None, ignored=1)
        handle.fail(None, "ignored")
        with handle.span("boundroom.run") as span:
            off.annotate(span, leaked=1)
            off.fail(span, "leaked")
        assert handle.flush()
        (record,) = records(tmp_path / "out")
        assert "leaked" not in record["attributes"]
        assert (record["status"], record["status_description"]) == ("UNSET", None)


class TestFailuresSaySoAndNeverRaise:
    def test_a_metrics_file_that_cant_be_written_is_dropped_and_said(self, tmp_path, capsys):
        handle = file_handle(tmp_path)
        (tmp_path / "out" / f"{RUN_ID}.metrics.jsonl").mkdir(parents=True)
        with handle.span("boundroom.run"):
            handle.metric("boundroom.runs", entry="run_next", outcome="ok")
        assert handle.flush() is False
        assert capsys.readouterr().err.splitlines() == ["telemetry: metrics could not be written; they were dropped"]
        assert records(tmp_path / "out")[0]["name"] == "boundroom.run"

    def test_a_spans_file_that_cant_be_written_neither_raises_nor_logs(self, tmp_path, caplog):
        # The exporter reports the failure to the SDK as a result; an exception would reach the
        # SDK's log as "Exception while exporting" instead.
        handle = file_handle(tmp_path)
        (tmp_path / "out" / f"{RUN_ID}.spans.jsonl").mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            with handle.span("boundroom.run"):
                pass
            handle.flush()
        assert (tmp_path / "out" / f"{RUN_ID}.spans.jsonl").is_dir()
        assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == []

    def test_a_relative_file_dir_is_refused_with_its_reason(self, tmp_path):
        handle = telemetry.configure(file_settings(Path("relative/out")), repo_root=repo(tmp_path), run_id=RUN_ID)
        assert not handle.enabled
        assert handle.messages == [
            "telemetry: file_dir must be an absolute path, not relative/out; continuing with telemetry off"
        ]

    @pytest.mark.parametrize("module", ["opentelemetry.sdk.trace.export", "opentelemetry.sdk.metrics.export"])
    def test_a_missing_sdk_part_turns_telemetry_off_and_says_so(self, tmp_path, module):
        import logfire  # noqa: F401, PLC0415 - installed first, so only the named SDK part is missing

        with mock.patch.dict(sys.modules, {module: None}):
            handle = telemetry.configure(file_settings(tmp_path / "out"), repo_root=repo(tmp_path), run_id=RUN_ID)
            assert not handle.enabled
            assert handle.messages == [SDK_MISSING]
            with pytest.raises(telemetry.TelemetryRefused, match="SDK isn't installed"):
                telemetry.configure(
                    file_settings(tmp_path / "out", telemetry_required=True), repo_root=repo(tmp_path), run_id=RUN_ID
                )

    def test_a_flush_after_the_sdk_shut_down_says_what_it_dropped(self, tmp_path, capsys):
        handle = file_handle(tmp_path)
        with handle.span("boundroom.run"):
            pass
        # The SDK's own shutdown, as at interpreter exit, ends every processor before the flush.
        # configure(local=True) builds its own Logfire instance, which the module-level
        # logfire.shutdown() can't reach, so the handle's instance is shut down directly.
        handle._logfire.shutdown()
        capsys.readouterr()
        assert handle.flush() is False
        assert "telemetry: flush did not finish within 5s; the rest was dropped" in capsys.readouterr().err.splitlines()


class TestMetricRecords:
    def test_only_catalogued_instruments_become_records(self):
        from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader], resource=Resource({"service.name": "boundroom", "host.name": "h"}))
        meter = provider.get_meter("test")
        meter.create_counter("boundroom.runs", unit="1").add(2, {"entry": "run_next", "outcome": "ok"})
        meter.create_counter("someone.elses.counter", unit="1").add(1)
        data = reader.get_metrics_data()
        provider.shutdown()

        found = list(telemetry.metric_records(data, "r", "ab" * 16, telemetry.STANDARD_RESOURCE_KEYS))
        assert len(found) == 1
        assert {key: value for key, value in found[0].items() if key != "time_unix_nano"} == {
            "name": "boundroom.runs",
            "kind": "counter",
            "unit": "1",
            "attributes": {"entry": "run_next", "outcome": "ok"},
            "run_id": "r",
            "trace_id": "ab" * 16,
            "resource": {"service.name": "boundroom"},
            "value": 2,
        }

    def test_no_data_gives_no_records(self):
        assert list(telemetry.metric_records(None, "r", None, telemetry.STANDARD_RESOURCE_KEYS)) == []


class TestPureHelpers:
    def test_a_run_id_names_its_time_and_is_unique(self):
        moment = datetime(2026, 11, 16, 1, 2, 3, tzinfo=timezone.utc)
        first, second = telemetry.new_run_id(moment), telemetry.new_run_id(moment)
        assert re.fullmatch(r"20261116T010203Z-[0-9a-f]{8}", first)
        assert first != second
        later = [telemetry.new_run_id(moment + timedelta(seconds=seconds)) for seconds in (1, 3600, 86400)]
        assert sorted([*later, first]) == [first, *later]
        assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", telemetry.new_run_id())

    def test_a_run_id_names_its_time_in_utc_whatever_zone_it_is_given(self):
        eastern = datetime(2026, 11, 15, 20, 2, 3, tzinfo=timezone(timedelta(hours=-5)))
        assert telemetry.new_run_id(eastern).startswith("20261116T010203Z-")

    def test_an_exception_never_raised_was_raised_nowhere_known(self):
        assert telemetry.exception_where(ValueError("never raised")) == "unknown"

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("a" * 32, "a" * 32),
            ("a" * 33, "error"),
            ("a" * 32 + ":ValueError", "a" * 32 + ":ValueError"),
            ("a" * 33 + ":ValueError", "error"),
            ("exception:Planted", "exception:Planted"),
        ],
    )
    def test_reason_codes_are_short(self, code, expected):
        assert telemetry.reason_code(code) == expected

    def test_the_runner_gets_the_users_headers_in_otel_form(self):
        settings = resolve(
            flags={"telemetry": "otlp", "telemetry_endpoint": "https://c.example.test", "telemetry_runner": "metadata"},
            environ={"BOUNDROOM_TELEMETRY_HEADERS": "a=1,b=2"},
            operator_file=None,
        )
        runner = telemetry.runner_environment(settings, {}, Session(), "r", "S-1")
        assert runner.variables["OTEL_EXPORTER_OTLP_HEADERS"] == "a=1,b=2"

    def test_the_runner_gets_nothing_without_both_telemetry_on_and_a_session(self):
        on = resolve(
            flags={"telemetry": "otlp", "telemetry_endpoint": "https://c.example.test", "telemetry_runner": "metadata"},
            environ={},
            operator_file=None,
        )
        off = resolve(flags={"telemetry_runner": "metadata"}, environ={}, operator_file=None)
        for settings, session in ((off, Session()), (on, None)):
            runner = telemetry.runner_environment(settings, {}, session, "r", "S-1")
            assert (runner.variables, runner.passthrough, runner.note) == ({}, (), "")

    def test_an_empty_reason_stays_empty(self, tmp_path):
        assert telemetry.without_root("", tmp_path) == ""


class Session:
    def get_span_context(self) -> Any:
        return SimpleNamespace(trace_id=1, span_id=2)


class TestTwoRunsInOneProcess:
    def test_each_handle_writes_only_its_own_runs_spans(self, tmp_path):
        first = telemetry.configure(file_settings(tmp_path / "one"), repo_root=repo(tmp_path), run_id="one")
        second = telemetry.configure(file_settings(tmp_path / "two"), repo_root=repo(tmp_path), run_id="two")
        with first.span("first.run"):
            pass
        with second.span("second.run"):
            pass
        assert first.flush() is True
        assert second.flush() is True
        assert [span["name"] for span in records(tmp_path / "one", "one")] == ["first.run"]
        assert [span["name"] for span in records(tmp_path / "two", "two")] == ["second.run"]


class TestWhatTheFilesHold:
    def test_a_run_without_metrics_writes_no_metrics_file(self, tmp_path):
        handle = file_handle(tmp_path)
        with handle.span("boundroom.run"):
            pass
        assert handle.flush() is True
        assert sorted(path.name for path in (tmp_path / "out").iterdir() if path.is_file()) == [f"{RUN_ID}.spans.jsonl"]

    def test_metrics_alone_reach_a_file_dir_whose_parents_dont_exist_yet(self, tmp_path):
        out = tmp_path / "a" / "b" / "out"
        handle = telemetry.configure(file_settings(out), repo_root=repo(tmp_path), run_id=RUN_ID)
        handle.metric("boundroom.runs", entry="run_next", outcome="ok")
        assert handle.flush() is True
        (line,) = (out / f"{RUN_ID}.metrics.jsonl").read_text(encoding="utf-8").splitlines()
        assert (json.loads(line)["name"], json.loads(line)["value"]) == ("boundroom.runs", 1)

    def test_every_line_lists_its_keys_in_sorted_order(self, tmp_path):
        # Stable lines diff and grep the same from run to run.
        handle = file_handle(tmp_path)
        with handle.span("boundroom.run", slice_id="S-1"):
            handle.metric("boundroom.runs", entry="run_next", outcome="ok")
        assert handle.flush() is True
        for name in (f"{RUN_ID}.spans.jsonl", f"{RUN_ID}.metrics.jsonl"):
            (line,) = (tmp_path / "out" / name).read_text(encoding="utf-8").splitlines()
            assert list(json.loads(line)) == sorted(json.loads(line)), name

    def test_an_endpoint_with_no_metrics_path_sends_the_spans_and_says_nothing_more(self, tmp_path, capsys):
        sink = Sink()
        try:
            endpoint = f"{sink.url}/custom/traces"
            settings = resolve(flags={"telemetry": "otlp", "telemetry_endpoint": endpoint}, environ={}, operator_file=None)
            handle = telemetry.configure(settings, repo_root=repo(tmp_path), run_id=RUN_ID)
            assert handle.messages == [
                "telemetry: the endpoint names a traces path, so metrics aren't sent; give a bare endpoint for both"
            ]
            with handle.span("boundroom.run"):
                handle.metric("boundroom.runs", entry="run_next", outcome="ok")
            capsys.readouterr()
            assert handle.flush() is True
        finally:
            sink.close()
        assert capsys.readouterr().err == ""
        assert [path for path, _, _ in sink.received] == ["/custom/traces"]


class TestRequiredFileMode:
    @pytest.mark.parametrize("file_dir", ["out", "a/b/out"])
    def test_a_required_run_starts_whether_file_dir_exists_or_has_missing_parents(self, tmp_path, file_dir):
        out = tmp_path / file_dir
        if file_dir == "out":
            out.mkdir()
        handle = telemetry.configure(file_settings(out, telemetry_required=True), repo_root=repo(tmp_path), run_id=RUN_ID)
        assert handle.enabled, handle.messages
        assert out.is_dir()
        assert handle.flush() is True


class TestTheJsonLinesExporter:
    def test_it_makes_missing_parents_and_succeeds_again_once_they_exist(self, tmp_path):
        from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

        path = tmp_path / "a" / "b" / "spans.jsonl"
        exporter = telemetry.json_lines_exporter(path)
        assert exporter.export([]) is SpanExportResult.SUCCESS
        assert exporter.export([]) is SpanExportResult.SUCCESS
        assert path.parent.is_dir()

    def test_a_path_it_cant_write_is_a_failure_result(self, tmp_path):
        from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

        path = tmp_path / "spans.jsonl"
        path.mkdir()
        assert telemetry.json_lines_exporter(path).export([]) is SpanExportResult.FAILURE
