"""A supervised run's span tree in file mode, joined to its records (SPEC-006 RA-TEL-003).

One slice on a local board with no Hermes, traced to a JSON Lines file: the spans form the tree
the spec names, in order, under one ``boundroom.run``; their digests match the board record and
the decision report; nothing planted as a secret reaches a span; and an exporter that fails
leaves the verdict and the exit code exactly as they are with telemetry off.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

from automation.supervisor import telemetry
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
    assert by_name["boundroom.select"]["attributes"]["slice_id"] == board.slice_id(task)

    text = (file_dir / next(p.name for p in file_dir.glob("*.spans.jsonl"))).read_text(encoding="utf-8")
    assert SECRET not in text and "planted-telemetry-secret" not in text
    assert "bundle_text" not in text and "handoff_text" not in text  # metadata by default


def test_an_exporter_failure_changes_neither_the_verdict_nor_the_exit_code(
    consumer, agent, no_hermes, monkeypatch, capsys, tmp_path
):
    def outcome(extra: tuple[str, ...], name: str) -> tuple[int, str, str]:
        board = local.Board(tmp_path / name, tasks=1)
        code = local.supervise(monkeypatch, board, agent, extra=extra)
        report = json.loads(capsys.readouterr().out)
        return code, report["decision"], report["queue_status"]

    untraced = outcome((), "off")

    def failing_exporter(path: Path):  # type: ignore[no-untyped-def]
        exporter = real(path)
        exporter.export = mock.Mock(side_effect=OSError("disk full"))
        return exporter

    real = telemetry.json_lines_exporter
    with mock.patch.object(telemetry, "json_lines_exporter", failing_exporter):
        traced = outcome(("--telemetry", "file", "--telemetry-file-dir", str(tmp_path / "spans")), "failing")

    assert traced == untraced
