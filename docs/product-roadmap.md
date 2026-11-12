# Product roadmap: Boundroom

**Snapshot:** 2026-11-10 (original capability audit: 2026-11-03) | **Planning status:** proposed product priorities; not an execution authorization | **Current published release:** [v0.3.0](https://github.com/raelldottin/boundroom/releases/tag/v0.3.0) (2026-11-12)

**Product owner:** repository maintainer. **Delivery model:** reviewed pull requests, exact pinned releases, explicit consumer adoption. **Primary technical contract:** [SPEC-001](architecture/control-plane.md), [SPEC-002](architecture/agent-plugin.md), and [repository policy](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md).

This is the **product roadmap and source of truth for priorities, outcomes, sequencing and readiness**. The [earlier technical roadmap](roadmap.md) preserves RA-CP-001 through RA-CP-005 implementation history and their detailed acceptance contracts. A green test, prototype or synthetic canary is not, by itself, proof that an end-to-end system is operating on a production project.

## 1. Product thesis

**Boundroom makes AI-assisted repository work bounded, reproducible and reviewable.** A person or authorized control plane chooses a narrow slice. The deterministic supervisor enforces eligibility, scope, validation, evidence and a clean stop. The runner can be Hermes, Claude Code or Codex; neither an agent's reasoning nor a memory or wiki summary grants execution authority.

**The job to be done:** “I need an AI agent to make one well-scoped repository change, show what it touched and verified, stop safely, and let me decide what happens next—across my different repositories and agent tools.”

The long-term product is an **open, self-hostable agentic software-delivery control plane** that connects:
- **Hermes Kanban** for human-visible canonical work state and approvals;
- **Hindsight** for isolated, durable, source-attributed project memory;
- **Swarm Wiki** for deterministic, versioned knowledge publications; and
- the **Boundroom supervisor** as the sole execution and continuation authority.

These are complementary responsibilities, not four competing task stores. The plugin is a thin operator interface to a *consumer's synced copy*, not an additional supervisor.

**New product extension (2026-11-09):** an opt-in **Discord Knowledge Loop** captures approved conversations and evidence, organizes them in Hindsight and Swarm Wiki, answers permitted questions in Discord, shares reviewed knowledge, and evaluates retrieval quality. It is a **separate, read-only-with-respect-to-execution knowledge service**; it cannot dispatch slices, change queues or Kanban tasks, run commands, edit repositories or bypass supervisor policy. See [SPEC-003](architecture/discord-knowledge-loop.md) and [RA-KNOW-000](https://github.com/raelldottin/boundroom/issues/173). A locally deployable open-source runtime is a **target requiring proof**, not a claim satisfied merely because the repository is public or GPL-licensed.

### Who it serves

| Persona | Primary workflow | Definition of value |
| --- | --- | --- |
| Solo maintainer / independent builder | Pin a release, adopt the supervisor, delegate one bounded change and review the handoff | Less babysitting without losing control of the repository |
| Maintainer operating several repositories | Compare pins, ship reviewed sync PRs, track exceptions and rollback | Repeatable adoption without configuration drift or surprise consumer changes |
| Agent operator using Claude Code, Codex or Hermes | Invoke common status / run-next / handoff / sync skills | One familiar workflow with no harness-specific bypass of policy |
| Security- and reliability-minded engineering team | Audit scope, authorizations, validations, source lineage and release provenance | Clear fail-closed behavior, explainable refusals and independently reproducible evidence |
| Evaluator / researcher | Compare pinned model-and-harness variants in isolated benchmarks | Honest results, cost and failure classification without contaminating live workflows |
| Discord knowledge contributor / maintainer | Ask what was decided, retrieve source-backed answers, and review what is shared | A permission-aware knowledge loop that does not acquire code-execution authority |

**Positioning:** a supervised repository-work harness, **not** a new IDE, general agent framework, replacement for Hermes Kanban, hosted model provider, issue tracker, autonomous merge bot or mandatory orchestration daemon.

## 2. Evidence-based starting point

Use three statuses consistently: **SHIPPED** means code or a release exists; **PROVEN IN A BOUNDED TEST** means the claimed path has evidence but is not fleet-enabled; **NOT ACCEPTED** means an end-to-end product gate remains unmet.

| Capability | Evidence as of 2026-11-03 | Product status and limitation |
| --- | --- | --- |
| Deterministic slice supervisor | `automation/supervisor/`, Pydantic/JSON schemas, replayed validations, bounded diffs, handoffs, runner selection and `run_next.py --single-slice` tests | **SHIPPED**. `--single-slice` limits an invocation only when supplied; the queue's ordinary autonomous limit remains caller-overridable. |
| Versioned consumer distribution | `Tools/boundroom-sync.sh`, reusable manifest, GPL `automation/COPYING` export, provenance/pin checks and release archive | **SHIPPED**. Consumer changes still require an explicit PR and owner-controlled merge; no automatic updates. |
| Portable agent plugin | [SPEC-002](architecture/agent-plugin.md), `plugins/boundroom/`, four shared skills, three-harness manifest/install tests; [v0.2.0](https://github.com/raelldottin/boundroom/releases/tag/v0.2.0) through v0.2.4 | **SHIPPED**, including a limited [Owlory trial](architecture/agent-plugin.md#rollout-trial-step-6-2026-11-02). The run-next trial stopped cleanly with no eligible slice; it did not prove a full live production slice. |
| GitOps, quality and security gates | `AGENTS.md`; five required checks; Python 3.11/3.12; full-suite coverage with an **88% floor**; dependency review/audit; release tag/manifest guards | **SHIPPED**. The 90.04% full-suite coverage figure was measured in the [#139 validation](https://github.com/raelldottin/boundroom/pull/139), not promised as a future constant. |
| Read-only Kanban projection | [RA-CP-002](architecture/kanban-projection.md); deterministic overlay, shadow comparison and stale/foreign/unapproved-task refusals | **SHIPPED as opt-in adapter / PROVEN IN TESTS**. JSON queue is still the default. |
| Kanban claim and status write-back | [RA-CP-005](architecture/kanban-cutover.md); real Hermes CLI test and dedicated two-task scripted canary (2026-10-29) | **PROVEN IN A BOUNDED TEST**. Not a fleet cutover; no claimed race proof with the gateway or real-model full-control-plane canary. |
| Source-attributed Hindsight adapter | [RA-CP-003](architecture/memory-interface.md); bank grants, recall, retain, snapshots and independent backup/restore | **PROVEN IN A BOUNDED TEST**. The synthetic local-server trial used hosted LLM inference; reflect, egress-denied local inference and a real project-bank acceptance remain open. |
| Deterministic Swarm Wiki adapter | [RA-CP-004](architecture/swarm-wiki-compiler.md); pinned Hindsight-snapshot/ByteRover-capable compiler, synthetic build/check and provenance refusals | **PROVEN IN SYNTHETIC CI**. No verified end-to-end live project publication from a real project bank and board. |
| Effectiveness research | [ProgramBench workflow](workflows/effectiveness-benchmark.md); pinned fixtures, isolated cleanroom and official scoring path | **EXPERIMENTAL AND PARALLEL**. Manual/owner-authorized; stalled or unavailable model endpoints are infrastructure failures, not zero quality scores. |
| Fully open-source, egress-denied runtime | [SPEC-001 open-source gate](architecture/control-plane.md#open-source-and-self-hosted-requirement) | **NOT ACCEPTED**. All required model weights, dependency licenses, local inference routes and closed-egress recovery paths need verification. |
| Continuous Discord Knowledge Loop | [SPEC-003](architecture/discord-knowledge-loop.md), [RA-KNOW-000](https://github.com/raelldottin/boundroom/issues/173) | **PLANNED, NOT IMPLEMENTED**. Existing memory and wiki adapters are reusable building blocks; there is no opted-in Discord capture/bot, automatic nightly/weekly knowledge pipeline or knowledge-retrieval evaluation suite. |

**Important update from older documents:** both `repo-automation` and `hermes-swarm-protocol` have owner-approved `GPL-2.0-only` licenses; the plugin is released. Older statements saying these licenses are undecided or that the plugin has not been built describe past snapshots, not the current product. The ByteRover historical-data dependency still needs a license-compliant migration strategy before claiming an entirely OSI-licensed runtime.

## 3. Principles and non-negotiable boundaries

1. **Evidence before autonomy.** No agent starts another slice, advances a Kanban card, approves a handoff, merges a consumer PR or publishes a release on the strength of its own summary.
2. **Exactly one execution authority.** The per-repository synced supervisor validates scope and continuation; Kanban supplies authorized work metadata, Hindsight supplies bounded context, and the Wiki is a generated read-only view.
3. **Fail closed with a useful reason.** Stale revision, wrong project bank, missing pin, invalid provenance, unverified tool, absent flag, expired claim or failed validation must not trigger fallback to broader privileges.
4. **Reversible adoption.** Consumer source, current queue, histories, pins and backups remain reconstructable. New integrations initially run read-only or in shadow mode and are enabled per consumer by a reviewed change.
5. **Open, portable, local-first production target.** No required proprietary hosted model, hidden telemetry, proprietary orchestration service, or Docker Desktop-only deployment. Optional commercial development tools and benchmark providers remain explicitly separate.
6. **No invisible writes.** Human approval, clean Git state, exact ref, PR review and a verifiable audit trail are part of the product experience—not deployment details to hide.
7. **Compatibility beats a second implementation.** Keep existing release pins, schema contracts, Hermes-native interfaces and the four shared skills; do not add a second scheduler, persistence tier or agent framework without a new architectural decision.
8. **Knowledge cannot authorize action.** The Discord Knowledge Loop may read approved evidence and post permission-checked answers, but it has no path to the supervisor, shell, GitHub write APIs, Kanban mutations or next-slice dispatch. Enforce this in process/credential permissions and negative tests, not just prompts.

## 4. Release horizons and milestone map

Horizon names are **planning increments**, not committed dates or promises of a particular semantic version. Version numbers after v0.3.0 are illustrative until the maintainer approves a release tag. P0 control-plane acceptance takes priority over optional supervisor expansion. Authorized effectiveness research is exempt and can run in parallel.

| Horizon | Indicative window | Product result | Dependency / exit decision |
| --- | --- | --- | --- |
| **H0 — Preserve the shipped core** | Now / November 2026 | Secure and document v0.2.x; remove adoption friction; triage pinned dependency advisories | Continuous required checks, no weakening of safety boundaries; no hidden consumer updates |
| **H1 — Operator-grade adoption** | Next increment | Repeatable multi-harness onboarding, meaningful status/refusal output and tested consumer upgrades | End-to-end install, dry-run, one-slice, handoff and sync evidence on opted-in consumers |
| **H2 — Prove the self-hosted control plane** | Following increment | Fully local, licensed Hindsight stack; verified snapshot-to-Wiki chain; revision-aware Kanban flow | All SPEC-001 per-component acceptance evidence, including reflect, egress denial, bank isolation and provenance |
| **H3 — Controlled composition and canaries** | After H2 | One authorized real slice can go from approved Kanban card through supervisor, memory and immutable Wiki evidence | Normal, blocked, stale, retry, rollback, gateway-race and idempotency proofs; owner approves each cutover |
| **H4 — Opt-in fleet adoption and release candidate** | After H3 | Versioned, supportable adoption across opted-in repos/profiles, no cross-project leakage | Per-consumer signed-off smoke matrix, migration/recovery runbooks, license/SBOM and compatibility reports |
| **H5 — Stable 1.0 readiness** | After H4 evidence | Documented stable interfaces and operational expectations, not merely a tag | Explicit maintainer go/no-go after SLO, security, integration, recovery and consumer acceptance results |
| **R — Effectiveness experiments** | Parallel, explicitly authorized | Reproducible ProgramBench evidence for harness/model changes | Exact provider/model/build pins, independent scoring, budget authority and diagnosis of failures |
| **K — Discord Knowledge Loop** | Design/synthetic work in parallel; live canary **after H2 memory/wiki evidence** | Opted-in capture → Hindsight/Wiki → Discord ask/share → retrieval evaluations; nightly/weekly knowledge operations | [SPEC-003](architecture/discord-knowledge-loop.md) security, ACL, provenance, denied-access and evaluation gates; **independent of H3 supervisor cutover** |

### H0 — Ship quality, security and documentation truth

**Outcome:** people can trust the existing release, understand precisely what is and is not implemented, and upgrade without accidental privilege expansion.

- **H0.1 Security maintenance.** The [pydantic update #145](https://github.com/raelldottin/boundroom/pull/145), which cited security advisories affecting the 2.14.1 baseline, merged on 2026-11-10; `uv.lock` pins pydantic 2.14.2, and v0.2.5 and v0.3.0 ship it. Keep triaging each later dependency bump on its own patch, lockfile, audit results and compatibility; do **not** assume a bot bump is safe.
- **H0.2 Accuracy pass.** Correct stale README and documentation claims when touching their files (e.g. an old description of the subset-based coverage job, stale owner/repository URLs). See [consumer-link correction #167](https://github.com/raelldottin/boundroom/pull/167); preserve exact remote identifiers required by locks and plugin install commands.
- **H0.3 Release and plugin contract.** Audit version parity across plugin manifests, catalog resolution and release tag; test SHA-based Hermes installs, partial-copy rejection, supported Python and fail-closed old-copy detection.
- **H0.4 Maintenance gate.** Keep the five protected checks, strict documentation build, coverage floor and supply-chain review. Treat a coverage number as a measured statistic, never a substitute for failure-path tests.

**Exit evidence:** refreshed installation/upgrade instructions; green Python 3.11/3.12 and coverage checks; reviewed security disposition; one pinned release reproduced from its archive/checksum; tested negative paths. **No auto-merge or release implied.**

### H1 — Make the operator journey reliable

**Outcome:** a maintainer can go from an unfamiliar repo to a reviewable one-slice result without learning three separate agent-control dialects.

1. **Discover:** plugin status reports version, consumer sync pin, queue/board mode, next eligible slice, refusal reasons and provenance without mutating the repository.
2. **Adopt:** operator chooses exact tag/SHA, checks manifest differences and consumer lock, and receives an isolated sync PR; there is no silent bootstrap.
3. **Execute:** `run_next.py --single-slice` is a trusted one-slice path, including refusals for conflicting autonomous-limit flags. The agent's natural-language skill is guidance, not an enforcement boundary.
4. **Review:** handoff shows changed paths, evidence references, independent validation replay, blocked conditions, base/head commit identities and a safe next action.
5. **Upgrade or revert:** a consumer can inspect current versus desired pin, reproduce the check, roll back to the previous tag and demonstrate a clean Git state.

**Proposed backlog:** RA-PROD-101 operator status contract; 102 multi-harness install/update matrix; 103 consumer lock and pin UX; 104 one-slice success/failure smoke fixture; 105 handoff diagnostic summary; 106 version skew and migration guidance. RA-PROD-106 is written up as [Upgrading, version skew and rolling back](workflows/upgrading.md) (docs only; no tool changes).

**RA-PROD-105 status (2026-11-12): implemented.** `automation/supervisor/handoff_summary.py` is a read-only command that prints step 4's review summary from a handoff and, optionally, the supervisor's decision or evaluation report ([Reviewing a Handoff](workflows/boundroom.md#reviewing-a-handoff)). It labels the agent's claims apart from the supervisor's evidence, and without a report it says the handoff is not independently verified. One gap remains: `run_next.py`'s decision report records no base or head commit, so with that report the summary can show only the head commit the agent claims. Recording them in the report is a separate change. The summary alone does not meet H1's exit evidence.

**RA-PROD-101 status (2026-11-12): implemented.** `automation/supervisor/operator_status.py` is a read-only command for step 1 ([Operator Status](workflows/boundroom.md#operator-status)). It reports the Boundroom version, the consumer's sync pin, queue or board mode, the next eligible slice, blocked slices and their unblockers, the reasons `run_next.py` would refuse, and provenance: the HEAD commit, whether the worktree is clean, and the files it read. It selects with the supervisor's own functions and writes nothing. The `boundroom-status` skill runs it first. One gap remains: a consumer's synced copy carries no version, so there the report shows the lock's commit instead. It has not been run in a consumer or in a fresh-home install of each harness, so it does not meet H1's exit evidence.

**Exit evidence:** repeatable fresh-home installs in Claude Code, Codex and Hermes; at least one opted-in consumer with both a successful *bounded* slice and a failure-path proof; manifest-owned sync is the only writer; no default-branch mutation by an agent; exact commands and transcripts attached to a PR. An empty-queue stop alone does not satisfy this gate.

### H2 — Finish per-component P0 acceptance, without cutover

**Outcome:** each integration is independently trustworthy before any canonical authority changes.

**Kanban (RA-CP-002/005):**
- **Proposed (2026-11-10):** [SPEC-004](architecture/agent-agnostic-board.md) puts the board behind one backend interface, with a local, Hermes-free backend as the default, so Kanban works with any parent agent. If it is accepted, the Hermes-only items below apply only to consumers that choose the Hermes backend.
- Revalidate the read-only revision snapshot and task-to-slice approval overlay against a pinned Hermes revision. Preserve board IDs, task IDs, explicit dependencies, priority mapping and foreign-board refusals.
- Exercise a gateway claim race and goal-mode task refusal in a dedicated, backed-up canary; verify that no stale or superseded run can record success.
- Design claim renewal or a bounded TTL policy only if measured real work exceeds current claims; do not couple to undocumented Hermes internals merely for convenience.
- Keep the JSON queue default until an authorized per-consumer cutover proves no double writer.

**Hindsight (RA-CP-003):**
- Pin and audit Hindsight, PostgreSQL, embeddings, reranker, local chat model, model weights and every runtime license. Separate endpoint authentication from project/bank grant checks.
- Retain, recall and **reflect** on a genuinely tool-calling local model route; fail explicitly if that route is unsupported. Prove source digests and deny missing, changed, unauthorized or cross-project evidence.
- Repeat restart, backup/restore, budget, timeout, unavailable-server and denied-egress tests with local inference; demonstrate that no sensitive project content reaches a hosted API.
- Migrate only synthetic or owner-approved history first; retain ByteRover originals and record a traceable mapping rather than assuming a one-to-one memory import.

**Swarm Wiki (RA-CP-004):**
- Rebuild from authorized board exports, Hindsight snapshots and historical ByteRover records with the pinned upstream compiler. Capture source hashes and explicit unverified/contradictory claim labels.
- Produce a **live operator-approved** build plus an independent byte-identical `--check`, not only synthetic CI. Keep publication separate from execution and memory writes.
- Resolve the historical ByteRover licensing constraint in the planned runtime; do not claim an OSI-only profile while an incompatible runtime dependency is still required.

**Exit evidence:** per-component audit packets contain pinned commits/images/models, read-only and destructive-test scopes, failures, hashes, backup checks, network-egress witness and reviewed owner decisions. Until then, label the whole control plane **partially validated**, not deployed.

### H3 — Compose and prove the golden path

**Outcome:** one explicitly approved real task follows one coherent, explainable lifecycle with no hidden second scheduler.

**Golden path:** an owner-approved Hermes task + overlay -> revision-bound projection -> supervisor eligibility check -> bounded, project-scoped memory recall -> single authorized runner -> independently replayed validations + immutable handoff -> guarded Hermes review request -> captured Hindsight snapshot -> deterministic, read-only Wiki rebuild.

**Negative path matrix:** stale board revision; simultaneous gateway claim; duplicate/expired claim; blocked dependency; wrong project bank; empty/missing bank; changed source hash; bad handoff; validation failure; no eligible task; network outage; replay after partial write; invalid Wiki provenance. A failure must leave recoverable canonical state and an operator-readable reason.

**Rollout rules:** first a disposable board and synthetic bank; then an opted-in low-risk consumer; then a second distinct consumer/profile. A real model run, authority transfer, credentials and any repository write need their own scoped approval. A worker never approves its own successor.

**Exit evidence:** trace IDs tie board task, slice ID, Git commit(s), bank ID, snapshot digest, handoff digest and Wiki publication together. Test a rollback of board state, memory capture, plugin pin and consumer sync without loss of original evidence.

### H4 — Fleet-ready packaging and adoption

**Outcome:** new repositories adopt a known-good version predictably; maintainers can tell which profile/repo runs which version and recover from a bad update.

- **Compatibility matrix:** release tag/SHA; Python version; Claude/Codex/Hermes version; Hermes Kanban schema; Hindsight API version; compiler pin; consumer schema/pin; supported migration direction.
- **Opt-in adoption:** document check, sync, isolated branch/PR, consumer validation, owner acceptance, post-merge smoke and rollback. Consumers retain local queues, secrets, validation commands, proof thresholds and product release rules.
- **Distribution:** source release/archive + SHA256, GPL-2.0-only text in distributed `automation/COPYING`, third-party attributions, verified dependencies and software bill of materials (SBOM). Every pinned model and container license is separately auditable.
- **Operations:** version skew and stale-pin reports; backups and restoration tests; redacted logs; documented environment and token boundaries; monitoring of failed claims and malformed handoffs without a mandatory hosted telemetry service.
- **Support policy:** upgrade notes, deprecation windows, reproducible failure reports and a narrow security-vulnerability response path. Never promise zero-downtime migration without a demonstrated mechanism.

**Exit evidence:** at least two independently reviewed consumer adoption canaries using distinct profiles; a reversible update from one released pin to another; no undeclared consumer file changes; licensing and egress acceptance signed off.

### H5 — 1.0 decision, not an automatic promotion

Propose a stable 1.0 only when the maintainer accepts: product boundaries and support policy, deterministic gates, complete required integration evidence, source and model license review, separate consumer release authority, backwards-compatible upgrade rules, reproducible rollback, failure injection and operational metrics. A green CI run or a completed prototype is not equivalent to this decision.

**Post-1.0 candidates (uncommitted):** richer cross-repo portfolio views, readonly fleet health summaries, structured evidence queries, extensible policy packs, optional typed domain ontology (Project/Component/Risk/EvidenceType) and improved developer ergonomics. None may create a competing task store, memory database or automatic cross-slice dispatcher.

### K — Discord Knowledge Loop (new capability; planned)

**Outcome:** team knowledge becomes answerable in Discord without giving a conversational bot any authority to execute repository work. The five-stage lifecycle is **Capture → Organize → Ask → Share → Evaluate**, using existing scoped Hindsight and pinned Swarm Wiki components rather than a second knowledge/task database. This feature is **not yet deployed**.

**Boundary:** this is a separately deployed knowledge service and a separate privilege domain. Its bot token and process have no supervisor runner, command execution, repository write/merge, Kanban mutation, plugin run-next, or policy-editing capabilities. Incoming Discord messages and retrieved content are untrusted data. The service may only read authorized sources, maintain derived knowledge under source grants, and reply/publish in specifically permitted Discord channels; cross-channel public sharing requires a reviewed, sanitized candidate.

**Planned increments and tracked slices:**

| Slice | User-facing outcome | Independent acceptance |
| --- | --- | --- |
| [RA-KNOW-001](https://github.com/raelldottin/boundroom/issues/174) | **Capture:** opt-in Discord threads, approved docs, GitHub reviews and consented transcripts | Provenance, project/channel permissions, edits/deletes, retention and replay-safe cursors; no blanket DM/private capture |
| [RA-KNOW-002](https://github.com/raelldottin/boundroom/issues/175) | **Organize:** authorized source evidence → Hindsight → deterministic Swarm Wiki | Source-hash and source-kind verification, immutable complete snapshot, reproducible build/check, bank isolation, revoked-source invalidation |
| [RA-KNOW-003](https://github.com/raelldottin/boundroom/issues/176) | **Ask and Share:** read-only Discord questions, cited answers, reviewed cross-channel sharing | Requester and destination audience permissions checked before answering; abstain on unavailable/uncertain evidence; no private-data disclosure or supervisor calls |
| [RA-KNOW-004](https://github.com/raelldottin/boundroom/issues/177) | **Nightly/Weekly:** incremental ingest and wiki build nightly; propose curation and cleanup weekly | Explicit owner-enabled schedule, budgets, idempotency, complete-or-no-publish, retention-aware cleanup, no changes to canonical evidence without approval |
| [RA-KNOW-005](https://github.com/raelldottin/boundroom/issues/178) | **Evaluate:** retrieval-grounded answer test harness and release checks | Frozen authorized/denied questions, source citation fidelity, abstention and staleness, prompt-injection/leak tests, before/after results with denominators |

**Critical dependencies:** work on contracts and hermetic fixtures can proceed without a live control plane. Any real Discord/Hindsight/Wiki canary requires H2's vetted local memory and wiki evidence, a licensed model configuration, a reviewed source visibility/retention policy, restricted Discord credentials and explicit operator authorization. It does **not** require H3's supervisor/Kanban execution cutover and cannot be used to authorize that cutover.

**Exit evidence:** end-to-end synthetic pipeline produces cited answers and correct refusals; source edits/deletions are reflected; before/after retrieval results show correct and denied cases; no code path can dispatch a slice or modify a board. A later real-guild canary, schedule activation, external API access or model spend needs its own scoped approval. No Discord connection is made merely by accepting this roadmap.

## 4a. Where the work can run (2026-11-10)

The agent works on the owner's machine, which is a company computer. **Of the agent harnesses and services this roadmap uses, only Claude Code is approved there.** Codex is explicitly not allowed. The agent records the work that needs something else as **deferred to an approved machine**, rather than stalling on it. Running deferred work later still needs the approvals AGENTS.md requires, such as a person's approval for spending, cutovers and schedules.

| Work | Needs | On the owner's machine |
| --- | --- | --- |
| H0, H1, RA-BD-001 to 003, RA-BD-005's in-repo side, release preparation | Claude Code and the existing developer tools (git, gh, Python, uv) | **Proceeds.** Merges and release tags still need a person's approval. |
| RA-BD-004 Claude Code canary | Claude Code | **Done** (2026-11-10) |
| RA-BD-004 Codex canary | Codex | Deferred |
| H2 Hindsight: local inference, reflect, a real project bank, egress denial | a Hindsight server, PostgreSQL, local model weights | Deferred |
| H2 Kanban: Hermes gateway races and goal mode | Hermes | Deferred. The local board doesn't need Hermes. |
| H3 to H5 | H2's evidence | Deferred behind H2 |
| K: live capture, Discord or Slack answers, schedules ([SPEC-007](architecture/knowledge-loop-platforms.md)) | a Discord or Slack bot, its credentials, workspace approval, Hindsight | Deferred. Contracts and hermetic fixtures proceed. |
| [SPEC-006](architecture/agent-telemetry.md) telemetry: hermetic slices and the `file`-mode canary | Claude Code, and the Logfire Python package as a pinned optional dependency | **Proceeds.** Sending to any `otlp` destination waits on the owner's data-handling decision. |
| [SPEC-008](architecture/plugin-settings.md) settings and Claude runner profiles, including the RA-SET-009 canary | Claude Code | **Proceeds** |
| R: ProgramBench runs | Hermes and a hosted model provider | Deferred |

When an approved machine or hosted environment exists, record it here and run the deferred work unchanged. Nothing deferred here counts as accepted.

## 5. Dependency graph and critical path

| Workstream | Depends on | Blocks |
| --- | --- | --- |
| Current safety / dependency maintenance (H0) | Existing PR and release gates | Reliable upgrade foundation; may proceed immediately |
| Multi-harness operator experience (H1) | Shipped v0.2.x plugin/supervisor and canary evidence | Fleet onboarding confidence; may run alongside P0 |
| Kanban, Hindsight and Wiki per-component proof (H2) | Current RA-CP-002/003/004 adapters and owner-approved local stack | Combined control-plane canary |
| Golden-path composition (H3) | **All three** independently accepted H2 paths | Per-consumer authority cutover |
| Fleet opt-in (H4) | H3 normal/failure/rollback evidence and license/egress audit | 1.0 readiness |
| 1.0 decision (H5) | H4 proof, product support policy and owner review | Stable release authorization |
| ProgramBench research (R) | Separate experiment approval, pinned model and budget | Does **not** block or bypass P0 |
| Discord Knowledge Loop (K) | H2 memory/wiki acceptance for live integration; opt-in source and permission review; [SPEC-003](architecture/discord-knowledge-loop.md) | Knowledge-service rollout only; **not** H3 supervisor execution or dispatch authority |

**Critical path:** local licensed inference + reflect, real source-backed Wiki publication, gateway-race-safe board write-back, combined negative-path canary, then reversible per-consumer cutover. The independently released plugin is **not** evidence that this critical path is complete.

## 6. Prioritized delivery backlog (candidate issue/PR slices)

These are *proposed work packages*, not created issues or an authorization to run them. Each slice must define allowed paths, maximum diff, validation ownership, proof level, runner and explicit review gate before work is admitted.

| ID | Priority | Deliverable | Acceptance check | Horizon |
| --- | --- | --- | --- | --- |
| RA-PROD-001 | P0 | Dependency/security triage (pydantic #145 released in v0.2.5) | Audit results, lock validation, compatibility tests, documented disposition | H0 |
| RA-PROD-002 | P0 | Documentation truth + install matrix | Correct release and coverage claims; fresh installs and exact command transcripts | H0–H1 |
| RA-PROD-003 | P0 | One-slice success and refused-invocation canary | Exactly one agent run, second slice queued; invalid limits refused before mutation | H1 |
| RA-PROD-004 | P0 | Consumer sync/pin/rollback usability | Check, locked import, PR evidence, old-pin restore and clean stop | H1 |
| RA-BD-001–005 | P0 (proposed) | Agent-agnostic board per [SPEC-004](architecture/agent-agnostic-board.md): interface, local backend, `run_next` wiring, Claude Code canary (passed) and Codex canary (deferred: Codex not approved on the available machine), Wiki input | Contract suite on every backend; full Kanban flow with no `hermes` on `PATH`; owner-authorized canary | H2 |
| RA-CP-002R | P0 | Kanban race/revision and goal-mode hardening | Real CLI/gateway negative tests; no duplicated canonical writer | H2 |
| RA-CP-003R | P0 | Licensed local Hindsight route and reflect | Local retain/recall/reflect; denied egress, bank isolation, restore witness | H2 |
| RA-CP-004R | P0 | Real-bank Wiki build/check proof | Source hashes, snapshot completeness, deterministic rebuild, visible contradictions | H2 |
| RA-CP-005R | P0 | End-to-end golden-path acceptance | Authorized task, validation replay, handoff, guarded review, Wiki, rollback | H3 |
| RA-PROD-009 | P1 | Fleet compatibility and opt-in canaries | Two distinct consumer/profile proofs, version skew handling and migration report | H4 |
| RA-PROD-010 | P1 | Release operations / SBOM / recovery | Exact pins, notices, vulnerability disposition, backup and rollback drill | H4 |
| RA-PROD-011 | P1 | Stable-interface policy and 1.0 go/no-go report | Accepted support, SLO, upgrade and security contracts | H5 |
| RA-EXP-001 | Parallel | Controlled ProgramBench experiments | Scored official artifacts, provider failure classification and budget receipt | R |
| RA-KNOW-001 | P1 (design now) | Permission-aware source capture | Consent/ACL-restricted ingest, replay, edit/delete and provenance tests | K |
| RA-KNOW-002 | P1 (after source contract) | Hindsight and Wiki projection | Deterministic evidence lineage, complete snapshots, changed-source refusals | K |
| RA-KNOW-003 | P1 (after verified projection) | Read-only Discord Q&A and sharing | Source/audience ACLs, evidence citations, abstention, reviewed publication, no supervisor APIs | K |
| RA-KNOW-004 | P1 (after component gates) | Owner-opt-in nightly/weekly knowledge tasks | Idempotent retries, fail-closed partial capture, human-approved cleanup | K |
| RA-KNOW-005 | P1 (before live rollout) | Knowledge retrieval evaluation suite | Frozen question/source fixtures, leakage and citation regression gates | K |
| RA-PROD-012 | P3 | Optional ontology / portfolio view exploration | Separate design decision; no new canonical store or dispatch authority | Later |

**Immediate recommended order:** (1) security/dependency triage, (2) documentation and operator smoke tests, while (3) local Hindsight/reflect and real Wiki proof proceed as the P0 critical path; (4) validate Kanban gateway races before integrating a real owner task. Do not “finish P0” by changing roadmap statuses alone.

## 7. Product success measures and release scorecard

Metrics must be generated from independently inspectable receipts. The targets below are **proposed acceptance thresholds** and are not claimed current results.

| Dimension | Metric / denominator | Proposed target or rule |
| --- | --- | --- |
| Safety | Unauthorized worker starts, out-of-scope writes, cross-bank leaks, bypassed approvals | **Zero tolerated** in negative-path tests and canaries; any finding blocks rollout |
| One-slice behavior | Agent starts per `--single-slice` invocation and mutations under forbidden flags | At most **one** start; **zero** mutations when conflicting limit is rejected |
| Determinism | Byte-identical projection, snapshot verification and Wiki check on fixed inputs | 100% repeatable fixture runs; report any mutable upstream input explicitly |
| Test protection | Full-suite branch-aware coverage with enforced floor | Preserve **>=88%** until a separately reviewed change; do not mistake total coverage for hot-path safety |
| Adoption | Eligible opted-in consumer canaries with required stop, PR, pin and rollback receipts | 100% pass among declared release-blocking canaries; report sample size |
| Recovery | Tested backup/read-back/rollback exercises per included service | No unverified restored dataset declared successful; failures surfaced and isolated |
| Operational reliability | Correctly classified outcomes (done, blocked, policy-refused, infrastructure-failed) | 100% terminal outcomes have an owner-readable reason and traceable evidence |
| Release integrity | Release tag, main ancestor, manifest version, archive checksum and notices | 100% verified for every published release |
| Open-source profile | Required software/model licenses, local inference and egress witness | 100% audited before claiming full-stack OSS; no unexplained outbound inference |
| Benchmark effectiveness | Resolved/near-resolved tasks and mean pass fraction for eligible scored instances | Publish exact denominator, pins, cost and failure categories; no invented score targets |
| Knowledge answer quality | Grounded correctness, source citation precision, retrieval recall@k and abstention on frozen authorized questions | Record denominators and uncertainty; no claimed improvement without before/after matched evaluation |
| Knowledge access safety | Unauthorized source retrieval, private-to-public disclosures, revoked/deleted source serving or prohibited supervisor interactions | **Zero tolerated** in permission and injection tests; any finding blocks Discord rollout |
| Knowledge lifecycle | Nightly ingestion/snapshot completeness, freshness, failed cursor retries, weekly reviewed proposals | Every run has bounded receipts; no incomplete wiki publication and no unapproved destructive cleanup |

**Reporting cadence:** maintain a short evidence table for each milestone, review blockers after every material PR and revisit priorities monthly. Record the date, source commit, test environment, owner, measured denominator, failed cases and remaining uncertainty. Prefer “not measured” over fabricated improvement percentages or arbitrary deadlines.

## 8. Release, change management and go/no-go rules

- **Branch/merge:** each implementation or documentation change uses a PR; all five protected checks must pass at the exact reviewed head. The maintainer owns the merge decision. Do not push to `main` or silently edit a consumer.
- **Version/release:** tags are `vMAJOR.MINOR.PATCH[-prerelease]` on `main`; release workflow verifies ancestry and manifest version and ships the manifest-owned archive plus checksum. New tags require specific maintainer authorization. A proposed horizon does not itself authorize a release.
- **Consumer deployment:** use a released tag/pinned SHA, provenance and `--check` first, review actual file diff, carry consumer lock when required, then a consumer PR and an explicit owner merge decision. Do not force a consumer upgrade because this source repository releases.
- **Runtime/control-plane:** a new real board, Hindsight bank, credential, provider, storage location, automatic dispatch setting or external network route needs scoped operator approval and independent rollback evidence.
- **License boundary:** the Boundroom code and distributed files are GPL-2.0-only; this does not automatically relicense separately authored application code. Preserve upstream notices and check packaging/combined-work obligations case by case. A fully self-hosted open-source runtime remains subject to the model, ByteRover-history and transitive-dependency audit.
- **Research boundary:** ProgramBench runs can proceed in parallel only under their own explicit provider, model, experiment and budget approval, with isolation from live boards, banks and Wikis.

**Go/no-go packet for every milestone:** exact refs, scope/owners, dependencies, tests and failures, security/license assessment, benchmark/egress evidence if applicable, migration/rollback plan, observed metrics and a written maintainer decision. Missing evidence means **NO-GO**, even if CI is green.

## 9. Risks, tradeoffs and explicit non-goals

| Risk | Why it matters | Mitigation / decision |
| --- | --- | --- |
| False sense of deployment | A released plugin or passing synthetic test looks like a working fleet | Show shipped vs bounded-test vs not-accepted status everywhere |
| Trusting prose as policy | Agent skill instructions can be ignored | Enforce safety in supervisor code; test hostile flags and off-contract invocation limitations |
| Dual canonical writers | Concurrent Kanban gateway, JSON queue or agent actions can diverge | Owner-controlled one-writer cutover, revisions, shadow diff, gateway-race tests |
| Data or credential leakage | Memory banks and recall cross project boundaries | Explicit project grants, minimum permissions, changed-source verification, local credentials, denied egress |
| Unsourced memory synthesis | Reflections and Wiki pages can look like evidence | Source hashes, original receipts, visible “UNVERIFIED”/incomplete states, deterministic compilation |
| License or provenance mismatch | GPL code, ByteRover Elastic history, model weights and transitive packages differ | Preserve historical originals; audit each deployed component and combined distribution |
| Plugin/harness drift | Claude/Codex/Hermes manifests and installed versions change | Pinned releases, tested install matrix, manifest version gate and compatibility table |
| Experimental contamination | Benchmarks can consume budget, reuse dirty containers or hide provider failures | Separate manual dispatch, cleanroom receipts, exact pins and failure classification |
| Scope inflation | A generic orchestration service expands the trusted computing base | No second coordinator, hidden daemon, alternate task database or automatic next-slice chaining |
| Discord permission drift and prompt injection | Channel roles change; untrusted messages could leak private sources or request actions | Per-request ACL and destination checks, source invalidation, deny tests, hard credential/process separation from supervisor |
| Scheduled knowledge ingestion changes provenance | Backfill, edits and deletes can make an old wiki answer unsafe | Owner-scoped channels, retention tombstones, complete snapshots, replay-safe cursors and no publication on partial failures |

**Deliberately out of scope for this roadmap:** replacing Hermes Kanban or its dispatcher; cloud-hosted SaaS as the required runtime; a new proprietary model service; unreviewed autonomous PR merging; credentials embedded in skills; automatic consumer sync; migrating real project memory without owner authorization; and ontology-backed scheduling.

## 10. Decisions and source ledger

**Approval model (2026-11-10):** the project must meet CIS and SOC 2 change-management expectations. The agent writes, tests and prepares the work. A person approves merges to `main`, release tags, consumer merges, spending, cutovers and licenses, as a separate recorded action ([AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md), Agent Authority and Human Approval; [control map](compliance/controls.md)). This replaces the full delegation recorded earlier that day. The "still requiring a maintainer decision" list below is again the owner's, with the agent preparing each decision as a pull request.

**Already decided:** the knowledge loop's Q&A interface is Discord, Slack or both, behind one platform interface ([SPEC-007](architecture/knowledge-loop-platforms.md), accepted 2026-11-12); the knowledge service is separate from supervisor authority and requires permission-checked publishing; supervised per-repo authority; plugin as thin common skills; released v0.2.x; GPL-2.0-only original code; five required CI checks; 88% coverage floor; manual consumer sync and merge; separate benchmark authorization; P0 before control-plane-driven fleet dispatch.

**Still requiring a maintainer decision:** which Discord guilds/channels and source categories to opt in, allowed response/publishing destinations, retention/deletion policy, bot credentials and deployment boundary, nightly/weekly activation and first live canary; local model and weight set, Hindsight inference/reflect route and hosting, byte-for-byte/semantic memory migration policy, claim renewal or TTL policy, whether Wiki CI becomes required, first real consumer for H3, initial fleet set, 1.0 support commitments, exact release tags and any change to benchmark budgets.

**Evidence references, not substitutes for live acceptance:** [technical roadmap](roadmap.md), [SPEC-001](architecture/control-plane.md), [RA-CP-001 inventory](architecture/control-plane-inventory.md), [Kanban projection](architecture/kanban-projection.md), [Kanban write-back and canary](architecture/kanban-cutover.md), [Hindsight live proof](architecture/memory-interface.md), [Swarm Wiki compiler pin](architecture/swarm-wiki-compiler.md), [SPEC-002 plugin trial](architecture/agent-plugin.md), [consumer distribution workflow](workflows/boundroom.md), [effectiveness benchmark](workflows/effectiveness-benchmark.md), [CodSpeed removal history](codspeed-removal.md) and [release history](https://github.com/raelldottin/boundroom/releases), [SPEC-003 Discord knowledge loop](architecture/discord-knowledge-loop.md), [RA-KNOW-000 epic](https://github.com/raelldottin/boundroom/issues/173), and the draft [agentic SDLC and GitOps standard](architecture/agentic-gitops-sdlc-standard.md) for consumer rollout ([#170](https://github.com/raelldottin/boundroom/issues/170)).

---

**Maintenance rule:** update this roadmap by a reviewed PR when a milestone is accepted, a priority changes or a live integration disproves an assumption. Never silently rewrite an acceptance history; link the evidence and say what changed. Roadmap work items are proposals until admitted under the repository's existing scoped change process.