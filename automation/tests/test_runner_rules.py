"""The runner prompt is generic, and a repository adds its own rules (SPEC-009 RA-ENT-001)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from automation.context.build_context import build_context_bundle
from automation.supervisor import policy
from automation.supervisor.run_next import (
    RUNNER_RULES_MAX_BYTES,
    RUNNER_RULES_PATH,
    render_prompt,
    repository_rules,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_QUEUE = REPO_ROOT / "automation/examples/example-slices.json"

# Repositories known to consume Boundroom. A shipped prompt must name none of them, or
# its paths: every consumer's runner gets the shipped prompt.
CONSUMER_NAMES = ("owlory",)


def manifest_entries() -> list[dict]:
    manifest = json.loads((REPO_ROOT / "automation/reusable-manifest.json").read_text(encoding="utf-8"))
    return manifest["entries"]


def shipped_prompt_files() -> list[Path]:
    files: list[Path] = []
    for entry in manifest_entries():
        if not entry["source"].startswith("automation/prompts"):
            continue
        source = REPO_ROOT / entry["source"]
        files.extend(sorted(source.rglob("*.md")) if entry["kind"] == "directory" else [source])
    return files


class ShippedPromptTests(unittest.TestCase):
    def test_the_manifest_ships_the_prompts(self) -> None:
        names = {path.name for path in shipped_prompt_files()}
        self.assertTrue({"base.md", "slice.md"} <= names, names)

    def test_no_shipped_prompt_names_a_consumer(self) -> None:
        for path in shipped_prompt_files():
            text = path.read_text(encoding="utf-8").lower()
            for name in CONSUMER_NAMES:
                with self.subTest(prompt=path.relative_to(REPO_ROOT).as_posix(), consumer=name):
                    self.assertNotIn(name, text)

    def test_no_manifest_entry_writes_the_runner_rules(self) -> None:
        for entry in manifest_entries():
            for key in ("source", "destination"):
                value = entry[key].rstrip("/")
                with self.subTest(entry=value, field=key):
                    self.assertNotEqual(RUNNER_RULES_PATH, value)
                    self.assertFalse(RUNNER_RULES_PATH.startswith(value + "/"))


class RepositoryRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        prompts = self.root / "automation/prompts"
        prompts.mkdir(parents=True)
        for name in ("base.md", "slice.md"):
            shutil.copy(REPO_ROOT / "automation/prompts" / name, prompts / name)
        self.rules = self.root / RUNNER_RULES_PATH

    def render(self) -> str:
        queue_data = policy.load_queue(EXAMPLE_QUEUE)
        slice_record = queue_data["slices"][0]
        handoff_dir = Path(self.temp.name) / "handoffs"
        handoff_dir.mkdir(exist_ok=True)
        bundle = build_context_bundle(
            repo_root=REPO_ROOT,
            queue_path=EXAMPLE_QUEUE,
            handoff_dir=handoff_dir,
            slice_id=slice_record["slice_id"],
            max_doc_chars=1200,
        )
        return render_prompt(
            repo_root=self.root, slice_record=slice_record, context_bundle=bundle, handoff_path=handoff_dir / "h.json"
        )

    def test_without_a_rules_file_the_prompt_has_no_repository_section(self) -> None:
        prompt = self.render()
        self.assertTrue(prompt.startswith("# Boundroom Supervised Slice Run\n"))
        self.assertNotIn("## Repository Rules", prompt)

    def test_the_rules_sit_between_the_base_prompt_and_the_slice(self) -> None:
        self.rules.write_text("- Domain code lives in `app/Core/Domain/`.\n", encoding="utf-8")
        prompt = self.render()
        base_end = prompt.index("## Prohibited Behavior")
        rules_at = prompt.index("## Repository Rules")
        self.assertLess(base_end, rules_at)
        self.assertLess(rules_at, prompt.index("- Domain code lives in `app/Core/Domain/`."))
        slice_text = (REPO_ROOT / "automation/prompts/slice.md").read_text(encoding="utf-8").strip().splitlines()[0]
        self.assertLess(rules_at, prompt.index(slice_text))

    def test_the_rules_are_inserted_verbatim(self) -> None:
        self.rules.write_text("Keep the __SLICE_ID__ marker as written.\n", encoding="utf-8")
        self.assertIn("Keep the __SLICE_ID__ marker as written.", self.render())

    def test_an_empty_rules_file_adds_nothing(self) -> None:
        self.rules.write_text("  \n\n", encoding="utf-8")
        self.assertEqual("", repository_rules(self.root))

    def test_a_rules_file_over_the_limit_is_refused(self) -> None:
        self.rules.write_bytes(b"x" * (RUNNER_RULES_MAX_BYTES + 1))
        with self.assertRaises(policy.ConfigError) as raised:
            repository_rules(self.root)
        self.assertTrue(str(raised.exception).startswith("runner_rules_too_large: "))

    def test_a_rules_file_at_the_limit_is_read(self) -> None:
        self.rules.write_bytes(b"x" * RUNNER_RULES_MAX_BYTES)
        self.assertTrue(repository_rules(self.root).endswith("x" * 100))

    def test_a_symlinked_rules_file_is_refused(self) -> None:
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        os.symlink(outside, self.rules)
        with self.assertRaises(policy.ConfigError) as raised:
            repository_rules(self.root)
        self.assertTrue(str(raised.exception).startswith("runner_rules_symlink: "))

    def test_a_rules_directory_is_refused(self) -> None:
        self.rules.mkdir()
        with self.assertRaises(policy.ConfigError) as raised:
            repository_rules(self.root)
        self.assertTrue(str(raised.exception).startswith("runner_rules_not_file: "))

    def test_a_rules_file_that_is_not_utf8_is_refused(self) -> None:
        self.rules.write_bytes(b"\xff\xfe rules")
        with self.assertRaises(policy.ConfigError) as raised:
            repository_rules(self.root)
        self.assertTrue(str(raised.exception).startswith("runner_rules_not_utf8: "))


if __name__ == "__main__":
    unittest.main()
