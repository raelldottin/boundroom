#!/usr/bin/env python3
"""Print a release's section of CHANGELOG.md, or refuse when it has none (SPEC-009 RA-ENT-006).

Usage: Tools/changelog_section.py <tag>, run from the repository root. The tag is
``vMAJOR.MINOR.PATCH`` with an optional ``-prerelease``. The section is the text under the
heading ``## <version> (<YYYY-MM-DD>)``, up to the next ``## `` heading. A prerelease needs a
section of its own. release.yml publishes what this prints as the release notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")
TAG = re.compile(r"v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?)")
HEADING = re.compile(r"^## (?P<version>\S+) \((?P<date>\d{4}-\d{2}-\d{2})\)$")
UNRELEASED = "## Unreleased"
EX_USAGE = 64


class ChangelogError(Exception):
    pass


def sections(text: str) -> dict[str, tuple[str, str]]:
    """Each released version mapped to its date and its text, in file order."""
    found: dict[str, tuple[str, str]] = {}
    version = None
    date = ""
    body: list[str] = []

    def close() -> None:
        if version is not None:
            found[version] = (date, "\n".join(body).strip())

    for line in text.splitlines():
        if line.startswith("## "):
            close()
            heading = HEADING.match(line)
            if heading:
                version, date = heading.group("version"), heading.group("date")
                if version in found:
                    raise ChangelogError(f"{CHANGELOG} has two sections for {version}")
            elif line.strip() == UNRELEASED:
                version = None
            else:
                raise ChangelogError(f"{CHANGELOG}: unreadable heading {line!r}; use '## <version> (<YYYY-MM-DD>)'")
            body = []
        elif version is not None:
            body.append(line)
    close()
    return found


def section(text: str, tag: str) -> str:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"tag {tag!r} is not vMAJOR.MINOR.PATCH[-prerelease]")
    version = match.group(1)
    released = sections(text)
    if version not in released:
        raise ChangelogError(
            f"{CHANGELOG} has no section for {version}. The release pull request renames "
            f"'{UNRELEASED}' to '## {version} (<YYYY-MM-DD>)'."
        )
    body = released[version][1]
    if not body:
        raise ChangelogError(f"{CHANGELOG}: the section for {version} is empty")
    return body


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: Tools/changelog_section.py <tag>", file=sys.stderr)
        return EX_USAGE
    try:
        print(section(CHANGELOG.read_text(encoding="utf-8"), argv[1]))
    except ValueError as error:
        print(f"changelog_section: {error}", file=sys.stderr)
        return EX_USAGE
    except (ChangelogError, OSError) as error:
        print(f"changelog_section: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
