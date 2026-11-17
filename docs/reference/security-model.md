# Security model

What Boundroom protects, whom it trusts with what, where each control is enforced, and what it does not protect against. This page summarizes the specifications and the workflow guide. Where they disagree, they win, and this page is the one to fix.

To report a vulnerability, follow [`SECURITY.md`](https://github.com/raelldottin/boundroom/blob/main/SECURITY.md).

## What Boundroom is, and isn't

Boundroom is a supervisor. It picks one slice from a queue a person approved, launches one agent session for it in a repository you own, and then judges what the session did before anything else may start. Its controls are in what it lets start and what it accepts afterwards.

**It is not a sandbox.** While a session runs, the agent can do whatever its runner lets it do. What limits the agent during the session is the runner's own permission system: for Claude Code, the [runner profile](#runner-permissions) Boundroom chooses; for Codex, its `workspace-write` sandbox; for Hermes, its own configuration. For work you don't trust, run the session in a container or virtual machine.

## What it protects

| Asset | Why it matters |
| --- | --- |
| The repository's history and working tree | A slice must change only what it was allowed to, and leave evidence of what it changed. |
| The queue and its handoffs | The queue is the authority for what runs next. A handoff records what a session claims it did. |
| Consumer repositories and their pins | A consumer runs whatever its synced copy and its lock name. |
| Credentials | Runner credentials, telemetry headers and the memory service's key belong to the operator. |
| Release archives | Consumers sync from them, and must be able to tell a real one from a forgery. |

## Who is trusted with what

| Actor | Trusted to | Not trusted to |
| --- | --- | --- |
| **The operator**, the person who runs `run_next.py` | Choose the runner, its profile and ceiling, the telemetry destination, and supply credentials. | Bypass a gate: the supervisor applies the same checks whoever runs it. |
| **The repository**: its queue, policy, prompts and `automation/runner-rules.md` | Define slices, their allowed paths and required validations, and add instructions for its runners. | Turn telemetry on, choose where it goes, or widen what it records. Name a destination outside Boundroom's own places in a sync. |
| **The agent session** | Do the slice's work within its runner's permissions. | Report its own success. Its handoff is a claim, and the supervisor checks it. Start another run. |
| **A reviewer**, a person other than the change's author | Approve a pull request, a release tag, a sync merge, spending, a cutover. | Nothing is automatic about the approval: the reviewer still reads the evidence. |
| **`boundroom-agent[bot]`**, the agent's GitHub identity | Write code, open pull requests and merge one a person approved, pinned to the approved head. | Approve its own change, create a release tag nobody approved, or change a ruleset. It has no administration permission. |

Whoever can commit to the queue chooses what runs next and what counts as done. Protect the queue with the same review as code.

## Controls

### The agent's output is a claim

The supervisor doesn't take the handoff's word for anything it can check itself.

- **The handoff must match the schema** and name the slice in progress, or the slice fails (`handoff_invalid`, `handoff_slice_mismatch`).
- **Scope is measured from the slice's base commit.** The supervisor records HEAD before the session starts. Afterwards it adds every path the slice's commits changed to the scope check and to the `max_files_changed` count, whether or not the handoff reported them. If HEAD no longer descends from the base commit, the slice fails, because a rewrite hides which changes were the slice's.
- **Some validations are replayed.** Commands on a small exact-match allowlist (`make architecture`, `git diff --check`) are run again by the supervisor. Every other validation is reported by the session and only checked for presence. See [validation ownership](https://github.com/raelldottin/boundroom/blob/main/automation/README.md#validation-ownership-tiers).
- **Manual proofs must be real files** under the approved proof root, naming the slice and the HEAD commit the supervisor observed, not the one the handoff claims. `automation/tests/test_manual_proof_trust_boundary.py` changes one property at a time and checks each is enforced.
- **Review keeps claims and evidence apart.** `handoff_summary.py` labels the handoff's statements as claims, never as verified, and escapes terminal control characters in agent-written text.

### One run at a time, and no self-chaining

- **A run lock** in `$XDG_STATE_HOME/boundroom/run.lock` (by default `~/.local/state/boundroom/run.lock`) allows one supervised run per user. A second is refused with exit 78.
- **No worker may start the next slice.** `run_agent.sh` sets `BOUNDROOM_SUPERVISOR_SLICE_ID` for the session. With it set, `run_next.py`, `evaluate_handoff.py` without `--dry-run`, and any command that changes a local board are refused with exit 78.
- **Continuation is narrow.** A run continues only into the highest-priority eligible queued slice, never into work the queue doesn't hold, and only while the autonomous count is under the limit. The queue's `consecutive_autonomous_limit` is a default the caller can override, not a ceiling. `--single-slice` enforces exactly one.
- **Only one slice may be `in_progress`.** A run refuses to start while one is.

### Runner permissions

For a Claude Code runner, Boundroom chooses a profile ([SPEC-008](../architecture/plugin-settings.md#a-safer-default-for-the-claude-code-runner)):

- The default since v0.4.0 is `bounded`: anything not pre-approved is denied, the sandbox is on and fails closed, the network is closed, and the slice has a budget cap.
- A **ceiling** limits every profile except `custom`, whether requested or resolved. The default ceiling is `edits`. `unbounded` needs `BOUNDROOM_CLAUDE_ISOLATED=1`, the operator's declaration that the run is in a container or VM, or an explicit ceiling of `unbounded`.
- A run started from a Claude Code session in plan mode is refused.
- `custom` is the operator's own settings file, and isn't capped.

A runner that contradicts the harness the run was started from is refused (exit 78), so a run started from one agent can't quietly launch another.

### Repository inputs

- `automation/runner-rules.md` is refused if it is a symbolic link, not a regular file, larger than 32 KiB or not UTF-8.
- A prompt fragment with a placeholder the supervisor doesn't fill is refused.
- A board and a `--report-file` must be outside the repository, so a run can't write its own inputs.

### Sync to consumers

`Tools/boundroom-sync.sh` enforces its [import protections](../workflows/boundroom.md#import-protections) itself, whatever calls it. In short:

- It plans the whole import before it writes anything, and a refusal leaves the consumer unchanged.
- It refuses to write into the canonical repository or any other checkout of it.
- `--sync` needs a full commit `--pin`. The source's `origin` must equal `--expect-remote`, its worktree must be clean at the pin, and it must pass the quality gate.
- It writes only under `automation/` and `docs/workflows/`, plus three named files, and never to a consumer's queue, handoffs, proofs, hooks, `Makefile`, `automation/runner-rules.md` or lock.
- It never overwrites a file with uncommitted local changes.
- `--allow-unverified-source` waives only the provenance and quality checks, prints a warning, and is never passed by a consumer's `make` targets.

A sync reaches a consumer only as a pull request, which the consumer's owner approves.

### Telemetry and network

[SPEC-006](../architecture/agent-telemetry.md) sets these rules:

- Telemetry is off unless the operator turns it on. It records metadata by default: ids, timings, outcomes, counts, exit codes, digests and reason codes. Prompt, context and handoff text are recorded only with `capture: content`, chosen per run, and the decision report records that it was on.
- The repository can only reduce telemetry. It can't turn it on or name a destination.
- Headers, typically a token, come only from the environment or the operator's file. They are never read from the repository and never printed. Status output shows the destination's kind, never its address.
- `BOUNDROOM_OFFLINE` refuses every network connection Boundroom itself would make. Every one of them goes through `guard_egress` in `automation/supervisor/egress.py`, and a test fails if a network path bypasses it.

The runner's own network use is outside these rules: it is the runner's, and its profile or sandbox limits it.

### Changes and releases

- Every change reaches `main` through a pull request with five required checks, approved by a person other than its author. See the [control map](../compliance/controls.md).
- Every workflow action is pinned to a commit, and a test checks it.
- A release tag is created or approved by a person. `release.yml` reruns the gate, builds the archive from the sync tool's own output, attests it, and publishes the changelog section as the notes. The [upgrade guide](../workflows/upgrading.md#verifying-a-release) shows how to verify an archive with `gh attestation verify`.

## What it does not protect against

- **A session acting within its runner's permissions.** During the session, the agent can run any command and read any file its runner allows, and use any network the runner allows. Boundroom judges the result afterwards. Use the `bounded` profile, or a container or VM, for work you don't trust.
- **Validations it doesn't replay.** Outside the replay allowlist, a passing validation is the session's word.
- **Secrets the session can read.** Anything in the repository or the environment the runner can see, the agent can see. Keep credentials out of the repository, and out of the environment of runs that don't need them.
- **A queue or policy change that passed review.** The supervisor enforces the queue. It can't tell a bad slice definition from a good one.
- **A compromised operator machine or GitHub account.** The run lock, the profiles and the approvals all assume the operator's machine and the approver's account are theirs alone.
- **The runner's own telemetry and network.** The offline switch and the egress chokepoint cover Boundroom's connections, not the runner's.
