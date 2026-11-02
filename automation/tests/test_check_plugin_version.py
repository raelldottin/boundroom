"""The release gate refuses a tag the plugin manifests don't declare (SPEC-002 step 5)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "Tools/check_plugin_version.py"
spec = importlib.util.spec_from_file_location("check_plugin_version", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def plugin(tmp_path):
    """A copy of the real manifests, so each test can break one."""
    for name in gate.MANIFESTS:
        target = tmp_path / gate.PLUGIN / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / gate.PLUGIN / name, target)
    return tmp_path


def set_version(root: Path, name: str, version: str) -> None:
    path = root / gate.PLUGIN / name
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "version": version}), encoding="utf-8")


def test_the_real_manifests_pass_for_their_own_version():
    version = json.loads((REPO_ROOT / gate.PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]

    assert gate.problems(REPO_ROOT, f"v{version}") == []


def test_a_newer_tag_over_older_manifests_is_refused(plugin):
    for name in gate.MANIFESTS:
        set_version(plugin, name, "0.1.0")

    found = gate.problems(plugin, "v0.2.0")

    assert len(found) == 3 and all("declares '0.1.0', but tag v0.2.0 needs '0.2.0'" in line for line in found)


def test_manifests_that_disagree_are_refused_whatever_the_tag(plugin):
    set_version(plugin, gate.MANIFESTS[0], "0.2.0")
    set_version(plugin, gate.MANIFESTS[1], "0.2.0")
    set_version(plugin, gate.MANIFESTS[2], "0.3.0")

    assert gate.problems(plugin, "v0.2.0") == [
        f"{gate.PLUGIN / gate.MANIFESTS[2]}: declares '0.3.0', but tag v0.2.0 needs '0.2.0'"
    ]
    assert len(gate.problems(plugin, "v0.3.0")) == 2


@pytest.mark.parametrize("name", gate.MANIFESTS)
def test_a_missing_manifest_is_refused_even_when_the_rest_match(plugin, name):
    for each in gate.MANIFESTS:
        set_version(plugin, each, "0.2.0")
    (plugin / gate.PLUGIN / name).unlink()

    assert gate.problems(plugin, "v0.2.0") == [f"{gate.PLUGIN / name}: missing"]


def test_an_unreadable_manifest_is_refused(plugin):
    (plugin / gate.PLUGIN / gate.MANIFESTS[0]).write_text("{not json", encoding="utf-8")

    assert gate.problems(plugin, "v0.2.0")[0] == f"{gate.PLUGIN / gate.MANIFESTS[0]}: no readable version"


def test_a_prerelease_tag_must_match_exactly(plugin):
    for name in gate.MANIFESTS:
        set_version(plugin, name, "0.2.0-rc.1")

    assert gate.problems(plugin, "v0.2.0-rc.1") == []
    assert len(gate.problems(plugin, "v0.2.0")) == 3


def test_the_cli_exits_nonzero_and_names_each_problem(plugin):
    set_version(plugin, gate.MANIFESTS[1], "9.9.9")

    result = subprocess.run([sys.executable, str(SCRIPT), "v0.2.0"], cwd=plugin, capture_output=True, text=True)

    assert result.returncode == 1
    assert result.stdout.startswith(f"::error::{gate.PLUGIN / gate.MANIFESTS[1]}: declares '9.9.9'")


def test_the_cli_requires_exactly_one_tag():
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True)

    assert result.returncode == gate.EX_USAGE
