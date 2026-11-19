# Portable batch experience (SPEC-011 companion)

Status: **experimental contract and tests only** (2026-11-19). This is an addendum to [SPEC-011](native-batch-execution.md), not a second execution authority or an implementation of the batch runner.

## What an operator should eventually see

One portable `boundroom batch` experience in Claude Code, Codex and Hermes, with the same flow:

1. **Plan:** generate a read-only proposed decomposition from a complete specification; show dependency order, file boundaries, tests, budget and proposed executor.
2. **Check:** bind that proposal to approved, current Boundroom queue/board slices; refuse stale or unauthorized units before any runner starts.
3. **Approve:** require a separately recorded human authorization of the exact immutable manifest, including its digest, task revisions, budget, runner permissions and concurrency.
4. **Execute:** use the selected harness's native capabilities *only if* a pinned build exposes enforceable isolation, lifecycle, cancellation and machine-readable result evidence; otherwise use the established one-slice supervisor.
5. **Verify and review:** independently inspect each unit's commits, paths, validations and provenance; never automatically approve PRs, merges, releases, board changes or the next batch.

A future `boundroom-batch` skill would be a thin front-end to an **installed or consumer-pinned supervisor**, in accordance with SPEC-002 and any separately approved distribution transition. A skill alone is not an enforcement boundary. No new mandatory service, scheduler, queue, plugin code copy, credentials, proprietary provider or deployment is authorized.

The eventual command surface is illustrative, **not currently installed**:

```text
boundroom batch plan "Migrate authentication" --runner auto
boundroom batch check --plan proposal.json --runner claude
boundroom batch approve --plan proposal.json  # explicit human review
boundroom batch run --approved-manifest manifest.json
boundroom batch review --batch-id <id>
```

Do not add `batch run`, `approve` or `auto` execution until they are properly specified, versioned, implemented and accepted. Do not confuse Claude Code's interactive `/batch` interface with a documented noninteractive API.

## Three runner strategies, one contract

| Harness | Existing primitives to probe | Unverified integration question |
| --- | --- | --- |
| Claude Code | Interactive `/batch`; subagent/team delegation; Git worktrees | Can a pinned noninteractive invocation produce a side-effect-free plan and per-unit inspectable receipts under Boundroom's sandbox and budget ceiling? |
| Codex | CLI `exec`, multi-agent delegation, SDK/app-server interfaces | Which pinned interface can supervise isolated units with deterministic subprocess lifecycle, per-unit git refs and tool restrictions? |
| Hermes | `delegate_task`, Kanban and swarm workflows | Can a pinned adapter dispatch only Boundroom-approved units without letting Hermes's own board become a second canonical task source? |

All three are **capability probes, not accepted adapter contracts**. Runner-native delegation may happen inside an existing authorized slice today; that is not proof of supervisor-controlled multi-slice batch execution.

## Shipped-by-this-PR prototype: read-only candidate gate

`automation/supervisor/batch_plan.py` defines a strict candidate model with:

- `version`, `repository`, `base_sha`, `queue_sha256` and `max_concurrency`.
- A non-empty list of `units`, each with `unit_id`, existing `slice_id`, exact `depends_on`, `allowed_paths`, `max_files_changed`, `required_validations`, optional `manual_proof` and `required_proof_level`.
- **No runner in the plan**: the same JSON proposal can be checked for Claude, Codex or Hermes. The caller's runner selection is a separate input and cannot grant work authority.

Example read-only check from a repository with a prepared JSON candidate (there is **no planner** yet):

```shell
python3 -m automation.supervisor.batch_plan \
  --repo-root . \
  --repository owner/repo \
  --queue automation/queue/slices.json \
  --plan /tmp/batch-proposal.json \
  --runner codex \
  --max-concurrency 2
```

This reads the actual queue bytes and checks Git HEAD. It refuses a changed queue digest or base commit, unknown/not-queued slice, changed file scope/budget/validation/dependency/proof, unsafe path, duplicated unit, unmet dependency, overlapping scopes and concurrency above the operator-supplied ceiling. Output is deterministic JSON with `admissible_candidate`, `refusals`, `runner`, `unit_count` and **`execution_started: false`**. Exit 0 means only *candidate metadata is consistent*, never that a person approved anything or that a worker executed. The caller-provided concurrency ceiling is not a system-wide trust boundary.

The gate does **not** authenticate an approver, verify a signed/frozen manifest, claim a board task, isolate a process, enforce a model budget, schedule a worker, replay validations, update the canonical queue or collect runtime receipts. No PR or Git changes are made by the CLI. It deliberately refuses even dependency-ordered overlapping file scopes; that limitation can only change after a reviewed isolation and merge contract.

The existing `run_next.py --single-slice` path and plugin behavior are unchanged. Agent-specific adapters remain **deferred**, as are human-approval receipts and per-unit execution evidence.

## Acceptance proof in this stacked PR

`automation/tests/test_batch_plan.py` supplies an identical authorized three-slice plan to the Claude, Codex and Hermes modes and proves that all three receive the same positive candidate result with **no execution**. It also injects a fourth unknown slice, scope widening, stale revisions, missing dependencies, changed validations/proofs, duplicate units, overlapping scopes, blocked work and budget overruns, asserting deterministic fail-closed outcomes.

These are **offline contract tests**, not a live canary or evidence of native `/batch`, Codex parallel-agent or Hermes swarm compatibility. They do not authorize paid experiments or close SPEC-001 P0 acceptance.

## Next slices (not started)

- **RA-STRAT-001A:** confirm pinned build/version, plan-only behavior, tool limits, result handles and cancellation for each harness in disposable environments; explicitly mark unsupported paths.
- **RA-STRAT-002A:** finalize approved-manifest digest, reviewer identity and revision-bound authorization; emit portable JSON Schema and test exact provenance. This should reuse the canonical contract generator when promoted from the prototype.
- **RA-STRAT-003A:** build a single reusable `boundroom-batch` plugin skill and opt-in adapter for whichever runner first proves enforceable boundaries. All adapters must conform to the same supervisor-owned contract.
- **RA-STRAT-004A:** run separate, owner-approved, cost-capped comparison lanes with fixed tasks and independent scoring. Compare verified delivery and operator effort, not simply number of agents or PRs.

**No-Go:** Any runner that cannot provide a trustworthy plan/execution separation, worktree identity, cancellation and per-unit evidence remains on the existing one-slice path. Parallelism is an optimization, not authorization.
