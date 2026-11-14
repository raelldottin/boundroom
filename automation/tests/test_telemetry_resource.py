"""Resource attributes on every span (SPEC-006 RA-TEL-009).

Every span carries ``service.name=boundroom``, ``service.version`` and
``deployment.environment.name``, plus the operator's own ``resource_attributes``. Those come
from ``BOUNDROOM_TELEMETRY_RESOURCE_ATTRIBUTES`` or the operator's file, never from the
repository, and are refused with a named reason when they break the limits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation.schemas.models import RepositoryTelemetry
from automation.supervisor import telemetry, telemetry_settings
from automation.supervisor.telemetry_settings import resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV = "BOUNDROOM_TELEMETRY_RESOURCE_ATTRIBUTES"


def test_pairs_parse_and_are_checked():
    assert telemetry_settings.parse_resource_attributes("team.name=payments, cost_centre=cc-1") == {
        "team.name": "payments",
        "cost_centre": "cc-1",
    }
    assert telemetry_settings.parse_resource_attributes("") == {}


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (",".join(f"k{index}=v" for index in range(17)), "too_many"),
        ("Team=payments", "bad_key"),
        ("9team=payments", "bad_key"),
        ("team-name=payments", "bad_key"),
        ("t" * 65 + "=x", "bad_key"),
        ("service.name=other", "reserved_key"),
        ("deployment.environment.name=prod", "reserved_key"),
        ("boundroom.run_id=x", "reserved_key"),
        ("team.name=" + "v" * 257, "bad_value"),
        ("team.name", "not_key_value"),
        ("team.name=a,team.name=b", "duplicate_key"),
    ],
)
def test_each_refusal_names_its_reason_and_never_echoes_the_pair(raw, reason):
    with pytest.raises(ValueError) as caught:
        telemetry_settings.parse_resource_attributes(raw)
    message = str(caught.value)
    assert f"({reason})" in message
    assert "payments" not in message and "prod" not in message


def test_sixteen_pairs_and_a_256_character_value_are_allowed():
    pairs = {f"k{index}": "v" for index in range(15)} | {"long.value": "v" * 256}
    assert telemetry_settings.check_resource_attributes(pairs) == pairs


def test_a_control_character_in_a_value_is_refused():
    with pytest.raises(ValueError, match=r"\(bad_value\)"):
        telemetry_settings.check_resource_attributes({"team.name": "a\nb"})


def test_the_environment_and_the_operator_file_supply_them_and_a_bad_value_is_ignored_with_a_message(tmp_path):
    settings = resolve(environ={ENV: "team.name=payments"}, operator_file=None)
    assert settings.resource_attributes == {"team.name": "payments"}
    assert settings.sources["resource_attributes"] == "env"

    operator = tmp_path / "telemetry.json"
    operator.write_text(json.dumps({"resource_attributes": {"cost_centre": "cc-1"}}), encoding="utf-8")
    from_file = resolve(environ={}, operator_file=operator)
    assert from_file.resource_attributes == {"cost_centre": "cc-1"}
    assert from_file.sources["resource_attributes"] == "operator file"

    bad = resolve(environ={ENV: "Bad=secret-looking-value"}, operator_file=None)
    assert bad.resource_attributes == {}
    assert any(ENV in message and "(bad_key)" in message for message in bad.messages)
    assert not any("secret-looking-value" in message for message in bad.messages)


def test_reports_and_repr_show_keys_never_values():
    settings = resolve(environ={ENV: "team.name=payments-value"}, operator_file=None)
    assert settings.report()["resource_attribute_keys"] == ["team.name"]
    assert "payments-value" not in json.dumps(settings.report())
    assert "payments-value" not in repr(settings)


def test_the_repository_cannot_set_them():
    with pytest.raises(ValueError, match="can't label the operator's telemetry"):
        RepositoryTelemetry.model_validate({"resource_attributes": {"team.name": "x"}})


def test_every_span_carries_the_standard_and_the_operators_resource_attributes(tmp_path):
    settings = resolve(
        flags={"telemetry": "file", "telemetry_file_dir": str(tmp_path / "spans")},
        environ={ENV: "team.name=payments", "BOUNDROOM_TELEMETRY_ENVIRONMENT": "ci"},
        operator_file=None,
    )
    handle = telemetry.configure(settings, repo_root=REPO_ROOT, run_id="run-1", service_version="0.4.0")
    with handle.span("boundroom.run"):
        handle.mark("boundroom.select", slice_id="S-1")
    assert handle.flush()
    lines = [json.loads(line) for line in (tmp_path / "spans/run-1.spans.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for line in lines:
        assert line["resource"] == {
            "service.name": "boundroom",
            "service.version": "0.4.0",
            "deployment.environment.name": "ci",
            "team.name": "payments",
        }
