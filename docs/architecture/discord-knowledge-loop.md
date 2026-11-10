# SPEC-003: Discord knowledge loop (planned, not deployed)

**Status:** design and roadmap proposal, 2026-11-09. **Epic:** [RA-KNOW-000](https://github.com/raelldottin/repo-automation/issues/173). **Depends on:** [SPEC-001](control-plane.md), [Hindsight interface](memory-interface.md), [Swarm Wiki compiler pin](swarm-wiki-compiler.md), and [product roadmap](../product-roadmap.md).

## Decision and product boundary

Introduce a separately deployed, **opt-in Discord knowledge service** with five responsibilities: **Capture → Organize → Ask → Share → Evaluate**. Discord replaces Slack as the user-facing conversation interface. The knowledge service does not own work execution, a queue, a supervisor agent, or canonical Kanban state.

```text
  opted-in Discord channels, documents, PR reviews, approved transcripts
                           |
                  read-only capture
                           v
         immutable source evidence + access/retention metadata
                           |
       explicit owner-scoped Hindsight retain and snapshot
                           v
           pinned, deterministic Swarm Wiki compiler
                           |
           permission-filtered read-only retrieval
                           v
                   Discord Q&A agent
                           |
           same-channel reply or approved share
                           |
                retrieval evaluation receipts

  ----------------- STRICT TRUST BOUNDARY -----------------
  repo-automation supervisor: slice eligibility, execution,
  validation, handoffs, continuation and Kanban write-back
  (no calls or credentials from Discord knowledge service)
```

The knowledge service MUST NOT import or call `automation/supervisor/run_next.py`, the policy/runner/writeback entry points, Hermes Kanban mutation APIs, GitHub repository write APIs, command execution, plugin run-next skills, or any agent tool that can change project state. Its Discord bot token and Hindsight grants MUST NOT grant repository write/merge, shell, worker dispatch, Kanban update, or privileged automation abilities. Use process, credential, network and package boundaries; instruction text alone is not enforcement. If a user asks to run code, merge a PR, change a board or policy, or launch a slice through Discord, **refuse that action through this service** and direct the person to the existing authorized workflow.

## 1. Capture: evidence is not an instruction

- Start with an explicit operator-approved **allowlist** of guilds, channels, projects, source types, bot scopes, retention rules and authorized audiences. Never silently crawl an entire server. Direct messages and private channels are excluded by default.
- Capture only authorized Discord messages/threads (including edits and deletions), document or repository references, GitHub code reviews and explicitly consented meeting transcript exports through read-only adapters. Discord account data, private attachments and transcripts require separate permission/retention decisions. Do not treat a link as consent to follow it.
- Preserve stable origin identity, source revision, timestamp, author, project, channel/guild and audience restrictions, content digest, original evidence reference, capture time, consent/grant version and deletion/tombstone status. Protect secrets, personal information and tokens through redaction and minimized retention.
- Idempotent incremental cursor ingestion must tolerate rate limits, partial pages, retry, edit/delete, permissions changing mid-run, outage, crash and replay. Never claim a complete capture from a partial run.
- Raw original records stay under controlled evidence storage with source ACLs; the wiki and Hindsight are *derived projections*, not replacements. Untrusted messages can never supply tool or policy instructions.

## 2. Organize: Hindsight and Swarm Wiki, not another database

- Reuse `automation/context/memory.py` for project-mapped, explicitly authorized retain/recall/snapshot operations. The current adapter requires repository-source evidence hashes; extending provenance verification to Discord documents requires a **new reviewed source-type contract**, not a bypass or fabricated local source path.
- Use the existing pinned `automation/context/swarm_wiki.py` wrapper and an authorized Hindsight snapshot export. The Wiki compiler accepts only permitted, complete, source-hashed snapshots and canonical evidence; missing/changed sources remain visible as unresolved findings.
- Reject cross-project memory access, broken source links, unknown permissions, inconsistent snapshots and wiki/source disagreement. Preserve contradictions; do not silently choose a model-generated answer as truth.
- Any source edit, removal, expired consent or visibility change causes relevant derived entries to become unavailable pending safe re-indexing. A source marked inaccessible must not continue to answer from stale Hindsight or wiki copies.
- Content processing is **never** permitted to change task status, validation outcomes, a repo branch, original evidence or published source history.

## 3. Ask: permission-aware Discord retrieval

- A Discord interaction routes to a **read-only** retrieval service with an operator-managed mapping from guild/channel/user visibility to project evidence permissions. Recheck the **requester's current access and the destination's audience** at retrieval and response time; a channel's accessibility is not equivalent to each source's permission.
- Provide short, attributable answers with links or IDs to original allowed evidence, publication/snapshot version and age. Abstain explicitly when evidence is missing, incomplete, contradictory, stale or forbidden.
- Guard against prompt injection in messages, documents and retrieval results; they are data. A recalled memory, wiki page or Discord reply cannot instruct the service to change its tool scopes or override a deny.
- Restrict replies to authorized channels/threads or private permitted interactions. No broad "answer from every bank" or cross-guild search.

## 4. Share: approved knowledge, never unrestricted cross-posting

- Within an authorized conversation, answer only with sources visible to its audience. Cross-channel or public knowledge publishing requires **explicit destination allowlist, sensitivity check, reviewed sanitized candidate and an approval receipt**.
- Publish the source evidence and answer provenance together, not just generated prose. Treat follow-up corrections as linked revisions; never silently erase original audit records while honoring retention/deletion requirements.
- Answers in Discord can be offered as **new candidate sources** only through a separate, opted-in capture and review path. A bot's own answer is not independent proof.

## 5. Nightly, weekly and evaluation lifecycle

| Cadence | Proposed behavior | Mandatory restrictions |
| --- | --- | --- |
| **Nightly** | Incrementally capture approved sources, reconcile edits/deletions and ACL changes, retain verified source records in scoped Hindsight banks, snapshot and rebuild/check Wiki | Operator must separately authorize activation, destination and credentials; bounded time/cost, idempotent retry, complete-or-no-publish, failure alerts |
| **Weekly** | Detect duplicates, staleness, contradictory summaries and indexing gaps; propose curation and run retrieval evaluations | Human review before destructive or visibility-expanding changes; keep immutable source evidence where retention permits; no silent reclassification |
| **On change / pre-release** | Run a frozen permission-aware retrieval evaluation set against before/after snapshots | Report question-level failures and regression gates before any publish rollout; cannot claim quality improvement without matched evidence |

Retrieval evaluations are **not** ProgramBench code-generation evaluations. Include permitted/denied cases, citation correctness, source grounding, retrieval recall@k, answer correctness, required abstention, stale/deleted sources, prompt injection, latency and cost. The acceptable security target is **zero tested unauthorized disclosures**; report question/instance denominators, repeatability and confidence rather than claiming an unmeasured improvement.

Scheduling is explicitly restricted to knowledge maintenance. No overnight code-editing agent, task dispatch, hidden background supervisor, automatic merge, or real Discord messages in routine unit/CI tests. A fake-clock/offline adapter verifies the workflow before an owner approves a dedicated deployment.

## 6. Rollout sequence and acceptance gates

| Slice | Output | Acceptance |
| --- | --- | --- |
| [RA-KNOW-001](https://github.com/raelldottin/repo-automation/issues/174) | Opt-in capture contract and adapters | Reversible incremental ingest; permission/edit/delete and sensitive-content tests; no blanket DM capture |
| [RA-KNOW-002](https://github.com/raelldottin/repo-automation/issues/175) | Provenance-preserving evidence → Hindsight → Wiki pipeline | Source-kind aware verification, complete deterministic snapshots, independently reproducible `--check` and denial tests |
| [RA-KNOW-003](https://github.com/raelldottin/repo-automation/issues/176) | Read-only Discord Q&A and reviewed sharing | User + destination ACL enforced; citations and abstention; no cross-channel private leakage; no supervisor access |
| [RA-KNOW-004](https://github.com/raelldottin/repo-automation/issues/177) | Bounded nightly processing / weekly curation | Operator opt-in; fake clock/crash/backfill tests; no loss of canonical evidence or automatic policy writes |
| [RA-KNOW-005](https://github.com/raelldottin/repo-automation/issues/178) | Permission-aware retrieval evaluation and quality release gate | Reproducible seeded cases, before/after reports, zero access-control regression, no cost-bearing calls in ordinary CI |

**Dependency and prioritization:** the **design and hermetic adapter/tests** can proceed separately from supervisor deployment. A **live** capture/answer/wiki canary depends on accepted Hindsight/Wiki component evidence (product H2), scoped Discord permissions, local model/egress decisions, data-retention review and operator-approved hosting. It does **not** depend on or authorize a supervisor/Kanban writeback cutover (product H3); these systems remain separate.

**Release gate:** fresh-host sandbox with synthetic Discord payloads → reproducible ACL and injection tests → complete wiki build/check and source-delete invalidation → retrieval eval with seeded denied questions → reviewed security/license/data lifecycle packet → optional explicit, narrowly scoped operator-approved live guild canary → approved release and schedule enablement. No stage is approved by this specification itself.

## 7. Non-goals

No Slack support; no implicit archival of all Discord history; no private-attachment scraping; no silent transcription; no public knowledge dump; no new task store or parallel agent scheduler; no automatic governance edits, PR creation, repository writes or Kanban status updates; no semantic claim that Hindsight reflect output is independent source evidence; no assumption that a wiki test proves an operational Discord service.

**Implementation state:** as of this specification, repo-automation has Hindsight retain/recall/reflect/snapshot code, bounded memory context and a synthetically tested Swarm Wiki compiler. The full Discord loop, its schedules and retrieval evaluations are **proposed and not implemented**. Review every implementation slice independently through the existing `AGENTS.md` GitOps gates.