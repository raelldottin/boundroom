#!/usr/bin/env python3
"""Write the sample metrics the example dashboard is checked against (SPEC-009 RA-ENT-010).

Each synthetic run below is recorded through Boundroom's own telemetry, with the metric
catalogue's instruments and bounded attributes and the OpenTelemetry SDK's real histogram
buckets. The data each run would export over OTLP is then written as OpenMetrics, the way a
Prometheus backend stores OTLP: the names and labels follow the OpenTelemetry to Prometheus
compatibility rules (``.`` becomes ``_``, ``s`` adds ``_seconds``, a counter adds ``_total``, a
histogram gives ``_bucket``, ``_count`` and ``_sum``, ``service.name`` becomes ``job`` and
``service.instance.id`` becomes ``instance``). Every run is its own process, so it is its own
series with one sample, the way a real run arrives.

    uv run python Tools/telemetry_sample.py          # rewrite the sample
    uv run python Tools/telemetry_sample.py --check  # exit 1 if it is stale

To look at the dashboard with it, backfill it into a Prometheus you run (promtool tsdb
create-blocks-from openmetrics), as automation/examples/telemetry/README.md describes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import telemetry, telemetry_metrics  # noqa: E402
from automation.supervisor.telemetry_settings import resolve  # noqa: E402

SAMPLE = REPO_ROOT / "automation/examples/telemetry/sample-metrics.om"
JOB = telemetry.SERVICE_NAME  # service.name, with no service.namespace
# The sample's runs start at this time, one every 20 minutes.
START = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp())
STEP_SECONDS = 20 * 60


@dataclass
class Run:
    """One synthetic run: the metric calls its entry point makes, in the order it makes them."""

    entry: str
    outcome: str
    seconds: float
    decisions: list[tuple[str, str]] = field(default_factory=list)  # (decision, queue_status)
    sessions: list[tuple[str, str, float]] = field(default_factory=list)  # (runner, outcome, seconds)
    replays: list[bool] = field(default_factory=list)
    changes: dict[str, int] = field(default_factory=dict)  # kind -> files


def supervised(runner: str, session: str, seconds: float, decision: str, queue_status: str, replays: list[bool]) -> Run:
    return Run(
        "run_next",
        "ok",
        seconds + 9,
        decisions=[(decision, queue_status)],
        sessions=[(runner, session, seconds)],
        replays=replays,
    )


RUNS: tuple[Run, ...] = (
    supervised("claude", "ok", 412, "continue", "done", [True, True]),
    supervised("claude", "ok", 655, "stop_for_review", "done", [True, True, True]),
    supervised("codex", "ok", 318, "continue", "done", [True]),
    supervised("claude", "nonzero", 96, "stop_failed", "failed", []),
    supervised("codex", "ok", 1204, "stop_blocked", "blocked", [True, False]),
    supervised("claude", "timeout", 3600, "stop_failed", "failed", []),
    supervised("auto", "ok", 540, "continue", "done", [True, True]),
    supervised("claude", "ok", 233, "stop", "done", [True]),
    supervised("codex", "error", 4, "failed", "failed", []),
    Run("run_next", "failed", 2),
    Run("run_next", "error", 1),
    Run("evaluate_handoff", "ok", 3, decisions=[("accept", "done")], replays=[True, True]),
    Run("evaluate_handoff", "ok", 2, decisions=[("reject", "queued")], replays=[False]),
    Run("evaluate_handoff", "ok", 4, decisions=[("accept", "done")], replays=[True]),
    Run("sync", "ok", 41, changes={"synced": 37, "removed": 0}),
    Run("sync", "ok", 38, changes={"synced": 3, "removed": 6}),
    Run("sync", "failed", 12),
    Run("sync", "refused", 1),
    supervised("hermes", "ok", 980, "continue", "done", [True]),
    supervised("claude", "ok", 377, "stop_for_review", "done", [True, True]),
)


def record(run: Run, run_id: str, out: Path) -> Any:
    """The SDK's metrics data for one run, recorded through Boundroom's own telemetry."""
    settings = resolve(flags={"telemetry": "file", "telemetry_file_dir": str(out)}, environ={}, operator_file=None)
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id=run_id, service_version="sample")
    if not handle.enabled:
        raise SystemExit(f"telemetry_sample: telemetry didn't start: {handle.messages}")
    for runner, outcome, seconds in run.sessions:
        handle.metric("boundroom.agent_session.duration", seconds, entry=run.entry, outcome=outcome, runner=runner)
    for success in run.replays:
        handle.metric("boundroom.validation.replays", entry=run.entry, success=success)
    for decision, queue_status in run.decisions:
        handle.metric("boundroom.slice.decisions", decision=decision, queue_status=queue_status)
    for kind, files in run.changes.items():
        handle.metric("boundroom.sync.changes", files, kind=kind)
    handle.record_run(run.entry, run.outcome, run.seconds)
    data = handle._metric_reader.get_metrics_data()  # what the run's flush would export
    handle.flush()
    return data


def prometheus_name(instrument: telemetry_metrics.Instrument) -> str:
    """The family name a Prometheus backend gives an OTLP metric, before _total or _bucket."""
    name = instrument.name.replace(".", "_")
    return name + "_seconds" if instrument.unit == "s" else name  # unit "1" adds nothing to a counter


def series_names(instrument: telemetry_metrics.Instrument) -> tuple[str, ...]:
    family = prometheus_name(instrument)
    if instrument.kind == "counter":
        return (family + "_total",)
    return (family + "_bucket", family + "_count", family + "_sum")


def number(value: float) -> str:
    return "+Inf" if value == float("inf") else repr(float(value))


def labels(pairs: dict[str, str]) -> str:
    return "{" + ",".join(f'{key}="{value}"' for key, value in sorted(pairs.items())) + "}"


def points(data: Any) -> Iterator[tuple[str, Any]]:
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name not in telemetry_metrics.BY_NAME:
                    continue  # only the catalogue, as the metrics file (telemetry.metric_records)
                for point in metric.data.data_points:
                    yield metric.name, point


def render(runs: Sequence[Run] = RUNS) -> str:
    families: dict[str, list[str]] = {instrument.name: [] for instrument in telemetry_metrics.CATALOGUE}
    said = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="boundroom-sample-") as out, contextlib.redirect_stderr(said):
        for index, run in enumerate(runs):
            instance = f"sample-run-{index + 1:03d}"
            timestamp = START + index * STEP_SECONDS
            for name, point in points(record(run, instance, Path(out))):
                instrument = telemetry_metrics.BY_NAME[name]
                base = {**{key: str(value) for key, value in point.attributes.items()}, "job": JOB, "instance": instance}
                lines = families[name]
                if instrument.kind == "counter":
                    lines.append(f"{series_names(instrument)[0]}{labels(base)} {number(point.value)} {timestamp}")
                    continue
                bucket, count, total = series_names(instrument)
                cumulative = 0
                bounds = [*point.explicit_bounds, float("inf")]
                for bound, hits in zip(bounds, point.bucket_counts):
                    cumulative += hits
                    lines.append(f"{bucket}{labels({**base, 'le': number(bound)})} {cumulative} {timestamp}")
                lines.append(f"{count}{labels(base)} {point.count} {timestamp}")
                lines.append(f"{total}{labels(base)} {number(point.sum)} {timestamp}")
    if said.getvalue():
        # A metric that wasn't recorded or a flush that failed would leave the sample short.
        raise SystemExit(f"telemetry_sample: telemetry reported a problem:\n{said.getvalue()}")
    text = []
    for instrument in telemetry_metrics.CATALOGUE:
        family = prometheus_name(instrument)
        text.append(f"# TYPE {family} {instrument.kind}")
        if instrument.unit == "s":
            text.append(f"# UNIT {family} seconds")
        text.append(f"# HELP {family} {instrument.description}")
        text.extend(families[instrument.name])
    return "\n".join([*text, "# EOF", ""])


def shown(path: Path) -> str:
    """``path`` relative to the repository when it is inside it, as the messages name the sample."""
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def main(argv: list[str], sample: Path = SAMPLE) -> int:
    parser = argparse.ArgumentParser(description="Write the sample metrics the example dashboard is checked against.")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the sample is stale, and write nothing.")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if sample.read_text(encoding="utf-8") != text:
            print(f"{shown(sample)} is stale: run uv run python Tools/telemetry_sample.py", file=sys.stderr)
            return 1
        return 0
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(text, encoding="utf-8")
    print(f"wrote {shown(sample)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
