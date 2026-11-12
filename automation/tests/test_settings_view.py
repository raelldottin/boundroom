"""The settings views never print a secret and never write or connect (SPEC-008 PS-5, PS-6)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

from automation.supervisor import settings, settings_view

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKERS = ("MARKER-HEADER-TOKEN", "MARKER-OPERATOR-TOKEN", "MARKER-MEMORY-KEY", "/marker-path", "marker=query")

GOLDEN_QUICK = """\
Boundroom settings (quick view)          run with --all for everything
  runner                   auto                               default
  claude profile           none: bypassPermissions            default
  slices per run           2                                  repository queue.policy.consecutive_autonomous_limit
  agent command            from policy                        repository queue.policy.agent_command_template
  work source              queue automation/queue/slices.json default
  memory                   off                                default
  telemetry                off                                default
  supervisor owned paths   automation/queue/slices.json, automation/handoffs/ repository queue.policy.supervisor_owned_paths
  handoff timeout seconds  1800                               repository queue.policy.handoff_timeout_seconds
Warnings
  ! a Claude runner starts in bypassPermissions; choose a profile with BOUNDROOM_CLAUDE_PROFILE
"""


class ViewCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="settings-view-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        (self.repo / "automation/queue").mkdir(parents=True)
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def view(self, *argv: str, environ: dict[str, str] | None = None) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, settings_view.main(list(argv), environ=environ or {}, repo_root=self.repo))
        return out.getvalue()

    def example_queue(self) -> None:
        shutil.copy(REPO_ROOT / "automation/examples/example-slices.json", self.repo / "automation/queue/slices.json")


class GoldenTests(ViewCase):
    def test_the_quick_view_of_a_fresh_repository(self) -> None:
        self.example_queue()
        self.assertEqual(GOLDEN_QUICK, self.view())

    def test_a_legacy_name_in_effect_is_warned_about(self) -> None:
        self.assertIn(
            "! REPO_AUTOMATION_AGENT_RUNNER is set; use BOUNDROOM_AGENT_RUNNER",
            self.view(environ={"REPO_AUTOMATION_AGENT_RUNNER": "hermes"}),
        )

    def test_a_refused_repository_value_is_shown(self) -> None:
        queue = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
        overlay = {"policy": {**queue["policy"], "telemetry": {"capture": "content"}}}
        path = self.tmp / "overlay.json"
        path.write_text(json.dumps(overlay), encoding="utf-8")
        operator = self.home / ".config/boundroom"
        operator.mkdir(parents=True)
        (operator / "telemetry.json").write_text(json.dumps({"capture": "metadata"}), encoding="utf-8")
        self.assertIn("refused: the repository may only lower telemetry_capture", self.view("--kanban-overlay", str(path)))

    def test_an_unreadable_source_is_reported_not_fatal(self) -> None:
        (self.repo / "automation/queue/slices.json").write_text("{not json", encoding="utf-8")
        self.assertIn("could not read", self.view())

    def test_explain_shows_every_source_once(self) -> None:
        explained = json.loads(self.view("--explain", "telemetry_mode", environ={"BOUNDROOM_TELEMETRY": "off"}))
        self.assertEqual("kill switch", explained["source"])
        names = [step["name"] for step in explained["chain"]]
        self.assertEqual(len(names), len(set(names)))

    def test_json_lists_every_setting(self) -> None:
        document = json.loads(self.view("--json"))
        self.assertEqual({setting.id for setting in settings.SETTINGS}, {row["id"] for row in document["settings"]})

    def test_every_area_has_a_place_in_the_extensive_view(self) -> None:
        # An area missing from AREA_ORDER would drop its settings from --all and the reference page.
        self.assertEqual(set(settings_view.AREA_ORDER), set(typing.get_args(settings.Area)))

    def test_an_unknown_setting_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(64, settings_view.main(["--explain", "nope"], environ={}, repo_root=self.repo))


class ReferencePageTests(ViewCase):
    """PS-9: the reference page matches the registry, and the check catches drift."""

    def test_ps9_the_checked_in_page_matches_the_registry(self) -> None:
        self.assertEqual(settings_view.reference_page(), settings_view.REFERENCE_PAGE.read_text(encoding="utf-8"))

    def test_ps9_the_check_fails_on_a_stale_page_and_writes_nothing(self) -> None:
        stale = self.tmp / "settings.md"
        stale.write_text("# stale\n", encoding="utf-8")
        with mock.patch.object(settings_view, "REFERENCE_PAGE", stale), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, settings_view.main(["--docs", "--check"], environ={}, repo_root=self.repo))
        self.assertEqual("# stale\n", stale.read_text(encoding="utf-8"))

    def test_every_setting_has_a_row(self) -> None:
        page = settings_view.reference_page()
        for setting in settings.SETTINGS:
            self.assertIn(f"| `{setting.id}` |", page)


class SecretsTests(ViewCase):
    """PS-5: plant a marker in every secret and URL, then scan every view."""

    def planted(self) -> tuple[dict[str, str], Path]:
        operator = self.home / ".config/boundroom"
        operator.mkdir(parents=True)
        (operator / "telemetry.json").write_text(
            json.dumps(
                {
                    "headers": {"authorization": "MARKER-OPERATOR-TOKEN"},
                    "endpoint": "https://op.example.test/marker-path?marker=query",
                }
            ),
            encoding="utf-8",
        )
        memory = self.tmp / "memory.json"
        memory.write_text(
            json.dumps({"endpoint": "https://memory.example.test/marker-path?marker=query", "api_key_env": "MEMORY_KEY"}),
            encoding="utf-8",
        )
        environ = {
            "BOUNDROOM_TELEMETRY": "otlp",
            "BOUNDROOM_TELEMETRY_ENDPOINT": "https://collector.example.test/marker-path?marker=query",
            "BOUNDROOM_TELEMETRY_HEADERS": "authorization=MARKER-HEADER-TOKEN",
            "MEMORY_KEY": "MARKER-MEMORY-KEY",
        }
        return environ, memory

    def test_ps5_no_view_prints_a_secret_a_path_or_a_query(self) -> None:
        environ, memory = self.planted()
        outputs = [
            self.view("--memory-config", str(memory), environ=environ),
            self.view("--all", "--memory-config", str(memory), environ=environ),
            self.view("--json", "--memory-config", str(memory), environ=environ),
        ]
        outputs += [self.view("--explain", s.id, "--memory-config", str(memory), environ=environ) for s in settings.SETTINGS]
        for output in outputs:
            for marker in MARKERS:
                self.assertNotIn(marker, output)
        joined = "\n".join(outputs)
        self.assertIn("MEMORY_KEY (set)", joined)
        self.assertIn("https://collector.example.test", joined)


class ReadOnlyTests(ViewCase):
    """PS-6: sockets blocked, and the file system unchanged."""

    def snapshot(self) -> dict[str, bytes]:
        return {str(path): path.read_bytes() for path in self.tmp.rglob("*") if path.is_file()}

    def test_ps6_the_views_neither_connect_nor_write(self) -> None:
        self.example_queue()
        before = self.snapshot()

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("a settings view opened a socket")

        with mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "create_connection", refuse):
            for argv in ((), ("--all",), ("--json",), ("--explain", "runner")):
                self.view(
                    *argv, environ={"BOUNDROOM_TELEMETRY": "otlp", "BOUNDROOM_TELEMETRY_ENDPOINT": "https://c.example.test"}
                )
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
