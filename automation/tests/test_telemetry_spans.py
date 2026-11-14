"""A supervised run's span tree in file mode, joined to its records (SPEC-006 RA-TEL-003).

One slice on a local board with no Hermes, traced to a JSON Lines file: the spans form the tree
the spec names, in order, under one ``boundroom.run``; their digests match the board record and
the decision report; nothing planted as a secret reaches a span; and an exporter that fails
leaves the verdict and the exit code exactly as they are with telemetry off.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from automation.supervisor import telemetry
from automation.supervisor.telemetry_settings import resolve
from automation.tests import test_run_next_local_board as local

consumer = local.consumer
agent = local.agent
no_hermes = local.no_hermes

ORDER = [
    "boundroom.select",
    "boundroom.context",
    "boundroom.claim",
    "boundroom.agent_session",
    "boundroom.handoff.evaluate",
    "boundroom.decision",
    "boundroom.board.record",
]
SECRET = "Bearer planted-telemetry-secret"


def spans(file_dir: Path) -> list[dict]:
    (path,) = file_dir.glob("*.spans.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_traced_slice_has_the_full_tree_joined_to_its_records(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    (task,) = board.tasks
    file_dir = tmp_path / "spans"
    monkeypatch.setenv("BOUNDROOM_TELEMETRY_HEADERS", f"authorization={SECRET}")

    flags = ("--telemetry", "file", "--telemetry-file-dir", str(file_dir))
    assert local.supervise(monkeypatch, board, agent, extra=flags) == 0

    printed = capsys.readouterr().out
    report = json.loads(printed)
    records = spans(file_dir)
    by_name = {record["name"]: record for record in records}
    (root,) = [record for record in records if record["name"] == "boundroom.run"]

    children = sorted((r for r in records if r["parent_span_id"] == root["span_id"]), key=lambda r: r["start_time_unix_nano"])
    assert [r["name"] for r in children] == ORDER
    assert {r["trace_id"] for r in records} == {root["trace_id"]} == {report["telemetry"]["trace_id"]}

    payload = board.local.events(task)[-1]["payload"]
    assert by_name["boundroom.handoff.evaluate"]["attributes"]["handoff_sha256"] == payload["handoff_sha256"]
    report_digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode("utf-8")).hexdigest()
    assert by_name["boundroom.decision"]["attributes"]["report_sha256"] == report_digest
    assert by_name["boundroom.agent_session"]["attributes"]["exit_code"] == 0
    # The runner Boundroom was asked for, under a boundroom.* name; nothing it can't know.
    assert by_name["boundroom.agent_session"]["attributes"]["boundroom.runner.requested"] in ("auto", "claude", "codex", "hermes")
    assert not [key for record in records for key in record["attributes"] if key.startswith("gen_ai.")]
    assert by_name["boundroom.select"]["attributes"]["slice_id"] == board.slice_id(task)

    text = (file_dir / next(p.name for p in file_dir.glob("*.spans.jsonl"))).read_text(encoding="utf-8")
    assert SECRET not in text and "planted-telemetry-secret" not in text
    assert "bundle_text" not in text and "handoff_text" not in text  # metadata by default


def test_a_failed_slice_records_its_board_write_and_no_absolute_path(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    # The RA-TEL-007 canary: a slice that failed had no board.record span, and its decision reason
    # carried the handoff's absolute path, which names the person whose machine it is.
    board = local.Board(tmp_path, tasks=1)
    file_dir = tmp_path / "spans"
    flags = ("--telemetry", "file", "--telemetry-file-dir", str(file_dir))
    # An agent that exits 0 without writing its handoff: the reason names the handoff's path.
    assert local.supervise(monkeypatch, board, agent, extra=(*flags, "--agent-cmd", "true")) == 2
    assert "Missing handoff artifact" in json.loads(capsys.readouterr().out)["reason"]

    records = spans(file_dir)
    (root,) = [record for record in records if record["name"] == "boundroom.run"]
    children = sorted((r for r in records if r["parent_span_id"] == root["span_id"]), key=lambda r: r["start_time_unix_nano"])
    assert [r["name"] for r in children][-2:] == ["boundroom.decision", "boundroom.board.record"]
    (decision,) = [r for r in records if r["name"] == "boundroom.decision"]
    assert decision["attributes"]["reason"].startswith("Missing handoff artifact")
    assert "automation/handoffs/" in decision["attributes"]["reason"]
    text = next(file_dir.glob("*.spans.jsonl")).read_text(encoding="utf-8")
    assert str(consumer.resolve()) not in text


def test_without_root_cuts_the_repository_path_out_of_a_reason(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    reason = f"Missing handoff artifact at {root.resolve()}/automation/handoffs/h.json"
    assert telemetry.without_root(reason, root) == "Missing handoff artifact at automation/handoffs/h.json"
    assert telemetry.without_root(None, root) is None


def test_an_exporter_failure_changes_neither_the_verdict_nor_the_exit_code(
    consumer, agent, no_hermes, monkeypatch, capsys, tmp_path
):
    def outcome(extra: tuple[str, ...], name: str) -> tuple[int, str, str]:
        board = local.Board(tmp_path / name, tasks=1)
        code = local.supervise(monkeypatch, board, agent, extra=extra)
        report = json.loads(capsys.readouterr().out)
        return code, report["decision"], report["queue_status"]

    untraced = outcome((), "off")

    def failing_exporter(path: Path, **options: Any):  # type: ignore[no-untyped-def]
        exporter = real(path, **options)
        exporter.export = mock.Mock(side_effect=OSError("disk full"))
        return exporter

    real = telemetry.json_lines_exporter
    with mock.patch.object(telemetry, "json_lines_exporter", failing_exporter):
        traced = outcome(("--telemetry", "file", "--telemetry-file-dir", str(tmp_path / "spans")), "failing")

    assert traced == untraced


class FakeSession:
    def get_span_context(self):  # type: ignore[no-untyped-def]
        return mock.Mock(trace_id=int("ab" * 16, 16), span_id=int("cd" * 8, 16))


def settings_for(**flags: object):  # type: ignore[no-untyped-def]
    return resolve(flags=flags, environ={}, operator_file=None)


def test_ud9_off_gives_the_runner_nothing():
    runner = telemetry.runner_environment(settings_for(), {}, None, "r", "S-1")
    assert runner.variables == {} and runner.passthrough == ()


def test_ud9_on_gives_the_trace_context_and_attributes_only_unless_runner_is_on():
    settings = settings_for(telemetry="otlp", telemetry_endpoint="https://c.example.test")
    runner = telemetry.runner_environment(settings, {}, FakeSession(), "r1", "S-1")
    assert runner.variables == {
        "TRACEPARENT": f"00-{'ab' * 16}-{'cd' * 8}-01",
        "OTEL_RESOURCE_ATTRIBUTES": "boundroom.run_id=r1,boundroom.slice_id=S-1",
    }
    kept = telemetry.runner_environment(settings, {"OTEL_RESOURCE_ATTRIBUTES": "team=x"}, FakeSession(), "r1", "S-1")
    assert "OTEL_RESOURCE_ATTRIBUTES" not in kept.variables


def test_ud9_runner_metadata_points_claude_code_at_the_users_endpoint_and_never_raw_bodies():
    settings = settings_for(telemetry="otlp", telemetry_endpoint="https://c.example.test", telemetry_runner="metadata")
    runner = telemetry.runner_environment(settings, {}, FakeSession(), "r", "S-1")
    assert runner.variables["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert runner.variables["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://c.example.test"
    assert "OTEL_LOG_USER_PROMPTS" not in runner.variables
    content = settings_for(telemetry="otlp", telemetry_endpoint="https://c.example.test", telemetry_runner="content")
    detailed = telemetry.runner_environment(content, {}, FakeSession(), "r", "S-1").variables
    assert detailed["OTEL_LOG_USER_PROMPTS"] == detailed["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert telemetry.NEVER_SET not in detailed


def test_ud9_a_users_own_runner_settings_pass_through_unchanged():
    settings = settings_for(telemetry="otlp", telemetry_endpoint="https://c.example.test", telemetry_runner="metadata")
    environ = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://mine.example.test"}
    runner = telemetry.runner_environment(settings, environ, FakeSession(), "r", "S-1")
    assert set(runner.variables) == {"TRACEPARENT", "OTEL_RESOURCE_ATTRIBUTES"}
    assert runner.passthrough == ("OTEL_EXPORTER_OTLP_ENDPOINT",)
    assert runner.report()["runner_passthrough"] == ["OTEL_EXPORTER_OTLP_ENDPOINT"]


def test_file_mode_sets_no_runner_exporter_and_says_why():
    settings = settings_for(telemetry="file", telemetry_file_dir="/var/tmp/x", telemetry_runner="metadata")
    runner = telemetry.runner_environment(settings, {}, FakeSession(), "r", "S-1")
    assert set(runner.variables) == {"TRACEPARENT", "OTEL_RESOURCE_ATTRIBUTES"}
    assert "otlp" in runner.note


def test_the_runner_sees_the_agent_sessions_trace_context(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    def run(name: str, extra: tuple[str, ...]) -> tuple[dict, dict]:
        board = local.Board(tmp_path / name, tasks=1)
        seen = tmp_path / f"{name}-env.json"
        code = f"import json, os; json.dump(dict(os.environ), open({json.dumps(str(seen))}, 'w'))"
        dump = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        command = f"{dump} && {shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} 0"
        assert local.supervise(monkeypatch, board, agent, extra=(*extra, "--agent-cmd", command)) == 0
        return json.loads(capsys.readouterr().out), json.loads(seen.read_text(encoding="utf-8"))

    file_dir = tmp_path / "spans"
    report, env = run("on", ("--telemetry", "file", "--telemetry-file-dir", str(file_dir)))
    session = next(r for r in spans(file_dir) if r["name"] == "boundroom.agent_session")
    assert env["TRACEPARENT"] == f"00-{report['telemetry']['trace_id']}-{session['span_id']}-01"
    assert report["telemetry"]["runner_variables"] == ["OTEL_RESOURCE_ATTRIBUTES", "TRACEPARENT"]

    _, quiet = run("off", ())
    added = {name for name in quiet if name not in os.environ}
    assert not {name for name in added if name.startswith(("OTEL_", "CLAUDE_CODE_", "TRACEPARENT"))}
