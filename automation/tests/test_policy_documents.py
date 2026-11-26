"""Reading and writing the supervisor's documents: JSON files, the queue, handoffs and their errors."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from automation.supervisor import policy


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(min_length=1)]


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: Annotated[list[str], Field(min_length=1)]
    count: int
    inner: list[Inner]


def valid_queue(**slice_overrides: Any) -> dict[str, Any]:
    record = {
        "slice_id": "a",
        "title": "A",
        "status": "queued",
        "priority": 1,
        "domain": "core",
        "allowed_paths": ["docs"],
        "required_validations": [],
        "depends_on": [],
        "max_files_changed": 3,
        "notes": "",
    }
    record.update(slice_overrides)
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 3,
            "handoff_timeout_seconds": 60,
            "agent_command_template": "echo {prompt_file}",
            "supervisor_owned_paths": ["automation/queue/slices.json"],
        },
        "slices": [record],
    }


def valid_handoff(slice_id: str = "a", timestamp: str = "2026-01-01T00:00:00Z", **overrides: Any) -> dict[str, Any]:
    document = {
        "slice_id": slice_id,
        "status": "done",
        "summary": "did things",
        "files_touched": [],
        "validations_passed": [],
        "validations_failed": [],
        "proof_level": "doc-only",
        "missing_proof_levels": [],
        "contract_status_changes": [],
        "residual_risks": ["none"],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "repo_clean_status": "clean",
        "git_mirror_status": "not-checked",
        "dirty_paths_outside_scope": [],
        "timestamp": timestamp,
    }
    document.update(overrides)
    return document


def legacy_handoff(slice_id: str = "a", timestamp: str = "2026-01-01T00:00:00Z", **overrides: Any) -> dict[str, Any]:
    document = {
        "slice_id": slice_id,
        "status": "done",
        "summary": "old format",
        "files_touched": [],
        "validations_passed": [],
        "validations_failed": [],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "timestamp": timestamp,
        "risks": [],
    }
    document.update(overrides)
    return document


class DocumentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)

    def write(self, name: str, content: Any) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path


class LoadAndWriteJsonTest(DocumentTestCase):
    def test_load_json_returns_any_json_value(self) -> None:
        for value in ({"a": [1, None]}, [1, 2], "text", 3, None):
            with self.subTest(value=value):
                self.assertEqual(value, policy.load_json(self.write("v.json", json.dumps(value))))

    def test_load_json_reports_a_missing_file_and_invalid_json(self) -> None:
        missing = self.root / "missing.json"
        with self.assertRaises(policy.ConfigError) as raised:
            policy.load_json(missing)
        self.assertEqual(f"missing file: {missing}", str(raised.exception))

        broken = self.write("broken.json", "{oops")
        with self.assertRaises(policy.ConfigError) as raised:
            policy.load_json(broken)
        self.assertEqual(
            f"invalid JSON in {broken}: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            str(raised.exception),
        )

    def test_write_json_writes_indented_utf8_with_a_final_newline(self) -> None:
        target = self.root / "new" / "dir" / "out.json"
        policy.write_json(target, {"b": 1, "a": "é"})
        self.assertEqual('{\n  "b": 1,\n  "a": "é"\n}\n', target.read_text(encoding="utf-8"))

        policy.write_json(target, [])
        self.assertEqual("[]\n", target.read_text(encoding="utf-8"))
        self.assertEqual(["out.json"], [path.name for path in target.parent.iterdir()])


class ValidateDocumentTest(unittest.TestCase):
    def test_a_valid_document_has_no_errors(self) -> None:
        document = {"items": ["a"], "count": 1, "inner": []}
        self.assertEqual(policy.ValidationResult(True, []), policy.validate_document(document, Sample))
        self.assertEqual({"items": ["a"], "count": 1, "inner": []}, document)

    def test_each_error_kind_is_rendered_with_its_json_location(self) -> None:
        cases = (
            (
                "missing properties",
                {},
                [
                    "$: missing required property 'items'",
                    "$: missing required property 'count'",
                    "$: missing required property 'inner'",
                ],
            ),
            (
                "every other kind",
                {"items": [], "count": "3", "inner": [{"name": ""}, {}], "extra": 1},
                [
                    "$.items: expected at least 1 items, found 0",
                    "$.count: Input should be a valid integer, found '3'",
                    "$.inner[0].name: expected string length >= 1, found 0",
                    "$.inner[1]: missing required property 'name'",
                    "$.extra: additional property not allowed",
                ],
            ),
            ("not an object", [1], ["$: Input should be a valid dictionary or instance of Sample, found [1]"]),
        )
        for name, document, errors in cases:
            with self.subTest(name):
                self.assertEqual(policy.ValidationResult(False, errors), policy.validate_document(document, Sample))


class LoadQueueAndHandoffTest(DocumentTestCase):
    def test_a_valid_queue_and_handoff_load_unchanged(self) -> None:
        self.assertEqual(valid_queue(), policy.load_queue(self.write("q.json", valid_queue())))
        self.assertEqual(valid_handoff(), policy.load_handoff(self.write("h.json", valid_handoff())))

    def test_a_missing_queue_points_at_the_example(self) -> None:
        missing = self.root / "slices.json"
        with self.assertRaises(policy.ConfigError) as raised:
            policy.load_queue(missing)
        self.assertEqual(
            f"queue file not found: {missing}\n"
            "hint: copy automation/examples/example-slices.json to that path and edit it for this repository's slices.",
            str(raised.exception),
        )

    def test_schema_errors_come_before_integrity_errors(self) -> None:
        cases = (
            (
                "schema",
                valid_queue(priority=-1, depends_on=["ghost"]),
                "Invalid queue file:\n- $.slices[0].priority: Input should be greater than or equal to 0, found -1",
            ),
            (
                "integrity",
                valid_queue(depends_on=["ghost", "ghoul"]),
                "Queue integrity failure:\n- slice 'a' depends on unknown slice IDs: ['ghost', 'ghoul']",
            ),
        )
        for name, document, message in cases:
            with self.subTest(name), self.assertRaises(ValueError) as raised:
                policy.load_queue(self.write(f"{name}.json", document))
            self.assertEqual(message, str(raised.exception))

    def test_an_invalid_handoff_lists_every_error(self) -> None:
        document = valid_handoff(status="partial")
        del document["summary"]
        with self.assertRaises(ValueError) as raised:
            policy.load_handoff(self.write("h.json", document))
        self.assertEqual(
            "Invalid handoff file:\n"
            "- $.status: Input should be 'done', 'blocked' or 'failed', found 'partial'\n"
            "- $: missing required property 'summary'",
            str(raised.exception),
        )


class LatestHandoffForSliceTest(DocumentTestCase):
    def test_the_latest_handoff_for_the_slice_by_timestamp_then_file_name(self) -> None:
        older = valid_handoff(timestamp="2026-01-01T00:00:00Z", summary="older")
        newer = valid_handoff(timestamp="2026-02-01T00:00:00Z", summary="newer")
        self.write("handoffs/z-older.json", older)
        self.write("handoffs/a-newer.json", newer)
        self.write("handoffs/b-other.json", valid_handoff(slice_id="b", timestamp="2027-01-01T00:00:00Z"))
        self.assertEqual(newer, policy.latest_handoff_for_slice(self.root / "handoffs", "a"))

    def test_equal_timestamps_go_to_the_later_file_name(self) -> None:
        self.write("handoffs/1.json", valid_handoff(summary="first"))
        self.write("handoffs/2.json", valid_handoff(summary="second"))
        self.assertEqual(valid_handoff(summary="second"), policy.latest_handoff_for_slice(self.root / "handoffs", "a"))

    def test_a_legacy_handoff_counts_and_unreadable_files_are_skipped(self) -> None:
        legacy = legacy_handoff(timestamp="2026-03-01T00:00:00Z")
        self.write("handoffs/legacy.json", legacy)
        self.write("handoffs/valid.json", valid_handoff())
        self.write("handoffs/broken.json", "{oops")
        self.write("handoffs/list.json", [1, 2])
        self.write("handoffs/zz-invalid.json", {"slice_id": "a", "timestamp": "2099-01-01T00:00:00Z"})
        self.write("handoffs/zz-not-json.txt", valid_handoff(timestamp="2099-01-01T00:00:00Z"))
        self.assertEqual(legacy, policy.latest_handoff_for_slice(self.root / "handoffs", "a"))

    def test_none_when_nothing_matches(self) -> None:
        self.write("handoffs/b.json", valid_handoff(slice_id="b"))
        for name, directory in (("other slices only", "handoffs"), ("empty", "empty"), ("missing", "missing")):
            with self.subTest(name):
                (self.root / "empty").mkdir(exist_ok=True)
                self.assertIsNone(policy.latest_handoff_for_slice(self.root / directory, "a"))


class LoadLegacyHandoffTest(DocumentTestCase):
    def test_a_legacy_payload_needs_its_keys_and_either_risk_key(self) -> None:
        with_residual = legacy_handoff()
        with_residual["residual_risks"] = with_residual.pop("risks")
        cases: list[tuple[str, Any, Any]] = [
            ("risks", legacy_handoff(), legacy_handoff()),
            ("residual_risks", with_residual, with_residual),
            ("no risk key", {k: v for k, v in legacy_handoff().items() if k != "risks"}, None),
            ("not an object", [legacy_handoff()], None),
            ("invalid JSON", "{oops", None),
        ]
        for key in (
            "slice_id",
            "status",
            "summary",
            "files_touched",
            "validations_passed",
            "validations_failed",
            "recommended_next_slice",
            "recommended_next_reason",
            "timestamp",
        ):
            cases.append((f"without {key}", {k: v for k, v in legacy_handoff().items() if k != key}, None))
        for name, content, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.load_legacy_handoff_for_context(self.write("legacy.json", content)))

    def test_a_missing_file_is_none(self) -> None:
        self.assertIsNone(policy.load_legacy_handoff_for_context(self.root / "missing.json"))


class MakeHandoffFilenameTest(unittest.TestCase):
    def test_each_unsafe_character_becomes_a_dash(self) -> None:
        cases = (
            ("slice-1_x", "20260101T000000Z", "20260101T000000Z-slice-1_x.json"),
            ("a b/é_c.d", "T", "T-a-b-é_c-d.json"),
            ("a..b", "T", "T-a--b.json"),
            ("", "T", "T-.json"),
        )
        for slice_id, token, expected in cases:
            with self.subTest(slice_id=slice_id):
                self.assertEqual(expected, policy.make_handoff_filename(slice_id, token))


if __name__ == "__main__":
    unittest.main()
