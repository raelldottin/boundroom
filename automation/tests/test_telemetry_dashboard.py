"""The example dashboard reads the metric catalogue, and loads with sample data (SPEC-009 RA-ENT-010).

The sample is what Boundroom's own telemetry records for a set of synthetic runs, named the way a
Prometheus backend stores OTLP (Tools/telemetry_sample.py). Each dashboard query is evaluated
against it here. The evaluator knows only the few query shapes the dashboard uses, and a query in
any other shape fails the test, so a new panel can't slip in unchecked. It models a backend; it
isn't one, and the README says how to load the sample into a Prometheus you run.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from automation.supervisor import telemetry_metrics

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "automation/examples/telemetry"
DASHBOARD = EXAMPLE / "boundroom-dashboard.json"
SAMPLE = EXAMPLE / "sample-metrics.om"
README = EXAMPLE / "README.md"

_spec = importlib.util.spec_from_file_location("telemetry_sample", REPO_ROOT / "Tools/telemetry_sample.py")
assert _spec and _spec.loader
generator = importlib.util.module_from_spec(_spec)
sys.modules["telemetry_sample"] = generator  # its dataclasses look their module up there
_spec.loader.exec_module(generator)

VARIABLES = {"$job": generator.JOB}
SELECTOR = re.compile(r"(boundroom_[a-z_]+)\{([^}]*)\}")
MATCHER = re.compile(r'^([a-z_]+)(=~|!=|=)"([^"]*)"$')
LAST_OVER = r"last_over_time\((boundroom_[a-z_]+\{[^}]*\})\[\$__(?:range|interval)\]\)"
SUM = re.compile(r"^sum(?: by \(([a-z_, ]+)\) )?\(" + LAST_OVER + r"\)$")
QUANTILE = re.compile(r"^histogram_quantile\((0\.\d+), (sum by \(le, [a-z_]+\) \(" + LAST_OVER + r"\))\)$")
RATIO = re.compile(r"^(sum\(" + LAST_OVER + r"\)) / (sum\(" + LAST_OVER + r"\))$")

Series = tuple[str, dict[str, str], float]
Vector = dict[tuple[tuple[str, str], ...], float]


def dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def queries() -> list[tuple[str, str]]:
    return [
        (panel["title"], target["expr"])
        for panel in dashboard()["panels"]
        if panel["type"] != "row"
        for target in panel["targets"]
    ]


def sample() -> list[Series]:
    found = []
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        match = re.fullmatch(r"(boundroom_[a-z_]+)\{([^}]*)\} (\S+) \d+", line)
        assert match, f"not an OpenMetrics sample line: {line}"
        labels = dict(re.findall(r'([a-z_]+)="([^"]*)"', match.group(2)))
        found.append((match.group(1), labels, float(match.group(3))))
    return found


def matchers(selector: str) -> tuple[str, list[tuple[str, str, str]]]:
    match = SELECTOR.fullmatch(selector)
    assert match, f"not a selector: {selector}"
    parsed = []
    for part in match.group(2).split(","):
        found = MATCHER.match(part)
        assert found, f"unreadable matcher {part!r} in {selector}"
        key, op, value = found.groups()
        parsed.append((key, op, value))
    return match.group(1), parsed


def selects(selector: str, series: list[Series]) -> list[Series]:
    name, wanted = matchers(selector)
    chosen = []
    for series_name, labels, value in series:
        if series_name != name:
            continue
        ok = True
        for key, op, raw in wanted:
            pattern = VARIABLES.get(raw, raw)
            have = labels.get(key, "")
            if op == "=":
                ok &= have == pattern
            elif op == "!=":
                ok &= have != pattern
            else:
                ok &= re.fullmatch(pattern, have) is not None
        if ok:
            chosen.append((series_name, labels, value))
    return chosen


def summed(by: str | None, selector: str, series: list[Series]) -> Vector:
    """sum by (...) (last_over_time(selector[...])): every run is one series with one sample."""
    keys = [key.strip() for key in by.split(",")] if by else []
    groups: Vector = defaultdict(float)
    for _, labels, value in selects(selector, series):
        groups[tuple((key, labels.get(key, "")) for key in keys)] += value
    return dict(groups)


def quantile(q: float, buckets: Vector) -> dict[tuple[tuple[str, str], ...], float]:
    """Prometheus' histogram_quantile: linear interpolation inside the bucket that holds the rank."""
    by_group: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = defaultdict(list)
    for key, count in buckets.items():
        labels = dict(key)
        le = float(labels.pop("le"))
        by_group[tuple(labels.items())].append((le, count))
    result = {}
    for group, points in by_group.items():
        points.sort()
        total = points[-1][1]
        if total == 0:
            continue
        rank = q * total
        lower, below = 0.0, 0.0
        for le, count in points:
            if count >= rank:
                if math.isinf(le):
                    result[group] = lower
                else:
                    result[group] = lower + (le - lower) * (rank - below) / (count - below) if count > below else le
                break
            lower, below = le, count
    return result


def evaluate(expr: str, series: list[Series]) -> dict[Any, float]:
    if match := SUM.fullmatch(expr):
        return summed(match.group(1), match.group(2), series)
    if match := QUANTILE.fullmatch(expr):
        inner = SUM.fullmatch(match.group(2))
        assert inner
        return quantile(float(match.group(1)), summed(inner.group(1), inner.group(2), series))
    if match := RATIO.fullmatch(expr):
        numerator, denominator = summed(None, match.group(2), series), summed(None, match.group(4), series)
        return {(): numerator[()] / denominator[()]} if denominator.get(()) else {}
    raise AssertionError(f"a dashboard query in a shape this test can't check: {expr}")


class TestTheDashboard:
    def test_it_is_a_grafana_dashboard_with_no_fixed_data_source(self):
        board = dashboard()
        assert board["uid"] == "boundroom-overview"
        assert board["schemaVersion"] >= 39
        variables = {variable["name"]: variable for variable in board["templating"]["list"]}
        assert variables["datasource"]["type"] == "datasource" and variables["datasource"]["query"] == "prometheus"
        for panel in board["panels"]:
            if panel["type"] == "row":
                continue
            assert panel["targets"], panel["title"]
            assert panel["datasource"] == {"type": "prometheus", "uid": "${datasource}"}, panel["title"]
            for target in panel["targets"]:
                assert target["datasource"] == panel["datasource"]
        # No hosted service is assumed: the board names no URL.
        assert not re.search(r"https?://", DASHBOARD.read_text(encoding="utf-8"))

    def test_every_instrument_has_a_panel_and_every_query_reads_the_catalogue(self):
        named = {name: instrument for instrument in telemetry_metrics.CATALOGUE for name in generator.series_names(instrument)}
        used: set[str] = set()
        for title, expr in queries():
            for match in SELECTOR.finditer(expr):
                name, wanted = matchers(match.group(0))
                assert name in named, f"{title}: {name} is no catalogue instrument's series"
                instrument = named[name]
                used.add(instrument.name)
                for key, op, value in wanted:
                    if key == "job":
                        assert value == "$job", title
                        continue
                    assert key in instrument.attributes, f"{title}: {instrument.name} has no attribute {key}"
                    literals = value.split("|") if op == "=~" else [value]
                    assert set(literals) <= set(instrument.attributes[key]) | {telemetry_metrics.OTHER}, title
            for by in re.findall(r"by \(([a-z_, ]+)\)", expr):
                for key in (part.strip() for part in by.split(",")):
                    assert key == "le" or any(key in named[m.group(1)].attributes for m in SELECTOR.finditer(expr)), (title, key)
        assert used == set(telemetry_metrics.BY_NAME), "an instrument with no panel"

    def test_every_query_returns_data_from_the_sample(self):
        series = sample()
        for title, expr in queries():
            result = evaluate(expr, series)
            assert result, f"{title}: {expr} returns nothing from the sample"
            assert all(math.isfinite(value) for value in result.values()), (title, result)

    def test_the_headline_numbers_match_the_sample_runs(self):
        series = sample()
        runs = generator.RUNS
        by_title = {title: expr for title, expr in queries()}
        assert evaluate(by_title["Runs"], series) == {(): float(len(runs))}
        not_ok = sum(run.outcome in ("failed", "refused", "error") for run in runs)
        assert evaluate(by_title["Runs that didn't exit 0"], series) == {(): float(not_ok)}
        refused = sum(run.entry == "sync" and run.outcome == "refused" for run in runs)
        assert evaluate(by_title["Sync runs refused"], series) == {(): float(refused)}
        sessions = [session for run in runs for session in run.sessions]
        average = sum(seconds for _, _, seconds in sessions) / len(sessions)
        assert evaluate(by_title["Average session"], series) == {(): pytest.approx(average)}


class TestTheSample:
    def test_it_is_what_boundroom_records_today(self):
        # Rerun the generator through the real telemetry path; a stale sample fails here.
        assert generator.render() == SAMPLE.read_text(encoding="utf-8"), "run uv run python Tools/telemetry_sample.py"

    def test_it_is_openmetrics_with_whole_histograms(self):
        text = SAMPLE.read_text(encoding="utf-8")
        assert text.endswith("# EOF\n")
        buckets: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
        counts: dict[tuple, float] = {}
        for name, labels, value in sample():
            if name.endswith("_bucket"):
                rest = tuple(sorted((k, v) for k, v in labels.items() if k != "le"))
                buckets[(name.removesuffix("_bucket"), rest)].append((float(labels["le"]), value))
            elif name.endswith("_count"):
                counts[(name.removesuffix("_count"), tuple(sorted(labels.items())))] = value
        assert buckets
        bounds = [*telemetry_metrics.DURATION_BUCKETS_SECONDS, math.inf]
        for key, points in buckets.items():
            assert [le for le, _ in points] == bounds, "the explicit duration buckets Boundroom exports"
            values = [count for _, count in points]
            assert values == sorted(values) and values[-1] == counts[key]

    def test_every_run_is_its_own_series(self):
        runs = {labels["instance"] for name, labels, _ in sample() if name == "boundroom_runs_total"}
        assert len(runs) == len(generator.RUNS)


def test_the_readme_says_how_to_load_it():
    text = README.read_text(encoding="utf-8")
    for phrase in (
        "boundroom-dashboard.json",
        "sample-metrics.om",
        "create-blocks-from openmetrics",
        "last_over_time",
        "Tools/telemetry_sample.py",
    ):
        assert phrase in text
