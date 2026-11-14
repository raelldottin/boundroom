"""One egress chokepoint and the offline switch (SPEC-006 RA-TEL-011).

``BOUNDROOM_OFFLINE`` refuses every network connection Boundroom itself would make, before it is
tried, with a named reason. Every module in ``automation/`` and ``Tools/`` that can open a
connection goes through ``egress.guard_egress``; the meta-test below fails on one that doesn't.
"""

from __future__ import annotations

import ast
import json
import re
import socket
from pathlib import Path

import pytest

from automation.context import memory
from automation.supervisor import egress, telemetry
from automation.supervisor.telemetry_settings import resolve
from automation.tests import test_run_next_local_board as local
from automation.benchmark import tracing

consumer = local.consumer
agent = local.agent
no_hermes = local.no_hermes

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "automation/examples/memory-config.json"
OFFLINE = {"BOUNDROOM_OFFLINE": "1"}

# Imports that can open a network connection.
NETWORK_MODULES = ("urllib.request", "http.client", "socket", "requests", "httpx", "aiohttp", "urllib3", "smtplib", "ftplib")
# Calls that open one, or build a client that will.
CONNECTING_CALLS = {
    "urlopen",
    "create_connection",
    "HTTPConnection",
    "HTTPSConnection",
    "OTLPSpanExporter",
    "OTLPMetricExporter",
    "OTLPLogExporter",
}
# Modules allowed to use the network without the guard, each with the reason. Empty today: add
# an entry only with a reviewed reason, never to make this test pass.
ALLOWLIST: dict[str, str] = {}


def sources() -> list[Path]:
    paths = [p for p in (REPO_ROOT / "automation").rglob("*.py") if "tests" not in p.relative_to(REPO_ROOT).parts]
    return sorted(paths + list((REPO_ROOT / "Tools").rglob("*.py")))


def imports_network(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        if any(name == module or name.startswith(module + ".") for name in names for module in NETWORK_MODULES):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("opentelemetry.exporter.otlp"):
            return True
    return False


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        # An opener's open(), as memory.py's _OPENER.open(...), connects too.
        if func.attr == "open" and isinstance(func.value, ast.Name) and "opener" in func.value.id.lower():
            return "opener.open"
        return func.attr
    return ""


def unguarded_connections(tree: ast.AST) -> list[str]:
    """Functions that connect without calling guard_egress themselves."""
    problems = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)]
        connecting = [name for name in calls if name in CONNECTING_CALLS or name == "opener.open"]
        if connecting and "guard_egress" not in calls:
            problems.append(f"{function.name} calls {', '.join(sorted(set(connecting)))}")
    return problems


def test_every_network_path_goes_through_the_guard():
    checked = []
    for path in sources():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if imports_network(tree):
            checked.append(relative)
            calls = {call_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
            assert "guard_egress" in calls, f"{relative} can open a connection but never calls egress.guard_egress"
        assert unguarded_connections(tree) == [], relative
    # The scan must have found the two network paths that exist today, or it is checking nothing.
    assert {"automation/context/memory.py", "automation/supervisor/telemetry.py"} <= set(checked)


def test_the_meta_test_catches_an_unguarded_module():
    tree = ast.parse("import urllib.request\ndef fetch(url):\n    return urllib.request.urlopen(url)\n")
    assert imports_network(tree)
    assert unguarded_connections(tree) == ["fetch calls urlopen"]
    guarded = ast.parse("def fetch(url):\n    return urlopen(egress.guard_egress('x', url))\n")
    assert unguarded_connections(guarded) == []


def test_the_chokepoint_itself_imports_no_network_module():
    assert not imports_network(ast.parse((REPO_ROOT / "automation/supervisor/egress.py").read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False), ("", False)]
)
def test_the_switch_fails_closed(value, expected):
    assert egress.offline({"BOUNDROOM_OFFLINE": value}) is expected
    assert egress.offline({}) is False


def test_the_guard_returns_the_url_or_refuses_without_naming_it():
    url = "https://collector.example.test/v1/traces?tenant=secret-tenant"
    assert egress.guard_egress("telemetry", url, environ={}) == url
    with pytest.raises(egress.EgressRefused) as caught:
        egress.guard_egress("telemetry", url, environ=OFFLINE)
    assert (caught.value.purpose, caught.value.reason) == ("telemetry", "offline")
    assert "secret-tenant" not in str(caught.value) and "example.test" not in str(caught.value)
    assert "(offline)" in str(caught.value)


def test_offline_turns_otlp_off_at_resolve_time_and_leaves_file_mode_alone(tmp_path):
    flags = {"telemetry": "otlp", "telemetry_endpoint": "https://collector.example.test"}
    settings = resolve(flags=flags, environ=OFFLINE, operator_file=None)
    assert (settings.mode, settings.sources["mode"], settings.refusal) == ("off", "offline", None)
    assert any("otlp mode refused (offline)" in message for message in settings.messages)

    required = resolve(flags={**flags, "telemetry_required": True}, environ=OFFLINE, operator_file=None)
    assert required.refusal and "refusing to start" in required.refusal

    files = resolve(flags={"telemetry": "file", "telemetry_file_dir": str(tmp_path)}, environ=OFFLINE, operator_file=None)
    assert files.mode == "file"


def test_the_benchmark_honours_the_switch():
    flags = {"telemetry": "otlp", "telemetry_endpoint": "https://collector.example.test"}
    assert tracing.benchmark_settings(flags, OFFLINE).mode == "off"


def test_configure_refuses_at_the_chokepoint_even_if_resolve_was_bypassed(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened while offline")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setenv("BOUNDROOM_OFFLINE", "1")
    settings = resolve(flags={"telemetry": "otlp", "telemetry_endpoint": "http://127.0.0.1:9"}, environ={}, operator_file=None)
    assert settings.mode == "otlp"  # resolved without the switch, as a caller that skipped it would
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="r")
    assert not handle.enabled
    assert any("network egress refused (offline)" in message for message in handle.messages)

    required = resolve(
        flags={"telemetry": "otlp", "telemetry_endpoint": "http://127.0.0.1:9", "telemetry_required": True},
        environ={},
        operator_file=None,
    )
    with pytest.raises(telemetry.TelemetryRefused):
        telemetry.configure(required, repo_root=REPO_ROOT, run_id="r")


def test_offline_memory_is_refused_before_any_connection(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened while offline")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("BOUNDROOM_OFFLINE", "1")
    monkeypatch.setenv("HINDSIGHT_API_KEY", "test-key-not-a-real-credential")
    config = memory.load_config(CONFIG)
    with pytest.raises(memory.MemoryOffline, match=r"memory: network egress refused \(offline\)"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT)
    section = memory.recall_section(config, "example-project", "q", REPO_ROOT)
    assert section["status"] == "unavailable"
    assert "(offline)" in section["error"]


def test_a_required_otlp_run_offline_stops_with_ex_config(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    monkeypatch.setenv("BOUNDROOM_OFFLINE", "1")
    flags = ("--telemetry", "otlp", "--telemetry-endpoint", "https://collector.example.test", "--telemetry-required")
    assert local.supervise(monkeypatch, board, agent, extra=flags) == 78
    err = capsys.readouterr().err
    assert "otlp mode refused (offline)" in err and "collector.example.test" not in err
    assert board.status(board.tasks[0]) == "ready", "nothing was claimed"


def test_an_unrequired_otlp_run_offline_runs_untraced(consumer, agent, no_hermes, monkeypatch, capsys, tmp_path):
    board = local.Board(tmp_path, tasks=1)
    monkeypatch.setenv("BOUNDROOM_OFFLINE", "1")
    flags = ("--telemetry", "otlp", "--telemetry-endpoint", "https://collector.example.test")
    assert local.supervise(monkeypatch, board, agent, extra=flags) == 0
    captured = capsys.readouterr()
    assert "otlp mode refused (offline)" in captured.err
    report = json.loads(captured.out)
    assert (report["telemetry"]["mode"], report["telemetry"]["source"]) == ("off", "offline")


def test_the_registry_lists_the_switch_as_env_only():
    from automation.supervisor import settings

    setting = settings.env_names()["BOUNDROOM_OFFLINE"]
    assert (setting.id, setting.area, setting.flags, setting.repository_fields, setting.operator_file) == (
        "offline",
        "network",
        (),
        (),
        None,
    )
    assert re.search(r"BOUNDROOM_OFFLINE", (REPO_ROOT / "docs/reference/settings.md").read_text(encoding="utf-8"))
