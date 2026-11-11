# SPEC-006: Traceable agent runs with Logfire and OpenTelemetry

Status: **proposed** in the pull request that adds it. Merging that pull request, after a person's approving review, accepts the design. Each delivery slice below is still its own pull request and needs its own approval. Nothing here is implemented yet.

Owner request recorded here (2026-11-11): use Logfire to make every agent interaction auditable and traceable, in every lane the plugin can start, with the user able to configure it.

## Problem

Today a supervised run leaves its record in separate places: the queue or board events, the handoff JSON, the decision report on stdout, the runner's session files (`<stem>.log`, `.stream.jsonl`, `.usage.json`, `.controls.json`), git, and for the benchmark, `run.json` and `lane-comparison.json` in each cell. Each is accurate, but nothing joins them. Answering "what did the agent do in this slice, how long did each step take, and which gate stopped it" means reading five files by hand. Across lanes, repeats and repositories, it doesn't scale.

**What "every lane" covers.** An *interaction* is one thing an agent or the supervisor does on the agent's behalf. This spec traces all of them, from both ways a run starts:

| Entry | What runs | Lane |
| --- | --- | --- |
| Plugin skill `boundroom-run-next` | `run_next.py`, one slice | the supervised lane |
| Plugin skill `boundroom-evaluate-handoff` | `evaluate_handoff.py` | the supervised lane |
| Plugin skill `boundroom-status` | read-only projection and queue reads | none: read-only, traced only when asked |
| Plugin skill `boundroom-sync` | `Tools/boundroom-sync.sh` against a consumer | the sync lane |
| Effectiveness benchmark | `automation/benchmark/lanes.py` | lanes A to E |

## Decision (proposed)

1. **OpenTelemetry is the contract; Logfire is the SDK and one optional destination.** The supervisor emits OpenTelemetry spans through the Logfire Python SDK (MIT-licensed). Where the spans go is the user's choice: nowhere, a local file, any OTLP endpoint, or Pydantic's hosted Logfire. Nothing in Boundroom requires the hosted service, which keeps the [roadmap's](../product-roadmap.md) "no mandatory hosted telemetry" target.
2. **Off by default.** Telemetry does nothing until a person turns it on. A consumer that doesn't turn it on needs no new dependency and gets no new behavior.
3. **Telemetry is an index, not the audit record.** The authoritative record stays where it is: git, the handoff, the decision report and board events. A trace points at those records by id and digest, and each record carries its trace id back. A trace is never verification evidence and never an input to a gate, in the same way [AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md) treats wiki and memory summaries.
4. **Telemetry can't change an outcome.** No verdict, exit code or board write depends on whether a span was exported. An export failure is reported on stderr and the run carries on. The one exception is opt-in: `required: true` makes the supervisor refuse to start a run whose exporter can't start, before anything is claimed.
5. **The supervisor emits; the plugin only passes settings.** Under [SPEC-002](agent-plugin.md) the plugin is skills and manifests. The skills' text learns the new flags. No hook, MCP server, background process or bundled code is added to the plugin.
6. **Read-only toward the backend.** The supervisor sends telemetry and reads nothing back. Logfire's remote management (`fleet`) and managed `variables` stay off, so no backend can change how a run behaves.

## Configuration

### Settings

| Setting | Values | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `off`, `file`, `otlp`, `logfire` | `off` | Where spans go. `file` writes JSON Lines under `file_dir`. `otlp` sends OTLP over HTTP/protobuf to the endpoint in the standard `OTEL_EXPORTER_OTLP_*` variables. `logfire` sends to the hosted Logfire project named by `LOGFIRE_TOKEN`. |
| `file_dir` | a directory outside the repository | `~/.local/state/boundroom/telemetry` | `file` mode only. One file per run: `<run_id>.spans.jsonl`. |
| `capture` | `metadata`, `content` | `metadata` | `metadata`: ids, timings, outcomes, counts, exit codes, digests, and gate reason codes. `content` also records prompt, context-bundle and handoff text, after scrubbing. |
| `runner` | `off`, `metadata`, `content` | `off` | Whether the runner's own telemetry joins the trace. See [Runner sessions](#runner-sessions). |
| `required` | `true`, `false` | `false` | `true` refuses to start a run whose exporter fails its startup check. |
| `environment` | a short name | `local` | Recorded on every span, such as `local`, `ci` or `benchmark`. |

### Where each setting comes from

From highest precedence to lowest:

1. `run_next.py` and `evaluate_handoff.py` flags: `--telemetry <mode>`, `--telemetry-capture`, `--telemetry-runner`, `--telemetry-required`.
2. Environment variables: `BOUNDROOM_TELEMETRY`, `BOUNDROOM_TELEMETRY_FILE_DIR`, `BOUNDROOM_TELEMETRY_CAPTURE`, `BOUNDROOM_TELEMETRY_RUNNER`, `BOUNDROOM_TELEMETRY_REQUIRED`, `BOUNDROOM_TELEMETRY_ENVIRONMENT`. Destinations and credentials use the standard names: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` and `LOGFIRE_TOKEN`.
3. The operator's file, `~/.config/boundroom/telemetry.json`, validated by a pydantic model like the overlay.
4. The repository's policy: an optional `telemetry` block in the queue policy or Kanban overlay.
5. The default: `off`.

**The repository can reduce telemetry, never redirect or widen it.** The repository's block may only set `mode` to `off`, lower `capture` or `runner` to `metadata` or `off`, or name the `environment`. It can't turn telemetry on, choose a destination, or raise the capture level. A file a slice can edit must not decide where the agent's activity is sent. Claude Code makes the same choice: repository settings there can't enable telemetry or choose its destination.

**The resolved settings are reported, without secrets.** The decision report gets a `telemetry` block (`mode`, `capture`, `runner`, `environment`, `trace_id`), and `boundroom-status` prints the settings that would apply. Endpoint headers and tokens are never printed.

## Trace model

One trace per run. Span names are stable and use the `boundroom.` prefix. Every attribute is metadata unless the table says `content`.

### Supervised lane (`run_next`)

```text
boundroom.run                      entry, run_id, repo, base commit, boundroom version, queue kind, board backend, actor
├── boundroom.select               slice id, or the reason none was eligible
├── boundroom.claim                task id, expected revision, claim result
├── boundroom.context              document count, bundle digest; bundle text only with capture=content
├── boundroom.agent_session        runner kind, transport, model, budget, exit code, duration, session stem
│   └── (the runner's own spans, when runner telemetry is on)
├── boundroom.handoff.evaluate     handoff digest, status, schema result; handoff text only with capture=content
├── boundroom.validation.replay    one per replayed command: command, base sha, success, duration
├── boundroom.decision             verdict, reason code, changed-file count, decision-report digest
└── boundroom.board.record         verdict written, revision, event id
```

A refusal, such as an out-of-scope path or a missing handoff, is an event on the span where it happened, carrying the same reason the decision report gives. `evaluate_handoff.py` emits the same `handoff.evaluate`, `validation.replay` and `decision` spans under its own `boundroom.run`.

### Sync lane

`boundroom.run` with `entry=sync`, plus `boundroom.sync.verify` (pin, expected remote, result), `boundroom.sync.plan` (file count, drift count) and `boundroom.sync.write`. These spans come from the sync tool's Python core. The shell wrapper only passes settings.

### Benchmark lanes A to E

```text
boundroom.benchmark.experiment     run dir, lanes, repeats, instance count, repo sha
└── boundroom.benchmark.cell       lane, repeat, instance, provider validity
    ├── boundroom.benchmark.phase  research | plan | implement (lanes C to E), artifact digest
    │   └── boundroom.agent_session
    └── boundroom.benchmark.score  ProgramBench score, treatment validity
```

### Joining a trace to the record

- The decision report and the handoff's supervisor provenance gain a `telemetry.trace_id`. Both are additive, optional schema fields, and a record without them stays valid.
- Each span that reads or writes a record carries that record's SHA-256 digest. An auditor can compare a trace with the record it claims to describe.

## Runner sessions

`run_agent.sh` passes the trace context to the runner in `TRACEPARENT`, and adds `boundroom.run_id` and `boundroom.slice_id` to `OTEL_RESOURCE_ATTRIBUTES`. What happens next depends on the runner:

- **Claude Code** reads an inbound `TRACEPARENT` in `claude -p` sessions, so its spans nest under `boundroom.agent_session`. With `runner: metadata`, the supervisor sets `CLAUDE_CODE_ENABLE_TELEMETRY=1` and the trace and log exporters for the chosen destination. Claude Code's traces are a beta feature (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`). Claude Code redacts prompts, tool input and tool content by default. `runner: content` adds `OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_TOOL_DETAILS`. It never sets `OTEL_LOG_RAW_API_BODIES`, which exports the entire conversation.
- **Codex and Hermes** receive `TRACEPARENT` and the resource attributes. Whether they honor them is recorded per runner when that runner is canaried, not assumed.

**Runner telemetry is not scrubbed by Boundroom.** Claude Code exports directly to the destination, so the supervisor's scrubbing never sees it. Claude Code's events also carry `user.email` and account ids as standard attributes, which can't be overridden. That's why `runner` defaults to `off`, and why sending runner telemetry to a hosted destination is an owner decision.

## Redaction

- **Metadata by default.** With `capture: metadata`, no prompt, context, handoff or tool text is recorded.
- **Scrubbing on every span.** Logfire's `ScrubbingOptions` adds patterns for GitHub tokens (`gh[pousr]_`), Anthropic and OpenAI keys, `Authorization` and `Bearer` values, Logfire tokens and private-key blocks.
- **Environment values aren't recorded.** Only allowlisted environment variable names are recorded, never their values. The allowlist is the provenance list the benchmark already keeps (`PROVENANCE_ENV_KEYS`).
- **Content capture is opt-in per run,** and the decision report records that it was on.

## Lane invariance (benchmark)

[`strategies.py`](https://github.com/raelldottin/boundroom/blob/main/automation/benchmark/strategies.py) requires that lanes differ only in context and workflow treatment. For telemetry, that means:

- **The same settings for every lane in an experiment.** The settings are part of the execution context handed to every lane unchanged, and recorded in each cell's `run.json`.
- **Nothing in the agent's prompt, context or workspace.** Telemetry never adds to what the agent sees. Phase artifacts and submissions are unchanged.
- **Bounded overhead.** Spans export in a background batch, and the final flush has a timeout. The overhead per cell is measured in the RA-TEL-005 canary and reported against `LANE_CEILING_SECONDS`.
- **Separate from live runs.** Benchmark spans use `environment: benchmark`, and in `otlp` or `logfire` mode a separate project or token. This keeps the AGENTS.md rule that benchmarks stay isolated from live control-plane integrations. Turning telemetry on for a benchmark run is a run-setting change, so it needs a person's approval like any other benchmark change.

## Failure handling

- **The SDK is optional.** `logfire` is an optional extra (`boundroom[telemetry]`), imported only when `mode` isn't `off`. It isn't added to the sync manifest's `runtime.python.dependencies`, so consumers' imports aren't refused for lacking it. If the SDK is missing and telemetry was requested, the run continues with telemetry off and a stderr warning, unless `required: true`.
- **Startup check.** With `required: true`, the exporter is configured and checked before the claim: the `file_dir` is writable, or the endpoint answers. A failure stops the run with nothing claimed.
- **No files in the worktree.** `file_dir`, and Logfire's own `data_dir`, must be outside the repository. A path inside it is refused, the same rule `board_for` applies to a board directory, because a write there would dirty the worktree and break the clean stop.
- **Bounded flush.** The final flush waits at most 5 seconds, then drops what's left and says so on stderr.

The SDK is configured explicitly, never from its defaults: `send_to_logfire` is `True` only in `logfire` mode (it defaults to sending), and `console=False`, `fleet=False`, `inspect_arguments=False`, `data_dir` outside the repository, and `service_name="boundroom-supervisor"`. `service_version` is the Boundroom version and commit.

## Effects on other specs and rules

- **[SPEC-002](agent-plugin.md):** skill text only. `boundroom-run-next` and `boundroom-evaluate-handoff` list the flags. `boundroom-status` reports the resolved settings. Each skill reports the `trace_id` from the decision report. No plugin code.
- **[SPEC-001](control-plane.md) and [SPEC-003](discord-knowledge-loop.md):** no new authority. The knowledge loop gets no telemetry write path, and telemetry never dispatches or approves anything.
- **Performance (AGENTS.md):** this adds file and network work to the supervisor path when telemetry is on. It grows with the number of validations replayed per slice, not with queue or repository size. Each implementing pull request must say so.
- **[Control map](../compliance/controls.md):** adds a monitoring row (SOC 2 CC7.2, CIS 2) once RA-TEL-003 ships, stated as Process, because exporting is optional.

## Delivery slices

Each slice is one pull request by the agent, approved by a person, with tests in `automation/tests/`.

| Slice | Delivers | Acceptance |
| --- | --- | --- |
| RA-TEL-001 | The settings model and resolution order, flags, environment variables, the operator file, the repository's reduce-only block, and the `telemetry` block in the decision report. No SDK yet. | Precedence tests. A repository block that turns telemetry on, sets a destination or raises capture is refused with a named reason. Secrets are never printed. |
| RA-TEL-002 | `automation/supervisor/telemetry.py`: a no-op by default, a lazy Logfire import, the JSON Lines exporter, hardened `configure()`, the `required` startup check and the bounded flush. `logfire` as an optional extra, pinned. | With `mode: off`: no import, no file and no network, checked with sockets blocked. Without the SDK: a warning, or a refusal with `required: true`. A `file_dir` inside the repository is refused. `fleet`, `send_to_logfire` and `data_dir` are asserted. |
| RA-TEL-003 | The supervised-lane span tree in `run_next.py`, `evaluate_handoff.py` and `policy.py`, plus `trace_id` in the decision report and handoff provenance. | An end-to-end run on a local board produces the full tree in `file` mode, in order, with digests that match the records. Planted secrets never appear in any span. A forced exporter failure leaves the verdict and exit code unchanged. |
| RA-TEL-004 | Runner propagation in `run_agent.sh`: `TRACEPARENT`, resource attributes and the `runner` levels for Claude Code. | A fake runner sees the expected `TRACEPARENT` and attributes. `runner: off` sets no `CLAUDE_CODE_*` or `OTEL_*` telemetry variables. `OTEL_LOG_RAW_API_BODIES` is never set. |
| RA-TEL-005 | The sync and benchmark spans. | The benchmark gives every lane identical settings, recorded in `run.json`. The agent's prompt and workspace are byte-identical with telemetry on and off. Sync spans cover verify, plan and write. |
| RA-TEL-006 | Plugin skill text, the workflow doc, and the control-map row. A plugin version bump ships with a release a person approves. | The SPEC-002 manifest-version check passes. The skills contain no code. |
| RA-TEL-007 | Canary: one slice on a disposable local board with Claude Code as parent and runner, in `file` mode on the owner's machine. No new software and no network egress. | The trace tree is complete and joins the decision report through `trace_id`. The runner's spans nest under `agent_session` when `runner: metadata` with a file-backed exporter. Nothing sensitive appears in the files. Recorded in this spec. |

Hosted Logfire and OTLP canaries come after the owner's decisions below.

## Alternatives considered

- **Plain OpenTelemetry SDK without Logfire.** It needs more code for the same result (configuration, scrubbing, a pydantic integration) and gives no hosted option. Because the contract is OpenTelemetry, switching later doesn't change the span model.
- **A Claude Code hook in the plugin.** That would only see Claude Code sessions, not the supervisor's gates or other runners, and SPEC-002 forbids plugin hooks without a separate design decision.
- **Parsing session files after the run.** It works without new dependencies and stays possible as a `file`-mode fallback, but it gives no live view and no runner correlation.

## Open decisions for the owner

1. **Destination for live runs:** `file` only, an OTLP backend you run (open source, such as SigNoz, OpenObserve or Jaeger), or hosted Logfire. Logfire self-hosting is an Enterprise plan.
2. **Company machine.** On the company computer, only Claude Code is approved. `file` mode needs only the Python package. Sending agent activity from that machine to a hosted service, Logfire or otherwise, is a company data-handling decision, not the agent's.
3. **Runner telemetry to a hosted destination,** given that Claude Code's events carry `user.email` and account ids.
4. **Whether content capture is ever allowed** for a hosted destination, or only for `file`.
5. **Retention.** How long traces are kept, and where. This depends on the destination's plan and on SOC 2 evidence needs.
6. **License note** for the open-source production profile. The Logfire SDK is MIT, which is compatible with this repository's GPL-2.0-only license. The hosted Logfire service is proprietary and sits outside that profile, like the existing hosted benchmark.
