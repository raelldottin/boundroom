#!/usr/bin/env python3
"""Write the release's CycloneDX SBOM from uv.lock, or refuse one that is wrong (SPEC-009 RA-ENT-004).

Usage: Tools/release_sbom.py <output> <tag>, run from the repository root with uv on PATH. The
SBOM covers what installing Boundroom brings in: its dependencies and every extra, at their
locked versions, without the dev group, whose tools the archive doesn't ship. It is written by
``uv export --format cyclonedx1.5``. uv marks that format as a preview, so the packages it lists
must match exactly the ones uv's established requirements export lists for the same selection,
and the project's own version must be the tag's. release.yml publishes the file and attests it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# The same selection for both exports: every extra, no dependency group, the lock as it is.
SELECTION = ("--frozen", "--all-extras", "--no-dev", "--no-progress")
EXPORT = ("uv", "export", "--format", "cyclonedx1.5", "--preview-features", "sbom-export", *SELECTION)
REQUIREMENTS = ("uv", "export", "--format", "requirements.txt", "--no-hashes", "--no-header", "--no-annotate", *SELECTION)
TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-[0-9A-Za-z.-]+)?")
PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
EX_USAGE = 64


class SbomError(Exception):
    pass


def required(requirements: str) -> set[tuple[str, str]]:
    """Every package the requirements export pins, as (name, version)."""
    found = set()
    for line in requirements.splitlines():
        match = PINNED.match(line.strip())
        if match:
            found.add((match.group(1).lower(), match.group(2)))
    return found


def listed(sbom: dict[str, Any]) -> set[tuple[str, str]]:
    """Every package the SBOM lists besides the project itself, as (name, version)."""
    return {(component.get("name", "").lower(), component.get("version", "")) for component in sbom.get("components", [])}


def check(sbom: dict[str, Any], requirements: str, tag: str) -> None:
    if sbom.get("bomFormat") != "CycloneDX":
        raise SbomError("uv export didn't write a CycloneDX document")
    release = TAG.fullmatch(tag)
    if not release:
        raise SbomError(f"{tag} isn't a vMAJOR.MINOR.PATCH tag")
    root = sbom.get("metadata", {}).get("component", {})
    if root.get("name") != "boundroom" or not str(root.get("version", "")).startswith(release.group(1)):
        raise SbomError(
            f"the SBOM describes {root.get('name')} {root.get('version')}, not boundroom {release.group(1)}: bump pyproject.toml"
        )
    expected, actual = required(requirements), listed(sbom)
    if not expected:
        raise SbomError("uv's requirements export lists no packages")
    if missing := expected - actual:
        raise SbomError("the SBOM misses packages: " + ", ".join(f"{name} {version}" for name, version in sorted(missing)))
    if extra := actual - expected:
        raise SbomError(
            "the SBOM lists packages an install doesn't bring in: "
            + ", ".join(f"{name} {version}" for name, version in sorted(extra))
        )


def run(command: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise SbomError(f"can't run uv: {error}") from error
    if result.returncode != 0:
        raise SbomError(f"{' '.join(command[:4])} failed: {result.stderr.strip()}")
    return result.stdout


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: Tools/release_sbom.py <output> <tag>", file=sys.stderr)
        return EX_USAGE
    output, tag = Path(argv[0]), argv[1]
    try:
        try:
            sbom = json.loads(run(EXPORT))
        except ValueError as error:
            raise SbomError(f"uv export didn't write JSON: {error}") from error
        check(sbom, run(REQUIREMENTS), tag)
        output.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    except (OSError, SbomError) as error:
        print(f"release_sbom: {error}", file=sys.stderr)
        return 1
    print(f"wrote {output}: boundroom and {len(listed(sbom))} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
