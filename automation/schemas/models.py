"""Canonical contract models for the supervisor queue and the agent handoff.

These Pydantic models are the *single source of truth*. The JSON Schema files next to
this module are generated from them by ``automation/schemas/generate.py``; they are
checked in only so non-Python consumers (editors, other languages, CI linters) can read
the contract without importing this package. ``make schemas-check`` — and
``automation/tests/test_schema_generation.py`` — fail if the checked-in files drift.

Validation happens at the door: ``policy.load_queue`` and ``policy.load_handoff`` parse
raw JSON through these models and then hand the *original* dictionaries to the rest of
the harness, which stays dict-based.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _without_logfire_plugin() -> None:
    """Keep pydantic from importing Logfire just because it is installed (SPEC-006 UD-1).

    Logfire registers a pydantic plugin, and pydantic loads every installed plugin the first time
    a model is built, so an installed telemetry extra would import the SDK on every run, with
    telemetry off. The plugin is switched off only while pydantic loads its plugins, which it does
    once per process, and the environment is restored, so no agent or child process inherits it.
    """
    key = "PYDANTIC_DISABLE_PLUGINS"
    previous = os.environ.get(key)
    if previous in ("__all__", "1", "true"):
        return
    os.environ[key] = ",".join(filter(None, (previous, "logfire-plugin")))
    try:

        class _LoadsPluginsOnce(BaseModel):
            pass

    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


_without_logfire_plugin()

SliceStatus = Literal["queued", "in_progress", "blocked", "deferred", "done", "failed"]
HandoffStatus = Literal["done", "blocked", "failed"]
RepoCleanStatus = Literal["clean", "dirty", "unknown"]
GitMirrorStatus = Literal["mirrored", "not-mirrored", "not-relevant", "not-checked"]
ProofLevel = Literal[
    "doc-only",
    "domain-tested",
    "build-tested",
    "running-app-smoke",
    "flow-verified",
    "screenshot-verified",
    "device-verified",
    "testflight-verified",
]

NonEmptyStr = Annotated[str, Field(min_length=1)]
StrList = list[NonEmptyStr]
NonEmptyStrList = Annotated[StrList, Field(min_length=1)]
Sha256 = Annotated[str, Field(min_length=64, max_length=64)]

# "Optional" in these contracts means the key may be *absent*, never that its value may
# be null: harness code reads optional keys with ``.get(key, "")``. Pydantic does not
# validate defaults, so a default of None makes the key optional without widening the
# accepted type.
Omittable = Field(default=None)


class Contract(BaseModel):
    """Contract documents reject unknown keys: a typo is a contract violation.

    ``strict`` keeps validation JSON-shaped: ``"3"`` is not an integer and ``true`` is
    not a ``1``, exactly as the hand-written schemas these models replaced required.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


# Why each field a repository's telemetry block may not hold is refused (SPEC-006 UD-7). A file a
# slice can edit must not decide where the agent's activity is sent, or turn the sending on.
REPOSITORY_TELEMETRY_REFUSALS = {
    "endpoint": "the repository can't choose a telemetry destination",
    "file_dir": "the repository can't choose a telemetry destination",
    "headers": "the repository can't hold telemetry credentials",
    "required": "the repository can't require telemetry",
    "resource_attributes": "the repository can't label the operator's telemetry",
}


class RepositoryTelemetry(Contract):
    """The queue policy's optional ``telemetry`` block, which can only reduce telemetry (SPEC-006)."""

    mode: Annotated[Literal["off"], Omittable]
    capture: Annotated[Literal["metadata"], Omittable]
    runner: Annotated[Literal["off", "metadata"], Omittable]
    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$"), Omittable]

    @model_validator(mode="before")
    @classmethod
    def _refuse_widening(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for field, reason in REPOSITORY_TELEMETRY_REFUSALS.items():
            if field in data:
                raise ValueError(f"telemetry.{field}: {reason} (SPEC-006 UD-7)")
        if data.get("mode", "off") != "off":
            raise ValueError("telemetry.mode: the repository can only turn telemetry off (SPEC-006 UD-7)")
        if data.get("capture", "metadata") != "metadata":
            raise ValueError("telemetry.capture: the repository can't raise capture above metadata (SPEC-006 UD-7)")
        if data.get("runner", "off") not in ("off", "metadata"):
            raise ValueError("telemetry.runner: the repository can't raise runner telemetry above metadata (SPEC-006 UD-7)")
        return data


class QueuePolicy(Contract):
    consecutive_autonomous_limit: Annotated[int, Field(ge=1)]
    handoff_timeout_seconds: Annotated[int, Field(ge=1)]
    agent_command_template: str
    supervisor_owned_paths: NonEmptyStrList
    telemetry: Annotated[RepositoryTelemetry, Omittable]


class SliceRecord(Contract):
    slice_id: NonEmptyStr
    title: NonEmptyStr
    status: SliceStatus
    priority: Annotated[int, Field(ge=0)]
    domain: NonEmptyStr
    allowed_paths: NonEmptyStrList
    required_validations: StrList
    depends_on: StrList
    max_files_changed: Annotated[int, Field(ge=1)]
    notes: str
    entry_condition: str = ""
    recommended_unblocker: str = ""
    manual_proof: StrList = Field(default_factory=list)
    required_proof_level: Annotated[ProofLevel, Omittable]


class SliceQueue(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, title="Automation Slice Queue")

    version: Annotated[int, Field(ge=1)]
    policy: QueuePolicy
    slices: list[SliceRecord]


class ContractStatusChange(Contract):
    contract: NonEmptyStr
    before: NonEmptyStr
    after: NonEmptyStr
    proof: StrList


class ProvidedProof(BaseModel):
    """Manual-proof records stay open: which extra fields a proof carries is the
    proving repository's business, not this contract's."""

    model_config = ConfigDict(strict=True)

    proof_type: NonEmptyStr
    path: NonEmptyStr
    commit_sha: NonEmptyStr
    created_at: NonEmptyStr
    slice_id: NonEmptyStr
    sha256: Sha256


class Handoff(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, title="Automation Handoff")

    slice_id: NonEmptyStr
    status: HandoffStatus
    summary: NonEmptyStr
    files_touched: StrList
    validations_passed: StrList
    validations_failed: StrList
    proof_level: ProofLevel
    missing_proof_levels: list[ProofLevel]
    contract_status_changes: list[ContractStatusChange]
    residual_risks: NonEmptyStrList
    recommended_next_slice: str
    recommended_next_reason: str
    repo_clean_status: RepoCleanStatus
    git_mirror_status: GitMirrorStatus
    dirty_paths_outside_scope: StrList
    timestamp: NonEmptyStr
    open_questions: StrList = Field(default_factory=list)
    provided_proofs: list[ProvidedProof] = Field(default_factory=list)
    verified_commit_sha: Annotated[NonEmptyStr, Omittable]


# (model, generated file) pairs: the only place the mapping is declared.
CONTRACTS: tuple[tuple[type[BaseModel], str], ...] = (
    (SliceQueue, "slice.schema.json"),
    (Handoff, "handoff.schema.json"),
)
