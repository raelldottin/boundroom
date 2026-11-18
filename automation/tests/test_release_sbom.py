"""The release SBOM names every locked package, or the release refuses it (SPEC-009 RA-ENT-004)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("release_sbom", REPO_ROOT / "Tools/release_sbom.py")
assert _spec and _spec.loader
release_sbom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_sbom)

LOCK = """
version = 1

[[package]]
name = "boundroom"
version = "0.4.0"
source = { virtual = "." }

[[package]]
name = "pyyaml"
version = "6.0.2"
"""


def bom(*components: tuple[str, str], root: tuple[str, str] | None = ("boundroom", "0.4.0")) -> dict:
    document: dict = {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}
    if root:
        document["metadata"] = {"component": {"name": root[0], "version": root[1]}}
    document["components"] = [{"name": name, "version": version} for name, version in components]
    return document


class CheckTests(unittest.TestCase):
    def test_an_sbom_naming_every_locked_package_passes(self) -> None:
        release_sbom.check(bom(("pyyaml", "6.0.2")), LOCK)

    def test_a_missing_package_or_version_is_refused(self) -> None:
        for document in (bom(), bom(("pyyaml", "6.0.1")), bom(("pyyaml", "6.0.2"), root=None)):
            with self.subTest(document=document), self.assertRaisesRegex(release_sbom.SbomError, "misses locked packages"):
                release_sbom.check(document, LOCK)

    def test_a_document_that_is_not_cyclonedx_is_refused(self) -> None:
        with self.assertRaisesRegex(release_sbom.SbomError, "CycloneDX"):
            release_sbom.check({**bom(("pyyaml", "6.0.2")), "bomFormat": "SPDX"}, LOCK)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.output = self.directory / "sbom.json"

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return release_sbom.main(argv), stderr.getvalue()

    def test_no_output_path_is_a_usage_error(self) -> None:
        self.assertEqual(release_sbom.EX_USAGE, self.run_main([])[0])

    def test_a_failing_export_writes_nothing_and_exits_1(self) -> None:
        failed = mock.Mock(returncode=2, stdout="", stderr="error: no lock")
        with mock.patch.object(release_sbom.subprocess, "run", return_value=failed):
            code, said = self.run_main([str(self.output)])
        self.assertEqual((1, False), (code, self.output.exists()))
        self.assertIn("uv export failed: error: no lock", said)

    def test_an_incomplete_export_writes_nothing_and_exits_1(self) -> None:
        exported = mock.Mock(returncode=0, stdout=json.dumps(bom()), stderr="")
        with mock.patch.object(release_sbom.subprocess, "run", return_value=exported):
            code, said = self.run_main([str(self.output)])
        self.assertEqual((1, False), (code, self.output.exists()))
        self.assertIn("misses locked packages", said)

    @unittest.skipUnless(shutil.which("uv"), "uv is not on PATH")
    def test_this_repositorys_lock_exports_completely(self) -> None:
        """The real export, as release.yml runs it, from this checkout's uv.lock."""
        cwd = Path.cwd()
        os.chdir(REPO_ROOT)
        self.addCleanup(os.chdir, cwd)
        code, said = self.run_main([str(self.output)])
        self.assertEqual(0, code, said)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        locked = release_sbom.locked((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        self.assertEqual(locked, release_sbom.listed(document))
        self.assertTrue(all(component.get("purl", "").startswith("pkg:pypi/") for component in document["components"]))


if __name__ == "__main__":
    unittest.main()
