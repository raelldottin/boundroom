# Symphony evaluation: supervised, isolated, recoverable agent execution

**Status:** Proposed architecture and research backlog (2026-11-16). This document is **not** approval to implement a scheduler, start a service, enable a model/provider, spend funds, create a production board, merge code, or deploy to a consumer. Acceptance requires a reviewed architecture decision and the independent human authorizations in [AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md).

**Scope:** Compare [OpenAI Symphony](https://github.com/openai/symphony) with Boundroom and define a safe, incremental evaluation track. Sources: [Symphony specification](https://github.com/openai/symphony/blob/main/SPEC.md), [experimental Elixir implementation](https://github.com/openai/symphony/blob/main/elixir/README.md), [Boundroom product roadmap](../product-roadmap.md), [SPEC-001](control-plane.md), [SPEC-004](agent-agnostic-board.md), [Kanban cutover](kanban-cutover.md), [SPEC-008](plugin-settings.md), [SPEC-009](enterprise-readiness.md) and [SPEC-010](installed-supervisor.md).

## 1. Decision proposed

**Study Symphony as a reference for an *optional* execution lifecycle, not a replacement for Boundroom and not a second dispatcher.** Boundroom remains responsible for authorization, scoped slice selection, dependency eligibility, claims, validation replay, review, handoff provenance, and the decision to stop. Runner integrations remain agent-agnostic (Claude Code, Codex and Hermes), with capability-specific adapters.

Symphony contributes useful operating patterns: polling and reconciliation, isolated workspaces, explicit active-run states, retry/backoff, streaming agent protocols, in-repository workflow configuration, and operational views. Its reference service is an experimental Elixir/OTP prototype intended for trusted-environment evaluation, not a production-ready component to drop into Boundroom.

The *proposed* boundary is:

1. **Canonical work state:** The existing JSON queue (the default) **or** one selected Boundroom board backend, Hermes Kanban or the local SQLite board (`--board-backend` defaults to `hermes`). No new canonical tracker, mirrored issue queue or dual writer. External trackers (including GitHub Issues) may later have *read-only* discovery adapters, but none may authorize execution by themselves.
2. **One execution authority:** The existing deterministic supervisor admits slices, owns claim and continuation decisions, and calls an optional worker/session lifecycle module. Any long-running loop is an invocation mode of this authority, not an independent scheduler or daemon with its own authorization logic.
3. **Execution:** A worker runs an explicitly approved slice in an isolated workspace, under the approved runner profile and scoped credentials. Workers cannot approve successors or request broader privileges.
4. **Verification and promotion:** Independent checks, bounded diffs, immutable handoffs, versioned pins, CI and a person's separately recorded approvals continue to gate board completion, merges, releases, spending, schedules and deployments.
5. **Knowledge:** Hindsight retains only authorized project-scoped evidence; Swarm Wiki publishes source-attributed, versioned, read-only views. Neither controls dispatch.

**Current baseline:** The slice supervisor, queue, portable plugin, validation replay and release governance are shipped. The local board interface and Claude Code canary have bounded evidence; the Codex live canary, combined Hindsight/Swarm Wiki acceptance and full control-plane cutover remain incomplete. This is a proposed architecture, not a claim that Symphony-inspired supervision exists.

**Refreshed 2026-11-19, against v0.5.0 and the v0.5.1 fixes on `main`:** Since the proposal was written:

- v0.5.0 shipped, and the v0.5.1 fixes are on `main`.
- The default Claude runner is now the SPEC-008 `bounded` profile: the slice's own allowlist, a sandbox and a budget cap.
- Telemetry has metrics, and operator status reports them (#240).
- A claimed slice whose handoff can't be read fails, so it isn't left claimed (#258).
- A run that writes any file beside its handoff in a supervisor-owned directory fails (#271).

[SPEC-010](installed-supervisor.md) proposes an installed, version-pinned supervisor with `--repo-root`. The per-user run lock it records bears on the workspace and concurrency items below. None of this changes the proposal's boundary.

## 2. Fit and gap assessment

| Symphony pattern | Boundroom baseline | Proposed improvement | Constraints |
| --- | --- | --- | --- |
| Repeated tracker polling and active-run reconciliation | Invocation-oriented `run_next.py`, JSON queue default; local/Hermes board available | Optional supervisor-owned watch/dispatch loop; reconcile before admission | No second scheduler; owner must explicitly approve a runtime cutover/schedule |
| Per-issue isolated workspace | Runner currently launches at the consumer repository root (`run_next.py` runs the agent command with `cwd=repo_root`); the bounded profile's sandbox limits its writes, and the supervisor fails a run that changes files outside its paths or beside its handoff (v0.5.1) | Per-slice Git worktree or temporary checkout with explicit containment, cleanup and recovery | No dirty main worktree, out-of-scope writes, cross-repo secrets, or untracked artifact loss |
| Explicit claim, stall, retry and backoff states | Bounded stop rules, claim checks and classification already exist; one supervised run per user at a time (`runner_profile.run_lock`) | Persistent run attempt identity; classify/reconcile transient infrastructure failures | Never auto-retry policy, approval or scope refusals; no duplicate or stale claim |
| Codex App Server protocol | Codex/Claude/Hermes CLI runner selection; fake-runner tests | Opt-in Codex session adapter with structured events, usage and blocking signals | Do not replace other runners; refuse incompatible versions or unapproved capabilities |
| Versioned `WORKFLOW.md` plus reload | `AGENTS.md`, queue/overlay, prompt fragments and typed settings | Evaluate a schema-validated compatibility view, not a second source of truth | Config reload cannot widen permissions or silently approve additional work |
| Runtime dashboard and counters | Operator status, handoff summary, opt-in telemetry/metrics | Read-only running/blocked/retrying/review, durations and usage view | Local/offline by default; redact content; dashboard does not mutate authority |
| Remote worker support | Local supervised invocation; remote profile is not proven | Later opt-in worker host with isolation and attested receipts | No default remote credentials, fleet deployment or cloud egress |
| Automatic continuation on active issues | Each slice ends with a handoff the supervisor evaluates. Below `consecutive_autonomous_limit` (2 in the example queue), `run_next` then starts the next eligible slice itself (`policy.next_slice_eligibility`); `--single-slice` stops after one. On a board, a dependent task stays `todo` until a person approves its parent | **Do not port as-is**; optionally continue *within* the same approved slice under bounded turns | Never chain into another slice on an agent's assertion or tracker status alone |

## 3. Invariants

- **Authorization:** An issue title, comment, tag, model response, workflow instruction, Hindsight reflection or Wiki page is untrusted input, not permission. The supervisor checks an explicit approved slice and current board revision/queue state before launch and again before state mutation.
- **Separation of duties:** Workers can author branches, tests, evidence and pull requests. A person other than the author approves merges at the exact reviewed head, release tags, board approvals outside canaries, consumer sync merges, spending, cutovers, deployments, schedules and licenses; see [AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md).
- **Single writer:** The board/queue is authoritative; state updates are conditional on the current run identity, revision, lease and allowed transition. An adapter or dashboard must not bypass the board backend. A claimed slice cannot be concurrently dispatched a second time.
- **Fail closed:** Scope, diff budgets, validation ownership, egress policy, missing/expired claims, stale configurations, absent credentials, denied approvals and invalid evidence produce blocked/refused outcomes with reasons, not a broader fallback or hidden continuation.
- **Recoverability:** Reconciliation on startup and during operation must not convert an unknown prior outcome into success. Retain durable receipts; never erase a dirty worktree, partial artifact or forensic evidence merely to clear a dashboard.
- **No hidden services:** No background process, new task database, new worker pool, remote host or network listener is enabled by merging this design. Their future introduction requires an explicit architectural and operator authorization.
- **Portable/local-first:** Existing GPL-2.0-only distribution and release pins, multi-harness parity, telemetry consent, offline operation and consumer-controlled upgrades remain in effect.

## 4. Execution and recovery contract to evaluate

### 4.1 State machine

Model each attempt separately from the task's canonical status:

`eligible -> claim_preflight -> claimed -> workspace_prepared -> running -> verifying -> review_requested`

Failure/recovery edges: `running -> stalled | timed_out | transient_failed | blocked_by_input | policy_refused | canceled`. A *transient_failed* attempt may enter `retry_pending` only if owner-approved policy, retry budget, still-valid lease/revision and original slice authorization all permit it. `review_requested` waits for human review; it is not `done`.

Persist a stable tuple: project/consumer ID, backend, task ID, slice ID, approval/overlay revision, attempt ID, claim/lease ID, base/head commits, workspace ID, runner/version, workflow digest, handoff digest and trace ID. Record transition actor, reason and timestamp. Make completion idempotent. The tracker/board may display a simplified state; the supervisor is the authority for attempt transitions.

### 4.2 Workspaces and concurrency

- Put each approved slice in an isolated worktree/checkout under an explicitly configured root outside the consumer's main worktree; validate canonical paths and symlink traversal before each launch.
- Limit filesystem, environment, GitHub App permissions, tool permissions and network access to the slice. Do not pass high-scope tracker tokens into a child; use host-side narrowly scoped API operations and keep secrets out of prompts, logs and handoffs.
- Default to **one running slice per consumer** until independent tests establish that allowed paths, shared generated files, Git branches, board claims and validations cannot interfere. A later worker pool needs global/per-consumer/per-runner concurrency and budget ceilings.
- Clean a workspace only after recording immutable verification receipts and demonstrating safe retention/recovery rules. A crash or terminal board transition must not silently destroy uncommitted work.

### 4.3 Retries, stalls and interruption

- Heartbeat or streaming activity must distinguish a slow reasoning turn from a dead session; require a total runtime ceiling as well as a stall timeout and clearly defined cancellation semantics.
- Use bounded exponential backoff with jitter for classified rate limits, temporary network failures and crashed processes; respect provider backoff guidance and an owner-approved maximum attempt count/cost. No unbounded loop.
- Never retry on invalid scope, stale authorization, changed approval revision, failed independent tests, denied spend, unknown secret isolation, or request for human input. Show a blocked or failed state and preserve the workspace.
- On restart, reconcile the canonical board/queue and durable attempt receipts **before** dispatch. Reject expired/superseded claims and duplicate work; test the race between a gateway claim, lease expiry, and supervisor completion.
- If the configured tracker or memory is unavailable, no new authority is inferred. Preserve already-running sessions only under the explicitly approved failure policy; otherwise stop safely.

### 4.4 Runner adapters and configuration

- Specify a small capability contract for `start`, `stream/status`, `cancel`, `usage`, `blocked_input` and `result` without forcing Claude or Hermes to impersonate Codex.
- Evaluate Codex App Server as an *optional* adapter only after approved-machine canary and version/protocol pinning; retain CLI adapters and their existing safety profiles.
- Map any Symphony-style `WORKFLOW.md` into the existing queue/overlay, `AGENTS.md`, `automation/runner-rules.md` and typed settings. If multiple sources disagree, explain precedence and refuse privilege widening. Invalid hot reload must not start new work under guessed defaults.
- A configuration change affecting runner, sandbox, authentication, dispatch cadence, concurrency or total spend is a separately approved change. Runtime reload may use only an already approved, immutable effective profile.

### 4.5 Operational status

Extend the existing read-only operator status and opt-in metrics rather than building a second control plane. Show task/slice/attempt identity, eligibility/refusal reason, session state, claim expiry, heartbeat/stall age, retry budget, review gate, base/head SHA, runner/version, tokens/cost when provided by the runner, and evidence links. Label unknown usage as **unavailable**; avoid fabricated totals. Content traces remain opt-in, redacted and offline-capable; no external telemetry requirement.

## 5. Incremental delivery track (proposed, not admitted)

| ID | Priority | Deliverable | Dependencies / gate | Acceptance evidence |
| --- | --- | --- | --- | --- |
| **RA-SYM-001** | P0 research | Architecture decision record, security/licensing review and conformance mapping | Existing SPEC-001, SPEC-004, SPEC-008, SPEC-009; human decision | Explicit chosen/no-go boundary, non-goals, no second writer and source ledger |
| **RA-SYM-002** | P0 safety prototype | Opt-in per-slice workspace isolation | Approved disposable consumer, runner and filesystem scope | Path-traversal/dirty-worktree/secret tests, base/head receipts, safe cleanup and rollback |
| **RA-SYM-003** | P0 recovery prototype | Durable attempt/claim/reconciliation and bounded retry design | Existing board/queue contracts and RA-SYM-002 | Crash/restart/duplicate/gateway-race/TTL/429 tests; no unauthorized second starts |
| **RA-SYM-004** | P1 adapter | Opt-in structured Codex App Server adapter | Protocol/version pin, approved machine and explicit spend | One bounded live canary, streaming/usage/blocked-input parity, no bypass of CLI runner policy |
| **RA-SYM-005** | P1 contract | Workflow configuration compatibility view and validated reload policy | RA-SYM-001; config precedence decision | Invalid-reload and privilege-escalation tests; unchanged source of authority |
| **RA-SYM-006** | P1 visibility | Read-only session dashboard/API or CLI snapshot | Durable event schema; existing telemetry controls | No dispatch/write permissions; offline/redaction tests; receipts match canonical state |
| **RA-SYM-007** | P2 research | Distributed workers / parallel dispatch feasibility | H2/H3 golden-path and RA-SYM-002/003 proofs; separate owner authorization | Host identity and secret isolation, fencing, kill/recovery, cost/egress/rollback matrix |

**Sequencing:** RA-SYM-001 is documentation and may proceed without enabling anything. RA-SYM-002/003 can be prototyped in disposable, explicitly authorized environments in parallel with control-plane work when they harden single-slice operation. **No continuous dispatch, fleet enablement, real board cutover or distributed worker rollout until the existing H2 per-component acceptance, H3 golden-path proof and an additional explicit owner-approved decision.** Benchmarks remain a separate authorized parallel track.

## 6. Negative tests and benchmarks

Minimum evidence for any supervised-mode rollout:

| Scenario | Required result |
| --- | --- |
| Two dispatchers or restarted process target same slice | At most one valid claim/active runner; loser leaves an observable refusal |
| Stale revision, expired/reclaimed lease, changed task approval | No dispatch or write-back based on stale authority |
| Failed validation, out-of-scope path, dirty worktree, missing handoff | Stop/refuse; no automatic promotion or hidden next task |
| User approval requested or CI pending | Mark awaiting review; no merge, release, deployment or successor launch |
| Worker crash, stalled stream, provider 429, network outage | Bounded, classified recovery only where policy explicitly permits; durable evidence |
| Malicious task body, symlink escape, high-scope token | No permission escalation, unexpected egress or secret exposure |
| Bad workflow reload or offline mode | Previous approved config remains; no unapproved launches; operator-visible error |
| Restart between commit and board write | Reconcile receipts and claim before action; no duplicate or false success |

**Safety targets:** zero unauthorized worker starts, zero duplicate active claims, zero approval bypasses, zero out-of-scope writes or cross-project secret leaks in the negative-path suite. Count test cases and attempts and attach evidence; a target is not a result.

**Effectiveness experiment:** With independently authorized benchmark spend and identical pinned task fixtures, compare existing one-slice baseline to isolated/recoverable mode on: independently verified completions, policy refusal accuracy, invalid/duplicate attempts, recovery outcome, time to review, reviewer interventions, wall-clock time, tokens and cost, provider/model failures, and workspace cleanup. Report distributions, exact sample sizes, model/runner versions, hardware and raw receipts. Do not claim a productivity improvement from Symphony's published anecdotes.

## 7. License and supply-chain boundary

Symphony is [Apache-2.0](https://github.com/openai/symphony/blob/main/LICENSE); Boundroom's original/distributed files are [GPL-2.0-only](https://github.com/raelldottin/boundroom/blob/main/COPYING). Apache-2.0 is generally **not compatible with GPL-2.0-only for incorporation of Apache-licensed code into a combined GPLv2-only derivative** (see the [Apache Foundation's GPL compatibility guidance](https://www.apache.org/licenses/GPL-compatibility.html)). This document describes patterns; it does **not** import or adapt Symphony source. Any subsequent code reuse, linking, copying of configuration/templates, or distribution plan needs an explicit license compatibility assessment, provenance and notices, plus the owner's separate license decision if proposed. Independent implementation of architectural concepts does not grant a blanket legal clearance.

## 8. Decision gates and references

A reviewer must explicitly decide: (1) whether an optional long-running execution mode belongs within Boundroom's one-supervisor architecture, (2) whether the JSON queue should remain single-slice-only, (3) which isolation boundary is acceptable, (4) persistent attempt/lease and recovery authority, (5) approved runner protocols, and (6) whether to pursue any remote worker profile. Missing decisions are **NO-GO** for implementation.

Sources, inspected 2026-11-16:

1. [OpenAI Symphony README](https://github.com/openai/symphony/blob/main/README.md) — purpose and prototype warning.
2. [Symphony language-independent SPEC.md](https://github.com/openai/symphony/blob/main/SPEC.md) — issue orchestration, reconciliation, workspace safety, workflow reload, protocols and security model.
3. [Symphony Elixir implementation README](https://github.com/openai/symphony/blob/main/elixir/README.md) — available adapters, blocking semantics and local implementation status.
4. [Boundroom product roadmap](../product-roadmap.md) — shipped versus bounded-test versus not-accepted capabilities and sequencing.
5. [Boundroom SPEC-004](agent-agnostic-board.md) and [Kanban cutover](kanban-cutover.md) — canonical board, claims, review and existing fail-closed writer.
6. [Boundroom AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md) and [enterprise-readiness SPEC-009](enterprise-readiness.md) — human authorization, audit trail, telemetry and release controls.
7. [Apache Foundation licensing guidance](https://www.apache.org/licenses/GPL-compatibility.html) — Apache 2.0 / GPLv2 compatibility caveat.
