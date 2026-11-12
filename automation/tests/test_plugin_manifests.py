"""The SPEC-002 plugin's manifests agree, and every skill satisfies all three harnesses.

The release gate that compares these versions with a tag is a separate, later step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "plugins/boundroom"
MANIFESTS = tuple(f"{PLUGIN}/{name}" for name in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"))
VERSION = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
# The intersection of Agent Plugins v1 (Hermes), Claude Code and Codex skill rules.
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SKILL_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SKILLS = sorted((REPO_ROOT / PLUGIN / "skills").glob("*/SKILL.md"))


def load(relative: str) -> dict:
    return json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))


def frontmatter(path: Path) -> dict[str, str]:
    # ponytail: flat `key: value` lines only; a nested YAML field would need a real parser.
    head = path.read_text(encoding="utf-8").split("---\n")
    assert head[0] == "" and len(head) >= 3, f"{path} has no frontmatter"
    return dict(line.split(": ", 1) for line in head[1].splitlines() if line.strip())


def test_every_manifest_agrees_on_identity_and_license():
    manifests = [load(path) for path in MANIFESTS]

    assert {(m["name"], m["version"], m["license"]) for m in manifests} == {
        ("boundroom", manifests[0]["version"], "GPL-2.0-only")
    }
    assert VERSION.fullmatch(manifests[0]["version"])
    assert manifests[0]["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert manifests[2]["skills"] == "./skills/"


def test_both_marketplaces_list_the_plugin_directory():
    version = load(MANIFESTS[0])["version"]
    claude = load(".claude-plugin/marketplace.json")["plugins"]
    codex = load(".agents/plugins/marketplace.json")["plugins"]

    assert [(p["name"], p["source"], p["version"]) for p in claude] == [("boundroom", f"./{PLUGIN}", version)]
    assert [(p["name"], p["source"]) for p in codex] == [("boundroom", {"source": "local", "path": f"./{PLUGIN}"})]


def test_the_five_skills_ship():
    assert [path.parent.name for path in SKILLS] == [
        "boundroom-evaluate-handoff",
        "boundroom-run-next",
        "boundroom-settings",
        "boundroom-status",
        "boundroom-sync",
    ]


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_skill_frontmatter_satisfies_every_harness(skill):
    fields = frontmatter(skill)

    assert set(fields) <= SKILL_KEYS
    assert fields["name"] == skill.parent.name and len(fields["name"]) <= 64 and SKILL_NAME.fullmatch(fields["name"])
    assert 1 <= len(fields["description"]) <= 1024
    assert fields["license"] == "GPL-2.0-only"
