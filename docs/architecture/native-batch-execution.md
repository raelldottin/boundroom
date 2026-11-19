# SPEC-011: Native batch execution under Boundroom supervision

Status: **proposed; documentation only; not an implementation or approval to execute**  
Date: 2026-11-19  
Priority: **design decision / discovery; no change to the SPEC-001 P0 acceptance order**  
Related: [SPEC-001](control-plane.md), [SPEC-008](plugin-settings.md), [product roadmap](../product-roadmap.md), [effectiveness benchmark](../workflows/effectiveness-benchmark.md), [portable three-runner batch experience](portable-batch-experience.md), and [repository approval policy](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md).

**Cross-runner addendum:** [Portable batch experience](portable-batch-experience.md) describes the shared Claude/Codex/Hermes interface and an **offline-only** plan validator. It does not enable any native batch integration or authorize runtime execution.

## Context and product boundary

Claude Code's native `/batch` workflow is an example of a vendor investing in task decomposition, parallel agents, worktree isolation and pull-request creation. Exact syntax, availability, lifecycle, permissions and result APIs are version-specific **and must be verified against the pinned Claude Code build before integrating**. Do not assume an interactive slash command is a stable programmatic runner interface.

Boundroom should **not** build another generic agent scheduler simply to reproduce vendor-native execution. Its distinct job is to authorize bounded repository work, enforce scope and budget constraints independently of model reasoning, record reproducible evidence, and control review and continuation across Claude Code, Codex and Hermes. An executor's own planning or success summary never grants execution or merge authority.

Existing behavior is narrower than this proposal: `run_next.py` chooses one eligible slice, `run_agent.sh` launches one configured agent session, and agent-native subagents may exist inside that session without the supervisor controlling them. The operational queue is still the JSON slice queue unless a separately approved Kanban cutover occurs. A multi-slice `/batch` adapter, supervisor-managed parallel execution, batch-aware evidence collection and batch authorization **do not ship today**.

## Decision proposed for review

1. **Separate the control plane from execution strategies.** Keep canonical work and approval in the existing queue/approved board; preserve Boundroom's independent supervisor, versioned distribution, bounded context, evidence and clean-stop controls. Treat Claude native batch, Codex and Hermes as possible execution backends, not additional task stores or work-authority sources.
2. **Prefer vendor-native parallelism over a duplicate scheduler.** Pause speculative development of a second supervisor-owned parallel worker scheduler. Preserve the shipped one-slice path and the right to reject native batch if its safety properties cannot be verified. This is a scope-prioritization proposal, not permission to remove existing code or cancel the SPEC-001 P0 acceptance work.
3. **Introduce a strategy boundary only after contract tests.** A proposed adapter may expose capabilities such as read-only plan, execute an approved manifest, collect results and cancel, but a single-session runner is not automatically a safe batch strategy. No generic command-template escape hatch that bypasses the existing supervised runner profile, lock or policy.
4. **Keep authority outside the executor.** Native batch may suggest decomposition and implement approved units. Boundroom or a person must admit every work unit and independently evaluate the resulting commits, tests and provenance. The executor cannot approve, extend scope, chain another slice, merge, deploy, publish a release or advance canonical board state on its own.
5. **Do not make proprietary execution mandatory.** The existing Claude, Codex and Hermes single-slice paths remain supported. An optional Claude adapter must not become a requirement of the open, self-hostable target profile. No new dependency, license selection or model spend is authorized by this spec.

## Ownership and trust boundaries

| Concern | Authority | Not authoritative |
| --- | --- | --- |
| Task IDs, approval, priority, dependencies, claim/revision | Consumer queue today; approved Kanban adapter after cutover | A `/batch` plan or worker's suggestion |
| Allowed paths, maximum changed files, time/cost ceilings, runner profile, run lock | Boundroom supervisor and operator-approved policy | An agent prompt, default vendor configuration or a plugin skill |
| Candidate decomposition, implementation, branch/worktree output, proposed PRs | Selected native executor within admitted work | Executor's choice to start additional canonical slices |
| Base/head commits, touched paths, validations and proof | Independent supervisor evaluation and inspectable CI/review receipts | Agent-authored success text alone |
| Handoff disposition, canonical update, continuation | Boundroom's verified decision and authorized writer | Model's own self-assessment or wiki/memory content |
| Merge, releases, spend and consumer cutover | Separate human approval under `AGENTS.md` | Agent, batch planner or autonomous PR tool |

A model may have filesystem or network privileges outside a prompt's intended scope. Policy must be backed by tested filesystem/credential isolation and independent Git diff checks, not only natural-language instructions.

## Proposed admission lifecycle: plan -> authorize -> execute -> verify

1. **Pin and inspect.** Record repository/base commit, approved task IDs, queue or board revision, Boundroom pin, Claude Code build, runner profile and execution limits. Stop on stale claims or dirty/foreign repository state according to the current supervisor contract.
2. **Produce a read-only decomposition.** Obtain a machine-readable candidate plan with stable proposed unit IDs, dependencies, file scopes, expected validations and estimated resource needs. The planner must have no code-write, push or launch side effects in this phase. If the native command cannot prove plan-only behavior, **do not integrate it into the supervised path**; use a separately reviewed human plan or a test-only prototype instead.
3. **Admit units, not an unbounded request.** Map every proposed unit to an existing approved slice or require an explicit human-approved split and new queue/board authorization. Refuse unrecognized units, a changed revision, missing dependency proofs, overlapping edits without an approved integration strategy, requests beyond time/cost/agent ceilings, and new or widened file scopes. Re-approval is required for a changed manifest.
4. **Execute only the frozen manifest.** A tested adapter may launch work inside declared isolated worktrees with least-privilege credentials, specified runner identity and capped concurrency. It must not allow the executor to add fresh tasks, widen paths, spawn an unsupervised second batch, change execution permissions or self-chain the next canonical slice. If the tool does not offer enforceable controls and inspectable lifecycle, refuse native batch and fall back to the existing approved single-slice flow **without automatically widening privileges**.
5. **Collect one receipt per admitted unit.** Record the unit's base/head commits, worktree/branch/PR refs (where relevant), actual changed paths, test commands and outputs, resource usage when measurable, subprocess exit status, artifacts, and a terminal outcome. Preserve failures, incomplete results and missing evidence; never silently score them as success.
6. **Verify independently.** For every unit, derive changed paths from Git rather than trusting worker-reported files, check its approved scope/file budget and ancestry, run the appropriate supervisor-owned validations in a controlled worktree, and preserve proof levels and residual risks. A successful vendor test run is not automatically an independent validation. Failed or unverifiable units remain blocked/failed, and the batch never marks them `done`.
7. **Review and stop.** Write canonical state only through its approved writer after checks and revision revalidation; require a separate human reviewer for any merge. No automatic merge, deployment, release, consumer sync, board cutover or next batch. Resume or retry only after a new eligible, authorized supervisor decision.

### Proposed manifest and receipts (conceptual, not a schema in this PR)

An immutable approved batch manifest must bind `batch_id`, `repository`, `base_sha`, `queue_or_board_revision`, `runner/version/profile`, `approval_actor/time`, `budget/concurrency ceiling` and an ordered list of `{unit_id, slice_id, dependencies, allowed_paths, max_files_changed, required_validations, proof_requirement}`. It must have a stable digest covering all authority-bearing fields.

Each unit receipt must bind `batch_id`, `unit_id`, `slice_id`, `manifest_digest`, `base_sha`, `head_sha`, `worktree_or_branch`, `changed_paths`, `validation evidence`, `status`, `failure classification`, and traceable cost/usage if the runner exposes it. Store untrusted model text separately from supervisor-derived proof. A batch-level receipt aggregates unit verdicts but does not replace them.

Do not store credentials, full proprietary model transcripts or unreviewed Hindsight content in canonical receipts.

## Capability gate before using Claude `/batch`

A pinned, owner-approved disposable canary must establish **all** of the following before anyone writes a production adapter:

- A documented, stable way to obtain a read-only plan and to distinguish plan approval from execution; no scraping of interactive text as a security boundary.
- A way to constrain exactly which approved work units execute, with an enforceable concurrency/agent cap and no unexpected recursive dispatch.
- Verified per-unit worktree/branch identities and reliably inspectable base/head commits; no shared-worktree or out-of-band writes that evade scope checks.
- Bounded permissions, least-privilege tokens and explicit push/PR semantics compatible with `AGENTS.md`. A PR may be proposed; auto-merge or direct `main` writes are forbidden.
- Cancellation, timeout, partial-failure and interrupted-run behavior; reentry/idempotency and restart recovery without duplicate work or canonical updates.
- Reproducible independent validation and per-unit terminal outcomes. A missing receipt is a refusal, not a pass.

If any requirement is unavailable, record `unsupported` with versioned evidence and retain single-slice mode. A native batch integration is optional, never an implicit upgrade of all Claude runs.

## Negative-path acceptance matrix

| Scenario | Required result |
| --- | --- |
| Plan adds a task with no approved slice, or asks for a wider path | Refuse before any worker launch |
| Board/queue revision or base SHA changes after plan approval | Reject stale manifest and request re-authorization |
| Two workers edit overlapping paths without an approved merge plan | Refuse the conflicting batch or isolate/replan before launch |
| Worker starts a child batch, exceeds concurrency/budget or reaches forbidden credentials | Deny through tested process boundaries, stop and report violation |
| Worker commits out of scope, rewrites ancestry or changes supervisor-owned files | Per-unit verification fails; no canonical `done` |
| One PR succeeds while another fails or produces no receipt | Record distinct unit states; no all-success inference |
| PR merges, default branch moves or approval identity is missing | Refuse continuation; independent human review remains required |
| Cancellation, outage, timeout or process crash mid-batch | Recoverable partial state; no duplicate dispatch or false success |
| Missing/incompatible Claude version or plan-only API | Report unsupported; no automatic permission downgrade or unsupervised launch |

## Benchmark before implementation or rollout

Run **only after owner authorization of experiment, tool, model, provider and budget**, and in disposable repositories. This design/measurement work is eligible for the roadmap's benchmark exemption from P0 sequencing; production deployment is not.

Compare three measured modes on the **same independently specified tasks**: (A) current supervised single-slice sequence, (B) Claude-native delegation within one approved slice, and (C) proposed approved native batch, but only after the capability gate passes. Record exact task fixture, difficulty, base SHA, model and CLI versions, prompt/spec, allowed tools, permission profiles, concurrency, timeouts and cost ceiling. Do not confuse ProgramBench's program-reconstruction score with a multi-PR delivery benchmark unless the fixture and evaluator genuinely support both.

Primary outcome is **independently verified completed units per authorized task**, including the denominator. Secondary measures: end-to-end elapsed time, actual cost/tokens where available, PRs created versus PRs accepted, conflicts, failed units, review effort, retries and recovery, unauthorized-work attempts and provenance completeness. Distinguish tool/provider outages from implementation failures. Report medians and distributions when samples permit, not invented improvements or arbitrary winners.

Gate any adoption on **non-inferior safety and evidence completeness** plus a demonstrated improvement in verified delivery or operator burden. Publish fixtures, disaggregated receipts, exact pins, negative tests and an explicit owner go/no-go decision before enabling batch on any consumer.

## Sequenced delivery slices (proposed, not authorized)

| ID | Scope | Exit condition |
| --- | --- | --- |
| RA-STRAT-001 | Document/version-probe native batch behavior and its plan/permission/result contracts | Reproducible disposable observations; unsupported capabilities plainly identified; no production changes |
| RA-STRAT-002 | Define typed strategy/manifest/receipt boundaries and parser/validator fixtures | Deterministic accept/refuse tests with frozen authority fields; no new queue or dispatcher |
| RA-STRAT-003 | Optional Claude batch adapter behind explicit opt-in **only if RA-STRAT-001 proves controls** | End-to-end per-unit verification, cancellation and negative-path tests; unchanged single-slice default |
| RA-STRAT-004 | Fair isolated comparison against existing supervised mode and native delegation | Owner-approved budget, verified receipts, complete denominator and published go/no-go; no automatic rollout |

RA-STRAT-001/004 research may proceed in parallel with P0 by separate authorization; implementation, live task execution and consumer cutover remain behind existing safety and human-review gates. A positive benchmark never permits bypassing SPEC-001's Kanban/Hindsight/Wiki acceptance work.

## Out of scope for this PR

No changes to `run_next.py`, `run_agent.sh`, settings, plugin skills, queue semantics, branch protection or default runner; no Claude `/batch` invocation, batch scheduler, external credentials, benchmarks, releases or consumer repository changes. This PR establishes a reviewable proposal and backlog only.