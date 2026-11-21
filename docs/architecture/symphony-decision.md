# SPEC-011: What Boundroom takes from Symphony (RA-SYM-001)

Status: **proposed** in the pull request that adds it. This is the RA-SYM-001 architecture decision that the [Symphony evaluation](symphony-evaluation.md) calls for. Merging that pull request, after a person's approving review, records the decision.

The decision authorizes no runtime change. Each "adopt" or "adapt" below is a later slice with its own pull request and approval. A slice marked *owner gate* needs a person's decision before an agent starts it. RA-SYM-002 and 003 also need an approved disposable consumer.

## Decision

Boundroom keeps its own model: **one deterministic supervisor, one canonical queue or board, a person's approval at every gate.** [OpenAI Symphony](https://github.com/openai/symphony) is read as a reference for operating patterns only. Its scheduler, in-memory claims, continuation retries, repository-defined hooks, HTTP control surface and remote workers are not adopted.

- **License boundary.** Symphony is Apache-2.0, and Boundroom is GPL-2.0-only. This spec, and every slice that follows it, reuses ideas only, implemented independently. No Symphony code, text or generated schema enters this repository without the owner's licensing decision.
- **Sources.** Everything was read on 2026-11-19 at Boundroom `main` `8571319`: Symphony's [SPEC.md](https://github.com/openai/symphony/blob/main/SPEC.md) and [Elixir README](https://github.com/openai/symphony/blob/main/elixir/README.md), and OpenAI's [Codex app-server documentation](https://developers.openai.com/codex/app-server). Nothing was run against a model.

## Summary

| Area | Verdict | Next slice |
| --- | --- | --- |
| Continuous supervision | **Adapt:** a hand-started `run_next --watch`, one slice per pass, board only | RA-SYM-003 |
| Reconcile before dispatch | **Adopt** | RA-SYM-003 |
| Leases, stalls, retries | **Adapt:** heartbeat renewal, a stall timeout with a process-group kill, retries off by default | RA-SYM-003, *owner gate* for retries |
| Symphony's in-memory claims, continuation retries, uncapped backoff | **Reject** | — |
| Per-slice workspaces | **Adapt:** opt-in, one at a time, retained on anything but `done` | RA-SYM-002 |
| Repository-defined workspace hooks | **Reject** | — |
| Codex App Server | **Adapt:** an opt-in transport inside the Codex runner only | RA-SYM-004, *owner gate* |
| `WORKFLOW.md` and hot reload | **Reject** as a source; **adapt** a per-run config snapshot | RA-SYM-005 |
| Runtime view | **Adapt:** operator status v2, computed from canonical state | RA-SYM-006 |
| HTTP dashboard, refresh trigger | **Reject** | — |
| Remote workers, concurrency limits | **Reject now**, research later | RA-SYM-007 |

## 1. Continuous supervision and recovery

**Today.**
- `run_next.py` has one bounded loop. It neither polls nor waits, and returns when nothing is eligible.
- Below `consecutive_autonomous_limit`, it already starts the next eligible slice itself (`policy.next_slice_eligibility`). The example queue sets that limit to 2.
- One supervised run per user per host (`runner_profile.run_lock`).
- The local board fences `record` and `renew` on the run id, and releases expired claims. `run_next` never calls `renew`.
- The runner has a total timeout, but no stall detection and no retry.

**Symphony.**
- A single orchestrator ticks on an interval, reconciles running work and dispatches up to a concurrency limit.
- Its claims live in memory, with no lease or fence.
- A run that stalls is killed and retried, with exponential backoff and no attempt cap.
- After a normal exit, a "continuation" retry keeps the agent working while the issue stays active.
- Nothing survives a restart.

**Decision.**
- **Adapt: `run_next --watch`.** It runs in the same process, under the same `run_lock`.
  - Each pass reconciles, then runs exactly one slice, as `--single-slice` does.
  - It is started only by hand. Installing it as a schedule is a cutover that needs a person's approval.
  - It runs only on a board where a person moves each task to `ready`, and is refused on the JSON queue, which has no compare-and-set.
  - Watch mode never lets `should_continue` start a dependent task.
- **Adopt: reconcile before every pass.** An unknown earlier outcome becomes `blocked` with reason `unknown_prior_outcome`, never `ready` or `done`.
- **Adapt: lease renewal** on the local board, on a heartbeat. A record after the lease expired is refused. Hermes keeps its fixed TTL.
- **Adapt: stall detection.**
  - A stall timeout on runner output sits beside the total timeout.
  - The supervisor kills the runner's whole process group. Today `subprocess.run(shell=True)` may leave grandchildren alive, and that branch is untested.
  - A stall becomes `failed` with reason `agent_stalled`.
- **Adapt, owner gate: bounded retry.** Only for failures classified as transient, such as a 429 or network loss before any commit.
  - The retry keeps the same claim, with a capped attempt count, exponential backoff with jitter, and `Retry-After` honored.
  - Never for policy, scope, validation, approval or input-needed failures.
  - The default attempt count is 0. A retry spends money, so the cap is a spending decision.
- **Reject:** in-memory claims, continuation retries, uncapped backoff, and agent writes to the board.

## 2. Per-slice workspaces

**Today.**
- The runner runs in the consumer's checkout (`run_next.py`, `cwd=repo_root`).
- Dirt inside the slice's scope is allowed before the run. Dirt outside it stops the run.
- The base commit is read before the claim. The supervisor measures what the run committed (base to `HEAD`) and what it left dirty, then replays validations in the same checkout.
- The bounded profile's sandbox runs `git add`, `git commit` and `git push` outside the sandbox.

**Symphony.**
- One directory per issue, under a root, with a sanitized key.
- The agent's working directory must be that directory, and the directory must lie under the root.
- Shell hooks run around create, run and remove.
- Workspaces are reused, and deleted when the issue reaches a terminal state.

**Decision.**
- **Adapt: opt-in workspaces, off by default.** One at a time, under the existing lock.
  - The root lives outside the repository, and the supervisor checks the canonical path, its prefix and any symlinks before launching.
  - The key is the slice id plus the attempt, with a hash suffix.
  - The runner's working directory must equal the workspace.
- **Every git call, the validation replay and the owned-path deny walk** take the workspace path, not `repo_root`. The queue stays in the main checkout.
- **Retention.** A workspace is kept on every verdict except `done`, and always after a crash. It is removed only after the report, head and handoff digest are recorded and the head is reachable from a pushed branch. Removal never uses `--force`: a dirty workspace is kept and reported.
- **Adopt: an environment allowlist for the runner,** rather than today's full inheritance.
- **Reject:** repository-defined hooks, which would give repository-authored scripts unsandboxed power, and deleting a workspace when a tracker says the work is finished.
- **Owner gate: a worktree or a local clone.** A worktree shares refs and hooks with the main checkout, so an unsandboxed `git commit` could move shared refs. A clone isolates them, at the cost of disk and time.

## 3. Codex App Server

**Today.**
- The Codex path is one line in `run_agent.sh`: `codex --ask-for-approval never exec -C <repo> --sandbox workspace-write`, with the prompt on stdin.
- It records no usage, events or controls. SPEC-008 profiles apply to Claude only.
- `run_next` picks a profile whatever the runner is, so a Codex run's report names a Claude profile Codex never applied. This needs confirming with a run.

**Codex App Server.**
- JSON-RPC over stdio: `initialize`, `thread/start` and `turn/start`, with `item/*` notifications and `turn/completed`.
- Approvals arrive as requests from the server.
- There is no protocol-version negotiation.

**Decision. Adapt, owner gate:** an opt-in transport inside the Codex runner, such as `BOUNDROOM_CODEX_TRANSPORT=exec|app-server`, with `exec` as the default.
- **Same policy as the CLI:** `approvalPolicy: never`, `sandbox: workspace-write`, `cwd` set to the repository, and one turn.
- **Allowlisted methods only.** Any request from the server for approval, input, elicitation or permissions is declined, the turn is interrupted, and the run fails as blocked.
- **Version pin:** the Codex version and a schema hash are pinned, and a mismatch is refused before any prompt is sent.
- **Evidence only:** events and usage are recorded as the runner reported them, never as Boundroom measurements.
- **The handoff and the supervisor's evaluation are unchanged.**

**Reject:** replacing the CLI runner, the WebSocket transport, and experimental methods.

**Before any live canary:**
- an approved machine (Codex isn't approved on the owner's company machine);
- spend approval (Codex has no budget cap like Claude's `--max-budget-usd`);
- a licensing decision on any generated, Apache-derived schema.

## 4. Workflow configuration and reload

**Today.**
- One settings registry with a fixed precedence: flag, then `BOUNDROOM_*`, then operator file, then repository, then default.
- The repository may only narrow telemetry. It can't name the runner profile at all.
- The queue schema rejects unknown keys.
- Each invocation reads its settings once. But a multi-slice run fixes some settings at the start and re-reads the queue policy for every slice. A slice that may edit `automation/prompts/` or `automation/runner-rules.md` changes the next slice's prompt in the same run.
- The queue's `version` isn't checked against a supported range.

**Symphony.** A repository-owned `WORKFLOW.md` holds the configuration and the prompt. Changes are applied to future dispatch without a restart, unknown keys are ignored, and an invalid reload keeps the last good config.

**Decision.**
- **Reject:** `WORKFLOW.md` as a source, hot reload, and ignoring unknown keys. Ignoring a key is how a permission would slip through unnoticed.
- **Adapt: one snapshot per run.**
  - The effective configuration is resolved once, with a hash.
  - Every slice in the run uses it, and every report records its hash.
  - A change during the run, valid or not, stops the run at the next selection.
- **Adopt: a supported range of queue versions,** refused before the claim when out of range.
- **Adapt: document how Symphony's fields map** to Boundroom's existing sources, with `settings_view.py --json` as the read-only effective view. No new file.

**Why no reload can widen permissions:**
- The profile has no repository field.
- Narrow-only values are applied last and refused when wider.
- The slice allowlist is a pure function of the slice record and the policy.
- Approvals live in supervisor-owned paths and a person's GitHub approval.

## 5. Runtime view and distributed workers

**Today.**
- `operator_status.py` is read-only. It reports counts, the next eligible slice, refusals and a telemetry block.
- "Running" shows only as an `in_progress` count. No usage or cost is captured, on purpose: SPEC-006 sets no `gen_ai.*` fields, because the supervisor never sees tokens.

**Symphony.**
- A runtime snapshot of running and retrying sessions with token totals.
- An optional loopback HTTP server, including `POST /api/v1/refresh`, which triggers a poll.
- SSH workers driven by one central orchestrator, with no lease or fence.

**Decision.**
- **Adapt: `boundroom.operator-status/2`.**
  - It adds running, blocked, awaiting-review and evidence-link lists, computed from the queue or board at call time, with no cache.
  - Usage and rate limits appear only when a runner reports them, labelled with their source. Otherwise the view says `unavailable`, never an estimate.
  - A `retrying` list waits for RA-SYM-003.
- **Reject:**
  - an HTTP server or live dashboard, which would add a network listener and a background process;
  - any trigger, since a view has no path to dispatch, claim or write;
  - concurrency limits beyond one run per user.
- **Reject now: remote workers (RA-SYM-007).** They are blocked on:
  - H2 per-component acceptance and the H3 golden path;
  - RA-SYM-002 and 003;
  - a per-host identity and secret-isolation design;
  - the SPEC-001 dispatcher decision;
  - the owner's authorization for credentials, spend and egress.

## Findings outside Symphony

The research found these on `main`. Each is its own pull request, not part of this decision:

1. **A crashed run's commits can escape the scope check.** `run_next` reads the base commit from `HEAD` when it claims a slice. If an earlier run committed and then crashed, and its claim expired back to `ready`, the next run's base already includes those commits, so no scope or diff-budget check ever measures them. *Fix:* record each run's head, and refuse to claim when `HEAD` has moved since the last recorded head without a decision.
2. **SPEC-006 claims custom scrubbing that the code doesn't do.** [agent-telemetry.md](agent-telemetry.md) says Logfire's `ScrubbingOptions` adds token and key patterns. `telemetry.py` passes no scrubbing options, so only Logfire's defaults apply. *Fix:* add the patterns with a test, or correct the doc.
3. **`allowed_paths` accepts `..` and absolute paths.** *Fix:* reject them in the queue schema.
4. **`agent_command_template` is set in the queue's policy, and a command that doesn't run `run_agent.sh` gets no runner profile.** The queue is supervisor-owned, so changing it takes a reviewed pull request. *Owner decision:* keep it repository-settable, or require `run_agent.sh`.
5. **`evaluate_handoff` writes the JSON queue without taking `run_lock`,** so it can race `run_next`.
6. **A Codex run's report may name a Claude runner profile** that Codex never applied (see §3).

## Negative tests the slices must add

| Area | Test | Covered today |
| --- | --- | --- |
| Claims | Two claims at one revision; stale revision; expired lease then record; superseded renewal; duplicate record | Yes, in the local board and Kanban tests |
| Recovery | Restart between the agent's commit and the board write; expiry then re-claim with commits present; timeout kills the process tree; stalled output | No |
| Retry | 429 classified as transient; attempt cap; no retry on a policy refusal | No |
| Watch | Never starts a dependent or unapproved task; stops on a reconcile failure | No |
| Workspaces | `..`, absolute or symlinked paths; runner writes the main checkout by absolute path; shared-ref move; cleanup with dirt, ignored files or an unpushed head; crash leaves a recoverable workspace | No |
| Secrets | Only allowlisted environment variables reach the runner; token patterns are scrubbed from spans | No |
| Codex | Version mismatch refused before the prompt; approval request declined; stream drop; stall; usage-limit error | No |
| Config | Unsupported queue version; profile keys in the queue policy; config changed mid-run stops the run | Partly: wider repository values are refused |
| View | The view can't reach claim or write paths; a tampered receipt loses to canonical state; works offline | Partly: `test_the_status_writes_nothing`, the offline tests |

## Open decisions for the owner

1. **Today's chaining.** Is `consecutive_autonomous_limit` of 2 or more an "automatic successor slice" in the sense of the evaluation? If so, should the default become 1?
2. Watch mode on the board only, as recommended, or also on the JSON queue once it has compare-and-set?
3. A worktree or a local clone for workspaces, and whether runners may `git push -u` to a per-slice branch.
4. Whether any automatic retry is allowed, and its attempt cap and cost ceiling.
5. Whether a config change mid-run stops the run, as recommended, or waits for the next run.
6. Whether `automation/prompts/` and `automation/runner-rules.md` become supervisor-owned by default.
7. Whether `agent_command_template` stays repository-settable (finding 4).
8. For Codex: the machine, the spend, the version-pin policy, and any generated schema's license.
9. Whether RA-SYM-007 stays on the roadmap or moves to post-1.0, as "read-only fleet health summaries".
