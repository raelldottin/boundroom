"""Contract tests for the Hindsight snapshot export (RA-CP-004).

Every call goes to the in-process fake Hindsight server on 127.0.0.1 (``hindsight_fake``), with
the credential passed in as ``environ``; snapshots are written only into pytest's temporary
directories.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from automation.context import memory
from automation.tests import hindsight_fake
from automation.tests.hindsight_fake import SECRET, closed_port, make_config

# The fake Hindsight server fixture, shared by the adapter tests.
fake = hindsight_fake.fake

ENV = {"HINDSIGHT_API_KEY": SECRET}

PROJECT = "other-project"
BANK = "other-bank"
CAPTURED_AT = "2026-10-28T00:00:00Z"


def row(index: int, **extra: object) -> dict[str, object]:
    metadata = {
        "source_path": "automation/examples/example-handoff.json",
        "source_sha256": "0" * 64,
        "evidence_ref": "commit:abc1234",
    }
    return {
        "id": f"m{index:04d}",
        "text": f"Synthetic fact {index}",
        "fact_type": "world",
        "metadata": metadata,
        "state": "valid",
        **extra,
    }


def take(config: memory.MemoryConfig) -> dict[str, object]:
    return memory.snapshot(config, PROJECT, BANK, "operator", CAPTURED_AT, environ=ENV)


def test_snapshot_pages_the_whole_bank_in_id_order(fake):
    fake.rows = [row(index) for index in reversed(range(250))]

    document = take(make_config(fake.endpoint))

    assert {key: value for key, value in document.items() if key != "memories"} == {
        "format": "hindsight-snapshot/v1",
        "project": PROJECT,
        "bank_id": BANK,
        "captured_at": CAPTURED_AT,
        "captured_by": "operator",
        "hindsight_revision": memory.HINDSIGHT_REVISION,
        "total": 250,
    }
    assert document["memories"] == [row(index) for index in range(250)]
    assert [request["query"] for request in fake.requests] == [
        {"limit": 100, "offset": 0},
        {"limit": 100, "offset": 100},
        {"limit": 100, "offset": 200},
    ]
    assert {request["path"] for request in fake.requests} == {"/v1/default/banks/other-bank/memories/list"}
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_empty_bank_is_an_empty_snapshot(fake):
    document = take(make_config(fake.endpoint))
    assert document["memories"] == []
    assert document["total"] == 0
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("project", "bank_id", "message"),
    [("example-project", "example-bank", "snapshot is not granted"), (PROJECT, "example-bank", "not granted to project")],
)
def test_unauthorized_snapshot_never_reaches_the_server(fake, project, bank_id, message):
    with pytest.raises(memory.MemoryAccessError, match=message):
        memory.snapshot(make_config(fake.endpoint), project, bank_id, "operator", environ=ENV)
    assert fake.requests == []


def test_a_bank_that_changes_between_pages_is_refused(fake):
    fake.rows = [row(index) for index in range(150)]
    pages = fake.list_page
    fake.list_page = lambda limit, offset: {**pages(limit, offset), "total": 150 + offset}

    with pytest.raises(memory.MemoryUnavailable, match="changed during the snapshot"):
        take(make_config(fake.endpoint))


def test_a_row_repeated_across_pages_is_refused(fake):
    fake.rows = [row(index) for index in range(150)]
    pages = fake.list_page
    fake.list_page = lambda limit, offset: pages(limit, offset - 1 if offset else 0)

    with pytest.raises(memory.MemoryUnavailable, match="changed during the snapshot"):
        take(make_config(fake.endpoint))


def test_a_bank_that_shrinks_mid_snapshot_is_refused(fake):
    fake.rows = [row(index) for index in range(150)]
    fake.list_page = lambda limit, offset: {"items": fake.rows[offset : offset + limit] if not offset else [], "total": 150}

    with pytest.raises(memory.MemoryUnavailable, match="changed during the snapshot"):
        take(make_config(fake.endpoint))


def test_a_bank_over_budget_is_refused_not_truncated(fake):
    fake.rows = [row(index) for index in range(3)]

    with pytest.raises(memory.MemoryAccessError, match="max_snapshot_memories=2"):
        take(make_config(fake.endpoint, max_snapshot_memories=2))
    assert len(fake.requests) == 1


@pytest.mark.parametrize("body", [{"items": [{"text": "no id"}], "total": 1}, {"total": 0}, [1, 2]])
def test_malformed_list_response_is_unavailable(fake, body):
    fake.list_page = lambda limit, offset: body

    with pytest.raises(memory.MemoryUnavailable, match="ListResponse|ListItem"):
        take(make_config(fake.endpoint))


def test_unreachable_server_is_unavailable():
    with pytest.raises(memory.MemoryUnavailable, match="connection refused"):
        take(make_config(f"http://127.0.0.1:{closed_port()}"))


def test_snapshot_needs_an_operator(fake):
    with pytest.raises(memory.MemoryAccessError, match="operator"):
        memory.snapshot(make_config(fake.endpoint), PROJECT, BANK, "  ", environ=ENV)


def test_written_snapshot_is_content_addressed_canonical_and_idempotent(fake, tmp_path):
    fake.rows = [row(2), row(1)]
    document = take(make_config(fake.endpoint))

    path = memory.write_snapshot(document, tmp_path)
    again = memory.write_snapshot(take(make_config(fake.endpoint)), tmp_path)

    data = path.read_bytes()
    assert path == again and path.name == hashlib.sha256(data).hexdigest() + ".json"
    assert data == json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    assert json.loads(data) == document
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_an_edited_snapshot_is_never_overwritten(tmp_path):
    document = {"format": memory.SNAPSHOT_FORMAT, "memories": []}
    path = memory.write_snapshot(document, tmp_path)
    path.write_bytes(b"edited")

    with pytest.raises(memory.MemoryAccessError, match="edited after capture"):
        memory.write_snapshot(document, tmp_path)
    assert path.read_bytes() == b"edited"


def test_snapshot_directory_must_exist_and_not_be_a_symlink(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    for out_dir in (link, tmp_path / "missing"):
        with pytest.raises(memory.MemoryAccessError, match="existing directory"):
            memory.write_snapshot({"memories": []}, out_dir)


def test_non_finite_numbers_are_unavailable(tmp_path):
    with pytest.raises(memory.MemoryUnavailable, match="non-finite"):
        memory.write_snapshot({"memories": [{"id": "m", "score": float("nan")}]}, tmp_path)


def write_config(tmp_path: Path, endpoint: str) -> Path:
    path = tmp_path / "memory-config.json"
    path.write_text(make_config(endpoint).model_dump_json(exclude_none=True), encoding="utf-8")
    return path


def cli(tmp_path: Path, endpoint: str, project: str = PROJECT) -> list[str]:
    out = tmp_path / "snapshots"
    out.mkdir(exist_ok=True)
    return [
        "snapshot",
        "--config",
        str(write_config(tmp_path, endpoint)),
        "--project",
        project,
        "--captured-by",
        "operator",
        "--out",
        str(out),
    ]


def test_cli_writes_one_snapshot(fake, tmp_path, capsys):
    fake.rows = [row(1)]

    assert memory.main(cli(tmp_path, fake.endpoint), now=lambda: datetime(2026, 10, 28, tzinfo=timezone.utc), environ=ENV) == 0

    document = {
        "format": "hindsight-snapshot/v1",
        "project": PROJECT,
        "bank_id": BANK,
        "captured_at": CAPTURED_AT,
        "captured_by": "operator",
        "hindsight_revision": memory.HINDSIGHT_REVISION,
        "memories": [row(1)],
        "total": 1,
    }
    data = memory.snapshot_bytes(document)
    path = tmp_path / "snapshots" / (hashlib.sha256(data).hexdigest() + ".json")
    assert json.loads(capsys.readouterr().out) == {"path": str(path), "bank_id": BANK, "memories": 1}
    assert path.read_bytes() == data


def test_cli_stops_on_access_errors_and_reports_unavailability(fake, tmp_path, capsys):
    assert memory.main(cli(tmp_path, fake.endpoint, project="example-project"), environ=ENV) == 2
    assert capsys.readouterr() == ("", "stop: snapshot is not granted to project 'example-project'\n")
    assert memory.main(cli(tmp_path, f"http://127.0.0.1:{closed_port()}"), environ=ENV) == 1
    assert capsys.readouterr() == ("", "unavailable: connection refused\n")
    assert list((tmp_path / "snapshots").iterdir()) == []
