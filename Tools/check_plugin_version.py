#!/usr/bin/env python3
"""Refuse a release tag whose version the plugin manifests or pyproject.toml don't declare (SPEC-002).

Usage: Tools/check_plugin_version.py <tag>, run from the repository root. Every required
manifest must exist and declare the tag's version without its leading ``v``. pyproject.toml's
project version must be the tag's too; for a prerelease tag, the PEP 440 form (``0.5.0rc1`` for
``v0.5.0-rc1``) also counts. It reads pyproject.toml without tomllib, so a person can run it with
any python3 before tagging (SPEC-009 RA-ENT-004).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLUGIN = Path("plugins/boundroom")
MANIFESTS = ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
PYPROJECT = Path("pyproject.toml")
PROJECT_VERSION = re.compile(r'^\[project\]\s*$(?:(?!^\[).)*?^version\s*=\s*"([^"]+)"', re.MULTILINE | re.DOTALL)
# Tools/changelog_section.py's tag, with the prerelease part captured apart.
TAG = re.compile(r"v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(-[0-9A-Za-z.-]+)?")
# What PEP 440 makes of a semver prerelease such as -rc1, -rc.1 or -beta.2.
PEP440_PRERELEASE = re.compile(r"[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)[0-9A-Za-z._-]*")
EX_USAGE = 64


def is_tag_version(version: str, base: str, prerelease: str | None) -> bool:
    """A final tag needs exactly its version; a prerelease tag also allows a PEP 440 prerelease of it."""
    if version == base:
        return True
    return bool(prerelease) and version.startswith(base) and bool(PEP440_PRERELEASE.fullmatch(version[len(base) :]))


def problems(root: Path, tag: str) -> list[str]:
    """One line per manifest that is missing, unreadable or at another version."""
    expected = tag.removeprefix("v")
    found: list[str] = []
    for name in MANIFESTS:
        relative = PLUGIN / name
        try:
            version = json.loads((root / relative).read_text(encoding="utf-8"))["version"]
        except FileNotFoundError:
            found.append(f"{relative}: missing")
            continue
        except (ValueError, KeyError, TypeError):
            found.append(f"{relative}: no readable version")
            continue
        if version != expected:
            found.append(f"{relative}: declares {version!r}, but tag {tag} needs {expected!r}")
    return found


def project_problems(root: Path, tag: str) -> list[str]:
    """One line if pyproject.toml's project version is missing or isn't the tag's."""
    found: list[str] = []
    release = TAG.fullmatch(tag)
    try:
        project = PROJECT_VERSION.search((root / PYPROJECT).read_text(encoding="utf-8"))
    except OSError:
        project = None
    if not project:
        found.append(f"{PYPROJECT}: no readable [project] version")
    elif release and not is_tag_version(project.group(1), *release.groups()):
        found.append(
            f"{PYPROJECT}: declares {project.group(1)!r}, but tag {tag} needs {release.group(1)!r}; run uv lock after bumping it"
        )
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: Tools/check_plugin_version.py <tag>", file=sys.stderr)
        return EX_USAGE
    found = problems(Path.cwd(), argv[1]) + project_problems(Path.cwd(), argv[1])
    for line in found:
        print(f"::error::{line}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
