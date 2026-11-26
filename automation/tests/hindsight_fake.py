"""A fake Hindsight server on 127.0.0.1 and the memory map the adapter tests share.

Hindsight is an unmanaged out-of-process dependency, so tests stub it here, at the HTTP
boundary, and nowhere else. Not a test module: pytest collects only ``test_*.py``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from automation.context import memory

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "automation/examples"
CONFIG = EXAMPLES / "memory-config.json"
RECALL_RESPONSE = json.loads((EXAMPLES / "memory-recall-response.json").read_text(encoding="utf-8"))
SECRET = "test-key-not-a-real-credential"


class FakeHindsight:
    """Answers every POST with a canned (status, body) after an optional delay, and records requests.

    A GET answers from ``list_page(limit, offset)``, which pages ``rows`` by default.
    """

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = RECALL_RESPONSE
        self.delay = 0.0
        self.headers: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.list_page = lambda limit, offset: {
            "items": self.rows[offset : offset + limit],
            "total": len(self.rows),
            "limit": limit,
            "offset": offset,
        }
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                query = {key: int(value) for key, value in urllib.parse.parse_qsl(parts.query)}
                fake.requests.append({"path": parts.path, "headers": dict(self.headers), "query": query})
                self._answer(fake.list_page(query["limit"], query["offset"]))

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(length))}
                )
                self._answer(fake.body)

            def _answer(self, body: Any) -> None:
                time.sleep(fake.delay)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(fake.status)
                    for name, value in fake.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A delayed handler must not hold up teardown after the client has given up.
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    server = FakeHindsight()
    yield server
    server.server.shutdown()
    server.server.server_close()


def make_config(endpoint: str, **overrides: Any) -> memory.MemoryConfig:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["endpoint"] = endpoint
    data["timeout_seconds"] = 2
    for key, value in overrides.items():
        if key in data["budgets"]:
            data["budgets"][key] = value
        else:
            data[key] = value
    return memory.MemoryConfig.model_validate(data)


def closed_port() -> int:
    """A loopback port nothing listens on: bound once to find a free one, then released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
