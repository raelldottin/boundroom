# Example dashboard for Boundroom's metrics

`boundroom-dashboard.json` is a Grafana dashboard for every instrument in Boundroom's metric catalogue (`automation/supervisor/telemetry_metrics.py`, SPEC-006). It reads a Prometheus-compatible backend that you run and that receives Boundroom's OTLP metrics. It assumes no hosted service. `sample-metrics.om` is sample data to try it with. It is an example: copy it, change it, and treat your copy as yours.

## What the dashboard shows

- **Runs:** runs of each entry point by outcome, runs that didn't exit 0, runs over time, and run duration (median and 95th percentile) by entry point.
- **Slices:** the supervisor's verdicts, and the queue status it wrote with each.
- **Agent sessions:** sessions by runner and outcome, session duration by runner, and the average session.
- **Validation and sync:** validation replays by result, files the sync tool wrote or removed, and syncs it refused.

A run's outcome describes the process. A slice that ends `stop_failed` still exits 0, so look for it under **Slices**.

## Getting Boundroom's metrics into Prometheus

Turn telemetry on in `otlp` mode with an endpoint you run (`docs/workflows/boundroom.md`, Telemetry). Boundroom sends metrics once per run, over OTLP/HTTP, to the endpoint's `/v1/metrics`. The usual setup is an OpenTelemetry Collector you run: it receives OTLP and forwards metrics to Prometheus, through Prometheus's OTLP receiver (`prometheus --web.enable-otlp-receiver`, at `/api/v1/otlp/v1/metrics`) or remote write. Traces go to whichever tracing backend you use.

The dashboard's queries assume the standard OpenTelemetry to Prometheus names, which Prometheus's OTLP receiver and the Collector's Prometheus exporters use by default:

| Boundroom | Prometheus |
| --- | --- |
| `boundroom.runs` (counter) | `boundroom_runs_total` |
| `boundroom.run.duration` (histogram, seconds) | `boundroom_run_duration_seconds_bucket`, `_count`, `_sum` |
| `service.name` | `job` (the dashboard's Job variable, `boundroom` unless you set another) |
| `service.instance.id` | `instance` |

If your backend keeps the dotted names, adjust the queries.

**Histograms.** Boundroom exports every duration as an explicit-bucket histogram, with bounds from 1 second to 2 hours (`DURATION_BUCKETS_SECONDS` in the catalogue). Any Prometheus-compatible backend stores these as classic histograms, so they don't need native histograms.

**Why `last_over_time`, not `rate()`.** Each run is its own short process, so it arrives as its own series: its `instance` is a fresh `service.instance.id`, and it carries one cumulative value. `rate()` and `increase()` need two samples of a series, and a run gives one. So each panel takes every series' last value in the window with `last_over_time`, then sums. For the same reason, keep `instance`. If you drop it, every run lands in one series that keeps resetting, and the totals come out low. Each run adds a few dozen series, so plan retention by how many runs you keep.

## Trying it with the sample data

`sample-metrics.om` holds 20 synthetic runs on 2026-11-16 (UTC), one every 20 minutes. They are recorded through Boundroom's own telemetry and named as Prometheus stores them. Load it into a Prometheus you run:

```bash
promtool tsdb create-blocks-from openmetrics automation/examples/telemetry/sample-metrics.om ./boundroom-sample-data
prometheus --storage.tsdb.path=./boundroom-sample-data --storage.tsdb.retention.time=10y
```

Then, in Grafana, add that Prometheus as a data source, import `boundroom-dashboard.json` (**Dashboards → New → Import**), choose the data source, and set the time range to 2026-11-16.

In the Boundroom repository, `automation/tests/test_telemetry_dashboard.py` evaluates each dashboard query against the sample, and fails if any returns nothing or reads a series the catalogue doesn't have. It also checks that the sample matches what Boundroom records today. After changing the catalogue or the runs in `Tools/telemetry_sample.py`, regenerate the sample:

```bash
uv run python Tools/telemetry_sample.py
```
