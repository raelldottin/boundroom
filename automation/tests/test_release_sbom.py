"""The release SBOM lists exactly what installing Boundroom brings in, or the release refuses it (SPEC-009 RA-ENT-004)."""

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

REQUIREMENTS = "pydantic==2.14.3\ncolorama==0.4.6 ; sys_platform == 'win32'\n"


def bom(*components: tuple[str, str], version: str = "0.5.0") -> dict:
    return {
        "bomFormat": "CycloneDX",
        "metadata": {"component": {"name": "boundroom", "version": version}},
        "components": [{"name": name, "version": number} for name, number in components],
    }


COMPLETE = bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"))


def completed(stdout: str, returncode: int = 0, stderr: str = "") -> mock.Mock:
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class CheckTests(unittest.TestCase):
    def test_an_sbom_matching_the_requirements_and_the_tag_passes(self) -> None:
        release_sbom.check(COMPLETE, REQUIREMENTS, "v0.5.0")
        release_sbom.check(bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.5.0rc1"), REQUIREMENTS, "v0.5.0-rc1")

    def test_a_missing_package_or_version_is_refused(self) -> None:
        for document in (bom(("pydantic", "2.14.3")), bom(("pydantic", "2.14.2"), ("colorama", "0.4.6"))):
            with self.subTest(document=document), self.assertRaisesRegex(release_sbom.SbomError, "misses packages"):
                release_sbom.check(document, REQUIREMENTS, "v0.5.0")

    def test_a_package_an_install_doesnt_bring_in_is_refused(self) -> None:
        """A dev tool in the SBOM would be reported against the release."""
        document = bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), ("pytest", "9.2.0"))
        with self.assertRaisesRegex(release_sbom.SbomError, "doesn't bring in: pytest 9.2.0"):
            release_sbom.check(document, REQUIREMENTS, "v0.5.0")

    def test_a_version_that_isnt_the_tags_is_refused(self) -> None:
        with self.assertRaisesRegex(release_sbom.SbomError, "boundroom 0.4.0, not boundroom 0.5.0: bump pyproject.toml"):
            release_sbom.check(bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.4.0"), REQUIREMENTS, "v0.5.0")

    def test_a_malformed_tag_an_empty_export_or_another_format_is_refused(self) -> None:
        for document, requirements, tag, said in (
            (COMPLETE, REQUIREMENTS, "0.5.0", "isn't a vMAJOR"),
            (COMPLETE, "", "v0.5.0", "lists no packages"),
            ({**COMPLETE, "bomFormat": "SPDX"}, REQUIREMENTS, "v0.5.0", "CycloneDX"),
        ):
            with self.subTest(said=said), self.assertRaisesRegex(release_sbom.SbomError, said):
                release_sbom.check(document, requirements, tag)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.output = self.directory / "sbom.json"

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return release_sbom.main(argv), stderr.getvalue()

    def test_a_missing_argument_is_a_usage_error(self) -> None:
        for argv in ([], [str(self.output)]):
            with self.subTest(argv=argv):
                self.assertEqual(release_sbom.EX_USAGE, self.run_main(argv)[0])

    def test_a_failing_export_writes_nothing_and_exits_1(self) -> None:
        with mock.patch.object(release_sbom.subprocess, "run", return_value=completed("", 2, "error: no lock")):
            code, said = self.run_main([str(self.output), "v0.5.0"])
        self.assertEqual((1, False), (code, self.output.exists()))
        self.assertIn("failed: error: no lock", said)

    def test_a_wrong_export_writes_nothing_and_exits_1(self) -> None:
        exports = [completed(json.dumps(bom(("pydantic", "2.14.3")))), completed(REQUIREMENTS)]
        with mock.patch.object(release_sbom.subprocess, "run", side_effect=exports):
            code, said = self.run_main([str(self.output), "v0.5.0"])
        self.assertEqual((1, False), (code, self.output.exists()))
        self.assertIn("misses packages: colorama 0.4.6", said)

    @unittest.skipUnless(shutil.which("uv"), "uv is not on PATH")
    def test_this_repositorys_lock_exports_its_runtime_packages(self) -> None:
        """The real export, as release.yml runs it, with this checkout's own version as the tag."""
        cwd = Path.cwd()
        os.chdir(REPO_ROOT)
        self.addCleanup(os.chdir, cwd)
        project = next(
            line
            for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version = ")
        )
        tag = "v" + project.split('"')[1]
        code, said = self.run_main([str(self.output), tag])
        self.assertEqual(0, code, said)
        names = {name for name, _ in release_sbom.listed(json.loads(self.output.read_text(encoding="utf-8")))}
        self.assertIn("pydantic", names)
        self.assertIn("logfire", names)  # the telemetry extra
        self.assertFalse(names & {"pytest", "ruff", "ty", "mkdocs-material", "pip-audit", "coverage"})


if __name__ == "__main__":
    unittest.main()
