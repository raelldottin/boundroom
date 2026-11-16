# Architecture overview

How Boundroom's parts fit together today, for an operator deciding whether to adopt it or tracing a problem. The specifications hold the detail and the decisions, and they win where this page disagrees. Proposed parts are marked as proposed.

## In one paragraph

A repository keeps a queue of small, pre-approved slices. `run_next.py` picks the next eligible one, checks the working tree, marks the slice `in_progress`, builds a bounded context bundle, renders a prompt, and launches one agent session through `run_agent.sh`. The session does the work, commits it and writes a JSON handoff. The supervisor then checks the handoff against what it can see for itself, records a verdict in the queue, prints a decision report, and either continues into the next eligible slice or stops. Consumers get the supervisor by syncing a released copy into their own repository through a reviewed pull request.

## One slice

```text
  queue (automation/queue/slices.json, or a Kanban board through an overlay)
    │
    ▼
  run_next.py ── refuses: nested run, run lock held, a slice in progress,
    │            dirty paths outside scope, runner profile, required telemetry
    │  select ─► claim (in_progress) ─► record base commit
    ▼
  build_context.py ──► context bundle (slice, nearby docs, optional memory recall)
    │
    ▼
  render prompt = automation/prompts/base.md
                + automation/runner-rules.md (the repository's own, optional)
                + automation/prompts/slice.md
    │
    ▼
  run_agent.sh ──► Claude Code, Codex or Hermes (one session, its own permissions)
    │                 └── commits its work, writes automation/handoffs/<time>-<slice>.json
    ▼
  run_next.py judges:
    schema ─► scope from the base commit ─► required validations ─► replays
    ─► manual proofs ─► file budget ─► continuation rules
    │
    ▼
  queue status (done / blocked / failed) + decision report ─► continue or stop
```

The [harness guide](https://github.com/raelldottin/boundroom/blob/main/automation/README.md#lifecycle) lists each step, and the [exit-code reference](exit-codes.md) lists every decision and failure.

## Components

| Path | Role |
| --- | --- |
| `automation/supervisor/run_next.py` | The supervisor: selection, claim, launch, judgment, continuation. The only thing that starts a slice. |
| `automation/supervisor/policy.py` | The rules the supervisor applies: eligibility, scope, validation replay, manual proofs, decisions. |
| `automation/supervisor/run_agent.sh` | The shipped agent command: chooses the runner, checks its inputs, launches it. |
| `automation/supervisor/runner_profile.py` | Chooses the Claude Code runner's profile and ceiling, holds the run lock, refuses nested runs. |
| `automation/supervisor/settings.py`, `settings_view.py` | The settings registry: every setting, its sources and precedence, and a view of where each value came from. |
| `automation/supervisor/evaluate_handoff.py` | Judges a handoff for a slice finished outside `run_next.py`. |
| `automation/supervisor/operator_status.py` | Reports what `run_next.py` would do, without doing it. |
| `automation/supervisor/handoff_summary.py` | A review summary that keeps the handoff's claims apart from the supervisor's evidence. |
| `automation/supervisor/telemetry*.py`, `egress.py` | Optional traces and metrics, and the one chokepoint every network connection goes through. |
| `automation/supervisor/local_board.py`, `board_backend.py`, `kanban_projection.py`, `kanban_writer.py` | Optional Kanban: a local board or Hermes Kanban, projected into the queue's shape through an approval overlay. |
| `automation/context/build_context.py` | Builds the bounded context bundle for one slice. |
| `automation/context/memory.py` | Optional recall from, and export of, a granted Hindsight memory bank. |
| `automation/context/swarm_wiki.py` | Runs the pinned Swarm Wiki compiler and records its evidence. |
| `automation/schemas/` | The queue and handoff models, and the JSON Schemas generated from them. |
| `automation/prompts/` | The base and slice prompts, and fragments for review and other manual runs. |
| `Tools/boundroom-sync.sh` | Syncs, checks and reports a consumer's copy against a pinned release. |
| `plugins/boundroom/` | Skills for Claude Code, Codex and Hermes that drive a repository's synced supervisor. It holds no supervisor code. |

## State, and who owns it

| State | Where it lives | Owner |
| --- | --- | --- |
| The queue | `automation/queue/slices.json`, or a board outside the repository | The repository. It is the operational authority for what runs. |
| Handoffs | `automation/handoffs/` | The repository. Each is the session's account of its work. |
| Decision reports | Standard output, or `--report-file` outside the repository | The operator. |
| The run lock | `$XDG_STATE_HOME/boundroom/run.lock` | The operator's account on that machine. |
| Runner rules | `automation/runner-rules.md` | The repository. A sync never writes it. |
| The sync pin | `automation/boundroom.lock` (or `automation/repo-automation.lock` before 0.3.0) | The consumer. The sync tool never writes it. |
| Traces and metrics | A local directory or an OTLP endpoint the operator chooses | The operator. Off unless turned on. |
| Settings | Flags, environment, the operator's file, the repository's policy | Each source, in the order the [settings reference](settings.md) gives. |

## Distribution

```text
  raelldottin/boundroom main ─► release tag (approved by a person)
     └─► release.yml: gate ─► build archive with the sync tool ─► attest ─► publish
  consumer: Tools/boundroom-sync.sh --sync --pin <sha> --expect-remote <url>
     └─► branch + pull request in the consumer ─► the consumer's owner approves
```

The sync writes only Boundroom's own files, under `automation/` and `docs/workflows/`. A consumer keeps its queue, handoffs, runner rules, hooks and `Makefile`. See the [sync contract](../workflows/boundroom.md#sync-contract) and the [upgrade guide](../workflows/upgrading.md).

## Proposed, not built

- **The control plane in [SPEC-001](../architecture/control-plane.md):** Hermes Kanban, self-hosted Hindsight memory and a generated Swarm Wiki above the supervisor. Parts exist as optional adapters, listed above, but the file queue is still the authority. Moving that authority needs a person's approval.
- **Supervisor-controlled delegation.** Today a runner may launch its own sub-agents within one session, but no worker or sub-agent may start the next slice.

See [the security model](security-model.md) for what each part is trusted with, and [troubleshooting](troubleshooting.md) when a run stops.
