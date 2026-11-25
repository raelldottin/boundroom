"""The offline wheelhouse for Track R v2: every package a session, the oracle or the supervisor installs.

PyPI is reached once, here, outside any sandbox, to download exact pins into a directory outside
the repository. Their names, versions and sha256 hashes are frozen in ``wheelhouse.v2.json``. After
that, every install is offline (``uv pip install --offline --no-index --find-links``) with
``--require-hashes``, so a wheel that doesn't match the frozen hash is refused.

Two sets are installed:

- **target**, into each python-semver clone's ``.venv``: what semver's own test configuration needs
  (``.pytest.ini`` adds ``--cov``, and ``tests/conftest.py`` imports ``packaging``). The versions are
  the ones semver's ``uv.lock`` pins at the tasks' bases.
- **supervisor**, into one interpreter outside every clone: Boundroom v0.5.3's runtime dependency,
  pydantic 2, at the versions Boundroom's ``uv.lock`` pins at b193297.

    python -m automation.benchmark.trackr.wheelhouse build --dest /outside/the/repo/wheelhouse
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .common import TrackRError, read_json, require_outside_repository, write_json

LOCK_FILE = Path(__file__).with_name("wheelhouse.v2.json")
PYTHON = "3.12"
PLATFORM = "macosx_11_0_arm64"
PINS: Mapping[str, tuple[str, ...]] = {
    # python-semver's uv.lock at the tasks' bases (49a14e0 and earlier); 3.12 needs neither
    # exceptiongroup nor tomli.
    "target": (
        "pytest==9.1.1",
        "pytest-cov==7.1.0",
        "coverage==7.14.3",
        "packaging==26.3",
        "pluggy==1.6.0",
        "iniconfig==2.3.0",
        "pygments==2.20.0",
    ),
    # Boundroom's uv.lock at b193297 (v0.5.3).
    "supervisor": (
        "pydantic==2.14.3",
        "pydantic-core==2.50.4",
        "typing-extensions==4.16.0",
        "annotated-types==0.8.0",
        "typing-inspection==0.4.4",
    ),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(dest: Path) -> dict:
    """Download every pin as a wheel for CPython 3.12 on macOS arm64. Needs the network, once."""
    require_outside_repository(dest, "the wheelhouse")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trackr-wheelhouse-") as scratch:
        venv = Path(scratch) / "venv"
        subprocess.run(["uv", "venv", "--quiet", "--seed", "--python", PYTHON, str(venv)], check=True)
        pins = [pin for group in PINS.values() for pin in group]
        subprocess.run(
            [str(venv / "bin" / "python"), "-m", "pip", "download", "--quiet", "--no-deps", "--only-binary=:all:"]
            + ["--python-version", PYTHON, "--platform", PLATFORM, "--implementation", "cp", "--dest", str(dest), *pins],
            check=True,
        )
    lock: dict = {"python": PYTHON, "platform": PLATFORM, "source": "https://pypi.org/simple", "groups": {}}
    files = {path.name: path for path in dest.glob("*.whl")}
    for group, pins in PINS.items():
        entries = []
        for pin in pins:
            name, version = pin.split("==")
            stem = name.replace("-", "_").lower()
            matches = [file for file in files if file.lower().startswith(f"{stem}-{version}-")]
            if len(matches) != 1:
                raise TrackRError(f"expected one wheel for {pin} in {dest}, found {matches}")
            entries.append({"name": name, "version": version, "file": matches[0], "sha256": sha256(files[matches[0]])})
        lock["groups"][group] = entries
    return lock


def requirements(group: str, lock: Optional[Mapping] = None) -> str:
    """A ``--require-hashes`` requirements file for one group, from the frozen lock."""
    lock = lock or read_json(LOCK_FILE)
    return "".join(f"{entry['name']}=={entry['version']} --hash=sha256:{entry['sha256']}\n" for entry in lock["groups"][group])


def verify(wheelhouse: Path, lock: Optional[Mapping] = None) -> list[str]:
    """Wheels missing from ``wheelhouse`` or whose hash differs from the lock."""
    lock = lock or read_json(LOCK_FILE)
    problems = []
    for entry in (entry for group in lock["groups"].values() for entry in group):
        path = wheelhouse / entry["file"]
        if not path.exists():
            problems.append(f"missing {entry['file']}")
        elif sha256(path) != entry["sha256"]:
            problems.append(f"hash mismatch {entry['file']}")
    return problems


def install_argv(python: Path, wheelhouse: Path, requirements_file: Path) -> list[str]:
    return [
        "uv",
        "pip",
        "install",
        "--quiet",
        "--python",
        str(python),
        "--offline",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--require-hashes",
        "-r",
        str(requirements_file),
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build or check the Track R v2 offline wheelhouse. Starts no model.")
    sub = parser.add_subparsers(dest="command", required=True)
    built = sub.add_parser("build", help="Download the pins (needs the network) and freeze them in wheelhouse.v2.json.")
    built.add_argument("--dest", type=Path, required=True)
    checked = sub.add_parser("verify", help="Check a wheelhouse against wheelhouse.v2.json.")
    checked.add_argument("--wheelhouse", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        lock = build(args.dest.resolve())
        write_json(LOCK_FILE, lock)
        print(json.dumps(lock, indent=2))
        return 0
    problems = verify(args.wheelhouse.resolve())
    print("\n".join(problems) or "wheelhouse matches wheelhouse.v2.json")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
