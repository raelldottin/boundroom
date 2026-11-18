#!/usr/bin/env python3
"""Write the release's CycloneDX SBOM from uv.lock, or refuse when it misses a package (SPEC-009 RA-ENT-004).

Usage: Tools/release_sbom.py <output>, run from the repository root with uv on PATH. It runs
``uv export --format cyclonedx1.5`` over the locked versions of every extra and dependency group,
then checks that the SBOM names every package in uv.lock at its locked version, because uv marks
that format as a preview. release.yml publishes the file and attests it against the archive.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

LOCK = Path("uv.lock")
EXPORT = (
    "uv",
    "export",
    "--format",
    "cyclonedx1.5",
    "--preview-features",
    "sbom-export",
    "--frozen",
    "--all-extras",
    "--all-groups",
    "--no-progress",
)
EX_USAGE = 64


class SbomError(Exception):
    pass


def locked(lock_text: str) -> set[tuple[str, str]]:
    """Every package in the lock, as (name, version)."""
    return {(package["name"], package.get("version", "")) for package in tomllib.loads(lock_text).get("package", [])}


def listed(sbom: dict[str, Any]) -> set[tuple[str, str]]:
    """Every package the SBOM names, the project itself included, as (name, version)."""
    root = sbom.get("metadata", {}).get("component")
    components = [root, *sbom.get("components", [])] if root else sbom.get("components", [])
    return {(component.get("name", ""), component.get("version", "")) for component in components}


def check(sbom: dict[str, Any], lock_text: str) -> None:
    if sbom.get("bomFormat") != "CycloneDX":
        raise SbomError("uv export didn't write a CycloneDX document")
    missing = locked(lock_text) - listed(sbom)
    if missing:
        names = ", ".join(f"{name} {version}" for name, version in sorted(missing))
        raise SbomError(f"the SBOM misses locked packages: {names}")


def export() -> dict[str, Any]:
    try:
        result = subprocess.run(EXPORT, capture_output=True, text=True, check=False)
    except OSError as error:
        raise SbomError(f"can't run uv: {error}") from error
    if result.returncode != 0:
        raise SbomError(f"uv export failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise SbomError(f"uv export didn't write JSON: {error}") from error


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: Tools/release_sbom.py <output>", file=sys.stderr)
        return EX_USAGE
    output = Path(argv[0])
    try:
        sbom = export()
        check(sbom, LOCK.read_text(encoding="utf-8"))
        output.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError, SbomError) as error:
        print(f"release_sbom: {error}", file=sys.stderr)
        return 1
    print(f"wrote {output}: {len(listed(sbom))} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
