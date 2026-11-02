#!/usr/bin/env python3
"""Refuse a release tag whose version the plugin manifests don't declare (SPEC-002).

Usage: Tools/check_plugin_version.py <tag>, run from the repository root. Every required
manifest must exist and declare the tag's version without its leading ``v``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN = Path("plugins/repo-automation")
MANIFESTS = ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
EX_USAGE = 64


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


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: Tools/check_plugin_version.py <tag>", file=sys.stderr)
        return EX_USAGE
    found = problems(Path.cwd(), argv[1])
    for line in found:
        print(f"::error::{line}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
