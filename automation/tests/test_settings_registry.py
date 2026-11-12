"""The settings registry is complete and matches today's behavior (SPEC-008 PS-1 to PS-4, PS-10).

Each test compares the registry with the code it describes, so a new flag, variable or policy field
can't appear without a registry entry, and the registry can't claim a default the code doesn't use.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import unittest
from pathlib import Path

from pydantic import BaseModel

from automation.context.memory import MemoryConfig
from automation.schemas.models import SliceQueue
from automation.schemas.models import RepositoryTelemetry
from automation.supervisor import evaluate_handoff, kanban_projection, operator_status, run_next, settings, telemetry_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_AGENT = REPO_ROOT / "automation/supervisor/run_agent.sh"
SYNC_TOOL = REPO_ROOT / "Tools/boundroom-sync.sh"
ENV_NAME = re.compile(r"\b(?:BOUNDROOM|REPO_AUTOMATION|OWLORY)_[A-Z0-9_]*[A-Z0-9]\b")
# Flags in skill text that belong to other tools, not to Boundroom's entry points. The settings
# view's own flags choose a view; they aren't settings.
OTHER_TOOLS_FLAGS = {"--help", "--match-head-commit", "--all", "--json", "--explain"}


def knowledge_service(path: Path) -> bool:
    """The knowledge service is deployed separately with its own configuration (SPEC-007), and its
    BOUNDROOM_KNOWLEDGE_* names are secret references the operator chooses, not supervisor settings.
    """
    relative = path.relative_to(REPO_ROOT).as_posix()
    return relative.startswith("automation/knowledge/") or path.name.startswith("test_knowledge_")


def parser_options(parser: argparse.ArgumentParser) -> set[str]:
    return {option for action in parser._actions for option in action.option_strings if option not in ("-h", "--help")}


def sync_options() -> set[str]:
    result = subprocess.run([str(SYNC_TOOL), "--help"], capture_output=True, text=True, check=True)
    return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", result.stdout)) - {"--help"}


class RegistryIntegrityTests(unittest.TestCase):
    def test_ids_variables_and_options_are_unique(self) -> None:
        ids = [setting.id for setting in settings.SETTINGS]
        self.assertEqual(len(ids), len(set(ids)))
        names = [name for setting in settings.SETTINGS for name in setting.env_names]
        self.assertEqual(len(names), len(set(names)), "a variable maps to two settings")
        for entry_point in settings.ENTRY_POINTS:
            spellings = [
                option
                for setting in settings.SETTINGS
                for flag in setting.flags
                if flag.entry_point == entry_point
                for option in flag.options
            ]
            self.assertEqual(len(spellings), len(set(spellings)), entry_point)

    def test_every_entry_is_well_formed(self) -> None:
        for setting in settings.SETTINGS:
            with self.subTest(setting=setting.id):
                self.assertIn(setting.klass, settings.CLASSES)
                self.assertIn(setting.repository_rule, settings.REPOSITORY_RULES)
                self.assertTrue(setting.description.strip())
                self.assertTrue(setting.flags or setting.env or setting.repository_fields, "a setting with no source")
                self.assertEqual(setting.repository_rule != "never", bool(setting.repository_fields))
                if setting.choices and setting.default not in (None, settings.COMPUTED):
                    self.assertIn(setting.default, setting.choices)
                if setting.only_when:
                    self.assertIn(setting.only_when[0], settings.BY_ID)


class CompletenessTests(unittest.TestCase):
    def test_ps1_every_flag_of_every_entry_point_is_registered(self) -> None:
        parsers: dict[settings.EntryPoint, set[str]] = {
            "run_next": parser_options(run_next.build_parser()),
            "evaluate_handoff": parser_options(evaluate_handoff.build_parser()),
            "kanban_projection": parser_options(kanban_projection.build_parser()),
            "operator_status": parser_options(operator_status.build_parser()),
            "sync": sync_options(),
        }
        for entry_point, options in parsers.items():
            with self.subTest(entry_point=entry_point):
                self.assertEqual(options, set(settings.flags_for(entry_point)))

    def test_ps2_every_variable_the_code_reads_is_registered(self) -> None:
        found: set[str] = set()
        for root in (REPO_ROOT / "automation", REPO_ROOT / "Tools"):
            for path in root.rglob("*"):
                if path.suffix in {".py", ".sh"} and path.is_file() and not knowledge_service(path):
                    found |= set(ENV_NAME.findall(path.read_text(encoding="utf-8")))
        # run_agent.sh builds the legacy names from a list of suffixes, so they aren't written out.
        suffixes = re.search(r"for legacy_suffix in (.*?); do", RUN_AGENT.read_text(encoding="utf-8"), re.S)
        assert suffixes is not None
        aliased = suffixes.group(1).replace("\\\n", " ").split()
        found |= {f"REPO_AUTOMATION_{suffix}" for suffix in aliased}
        self.assertEqual(set(), found - set(settings.env_names()), "variables with no registry entry")
        for suffix in aliased:
            with self.subTest(suffix=suffix):
                setting = settings.env_names()[f"BOUNDROOM_{suffix}"]
                self.assertIn(f"REPO_AUTOMATION_{suffix}", setting.legacy_env)

    def test_ps3_every_policy_overlay_and_memory_field_is_registered_or_explained(self) -> None:
        def fields(model: type[BaseModel], prefix: str) -> set[str]:
            return {f"{prefix}.{name}" for name in model.model_fields}

        known = set(settings.repository_fields()) | set(settings.NOT_SETTINGS)
        policy_model = SliceQueue.model_fields["policy"].annotation
        assert isinstance(policy_model, type) and issubclass(policy_model, BaseModel)
        budgets_model = MemoryConfig.model_fields["budgets"].annotation
        assert isinstance(budgets_model, type) and issubclass(budgets_model, BaseModel)
        expected = (
            fields(SliceQueue, "queue") - {"queue.policy"}
            | fields(policy_model, "queue.policy")
            | fields(kanban_projection.KanbanOverlay, "overlay")
            | fields(policy_model, "overlay.policy")
            | fields(RepositoryTelemetry, "queue.policy.telemetry")
            | fields(RepositoryTelemetry, "overlay.policy.telemetry")
            | fields(MemoryConfig, "memory")
            | fields(budgets_model, "memory.budgets")
        )
        self.assertEqual(set(), expected - known, "fields with no registry entry")
        self.assertEqual(set(), known - expected, "registry entries for fields that don't exist")

    def test_every_operator_file_key_is_registered(self) -> None:
        registered = {setting.operator_file for setting in settings.SETTINGS if setting.operator_file}
        expected = {f"telemetry.json:{name}" for name in telemetry_settings.OperatorTelemetry.model_fields}
        self.assertEqual(expected, registered)

    def test_ps4_every_flag_a_skill_mentions_is_registered(self) -> None:
        registered = {option for entry_point in settings.ENTRY_POINTS for option in settings.flags_for(entry_point)}
        for skill in sorted((REPO_ROOT / "plugins/boundroom/skills").glob("*/SKILL.md")):
            mentioned = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", skill.read_text(encoding="utf-8")))
            with self.subTest(skill=skill.parent.name):
                self.assertEqual(set(), mentioned - registered - OTHER_TOOLS_FLAGS)


class DefaultsTests(unittest.TestCase):
    """PS-10: the registry's defaults are the ones the code uses today."""

    def test_flag_defaults_match_the_parsers(self) -> None:
        parsers: dict[settings.EntryPoint, argparse.ArgumentParser] = {
            "run_next": run_next.build_parser(),
            "evaluate_handoff": evaluate_handoff.build_parser(),
            "kanban_projection": kanban_projection.build_parser(),
            "operator_status": operator_status.build_parser(),
        }
        for entry_point, parser in parsers.items():
            by_option = settings.flags_for(entry_point)
            for action in parser._actions:
                if not action.option_strings or action.dest == "help":
                    continue
                setting = by_option[action.option_strings[0]]
                with self.subTest(entry_point=entry_point, setting=setting.id):
                    if setting.repository_fields:
                        # The repository supplies the value when the flag is absent; the flag has no default.
                        self.assertIsNone(action.default)
                    elif action.default is not None:
                        self.assertEqual(setting.default, action.default)
                    else:
                        self.assertIsNone(setting.default)

    def test_variable_defaults_match_run_agent(self) -> None:
        text = RUN_AGENT.read_text(encoding="utf-8")
        checked = set()
        for name, default in re.findall(r"\$\{(BOUNDROOM_[A-Z_]+):-((?:[^{}]|\{[^{}]*\})*)\}", text):
            while default.startswith("${"):  # a legacy fallback such as ${OWLORY_CODEX_BIN:-codex}
                default = default.split(":-", 1)[1][:-1]
            setting = settings.env_names()[name]
            with self.subTest(variable=name):
                if default.startswith("$("):
                    self.assertEqual(settings.COMPUTED, setting.default)
                elif default == "":
                    self.assertIsNone(setting.default)
                else:
                    self.assertEqual(default, setting.default)
            checked.add(name)
        for name, default in re.findall(r'os\.environ\.get\("(BOUNDROOM_[A-Z_]+)", "([^"]*)"\)', text):
            with self.subTest(variable=name):
                self.assertEqual(default, settings.env_names()[name].default)
            checked.add(name)
        self.assertIn("BOUNDROOM_CLAUDE_PERMISSION_MODE", checked)
        self.assertEqual("bypassPermissions", settings.BY_ID["claude_permission_mode"].default)


if __name__ == "__main__":
    unittest.main()
