"""Guards on the canonical -> consumer import path.

Boundroom is the canonical source of reusable automation. Consumers vendor a pinned
snapshot of it and never write back. These tests pin the nine protections that make that
direction unbreakable; they are the canonical copy, and consumers are expected to keep
their own equivalents against their vendored checkout.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any, Iterator, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_TOOL = REPO_ROOT / "Tools" / "boundroom-sync.sh"


class BoundroomImportGuardTests(unittest.TestCase):
    """The nine protections that make the canonical -> consumer direction unbreakable.

    Boundroom is canonical and Owlory vendors a pinned snapshot of it. Every test
    here drives the real tool the way `make repo-automation-import` drives it: a pinned,
    published, clean canonical source importing into a consumer checkout. Each protection
    must exit non-zero and leave the filesystem untouched.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="boundroom-guard-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.source = self.tmpdir / "canonical"
        self.target = self.tmpdir / "consumer"
        self.source.mkdir()
        self.target.mkdir()
        # Outside the source tree on purpose: a manifest written into the canonical checkout
        # would leave it dirty, which protection 4 correctly refuses.
        self.manifest = self.tmpdir / "manifest.json"
        self.remote = "https://github.com/raelldottin/boundroom.git"

    # --- helpers ---------------------------------------------------------

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, msg=f"git {' '.join(args)}: {result.stderr}")
        return result

    def write_source(self, relative: str, contents: str) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def write_target(self, relative: str, contents: str) -> Path:
        path = self.target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def write_manifest(self, entries: list[dict[str, Any]], **extra: Any) -> None:
        self.manifest.write_text(
            json.dumps({"version": 1, **extra, "entries": entries}, indent=2),
            encoding="utf-8",
        )

    def entry(
        self,
        source: str,
        destination: str,
        kind: str = "file",
        delete_stale: bool = False,
        template: bool = False,
        component: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "source": source,
            "destination": destination,
            "kind": kind,
            "preserve_executable": False,
            "delete_stale": delete_stale,
            "template": template,
        }
        if component is not None:
            entry["component"] = component
        return entry

    def init_canonical_source(self) -> None:
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.email", "canonical@example.com")
        self.git(self.source, "config", "user.name", "Canonical")
        self.git(self.source, "remote", "add", "origin", self.remote)

    def publish_source(self, message: str = "canonical state") -> str:
        """Commit everything in the canonical checkout and return the published sha."""
        self.git(self.source, "add", "-A")
        self.git(self.source, "commit", "-m", message)
        return self.git(self.source, "rev-parse", "HEAD").stdout.strip()

    def canonical_source_with(self, *files: tuple[str, str]) -> str:
        """A published, clean canonical checkout containing `files`. Returns its HEAD sha."""
        self.init_canonical_source()
        for relative, contents in files or (("seed.txt", "seed\n"),):
            self.write_source(relative, contents)
        return self.publish_source()

    def init_consumer(self) -> None:
        self.git(self.target, "init", "-b", "main")
        self.git(self.target, "config", "user.email", "consumer@example.com")
        self.git(self.target, "config", "user.name", "Consumer")

    def commit_consumer(self) -> None:
        self.git(self.target, "add", "-A")
        self.git(self.target, "commit", "-m", "consumer state")

    def run_tool(
        self,
        *args: str,
        pin: str | None = None,
        env_path: str | None = None,
        quality_command: str = "true",
        runtime_python: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # Fixture sources are bare git repos with no build system, so the canonical quality
        # gate is stubbed out here. Protection 8 exercises the real gate directly.
        provenance: list[str] = (
            ["--pin", pin, "--expect-remote", self.remote, "--quality-command", quality_command] if pin else []
        )
        env = None
        if env_path is not None:
            # Used to take git away from the tool mid-run, which is one of the ways a guard
            # that reads returncode alone quietly stops guarding.
            env = {**os.environ, "PATH": env_path}
        runtime = ["--runtime-python", runtime_python] if runtime_python else []
        return subprocess.run(
            [
                str(SYNC_TOOL),
                *args,
                *provenance,
                *runtime,
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
            env=env,
        )

    # --- protection 1 ----------------------------------------------------

    def test_1_no_import_can_mutate_the_canonical_source(self) -> None:
        """Owlory has no write path outward: the tool refuses to write into its own source."""
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--pin",
                pin,
                "--expect-remote",
                self.remote,
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(self.source),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("canonical source", result.stderr)
        self.assertEqual(
            pin,
            self.git(self.source, "rev-parse", "HEAD").stdout.strip(),
            msg="the canonical source must be byte-for-byte untouched",
        )
        self.assertEqual([], [line for line in self.git(self.source, "status", "--short").stdout.splitlines()])

    def test_1b_the_outward_auto_update_mode_no_longer_exists(self) -> None:
        """--auto-update was the outward mutation path. It must not be reachable at all."""
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", "--auto-update", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unrecognized arguments: --auto-update", result.stderr)
        self.assertNotIn("--auto-update", self.run_tool("--help").stdout)

    def test_1c_a_sibling_checkout_of_the_canonical_repo_is_refused(self) -> None:
        """Containment does not establish identity.

        Two checkouts side by side contain neither the other, so the destructive case looked
        like an ordinary import. This is the one the default target used to point at.
        """
        self.canonical_source_with(("harness/keep.py", "CANONICAL\n"))

        sibling = self.tmpdir / "canonical-sibling"
        self.git(self.tmpdir, "clone", "--quiet", str(self.source), str(sibling))
        self.git(sibling, "remote", "set-url", "origin", self.remote)
        (sibling / "harness/only-upstream.py").write_text("upstream only\n", encoding="utf-8")
        self.git(sibling, "add", "-A")
        self.git(sibling, "-c", "user.email=s@example.com", "-c", "user.name=S", "commit", "-m", "sibling")
        before = (sibling / "harness/keep.py").read_text(encoding="utf-8")

        self.write_source("harness/keep.py", "PAYLOAD\n")
        pin = self.publish_source("payload")
        self.write_manifest([self.entry("harness", "automation/harness", kind="directory", delete_stale=True)])

        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--pin",
                pin,
                "--expect-remote",
                self.remote,
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(sibling),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("canonical", result.stderr)
        self.assertEqual(before, (sibling / "harness/keep.py").read_text(encoding="utf-8"))
        self.assertTrue(
            (sibling / "harness/only-upstream.py").exists(),
            msg="delete_stale must never reach a checkout of the canonical repository",
        )

    def test_1d_a_target_whose_origin_is_the_canonical_repo_is_refused(self) -> None:
        """An independent clone shares no git dir, so identity falls back to the remote."""
        pin = self.canonical_source_with()
        self.init_consumer()
        self.git(self.target, "remote", "add", "origin", self.remote)
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("origin", result.stderr)
        self.assertFalse((self.target / "automation/seed.txt").exists())

    def test_1e_there_is_no_default_destination(self) -> None:
        """A manifest cannot name where the import lands - the caller must say.

        default_target named the canonical repository, which was correct before the
        ownership flip and a --sync away from writing outward after it.
        """
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        no_target = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--pin",
                pin,
                "--expect-remote",
                self.remote,
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, no_target.returncode)
        self.assertIn("--target", no_target.stderr)

        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")], default_target=str(self.target))
        reintroduced = self.run_tool("--sync", pin=pin)
        self.assertNotEqual(0, reintroduced.returncode)
        self.assertIn("default_target", reintroduced.stderr)

    def test_1f_the_shipped_manifest_names_no_destination(self) -> None:
        """The real manifest, not a fixture: the field must be gone from the repository."""
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("default_target", manifest)

    def test_1g_synced_tests_that_run_the_wrapper_bring_their_own_ps_double(self) -> None:
        """Consumers run synced tests with unittest, which never loads conftest.py. A synced test
        that runs run_agent.sh without importing fake_ps sees the consumer's enclosing harness."""
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        synced = {entry["destination"] for entry in manifest["entries"]}
        self.assertIn("automation/tests/fake_ps.py", synced)
        for destination in sorted(synced):
            if destination.startswith("automation/tests/test_") and destination.endswith(".py"):
                text = (REPO_ROOT / destination).read_text(encoding="utf-8")
                if "run_agent.sh" in text:
                    with self.subTest(destination=destination):
                        self.assertIn("import automation.tests.fake_ps", text)

    # --- protection 2 ----------------------------------------------------

    def test_2_unpinned_import_is_an_error(self) -> None:
        self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--pin", result.stderr)
        self.assertFalse((self.target / "automation/seed.txt").exists())

    def test_2b_explicit_development_mode_is_the_only_way_past_the_pin(self) -> None:
        self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", "--allow-unverified-source")

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("seed\n", (self.target / "automation/seed.txt").read_text(encoding="utf-8"))
        self.assertIn("unverified source", result.stdout + result.stderr)

    # --- protection 3 ----------------------------------------------------

    def test_3_source_repository_identity_must_match_the_lock(self) -> None:
        pin = self.canonical_source_with()
        self.git(self.source, "remote", "set-url", "origin", "https://github.com/attacker/boundroom.git")
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("identity", result.stderr)
        self.assertFalse((self.target / "automation/seed.txt").exists())

    def test_3b_a_source_with_no_origin_fails_identity_rather_than_skipping_it(self) -> None:
        """An absent remote must be loud. Silently passing is the case you want caught."""
        pin = self.canonical_source_with()
        self.git(self.source, "remote", "remove", "origin")
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("identity", result.stderr)
        self.assertFalse((self.target / "automation/seed.txt").exists())

    # --- protection 4 ----------------------------------------------------

    def test_4_dirty_canonical_source_is_refused(self) -> None:
        pin = self.canonical_source_with()
        self.write_source("seed.txt", "uncommitted local edit\n")
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("uncommitted", result.stderr)
        self.assertFalse((self.target / "automation/seed.txt").exists())

    def test_4b_source_on_a_different_commit_than_the_pin_is_refused(self) -> None:
        pin = self.canonical_source_with()
        self.write_source("seed.txt", "moved on\n")
        self.publish_source("second")
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("pinned commit", result.stderr)
        self.assertFalse((self.target / "automation/seed.txt").exists())

    def test_4c_check_mode_verifies_the_pin_too(self) -> None:
        """A drift report against the wrong commit answers the wrong question.

        This is the path the consumer's pre-push hook runs, so it has to notice that the
        source has moved rather than reporting the snapshot as current.
        """
        pin = self.canonical_source_with()
        self.write_manifest([self.entry("seed.txt", "automation/seed.txt")])
        self.assertEqual(0, self.run_tool("--sync", pin=pin).returncode)

        self.write_source("seed.txt", "moved on\n")
        moved = self.publish_source("second")

        result = self.run_tool("--check", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("pinned commit", result.stderr)
        self.assertIn(moved[:12], result.stderr)

    # --- protection 5 ----------------------------------------------------

    def test_5_import_cannot_write_to_a_consumer_owned_destination(self) -> None:
        """The canonical side supplies the file list, so it must not be able to aim it.

        An upstream entry whose destination is .githooks/pre-push or Makefile would be
        executed by the consumer on the very next push or make invocation.
        """
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()
        self.write_target(".githooks/pre-push", "#!/bin/sh\nexit 0\n")
        self.write_target("Makefile", "real:\n\t@echo real\n")
        self.write_target("automation/queue/slices.json", '{"slices": []}\n')
        self.commit_consumer()

        for destination in (".githooks/pre-push", "Makefile", "automation/queue/slices.json"):
            with self.subTest(destination=destination):
                self.write_manifest([self.entry("payload.txt", destination)])

                result = self.run_tool("--sync", pin=pin)

                self.assertNotEqual(0, result.returncode)
                self.assertIn("destination", result.stderr)
                self.assertNotIn(
                    "payload",
                    (self.target / destination).read_text(encoding="utf-8"),
                    msg=f"{destination} is consumer-owned and must never be written by an import",
                )

    def test_5b_the_destination_guard_is_case_insensitive(self) -> None:
        """On APFS and NTFS 'makefile' is the file the guard is protecting."""
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()

        for destination in ("makefile", "MAKEFILE", ".GitHooks/pre-push", "Automation/Queue/slices.json"):
            with self.subTest(destination=destination):
                self.write_manifest([self.entry("payload.txt", destination)])

                result = self.run_tool("--sync", pin=pin)

                self.assertNotEqual(0, result.returncode)
                self.assertIn("destination", result.stderr)

    def test_5c_the_lock_file_is_consumer_owned(self) -> None:
        """The consumer's Makefile reads the lock and passes it to this tool.

        Whoever writes the lock chooses what `make` executes in the consumer, so an import
        must never be able to write it - the manifest comes from the same place an attacker
        would be.
        """
        self.assert_lock_is_refused("automation/boundroom.lock")

    def test_5d_the_lock_name_from_before_the_rename_is_still_consumer_owned(self) -> None:
        """SPEC-005: a consumer synced before 0.3.0 keeps its lock under the old name."""
        self.assert_lock_is_refused("automation/repo-automation.lock")

    def assert_lock_is_refused(self, lock: str) -> None:
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()
        self.write_target(lock, '{"commit": "real"}\n')
        self.commit_consumer()
        self.write_manifest([self.entry("payload.txt", lock)])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("destination", result.stderr)
        self.assertIn("real", (self.target / lock).read_text(encoding="utf-8"))

    def test_5e_the_runner_rules_are_consumer_owned(self) -> None:
        """SPEC-009 RA-ENT-001: a repository's runner rules are its own, never an import's."""
        self.assert_lock_is_refused("automation/runner-rules.md")

    def test_5f_an_import_writes_only_where_boundroom_lives(self) -> None:
        """SPEC-009 RA-ENT-002: an allowlist protects every consumer's own code, naming none."""
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()
        outside = (
            "app/Core/Domain/Rule.swift",
            "localization/en.json",
            "Tools/bump-version.sh",
            "README.md",
            "docs/product/plan.md",
        )
        for destination in outside:
            self.write_target(destination, "real\n")
        self.commit_consumer()

        for destination in outside + ("automation", "docs/workflows", "tools/boundroom-sync.sh.bak"):
            with self.subTest(destination=destination):
                self.write_manifest([self.entry("payload.txt", destination)])

                result = self.run_tool("--sync", pin=pin)

                self.assertNotEqual(0, result.returncode)
                self.assertIn("outside the places an import may write", result.stderr)
        for destination in outside:
            self.assertEqual("real\n", (self.target / destination).read_text(encoding="utf-8"))

    def test_5g_the_allowed_places_still_take_an_import(self) -> None:
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        pin = self.publish_source()
        self.init_consumer()
        allowed = ("automation/payload.txt", "docs/workflows/payload.md", "THIRD-PARTY-NOTICES.md", "Tools/boundroom-sync.sh")
        self.write_manifest([self.entry("payload.txt", destination) for destination in allowed])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, result.stderr)
        for destination in allowed:
            self.assertEqual("payload\n", (self.target / destination).read_text(encoding="utf-8"))

    def test_5h_the_retired_opt_out_key_warns_and_bypasses_nothing(self) -> None:
        """allow_owlory_specific used to let an entry ship a forbidden source. It no longer does."""
        self.init_canonical_source()
        self.write_source("payload.txt", "payload\n")
        self.write_source("Makefile", "all:\n")
        pin = self.publish_source()
        self.init_consumer()

        forbidden = self.entry("Makefile", "automation/Makefile")
        forbidden["allow_owlory_specific"] = True
        self.write_manifest([forbidden])
        refused = self.run_tool("--sync", pin=pin)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("forbidden source is never shipped: Makefile", refused.stderr)
        self.assertIn("allow_owlory_specific, which is deprecated and ignored", refused.stderr)
        self.assertIn("refused from v0.6.0", refused.stderr)

        ordinary = self.entry("payload.txt", "automation/payload.txt")
        ordinary["allow_owlory_specific"] = False
        self.write_manifest([ordinary])
        accepted = self.run_tool("--sync", pin=pin)
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertIn("allow_owlory_specific, which is deprecated and ignored", accepted.stderr)

    # --- protection 6 ----------------------------------------------------

    def test_6_stale_deletion_never_reaches_outside_canonical_owned_paths(self) -> None:
        """delete_stale is how the 1937-line deletion happened. Bound it to what it owns."""
        pin = self.canonical_source_with(("harness/keep.py", "keep\n"))
        self.init_consumer()
        self.write_target("automation/handoffs/closed.json", "{}\n")
        self.write_target("automation/proofs/slice/proof.md", "proof\n")
        self.write_target("automation/queue/slices.json", '{"slices": []}\n')
        self.write_target("automation/vendored/harness/gone.py", "upstream deleted this\n")
        self.commit_consumer()
        self.write_manifest(
            [
                self.entry("harness", "automation/vendored/harness", kind="directory", delete_stale=True),
                self.entry("harness", "automation", kind="directory", delete_stale=True),
            ]
        )

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("destination", result.stderr)
        for survivor in (
            "automation/handoffs/closed.json",
            "automation/proofs/slice/proof.md",
            "automation/queue/slices.json",
            "automation/vendored/harness/gone.py",
        ):
            self.assertTrue(
                (self.target / survivor).exists(),
                msg=f"a refused import must delete nothing at all, including {survivor}",
            )

    def test_6b_a_clean_import_deletes_stale_files_only_under_its_own_destination(self) -> None:
        pin = self.canonical_source_with(("harness/keep.py", "keep\n"))
        self.init_consumer()
        self.write_target("automation/vendored/harness/gone.py", "upstream deleted this\n")
        self.write_target("automation/handoffs/closed.json", "{}\n")
        self.commit_consumer()
        self.write_manifest([self.entry("harness", "automation/vendored/harness", kind="directory", delete_stale=True)])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertFalse((self.target / "automation/vendored/harness/gone.py").exists())
        self.assertTrue((self.target / "automation/vendored/harness/keep.py").exists())
        self.assertTrue((self.target / "automation/handoffs/closed.json").exists())

    # --- protection 7 ----------------------------------------------------

    def test_7_import_refuses_to_clobber_a_locally_modified_file(self) -> None:
        """A customized vendored file is uncommitted work. Overwriting it loses it silently."""
        pin = self.canonical_source_with(("harness/tool.py", "canonical\n"))
        self.init_consumer()
        self.write_target("automation/vendored/harness/tool.py", "canonical\n")
        self.commit_consumer()
        self.write_target("automation/vendored/harness/tool.py", "local customization in progress\n")
        self.write_manifest([self.entry("harness", "automation/vendored/harness", kind="directory", delete_stale=True)])

        result = self.run_tool("--sync", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("locally modified", result.stderr)
        self.assertEqual(
            "local customization in progress\n",
            (self.target / "automation/vendored/harness/tool.py").read_text(encoding="utf-8"),
        )

    def test_7b_template_entries_are_not_overwritten_by_an_ordinary_import(self) -> None:
        pin = self.canonical_source_with(("prompts/slice.md", "canonical prompt\n"))
        self.init_consumer()
        self.write_target("automation/prompts/slice.md", "consumer prompt\n")
        self.commit_consumer()
        self.write_manifest([self.entry("prompts", "automation/prompts", kind="directory", template=True)])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(
            "consumer prompt\n",
            (self.target / "automation/prompts/slice.md").read_text(encoding="utf-8"),
        )

    def test_7c_a_git_failure_stops_the_import_rather_than_disabling_the_guard(self) -> None:
        """Protection 7 must fail closed.

        Returning "nothing is modified" whenever git exits non-zero means dubious ownership
        or a missing git silently removes the guard, and the import proceeds to overwrite
        the very work the guard exists to protect.
        """
        self.canonical_source_with(("harness/tool.py", "canonical v2\n"))
        self.init_consumer()
        self.write_target("automation/vendored/harness/tool.py", "local work\n")
        self.write_manifest([self.entry("harness", "automation/vendored/harness", kind="directory")])

        # git is present but refuses, which is what dubious ownership looks like. Exit 128 is
        # also what "not a git repository" returns, so returncode alone cannot tell them apart.
        shim = self.tmpdir / "shim"
        shim.mkdir()
        fake_git = shim / "git"
        fake_git.write_text(
            '#!/bin/sh\necho "fatal: detected dubious ownership in repository" >&2\nexit 128\n',
            encoding="utf-8",
        )
        fake_git.chmod(0o755)

        result = self.run_tool("--sync", "--allow-unverified-source", env_path=f"{shim}:{os.environ['PATH']}")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("uncommitted", result.stderr)
        self.assertEqual("local work\n", (self.target / "automation/vendored/harness/tool.py").read_text(encoding="utf-8"))

    def test_7d_an_untracked_consumer_is_still_importable(self) -> None:
        """Fail-closed must not mean refusing a consumer that simply is not a Git checkout."""
        pin = self.canonical_source_with(("harness/tool.py", "canonical\n"))
        self.write_manifest([self.entry("harness", "automation/vendored/harness", kind="directory")])

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("canonical\n", (self.target / "automation/vendored/harness/tool.py").read_text(encoding="utf-8"))

    def test_7e_modified_paths_are_resolved_against_the_repository_root(self) -> None:
        """Status paths are relative to the toplevel, so a consumer in a subdirectory needs it."""
        pin = self.canonical_source_with(("harness/tool.py", "canonical v2\n"))
        self.init_consumer()

        nested = self.target / "nested/consumer"
        nested.mkdir(parents=True)
        (nested / "automation/vendored").mkdir(parents=True)
        (nested / "automation/vendored/tool.py").write_text("canonical\n", encoding="utf-8")
        self.commit_consumer()
        (nested / "automation/vendored/tool.py").write_text("local work\n", encoding="utf-8")
        self.write_manifest([self.entry("harness", "automation/vendored", kind="directory")])

        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--pin",
                pin,
                "--expect-remote",
                self.remote,
                "--quality-command",
                "true",
                "--source",
                str(self.source),
                "--manifest",
                str(self.manifest),
                "--target",
                str(nested),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("locally modified", result.stderr)
        self.assertEqual("local work\n", (nested / "automation/vendored/tool.py").read_text(encoding="utf-8"))

    # --- protection 8 ----------------------------------------------------

    def test_8_source_failing_its_own_quality_gate_is_refused(self) -> None:
        """Pinned, clean and correctly identified is not the same as releasable."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "automation/vendored/tool.py")])

        result = self.run_tool("--sync", pin=pin, quality_command="false")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("quality gate", result.stderr)
        self.assertFalse((self.target / "automation/vendored/tool.py").exists())

    def test_8_quality_gate_failure_output_is_reported(self) -> None:
        """A refusal that does not say what failed sends the operator back to guessing."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "automation/vendored/tool.py")])

        result = self.run_tool("--sync", pin=pin, quality_command="sh -c 'echo would-reformat-something >&2; exit 1'")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("would-reformat-something", result.stderr)

    def test_8_unrunnable_quality_gate_is_a_refusal_not_a_pass(self) -> None:
        """A gate that cannot run has not passed; the tool must not read that as success."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "automation/vendored/tool.py")])

        result = self.run_tool("--sync", pin=pin, quality_command="boundroom-no-such-gate")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("could not run the canonical quality gate", result.stderr)
        self.assertFalse((self.target / "automation/vendored/tool.py").exists())

    def test_8_quality_gate_runs_in_the_source_not_the_consumer(self) -> None:
        """The gate describes the canonical snapshot, so it must execute there."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "automation/vendored/tool.py")])
        marker = self.tmpdir / "gate-cwd.txt"

        result = self.run_tool("--sync", pin=pin, quality_command=f"sh -c 'pwd > {marker}'")

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(self.source.resolve(), Path(marker.read_text(encoding="utf-8").strip()).resolve())
        self.assertEqual("canonical\n", (self.target / "automation/vendored/tool.py").read_text(encoding="utf-8"))

    def test_8_check_with_a_pin_also_requires_the_quality_gate(self) -> None:
        """--check with a pin answers "is this snapshot importable", which includes quality."""
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        self.write_manifest([self.entry("tool.py", "automation/vendored/tool.py")])

        result = self.run_tool("--check", pin=pin, quality_command="false")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("quality gate", result.stderr)

    def test_8_default_quality_gate_is_the_canonical_make_target(self) -> None:
        """The tool names one contract; the canonical repo owns which tools it runs."""
        self.assertIn("reusable-check:", (REPO_ROOT / "Makefile").read_text(encoding="utf-8"))
        help_text = subprocess.run([str(SYNC_TOOL), "--help"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        self.assertIn("make reusable-check", help_text)

    # --- protection 9 ----------------------------------------------------

    @staticmethod
    def runtime_block(distribution: str, minimum_major: int, maximum_major_exclusive: int) -> dict[str, Any]:
        return {
            "python": {
                "dependencies": [
                    {
                        "distribution": distribution,
                        "import_name": distribution,
                        "minimum_major": minimum_major,
                        "maximum_major_exclusive": maximum_major_exclusive,
                    }
                ]
            }
        }

    def stage_runtime_case(self, runtime: dict[str, Any] | None) -> str:
        pin = self.canonical_source_with(("tool.py", "canonical\n"))
        extra = {"runtime": runtime} if runtime is not None else {}
        self.write_manifest([self.entry("tool.py", "automation/vendored/tool.py")], **extra)
        return pin

    def test_9_satisfied_runtime_dependency_lets_the_import_proceed(self) -> None:
        pin = self.stage_runtime_case(self.runtime_block("pydantic", 2, 3))

        result = self.run_tool("--sync", pin=pin, runtime_python=sys.executable)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("canonical\n", (self.target / "automation/vendored/tool.py").read_text(encoding="utf-8"))

    def test_9_absent_runtime_dependency_is_refused_before_any_write(self) -> None:
        pin = self.stage_runtime_case(self.runtime_block("boundroom-not-a-real-distribution", 1, 2))

        result = self.run_tool("--sync", pin=pin, runtime_python=sys.executable)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing_runtime_dependency: boundroom-not-a-real-distribution>=1,<2", result.stderr)
        self.assertFalse((self.target / "automation/vendored/tool.py").exists())

    def test_9_runtime_dependency_below_the_supported_major_is_refused(self) -> None:
        pin = self.stage_runtime_case(self.runtime_block("pydantic", 3, 4))

        result = self.run_tool("--sync", pin=pin, runtime_python=sys.executable)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing_runtime_dependency: pydantic>=3,<4 (found ", result.stderr)
        self.assertFalse((self.target / "automation/vendored/tool.py").exists())

    def test_9_runtime_dependency_past_the_supported_major_is_refused(self) -> None:
        """A future major is as unsupported as an old one; the range is closed on both ends."""
        pin = self.stage_runtime_case(self.runtime_block("pydantic", 1, 2))

        result = self.run_tool("--sync", pin=pin, runtime_python=sys.executable)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing_runtime_dependency: pydantic>=1,<2 (found ", result.stderr)

    def test_9_unusable_consumer_interpreter_is_a_refusal(self) -> None:
        pin = self.stage_runtime_case(self.runtime_block("pydantic", 2, 3))

        result = self.run_tool("--sync", pin=pin, runtime_python=str(self.tmpdir / "no-such-python"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("consumer interpreter", result.stderr)
        self.assertFalse((self.target / "automation/vendored/tool.py").exists())

    def test_9_manifest_without_runtime_metadata_is_unchanged(self) -> None:
        """Older manifests declare nothing, so nothing is enforced."""
        pin = self.stage_runtime_case(None)

        result = self.run_tool("--sync", pin=pin, runtime_python=str(self.tmpdir / "no-such-python"))

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("canonical\n", (self.target / "automation/vendored/tool.py").read_text(encoding="utf-8"))

    def test_9_pinned_check_verifies_the_consumer_runtime_too(self) -> None:
        pin = self.stage_runtime_case(self.runtime_block("boundroom-not-a-real-distribution", 1, 2))

        result = self.run_tool("--check", pin=pin, runtime_python=sys.executable)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing_runtime_dependency", result.stderr)

    def test_9_allow_unverified_source_does_not_waive_the_runtime_check(self) -> None:
        """That flag says the source is trusted, not that the consumer can run the code."""
        self.stage_runtime_case(self.runtime_block("boundroom-not-a-real-distribution", 1, 2))

        result = self.run_tool("--sync", "--allow-unverified-source", runtime_python=sys.executable)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing_runtime_dependency", result.stderr)
        self.assertFalse((self.target / "automation/vendored/tool.py").exists())

    def test_9_canonical_manifest_declares_its_pydantic_requirement(self) -> None:
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        declared = manifest["runtime"]["python"]["dependencies"]
        self.assertEqual(
            [{"distribution": "pydantic", "import_name": "pydantic", "minimum_major": 2, "maximum_major_exclusive": 3}],
            declared,
        )

    def test_10_a_consumer_export_carries_the_license(self) -> None:
        """GPL-2.0 has to travel with every distributed copy, beside the code rather than at the consumer's root."""
        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--allow-unverified-source",
                "--source",
                str(REPO_ROOT),
                "--manifest",
                str(REPO_ROOT / "automation/reusable-manifest.json"),
                "--target",
                str(self.target),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual((REPO_ROOT / "COPYING").read_bytes(), (self.target / "automation/COPYING").read_bytes())
        self.assertFalse((self.target / "COPYING").exists())

    # --- optional components (SPEC-009 RA-ENT-013) ------------------------

    EXTRAS = {"extras": "An optional part, for these tests."}

    def extras_case(self) -> str:
        """A core file, and an optional component of a delete_stale directory and a file."""
        pin = self.canonical_source_with(("core/tool.py", "core\n"), ("extras/one.py", "one\n"), ("extras-doc.md", "doc\n"))
        self.write_manifest(
            [
                self.entry("core", "automation/core", kind="directory", delete_stale=True),
                self.entry("extras", "automation/extras", kind="directory", delete_stale=True, component="extras"),
                self.entry("extras-doc.md", "docs/workflows/extras.md", component="extras"),
            ],
            optional_components=self.EXTRAS,
        )
        return pin

    def test_11_an_omitted_component_is_not_written(self) -> None:
        pin = self.extras_case()

        result = self.run_tool("--sync", "--omit", "extras", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertTrue((self.target / "automation/core/tool.py").exists())
        self.assertFalse((self.target / "automation/extras").exists())
        self.assertFalse((self.target / "docs/workflows/extras.md").exists())
        self.assertEqual(0, self.run_tool("--check", "--omit", "extras").returncode)
        # Without the flag, the same consumer is missing the component.
        self.assertEqual(1, self.run_tool("--check").returncode)

    def test_11b_omitting_a_component_removes_what_an_earlier_sync_left(self) -> None:
        pin = self.extras_case()
        self.init_consumer()
        self.assertEqual(0, self.run_tool("--sync", pin=pin).returncode)
        # A file no release ships, in a directory only Boundroom writes, goes too.
        self.write_target("automation/extras/leftover.py", "from an older release\n")
        self.write_target("automation/handoffs/closed.json", "{}\n")
        self.commit_consumer()

        check = self.run_tool("--check", "--omit", "extras")
        self.assertEqual(1, check.returncode)
        for path in ("automation/extras/one.py", "automation/extras/leftover.py", "docs/workflows/extras.md"):
            self.assertIn(f"omitted: {path}", check.stdout)
        self.assertTrue((self.target / "automation/extras/one.py").exists(), "a check changes nothing")

        result = self.run_tool("--sync", "--omit", "extras", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("removed omitted: automation/extras/one.py", result.stdout)
        self.assertFalse((self.target / "automation/extras").exists())
        self.assertFalse((self.target / "docs/workflows/extras.md").exists())
        self.assertTrue((self.target / "docs/workflows").is_dir())
        self.assertTrue((self.target / "automation/core/tool.py").exists())
        self.assertTrue((self.target / "automation/handoffs/closed.json").exists())
        self.assertEqual(0, self.run_tool("--check", "--omit", "extras").returncode)

    def test_11c_an_omitted_template_is_left_to_the_consumer(self) -> None:
        pin = self.canonical_source_with(("prompts/base.md", "upstream\n"))
        self.write_manifest(
            [self.entry("prompts", "automation/prompts", kind="directory", template=True, component="extras")],
            optional_components=self.EXTRAS,
        )
        self.write_target("automation/prompts/base.md", "the consumer's own\n")

        result = self.run_tool("--sync", "--omit", "extras", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("the consumer's own\n", (self.target / "automation/prompts/base.md").read_text(encoding="utf-8"))

    def test_11d_an_unknown_component_is_refused_before_any_write(self) -> None:
        pin = self.extras_case()

        unknown = self.run_tool("--sync", "--omit", "benchmark", pin=pin)

        self.assertEqual(2, unknown.returncode)
        self.assertIn(
            "--omit benchmark: the manifest declares no such optional component (optional components: extras)", unknown.stderr
        )
        self.assertEqual([], list(self.target.iterdir()))

        self.write_manifest([self.entry("core", "automation/core", kind="directory", component="extras")])
        undeclared = self.run_tool("--sync", pin=pin)
        self.assertEqual(2, undeclared.returncode)
        self.assertIn("names component 'extras', which optional_components doesn't declare", undeclared.stderr)

        malformed_entry = {**self.entry("core", "automation/core", kind="directory"), "component": ["extras"]}
        self.write_manifest([malformed_entry], optional_components=self.EXTRAS)
        malformed = self.run_tool("--sync", pin=pin)
        self.assertEqual(2, malformed.returncode, msg=malformed.stderr)
        self.assertIn("has a component that isn't a string", malformed.stderr)
        self.assertNotIn("Traceback", malformed.stderr)

    def test_11e_without_omit_every_component_syncs(self) -> None:
        pin = self.extras_case()

        result = self.run_tool("--sync", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertTrue((self.target / "automation/extras/one.py").exists())
        self.assertTrue((self.target / "docs/workflows/extras.md").exists())

    def test_11f_removing_an_omitted_file_respects_local_changes(self) -> None:
        pin = self.extras_case()
        self.init_consumer()
        self.assertEqual(0, self.run_tool("--sync", pin=pin).returncode)
        self.commit_consumer()
        self.write_target("automation/extras/one.py", "local work in progress\n")

        result = self.run_tool("--sync", "--omit", "extras", pin=pin)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("locally modified files: automation/extras/one.py", result.stderr)
        self.assertTrue((self.target / "docs/workflows/extras.md").exists(), "a refusal changes nothing")

    def test_11f2_a_core_file_inside_an_omitted_directory_is_kept(self) -> None:
        """Writes run before removals, so an omission must never claim what a kept entry writes."""
        pin = self.canonical_source_with(("extras/one.py", "one\n"), ("shared.py", "core\n"))
        self.write_manifest(
            [
                self.entry("extras", "automation/extras", kind="directory", delete_stale=True, component="extras"),
                self.entry("shared.py", "automation/extras/shared.py"),
            ],
            optional_components=self.EXTRAS,
        )
        self.init_consumer()
        self.assertEqual(0, self.run_tool("--sync", pin=pin).returncode)
        self.commit_consumer()

        result = self.run_tool("--sync", "--omit", "extras", pin=pin)

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertFalse((self.target / "automation/extras/one.py").exists())
        self.assertEqual("core\n", (self.target / "automation/extras/shared.py").read_text(encoding="utf-8"))
        self.assertEqual(0, self.run_tool("--check", "--omit", "extras").returncode)

    def test_11g_a_consumer_synced_without_the_benchmark_passes_its_checks(self) -> None:
        """RA-ENT-013's acceptance: the real manifest, with the benchmark left out, then the
        consumer's synced tests, run the way a consumer runs them, with unittest."""
        self.init_consumer()
        result = subprocess.run(
            [
                str(SYNC_TOOL),
                "--sync",
                "--allow-unverified-source",
                "--omit",
                "benchmark",
                "--runtime-python",
                sys.executable,
                "--source",
                str(REPO_ROOT),
                "--target",
                str(self.target),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["entries"]:
            with self.subTest(destination=entry["destination"]):
                self.assertEqual(entry.get("component") != "benchmark", (self.target / entry["destination"]).exists())
        self.commit_consumer()  # build_context reads the consumer's HEAD

        synced_tests = sorted(
            entry["destination"][:-3].replace("/", ".")
            for entry in manifest["entries"]
            if entry.get("component") is None and entry["destination"].startswith("automation/tests/test_")
        )
        self.assertIn("automation.tests.test_harness", synced_tests)
        tests = subprocess.run(
            [sys.executable, "-m", "unittest", *synced_tests],
            cwd=self.target,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(0, tests.returncode, msg=tests.stderr[-4000:])

    def test_11h_the_core_never_reaches_into_the_benchmark(self) -> None:
        """An omitted component is only safe to leave out if nothing the consumer keeps uses it."""
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        self.assertIn("benchmark", manifest["optional_components"])
        component = [entry for entry in manifest["entries"] if entry.get("component") == "benchmark"]
        for entry in manifest["entries"]:
            # Whatever is named for the benchmark belongs to it.
            if entry["source"].startswith(("automation/benchmark", "automation/tests/test_benchmark_")):
                with self.subTest(source=entry["source"]):
                    self.assertIn(entry, component)
        # Core code may name neither the benchmark's package nor any file the component ships.
        names = [re.escape(Path(entry["destination"]).name) for entry in component if entry["kind"] == "file"]
        reference = re.compile(r"\bautomation[./]benchmark\b|" + "|".join(names))
        for entry, path, _ in manifest_files(manifest):
            if entry.get("component") is None and path.suffix in {".py", ".sh"}:
                with self.subTest(core=str(path.relative_to(REPO_ROOT))):
                    self.assertIsNone(reference.search(path.read_text(encoding="utf-8")))


class PinStatusTests(unittest.TestCase):
    """RA-PROD-103: `--status` shows the consumer's pin, drift and Git state, and writes nothing."""

    REMOTE = "https://github.com/raelldottin/boundroom.git"

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="boundroom-status-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.source = self.tmpdir / "canonical"
        self.target = self.tmpdir / "consumer"
        self.source.mkdir()
        self.target.mkdir()
        self.manifest = self.tmpdir / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "source": "harness",
                            "destination": "automation/vendored",
                            "kind": "directory",
                            "preserve_executable": False,
                            "delete_stale": True,
                            "template": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "remote", "add", "origin", self.REMOTE)
        self.v1 = self.commit_source("one\n", "v0.1.0")
        self.v2 = self.commit_source("two\n", "v0.2.0")
        self.git(self.source, "checkout", "--quiet", "--detach", self.v1)
        self.git(self.target, "init", "-b", "main")

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, msg=f"git {' '.join(args)}: {result.stderr}")
        return result

    def commit_source(self, contents: str, tag: str) -> str:
        (self.source / "harness").mkdir(exist_ok=True)
        (self.source / "harness/tool.py").write_text(contents, encoding="utf-8")
        self.git(self.source, "add", "-A")
        self.git(self.source, "commit", "-m", tag)
        self.git(self.source, "tag", tag)
        return self.git(self.source, "rev-parse", "HEAD").stdout.strip()

    def vendor(self, contents: str = "one\n") -> None:
        (self.target / "automation/vendored").mkdir(parents=True, exist_ok=True)
        (self.target / "automation/vendored/tool.py").write_text(contents, encoding="utf-8")

    def write_lock(self, commit: str, name: str = "automation/boundroom.lock") -> None:
        path = self.target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "commit": commit,
                    "remote": self.REMOTE,
                    "repository": "raelldottin/boundroom",
                    "imported_at": "2026-11-12T00:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def commit_consumer(self, message: str = "consumer") -> None:
        self.git(self.target, "add", "-A")
        self.git(self.target, "commit", "-m", message)

    def status(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(SYNC_TOOL),
                "--status",
                *extra,
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
        )

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        for root in (self.source, self.target):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    found[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
        return found

    def test_no_lock_is_reported(self) -> None:
        self.vendor()
        self.commit_consumer()

        result = self.status()

        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("lock: none", result.stdout)
        self.assertIn("result: attention needed: no lock", result.stdout)

    def test_a_current_boundroom_lock_is_current(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer()

        result = self.status()

        self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)
        self.assertIn("lock: automation/boundroom.lock\n", result.stdout)
        self.assertIn(f"lock commit: {self.v1}", result.stdout)
        self.assertIn(f"lock remote: {self.REMOTE}", result.stdout)
        self.assertIn("lock imported_at: 2026-11-12T00:00:00Z", result.stdout)
        self.assertIn(f"pin: {self.v1} (tag v0.1.0)", result.stdout)
        self.assertIn("(the pinned commit)", result.stdout)
        self.assertIn("drift: none", result.stdout)
        self.assertIn("consumer worktree: clean", result.stdout)
        self.assertIn(f"Tools/repo-automation-sync.sh --check --target {self.target.resolve()} --pin {self.v1}", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("result: current"))

    def test_a_legacy_lock_is_read_in_key_value_form(self) -> None:
        self.vendor()
        lock = self.target / "automation/repo-automation.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"commit={self.v1}\nremote={self.REMOTE}\nimported_at=2026-10-01\n", encoding="utf-8")
        self.commit_consumer()

        result = self.status()

        self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)
        self.assertIn("lock: automation/repo-automation.lock", result.stdout)
        self.assertIn(f"lock commit: {self.v1}", result.stdout)
        self.assertIn("lock repository: (missing)", result.stdout)

    def test_desired_equal_to_the_pin(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer()

        result = self.status("--desired", "v0.1.0")

        self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)
        self.assertIn(f"desired: v0.1.0 -> {self.v1}", result.stdout)
        self.assertIn("pin vs desired: same", result.stdout)
        self.assertNotIn("update to", result.stdout)

    def test_desired_different_from_the_pin_prints_update_and_rollback(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer()

        result = self.status("--desired", "v0.2.0")

        self.assertEqual(1, result.returncode)
        self.assertIn(f"desired: v0.2.0 -> {self.v2} (tag v0.2.0)", result.stdout)
        self.assertIn("pin vs desired: differs", result.stdout)
        self.assertIn(f"update to v0.2.0 ({self.v2}):", result.stdout)
        self.assertIn(f"git checkout --detach {self.v2}", result.stdout)
        self.assertIn(f"--sync --target {self.target.resolve()} --pin {self.v2} --expect-remote {self.REMOTE}", result.stdout)
        self.assertIn(f"sync back to the current pin {self.v1}", result.stdout)
        self.assertIn("desired differs from the pin", result.stdout)

    def test_omitted_components_are_reported_and_carried_into_the_commands(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["optional_components"] = {"extras": "An optional part, for this test."}
        manifest["entries"][0]["component"] = "extras"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        # v3's sync script knows --omit; v1's, from before optional components, doesn't.
        (self.source / "Tools").mkdir()
        (self.source / "Tools/boundroom-sync.sh").write_text('parser.add_argument("--omit")\n', encoding="utf-8")
        v3 = self.commit_source("three\n", "v0.3.0")
        self.git(self.source, "checkout", "--quiet", "--detach", self.v1)
        self.write_lock(self.v1)
        self.commit_consumer()

        result = self.status("--omit", "extras", "--desired", "v0.3.0")

        self.assertIn("omitted components: extras", result.stdout)
        self.assertIn("drift: none", result.stdout, "the omitted directory isn't drift")
        self.assertIn(f"--pin {v3} --expect-remote {self.REMOTE} --omit extras", result.stdout)
        self.assertIn(f"--pin {self.v1} --expect-remote {self.REMOTE}\n", result.stdout)

    def test_previous_pin_comes_from_the_lock_history(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer("pin v1")
        self.vendor("two\n")
        self.write_lock(self.v2)
        self.commit_consumer("pin v2")
        self.git(self.source, "checkout", "--quiet", "--detach", self.v2)

        result = self.status()

        self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)
        self.assertIn(f"roll back to the previous pin {self.v1} (tag v0.1.0)", result.stdout)
        self.assertIn(f"--sync --target {self.target.resolve()} --pin {self.v1}", result.stdout)

    def test_unknown_desired_ref_is_reported(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer()

        result = self.status("--desired", "v9.9.9")

        self.assertEqual(1, result.returncode)
        self.assertIn("desired: v9.9.9 (not found in the source checkout", result.stdout)

    def test_drifted_vendored_files_are_reported(self) -> None:
        self.vendor("edited locally\n")
        (self.target / "automation/vendored/extra.py").write_text("stale\n", encoding="utf-8")
        self.write_lock(self.v1)
        self.commit_consumer()

        result = self.status()

        self.assertEqual(1, result.returncode)
        self.assertIn("  changed: automation/vendored/tool.py", result.stdout)
        self.assertIn("  stale: automation/vendored/extra.py", result.stdout)
        self.assertIn("drift: found (2 issue(s))", result.stdout)

    def test_source_at_another_commit_is_not_called_the_pin(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer()
        self.git(self.source, "checkout", "--quiet", "--detach", self.v2)

        result = self.status()

        self.assertEqual(1, result.returncode)
        self.assertIn("not the pinned commit", result.stdout)
        self.assertIn("source is not at the pinned commit", result.stdout)

    def test_a_dirty_consumer_worktree_is_reported(self) -> None:
        self.vendor()
        self.write_lock(self.v1)
        self.commit_consumer()
        (self.target / "notes.txt").write_text("uncommitted\n", encoding="utf-8")

        result = self.status()

        self.assertEqual(1, result.returncode)
        self.assertIn("consumer worktree: dirty (1 path(s))", result.stdout)
        self.assertIn("?? notes.txt", result.stdout)

    def test_status_writes_nothing(self) -> None:
        self.vendor("edited locally\n")
        self.write_lock(self.v1)
        self.commit_consumer("pin v1")
        self.write_lock(self.v2)
        self.commit_consumer("pin v2")
        (self.target / "notes.txt").write_text("uncommitted\n", encoding="utf-8")
        before = self.snapshot()

        result = self.status("--desired", "v0.2.0")

        self.assertEqual(1, result.returncode)
        self.assertIn("commands (printed only; --status runs none of them):", result.stdout)
        self.assertEqual(before, self.snapshot(), "--status must not write the lock, the index or any other file")

    def test_status_refuses_pin_and_desired_needs_status(self) -> None:
        refused = self.status("--pin", self.v1)
        self.assertEqual(2, refused.returncode)
        self.assertIn("reads the pin from the consumer's lock", refused.stderr)

        result = subprocess.run(
            [str(SYNC_TOOL), "--check", "--desired", "v0.1.0", "--target", str(self.target)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--desired is used only with --status", result.stderr)


class PreRenameNamesTests(unittest.TestCase):
    """SPEC-005: what a consumer synced before 0.3.0 still calls keeps working."""

    def test_the_old_script_path_runs_the_renamed_script(self) -> None:
        old = REPO_ROOT / "Tools" / "repo-automation-sync.sh"
        self.assertTrue(os.access(old, os.X_OK))
        result = subprocess.run([str(old), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--target", result.stdout)
        self.assertIn("renamed to boundroom-sync.sh", result.stderr)

    def test_the_manifest_still_delivers_the_old_names(self) -> None:
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
        destinations = {entry["destination"] for entry in manifest["entries"]}
        for path in (
            "Tools/boundroom-sync.sh",
            "Tools/repo-automation-sync.sh",
            "docs/workflows/boundroom.md",
            "docs/workflows/repo-automation.md",
        ):
            self.assertIn(path, destinations)


# What Tools/boundroom-sync.sh never copies out of a directory entry (its SKIP_NAMES).
SYNC_SKIP_NAMES = {"__pycache__", ".DS_Store"}


def manifest_files(manifest: Optional[dict[str, Any]] = None) -> Iterator[tuple[dict[str, Any], Path, str]]:
    """Every file the sync manifest ships: its entry, its source path and where a consumer gets it."""
    if manifest is None:
        manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["entries"]:
        source, destination = REPO_ROOT / entry["source"], entry.get("destination", entry["source"])
        if not source.is_dir():
            yield entry, source, destination
            continue
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_file() and not SYNC_SKIP_NAMES.intersection(relative.parts):
                yield entry, path, f"{destination}/{relative.as_posix()}"


class SyncedDocLinkTests(unittest.TestCase):
    """A synced document's relative links must resolve in a consumer, which gets only the manifest's
    files. A core document may link only to core files, since a consumer can leave a component out."""

    def test_every_relative_link_in_a_synced_document_resolves_to_a_synced_file(self) -> None:
        core: set[str] = set()
        shipped: set[str] = set()
        documents: dict[Path, tuple[str, bool]] = {}
        for entry, path, shipped_path in manifest_files():
            is_core = "component" not in entry
            shipped.add(shipped_path)
            if is_core:
                core.add(shipped_path)
            if path.suffix == ".md":
                documents[path] = (shipped_path, is_core)
        broken = []
        for path, (shipped_path, is_core) in sorted(documents.items()):
            reachable = core if is_core else shipped
            for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
                if re.match(r"[a-z][a-z0-9+.-]*:|#", target):
                    continue
                resolved = os.path.normpath(os.path.join(os.path.dirname(shipped_path), target.split("#")[0])).replace(
                    os.sep, "/"
                )
                if resolved not in reachable and not any(name.startswith(f"{resolved}/") for name in reachable):
                    broken.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual([], broken, "link to the file on GitHub instead, as the consumer may not get it")


class OptionalImportTests(unittest.TestCase):
    """A synced module's imports of optional packages are marked for the consumer's type checker."""

    # The telemetry extra (SPEC-006). A consumer that doesn't install it still type-checks the
    # synced code, and Owlory's pyright treats a missing import as an error.
    OPTIONAL = ("logfire", "opentelemetry")
    MARKER = re.compile(r"#\s*pyright:\s*ignore\[[^\]]*\breportMissingImports\b")

    def test_every_optional_dependency_is_listed(self) -> None:
        # OPTIONAL must cover every package in pyproject's extras, or a new one passes unchecked.
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        for extra, requirements in project.get("optional-dependencies", {}).items():
            for requirement in requirements:
                match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
                assert match is not None, requirement
                with self.subTest(extra=extra, requirement=requirement):
                    self.assertIn(re.split(r"[-_.]", match.group(0).lower())[0], self.OPTIONAL)

    def test_every_synced_import_of_an_optional_package_is_marked(self) -> None:
        # Synced tests included: the consumer's pyright checks them too, and a skip doesn't help it.
        unmarked = []
        for _, path, _ in manifest_files():
            if path.suffix != ".py":
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for node in ast.walk(ast.parse(text)):
                # Pyright reports a missing import at the module name: each alias's own line for
                # ``import``, which a continued ``import os, \`` can move past the statement's first.
                if isinstance(node, ast.Import):
                    modules = [(alias.name, alias.lineno) for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    modules = [(node.module, node.lineno)]
                else:
                    continue
                for module, lineno in modules:
                    if module.split(".")[0] in self.OPTIONAL and not self.MARKER.search(lines[lineno - 1]):
                        unmarked.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual([], unmarked, "add  # pyright: ignore[reportMissingImports]")


if __name__ == "__main__":
    unittest.main()
