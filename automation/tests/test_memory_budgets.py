"""Output-based tests for the Hindsight adapter's budgets, provenance and failure reports.

Hindsight is the only unmanaged dependency, so it is stubbed at the HTTP boundary and nowhere
else: the in-process fake server from ``hindsight_fake`` on 127.0.0.1, or ``memory._OPENER``. The
credential and the offline switch come from an ``environ`` passed in, never the process
environment, and the snapshot clock is passed in too.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from automation.context import memory
from automation.tests import hindsight_fake
from automation.tests.hindsight_fake import CONFIG, SECRET, closed_port

# The fake Hindsight server fixture, shared by the adapter tests.
fake = hindsight_fake.fake

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV = {"HINDSIGHT_API_KEY": SECRET}
ALPHA = "alpha source\n"
BETA = "beta source é\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def budgets(**overrides: int) -> memory.MemoryBudgets:
    base = {
        "max_request_bytes": 65536,
        "max_response_bytes": 262144,
        "recall_max_tokens": 1024,
        "reflect_max_tokens": 512,
        "max_results": 5,
        "max_result_chars": 600,
        "max_total_chars": 2000,
    }
    return memory.MemoryBudgets(**{**base, **overrides})


def config_data(endpoint: str = "http://127.0.0.1:8888", **budget_overrides: Any) -> dict[str, Any]:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["endpoint"] = endpoint
    data["timeout_seconds"] = 2
    data["budgets"].update(budget_overrides)
    return data


def config(endpoint: str = "http://127.0.0.1:8888", **budget_overrides: Any) -> memory.MemoryConfig:
    return memory.MemoryConfig.model_validate(config_data(endpoint, **budget_overrides))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "a.md").write_text(ALPHA, encoding="utf-8")
    (root / "b.md").write_text(BETA, encoding="utf-8")
    return root


def fact(fact_id: str, text: str, path: str = "a.md", digest: str | None = None, evidence: str = "commit:1") -> dict[str, Any]:
    return {
        "id": fact_id,
        "text": text,
        "metadata": {"source_path": path, "source_sha256": sha(ALPHA) if digest is None else digest, "evidence_ref": evidence},
    }


def kept(fact_id: str, text: str, path: str = "a.md", evidence: str = "commit:1") -> dict[str, Any]:
    digest = sha(ALPHA) if path == "a.md" else sha(BETA)
    return {"id": fact_id, "text": text, "source_path": path, "source_sha256": digest, "evidence_ref": evidence}


def shaped(
    results: list[dict[str, Any]], rejected: list[dict[str, str]] | None = None, dropped: int = 0, truncated: bool = False
) -> dict[str, Any]:
    return {
        "status": "ok" if results else "empty",
        "notice": memory.UNTRUSTED_NOTICE,
        "results": results,
        "rejected": rejected or [],
        "dropped": dropped,
        "truncated": truncated,
    }


# shape_recall: provenance first, then budgets, in server order.


def test_verified_results_within_budget_are_kept_whole(repo):
    response = {"results": [fact("r1", "first"), fact("r2", "second", "b.md", sha(BETA), "handoff:x")]}

    assert memory.shape_recall(response, repo, budgets()) == shaped(
        [kept("r1", "first"), kept("r2", "second", "b.md", "handoff:x")]
    )


def test_the_result_count_budget_drops_the_rest(repo):
    response = {"results": [fact("r1", "first"), fact("r2", "second")]}

    assert memory.shape_recall(response, repo, budgets(max_results=1)) == shaped([kept("r1", "first")], dropped=1, truncated=True)


def test_a_spent_character_budget_drops_the_rest(repo):
    response = {"results": [fact("r1", "first"), fact("r2", "second")]}

    assert memory.shape_recall(response, repo, budgets(max_total_chars=5)) == shaped(
        [kept("r1", "first")], dropped=1, truncated=True
    )


def test_the_character_budget_left_cuts_the_next_result(repo):
    response = {"results": [fact("r1", "first"), fact("r2", "second")]}

    assert memory.shape_recall(response, repo, budgets(max_total_chars=8)) == shaped(
        [kept("r1", "first"), kept("r2", "sec")], truncated=True
    )


def test_the_per_result_budget_cuts_each_result(repo):
    response = {"results": [fact("r1", "first"), fact("r2", "second")]}

    assert memory.shape_recall(response, repo, budgets(max_result_chars=3)) == shaped(
        [kept("r1", "fir"), kept("r2", "sec")], truncated=True
    )


def test_every_rejection_reason_is_reported_sorted_and_spends_no_budget(repo):
    response = {
        "results": [
            {"id": "z", "text": "t", "metadata": {"source_path": "a.md"}},
            {"id": "y", "text": "t", "metadata": None},
            fact("x", "t", "../outside.md"),
            fact("w", "t", "/etc/hosts"),
            fact("v", "t", "gone.md"),
            fact("u", "t", "docs"),
            fact("t", "t", "a.md", "0" * 64),
            fact("dup", "t", "gone.md"),
            {"id": "dup", "text": "t", "metadata": {"source_path": "a.md", "source_sha256": "", "evidence_ref": "c"}},
            fact("s", "kept"),
        ]
    }

    assert memory.shape_recall(response, repo, budgets(max_results=1)) == shaped(
        [kept("s", "kept")],
        rejected=[
            {"id": "dup", "reason": "missing provenance: source_sha256"},
            {"id": "dup", "reason": "source missing"},
            {"id": "t", "reason": "source changed since retained"},
            {"id": "u", "reason": "source missing"},
            {"id": "v", "reason": "source missing"},
            {"id": "w", "reason": "source_path escapes the repository"},
            {"id": "x", "reason": "source_path escapes the repository"},
            {"id": "y", "reason": "missing provenance: source_path, source_sha256, evidence_ref"},
            {"id": "z", "reason": "missing provenance: source_sha256, evidence_ref"},
        ],
    )


def test_one_source_is_checked_against_each_results_own_digest(repo):
    response = {"results": [fact("r1", "first"), fact("r2", "stale", "a.md", sha("older\n"))]}

    assert memory.shape_recall(response, repo, budgets()) == shaped(
        [kept("r1", "first")], rejected=[{"id": "r2", "reason": "source changed since retained"}]
    )


def test_nothing_verified_is_empty(repo):
    response = {"results": [fact("r1", "t", "gone.md")]}

    assert memory.shape_recall(response, repo, budgets()) == shaped([], rejected=[{"id": "r1", "reason": "source missing"}])


# Request and response limits, at the byte.

REFLECT_PAYLOAD = '{"budget": "low", "max_tokens": 512, "query": "é"}'.encode("utf-8")


def test_a_request_of_exactly_the_byte_budget_is_sent(fake):
    fake.body = {"text": "ok"}

    result = memory.reflect(
        config(fake.endpoint, max_request_bytes=len(REFLECT_PAYLOAD)), "other-project", "other-bank", "é", environ=ENV
    )

    assert result["text"] == "ok"
    assert fake.requests[0]["body"] == {"query": "é", "budget": "low", "max_tokens": 512}


def test_a_request_one_byte_over_budget_is_refused_before_sending(fake):
    limit = len(REFLECT_PAYLOAD) - 1

    with pytest.raises(memory.MemoryAccessError) as raised:
        memory.reflect(config(fake.endpoint, max_request_bytes=limit), "other-project", "other-bank", "é", environ=ENV)

    assert str(raised.value) == f"request of {len(REFLECT_PAYLOAD)} bytes exceeds max_request_bytes={limit}"
    assert fake.requests == []


def test_a_response_of_exactly_the_byte_budget_is_read(fake):
    fake.body = b'{"text": "ok"}'

    result = memory.reflect(
        config(fake.endpoint, max_response_bytes=len(fake.body)), "other-project", "other-bank", "q", environ=ENV
    )

    assert result["text"] == "ok"


def test_a_response_one_byte_over_budget_is_unavailable(fake):
    fake.body = b'{"text": "ok"}'
    limit = len(fake.body) - 1

    with pytest.raises(memory.MemoryUnavailable) as raised:
        memory.reflect(config(fake.endpoint, max_response_bytes=limit), "other-project", "other-bank", "q", environ=ENV)

    assert str(raised.value) == f"response exceeds max_response_bytes={limit}"


# Failures at the HTTP boundary, each with its report.


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (urllib.error.URLError(TimeoutError()), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError()), "connection refused"),
        (urllib.error.URLError(PermissionError()), "egress denied"),
        (urllib.error.URLError(OSError(5, "Input/output error")), "network error: Input/output error"),
        (urllib.error.URLError(OSError()), "network error: OSError"),
        (urllib.error.URLError("unknown host"), "network error: unknown host"),
        (TimeoutError(), "timeout"),
        (OSError(54, "Connection reset by peer"), "network error: Connection reset by peer"),
    ],
)
def test_network_failures_are_unavailable_with_their_reason(monkeypatch, error, message):
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(memory._OPENER, "open", fail)

    with pytest.raises(memory.MemoryUnavailable) as raised:
        memory.recall(config("https://hindsight.example.com"), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert (type(raised.value), str(raised.value)) == (memory.MemoryUnavailable, message)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json", "response is not JSON"),
        ({"results": "nope"}, "response does not match the pinned RecallResponse shape"),
        ({"unexpected": []}, "response does not match the pinned RecallResponse shape"),
    ],
)
def test_a_malformed_response_names_what_was_wrong(fake, body, message):
    fake.body = body

    with pytest.raises(memory.MemoryUnavailable) as raised:
        memory.recall(config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert str(raised.value) == message


class CannedResponse:
    """What ``_OPENER.open`` returns: a context manager whose body is ``body``."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> CannedResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


def test_the_request_url_is_built_from_the_endpoint_bank_and_route(monkeypatch):
    # The fake server collapses a doubled slash, so the URL is read at the opener instead.
    urls: list[str] = []

    def record(request: Any, timeout: float) -> CannedResponse:
        urls.append(request.full_url)
        return CannedResponse(b'{"results": []}')

    monkeypatch.setattr(memory._OPENER, "open", record)

    memory.recall(config("https://hindsight.example.com/"), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert urls == ["https://hindsight.example.com/v1/default/banks/example-bank/memories/recall"]


# Credentials and the offline switch come from the environ passed in.


def test_the_credential_is_sent_as_a_bearer_token(fake):
    fake.body = {"results": []}

    memory.recall(config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_a_missing_credential_is_refused_before_connecting(fake):
    with pytest.raises(memory.MemoryAccessError) as raised:
        memory.recall(config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ={})

    assert str(raised.value) == "credential variable HINDSIGHT_API_KEY is not set"
    assert fake.requests == []


def test_a_map_without_a_credential_variable_sends_no_authorization(fake):
    data = config_data(fake.endpoint)
    del data["api_key_env"]
    fake.body = {"results": []}

    memory.recall(memory.MemoryConfig.model_validate(data), "example-project", "example-bank", "q", REPO_ROOT, environ={})

    assert "Authorization" not in fake.requests[0]["headers"]


def test_offline_refuses_before_connecting(fake):
    with pytest.raises(memory.MemoryOffline) as raised:
        memory.recall(
            config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ={**ENV, "BOUNDROOM_OFFLINE": "1"}
        )

    assert str(raised.value) == "memory: network egress refused (offline): BOUNDROOM_OFFLINE is set"
    assert fake.requests == []


# The operator's map.


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("ftp://127.0.0.1", "endpoint must be an http(s) URL with a host"),
        ("https://", "endpoint must be an http(s) URL with a host"),
        ("https://user@hindsight.example.com", "endpoint must not carry credentials, a query or a fragment"),
        ("https://:pw@hindsight.example.com", "endpoint must not carry credentials, a query or a fragment"),
        ("https://hindsight.example.com/?key=x", "endpoint must not carry credentials, a query or a fragment"),
        ("https://hindsight.example.com/#x", "endpoint must not carry credentials, a query or a fragment"),
        ("http://hindsight.example.com", "plain http is allowed only to a loopback host"),
        ("http://10.0.0.1:8888", "plain http is allowed only to a loopback host"),
    ],
)
def test_endpoint_refusals_name_the_rule(endpoint, message):
    with pytest.raises(ValueError) as raised:
        memory.check_endpoint(endpoint)

    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://hindsight.example.com", "plain http is allowed only to a loopback host"),
        ("https://user:pw@hindsight.example.com", "endpoint must not carry credentials, a query or a fragment"),
    ],
)
def test_the_map_refuses_an_endpoint_the_rules_refuse(endpoint, message):
    with pytest.raises(ValidationError) as raised:
        memory.MemoryConfig.model_validate(config_data(endpoint))

    assert [error["msg"] for error in raised.value.errors()] == [f"Value error, {message}"]


@pytest.mark.parametrize(
    "endpoint", ["https://hindsight.example.com", "http://localhost:8888", "http://127.0.0.2:8888", "http://[::1]:8888"]
)
def test_endpoints_that_are_allowed(endpoint):
    assert memory.check_endpoint(endpoint) is None


@pytest.mark.parametrize(
    "budget",
    [
        "max_request_bytes",
        "max_response_bytes",
        "recall_max_tokens",
        "reflect_max_tokens",
        "max_results",
        "max_result_chars",
        "max_total_chars",
        "max_snapshot_memories",
    ],
)
def test_every_budget_accepts_one_and_refuses_zero(budget):
    data = config_data()
    data["budgets"][budget] = 1
    assert getattr(memory.MemoryConfig.model_validate(data).budgets, budget) == 1
    data["budgets"][budget] = 0
    with pytest.raises(ValidationError):
        memory.MemoryConfig.model_validate(data)


@pytest.mark.parametrize(("seconds", "valid"), [(0.5, True), (60, True), (0, False), (61, False)])
def test_the_timeout_is_above_zero_and_at_most_a_minute(seconds, valid):
    data = {**config_data(), "timeout_seconds": seconds}
    if valid:
        assert memory.MemoryConfig.model_validate(data).timeout_seconds == seconds
    else:
        with pytest.raises(ValidationError):
            memory.MemoryConfig.model_validate(data)


def test_a_grant_needs_an_operation():
    data = config_data()
    data["projects"]["example-project"]["operations"] = []

    with pytest.raises(ValidationError):
        memory.MemoryConfig.model_validate(data)


def test_load_config_reports_an_unreadable_file(tmp_path):
    missing = tmp_path / "missing.json"

    with pytest.raises(memory.MemoryAccessError) as raised:
        memory.load_config(missing)

    assert str(raised.value) == f"cannot read memory config {missing}: No such file or directory"


def test_load_config_reports_an_invalid_map(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(memory.MemoryAccessError) as raised:
        memory.load_config(path)

    assert str(raised.value).startswith(f"invalid memory config {path}: ")


def test_authorize_returns_the_grant():
    loaded = config()

    assert memory.authorize(loaded, "other-project", "other-bank", "reflect") is loaded.projects["other-project"]


# Whole outputs of retain, reflect and the bundle section.


def test_retain_sends_the_whole_file_with_its_digest_and_returns_the_receipt(fake, repo):
    fake.body = {"success": True, "bank_id": "other-bank", "items_count": 1}

    receipt = memory.retain(config(fake.endpoint), "other-project", "other-bank", repo, "b.md", "commit:abc", environ=ENV)

    assert receipt == {
        "bank_id": "other-bank",
        "document_id": "b.md",
        "source_sha256": sha(BETA),
        "server_success": True,
        "server_items_count": 1,
    }
    assert fake.requests[0]["path"] == "/v1/default/banks/other-bank/memories"
    assert fake.requests[0]["body"] == {
        "items": [
            {
                "content": BETA,
                "document_id": "b.md",
                "update_mode": "replace",
                "metadata": {"source_path": "b.md", "source_sha256": sha(BETA), "evidence_ref": "commit:abc"},
            }
        ],
        "async": False,
    }


def test_retain_refuses_an_empty_source_path(fake, repo):
    with pytest.raises(memory.MemoryAccessError, match="^source_path must be repo-relative$"):
        memory.retain(config(fake.endpoint), "other-project", "other-bank", repo, "", "commit:abc", environ=ENV)
    assert fake.requests == []


def test_a_short_reflection_is_whole_and_not_truncated(fake):
    fake.body = {"text": "a summary"}

    assert memory.reflect(config(fake.endpoint), "other-project", "other-bank", "q", environ=ENV) == {
        "bank_id": "other-bank",
        "kind": "generated-reflection",
        "notice": memory.UNTRUSTED_NOTICE,
        "text": "a summary",
        "truncated": False,
    }


def test_a_long_reflection_is_cut_to_the_total_budget(fake):
    fake.body = {"text": "x" * 11}

    result = memory.reflect(config(fake.endpoint, max_total_chars=10), "other-project", "other-bank", "q", environ=ENV)

    assert result == {
        "bank_id": "other-bank",
        "kind": "generated-reflection",
        "notice": memory.UNTRUSTED_NOTICE,
        "text": "x" * 10,
        "truncated": True,
    }


def test_the_bundle_section_carries_the_recall(fake, repo):
    fake.body = {"results": [fact("r1", "first")]}

    section = memory.recall_section(config(fake.endpoint), "example-project", "the slice", repo, environ=ENV)

    assert section == {
        "source": "hindsight",
        "project": "example-project",
        "query": "the slice",
        "bank_id": "example-bank",
        **shaped([kept("r1", "first")]),
    }


def test_the_bundle_section_says_when_hindsight_is_unavailable(repo):
    endpoint = f"http://127.0.0.1:{closed_port()}"

    section = memory.recall_section(config(endpoint), "example-project", "the slice", repo, environ=ENV)

    assert section == {
        "source": "hindsight",
        "project": "example-project",
        "query": "the slice",
        "bank_id": "example-bank",
        "status": "unavailable",
        "error": "connection refused",
        "results": [],
    }


def test_the_bundle_section_refuses_an_unmapped_project(repo):
    with pytest.raises(memory.MemoryAccessError, match="^project 'nope' has no memory bank in the map$"):
        memory.recall_section(config(), "nope", "q", repo, environ=ENV)


# Snapshots.

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def rows(count: int) -> list[dict[str, Any]]:
    return [{"id": f"m{index}", "text": f"fact {index}"} for index in range(count)]


def test_a_bank_exactly_at_the_snapshot_limit_is_exported(fake):
    fake.rows = rows(3)

    document = memory.snapshot(
        config(fake.endpoint, max_snapshot_memories=3), "other-project", "other-bank", "op", now=lambda: NOW, environ=ENV
    )

    assert document == {
        "format": "hindsight-snapshot/v1",
        "project": "other-project",
        "bank_id": "other-bank",
        "captured_at": "2026-01-02T03:04:05Z",
        "captured_by": "op",
        "hindsight_revision": memory.HINDSIGHT_REVISION,
        "memories": rows(3),
        "total": 3,
    }


def test_a_map_without_a_snapshot_limit_uses_the_default(fake):
    data = config_data(fake.endpoint)
    del data["budgets"]["max_snapshot_memories"]
    fake.rows = rows(2)

    document = memory.snapshot(memory.MemoryConfig.model_validate(data), "other-project", "other-bank", "op", "t", environ=ENV)

    assert document == {
        "format": "hindsight-snapshot/v1",
        "project": "other-project",
        "bank_id": "other-bank",
        "captured_at": "t",
        "captured_by": "op",
        "hindsight_revision": memory.HINDSIGHT_REVISION,
        "memories": rows(2),
        "total": 2,
    }


def test_an_explicit_capture_time_wins_over_the_clock(fake):
    document = memory.snapshot(
        config(fake.endpoint), "other-project", "other-bank", "op", "2025-12-31T00:00:00Z", now=lambda: NOW, environ=ENV
    )

    assert document == {
        "format": "hindsight-snapshot/v1",
        "project": "other-project",
        "bank_id": "other-bank",
        "captured_at": "2025-12-31T00:00:00Z",
        "captured_by": "op",
        "hindsight_revision": memory.HINDSIGHT_REVISION,
        "memories": [],
        "total": 0,
    }


def test_a_server_returning_more_rows_than_its_total_is_refused(fake):
    fake.rows = rows(3)
    fake.list_page = lambda limit, offset: {"items": fake.rows[offset : offset + limit], "total": 2}

    with pytest.raises(memory.MemoryUnavailable, match="^bank changed during the snapshot; capture again$"):
        memory.snapshot(config(fake.endpoint), "other-project", "other-bank", "op", "t", environ=ENV)


def test_a_negative_total_is_a_malformed_response(fake):
    fake.list_page = lambda limit, offset: {"items": [], "total": -1}

    with pytest.raises(memory.MemoryUnavailable, match="^response does not match the pinned ListResponse shape$"):
        memory.snapshot(config(fake.endpoint), "other-project", "other-bank", "op", "t", environ=ENV)


def test_snapshot_bytes_are_canonical_ascii():
    assert memory.snapshot_bytes({"b": [1, 2], "a": "é"}) == b'{"a":"\\u00e9","b":[1,2]}'


def test_writing_the_same_snapshot_again_leaves_the_file_alone(tmp_path):
    document = {"format": memory.SNAPSHOT_FORMAT, "memories": rows(1)}
    first = memory.write_snapshot(document, tmp_path)
    data = first.read_bytes()
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))

    again = memory.write_snapshot(document, tmp_path)

    assert again == first
    assert (first.read_bytes(), first.stat().st_mtime_ns) == (data, 1_000_000_000)
    assert sorted(path.name for path in tmp_path.iterdir()) == [first.name]


# The snapshot command.


def cli_args(tmp_path: Path, endpoint: str, project: str = "other-project") -> list[str]:
    path = tmp_path / "memory-config.json"
    path.write_text(json.dumps(config_data(endpoint)), encoding="utf-8")
    out = tmp_path / "snapshots"
    out.mkdir(exist_ok=True)
    return ["snapshot", "--config", str(path), "--project", project, "--captured-by", "op", "--out", str(out)]


def test_the_command_writes_the_snapshot_and_reports_it(fake, tmp_path, capsys):
    fake.rows = rows(2)
    expected = memory.snapshot_bytes(
        {
            "format": "hindsight-snapshot/v1",
            "project": "other-project",
            "bank_id": "other-bank",
            "captured_at": "2026-01-02T03:04:05Z",
            "captured_by": "op",
            "hindsight_revision": memory.HINDSIGHT_REVISION,
            "memories": rows(2),
            "total": 2,
        }
    )
    path = tmp_path / "snapshots" / (hashlib.sha256(expected).hexdigest() + ".json")

    code = memory.main(cli_args(tmp_path, fake.endpoint), now=lambda: NOW, environ=ENV)

    assert code == 0
    assert capsys.readouterr() == (json.dumps({"path": str(path), "bank_id": "other-bank", "memories": 2}) + "\n", "")
    assert path.read_bytes() == expected


def test_the_command_stops_on_an_unmapped_project(fake, tmp_path, capsys):
    code = memory.main(cli_args(tmp_path, fake.endpoint, project="nope"), environ=ENV)

    assert (code, capsys.readouterr()) == (2, ("", "stop: project 'nope' has no memory bank in the map\n"))
    assert fake.requests == []


def test_the_command_reports_an_unreachable_server(tmp_path, capsys):
    code = memory.main(cli_args(tmp_path, f"http://127.0.0.1:{closed_port()}"), environ=ENV)

    assert (code, capsys.readouterr()) == (1, ("", "unavailable: connection refused\n"))
    assert list((tmp_path / "snapshots").iterdir()) == []


def test_the_module_runs_as_a_command(tmp_path):
    missing = tmp_path / "missing.json"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "automation.context.memory",
            "snapshot",
            "--config",
            str(missing),
            "--project",
            "p",
            "--captured-by",
            "op",
            "--out",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        f"stop: cannot read memory config {missing}: No such file or directory\n",
    )
