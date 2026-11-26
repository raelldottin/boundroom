"""CHANGELOG.md has the release's section, and the release notes come from it (SPEC-009 RA-ENT-006)."""

from __future__ import annotations

import importlib.util
import json
import re
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


# The rest specifies the tool on changelogs written for each test.

SAMPLE = """# Changelog

All notable changes are recorded here.

## Unreleased

- Something pending.

## 1.2.0 (2026-01-15)

### Added

- A feature.

### Fixed

- A bug.

## 1.1.0 (2025-12-01)

- A fix.
"""
SECTION_1_2_0 = "### Added\n\n- A feature.\n\n### Fixed\n\n- A bug."
NEWER = "## 1.3.0 (2026-02-01)\n\n- Newer.\n\n"


def test_a_released_section_is_the_text_under_its_heading_up_to_the_next() -> None:
    assert changelog.section(SAMPLE, "v1.2.0") == SECTION_1_2_0
    assert changelog.sections(SAMPLE) == {
        "1.2.0": ("2026-01-15", SECTION_1_2_0),
        "1.1.0": ("2025-12-01", "- A fix."),
    }


def test_subheadings_h1_and_unspaced_hashes_stay_in_the_section() -> None:
    text = "## 1.2.0 (2026-01-15)\n#### Deep\n# Not a section\n##x\n##\n- item\n## 1.1.0 (2025-12-01)\n- A fix.\n"

    assert changelog.section(text, "v1.2.0") == "#### Deep\n# Not a section\n##x\n##\n- item"


@pytest.mark.parametrize("ending", ["", "\n", "\n\n\n"])
def test_the_last_section_runs_to_end_of_file(ending: str) -> None:
    text = "## 1.2.0 (2026-01-15)\n\n- A feature.\n\n## 1.1.0 (2025-12-01)\n\n- A fix.\n- Another fix." + ending

    assert changelog.section(text, "v1.1.0") == "- A fix.\n- Another fix."


def test_unreleased_text_is_never_a_release_section() -> None:
    assert "pending" not in "".join(body for _, body in changelog.sections(SAMPLE).values())
    assert changelog.section(SAMPLE, "v1.2.0") == SECTION_1_2_0


def test_unreleased_is_not_a_releasable_version() -> None:
    assert changelog.sections("## Unreleased\n\n- Something pending.\n") == {}
    with pytest.raises(changelog.ChangelogError, match="no section for 1.0.0"):
        changelog.section("## Unreleased\n\n- Something pending.\n", "v1.0.0")


def test_a_prerelease_tag_needs_its_own_section() -> None:
    with pytest.raises(changelog.ChangelogError, match=re.escape("no section for 1.2.0-rc.1")):
        changelog.section(SAMPLE, "v1.2.0-rc.1")

    with_rc = SAMPLE.replace("## 1.2.0 (", "## 1.2.0-rc.1 (2026-01-10)\n\n- Candidate.\n\n## 1.2.0 (")
    assert changelog.section(with_rc, "v1.2.0-rc.1") == "- Candidate."

    only_rc = "## 1.2.0-rc.1 (2026-01-10)\n\n- Candidate.\n"
    with pytest.raises(changelog.ChangelogError, match=re.escape("no section for 1.2.0.")):
        changelog.section(only_rc, "v1.2.0")


@pytest.mark.parametrize(
    "heading",
    [
        "## 1.3.0",  # no date, on the requested version
        "## 0.9.0",  # no date, on another version
        "## 0.9.0 (2026-1-15)",
        "## 0.9.0 (26-01-15)",
        "## 0.9.0 [2026-01-15]",
        "## 0.9.0(2026-01-15)",
        "## 0.9.0 (2026-01-15) - codename",
        "## 0.9.0 (2026-01-15, yanked)",
        "##  0.9.0 (2026-01-15)",
        "## unreleased",
    ],
)
def test_an_unreadable_heading_is_refused_naming_it(heading: str) -> None:
    text = f"{NEWER}{heading}\n\n- Something.\n"

    with pytest.raises(changelog.ChangelogError, match=re.escape(f"unreadable heading '{heading}'")):
        changelog.section(text, "v1.3.0")


@pytest.mark.parametrize(
    ("text", "tag", "version"),
    [
        (SAMPLE, "v9.9.9", "9.9.9"),
        (SAMPLE, "v1.2.1", "1.2.1"),
        ("", "v1.0.0", "1.0.0"),
        ("# Changelog\n\nNothing released yet.\n", "v1.0.0", "1.0.0"),
    ],
)
def test_a_missing_version_is_refused_with_the_remedy(text: str, tag: str, version: str) -> None:
    with pytest.raises(changelog.ChangelogError) as refused:
        changelog.section(text, tag)

    assert f"no section for {version}" in str(refused.value)
    assert f"'## Unreleased' to '## {version} (<YYYY-MM-DD>)'" in str(refused.value)


@pytest.mark.parametrize("heading", ["### 1.2.0 (2026-01-15)", " ## 1.2.0 (2026-01-15)"])
def test_only_a_level_two_heading_at_column_0_starts_a_section(heading: str) -> None:
    with pytest.raises(changelog.ChangelogError, match="no section for 1.2.0"):
        changelog.section(f"{heading}\n\n- A feature.\n", "v1.2.0")


@pytest.mark.parametrize(
    "text",
    [
        "## 1.0.0 (2026-01-01)\n\n## 0.9.0 (2025-12-01)\n\n- x\n",
        "## 1.0.0 (2026-01-01)\n",
        "## 1.0.0 (2026-01-01)\n  \n\t\n\n## 0.9.0 (2025-12-01)\n\n- x\n",
    ],
    ids=["followed-by-a-heading", "at-end-of-file", "whitespace-only"],
)
def test_an_empty_section_is_refused(text: str) -> None:
    with pytest.raises(changelog.ChangelogError, match="the section for 1.0.0 is empty"):
        changelog.section(text, "v1.0.0")


@pytest.mark.parametrize("duplicated", ["1.2.0", "1.1.0"], ids=["the-requested-version", "another-version"])
def test_two_sections_for_one_version_are_refused(duplicated: str) -> None:
    text = SAMPLE + f"\n## {duplicated} (2025-01-01)\n\n- Again.\n"

    with pytest.raises(changelog.ChangelogError, match=re.escape(f"two sections for {duplicated}")):
        changelog.section(text, "v1.2.0")


@pytest.mark.parametrize(
    "tag", ["1.2.0", "v1.2", "v01.2.0", "v1.2.00", "V1.2.0", "v1.2.0+build", "v1.2.0-", "v1.2.0.0", "latest", ""]
)
def test_a_malformed_tag_is_a_usage_error(tag: str) -> None:
    with pytest.raises(ValueError, match="is not vMAJOR.MINOR.PATCH"):
        changelog.section(SAMPLE, tag)


def test_crlf_line_endings_give_the_same_section() -> None:
    assert changelog.section(SAMPLE.replace("\n", "\r\n"), "v1.2.0") == SECTION_1_2_0


# The command, run in-process from a directory holding the changelog.

PROG = "changelog_section.py"


@pytest.fixture
def in_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_changelog(root: Path, text: str) -> None:
    (root / "CHANGELOG.md").write_text(text, encoding="utf-8")


def test_the_command_prints_the_section_and_exits_0(in_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_changelog(in_repo, SAMPLE)

    assert changelog.main([PROG, "v1.2.0"]) == 0
    out, err = capsys.readouterr()
    assert out == SECTION_1_2_0 + "\n"
    assert err == ""


@pytest.mark.parametrize("argv", [[PROG], [PROG, "v1.2.0", "v1.1.0"]], ids=["no-tag", "two-tags"])
def test_the_command_needs_exactly_one_tag(in_repo: Path, capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    write_changelog(in_repo, SAMPLE)

    assert changelog.main(argv) == 64
    out, err = capsys.readouterr()
    assert err == "usage: Tools/changelog_section.py <tag>\n"
    assert out == ""


def test_a_malformed_tag_exits_64(in_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_changelog(in_repo, SAMPLE)

    assert changelog.main([PROG, "latest"]) == 64
    out, err = capsys.readouterr()
    assert err == "changelog_section: tag 'latest' is not vMAJOR.MINOR.PATCH[-prerelease]\n"
    assert out == ""


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        (SAMPLE, "v9.9.9"),
        ("## 1.0.0 (2026-01-01)\n", "v1.0.0"),
        (SAMPLE + "\n## 1.1.0 (2025-01-01)\n\n- Again.\n", "v1.2.0"),
        (SAMPLE + "\n## 1.0.0\n\n- Undated.\n", "v1.2.0"),
    ],
    ids=["missing", "empty", "duplicate", "bad-heading"],
)
def test_a_refused_changelog_exits_1(in_repo: Path, capsys: pytest.CaptureFixture[str], text: str, tag: str) -> None:
    write_changelog(in_repo, text)

    assert changelog.main([PROG, tag]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("changelog_section: CHANGELOG.md")


def test_a_missing_section_names_the_remedy_on_stderr(in_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_changelog(in_repo, SAMPLE)

    assert changelog.main([PROG, "v9.9.9"]) == 1
    assert capsys.readouterr().err == (
        "changelog_section: CHANGELOG.md has no section for 9.9.9. "
        "The release pull request renames '## Unreleased' to '## 9.9.9 (<YYYY-MM-DD>)'.\n"
    )


def test_an_absent_changelog_exits_1(in_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert changelog.main([PROG, "v1.2.0"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("changelog_section: ")
    assert "CHANGELOG.md" in err


@pytest.mark.parametrize(
    ("tag", "code", "stdout"), [("v1.2.0", 0, SECTION_1_2_0 + "\n"), ("v9.9.9", 1, ""), ("nonsense", 64, "")]
)
def test_the_script_as_release_yml_runs_it(tmp_path: Path, tag: str, code: int, stdout: str) -> None:
    write_changelog(tmp_path, SAMPLE)

    result = subprocess.run([sys.executable, str(SCRIPT), tag], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == code, result.stderr
    assert result.stdout == stdout
