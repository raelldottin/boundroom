"""The consumer's sync pin: the lock that names the Boundroom commit its synced copy came from.

Boundroom itself keeps no lock. ``operator_status`` reports the pin, and ``telemetry`` labels a
consumer's spans with its commit, because a synced copy has no plugin manifest to give a version.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The consumer's lock, then the name a consumer that adopted before 0.3.0 keeps (SPEC-005).
LOCK_FILES = ("automation/boundroom.lock", "automation/repo-automation.lock")


def parse_lock(text: str) -> dict[str, Any]:
    """The lock's fields: a JSON object, or ``key = value`` / ``key: value`` lines."""
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict):
        return document
    fields: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        separator = min((index for index in (line.find("="), line.find(":")) if index > 0), default=-1)
        if separator > 0:
            fields[line[:separator].strip()] = line[separator + 1 :].strip().strip("\"'")
    return fields


def pinned_commit(repo_root: Path) -> str:
    """The full commit SHA the first lock found names, or "" when there is none or it names none.

    The sync pins a full SHA, so anything else in ``commit`` is a hand edit and isn't reported.
    """
    for name in LOCK_FILES:
        path = repo_root / name
        if not path.is_file():
            continue
        try:
            value = parse_lock(path.read_text(encoding="utf-8")).get("commit")
        except (OSError, UnicodeDecodeError):
            return ""
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else ""
    return ""
