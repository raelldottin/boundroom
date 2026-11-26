"""The benchmark's telemetry: one setting for every lane, and nothing at all when it is off.

``benchmark_settings`` is a pure function of the command line's flags and an environment, so
it is checked as a table of whole results. The span helpers are checked twice: with no handle,
where they must do nothing, and against a real file-mode handle, where what they did is read
back from the spans and metrics files it writes.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from automation.benchmark import tracing
from automation.supervisor import egress, telemetry
from automation.supervisor.telemetry_settings import TelemetrySettings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = "https://otel.example"
KILL_SWITCH = "BOUNDROOM_TELEMETRY"
HEADERS = "BOUNDROOM_TELEMETRY_HEADERS"


def settings(sources: Optional[dict[str, str]] = None, **changes: Any) -> TelemetrySettings:
    """What ``benchmark_settings({}, {})`` returns, with ``changes`` and ``sources`` applied."""
    base: dict[str, Any] = {
        "mode": "off",
        "file_dir": None,
        "endpoint": None,
        "headers": {},
        "capture": "metadata",
        "runner": "off",
        "required": False,
        "environment": "benchmark",
        "resource_attributes": {},
        "messages": (),
        "refusal": None,
    }
    base.update(changes)
    all_sources = {
        name: "default"
        for name in (
            "mode",
            "file_dir",
            "endpoint",
            "headers",
            "capture",
            "runner",
            "required",
            "environment",
            "resource_attributes",
        )
    }
    all_sources.update(runner="benchmark", environment="benchmark")
    all_sources.update(sources or {})
    return TelemetrySettings(sources=all_sources, **base)


class BenchmarkSettingsTests(unittest.TestCase):
    def test_settings_come_from_the_command_line_and_only_three_variables(self) -> None:
        file_flags = {"telemetry": "file", "telemetry_file_dir": "/spans"}
        otlp_flags = {"telemetry": "otlp", "telemetry_endpoint": ENDPOINT}
        offline_message = f"telemetry: otlp mode refused ({egress.REASON}): {egress.OFFLINE_ENV} is set; telemetry is off"
        live_environment = {
            KILL_SWITCH: "file",
            "BOUNDROOM_TELEMETRY_FILE_DIR": "/live",
            "BOUNDROOM_TELEMETRY_CAPTURE": "content",
            "BOUNDROOM_TELEMETRY_RUNNER": "content",
            "BOUNDROOM_TELEMETRY_ENVIRONMENT": "live",
        }
        rows: dict[str, tuple[dict[str, Any], dict[str, str], TelemetrySettings]] = {
            "nothing chosen is off": ({}, {}, settings()),
            "a file flag is file mode": (
                file_flags,
                {},
                settings(mode="file", file_dir="/spans", sources={"mode": "flag", "file_dir": "flag"}),
            ),
            "the kill switch beats the file flag": (
                file_flags,
                {KILL_SWITCH: "off"},
                settings(file_dir="/spans", sources={"mode": "kill switch", "file_dir": "flag"}),
            ),
            "a live run's variables are ignored": ({}, live_environment, settings()),
            "headers go to the endpoint the flag named": (
                otlp_flags,
                {HEADERS: "authorization=Bearer t"},
                settings(
                    mode="otlp",
                    endpoint=ENDPOINT,
                    headers={"authorization": "Bearer t"},
                    sources={"mode": "flag", "endpoint": "flag", "headers": "env"},
                ),
            ),
            "an empty header variable is not passed": (
                otlp_flags,
                {HEADERS: ""},
                settings(mode="otlp", endpoint=ENDPOINT, sources={"mode": "flag", "endpoint": "flag"}),
            ),
            "offline refuses otlp": (
                otlp_flags,
                {egress.OFFLINE_ENV: "1"},
                settings(endpoint=ENDPOINT, messages=(offline_message,), sources={"mode": egress.REASON, "endpoint": "flag"}),
            ),
            "no offline variable keeps otlp": (
                otlp_flags,
                {},
                settings(mode="otlp", endpoint=ENDPOINT, sources={"mode": "flag", "endpoint": "flag"}),
            ),
            "a refusal from the settings is kept": (
                {**file_flags, "telemetry_required": True},
                {KILL_SWITCH: "off"},
                settings(
                    file_dir="/spans",
                    required=True,
                    refusal="telemetry: required, but turned off by the kill switch; refusing to start",
                    sources={"mode": "kill switch", "file_dir": "flag", "required": "flag"},
                ),
            ),
            "runner telemetry off is accepted": ({"telemetry_runner": "off"}, {}, settings()),
            "runner telemetry on is refused and stays off": (
                {"telemetry_runner": "metadata"},
                {},
                settings(refusal=tracing.RUNNER_REFUSAL),
            ),
        }
        for label, (flags, environ, expected) in rows.items():
            with self.subTest(label):
                self.assertEqual(expected, tracing.benchmark_settings(flags, environ))

    def test_the_operator_file_is_not_read(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="tracing-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        operator_file = home / ".config/boundroom/telemetry.json"
        operator_file.parent.mkdir(parents=True)
        operator_file.write_text(json.dumps({"mode": "file", "file_dir": str(home / "live")}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(settings(), tracing.benchmark_settings({}, {}))

    def test_an_untraced_handle_is_off_with_no_run_id(self) -> None:
        self.assertEqual(telemetry.Telemetry(settings=settings(), run_id=""), tracing.untraced())


class NoHandleTests(unittest.TestCase):
    """With no handle every helper is a no-op, so call sites never branch on telemetry."""

    def test_no_handle_and_an_untraced_handle_are_not_enabled(self) -> None:
        self.assertEqual((False, False), (tracing.enabled(None), tracing.enabled(tracing.untraced())))

    def test_the_helpers_do_nothing_without_a_handle(self) -> None:
        with tracing.span(None, "cell", lane="A") as span:
            self.assertIsNone(span)
            self.assertIsNone(tracing.annotate(None, span, verdict="ok"))
            self.assertIsNone(tracing.fail(None, span, "timeout"))
            self.assertIsNone(tracing.metric(None, "boundroom.runs", entry="benchmark", outcome="ok"))

    def test_a_run_without_a_handle_still_returns_and_raises(self) -> None:
        with tracing.recorded_run(None):
            pass
        with self.assertRaises(ValueError):
            with tracing.recorded_run(None):
                raise ValueError("boom")


class FileHandleTests(unittest.TestCase):
    """A real file-mode handle; what the helpers did is read back from the files it writes."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tracing-spans-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        flags = {"telemetry": "file", "telemetry_file_dir": str(self.dir)}
        self.handle = telemetry.configure(tracing.benchmark_settings(flags, {}), repo_root=REPO_ROOT, run_id="exp")
        self.assertTrue(tracing.enabled(self.handle), self.handle.messages)

    def records(self, kind: str) -> list[dict[str, Any]]:
        self.handle.flush()
        path = self.dir / f"exp.{kind}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_span_keeps_only_the_attributes_that_have_values(self) -> None:
        with tracing.span(self.handle, "cell", lane="A", repeat=None) as span:
            tracing.annotate(self.handle, span, verdict="ok", score=None)
        (record,) = self.records("spans")
        ours = {key: value for key, value in record["attributes"].items() if not key.startswith(("logfire.", "code."))}
        self.assertEqual(("cell", "UNSET", {"lane": "A", "verdict": "ok"}), (record["name"], record["status"], ours))

    def test_fail_gives_the_span_an_error_status_with_the_reason_code(self) -> None:
        with tracing.span(self.handle, "phase") as span:
            tracing.fail(self.handle, span, "timeout")
        (record,) = self.records("spans")
        self.assertEqual(("phase", "ERROR", "timeout"), (record["name"], record["status"], record["status_description"]))

    def test_a_metric_counts_one_by_default(self) -> None:
        tracing.metric(self.handle, "boundroom.runs", entry="benchmark", outcome="ok")
        (record,) = self.records("metrics")
        self.assertEqual(
            ("boundroom.runs", {"entry": "benchmark", "outcome": "ok"}, 1),
            (record["name"], record["attributes"], record["value"]),
        )

    def test_a_run_records_ok_when_it_returns_and_error_when_it_raises(self) -> None:
        with tracing.recorded_run(self.handle):
            pass
        with self.assertRaises(ValueError):
            with tracing.recorded_run(self.handle):
                raise ValueError("boom")
        records = self.records("metrics")
        runs = sorted((r["attributes"]["outcome"], r["value"]) for r in records if r["name"] == "boundroom.runs")
        durations = sorted(
            (r["attributes"]["outcome"], r["histogram"]["count"]) for r in records if r["name"] == "boundroom.run.duration"
        )
        self.assertEqual(([("error", 1), ("ok", 1)], [("error", 1), ("ok", 1)]), (runs, durations))
        self.assertEqual({"benchmark"}, {r["attributes"]["entry"] for r in records})


class FileDigestTests(unittest.TestCase):
    def test_a_file_is_digested_and_an_unreadable_one_is_none(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tracing-digest-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "record.json").write_bytes(b"abc")
        digest = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        self.assertEqual(
            (digest, None, None),
            (tracing.file_sha256(tmp / "record.json"), tracing.file_sha256(tmp / "absent"), tracing.file_sha256(tmp)),
        )


if __name__ == "__main__":
    unittest.main()
