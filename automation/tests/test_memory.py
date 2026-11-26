"""Contract tests for the Hindsight memory adapter (RA-CP-003).

Every call goes to an in-process fake server on 127.0.0.1 or to a patched opener; nothing here
reaches a live Hindsight, writes to ``~/.hermes`` or touches a memory store. The credential and
the offline switch come from an ``environ`` passed in, never from the process environment.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from automation.context import memory
from automation.context.build_context import build_context_bundle
from automation.supervisor import policy
from automation.tests import hindsight_fake
from automation.tests.hindsight_fake import (
    CONFIG,
    EXAMPLES,
    RECALL_RESPONSE,
    REPO_ROOT,
    SECRET,
    closed_port,
    make_config,
)

# The fake Hindsight server fixture, shared by the adapter tests.
fake = hindsight_fake.fake

ENV = {"HINDSIGHT_API_KEY": SECRET}
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def api_key(monkeypatch):
    """The credential in the process environment, for tests that run ``run_next.py``, which has no seam."""
    monkeypatch.setenv("HINDSIGHT_API_KEY", SECRET)
    # A developer shell with the offline switch on would refuse every call.
    monkeypatch.delenv("BOUNDROOM_OFFLINE", raising=False)


def test_example_config_loads():
    config = memory.load_config(CONFIG)
    assert config.projects["example-project"].bank_id == "example-bank"
    assert config.projects["example-project"].operations == ["recall"]


def test_bank_ids_must_be_unique():
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["projects"]["other-project"]["bank_id"] = "example-bank"
    with pytest.raises(ValueError, match="exactly one project"):
        memory.MemoryConfig.model_validate(data)


def verified(index: int) -> dict[str, Any]:
    """The example response's result at ``index``, as the adapter keeps it."""
    item = RECALL_RESPONSE["results"][index]
    return {"id": item["id"], "text": item["text"], **item["metadata"]}


def example_recall() -> dict[str, Any]:
    """What ``recall`` returns for the example response against this repository."""
    return {
        "bank_id": "example-bank",
        "status": "ok",
        "notice": memory.UNTRUSTED_NOTICE,
        "results": [verified(0), verified(1)],
        "rejected": [
            {"id": "fact-003", "reason": "missing provenance: source_path, source_sha256, evidence_ref"},
            {"id": "fact-004", "reason": "source changed since retained"},
            {"id": "fact-005", "reason": "source missing"},
            {"id": "fact-006", "reason": "source_path escapes the repository"},
        ],
        "dropped": 0,
        "truncated": False,
    }


def test_recall_keeps_only_verified_provenance(fake):
    config = make_config(fake.endpoint)

    result = memory.recall(config, "example-project", "example-bank", "focus handoff", REPO_ROOT, environ=ENV)

    assert result == example_recall()
    request = fake.requests[0]
    assert request["path"] == "/v1/default/banks/example-bank/memories/recall"
    assert request["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert request["body"] == {"query": "focus handoff", "types": ["world", "experience"], "budget": "low", "max_tokens": 1024}


def test_changed_source_is_rejected(fake, tmp_path):
    shutil.copytree(EXAMPLES, tmp_path / "automation/examples")
    config = make_config(fake.endpoint)
    assert memory.recall(config, "example-project", "example-bank", "q", tmp_path, environ=ENV)["results"] == [
        verified(0),
        verified(1),
    ]

    with (tmp_path / "automation/examples/example-handoff.json").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    result = memory.recall(config, "example-project", "example-bank", "q", tmp_path, environ=ENV)

    assert result["results"] == [verified(1)]
    assert result["rejected"] == [
        {"id": "fact-001", "reason": "source changed since retained"},
        {"id": "fact-003", "reason": "missing provenance: source_path, source_sha256, evidence_ref"},
        {"id": "fact-004", "reason": "source changed since retained"},
        {"id": "fact-005", "reason": "source missing"},
        {"id": "fact-006", "reason": "source_path escapes the repository"},
    ]


def test_ephemeral_cwd(fake, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    with pytest.raises(FileNotFoundError):
        os.getcwd()

    config = memory.load_config(CONFIG).model_copy(update={"endpoint": fake.endpoint})
    result = memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert result == example_recall()


def test_timeout_is_unavailable(fake):
    fake.delay = 1.0
    config = make_config(fake.endpoint, timeout_seconds=0.2)

    with pytest.raises(memory.MemoryUnavailable, match="timeout"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)


def test_connection_refused_is_unavailable():
    config = make_config(f"http://127.0.0.1:{closed_port()}")

    with pytest.raises(memory.MemoryUnavailable, match="connection refused"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)


def test_egress_denied_is_unavailable(monkeypatch):
    def deny(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError(PermissionError(1, "Operation not permitted"))

    monkeypatch.setattr(memory._OPENER, "open", deny)
    config = make_config("https://hindsight.example.com")

    with pytest.raises(memory.MemoryUnavailable, match="egress denied"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)


@pytest.mark.parametrize(
    ("project", "bank_id", "operation", "message"),
    [
        ("example-project", "other-bank", "recall", "not granted to project"),
        ("unknown-project", "example-bank", "recall", "no memory bank"),
        ("example-project", "example-bank", "retain", "retain is not granted"),
        ("example-project", "example-bank", "reflect", "reflect is not granted"),
    ],
)
def test_unauthorized_calls_never_reach_the_server(fake, project, bank_id, operation, message):
    config = make_config(fake.endpoint)
    calls = {
        "recall": lambda: memory.recall(config, project, bank_id, "q", REPO_ROOT, environ=ENV),
        "retain": lambda: memory.retain(
            config, project, bank_id, REPO_ROOT, "automation/examples/example-slices.json", "c", environ=ENV
        ),
        "reflect": lambda: memory.reflect(config, project, bank_id, "q", environ=ENV),
    }

    with pytest.raises(memory.MemoryAccessError, match=message):
        calls[operation]()

    assert fake.requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_access_errors(fake, status):
    fake.status = status
    fake.body = {"detail": "nope"}

    with pytest.raises(memory.MemoryAccessError, match=f"HTTP {status}"):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)


def test_redirects_are_not_followed(fake):
    fake.status = 307
    fake.headers = {"Location": "http://127.0.0.1:1/elsewhere"}

    with pytest.raises(memory.MemoryUnavailable, match="HTTP 307"):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)
    assert len(fake.requests) == 1


def test_empty_recall(fake):
    fake.body = {"results": []}

    result = memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert result == {
        "bank_id": "example-bank",
        "status": "empty",
        "notice": memory.UNTRUSTED_NOTICE,
        "results": [],
        "rejected": [],
        "dropped": 0,
        "truncated": False,
    }


def test_a_bank_with_nothing_retained_recalls_empty(fake):
    fake.status, fake.body = 404, {"detail": "Bank 'example-bank' not found"}

    result = memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)

    assert result == {
        "bank_id": "example-bank",
        "status": "empty",
        "notice": memory.UNTRUSTED_NOTICE,
        "results": [],
        "rejected": [],
        "dropped": 0,
        "truncated": False,
    }


@pytest.mark.parametrize("detail", ["Not Found", "Bank 'another-bank' not found", None])
def test_other_404s_stay_unavailable(fake, detail):
    fake.status, fake.body = 404, {"detail": detail}

    with pytest.raises(memory.MemoryUnavailable, match="HTTP 404"):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT, environ=ENV)


@pytest.mark.parametrize(
    ("source", "evidence", "message"),
    [("../outside.md", "c", "escapes"), ("/etc/hosts", "c", "repo-relative"), ("README.md", "", "evidence reference")],
)
def test_retain_refuses_bad_sources(fake, source, evidence, message):
    with pytest.raises(memory.MemoryAccessError, match=message):
        memory.retain(make_config(fake.endpoint), "other-project", "other-bank", REPO_ROOT, source, evidence, environ=ENV)
    assert fake.requests == []


def test_retain_respects_request_budget(fake):
    config = make_config(fake.endpoint, max_request_bytes=100)

    with pytest.raises(memory.MemoryAccessError, match="max_request_bytes"):
        memory.retain(
            config, "other-project", "other-bank", REPO_ROOT, "automation/examples/example-slices.json", "c", environ=ENV
        )
    assert fake.requests == []


def bundle(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir(exist_ok=True)
    shutil.copy2(EXAMPLES / "example-handoff.json", handoff_dir / "20260421T153000Z-today-nonfocus-add-to-focus.json")
    queue = policy.load_queue(EXAMPLES / "example-slices.json")
    selected = policy.select_next_slice(queue)
    assert selected is not None
    return build_context_bundle(
        repo_root=REPO_ROOT,
        queue_path=EXAMPLES / "example-slices.json",
        handoff_dir=handoff_dir,
        slice_id=selected["slice_id"],
        max_doc_chars=200,
        now=lambda: NOW,
        **kwargs,
    )


PRE_MEMORY_KEYS = {
    "generated_at",
    "policy_sentence",
    "repo",
    "slice",
    "queue",
    "validation_ownership",
    "previous_handoff",
    "previous_handoff_summary",
    "execution_constraints",
    "handoff_template",
    "documents",
}


def test_bundle_without_memory_is_unchanged(tmp_path):
    plain = bundle(tmp_path)
    explicit_none = bundle(tmp_path, memory_config=None, memory_project=None)

    assert set(plain) == PRE_MEMORY_KEYS
    assert plain["generated_at"] == "2026-01-02T03:04:05+00:00"
    assert plain == explicit_none


def test_bundle_with_memory_has_bounded_section(fake, tmp_path):
    query = "Add regression coverage for Continue Add to Focus interaction"

    section = bundle(tmp_path, memory_config=make_config(fake.endpoint), memory_project="example-project", environ=ENV)["memory"]

    assert section == {
        "source": "hindsight",
        "project": "example-project",
        "query": query,
        **example_recall(),
    }
    assert fake.requests[0]["body"]["query"] == query


def test_bundle_records_unavailable_memory(tmp_path):
    config = make_config(f"http://127.0.0.1:{closed_port()}")

    section = bundle(tmp_path, memory_config=config, memory_project="example-project", environ=ENV)["memory"]

    assert section == {
        "source": "hindsight",
        "project": "example-project",
        "query": "Add regression coverage for Continue Add to Focus interaction",
        "bank_id": "example-bank",
        "status": "unavailable",
        "error": "connection refused",
        "results": [],
    }


def test_bundle_refuses_unmapped_project(fake, tmp_path):
    with pytest.raises(memory.MemoryAccessError, match="^project 'unknown-project' has no memory bank in the map$"):
        bundle(tmp_path, memory_config=make_config(fake.endpoint), memory_project="unknown-project", environ=ENV)
    with pytest.raises(policy.ConfigError) as raised:
        bundle(tmp_path, memory_config=make_config(fake.endpoint), environ=ENV)
    assert type(raised.value) is policy.ConfigError
    assert fake.requests == []
