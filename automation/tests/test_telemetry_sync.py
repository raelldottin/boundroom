"""The sync lane's spans, from the sync tool's Python core (SPEC-006 RA-TEL-005).

Drives the real ``Tools/boundroom-sync.sh`` against a temporary canonical source and consumer,
traced to a JSON Lines file: ``boundroom.run`` with ``entry=sync``, then ``boundroom.sync.verify``,
``boundroom.sync.plan`` and ``boundroom.sync.write``. Also covers a refusal recorded on the span
where it happened, a ``file_dir`` inside either checkout, off by default with no SDK imported,
a missing SDK, an interpreter without pydantic, and the registry entries for the new flags.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from automation.supervisor import settings, telemetry_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_TOOL = REPO_ROOT / "Tools" / "boundroom-sync.sh"
# The token is planted in the remote on purpose: no span may carry it.
TOKEN = "ghp_plantedSyncTelemetryToken"
REMOTE = f"https://{TOKEN}@github.com/raelldottin/boundroom.git"
PUBLIC_REMOTE = "https://github.com/raelldottin/boundroom.git"


class SyncCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-sync-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "canonical"
        self.target = self.tmp / "consumer"
        self.spans_dir = self.tmp / "spans"
        self.manifest = self.tmp / "manifest.json"
        self.home = self.tmp / "home"  # no operator file, whatever the machine running this has
        self.home.mkdir()
        self.source.mkdir()
        self.target.mkdir()
        self.git(self.source, "init", "-q", "-b", "main")
        self.git(self.source, "remote", "add", "origin", REMOTE)
        for name in ("one.txt", "two.txt", "three.txt"):
            (self.source / "harness" / name).parent.mkdir(exist_ok=True)
            (self.source / "harness" / name).write_text(f"{name}\n", encoding="utf-8")
        self.pin = self.commit(self.source)
        self.git(self.target, "init", "-q", "-b", "main")
        (self.target / "README").write_text("consumer\n", encoding="utf-8")
        (self.target / "vendored").mkdir()
        (self.target / "vendored" / "one.txt").write_text("one.txt\n", encoding="utf-8")  # already current
        (self.target / "vendored" / "gone.txt").write_text("upstream deleted this\n", encoding="utf-8")
        self.commit(self.target)
        entry = {"source": "harness", "destination": "vendored", "kind": "directory"}
        flags = {"preserve_executable": False, "delete_stale": True, "template": False}
        self.manifest.write_text(json.dumps({"version": 1, "entries": [{**entry, **flags}]}), encoding="utf-8")

    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def commit(self, cwd: Path) -> str:
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-q", "-m", "state")
        return self.git(cwd, "rev-parse", "HEAD")

    def environment(self, pythonpath: Optional[Path] = None, **extra: str) -> dict[str, str]:
        env = {
            name: value for name, value in os.environ.items() if not name.startswith(("BOUNDROOM_TELEMETRY", "OTEL_", "LOGFIRE_"))
        }
        # The interpreter running these tests, which has pydantic and logfire, is the python3 the
        # tool's heredoc finds first.
        env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
        env["HOME"] = str(self.home)
        if pythonpath is not None:
            env["PYTHONPATH"] = str(pythonpath)
        return env | extra

    def sync(
        self, *args: str, pin: Optional[str] = None, env: Optional[dict[str, str]] = None
    ) -> subprocess.CompletedProcess[str]:
        provenance = ["--pin", pin, "--expect-remote", REMOTE, "--quality-command", "true"] if pin else []
        return subprocess.run(
            [
                str(SYNC_TOOL),
                *args,
                *provenance,
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(self.target),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env or self.environment(),
        )

    def traced(self, file_dir: Optional[Path] = None) -> tuple[str, ...]:
        return ("--telemetry", "file", "--telemetry-file-dir", str(file_dir or self.spans_dir))

    def spans(self) -> list[dict[str, Any]]:
        (path,) = self.spans_dir.glob("*.spans.jsonl")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def metrics(self) -> dict[tuple[str, str], Any]:
        """Each data point by its name and its attributes, rendered as key=value pairs."""
        (path,) = self.spans_dir.glob("*.metrics.jsonl")
        points = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return {(point["name"], ",".join(f"{k}={v}" for k, v in sorted(point["attributes"].items()))): point for point in points}

    def tree(self) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        records = self.spans()
        (root,) = [record for record in records if record["parent_span_id"] is None]
        children = sorted((r for r in records if r["parent_span_id"] == root["span_id"]), key=lambda r: r["start_time_unix_nano"])
        return records, root, children

    def shadow(self, *packages: str, marker: Path) -> Path:
        """A directory of stand-ins that record their import and fail it, as if not installed."""
        shadow = self.tmp / "shadow"
        for package in packages:
            (shadow / package).mkdir(parents=True)
            (shadow / package / "__init__.py").write_text(
                f"open({str(marker / package)!r}, 'w').close()\nraise ImportError('{package} is not installed here')\n",
                encoding="utf-8",
            )
        marker.mkdir(exist_ok=True)
        return shadow


class SyncSpanTests(SyncCase):
    def test_a_sync_is_one_run_of_verify_plan_and_write(self) -> None:
        result = self.sync("--sync", *self.traced(), pin=self.pin)
        self.assertEqual(0, result.returncode, result.stderr)

        records, root, children = self.tree()
        self.assertEqual("boundroom.run", root["name"])
        self.assertEqual(
            ("sync", "sync", 0), (root["attributes"]["entry"], root["attributes"]["mode"], root["attributes"]["exit_code"])
        )
        self.assertTrue(root["attributes"]["run_id"])
        self.assertEqual({"UNSET"}, {record["status"] for record in records})
        self.assertEqual({root["trace_id"]}, {record["trace_id"] for record in records})
        self.assertEqual(["boundroom.sync.verify", "boundroom.sync.plan", "boundroom.sync.write"], [r["name"] for r in children])

        verify, plan, write = (child["attributes"] for child in children)
        self.assertEqual((self.pin, PUBLIC_REMOTE, "verified"), (verify["pin"], verify["expected_remote"], verify["result"]))
        # Three source files; two of them missing in the consumer and one stale file to remove.
        self.assertEqual((3, 3, "planned"), (plan["file_count"], plan["drift_count"], plan["result"]))
        self.assertEqual((2, 1), (write["written"], write["removed"]))
        self.assertEqual("three.txt\n", (self.target / "vendored" / "three.txt").read_text(encoding="utf-8"))

        text = next(self.spans_dir.glob("*.spans.jsonl")).read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertEqual("", self.git(self.source, "status", "--porcelain"), "the source must stay clean")

        metrics = self.metrics()
        self.assertEqual(2, metrics[("boundroom.sync.changes", "kind=synced")]["value"])
        self.assertEqual(1, metrics[("boundroom.sync.changes", "kind=removed")]["value"])
        self.assertEqual(1, metrics[("boundroom.runs", "entry=sync,outcome=ok")]["value"])
        self.assertEqual(1, metrics[("boundroom.run.duration", "entry=sync,outcome=ok")]["histogram"]["count"])

    def test_a_check_plans_and_writes_nothing(self) -> None:
        result = self.sync("--check", *self.traced())
        self.assertEqual(1, result.returncode, result.stderr)  # drift found
        _, root, children = self.tree()
        self.assertEqual(("check", 1), (root["attributes"]["mode"], root["attributes"]["exit_code"]))
        self.assertEqual(("ERROR", "exit_nonzero"), (root["status"], root["status_description"]))
        self.assertEqual(["boundroom.sync.verify", "boundroom.sync.plan"], [r["name"] for r in children])
        self.assertEqual("unpinned drift report", children[0]["attributes"]["result"])
        self.assertNotIn("pin", children[0]["attributes"])
        self.assertEqual((3, 3), (children[1]["attributes"]["file_count"], children[1]["attributes"]["drift_count"]))
        self.assertFalse((self.target / "vendored" / "two.txt").exists())

    def test_a_refusal_is_recorded_on_the_span_where_it_happened(self) -> None:
        (self.source / "harness" / "one.txt").write_text("unpublished\n", encoding="utf-8")
        self.commit(self.source)  # the source moved past the pin
        result = self.sync("--sync", *self.traced(), pin=self.pin)
        self.assertEqual(2, result.returncode)
        self.assertIn("not at the pinned commit", result.stderr)

        _, root, children = self.tree()
        self.assertEqual(["boundroom.sync.verify"], [r["name"] for r in children])
        self.assertEqual("refused", children[0]["attributes"]["result"])
        self.assertIn("not at the pinned commit", children[0]["attributes"]["reason"])
        self.assertEqual("refused", root["attributes"]["result"])
        # Both spans fail with the reason code, never the refusal's text (SPEC-006 span status).
        self.assertEqual(("ERROR", "refused"), (children[0]["status"], children[0]["status_description"]))
        self.assertEqual(("ERROR", "refused"), (root["status"], root["status_description"]))
        self.assertIn(("boundroom.runs", "entry=sync,outcome=refused"), self.metrics())
        self.assertFalse((self.target / "vendored" / "two.txt").exists())
        self.assertTrue((self.target / "vendored" / "gone.txt").exists())

    def test_a_refusal_quoting_a_remote_never_records_its_token(self) -> None:
        self.git(self.source, "remote", "set-url", "origin", f"https://{TOKEN}@github.com/someone/else.git")
        result = self.sync("--sync", *self.traced(), pin=self.pin)
        self.assertEqual(2, result.returncode)
        _, _, (verify,) = self.tree()
        self.assertIn("identity mismatch", verify["attributes"]["reason"])
        self.assertNotIn(TOKEN, next(self.spans_dir.glob("*.spans.jsonl")).read_text(encoding="utf-8"))

    def test_a_file_dir_inside_either_checkout_is_refused_and_the_sync_carries_on(self) -> None:
        for inside in (self.target / "spans", self.source / "spans"):
            with self.subTest(inside=inside.parent.name):
                result = self.sync("--check", *self.traced(inside))
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn("file_dir must be outside the repository", result.stderr)
                self.assertFalse(inside.exists())

    def test_required_telemetry_that_cant_start_stops_the_sync_before_anything_is_written(self) -> None:
        result = self.sync("--sync", *self.traced(self.target / "spans"), "--telemetry-required", pin=self.pin)
        self.assertEqual(2, result.returncode)
        self.assertIn("refusing to start because telemetry is required", result.stderr)
        self.assertFalse((self.target / "vendored" / "two.txt").exists())


class OffAndMissingTests(SyncCase):
    def test_off_by_default_imports_no_sdk_and_writes_no_spans(self) -> None:
        marker = self.tmp / "imported"
        shadow = self.shadow("logfire", "opentelemetry", marker=marker)
        before = sorted(path for path in self.tmp.rglob("*") if marker not in path.parents)
        result = self.sync("--check", env=self.environment(pythonpath=shadow))
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual([], sorted(path.name for path in marker.iterdir()), "an SDK was imported with telemetry off")
        self.assertEqual("", result.stderr)
        self.assertEqual(before, sorted(path for path in self.tmp.rglob("*") if marker not in path.parents))

    def test_without_logfire_a_traced_sync_warns_and_still_syncs(self) -> None:
        marker = self.tmp / "imported"
        shadow = self.shadow("logfire", marker=marker)
        result = self.sync("--sync", *self.traced(), pin=self.pin, env=self.environment(pythonpath=shadow))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("logfire package isn't installed", result.stderr)
        self.assertEqual(["logfire"], [path.name for path in marker.iterdir()])
        self.assertTrue((self.target / "vendored" / "two.txt").exists())

    def test_without_pydantic_the_sync_works_untraced_and_refuses_telemetry_flags(self) -> None:
        marker = self.tmp / "imported"
        env = self.environment(pythonpath=self.shadow("pydantic", marker=marker))
        self.assertEqual(1, self.sync("--check", env=env).returncode)
        self.assertEqual(1, self.sync("--check", "--no-telemetry", env=env).returncode)
        refused = self.sync("--check", *self.traced(), env=env)
        self.assertEqual(2, refused.returncode)
        self.assertIn("telemetry was asked for, but this interpreter can't load it", refused.stderr)
        warned = self.sync("--check", env=env | {"BOUNDROOM_TELEMETRY": "file"})
        self.assertEqual(1, warned.returncode)
        self.assertIn("telemetry: off, because this interpreter can't load it", warned.stderr)


class RegistryTests(unittest.TestCase):
    def test_the_sync_tool_takes_every_telemetry_flag_run_next_takes_and_the_registry_lists_them(self) -> None:
        parser = argparse.ArgumentParser()
        telemetry_settings.add_flags(parser)
        shared = {option for action in parser._actions for option in action.option_strings if option not in ("-h", "--help")}
        self.assertEqual(7, len(shared))
        help_text = subprocess.run([str(SYNC_TOOL), "--help"], capture_output=True, text=True, check=True).stdout
        registered = settings.flags_for("sync")
        for option in sorted(shared):
            with self.subTest(option=option):
                self.assertIn(option, help_text)
                self.assertEqual("telemetry", registered[option].area)
                self.assertIn(option, settings.flags_for("run_next"))


if __name__ == "__main__":
    unittest.main()
