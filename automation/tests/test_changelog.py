"""CHANGELOG.md has the release's section, and the release notes come from it (SPEC-009 RA-ENT-006)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "Tools/changelog_section.py"
spec = importlib.util.spec_from_file_location("changelog_section", SCRIPT)
assert spec and spec.loader
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)

plugin_spec = importlib.util.spec_from_file_location("check_plugin_version", REPO_ROOT / "Tools/check_plugin_version.py")
assert plugin_spec and plugin_spec.loader
plugin_gate = importlib.util.module_from_spec(plugin_spec)
plugin_spec.loader.exec_module(plugin_gate)

TEXT = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def declared_versions() -> set[str]:
    """The version every manifest declares, which the release tag must match."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    versions = {project["project"]["version"]}
    for name in plugin_gate.MANIFESTS:
        manifest = json.loads((REPO_ROOT / plugin_gate.PLUGIN / name).read_text(encoding="utf-8"))
        versions.add(manifest["version"])
    return versions


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-", 1)[0].split("."))


def test_the_manifests_agree_on_one_version() -> None:
    assert len(declared_versions()) == 1, declared_versions()


def test_the_changelog_has_a_section_for_the_declared_version() -> None:
    (version,) = declared_versions()
    assert changelog.section(TEXT, f"v{version}").strip()


def test_unreleased_comes_first() -> None:
    headings = [line for line in TEXT.splitlines() if line.startswith("## ")]
    assert headings[0] == changelog.UNRELEASED


def test_releases_run_newest_first_and_dates_never_go_forward() -> None:
    released = changelog.sections(TEXT)
    versions = list(released)
    assert versions == sorted(versions, key=as_tuple, reverse=True)
    dates = [date for date, _ in released.values()]
    assert dates == sorted(dates, reverse=True)


def test_every_released_section_has_content() -> None:
    for version, (_, body) in changelog.sections(TEXT).items():
        assert body, version


SAMPLE = """# Changelog

## Unreleased

- Something pending.

## 1.2.0 (2026-12-01)

### Added

- A feature.

## 1.1.0 (2026-11-01)

- A fix.
"""


def test_the_section_is_the_text_under_its_heading() -> None:
    assert changelog.section(SAMPLE, "v1.2.0") == "### Added\n\n- A feature."
    assert changelog.section(SAMPLE, "v1.1.0") == "- A fix."


def test_unreleased_text_is_never_a_release_section() -> None:
    assert "pending" not in "".join(body for _, body in changelog.sections(SAMPLE).values())


def test_a_missing_section_is_refused_with_what_to_do() -> None:
    with pytest.raises(changelog.ChangelogError, match=r"no section for 1\.3\.0.*renames '## Unreleased'"):
        changelog.section(SAMPLE, "v1.3.0")


def test_a_prerelease_needs_its_own_section() -> None:
    with pytest.raises(changelog.ChangelogError, match="no section for 1.2.0-rc.1"):
        changelog.section(SAMPLE, "v1.2.0-rc.1")
    with_rc = SAMPLE.replace("## 1.2.0 (", "## 1.2.0-rc.1 (2026-11-20)\n\n- Candidate.\n\n## 1.2.0 (")
    assert changelog.section(with_rc, "v1.2.0-rc.1") == "- Candidate."


def test_an_empty_section_is_refused() -> None:
    with pytest.raises(changelog.ChangelogError, match="empty"):
        changelog.section("## 1.0.0 (2026-01-01)\n\n## 0.9.0 (2025-12-01)\n\n- x\n", "v1.0.0")


def test_a_duplicate_section_is_refused() -> None:
    with pytest.raises(changelog.ChangelogError, match="two sections for 1.1.0"):
        changelog.sections(SAMPLE + "\n## 1.1.0 (2026-10-01)\n\n- again\n")


def test_a_heading_without_a_date_is_refused() -> None:
    with pytest.raises(changelog.ChangelogError, match="unreadable heading"):
        changelog.sections("## 1.0.0\n\n- x\n")


def test_a_malformed_tag_is_a_usage_error() -> None:
    for tag in ("1.2.0", "v1.2", "v01.2.0", "latest"):
        with pytest.raises(ValueError):
            changelog.section(SAMPLE, tag)


@pytest.mark.parametrize(
    ("tag", "code", "stream", "expected"),
    [
        ("v1.2.0", 0, "stdout", "- A feature."),
        ("v9.9.9", 1, "stderr", "no section for 9.9.9"),
        ("nonsense", 64, "stderr", "is not vMAJOR.MINOR.PATCH"),
    ],
)
def test_the_command_line(tmp_path: Path, tag: str, code: int, stream: str, expected: str) -> None:
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE, encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), tag], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == code, result.stderr
    assert expected in getattr(result, stream)


def test_the_command_line_needs_one_tag(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 64
    assert "usage" in result.stderr
