# SPEC-010: An installed, version-pinned supervisor

Status: **proposed** in the pull request that adds it. Merging that pull request, after a person's approving review, accepts the plan. Each delivery slice below is still its own pull request with its own approval. A slice marked *owner gate* needs a person's decision before an agent starts it.

## Problem

Today a consumer such as Owlory vendors Boundroom: `Tools/boundroom-sync.sh` copies the supervisor, schemas, prompts, context builder, benchmark and their tests into the consumer, and a pull request records each update. Owlory carries 62 of Boundroom's files at its 2026-11-03 pin, and v0.5.0 adds 17 more. Each upgrade is a large pull request of code that isn't Owlory's.

The owner asked (2026-11-19) for Boundroom to be delivered the way [headroom](https://github.com/headroomlabs-ai/headroom) is: installed, not copied into each repository, and used from an agent through the plugin. Headroom ships packages (PyPI wheels, npm, signed container images) and agents reach it as an MCP server. Nothing of it is vendored into the repositories it works on ([SPEC-009](enterprise-readiness.md), section 4).

## Why the plugin carries no supervisor today

[SPEC-002](agent-plugin.md) ("Why a front end, not a second copy") keeps supervisor code out of the plugin. Boundroom's state is per repository: the queue, the handoffs, the repository's own validations. A plugin installs once per user. If the plugin carried the supervisor, a repository could be gated by code at the plugin's version while its queue sat at another, and nothing would say which version was in charge. Vendoring solves that by putting one version in each repository.

This spec keeps that guarantee by a different means: **the repository names the exact build that gates it, and nothing else runs.** Headroom doesn't need this, because it holds no per-repository state.

## Design

### 1. Boundroom becomes an installable package

`release.yml` builds a wheel, `boundroom-<version>-py3-none-any.whl`, from the same commit and the same files the sync delivers today. The wheel carries the supervisor, schemas, prompts, context builder and `run_agent.sh`, and declares pydantic (and the optional `telemetry` extra) as its dependencies. It exposes one command:

```
boundroom run-next [--single-slice] [--dry-run] ...
boundroom evaluate-handoff ...
boundroom status [--json]
boundroom settings
boundroom check
```

The wheel is published as a release asset beside the archive, with its `.sha256`, the same SLSA provenance attestation, and an entry in the release's CycloneDX SBOM. Before publishing, the release installs the wheel into a clean environment and runs `boundroom check` against Boundroom's example queue, so a wheel that can't run never ships.

The benchmark stays out of the wheel. It already ships apart from the supervisor (RA-ENT-013), and a consumer that wants it keeps syncing it.

### 2. The repository pins one build

A consumer in installed mode keeps its own files only: the queue, handoffs, `automation/runner-rules.md` and its own validations. Its lock names the build that gates it:

```json
{
  "delivery": "installed",
  "version": "0.6.0",
  "wheel_sha256": "<64 hex characters>",
  "repository": "raelldottin/boundroom",
  "remote": "https://github.com/raelldottin/boundroom.git"
}
```

A lock with `"delivery": "synced"`, or with no `delivery` field, is today's vendored mode, unchanged.

### 3. Each repository runs its own pinned version, with no global install

The plugin's skills don't call a globally installed `boundroom`. They read the lock and run the pinned wheel through uv, which the owner's machine already uses:

```
uvx --from "boundroom @ https://github.com/raelldottin/boundroom/releases/download/v0.6.0/boundroom-0.6.0-py3-none-any.whl#sha256=<wheel_sha256>" boundroom run-next --single-slice --repo-root .
```

uv refuses a wheel whose hash differs from the lock, and caches each version separately. RA-INST-005 proves both on the owner's uv (0.13.7) before any consumer relies on them. Two repositories pinned to different versions each run their own, on the same machine, at the same time. This is what removes SPEC-002's split: the version is chosen by the repository's reviewed lock, never by whatever plugin version the user installed.

The plugin itself still carries no supervisor code. It finds the lock, builds that command and runs it.

### 4. Fail closed

- A lock that names neither mode, or a repository that has both a vendored `automation/supervisor/` and `"delivery": "installed"`, is refused with a named reason. Nothing guesses.
- In installed mode, the supervisor compares its own version with the lock's `version` at start and refuses a mismatch. This catches a wheel run by hand outside the plugin.
- No fallback: if uv can't fetch or verify the wheel, the skill stops and says so. It never runs a vendored or bundled copy instead.

### 5. Each run records what gated it

The decision report, the handoff summary and telemetry's `service.version` name the version and the wheel's SHA-256 for every run. With vendored code, an audit read the consumer's git history. In installed mode, the lock's history says which build was approved, and each run's record says which build ran.

### 6. The supervisor finds the repository from an argument

Today `run_next.py`, `evaluate_handoff.py`, `operator_status.py`, `handoff_summary.py`, `settings_view.py` and `build_context.py` find the repository from their own location (`Path(__file__).parents[2]`). An installed package lives outside the repository, so each takes `--repo-root`, defaulting to the git top level of the current directory. In vendored mode, the default resolves to the same directory as today, so nothing changes for synced consumers.

### 7. Consumer CI and hooks

A consumer's CI and pre-push hook run `boundroom check` through the same `uvx --from …#sha256=…` command. In vendored mode they compared vendored files with the pin. In installed mode there are no vendored files, so `check` validates the queue, the runner rules and the lock against the pinned build. Before a lock change merges, the consumer's CI also runs `gh attestation verify` on the wheel the new lock names.

### 8. Upgrading and rolling back

An upgrade is a pull request that changes the lock's `version` and `wheel_sha256`, and nothing else of Boundroom's. It is small enough to review in full. Rolling back reverts that pull request. The agent prepares it with the values from the release's published `.sha256`; the consumer owner approves and merges it, as today.

## What changes in the rules

Some parts of the current policy survive unchanged:

- The plugin is still "skills and manifests only", with no supervisor, policy, queue or schema code in it, and no fallback to a bundled copy.
- No hook, MCP server or background process is added. Headroom reaches agents as an MCP server; Boundroom doesn't need one, because a skill calling a command does the job without a long-running process.
- A consumer still changes only through a pull request that its owner approves.

Others change, each in its own pull request:

- `AGENTS.md`, "Consumer Repositories": consumers receive Boundroom through the sync script **or** through a lock pin in installed mode. The agent may set `version` and `wheel_sha256` in an installed-mode lock, after showing the owner the values, as it may set `commit` and `imported_at` today.
- SPEC-002: the skills support both modes and refuse an ambiguous repository.
- SPEC-009, RA-ENT-014: decided on 2026-11-18 as "neither, for now". This spec reopens it for one channel only, a wheel attached to the GitHub release. PyPI and container images stay out.

## Delivery slices

| ID | Slice | Acceptance |
| --- | --- | --- |
| RA-INST-001 | This spec. | Merged after the owner's review. |
| RA-INST-002 | `--repo-root` for every supervisor entry point, defaulting to today's directory in a synced copy. | A synced consumer's checks pass unchanged; the supervisor runs against a repository it doesn't live in. |
| RA-INST-003 | *Owner gate (RA-ENT-014).* Package the supervisor: the `boundroom` command, a wheel in `release.yml` with its `.sha256`, provenance attestation and SBOM entry, and a clean-install smoke test before publishing. | A release carries a wheel that installs in a clean environment and passes `boundroom check`. |
| RA-INST-004 | The lock's `delivery`, `version` and `wheel_sha256` fields, the supervisor's version check, and the run record of version and wheel hash. | A mismatched or ambiguous repository is refused with a named reason; every decision report names the build. |
| RA-INST-005 | The plugin's skills run the pinned wheel through `uvx` in installed mode, and refuse an ambiguous repository. | Two scratch repositories pinned to different versions each run their own version on one machine. |
| RA-INST-006 | `boundroom check` for a consumer's CI and pre-push hook, with attestation verification on a lock change. | A consumer in installed mode has a CI check that fails on a wrong hash. |
| RA-INST-007 | Docs: the upgrade guide, the compatibility policy (how long vendored mode is supported), the workflow guide, and the `AGENTS.md` and SPEC-002 amendments. | `mkdocs build --strict` passes; the rules match the code. |
| RA-INST-008 | *Owner gate.* Move Owlory to installed mode: one pull request that removes Owlory's vendored Boundroom files, sets the lock and updates its `Makefile` targets and pre-push hook. | Owlory's checks pass; Owlory's owner approves and merges. |

RA-INST-002 is useful in both modes and can start once this spec merges. RA-INST-003 waits for the owner's decision on RA-ENT-014.

## Costs

- **The code that gates a consumer is no longer in its history.** The lock's history and each run's record of version and hash replace it. An auditor reads the release, not the consumer, for the code.
- **The first run of each version downloads the wheel.** uv caches it, so later runs start as fast as today. A machine that can't reach GitHub can still run a version already in its cache, and otherwise refuses.
- **Each harness needs the plugin.** Codex installs it from its own manifest. Hermes is not approved on the owner's company machine.
- **Two modes for a while.** The compatibility policy says how long vendored mode lasts. Retiring it is a later decision for the owner.

## Open decisions for the owner

1. RA-ENT-014: publish a wheel as a GitHub release asset (recommended), or keep "neither". PyPI and container images are not proposed.
2. How long vendored mode stays supported after installed mode ships.
3. Whether Owlory moves first (RA-INST-008), or a scratch consumer proves installed mode before Owlory does.
