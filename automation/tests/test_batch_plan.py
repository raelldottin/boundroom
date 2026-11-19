"""RA-STRAT-002: offline, runner-neutral batch admission tests (no agents)."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from automation.supervisor.batch_plan import evaluate_candidate

BASE = "a" * 40
REPOSITORY = "example/project"


def queue_fixture() -> dict:
    slices = []
    for index, (slice_id, path, depends) in enumerate(
        (
            ("auth-core", "src/auth/", []),
            ("auth-tests", "tests/auth/", ["auth-core"]),
            ("auth-docs", "docs/auth/", ["auth-core"]),
        )
    ):
        slices.append(
            {
                "slice_id": slice_id,
                "title": slice_id,
                "status": "queued",
                "priority": index,
                "domain": "auth",
                "allowed_paths": [path],
                "required_validations": ["git diff --check"],
                "depends_on": depends,
                "max_files_changed": 5,
                "notes": "",
            }
        )
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 1,
            "handoff_timeout_seconds": 1800,
            "agent_command_template": "",
            "supervisor_owned_paths": ["automation/"],
        },
        "slices": slices,
    }


def queue_bytes(queue: dict) -> bytes:
    return json.dumps(queue, sort_keys=True).encode()


def plan_fixture(queue: dict) -> dict:
    return {
        "version": 1,
        "repository": REPOSITORY,
        "base_sha": BASE,
        "queue_sha256": hashlib.sha256(queue_bytes(queue)).hexdigest(),
        "max_concurrency": 2,
        "units": [
            {
                "unit_id": f"batch-{index}",
                "slice_id": record["slice_id"],
                "depends_on": record["depends_on"],
                "allowed_paths": record["allowed_paths"],
                "required_validations": record["required_validations"],
                "max_files_changed": record["max_files_changed"],
            }
            for index, record in enumerate(queue["slices"])
        ],
    }


def result(queue: dict, plan: dict, runner: str = "claude", max_concurrency: int = 2) -> dict:
    return evaluate_candidate(
        plan,
        queue,
        repository=REPOSITORY,
        observed_base_sha=BASE,
        queue_bytes=queue_bytes(queue),
        runner=runner,
        max_concurrency=max_concurrency,
    )


@pytest.mark.parametrize("runner", ["claude", "codex", "hermes"])
def test_same_three_unit_plan_is_candidate_for_all_runners(runner):
    queue = queue_fixture()
    outcome = result(queue, plan_fixture(queue), runner=runner)
    assert outcome == {
        "admissible_candidate": True,
        "runner": runner,
        "execution_started": False,
        "unit_count": 3,
        "refusals": [],
    }


def test_unauthorized_fourth_unit_is_refused_before_any_execution():
    queue = queue_fixture()
    proposal = plan_fixture(queue)
    fourth = copy.deepcopy(proposal["units"][0])
    fourth.update(unit_id="batch-4", slice_id="auth-secret", allowed_paths=["secrets/"])
    proposal["units"].append(fourth)
    outcome = result(queue, proposal)
    assert outcome["execution_started"] is False
    assert "unknown_slice" in outcome["refusals"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p.update(base_sha="b" * 40), "stale_base_sha"),
        (lambda p: p.update(queue_sha256="0" * 64), "stale_queue_digest"),
        (lambda p: p.update(repository="another/project"), "repository_mismatch"),
        (lambda p: p["units"][0].update(allowed_paths=["src/"]), "scope_mismatch"),
        (lambda p: p["units"][0].update(required_validations=[]), "validation_mismatch"),
        (lambda p: p["units"][1].update(depends_on=[]), "dependency_mismatch"),
        (lambda p: p["units"][0].update(max_files_changed=999), "scope_mismatch"),
        (lambda p: p["units"][0].update(required_proof_level="device-verified"), "proof_mismatch"),
        (lambda p: p["units"][0].update(allowed_paths=["../secrets/"]), "unsafe_scope"),
        (lambda p: p["units"][0].update(allowed_paths=["src/auth/../"]), "unsafe_scope"),
    ],
)
def test_changed_authority_fields_are_refused(mutate, expected):
    queue = queue_fixture()
    proposal = plan_fixture(queue)
    mutate(proposal)
    outcome = result(queue, proposal)
    assert outcome["execution_started"] is False
    assert outcome["admissible_candidate"] is False
    assert expected in outcome["refusals"]


def test_concurrency_must_remain_under_operator_ceiling():
    queue = queue_fixture()
    assert "concurrency_ceiling_exceeded" in result(queue, plan_fixture(queue), max_concurrency=1)["refusals"]


def test_plan_rejects_duplicate_units_and_slices():
    queue = queue_fixture()
    proposal = plan_fixture(queue)
    proposal["units"][1]["unit_id"] = proposal["units"][0]["unit_id"]
    proposal["units"][2]["slice_id"] = proposal["units"][0]["slice_id"]
    outcome = result(queue, proposal)
    assert "duplicate_unit_id" in outcome["refusals"]
    assert "duplicate_slice_id" in outcome["refusals"]


def test_unmet_dependency_is_refused_even_if_subset_looks_valid():
    queue = queue_fixture()
    proposal = plan_fixture(queue)
    proposal["units"] = proposal["units"][1:]  # auth-core is still queued
    assert "unmet_dependency" in result(queue, proposal)["refusals"]


def test_dependency_completed_outside_batch_is_accepted():
    queue = queue_fixture()
    queue["slices"][0]["status"] = "done"
    proposal = plan_fixture(queue)
    proposal["units"] = proposal["units"][1:]
    assert result(queue, proposal)["admissible_candidate"]


def test_overlapping_scopes_fail_conservatively():
    queue = queue_fixture()
    queue["slices"][2]["allowed_paths"] = ["src/auth/login/"]
    proposal = plan_fixture(queue)
    assert "overlapping_scopes" in result(queue, proposal)["refusals"]


def test_nonqueued_slice_and_invalid_queue_fail_closed():
    queue = queue_fixture()
    queue["slices"][0]["status"] = "in_progress"
    assert "slice_not_queued" in result(queue, plan_fixture(queue))["refusals"]
    queue["slices"][0]["status"] = "unknown"
    assert result(queue, plan_fixture(queue))["refusals"] == ["invalid_queue_contract"]


def test_unknown_runner_and_bad_plan_schema_fail_closed():
    queue = queue_fixture()
    proposal = plan_fixture(queue)
    assert "unsupported_runner" in result(queue, proposal, runner="other")["refusals"]
    proposal["extra"] = "no"
    assert result(queue, proposal)["refusals"] == ["invalid_plan_contract"]
