"""The release SBOM lists exactly what installing Boundroom brings in, or the release refuses it (SPEC-009 RA-ENT-004)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "Tools/release_sbom.py"
_spec = importlib.util.spec_from_file_location("release_sbom", SCRIPT)
assert _spec and _spec.loader
release_sbom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_sbom)

REQUIREMENTS = "pydantic==2.14.3\ncolorama==0.4.6 ; sys_platform == 'win32'\n"


def bom(*components: tuple[str, str], version: str = "0.5.0") -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "metadata": {"component": {"name": "boundroom", "version": version}},
        "components": [{"name": name, "version": number} for name, number in components],
    }


COMPLETE = bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"))


# The check of an export against the requirements and the tag.


@pytest.mark.parametrize(
    ("document", "requirements", "tag"),
    [
        (COMPLETE, REQUIREMENTS, "v0.5.0"),
        (bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.5.0rc1"), REQUIREMENTS, "v0.5.0-rc1"),
        (bom(("pyyaml", "6.0.2")), "PyYAML==6.0.2\n", "v0.5.0"),
        (bom(("PyYAML", "6.0.2")), "pyyaml==6.0.2\n", "v0.5.0"),
        (COMPLETE, "pydantic==2.14.3\npydantic==2.14.3\ncolorama==0.4.6\n", "v0.5.0"),
        (COMPLETE, "# via boundroom\n\npydantic==2.14.3\n   \n# via pydantic\ncolorama==0.4.6\n", "v0.5.0"),
        (COMPLETE, "pydantic==2.14.3 ; python_version >= '3.11'\ncolorama==0.4.6; sys_platform == 'win32'\n", "v0.5.0"),
    ],
    ids=["final", "pep-440-rc", "lowercase-sbom", "lowercase-requirements", "duplicate-line", "comments-blanks", "markers"],
)
def test_an_sbom_matching_the_requirements_and_the_tag_passes(document, requirements, tag) -> None:
    assert release_sbom.check(document, requirements, tag) is None


@pytest.mark.parametrize(
    ("document", "said"),
    [
        (bom(("pydantic", "2.14.3")), "misses packages: colorama 0.4.6"),
        (bom(("pydantic", "2.14.2"), ("colorama", "0.4.6")), "misses packages: pydantic 2.14.3"),
        (
            {**COMPLETE, "components": [{"name": "pydantic"}, {"name": "colorama", "version": "0.4.6"}]},
            "misses packages: pydantic 2.14.3",
        ),
    ],
    ids=["missing-package", "wrong-version", "no-version"],
)
def test_a_missing_package_or_wrong_version_is_refused(document, said) -> None:
    with pytest.raises(release_sbom.SbomError, match=re.escape(said)):
        release_sbom.check(document, REQUIREMENTS, "v0.5.0")


@pytest.mark.parametrize(
    ("extra", "said"),
    [(("pytest", "9.2.0"), "doesn't bring in: pytest 9.2.0"), (("boundroom", "0.5.0"), "doesn't bring in: boundroom 0.5.0")],
    ids=["a-dev-tool", "the-project-itself"],
)
def test_a_package_an_install_doesnt_bring_in_is_refused(extra, said) -> None:
    """A dev tool in the SBOM would be reported against the release."""
    with pytest.raises(release_sbom.SbomError, match=re.escape(said)):
        release_sbom.check(bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), extra), REQUIREMENTS, "v0.5.0")


PACKAGES = COMPLETE["components"]


@pytest.mark.parametrize(
    ("document", "tag"),
    [
        ({**COMPLETE, "metadata": {"component": {"name": "other", "version": "0.5.0"}}}, "v0.5.0"),
        (bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.4.0"), "v0.5.0"),
        ({"bomFormat": "CycloneDX", "components": PACKAGES}, "v0.5.0"),
        ({"bomFormat": "CycloneDX", "metadata": {}, "components": PACKAGES}, "v0.5.0"),
        # A version that only starts with the tag's: 0.5.10 isn't 0.5.1, and a release
        # candidate or a dev release isn't the final release.
        (bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.5.10"), "v0.5.1"),
        (bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.5.0rc1"), "v0.5.0"),
        (bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.5.0.dev0"), "v0.5.0"),
        (bom(("pydantic", "2.14.3"), ("colorama", "0.4.6"), version="0.5.01"), "v0.5.0-rc1"),
    ],
    ids=["wrong-name", "wrong-version", "no-metadata", "no-metadata-component", "0.5.10", "rc1", "dev0", "0.5.01"],
)
def test_the_project_must_be_boundroom_at_the_tags_version(document, tag) -> None:
    with pytest.raises(release_sbom.SbomError, match="bump pyproject.toml"):
        release_sbom.check(document, REQUIREMENTS, tag)


@pytest.mark.parametrize(
    ("document", "requirements", "tag", "said"),
    [
        (COMPLETE, REQUIREMENTS, "0.5.0", "isn't a vMAJOR"),
        (COMPLETE, REQUIREMENTS, "v0.05.0", "isn't a vMAJOR"),
        (COMPLETE, "", "v0.5.0", "lists no packages"),
        (COMPLETE, "# via nothing\n\n", "v0.5.0", "lists no packages"),
        ({**COMPLETE, "bomFormat": "SPDX"}, REQUIREMENTS, "v0.5.0", "CycloneDX"),
    ],
    ids=["no-v", "leading-zero", "empty-export", "comment-only-export", "spdx"],
)
def test_a_malformed_tag_an_empty_export_or_another_format_is_refused(document, requirements, tag, said) -> None:
    with pytest.raises(release_sbom.SbomError, match=said):
        release_sbom.check(document, requirements, tag)


@pytest.mark.parametrize("line", ["tool @ https://example.invalid/tool.whl", "-e .", "pyyaml>=6.0"])
def test_a_requirement_it_cant_read_is_refused_not_skipped(line) -> None:
    with pytest.raises(release_sbom.SbomError, match=re.escape(f"can't read the requirement '{line}'")):
        release_sbom.required(f"pydantic==2.14.3\n{line}\n")


def test_comments_and_blank_lines_are_ignored() -> None:
    assert release_sbom.required("# via boundroom\n\npydantic==2.14.3\n") == {("pydantic", "2.14.3")}


# The command, with a fake uv on PATH. uv export is a read-only query, so the fake only answers it:
# from canned files named after the --format value, and with exit 2 for any other selection than
# the release's (every extra, no dev group, a lock that matches pyproject.toml).

FAKE_UV = """\
import sys
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[1:]
if not {"--locked", "--all-extras", "--no-dev"} <= set(args):
    print("fake uv: unexpected selection", file=sys.stderr)
    sys.exit(2)
kind = args[args.index("--format") + 1]
for name, stream in (("stderr", sys.stderr), ("stdout", sys.stdout)):
    canned = here / f"{kind}.{name}"
    if canned.exists():
        stream.write(canned.read_text(encoding="utf-8"))
code = here / f"{kind}.exit"
sys.exit(int(code.read_text(encoding="utf-8")) if code.exists() else 0)
"""

Answer = Callable[..., None]


def install_fake_uv(bin_dir: Path) -> Answer:
    """Write uv into bin_dir, answering both exports correctly until told otherwise."""
    bin_dir.mkdir()
    (bin_dir / "fake_uv.py").write_text(FAKE_UV, encoding="utf-8")
    uv = bin_dir / "uv"
    uv.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(bin_dir / "fake_uv.py"))} "$@"\n', encoding="utf-8"
    )
    uv.chmod(0o755)

    def answer(kind: str, stdout: str = "", stderr: str = "", code: int = 0) -> None:
        (bin_dir / f"{kind}.stdout").write_text(stdout, encoding="utf-8")
        (bin_dir / f"{kind}.stderr").write_text(stderr, encoding="utf-8")
        (bin_dir / f"{kind}.exit").write_text(str(code), encoding="utf-8")

    answer("cyclonedx1.5", json.dumps(COMPLETE))
    answer("requirements.txt", REQUIREMENTS)
    return answer


@pytest.fixture
def uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Answer:
    answer = install_fake_uv(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.chdir(tmp_path)
    return answer


@pytest.fixture
def output(tmp_path: Path) -> Path:
    return tmp_path / "sbom.json"


def test_a_matching_export_is_written_and_exits_0(uv, output, capsys) -> None:
    assert release_sbom.main([str(output), "v0.5.0"]) == 0

    written = output.read_text(encoding="utf-8")
    assert json.loads(written) == COMPLETE
    assert written.endswith("\n")
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {output}")
    assert err == ""


def test_a_wrong_export_writes_nothing_and_exits_1(uv, output, capsys) -> None:
    uv("cyclonedx1.5", json.dumps(bom(("pydantic", "2.14.3"))))

    assert release_sbom.main([str(output), "v0.5.0"]) == 1

    assert not output.exists()
    out, err = capsys.readouterr()
    assert err.startswith("release_sbom: the SBOM misses packages")
    assert out == ""


@pytest.mark.parametrize("kind", ["cyclonedx1.5", "requirements.txt"])
def test_a_failing_uv_export_exits_1_with_uvs_message(uv, output, capsys, kind) -> None:
    uv(kind, stderr="error: the lockfile needs to be updated\n", code=2)

    assert release_sbom.main([str(output), "v0.5.0"]) == 1

    assert not output.exists()
    err = capsys.readouterr().err
    assert err.startswith(f"release_sbom: uv export --format {kind} failed:")
    assert "error: the lockfile needs to be updated" in err


def test_uv_missing_from_path_exits_1(tmp_path, monkeypatch, output, capsys) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)

    assert release_sbom.main([str(output), "v0.5.0"]) == 1

    assert not output.exists()
    assert capsys.readouterr().err.startswith("release_sbom: can't run uv:")


def test_an_export_that_isnt_json_exits_1(uv, output, capsys) -> None:
    uv("cyclonedx1.5", "warning: sbom-export is a preview feature\n")

    assert release_sbom.main([str(output), "v0.5.0"]) == 1

    assert not output.exists()
    assert capsys.readouterr().err.startswith("release_sbom: uv export didn't write JSON")


def test_an_unwritable_output_exits_1(uv, tmp_path, capsys) -> None:
    output = tmp_path / "missing" / "sbom.json"

    assert release_sbom.main([str(output), "v0.5.0"]) == 1

    assert not output.exists()
    assert capsys.readouterr().err.startswith("release_sbom: ")


@pytest.mark.parametrize("arguments", [0, 1, 3])
def test_the_command_needs_the_output_and_the_tag(uv, output, capsys, arguments) -> None:
    argv = [str(output), "v0.5.0", "extra"][:arguments]

    assert release_sbom.main(argv) == 64

    assert not output.exists()
    assert capsys.readouterr() == ("", "usage: Tools/release_sbom.py <output> <tag>\n")


def test_the_script_as_release_yml_runs_it(tmp_path, output) -> None:
    install_fake_uv(tmp_path / "bin")
    environment = {**os.environ, "PATH": str(tmp_path / "bin")}

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(output), "v0.5.0"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == COMPLETE


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_this_repositorys_lock_exports_its_runtime_packages(monkeypatch, output) -> None:
    """The real export, as release.yml runs it, with this checkout's own version as the tag."""
    monkeypatch.chdir(REPO_ROOT)
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    assert release_sbom.main([str(output), f"v{version}"]) == 0

    names = {name for name, _ in release_sbom.listed(json.loads(output.read_text(encoding="utf-8")))}
    assert "pydantic" in names
    assert "logfire" in names  # the telemetry extra
    assert not names & {"pytest", "ruff", "ty", "mkdocs-material", "pip-audit", "coverage"}
