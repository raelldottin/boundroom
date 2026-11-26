"""Telemetry settings: off by default, user-chosen destinations only (SPEC-006 RA-TEL-001).

Covers UD-2 (a mode needs a destination), UD-5 (the kill switch wins), UD-6 (ambient OTEL_* and
LOGFIRE_* are ignored), UD-7 (the repository can only reduce), UD-10 (plain http only to loopback),
the precedence order, and that headers never reach a report or a repr.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from automation.schemas.models import RepositoryTelemetry
from automation.supervisor import egress, policy, run_next
from automation.supervisor.telemetry_settings import (
    TelemetrySettings,
    check_endpoint,
    check_resource_attributes,
    parse_headers,
    parse_resource_attributes,
    resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "Bearer s3cr3t-telemetry-token"


class TelemetryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-settings-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.no_file = self.tmp / "absent.json"

    def operator_file(self, **values: object) -> Path:
        path = self.tmp / "telemetry.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        return path


class DefaultsAndDestinationsTests(TelemetryTestCase):
    def test_off_with_no_setting_anywhere(self) -> None:
        resolved = resolve(environ={}, operator_file=self.no_file)
        self.assertFalse(resolved.enabled)
        self.assertEqual("default", resolved.sources["mode"])
        self.assertEqual((), resolved.messages)
        self.assertIsNone(resolved.refusal)

    def test_ud2_a_mode_without_its_destination_stays_off_and_says_which_is_missing(self) -> None:
        for mode, missing in (("file", "file_dir"), ("otlp", "endpoint")):
            with self.subTest(mode=mode):
                resolved = resolve(flags={"telemetry": mode}, environ={}, operator_file=self.no_file)
                self.assertFalse(resolved.enabled)
                self.assertIn(missing, resolved.messages[0])
                self.assertIsNone(resolved.refusal)

    def test_ud2_a_destination_without_a_mode_stays_off(self) -> None:
        resolved = resolve(flags={"telemetry_file_dir": str(self.tmp)}, environ={}, operator_file=self.no_file)
        self.assertFalse(resolved.enabled)

    def test_ud2_required_turns_a_missing_destination_into_a_refusal(self) -> None:
        resolved = resolve(flags={"telemetry": "otlp", "telemetry_required": True}, environ={}, operator_file=self.no_file)
        self.assertIsNotNone(resolved.refusal)
        self.assertIn("endpoint", str(resolved.refusal))

    def test_both_halves_turn_it_on(self) -> None:
        resolved = resolve(
            flags={"telemetry": "file", "telemetry_file_dir": str(self.tmp)}, environ={}, operator_file=self.no_file
        )
        self.assertTrue(resolved.enabled)
        self.assertEqual("flag", resolved.sources["mode"])
        self.assertEqual({"mode": "file", "destination": "file"}, {k: resolved.report()[k] for k in ("mode", "destination")})


class PrecedenceTests(TelemetryTestCase):
    def test_flag_then_env_then_operator_file_then_default(self) -> None:
        operator = self.operator_file(mode="file", file_dir="/var/tmp/op", capture="content", environment="ci")
        env = {"BOUNDROOM_TELEMETRY_FILE_DIR": "/var/tmp/env", "BOUNDROOM_TELEMETRY_CAPTURE": "metadata"}
        resolved = resolve(flags={"telemetry_file_dir": "/var/tmp/flag"}, environ=env, operator_file=operator)
        self.assertEqual(("file", "operator file"), (resolved.mode, resolved.sources["mode"]))
        self.assertEqual(("/var/tmp/flag", "flag"), (resolved.file_dir, resolved.sources["file_dir"]))
        self.assertEqual(("metadata", "env"), (resolved.capture, resolved.sources["capture"]))
        self.assertEqual(("ci", "operator file"), (resolved.environment, resolved.sources["environment"]))
        self.assertEqual(("off", "default"), (resolved.runner, resolved.sources["runner"]))

    def test_ud5_the_kill_switch_wins_over_every_other_source(self) -> None:
        operator = self.operator_file(mode="otlp", endpoint="https://collector.example.test")
        on_everywhere = {
            "telemetry": "file",
            "telemetry_file_dir": str(self.tmp),
            "telemetry_endpoint": "https://flag.example.test",
        }
        for flags, env in (
            ({**on_everywhere, "no_telemetry": True}, {"BOUNDROOM_TELEMETRY_ENDPOINT": "https://env.example.test"}),
            (on_everywhere, {"BOUNDROOM_TELEMETRY": "off"}),
        ):
            with self.subTest(flags=sorted(flags), env=sorted(env)):
                resolved = resolve(flags=flags, environ=env, operator_file=operator)
                self.assertFalse(resolved.enabled)
                self.assertEqual("kill switch", resolved.sources["mode"])

    def test_ud6_ambient_otel_and_logfire_settings_enable_nothing(self) -> None:
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "OTEL_TRACES_EXPORTER": "otlp",
            "LOGFIRE_TOKEN": "x",
            "LOGFIRE_SEND_TO_LOGFIRE": "true",
        }
        resolved = resolve(environ=env, operator_file=self.no_file)
        self.assertFalse(resolved.enabled)
        self.assertIsNone(resolved.endpoint)

    def test_an_invalid_operator_file_is_reported_and_ignored(self) -> None:
        operator = self.operator_file(mode="file", file_dir="/var/tmp/op", endpoint="http://collector.example.test")
        resolved = resolve(environ={}, operator_file=operator)
        self.assertFalse(resolved.enabled)
        self.assertIn("ignoring", resolved.messages[0])

    def test_an_invalid_variable_is_reported_and_ignored(self) -> None:
        resolved = resolve(environ={"BOUNDROOM_TELEMETRY": "vendor"}, operator_file=self.no_file)
        self.assertFalse(resolved.enabled)
        self.assertIn("BOUNDROOM_TELEMETRY", resolved.messages[0])


class RepositoryBlockTests(TelemetryTestCase):
    def test_ud7_each_widening_field_is_refused_with_a_named_reason(self) -> None:
        cases = {
            "endpoint": {"endpoint": "https://collector.example.test"},
            "file_dir": {"file_dir": "/tmp/x"},
            "headers": {"headers": {"authorization": SECRET}},
            "required": {"required": True},
            "mode": {"mode": "file"},
            "capture": {"capture": "content"},
            "runner": {"runner": "content"},
        }
        for field, block in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError) as caught:
                    RepositoryTelemetry.model_validate(block)
                self.assertIn(f"telemetry.{field}", str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))

    def test_ud7_reducing_fields_are_accepted(self) -> None:
        RepositoryTelemetry.model_validate({"mode": "off", "capture": "metadata", "runner": "metadata", "environment": "ci"})
        RepositoryTelemetry.model_validate({"runner": "off"})

    def test_ud7_a_queue_whose_policy_widens_telemetry_fails_to_load(self) -> None:
        queue = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
        queue["policy"]["telemetry"] = {"endpoint": "https://collector.example.test"}
        path = self.tmp / "slices.json"
        path.write_text(json.dumps(queue), encoding="utf-8")
        with self.assertRaises((ValueError, policy.ConfigError)) as caught:
            policy.load_queue(path)
        self.assertIn("telemetry.endpoint", str(caught.exception))

    def test_the_repository_turns_off_what_the_operator_turned_on(self) -> None:
        operator = self.operator_file(mode="file", file_dir="/var/tmp/op", capture="content", runner="content")
        resolved = resolve(environ={}, operator_file=operator, repository={"mode": "off"})
        self.assertFalse(resolved.enabled)
        self.assertEqual("repository", resolved.sources["mode"])

    def test_the_repository_lowers_capture_and_runner_but_never_raises_them(self) -> None:
        operator = self.operator_file(mode="file", file_dir="/var/tmp/op", capture="content", runner="content")
        resolved = resolve(environ={}, operator_file=operator, repository={"capture": "metadata", "runner": "metadata"})
        self.assertEqual(("metadata", "metadata"), (resolved.capture, resolved.runner))
        lowered = resolve(environ={}, operator_file=self.no_file, repository={"runner": "metadata"})
        self.assertEqual("off", lowered.runner)

    def test_the_repository_names_the_environment_only_when_nobody_else_did(self) -> None:
        self.assertEqual("ci", resolve(environ={}, operator_file=self.no_file, repository={"environment": "ci"}).environment)
        env = {"BOUNDROOM_TELEMETRY_ENVIRONMENT": "local"}
        self.assertEqual("local", resolve(environ=env, operator_file=self.no_file, repository={"environment": "ci"}).environment)

    def test_required_telemetry_turned_off_by_the_repository_refuses_to_start(self) -> None:
        flags = {"telemetry": "file", "telemetry_file_dir": "/var/tmp/x", "telemetry_required": True}
        resolved = resolve(flags=flags, environ={}, operator_file=self.no_file, repository={"mode": "off"})
        self.assertIsNotNone(resolved.refusal)


class EndpointAndSecretsTests(TelemetryTestCase):
    def test_ud10_plain_http_only_to_loopback(self) -> None:
        for url in (
            "https://collector.example.test/v1/traces",
            "http://localhost:4318",
            "http://127.0.0.1:4318",
            "http://[::1]:4318",
        ):
            with self.subTest(url=url):
                self.assertEqual(url, check_endpoint(url))
        for url in (
            "http://collector.example.test",
            "http://10.0.0.5:4318",
            "ftp://127.0.0.1",
            "https://u:p@collector.example.test",
            "x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    check_endpoint(url)

    def test_an_endpoint_with_only_a_user_name_or_only_a_password_is_refused(self) -> None:
        for url in ("https://u@collector.example.test", "https://:p@collector.example.test"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    check_endpoint(url)
                self.assertEqual("telemetry endpoint must not carry credentials; use headers", str(caught.exception))

    def test_the_endpoint_variable_gets_the_same_checks_as_the_flag(self) -> None:
        for url, reason in (
            ("http://collector.example.test", "telemetry endpoint must use https:// unless it is localhost, 127.0.0.1 or [::1]"),
            ("https://u@collector.example.test", "telemetry endpoint must not carry credentials; use headers"),
        ):
            with self.subTest(url=url):
                env = {"BOUNDROOM_TELEMETRY": "otlp", "BOUNDROOM_TELEMETRY_ENDPOINT": url}
                resolved = resolve(environ=env, operator_file=self.no_file)
                self.assertEqual(("off", None), (resolved.mode, resolved.endpoint))
                self.assertEqual(
                    (
                        f"telemetry: ignoring BOUNDROOM_TELEMETRY_ENDPOINT: {reason}",
                        "telemetry: otlp mode needs endpoint; telemetry is off",
                    ),
                    resolved.messages,
                )

    def test_resolved_settings_cannot_be_changed_afterwards(self) -> None:
        resolved = resolve(environ={}, operator_file=self.no_file)
        with self.assertRaises(AttributeError):  # immutable, however it is built
            resolved.mode = "otlp"  # ty: ignore[invalid-assignment]
        self.assertEqual("off", resolved.mode)

    def test_headers_parse_from_name_value_pairs(self) -> None:
        self.assertEqual({"authorization": "Bearer a", "x-team": "b"}, parse_headers("authorization=Bearer a, x-team=b"))
        with self.assertRaises(ValueError):
            parse_headers("no-equals-sign")

    def test_headers_paths_and_queries_never_appear_in_the_report_or_repr(self) -> None:
        env = {
            "BOUNDROOM_TELEMETRY": "otlp",
            "BOUNDROOM_TELEMETRY_ENDPOINT": "https://collector.example.test:4318/v1/traces?tenant=abc",
            "BOUNDROOM_TELEMETRY_HEADERS": f"authorization={SECRET}",
        }
        resolved = resolve(environ=env, operator_file=self.no_file)
        self.assertEqual({"authorization": SECRET}, resolved.headers)
        report = json.dumps(resolved.report(trace_id="0" * 32))
        self.assertEqual("https://collector.example.test:4318", resolved.report()["destination"])
        for leaked in (SECRET, "s3cr3t", "/v1/traces", "tenant=abc"):
            self.assertNotIn(leaked, report)
            self.assertNotIn(leaked, repr(resolved))


class VariableAndOfflineTests(TelemetryTestCase):
    def test_required_takes_only_true_or_false(self) -> None:
        resolved = resolve(environ={"BOUNDROOM_TELEMETRY_REQUIRED": "yes"}, operator_file=self.no_file)
        self.assertEqual(("telemetry: ignoring BOUNDROOM_TELEMETRY_REQUIRED: must be true or false",), resolved.messages)
        self.assertEqual((False, "default"), (resolved.required, resolved.sources["required"]))

    def test_required_from_the_variable_is_a_boolean(self) -> None:
        # File mode without a file_dir: only a truly required run turns that into a refusal.
        for raw, required, refused in (("true", True, True), ("false", False, False)):
            with self.subTest(raw=raw):
                env = {"BOUNDROOM_TELEMETRY": "file", "BOUNDROOM_TELEMETRY_REQUIRED": raw}
                resolved = resolve(environ=env, operator_file=self.no_file)
                self.assertIs(required, resolved.required)
                self.assertEqual("env", resolved.sources["required"])
                self.assertEqual(refused, resolved.refusal is not None)

    def test_offline_turns_otlp_off_and_says_so_without_refusing_unless_required(self) -> None:
        flags = {"telemetry": "otlp", "telemetry_endpoint": "https://collector.example.test"}
        message = f"telemetry: otlp mode refused ({egress.REASON}): {egress.OFFLINE_ENV} is set; telemetry is off"
        resolved = resolve(flags=flags, environ={egress.OFFLINE_ENV: "1"}, operator_file=self.no_file)
        self.assertEqual(("off", egress.REASON, None), (resolved.mode, resolved.sources["mode"], resolved.refusal))
        self.assertEqual((message,), resolved.messages)
        self.assertEqual(egress.REASON, resolved.report()["source"])
        required = resolve(
            flags={**flags, "telemetry_required": True}, environ={egress.OFFLINE_ENV: "1"}, operator_file=self.no_file
        )
        self.assertEqual(
            message.replace("; telemetry is off", "; refusing to start because telemetry is required"), required.refusal
        )

    def test_offline_leaves_file_mode_on(self) -> None:
        flags = {"telemetry": "file", "telemetry_file_dir": "/var/tmp/spans"}
        resolved = resolve(flags=flags, environ={egress.OFFLINE_ENV: "1"}, operator_file=self.no_file)
        self.assertEqual(("file", "flag", ()), (resolved.mode, resolved.sources["mode"], resolved.messages))


class ResourceAttributeBoundaryTests(TelemetryTestCase):
    def test_the_bounds_on_pairs_values_and_characters(self) -> None:
        def pairs(count: int) -> dict[str, str]:
            return {f"label.k{index}": "v" for index in range(count)}

        cases = [
            ("16 pairs", pairs(16), None),
            ("17 pairs", pairs(17), "too_many"),
            ("256 characters", {"team.name": "v" * 256}, None),
            ("257 characters", {"team.name": "v" * 257}, "bad_value"),
            ("character 31", {"team.name": "a" + chr(31)}, "bad_value"),
            ("character 32", {"team.name": "a" + chr(32)}, None),
            ("character 126", {"team.name": "a" + chr(126)}, None),
            ("character 127", {"team.name": "a" + chr(127)}, "bad_value"),
        ]
        for name, given, refused in cases:
            with self.subTest(name):
                if refused is None:
                    self.assertEqual(given, check_resource_attributes(given))
                else:
                    with self.assertRaises(ValueError) as caught:
                        check_resource_attributes(given)
                    self.assertIn(f"({refused})", str(caught.exception))

    def test_a_refusal_names_the_offending_pair_by_its_position_counting_from_one(self) -> None:
        cases = [
            (
                lambda: check_resource_attributes({"Team": "core"}),
                "resource_attributes refused (bad_key): key 1 must match ^[a-z][a-z0-9_.]{0,63}$",
            ),
            (
                lambda: check_resource_attributes({"team.name": "core", "service.name": "x"}),
                "resource_attributes refused (reserved_key): key 2 is one Boundroom sets itself",
            ),
            (
                lambda: parse_resource_attributes("team.name"),
                "resource_attributes refused (not_key_value): pair 1 must be key=value",
            ),
            (
                lambda: parse_resource_attributes("team.name=a,team.name=b"),
                "resource_attributes refused (duplicate_key): pair 2 repeats a key",
            ),
        ]
        for refuse, message in cases:
            with self.subTest(message):
                with self.assertRaises(ValueError) as caught:
                    refuse()
                self.assertEqual(message, str(caught.exception))


class RepositoryCapTests(TelemetryTestCase):
    def test_a_repository_value_that_is_none_changes_nothing(self) -> None:
        operator = self.operator_file(mode="file", file_dir="/var/tmp/op", capture="content", runner="content", environment="ci")
        given = resolve(environ={}, operator_file=operator)
        capped = resolve(
            environ={},
            operator_file=operator,
            repository={"mode": None, "capture": None, "runner": None, "environment": None},
        )
        self.assertEqual(given, capped)

    def test_a_repository_value_equal_to_the_resolved_one_keeps_its_source(self) -> None:
        operator = self.operator_file(mode="file", file_dir="/var/tmp/op", capture="metadata", runner="metadata")
        for setting in ("capture", "runner"):
            with self.subTest(setting):
                resolved = resolve(environ={}, operator_file=operator, repository={setting: "metadata"})
                self.assertEqual(("metadata", "operator file"), (getattr(resolved, setting), resolved.sources[setting]))


class ReportDestinationTests(TelemetryTestCase):
    def settings(self, mode: str, endpoint: Optional[str]) -> TelemetrySettings:
        return TelemetrySettings(
            mode=mode,
            file_dir=None,
            endpoint=endpoint,
            headers={},
            capture="metadata",
            runner="off",
            required=False,
            environment="local",
            sources={"mode": "flag"},
        )

    def test_the_destination_is_the_scheme_host_and_port_only(self) -> None:
        cases = [
            ("https://collector.example.test:4318/v1/traces?tenant=abc", "https://collector.example.test:4318"),
            ("https://collector.example.test/otlp", "https://collector.example.test"),
            (None, None),
        ]
        for endpoint, destination in cases:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(destination, self.settings("otlp", endpoint).report()["destination"])


class EntryPointTests(TelemetryTestCase):
    """The flags reach the supervisor, and a refusal comes before anything is claimed."""

    def queue(self) -> Path:
        path = self.tmp / "slices.json"
        shutil.copy(REPO_ROOT / "automation/examples/example-slices.json", path)
        return path

    def run_next(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp)}
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "automation/supervisor/run_next.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )

    def test_required_telemetry_without_a_destination_refuses_before_the_claim(self) -> None:
        queue = self.queue()
        before = queue.read_bytes()
        result = self.run_next("--queue", str(queue), "--telemetry", "file", "--telemetry-required")
        self.assertEqual(78, result.returncode, result.stderr)
        self.assertIn("file_dir", result.stderr)
        self.assertEqual(before, queue.read_bytes())

    def test_decision_and_failure_reports_carry_the_telemetry_block(self) -> None:
        block = resolve(
            flags={"telemetry": "file", "telemetry_file_dir": "/var/tmp/spans"}, environ={}, operator_file=None
        ).report()
        record = {"slice_id": "S-1"}
        failure = run_next.make_failure_report(record, "x", completed_runs=0, autonomous_limit=1, telemetry=block)
        self.assertEqual(
            {"mode": "file", "destination": "file", "source": "flag"},
            {k: failure["telemetry"][k] for k in ("mode", "destination", "source")},
        )
        self.assertNotIn("/var/tmp/spans", json.dumps(failure))
        self.assertNotIn("telemetry", run_next.make_failure_report(record, "x", completed_runs=0, autonomous_limit=1))


if __name__ == "__main__":
    unittest.main()
