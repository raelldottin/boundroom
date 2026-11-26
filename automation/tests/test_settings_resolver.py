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
from typing import Any

from automation.supervisor import settings, telemetry_settings
from automation.supervisor.settings import Sources, Step, resolve

ORDER = ("flag", "env", "legacy env", "operator file", "repository")


def step_tuple(step: Step) -> tuple[Any, ...]:
    return (step.source, step.name, step.is_set, step.value, step.note)


def observed(resolution: settings.Resolution) -> tuple[Any, ...]:
    """Everything a resolution reports, as plain tuples; the chain is what --explain prints."""
    return (
        resolution.source,
        resolution.value,
        resolution.refused,
        resolution.legacy_in_effect,
        tuple(step_tuple(step) for step in resolution.chain),
    )


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

    def test_every_chain_ends_with_the_default(self) -> None:
        for setting in settings.SETTINGS:
            with self.subTest(setting=setting.id):
                chain = resolve(setting.id, Sources()).chain
                self.assertEqual(("default", setting.default), (chain[-1].source, chain[-1].value))


class NarrowOnlyTests(unittest.TestCase):
    """PS-8: a repository value may lower a narrow-only setting, and is refused and shown otherwise."""

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


RUNNER_ENV = ("env", "BOUNDROOM_AGENT_RUNNER", False, None, "")
RUNNER_LEGACY = ("legacy env", "REPO_AUTOMATION_AGENT_RUNNER", False, None, "")
RUNNER_DEFAULT = ("default", "default", True, "auto", "")
AGENT_COMMAND_DEFAULT = ("default", "default", True, None, "")
POLICY = {"queue.policy.agent_command_template": "agent {slice_id}"}


class ChainTests(unittest.TestCase):
    """The chain --explain prints records every source, whether it was set, and what happened to it."""

    def test_each_source_appears_in_the_chain_as_resolved(self) -> None:
        cases: list[tuple[str, str, Sources, tuple[Any, ...]]] = [
            (
                "nothing set",
                "runner",
                Sources(),
                ("default", "auto", (), None, (RUNNER_ENV, RUNNER_LEGACY, RUNNER_DEFAULT)),
            ),
            (
                "an empty variable",
                "runner",
                Sources(environ={"BOUNDROOM_AGENT_RUNNER": ""}),
                (
                    "default",
                    "auto",
                    (),
                    None,
                    (
                        ("env", "BOUNDROOM_AGENT_RUNNER", False, "", "empty: treated as unset"),
                        RUNNER_LEGACY,
                        RUNNER_DEFAULT,
                    ),
                ),
            ),
            (
                "the variable",
                "runner",
                Sources(environ={"BOUNDROOM_AGENT_RUNNER": "claude"}),
                (
                    "env",
                    "claude",
                    (),
                    None,
                    (("env", "BOUNDROOM_AGENT_RUNNER", True, "claude", ""), RUNNER_LEGACY, RUNNER_DEFAULT),
                ),
            ),
            (
                "the legacy name alone",
                "runner",
                Sources(environ={"REPO_AUTOMATION_AGENT_RUNNER": "hermes"}),
                (
                    "legacy env",
                    "hermes",
                    (),
                    "REPO_AUTOMATION_AGENT_RUNNER",
                    (
                        RUNNER_ENV,
                        ("legacy env", "REPO_AUTOMATION_AGENT_RUNNER", True, "hermes", "use BOUNDROOM_AGENT_RUNNER"),
                        RUNNER_DEFAULT,
                    ),
                ),
            ),
            (
                "the variable and the legacy name",
                "runner",
                Sources(environ={"BOUNDROOM_AGENT_RUNNER": "claude", "REPO_AUTOMATION_AGENT_RUNNER": "hermes"}),
                (
                    "env",
                    "claude",
                    (),
                    None,
                    (
                        ("env", "BOUNDROOM_AGENT_RUNNER", True, "claude", ""),
                        ("legacy env", "REPO_AUTOMATION_AGENT_RUNNER", True, "hermes", "ignored: a newer name is set"),
                        RUNNER_DEFAULT,
                    ),
                ),
            ),
            (
                "both legacy names",
                "codex_bin",
                Sources(environ={"REPO_AUTOMATION_CODEX_BIN": "/new/codex", "OWLORY_CODEX_BIN": "/old/codex"}),
                (
                    "legacy env",
                    "/new/codex",
                    (),
                    "REPO_AUTOMATION_CODEX_BIN",
                    (
                        ("env", "BOUNDROOM_CODEX_BIN", False, None, ""),
                        ("legacy env", "REPO_AUTOMATION_CODEX_BIN", True, "/new/codex", "use BOUNDROOM_CODEX_BIN"),
                        ("legacy env", "OWLORY_CODEX_BIN", True, "/old/codex", "ignored: a newer name is set"),
                        ("default", "default", True, "codex", ""),
                    ),
                ),
            ),
            (
                "every source of a repository-any setting",
                "telemetry_environment",
                Sources(
                    environ={"BOUNDROOM_TELEMETRY_ENVIRONMENT": "from-env"},
                    operator={"telemetry.json:environment": "from-operator"},
                    repository={
                        "queue.policy.telemetry.environment": "from-queue",
                        "overlay.policy.telemetry.environment": "from-overlay",
                    },
                ),
                (
                    "env",
                    "from-env",
                    (),
                    None,
                    (
                        ("env", "BOUNDROOM_TELEMETRY_ENVIRONMENT", True, "from-env", ""),
                        ("operator file", "telemetry.json:environment", True, "from-operator", ""),
                        ("repository", "queue.policy.telemetry.environment", True, "from-queue", ""),
                        ("repository", "overlay.policy.telemetry.environment", True, "from-overlay", ""),
                        ("default", "default", True, "local", ""),
                    ),
                ),
            ),
            (
                "the operator file alone",
                "telemetry_environment",
                Sources(operator={"telemetry.json:environment": "from-operator"}),
                (
                    "operator file",
                    "from-operator",
                    (),
                    None,
                    (
                        ("env", "BOUNDROOM_TELEMETRY_ENVIRONMENT", False, None, ""),
                        ("operator file", "telemetry.json:environment", True, "from-operator", ""),
                        ("default", "default", True, "local", ""),
                    ),
                ),
            ),
            (
                "the repository alone",
                "telemetry_environment",
                Sources(repository={"queue.policy.telemetry.environment": "from-queue"}),
                (
                    "repository",
                    "from-queue",
                    (),
                    None,
                    (
                        ("env", "BOUNDROOM_TELEMETRY_ENVIRONMENT", False, None, ""),
                        ("operator file", "telemetry.json:environment", False, None, ""),
                        ("repository", "queue.policy.telemetry.environment", True, "from-queue", ""),
                        ("default", "default", True, "local", ""),
                    ),
                ),
            ),
            (
                "a flag of this entry point",
                "agent_command",
                Sources(entry_point="run_next", flags={"agent_cmd": "x"}),
                ("flag", "x", (), None, (("flag", "--agent-cmd", True, "x", ""), AGENT_COMMAND_DEFAULT)),
            ),
            (
                "an empty flag under a policy value",
                "agent_command",
                Sources(entry_point="run_next", flags={"agent_cmd": ""}, repository=POLICY),
                (
                    "repository",
                    "agent {slice_id}",
                    (),
                    None,
                    (
                        ("flag", "--agent-cmd", False, None, ""),
                        ("repository", "queue.policy.agent_command_template", True, "agent {slice_id}", ""),
                        AGENT_COMMAND_DEFAULT,
                    ),
                ),
            ),
            (
                "a flag another entry point owns",
                "agent_command",
                Sources(entry_point="evaluate_handoff", flags={"agent_cmd": "x"}),
                ("default", None, (), None, (AGENT_COMMAND_DEFAULT,)),
            ),
            (
                "a flag several entry points share, with no entry point",
                "telemetry_capture",
                Sources(flags={"telemetry_capture": "content"}),
                (
                    "flag",
                    "content",
                    (),
                    None,
                    (
                        ("flag", "--telemetry-capture", True, "content", ""),
                        ("env", "BOUNDROOM_TELEMETRY_CAPTURE", False, None, ""),
                        ("operator file", "telemetry.json:capture", False, None, ""),
                        ("default", "default", True, "metadata", ""),
                    ),
                ),
            ),
        ]
        for label, setting_id, sources, expected in cases:
            with self.subTest(label):
                self.assertEqual(expected, observed(resolve(setting_id, sources)))


class LegacyWarningTests(unittest.TestCase):
    def test_legacy_env_warnings_table(self) -> None:
        runner = "warning: REPO_AUTOMATION_AGENT_RUNNER is deprecated and stops working in v0.6.0; use BOUNDROOM_AGENT_RUNNER"
        codex = "warning: REPO_AUTOMATION_CODEX_BIN is deprecated and stops working in v0.6.0; use BOUNDROOM_CODEX_BIN."
        owlory = "warning: OWLORY_CODEX_BIN is deprecated and stops working in v0.6.0; use BOUNDROOM_CODEX_BIN."
        cases: list[tuple[str, dict[str, str], list[str]]] = [
            ("nothing set", {}, []),
            ("a legacy name alone", {"REPO_AUTOMATION_AGENT_RUNNER": "hermes"}, [runner + "."]),
            (
                "a legacy name and its new name",
                {"REPO_AUTOMATION_AGENT_RUNNER": "hermes", "BOUNDROOM_AGENT_RUNNER": "claude"},
                [runner + "; BOUNDROOM_AGENT_RUNNER is also set, so REPO_AUTOMATION_AGENT_RUNNER is ignored."],
            ),
            (
                "both legacy names of one setting",
                {"OWLORY_CODEX_BIN": "/old", "REPO_AUTOMATION_CODEX_BIN": "/new"},
                [codex, owlory],
            ),
            (
                "names the supervisor exports for its runner",
                {"REPO_AUTOMATION_SUPERVISOR_SLICE_ID": "S-1", "OWLORY_SUPERVISOR_SLICE_ID": "S-1"},
                [],
            ),
            (
                "registry order, not the order they were set",
                {"REPO_AUTOMATION_CODEX_BIN": "/new", "REPO_AUTOMATION_AGENT_RUNNER": "hermes"},
                [runner + ".", codex],
            ),
        ]
        for label, environ, expected in cases:
            with self.subTest(label):
                self.assertEqual(expected, settings.legacy_env_warnings(environ))


APPLIED = "applied: the repository may lower this"
SAME = "same as the effective value"


class NarrowOnlyTableTests(unittest.TestCase):
    """PS-8 for every ordered pair of choices: the operator's value against the repository's."""

    def test_every_ordered_pair_of_choices(self) -> None:
        # (key, operator value or None for the default, repository value, outcome, expected source, expected value)
        cases: list[tuple[str, str | None, str, str, str, str]] = [
            ("capture", None, "metadata", SAME, "default", "metadata"),
            ("capture", None, "content", "refused", "default", "metadata"),
            ("capture", "metadata", "metadata", SAME, "operator file", "metadata"),
            ("capture", "metadata", "content", "refused", "operator file", "metadata"),
            ("capture", "content", "metadata", APPLIED, "repository", "metadata"),
            ("capture", "content", "content", SAME, "operator file", "content"),
            ("runner", "off", "off", SAME, "operator file", "off"),
            ("runner", "off", "metadata", "refused", "operator file", "off"),
            ("runner", "off", "content", "refused", "operator file", "off"),
            ("runner", "metadata", "off", APPLIED, "repository", "off"),
            ("runner", "metadata", "metadata", SAME, "operator file", "metadata"),
            ("runner", "metadata", "content", "refused", "operator file", "metadata"),
            ("runner", "content", "off", APPLIED, "repository", "off"),
            ("runner", "content", "metadata", APPLIED, "repository", "metadata"),
            ("runner", "content", "content", SAME, "operator file", "content"),
            ("mode", "off", "off", SAME, "operator file", "off"),
            ("mode", "off", "file", "refused", "operator file", "off"),
            ("mode", "off", "otlp", "refused", "operator file", "off"),
            ("mode", "file", "off", APPLIED, "repository", "off"),
            ("mode", "file", "file", SAME, "operator file", "file"),
            ("mode", "file", "otlp", "refused", "operator file", "file"),
            ("mode", "otlp", "off", APPLIED, "repository", "off"),
            ("mode", "otlp", "file", APPLIED, "repository", "file"),
            ("mode", "otlp", "otlp", SAME, "operator file", "otlp"),
            # A repository value outside the choices is refused, never an error.
            ("capture", None, "everything", "refused", "default", "metadata"),
        ]
        for key, operator_value, repo_value, outcome, source, value in cases:
            path = f"queue.policy.telemetry.{key}"
            if outcome == "refused":
                reason = f"{path}={repo_value!r} refused: the repository may only lower telemetry_{key}"
                expected = (source, value, (reason,), ("repository", path, True, repo_value, reason))
            else:
                expected = (source, value, (), ("repository", path, True, repo_value, outcome))
            operator = {} if operator_value is None else {f"telemetry.json:{key}": operator_value}
            with self.subTest(key=key, operator=operator_value, repository=repo_value):
                resolution = resolve(f"telemetry_{key}", Sources(operator=operator, repository={path: repo_value}))
                self.assertEqual(
                    expected, (resolution.source, resolution.value, resolution.refused, step_tuple(resolution.chain[-1]))
                )

    def test_a_refusal_names_the_field_the_value_and_the_setting(self) -> None:
        # The table above builds each reason from one template; this pins the template's wording.
        resolution = resolve("telemetry_runner", Sources(repository={"overlay.policy.telemetry.runner": "content"}))
        self.assertEqual(
            ("overlay.policy.telemetry.runner='content' refused: the repository may only lower telemetry_runner",),
            resolution.refused,
        )

    def test_two_repository_fields_are_each_compared_with_the_value_so_far(self) -> None:
        queue, overlay = "queue.policy.telemetry.capture", "overlay.policy.telemetry.capture"
        refused = f"{overlay}='content' refused: the repository may only lower telemetry_capture"
        cases: list[tuple[str, str, tuple[Any, ...]]] = [
            (
                "content",
                "metadata",
                (
                    "repository",
                    "metadata",
                    (),
                    (("repository", queue, True, "content", SAME), ("repository", overlay, True, "metadata", APPLIED)),
                ),
            ),
            (
                "metadata",
                "content",
                (
                    "repository",
                    "metadata",
                    (refused,),
                    (("repository", queue, True, "metadata", APPLIED), ("repository", overlay, True, "content", refused)),
                ),
            ),
        ]
        for queue_value, overlay_value, expected in cases:
            with self.subTest(queue=queue_value, overlay=overlay_value):
                sources = Sources(
                    operator={"telemetry.json:capture": "content"},
                    repository={queue: queue_value, overlay: overlay_value},
                )
                resolution = resolve("telemetry_capture", sources)
                self.assertEqual(
                    expected,
                    (
                        resolution.source,
                        resolution.value,
                        resolution.refused,
                        tuple(step_tuple(step) for step in resolution.chain[-2:]),
                    ),
                )

    def test_the_repository_caps_every_source_not_only_the_operator_file(self) -> None:
        cases: list[tuple[str, str, Sources, str]] = [
            (
                "a variable",
                "telemetry_runner",
                Sources(
                    environ={"BOUNDROOM_TELEMETRY_RUNNER": "content"},
                    repository={"queue.policy.telemetry.runner": "metadata"},
                ),
                "queue.policy.telemetry.runner",
            ),
            (
                "a flag",
                "telemetry_capture",
                Sources(
                    entry_point="run_next",
                    flags={"telemetry_capture": "content"},
                    repository={"overlay.policy.telemetry.capture": "metadata"},
                ),
                "overlay.policy.telemetry.capture",
            ),
        ]
        for label, setting_id, sources, path in cases:
            with self.subTest(label):
                resolution = resolve(setting_id, sources)
                self.assertEqual(
                    ("repository", "metadata", (), ("repository", path, True, "metadata", APPLIED)),
                    (resolution.source, resolution.value, resolution.refused, step_tuple(resolution.chain[-1])),
                )


MODE_DEFAULT = ("default", "default", True, "off", "")


class KillSwitchTableTests(unittest.TestCase):
    def test_the_kill_switch_heads_the_chain_and_names_what_threw_it(self) -> None:
        refused = "queue.policy.telemetry.mode='otlp' refused: the repository may only lower telemetry_mode"
        cases: list[tuple[str, str, Sources, tuple[Any, ...]]] = [
            (
                "the flag",
                "telemetry_mode",
                Sources(
                    entry_point="run_next",
                    flags={"no_telemetry": True, "telemetry": "file"},
                    operator={"telemetry.json:mode": "otlp"},
                ),
                (
                    "kill switch",
                    "off",
                    (),
                    None,
                    (
                        ("kill switch", "--no-telemetry", True, "off", "always wins"),
                        ("flag", "--telemetry", True, "file", ""),
                        ("env", "BOUNDROOM_TELEMETRY", False, None, ""),
                        ("operator file", "telemetry.json:mode", True, "otlp", ""),
                        MODE_DEFAULT,
                    ),
                ),
            ),
            (
                "the variable set to off",
                "telemetry_mode",
                Sources(environ={"BOUNDROOM_TELEMETRY": "off"}, operator={"telemetry.json:mode": "otlp"}),
                (
                    "kill switch",
                    "off",
                    (),
                    None,
                    (
                        ("kill switch", "BOUNDROOM_TELEMETRY=off", True, "off", "always wins"),
                        ("flag", "--telemetry", False, None, ""),
                        ("env", "BOUNDROOM_TELEMETRY", True, "off", ""),
                        ("operator file", "telemetry.json:mode", True, "otlp", ""),
                        MODE_DEFAULT,
                    ),
                ),
            ),
            (
                "the flag and the variable",
                "telemetry_mode",
                Sources(entry_point="run_next", flags={"no_telemetry": True}, environ={"BOUNDROOM_TELEMETRY": "off"}),
                (
                    "kill switch",
                    "off",
                    (),
                    None,
                    (
                        ("kill switch", "--no-telemetry", True, "off", "always wins"),
                        ("flag", "--telemetry", False, None, ""),
                        ("env", "BOUNDROOM_TELEMETRY", True, "off", ""),
                        ("operator file", "telemetry.json:mode", False, None, ""),
                        MODE_DEFAULT,
                    ),
                ),
            ),
            (
                "the flag, with no entry point",
                "telemetry_mode",
                Sources(flags={"no_telemetry": True, "telemetry": "file"}, operator={"telemetry.json:mode": "otlp"}),
                (
                    "kill switch",
                    "off",
                    (),
                    None,
                    (
                        ("kill switch", "--no-telemetry", True, "off", "always wins"),
                        ("flag", "--telemetry", True, "file", ""),
                        ("env", "BOUNDROOM_TELEMETRY", False, None, ""),
                        ("operator file", "telemetry.json:mode", True, "otlp", ""),
                        MODE_DEFAULT,
                    ),
                ),
            ),
            (
                "the flag over a refused repository value",
                "telemetry_mode",
                Sources(
                    entry_point="run_next",
                    flags={"no_telemetry": True},
                    repository={"queue.policy.telemetry.mode": "otlp"},
                ),
                (
                    "kill switch",
                    "off",
                    (refused,),
                    None,
                    (
                        ("kill switch", "--no-telemetry", True, "off", "always wins"),
                        ("flag", "--telemetry", False, None, ""),
                        ("env", "BOUNDROOM_TELEMETRY", False, None, ""),
                        ("operator file", "telemetry.json:mode", False, None, ""),
                        MODE_DEFAULT,
                        ("repository", "queue.policy.telemetry.mode", True, "otlp", refused),
                    ),
                ),
            ),
            (
                "neither, with telemetry chosen",
                "telemetry_mode",
                Sources(entry_point="run_next", flags={"no_telemetry": False}, environ={"BOUNDROOM_TELEMETRY": "file"}),
                (
                    "env",
                    "file",
                    (),
                    None,
                    (
                        ("flag", "--telemetry", False, None, ""),
                        ("env", "BOUNDROOM_TELEMETRY", True, "file", ""),
                        ("operator file", "telemetry.json:mode", False, None, ""),
                        MODE_DEFAULT,
                    ),
                ),
            ),
            (
                "the kill switch's own resolution",
                "telemetry_kill_switch",
                Sources(entry_point="run_next", flags={"no_telemetry": True}),
                (
                    "flag",
                    True,
                    (),
                    None,
                    (("flag", "--no-telemetry", True, True, ""), ("default", "default", True, False, "")),
                ),
            ),
        ]
        for label, setting_id, sources, expected in cases:
            with self.subTest(label):
                self.assertEqual(expected, observed(resolve(setting_id, sources)))


class ImmutabilityTests(unittest.TestCase):
    def test_registry_entries_and_resolutions_are_read_only(self) -> None:
        # SETTINGS is shared by every entry point in the process, and a resolution is handed to the
        # views as a report: changing either in place would change what every later reader sees.
        objects: list[tuple[object, str]] = [
            (settings.BY_ID["telemetry_capture"].flags[0], "options"),
            (settings.BY_ID["runner"], "default"),
            (Sources(), "environ"),
            (Step("env", "BOUNDROOM_AGENT_RUNNER", True, "claude"), "value"),
            (resolve("runner", Sources()), "value"),
        ]
        for obj, attribute in objects:
            with self.subTest(type=type(obj).__name__, attribute=attribute):
                self.assertTrue(hasattr(obj, attribute))
                with self.assertRaises(AttributeError):
                    setattr(obj, attribute, "changed")


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
