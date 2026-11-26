"""The release gate refuses a tag the plugin manifests or pyproject.toml don't declare (SPEC-002 step 5, SPEC-009 RA-ENT-004)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "Tools/check_plugin_version.py"
spec = importlib.util.spec_from_file_location("check_plugin_version", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

PLUGIN_JSON = "plugins/boundroom/plugin.json"
CLAUDE_JSON = "plugins/boundroom/.claude-plugin/plugin.json"
CODEX_JSON = "plugins/boundroom/.codex-plugin/plugin.json"
MANIFESTS = (PLUGIN_JSON, CLAUDE_JSON, CODEX_JSON)
NO_PROJECT_VERSION = ["pyproject.toml: no readable [project] version"]
PROG = "check_plugin_version.py"


def write_manifest(root: Path, path: str, version: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"name": "boundroom", "version": version}), encoding="utf-8")


def write_pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "boundroom"\nversion = "{version}"\n\n[tool.uv]\nversion = "9.9.9"\n', encoding="utf-8"
    )


def repo(root: Path, version: str) -> Path:
    """A checkout whose three manifests and pyproject.toml all declare one version."""
    for path in MANIFESTS:
        write_manifest(root, path, version)
    write_pyproject(root, version)
    return root


def declares(path: str, version: str, tag: str, expected: str) -> str:
    return f"{path}: declares '{version}', but tag {tag} needs '{expected}'"


# This repository's own files.


def test_the_real_manifests_pass_for_their_own_version():
    version = json.loads((REPO_ROOT / gate.PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]

    assert gate.problems(REPO_ROOT, f"v{version}") == []


def test_the_real_pyproject_passes_for_its_own_version():
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    assert gate.project_problems(REPO_ROOT, f"v{version}") == []


# The manifests.


@pytest.mark.parametrize("version", ["0.5.0", "0.0.0"])
def test_manifests_at_the_tags_version_pass(tmp_path, version):
    repo(tmp_path, version)

    assert gate.problems(tmp_path, f"v{version}") == []
    assert gate.project_problems(tmp_path, f"v{version}") == []


@pytest.mark.parametrize("path", MANIFESTS)
def test_each_manifest_at_another_version_is_named(tmp_path, path):
    repo(tmp_path, "0.5.0")
    write_manifest(tmp_path, path, "0.4.0")

    assert gate.problems(tmp_path, "v0.5.0") == [f"{path}: declares '0.4.0', but tag v0.5.0 needs '0.5.0'"]


def test_a_newer_tag_over_older_manifests_names_every_manifest(tmp_path):
    repo(tmp_path, "0.1.0")

    assert gate.problems(tmp_path, "v0.2.0") == [declares(path, "0.1.0", "v0.2.0", "0.2.0") for path in MANIFESTS]


def test_manifests_that_disagree_are_refused_whatever_the_tag(tmp_path):
    repo(tmp_path, "0.2.0")
    write_manifest(tmp_path, CODEX_JSON, "0.3.0")

    assert gate.problems(tmp_path, "v0.2.0") == [declares(CODEX_JSON, "0.3.0", "v0.2.0", "0.2.0")]
    assert gate.problems(tmp_path, "v0.3.0") == [
        declares(PLUGIN_JSON, "0.2.0", "v0.3.0", "0.3.0"),
        declares(CLAUDE_JSON, "0.2.0", "v0.3.0", "0.3.0"),
    ]


@pytest.mark.parametrize("path", MANIFESTS)
def test_a_missing_manifest_is_refused_even_when_the_rest_match(tmp_path, path):
    repo(tmp_path, "0.5.0")
    (tmp_path / path).unlink()

    assert gate.problems(tmp_path, "v0.5.0") == [f"{path}: missing"]


def test_every_missing_manifest_is_named_in_manifest_order(tmp_path):
    write_pyproject(tmp_path, "0.5.0")

    assert gate.problems(tmp_path, "v0.5.0") == [
        "plugins/boundroom/plugin.json: missing",
        "plugins/boundroom/.claude-plugin/plugin.json: missing",
        "plugins/boundroom/.codex-plugin/plugin.json: missing",
    ]


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"", b"[]", b"null", b'"0.5.0"', b'{"name": "x"}', b'{"version": "0.5.0\xff"}'],
    ids=["not-json", "empty", "a-list", "null", "a-string", "no-version-key", "not-utf-8"],
)
def test_a_manifest_without_a_readable_version_is_refused(tmp_path, content):
    repo(tmp_path, "0.5.0")
    (tmp_path / PLUGIN_JSON).write_bytes(content)

    assert gate.problems(tmp_path, "v0.5.0") == ["plugins/boundroom/plugin.json: no readable version"]


@pytest.mark.parametrize("version", ["v0.5.0", " 0.5.0"])
def test_a_manifest_must_declare_the_tag_without_its_v(tmp_path, version):
    repo(tmp_path, "0.5.0")
    write_manifest(tmp_path, CLAUDE_JSON, version)

    assert gate.problems(tmp_path, "v0.5.0") == [declares(CLAUDE_JSON, version, "v0.5.0", "0.5.0")]


def test_a_prerelease_tag_needs_the_manifests_semver_prerelease(tmp_path):
    repo(tmp_path, "0.5.0-rc.1")
    assert gate.problems(tmp_path, "v0.5.0-rc.1") == []
    assert gate.problems(tmp_path, "v0.5.0") == [declares(path, "0.5.0-rc.1", "v0.5.0", "0.5.0") for path in MANIFESTS]

    for version in ("0.5.0", "0.5.0rc1"):
        repo(tmp_path, version)
        assert gate.problems(tmp_path, "v0.5.0-rc1") == [declares(path, version, "v0.5.0-rc1", "0.5.0-rc1") for path in MANIFESTS]


# pyproject.toml's project version.


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("0.5.0", "v0.5.0"),
        ("0.5.0rc1", "v0.5.0-rc1"),
        ("0.5.0rc1", "v0.5.0-rc.1"),
        ("0.5.0b2", "v0.5.0-beta.2"),
        ("0.5.0-rc1", "v0.5.0-rc1"),
        # The final version passes for its release candidate's tag. The module docstring says
        # otherwise; this pins today's behaviour until the owner decides.
        ("0.5.0", "v0.5.0-rc1"),
    ],
)
def test_pyproject_at_the_tags_version_passes(tmp_path, version, tag):
    write_pyproject(tmp_path, version)

    assert gate.project_problems(tmp_path, tag) == []


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("0.4.0", "v0.5.0"),
        ("0.5.10", "v0.5.1"),
        ("0.5.0rc1", "v0.5.0"),
        ("0.5.0-rc1", "v0.5.0"),
        ("0.5.0.post1", "v0.5.0-rc1"),
        ("0.5.0post1", "v0.5.0-rc1"),
        ("0.5.01", "v0.5.0-rc1"),
        ("0.5.0+local", "v0.5.0-rc1"),
        ("0.5.1rc1", "v0.5.0-rc1"),
    ],
)
def test_pyproject_must_declare_the_tags_version(tmp_path, version, tag):
    """A tag over an unbumped pyproject.toml is refused before the tag is pushed (SPEC-009 RA-ENT-004)."""
    write_pyproject(tmp_path, version)

    found = gate.project_problems(tmp_path, tag)

    assert len(found) == 1, found
    assert found[0].startswith(f"pyproject.toml: declares '{version}', but tag {tag} needs ")
    assert found[0].endswith("; run uv lock after bumping it")


def test_a_pyproject_at_another_version_names_the_remedy(tmp_path):
    write_pyproject(tmp_path, "0.4.0")

    assert gate.project_problems(tmp_path, "v0.5.0") == [
        "pyproject.toml: declares '0.4.0', but tag v0.5.0 needs '0.5.0'; run uv lock after bumping it"
    ]


@pytest.mark.parametrize(
    "content",
    [
        b'[project]\nname = "boundroom"\ndescription = "x"\nversion = "0.5.0"\n',
        b'[tool.uv]\nversion = "9.9.9"\n\n[project]\nversion = "0.5.0"\n',
        b'[build-system]\nrequires = []\n\n[project]\nname = "boundroom"\nversion = "0.5.0"\n',
        b'[project]\nversion = "0.5.0"  # bumped by the release pull request\n',
        b'[project]\nversion="0.5.0"\n',
        b'[project]\r\nname = "boundroom"\r\nversion = "0.5.0"\r\n',
        b'[project] \nversion = "0.5.0"\n',
    ],
    ids=[
        "not-the-first-key",
        "tool-version-before-project",
        "project-after-another-table",
        "trailing-comment",
        "no-spaces",
        "crlf",
        "trailing-space-after-table",
    ],
)
def test_pyproject_forms_that_declare_the_project_version(tmp_path, content):
    (tmp_path / "pyproject.toml").write_bytes(content)

    assert gate.project_problems(tmp_path, "v0.5.0") == []


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b'[tool.uv]\nversion = "0.5.0"\n',
        b'[project]\nname = "boundroom"\ndynamic = ["version"]\n',
        b'[project]\nname = "boundroom"\n\n[project.urls]\nversion = "0.5.0"\n',
        b'[project]\nname = "boundroom"\n\n[tool.uv]\nversion = "0.5.0"\n',
        b'[project]\nname = "boundroom"\nversion = ""\n',
    ],
    ids=[
        "missing-file",
        "empty-file",
        "no-project-table",
        "dynamic-version",
        "version-only-in-project-urls",
        "version-only-in-a-tool-table",
        "empty-version",
    ],
)
def test_pyproject_without_a_readable_project_version_is_refused(tmp_path, content):
    if content is not None:
        (tmp_path / "pyproject.toml").write_bytes(content)

    assert gate.project_problems(tmp_path, "v0.5.0") == NO_PROJECT_VERSION


# The command, run in-process from the checkout.


def test_the_command_passes_a_tag_every_file_declares(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo(tmp_path, "0.5.0"))

    assert gate.main([PROG, "v0.5.0"]) == 0
    assert capsys.readouterr() == ("", "")


def test_the_command_prints_one_error_annotation_per_problem_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo(tmp_path, "0.5.0"))
    write_manifest(tmp_path, CLAUDE_JSON, "0.4.0")
    write_pyproject(tmp_path, "0.4.0")

    assert gate.main([PROG, "v0.5.0"]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "::error::plugins/boundroom/.claude-plugin/plugin.json: declares '0.4.0', but tag v0.5.0 needs '0.5.0'",
        "::error::pyproject.toml: declares '0.4.0', but tag v0.5.0 needs '0.5.0'; run uv lock after bumping it",
    ]
    assert err == ""


def test_the_command_checks_pyproject_even_when_the_manifests_match(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo(tmp_path, "0.5.0"))
    write_pyproject(tmp_path, "0.4.0")

    assert gate.main([PROG, "v0.5.0"]) == 1
    out, err = capsys.readouterr()
    assert out == "::error::pyproject.toml: declares '0.4.0', but tag v0.5.0 needs '0.5.0'; run uv lock after bumping it\n"
    assert err == ""


@pytest.mark.parametrize("argv", [[PROG], [PROG, "v0.5.0", "v0.5.1"]], ids=["no-tag", "two-tags"])
def test_the_command_needs_exactly_one_tag(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(repo(tmp_path, "0.5.0"))

    assert gate.main(argv) == 64
    assert capsys.readouterr() == ("", "usage: Tools/check_plugin_version.py <tag>\n")


def test_the_script_as_release_yml_runs_it(tmp_path):
    repo(tmp_path, "0.5.0")
    write_manifest(tmp_path, CODEX_JSON, "9.9.9")

    result = subprocess.run([sys.executable, str(SCRIPT), "v0.5.0"], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 1, result.stderr
    assert result.stdout.startswith("::error::plugins/boundroom/.codex-plugin/plugin.json: declares '9.9.9'")
