"""The settings resolver reports the right value and the source that won (SPEC-008 PS-7, PS-8).

The precedence test is generated from the registry: for every setting, every combination of the
sources it accepts is set to distinct marker values, and the winner must be the highest one set.
"""

from __future__ import annotations

import itertools
import json
import tempfile
import unittest
from pathlib import Path

from automation.supervisor import settings, telemetry_settings
from automation.supervisor.settings import Sources, resolve

ORDER = ("flag", "env", "legacy env", "operator file", "repository")


def source_slots(setting: settings.Setting, entry_point: settings.EntryPoint | None) -> list[tuple[str, str]]:
    """(kind, name) for every place this setting can be read, in precedence order."""
    slots: list[tuple[str, str]] = []
    slots += [("flag", f.options[0]) for f in setting.flags if f.entry_point == entry_point][:1]
    slots += [("env", setting.env)] if setting.env else []
    slots += [("legacy env", name) for name in setting.legacy_env]
    slots += [("operator file", setting.operator_file)] if setting.operator_file else []
    if setting.repository_rule == "any":
        slots += [("repository", path) for path in setting.repository_fields]
    return slots


def sources_for(entry_point: settings.EntryPoint | None, chosen: list[tuple[str, str]]) -> Sources:
    flags, environ, operator, repository = {}, {}, {}, {}
    for kind, name in chosen:
        marker = f"from:{name}"
        if kind == "flag":
            flags[name.lstrip("-").replace("-", "_")] = marker
        elif kind in ("env", "legacy env"):
            environ[name] = marker
        elif kind == "operator file":
            operator[name] = marker
        else:
            repository[name] = marker
    return Sources(entry_point=entry_point, flags=flags, environ=environ, operator=operator, repository=repository)


class PrecedenceTests(unittest.TestCase):
    def test_ps7_every_combination_of_sources_yields_the_highest_one_set(self) -> None:
        checked = 0
        for setting in settings.SETTINGS:
            entry_point = setting.flags[0].entry_point if setting.flags else None
            slots = source_slots(setting, entry_point)
            for size in range(len(slots) + 1):
                for chosen in itertools.combinations(slots, size):
                    resolution = resolve(setting.id, sources_for(entry_point, list(chosen)))
                    if setting.kill_switch:
                        continue  # covered by KillSwitchTests; markers can't be its off value
                    with self.subTest(setting=setting.id, chosen=chosen):
                        if not chosen:
                            self.assertEqual(("default", setting.default), (resolution.source, resolution.value))
                        else:
                            kind, name = chosen[0]  # combinations keep the precedence order of `slots`
                            self.assertEqual((kind, f"from:{name}"), (resolution.source, resolution.value))
                    checked += 1
        self.assertGreater(checked, 200)

    def test_a_legacy_name_is_read_only_when_the_new_name_is_unset(self) -> None:
        both = Sources(environ={"BOUNDROOM_AGENT_RUNNER": "claude", "REPO_AUTOMATION_AGENT_RUNNER": "hermes"})
        resolution = resolve("runner", both)
        self.assertEqual(("env", "claude", None), (resolution.source, resolution.value, resolution.legacy_in_effect))
        self.assertIn("ignored", next(step.note for step in resolution.chain if step.name == "REPO_AUTOMATION_AGENT_RUNNER"))

        legacy = resolve("runner", Sources(environ={"REPO_AUTOMATION_AGENT_RUNNER": "hermes"}))
        self.assertEqual(("legacy env", "hermes"), (legacy.source, legacy.value))
        self.assertEqual("REPO_AUTOMATION_AGENT_RUNNER", legacy.legacy_in_effect)

    def test_legacy_names_are_read_newest_first(self) -> None:
        env = {"REPO_AUTOMATION_CODEX_BIN": "/new/codex", "OWLORY_CODEX_BIN": "/old/codex"}
        self.assertEqual("/new/codex", resolve("codex_bin", Sources(environ=env)).value)
        self.assertEqual("/old/codex", resolve("codex_bin", Sources(environ={"OWLORY_CODEX_BIN": "/old/codex"})).value)

    def test_an_empty_variable_or_flag_counts_as_unset(self) -> None:
        self.assertEqual("default", resolve("runner", Sources(environ={"BOUNDROOM_AGENT_RUNNER": ""})).source)
        policy = settings.repository_values({"agent_command_template": "agent {slice_id}"})
        resolution = resolve("agent_command", Sources(entry_point="run_next", flags={"agent_cmd": ""}, repository=policy))
        self.assertEqual(("repository", "agent {slice_id}"), (resolution.source, resolution.value))

    def test_flags_of_another_entry_point_are_ignored(self) -> None:
        sources = Sources(entry_point="evaluate_handoff", flags={"agent_cmd": "x"})
        self.assertEqual("default", resolve("agent_command", sources).source)

    def test_every_chain_ends_with_the_default(self) -> None:
        for setting in settings.SETTINGS:
            with self.subTest(setting=setting.id):
                chain = resolve(setting.id, Sources()).chain
                self.assertEqual(("default", setting.default), (chain[-1].source, chain[-1].value))


class NarrowOnlyTests(unittest.TestCase):
    """PS-8: a repository value may lower a narrow-only setting, and is refused and shown otherwise."""

    def test_a_lower_value_is_applied(self) -> None:
        sources = Sources(
            operator={"telemetry.json:capture": "content"}, repository={"queue.policy.telemetry.capture": "metadata"}
        )
        resolution = resolve("telemetry_capture", sources)
        self.assertEqual(("repository", "metadata", ()), (resolution.source, resolution.value, resolution.refused))

    def test_a_higher_value_is_refused_and_reported(self) -> None:
        for setting_id, path, value in (
            ("telemetry_capture", "queue.policy.telemetry.capture", "content"),
            ("telemetry_runner", "overlay.policy.telemetry.runner", "content"),
            ("telemetry_mode", "queue.policy.telemetry.mode", "otlp"),
        ):
            with self.subTest(setting=setting_id):
                resolution = resolve(setting_id, Sources(repository={path: value}))
                self.assertEqual("default", resolution.source)
                self.assertEqual(1, len(resolution.refused))
                self.assertIn(path, resolution.refused[0])
                self.assertIn("refused", resolution.chain[-1].note)

    def test_every_narrow_only_setting_has_ordered_choices(self) -> None:
        for setting in settings.SETTINGS:
            if setting.repository_rule == "narrow-only":
                with self.subTest(setting=setting.id):
                    assert setting.choices is not None
                    self.assertIn(setting.default, setting.choices)


class KillSwitchTests(unittest.TestCase):
    def test_the_kill_switch_beats_every_other_source(self) -> None:
        everything = {
            "flags": {"telemetry": "file"},
            "environ": {},
            "operator": {"telemetry.json:mode": "otlp"},
        }
        for flags, environ in (({"no_telemetry": True}, {}), ({}, {"BOUNDROOM_TELEMETRY": "off"})):
            with self.subTest(flags=flags, environ=environ):
                sources = Sources(
                    entry_point="run_next",
                    flags={**everything["flags"], **flags},
                    environ=environ,
                    operator=everything["operator"],
                )
                resolution = resolve("telemetry_mode", sources)
                self.assertEqual(("kill switch", "off"), (resolution.source, resolution.value))
                self.assertEqual("kill switch", resolution.chain[0].source)


class AgreementTests(unittest.TestCase):
    """The telemetry module and the generic resolver must agree on who wins."""

    def test_telemetry_precedence_matches_the_generic_resolver(self) -> None:
        operator = {"mode": "file", "file_dir": "/var/tmp/op", "capture": "content", "runner": "content", "environment": "ci"}
        cases = [
            ({}, {}, {}),
            ({"telemetry_capture": "metadata"}, {"BOUNDROOM_TELEMETRY_RUNNER": "metadata"}, {}),
            ({}, {}, {"capture": "metadata", "runner": "metadata", "environment": "qa"}),
            ({}, {}, {"mode": "off"}),
            ({"no_telemetry": True}, {}, {}),
        ]
        for flags, environ, repo in cases:
            with self.subTest(flags=flags, environ=environ, repo=repo):
                generic = Sources(
                    entry_point="run_next",
                    flags=flags,
                    environ=environ,
                    operator={f"telemetry.json:{k}": v for k, v in operator.items()},
                    repository={f"queue.policy.telemetry.{k}": v for k, v in repo.items()},
                )
                resolved = self.telemetry(flags, environ, operator, repo)
                for name in ("mode", "capture", "runner", "environment"):
                    self.assertEqual(resolved[name], resolve(f"telemetry_{name}", generic).value, name)

    @staticmethod
    def telemetry(flags: dict, environ: dict, operator: dict, repo: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.json"
            path.write_text(json.dumps(operator), encoding="utf-8")
            result = telemetry_settings.resolve(flags=flags, environ=environ, operator_file=path, repository=repo)
        return {"mode": result.mode, "capture": result.capture, "runner": result.runner, "environment": result.environment}


class RepositoryValuesTests(unittest.TestCase):
    def test_policy_overlay_and_memory_files_flatten_to_registry_paths(self) -> None:
        values = settings.repository_values(
            {"consecutive_autonomous_limit": 3, "telemetry": {"mode": "off"}},
            {"board": "demo", "backend": "local", "policy": {"agent_command_template": "x"}},
            {"endpoint": "https://memory.example.test", "budgets": {"max_results": 5}},
        )
        self.assertEqual(3, values["queue.policy.consecutive_autonomous_limit"])
        self.assertEqual("off", values["queue.policy.telemetry.mode"])
        self.assertEqual(
            ("demo", "local", "x"),
            (values["overlay.board"], values["overlay.backend"], values["overlay.policy.agent_command_template"]),
        )
        self.assertEqual(5, values["memory.budgets.max_results"])
        self.assertNotIn("queue.policy.agent_command_template", values)


if __name__ == "__main__":
    unittest.main()
