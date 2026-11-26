"""Output-based tests for schema generation and the contract models' own behaviour.

Every write goes to a temporary ``automation/schemas`` directory. The checked-in schema files are
read, never written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from automation.schemas import generate, models

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED_IN = REPO_ROOT / "automation/schemas"
SLICE = "slice.schema.json"
HANDOFF = "handoff.schema.json"
EXAMPLE_QUEUE = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
EXAMPLE_HANDOFF = json.loads((REPO_ROOT / "automation/examples/example-handoff.json").read_text(encoding="utf-8"))
RUN_HINT = "Run: uv run python -m automation.schemas.generate\n"


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "automation/schemas"
    directory.mkdir(parents=True)
    return directory


def test_write_schemas_writes_the_checked_in_bytes_in_contract_order(schema_dir):
    written = generate.write_schemas(schema_dir)

    assert written == [schema_dir / SLICE, schema_dir / HANDOFF]
    assert {path.name: path.read_bytes() for path in written} == {
        SLICE: (CHECKED_IN / SLICE).read_bytes(),
        HANDOFF: (CHECKED_IN / HANDOFF).read_bytes(),
    }


def test_check_reports_every_missing_file(schema_dir):
    assert generate.check_schemas(schema_dir) == [
        f"automation/schemas/{SLICE}: missing",
        f"automation/schemas/{HANDOFF}: missing",
    ]


def test_check_reports_an_edited_file_as_out_of_date_and_an_empty_one_as_missing(schema_dir):
    generate.write_schemas(schema_dir)
    (schema_dir / SLICE).write_text("{}\n", encoding="utf-8")
    (schema_dir / HANDOFF).write_text("", encoding="utf-8")

    assert generate.check_schemas(schema_dir) == [
        f"automation/schemas/{SLICE}: out of date",
        f"automation/schemas/{HANDOFF}: missing",
    ]


def test_check_passes_on_freshly_written_files(schema_dir):
    generate.write_schemas(schema_dir)

    assert generate.check_schemas(schema_dir) == []


def test_main_check_fails_on_drift_and_says_how_to_fix_it(schema_dir, capsys):
    generate.write_schemas(schema_dir)
    (schema_dir / HANDOFF).write_text("{}\n", encoding="utf-8")

    code = generate.main(["--check", "--schema-dir", str(schema_dir)])

    assert code == 1
    assert capsys.readouterr() == (
        "",
        f"Checked-in JSON Schemas are out of date:\n  - automation/schemas/{HANDOFF}: out of date\n{RUN_HINT}",
    )


def test_main_check_passes_on_matching_files(schema_dir, capsys):
    generate.write_schemas(schema_dir)

    code = generate.main(["--check", "--schema-dir", str(schema_dir)])

    assert code == 0
    assert capsys.readouterr() == ("JSON Schemas match automation/schemas/models.py.\n", "")


def test_main_without_check_writes_and_names_each_file(schema_dir, capsys):
    code = generate.main(["--schema-dir", str(schema_dir)])

    assert code == 0
    assert capsys.readouterr() == (f"wrote {schema_dir / SLICE}\nwrote {schema_dir / HANDOFF}\n", "")
    assert generate.check_schemas(schema_dir) == []


def test_render_schema_keeps_non_ascii_text_and_leads_with_the_banner():
    class Accented(BaseModel):
        name: str = Field(description="é")

    rendered = generate.render_schema(Accented)

    assert rendered == (
        "{\n"
        '  "$comment": "Generated from automation/schemas/models.py. Do not edit by hand.",\n'
        '  "properties": {\n'
        '    "name": {\n'
        '      "description": "é",\n'
        '      "title": "Name",\n'
        '      "type": "string"\n'
        "    }\n"
        "  },\n"
        '  "required": [\n'
        '    "name"\n'
        "  ],\n"
        '  "title": "Accented",\n'
        '  "type": "object"\n'
        "}\n"
    )


def test_the_module_runs_as_a_command(tmp_path):
    schema_dir = tmp_path / "automation/schemas"
    shutil.copytree(CHECKED_IN, schema_dir)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}

    result = subprocess.run(
        [sys.executable, "-m", "automation.schemas.generate", "--check", "--schema-dir", str(schema_dir)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "JSON Schemas match automation/schemas/models.py.\n", "")


# The models are strict: JSON-shaped values only. Python-mode validation is what policy uses.


def _errors(model: type[BaseModel], document: Any) -> list[tuple[tuple[Any, ...], str]]:
    try:
        model.model_validate(document)
    except ValidationError as error:
        return [(tuple(item["loc"]), item["type"]) for item in error.errors()]
    return []


def test_the_example_documents_are_valid():
    assert _errors(models.SliceQueue, EXAMPLE_QUEUE) == []
    assert _errors(models.Handoff, EXAMPLE_HANDOFF) == []


def test_a_nested_contract_rejects_a_numeric_string():
    queue = json.loads(json.dumps(EXAMPLE_QUEUE))
    queue["slices"][0]["priority"] = "1"
    queue["policy"]["consecutive_autonomous_limit"] = "3"

    assert _errors(models.SliceQueue, queue) == [
        (("policy", "consecutive_autonomous_limit"), "int_type"),
        (("slices", 0, "priority"), "int_type"),
    ]


def test_a_handoff_rejects_a_tuple_where_json_has_a_list():
    handoff = {**EXAMPLE_HANDOFF, "files_touched": ("a.md",)}

    assert _errors(models.Handoff, handoff) == [(("files_touched",), "list_type")]


def test_a_provided_proof_rejects_bytes_for_its_digest():
    proof = {
        "proof_type": "screenshot",
        "path": "proof.png",
        "commit_sha": "abc1234",
        "created_at": "2026-01-01T00:00:00Z",
        "slice_id": "s",
        "sha256": b"a" * 64,
    }

    assert _errors(models.ProvidedProof, proof) == [(("sha256",), "string_type")]


def test_a_provided_proof_keeps_extra_fields_open():
    proof = {
        "proof_type": "screenshot",
        "path": "proof.png",
        "commit_sha": "abc1234",
        "created_at": "2026-01-01T00:00:00Z",
        "slice_id": "s",
        "sha256": "a" * 64,
        "device": "iPhone",
    }

    assert _errors(models.ProvidedProof, proof) == []


# Importing the models switches pydantic's Logfire plugin off only while pydantic loads its
# plugins, then restores the variable. Only a fresh interpreter imports the models, so the test
# runs one and records every value written to the real process environment.

RECORD_IMPORT = """
import json, os

writes = []

class Recording(type(os.environ)):
    def __setitem__(self, key, value):
        if key == "PYDANTIC_DISABLE_PLUGINS":
            writes.append(value)
        super().__setitem__(key, value)

os.environ.__class__ = Recording
import automation.schemas.models
print(json.dumps([writes, os.environ.get("PYDANTIC_DISABLE_PLUGINS")]))
"""


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        (None, [["logfire-plugin"], None]),
        ("other", [["other,logfire-plugin", "other"], "other"]),
        ("__all__", [[], "__all__"]),
        ("1", [[], "1"]),
        ("true", [[], "true"]),
    ],
)
def test_importing_the_models_disables_logfire_only_while_plugins_load(before, expected):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "PYDANTIC_DISABLE_PLUGINS"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if before is not None:
        env["PYDANTIC_DISABLE_PLUGINS"] = before

    result = subprocess.run(
        [sys.executable, "-c", RECORD_IMPORT], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
    )

    assert (result.returncode, result.stderr, json.loads(result.stdout or "null")) == (0, "", expected)
