"""Span status and safe errors (SPEC-006 RA-TEL-008).

A failed or blocked outcome gives its span the ERROR status, described by a reason code. An
exception that escapes a span is recorded by its class and the function it came from, never by
its message or stack trace, so a secret in an exception message never reaches a span.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation.supervisor import telemetry
from automation.supervisor.telemetry_settings import resolve
from automation.tests import test_run_next_local_board as local
from automation.tests.test_telemetry_spans import spans

consumer = local.consumer
agent = local.agent
no_hermes = local.no_hermes

REPO_ROOT = Path(__file__).resolve().parents[2]
PLANTED = "ghp_plantedExceptionSecret0123456789abcdef"


def traced(tmp_path: Path) -> telemetry.Telemetry:
    settings = resolve(flags={"telemetry": "file", "telemetry_file_dir": str(tmp_path / "spans")}, environ={}, operator_file=None)
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="run-1")
    assert handle.enabled
    return handle


def raise_planted() -> None:
    raise ValueError(f"cannot reach https://user:{PLANTED}@example.test/{REPO_ROOT}")


def test_an_escaping_exception_is_recorded_by_type_and_never_by_message(tmp_path):
    handle = traced(tmp_path)
    with pytest.raises(ValueError) as caught:
        with handle.span("boundroom.run"):
            with handle.span("boundroom.agent_session"):
                raise_planted()
    error = caught.value
    assert PLANTED in str(error), "the exception itself is raised again unchanged"
    assert handle.flush()

    text = (tmp_path / "spans/run-1.spans.jsonl").read_text(encoding="utf-8")
    assert PLANTED not in text
    assert str(REPO_ROOT) not in text
    assert "Traceback" not in text and "example.test" not in text
    by_name = {record["name"]: record for record in spans(tmp_path / "spans")}
    for name in ("boundroom.run", "boundroom.agent_session"):
        record = by_name[name]
        assert (record["status"], record["status_description"]) == ("ERROR", "exception:ValueError")
        (event,) = record["events"]
        assert event["name"] == "exception"
        assert set(event["attributes"]) == {"exception.type", "boundroom.exception.where"}
        assert event["attributes"]["exception.type"] == "ValueError"
        # The module's name as pytest imported it, and the function: no file path.
        assert event["attributes"]["boundroom.exception.where"].endswith("test_telemetry_status.raise_planted")


def test_mark_and_fail_set_the_error_status_with_a_reason_code(tmp_path):
    handle = traced(tmp_path)
    with handle.span("boundroom.run") as root:
        handle.mark("boundroom.decision", error_code="stop_failed", verdict="stop_failed")
        handle.mark("boundroom.select", slice_id="S-1")
        handle.fail(root, "Not a code: /home/someone/secret")
    assert handle.flush()
    by_name = {record["name"]: record for record in spans(tmp_path / "spans")}
    assert (by_name["boundroom.decision"]["status"], by_name["boundroom.decision"]["status_description"]) == (
        "ERROR",
        "stop_failed",
    )
    assert by_name["boundroom.select"]["status"] == "UNSET"
    # Anything that isn't shaped like a reason code is replaced, never recorded.
    assert by_name["boundroom.run"]["status_description"] == "error"


def test_status_helpers_cost_nothing_when_telemetry_is_off():
    handle = telemetry.configure(resolve(environ={}, operator_file=None), repo_root=REPO_ROOT, run_id="r")
    with handle.span("boundroom.run") as root:
        handle.fail(root, "exit_nonzero")
        handle.mark("boundroom.decision", error_code="failed")
    with pytest.raises(KeyError):
        with handle.span("boundroom.run"):
            raise KeyError("off: the exception passes through untouched")


def test_verdict_error_names_failures_and_blocks_only():
    for verdict in ("failed", "stop_failed", "stop_blocked", "reject"):
        assert telemetry.verdict_error(verdict) == verdict
    for verdict in ("continue", "stop", "stop_for_review", "accept", None):
        assert telemetry.verdict_error(verdict) is None
    assert telemetry.reason_code("exit_nonzero") == "exit_nonzero"
    assert telemetry.reason_code("Missing handoff at /tmp/x") == "error"


def test_a_slice_without_a_handoff_fails_its_decision_and_run_spans(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    file_dir = tmp_path / "spans"
    flags = ("--telemetry", "file", "--telemetry-file-dir", str(file_dir))
    assert local.supervise(monkeypatch, board, agent, extra=(*flags, "--agent-cmd", "true")) == 2
    json.loads(capsys.readouterr().out)

    by_name = {record["name"]: record for record in spans(file_dir)}
    assert (by_name["boundroom.decision"]["status"], by_name["boundroom.decision"]["status_description"]) == (
        "ERROR",
        "handoff_missing",
    )
    assert (by_name["boundroom.run"]["status"], by_name["boundroom.run"]["status_description"]) == ("ERROR", "exit_nonzero")
    assert by_name["boundroom.run"]["attributes"]["exit_code"] == 2
    assert by_name["boundroom.agent_session"]["status"] == "UNSET"


def test_an_agent_that_exits_non_zero_fails_its_session_span(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    file_dir = tmp_path / "spans"
    flags = ("--telemetry", "file", "--telemetry-file-dir", str(file_dir))
    assert local.supervise(monkeypatch, board, agent, exit_code=3, extra=flags) == 3
    capsys.readouterr()

    by_name = {record["name"]: record for record in spans(file_dir)}
    session = by_name["boundroom.agent_session"]
    assert (session["status"], session["status_description"]) == ("ERROR", "agent_exit_nonzero")
    assert by_name["boundroom.decision"]["status_description"] == "agent_exit_nonzero"


def test_a_passing_slice_leaves_every_status_unset(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    file_dir = tmp_path / "spans"
    assert local.supervise(monkeypatch, board, agent, extra=("--telemetry", "file", "--telemetry-file-dir", str(file_dir))) == 0
    capsys.readouterr()
    assert {record["status"] for record in spans(file_dir)} == {"UNSET"}
