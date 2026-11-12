# SPEC-006: Traceable agent runs with Logfire and OpenTelemetry

Status: **accepted (raelldottin/boundroom#196, merged 2026-11-12 after the owner's approval).** Each delivery slice below is still its own pull request and needs its own approval. RA-TEL-001, the settings model and resolution (`automation/supervisor/telemetry_settings.py`), and RA-TEL-002, the exporter (`automation/supervisor/telemetry.py`, with `logfire==5.4.0` as the `telemetry` extra), are implemented, and so is RA-TEL-006's text: the run-next skill's telemetry rule, the workflow doc's Telemetry section and the control-map row. RA-TEL-003 is implemented too: `run_next.py` and `evaluate_handoff.py` emit the supervised lane's span tree under one `boundroom.run`, and the decision report's `telemetry.trace_id` joins a trace to its record. The handoff's own provenance doesn't carry the trace id yet, because the handoff is written by the agent; the board's run record and the decision report do. RA-TEL-004 is implemented as well: the runner gets `TRACEPARENT` for `boundroom.agent_session` and, unless the user set their own `OTEL_RESOURCE_ATTRIBUTES`, the run and slice ids. With `runner` on and an `otlp` destination, Claude Code's exporter is pointed at the user's endpoint; with any runner telemetry variable already exported by the user, Boundroom sets none of its own. In `file` mode the runner's own spans aren't exported, because Claude Code has no file exporter; the report says so. The decision report lists the variable names Boundroom set and the ones it passed through, never their values. RA-TEL-005's code is implemented too: the sync tool's Python core emits the sync lane's spans and takes the same telemetry flags, and the benchmark's `lanes` command emits the experiment, cell, phase, session and score spans, with one settings report in every cell's `run.json`. Its overhead measurement against `LANE_CEILING_SECONDS` is a benchmark run, which needs a person's approval, so it hasn't been done; see [Lane invariance](#lane-invariance-benchmark).

Owner request recorded here (2026-11-11): use Logfire to make every agent interaction auditable and traceable, in every lane the plugin can start, with the user able to configure it.

Second owner requirement (2026-11-12): **the user must be able to turn telemetry off or point it at their own backend. No hosted destination is ever built into the plugin or the supervisor. A destination exists only when the user names it and turns it on. By default, both telemetry and any backend are off.** [User-owned destinations](#user-owned-destinations) sets this out as testable requirements, and it overrides anything else in this spec that reads otherwise.

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

1. **OpenTelemetry is the contract; Logfire is the SDK.** The supervisor emits OpenTelemetry spans through the Logfire Python SDK (MIT-licensed). Where the spans go is entirely the user's choice: nowhere, a local directory they name, or an OTLP endpoint they name. A hosted service, including Pydantic's hosted Logfire, is just one more OTLP endpoint the user types in. Boundroom ships no destination of its own and never uses the SDK's built-in hosted address. This keeps the [roadmap's](../product-roadmap.md) "no mandatory hosted telemetry" target.
2. **Off by default, on only by the user's choice.** Telemetry does nothing until a person turns it on *and* names a destination. A consumer that doesn't needs no new dependency and gets no new behavior. A kill switch turns it off regardless of any other setting.
3. **Telemetry is an index, not the audit record.** The authoritative record stays where it is: git, the handoff, the decision report and board events. A trace points at those records by id and digest, and each record carries its trace id back. A trace is never verification evidence and never an input to a gate, in the same way [AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md) treats wiki and memory summaries.
4. **Telemetry can't change an outcome.** No verdict, exit code or board write depends on whether a span was exported. An export failure is reported on stderr and the run carries on. The one exception is opt-in: `required: true` makes the supervisor refuse to start a run whose exporter can't start, before anything is claimed.
5. **The supervisor emits; the plugin only passes settings.** Under [SPEC-002](agent-plugin.md) the plugin is skills and manifests. The skills' text learns the new flags. No hook, MCP server, background process or bundled code is added to the plugin.
6. **Read-only toward the backend.** The supervisor sends telemetry and reads nothing back. Logfire's remote management (`fleet`) and managed `variables` stay off, so no backend can change how a run behaves.

## Configuration

### Settings

| Setting | Values | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `off`, `file`, `otlp` | `off` | `off` emits nothing. `file` writes JSON Lines to the user's `file_dir`. `otlp` sends OTLP over HTTP/protobuf to the user's `endpoint`. There is no vendor mode. |
| `file_dir` | an absolute directory outside the repository | **none** | Required in `file` mode. One file per run: `<run_id>.spans.jsonl`. |
| `endpoint` | an `https://` URL, or `http://` only for `localhost`, `127.0.0.1` or `[::1]` | **none** | Required in `otlp` mode. The user's backend, such as a collector they run, an open-source backend or a hosted service's OTLP address. |
| `headers` | `name=value` pairs | none | Optional, for `otlp`. Sent only to `endpoint`, typically an authorization token. Read from the environment or the operator file, never from the repository, and never printed. |
| `capture` | `metadata`, `content` | `metadata` | `metadata`: ids, timings, outcomes, counts, exit codes, digests, and gate reason codes. `content` also records prompt, context-bundle and handoff text, after scrubbing. |
| `runner` | `off`, `metadata`, `content` | `off` | Whether the runner's own telemetry joins the trace. See [Runner sessions](#runner-sessions). |
| `required` | `true`, `false` | `false` | `true` refuses to start a run whose exporter fails its startup check. |
| `environment` | a short name | `local` | Recorded on every span, such as `local`, `ci` or `benchmark`. |

### Where each setting comes from

**The kill switch always wins:** `--no-telemetry` or `BOUNDROOM_TELEMETRY=off` turns telemetry off, whatever any source below says, including the operator's file. Below the kill switch, from highest precedence to lowest:

1. `run_next.py` and `evaluate_handoff.py` flags: `--telemetry <mode>`, `--telemetry-file-dir`, `--telemetry-endpoint`, `--telemetry-capture`, `--telemetry-runner`, `--telemetry-required`. Headers have no flag, so a token never appears in a process list or shell history.
2. Environment variables in Boundroom's own namespace: `BOUNDROOM_TELEMETRY`, `BOUNDROOM_TELEMETRY_FILE_DIR`, `BOUNDROOM_TELEMETRY_ENDPOINT`, `BOUNDROOM_TELEMETRY_HEADERS`, `BOUNDROOM_TELEMETRY_CAPTURE`, `BOUNDROOM_TELEMETRY_RUNNER`, `BOUNDROOM_TELEMETRY_REQUIRED`, `BOUNDROOM_TELEMETRY_ENVIRONMENT`.
3. The operator's file, `~/.config/boundroom/telemetry.json`, validated by a pydantic model like the overlay.
4. The repository's policy: an optional `telemetry` block in the queue policy or Kanban overlay.
5. The default: `off`, with no destination.

**Standard variables set for other tools are ignored.** `OTEL_EXPORTER_OTLP_*`, `OTEL_TRACES_EXPORTER` and `LOGFIRE_*` in the user's shell don't configure Boundroom. A user who set them for another tool hasn't chosen Boundroom telemetry, so their mere presence must not send Boundroom's traces anywhere. See requirement UD-6.

**The repository can reduce telemetry, never redirect or widen it.** The repository's block may only set `mode` to `off`, lower `capture` or `runner` to `metadata` or `off`, or name the `environment`. It can't turn telemetry on, choose a destination, or raise the capture level. A file a slice can edit must not decide where the agent's activity is sent. Claude Code makes the same choice: repository settings there can't enable telemetry or choose its destination. Because it can only reduce, the repository's block is applied after the sources above, as a cap: its `mode: off` turns off telemetry that the operator turned on, and its `capture` or `runner` lowers a higher level. It never raises one. Its `environment` applies only when no other source named one. If telemetry is `required` and the repository's block turns it off, the run is refused rather than run unobserved (RA-TEL-001).

**The resolved settings are reported, without secrets.** The decision report gets a `telemetry` block (`mode`, the destination's scheme and host only, `capture`, `runner`, `environment`, `trace_id`, and which source turned it on). `boundroom-status` prints the settings that would apply, or `telemetry: off (default)`. Headers, tokens, URL paths and query strings are never printed.

## User-owned destinations

These requirements carry the 2026-11-12 owner requirement. Each has an acceptance test. When another part of this spec reads otherwise, these win.

| Id | Requirement | Acceptance test |
| --- | --- | --- |
| UD-1 | **Off by default.** With no setting anywhere, Boundroom imports no telemetry SDK, opens no telemetry connection, writes no telemetry file and sets no telemetry variable for the runner. | A run with a clean environment and no operator file: `logfire` and `opentelemetry` aren't in `sys.modules`, sockets are blocked and unused, no file appears outside the run's normal records, and the runner's environment has no `OTEL_*`, `LOGFIRE_*` or `CLAUDE_CODE_ENABLE_TELEMETRY` that the parent didn't already have. |
| UD-2 | **On only with both a mode and a destination.** `file` without `file_dir`, or `otlp` without `endpoint`, isn't enabled. | Each half alone leaves telemetry off with a stderr message naming the missing setting. With `required: true`, the run is refused before the claim. |
| UD-3 | **No built-in destination.** No hosted or vendor address appears in the supervisor, the sync tool, the plugin or their defaults. The SDK is always configured with `send_to_logfire=False`, so its built-in hosted address is never used. | A test scans `automation/`, `Tools/` and `plugins/` for URLs in telemetry code and skill text, and for known telemetry vendor hosts, and fails on any. A test asserts `send_to_logfire=False` in every configuration path. |
| UD-4 | **The only connection is to the user's endpoint.** With `otlp` on, the supervisor connects to `endpoint`'s host and nowhere else for telemetry. | An egress test points `endpoint` at a local sink, blocks everything else and checks that the spans arrive and nothing else was attempted. |
| UD-5 | **A kill switch that always wins.** `--no-telemetry` or `BOUNDROOM_TELEMETRY=off` turns telemetry off whatever the operator file, repository or other flags say. | Precedence tests with every other source turned on. |
| UD-6 | **Ambient settings don't enable anything.** `OTEL_EXPORTER_OTLP_*`, `OTEL_TRACES_EXPORTER` and `LOGFIRE_*` in the environment neither turn Boundroom telemetry on nor change its destination. When telemetry is on, the SDK is configured only from Boundroom's resolved settings. | With those variables pointing at a sink and Boundroom off, the sink receives nothing. With Boundroom on to a different endpoint, only that endpoint receives spans. |
| UD-7 | **The repository can't choose or turn on a destination.** A repository `telemetry` block may only turn telemetry off or reduce it. A block naming `endpoint`, `file_dir` or `headers`, or setting `mode` to anything but `off`, is refused with a named reason. | Policy-load tests for each refused field. |
| UD-8 | **The plugin never enables telemetry.** The skills pass telemetry flags only when the user asked for them in that request, and contain no endpoint, example host or token. | The UD-3 scan, plus a review checklist item on each skill change. |
| UD-9 | **Runner telemetry follows the same rule.** Boundroom sets runner telemetry variables only when `runner` isn't `off` *and* Boundroom telemetry is on, and only to the user's own destination. A runner setting the user exported themselves passes through unchanged, and is never added to or redirected. Its names, not its values, are listed in the decision report. | Runner-environment tests for each combination. |
| UD-10 | **Insecure transport only to the user's own machine.** An `http://` endpoint is accepted only for a loopback host. Otherwise it's `https://`. | Validation tests. |

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

**As built (RA-TEL-005):**
- The core imports the telemetry modules from the checkout the tool runs from, never from `--source`, which provenance hasn't checked yet. Under an interpreter without pydantic, the tool runs untraced as before, and refuses a telemetry flag or `required` rather than ignore it.
- `file_dir` must be outside the consumer, the source and the tool's own checkout: a spans file in the consumer would land in the sync's pull request, and one in the source would fail the next sync's cleanliness check.
- A refusal sets `result: refused` and the reason's first line on the span where it happened. The error leaves the span before it's raised again, and credentials in a URL are removed, so a remote's token reaches no span.
- `--check` has a plan and no write span. The repository's reduce-only block isn't applied, because the sync tool reads no queue.

### Benchmark lanes A to E

```text
boundroom.benchmark.experiment     run dir, lanes, repeats, instance count, repo sha
└── boundroom.benchmark.cell       lane, repeat, instance, provider validity
    ├── boundroom.benchmark.phase  research | plan | implement (lanes C to E), artifact digest
    │   └── boundroom.agent_session
    └── boundroom.benchmark.score  ProgramBench score, treatment validity
```

**As built (RA-TEL-005):**
- ProgramBench scores a `<lane>/r<repeat>` directory after the whole matrix has run, so each `score` span is a child of the experiment, beside the cells, with the lane, repeat, report digest and treatment validity.
- Every agent session gets a `phase` span, including lanes A and B's single implement session and the compaction sessions of D and E. A phase that owes an artifact carries its digest; implement's output is the submission, whose digest is on the cell, with the cell's `run.json` digest.
- The benchmark reads telemetry only from the `lanes` command line. The operator file and `BOUNDROOM_TELEMETRY_*` configure live runs and are ignored, except the kill switch and the headers, which have no flag. Runner telemetry is refused, and the agent gets no trace context, so a runner's spans don't nest under `agent_session` here.
- `environment: benchmark` is the resource's `deployment.environment.name`, so it applies to every span but isn't repeated in file mode's per-span lines.

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
- **Bounded overhead.** Spans export in a background batch, and the final flush has a timeout. The overhead per cell is measured in the RA-TEL-005 canary and reported against `LANE_CEILING_SECONDS`. That canary is a benchmark run, so it waits for a person's approval; it hasn't run.
- **Separate from live runs.** Benchmark spans use `environment: benchmark`, and in `otlp` mode the user names a separate endpoint or token for benchmark runs. This keeps the AGENTS.md rule that benchmarks stay isolated from live control-plane integrations. Turning telemetry on for a benchmark run is a run-setting change, so it needs a person's approval like any other benchmark change.

## Failure handling

- **The SDK is optional.** `logfire` is an optional extra (`boundroom[telemetry]`), imported only when `mode` isn't `off`. It isn't added to the sync manifest's `runtime.python.dependencies`, so consumers' imports aren't refused for lacking it. If the SDK is missing and telemetry was requested, the run continues with telemetry off and a stderr warning, unless `required: true`.
- **Startup check.** With `required: true`, the exporter is configured and checked before the claim: the `file_dir` is writable, or the endpoint answers. A failure stops the run with nothing claimed.
- **No files in the worktree.** `file_dir`, and Logfire's own `data_dir`, must be outside the repository. A path inside it is refused, the same rule `board_for` applies to a board directory, because a write there would dirty the worktree and break the clean stop.
- **Bounded flush.** The final flush waits at most 5 seconds, then drops what's left and says so on stderr.

The SDK is configured explicitly, never from its defaults or the ambient environment: `send_to_logfire=False` always (it defaults to sending to Pydantic's hosted service), spans go only to Boundroom's own exporter for the user's destination, and `console=False`, `fleet=False`, `inspect_arguments=False`, `data_dir` outside the repository, and `service_name="boundroom-supervisor"`. `service_version` is the Boundroom version and commit.

**As built (RA-TEL-002, Logfire 5.4.0):**
- Logfire 5.4.0 has no `fleet` parameter, so there is nothing to set; the other settings above are passed as written, with `local=True` so no global tracer provider is installed, `metrics=False` and `distributed_tracing=False`.
- `logfire.configure` reads `OTEL_EXPORTER_OTLP_*` from the environment and adds an exporter for them. `configure` hides every `OTEL_*` and `LOGFIRE_*` variable while it runs and restores them afterwards, which is what makes UD-6 hold.
- Logfire registers a pydantic plugin, and pydantic imports every installed plugin the first time a model is built, so an installed extra would import the SDK on every run. `automation/schemas/models.py` switches that one plugin off while pydantic loads its plugins and restores the environment, so UD-1 holds with the extra installed and no child process inherits the switch.
- `logfire` is also in the dev group, at the same pin, so CI exercises the exporter.

## Effects on other specs and rules

- **[SPEC-002](agent-plugin.md):** skill text only. `boundroom-run-next` and `boundroom-evaluate-handoff` list the flags. `boundroom-status` reports the resolved settings. Each skill reports the `trace_id` from the decision report. No plugin code.
- **[SPEC-001](control-plane.md) and [SPEC-003](discord-knowledge-loop.md):** no new authority. The knowledge loop gets no telemetry write path, and telemetry never dispatches or approves anything.
- **Performance (AGENTS.md):** this adds file and network work to the supervisor path when telemetry is on. It grows with the number of validations replayed per slice, not with queue or repository size. Each implementing pull request must say so.
- **[Control map](../compliance/controls.md):** adds a monitoring row (SOC 2 CC7.2, CIS 2) once RA-TEL-003 ships, stated as Process, because exporting is optional.

## Delivery slices

Each slice is one pull request by the agent, approved by a person, with tests in `automation/tests/`.

| Slice | Delivers | Acceptance |
| --- | --- | --- |
| RA-TEL-001 | The settings model and resolution order, flags, environment variables, the operator file, the repository's reduce-only block, and the `telemetry` block in the decision report. No SDK yet. | UD-2, UD-5, UD-7 and UD-10. Precedence tests. A repository block that turns telemetry on, sets a destination or raises capture is refused with a named reason. Headers and tokens are never printed. |
| RA-TEL-002 | `automation/supervisor/telemetry.py`: a no-op by default, a lazy Logfire import, the JSON Lines exporter, hardened `configure()`, the `required` startup check and the bounded flush. `logfire` as an optional extra, pinned. | UD-1, UD-3, UD-4 and UD-6. With `mode: off`: no import, no file and no network, checked with sockets blocked. Without the SDK: a warning, or a refusal with `required: true`. A `file_dir` inside the repository is refused. `fleet`, `send_to_logfire=False` and `data_dir` are asserted. |
| RA-TEL-003 | The supervised-lane span tree in `run_next.py`, `evaluate_handoff.py` and `policy.py`, plus `trace_id` in the decision report and handoff provenance. | An end-to-end run on a local board produces the full tree in `file` mode, in order, with digests that match the records. Planted secrets never appear in any span. A forced exporter failure leaves the verdict and exit code unchanged. |
| RA-TEL-004 | Runner propagation in `run_agent.sh`: `TRACEPARENT`, resource attributes and the `runner` levels for Claude Code. | UD-9. A fake runner sees the expected `TRACEPARENT` and attributes. `runner: off` sets no `CLAUDE_CODE_*` or `OTEL_*` telemetry variables. `OTEL_LOG_RAW_API_BODIES` is never set. |
| RA-TEL-005 | The sync and benchmark spans. | The benchmark gives every lane identical settings, recorded in `run.json`. The agent's prompt and workspace are byte-identical with telemetry on and off. Sync spans cover verify, plan and write. |
| RA-TEL-006 | Plugin skill text, the workflow doc, and the control-map row. A plugin version bump ships with a release a person approves. | UD-8 and the UD-3 scan. The SPEC-002 manifest-version check passes. The skills contain no code, no endpoint and no example host. |
| RA-TEL-007 | Canary: one slice on a disposable local board with Claude Code as parent and runner, in `file` mode on the owner's machine. No new software and no network egress. | The trace tree is complete and joins the decision report through `trace_id`. The runner's spans nest under `agent_session` when `runner: metadata` with a file-backed exporter. Nothing sensitive appears in the files. Recorded in this spec. |

An `otlp` canary, against a destination the user names, comes after the owner's decisions below.

## Alternatives considered

- **Plain OpenTelemetry SDK without Logfire.** It needs more code for the same result, such as scrubbing and a pydantic integration. Because the contract is OpenTelemetry, and Boundroom never uses Logfire's hosted service directly, switching later doesn't change the span model or any setting.
- **A built-in `logfire` mode** that sends to Pydantic's hosted service when a token is present. Rejected under UD-3: it builds a hosted destination into Boundroom, and an ambient `LOGFIRE_TOKEN` could turn it on without the user choosing Boundroom telemetry. A user who wants hosted Logfire names its OTLP address as their `endpoint`.
- **A Claude Code hook in the plugin.** That would only see Claude Code sessions, not the supervisor's gates or other runners, and SPEC-002 forbids plugin hooks without a separate design decision.
- **Parsing session files after the run.** It works without new dependencies and stays possible as a `file`-mode fallback, but it gives no live view and no runner correlation.

## Open decisions for the owner

1. **Which destinations you'll use for live runs.** Boundroom has no default, so this is your runtime choice, not a code change. The options are `file`, an OTLP backend you run (open source, such as SigNoz, OpenObserve or Jaeger), or a hosted service's OTLP address. Logfire self-hosting is an Enterprise plan.
2. **Company machine.** On the company computer, only Claude Code is approved. `file` mode needs only the Python package. Sending agent activity from that machine to a hosted service, Logfire or otherwise, is a company data-handling decision, not the agent's.
3. **Runner telemetry to a hosted destination,** given that Claude Code's events carry `user.email` and account ids.
4. **Whether content capture is ever allowed** for a hosted destination, or only for `file`.
5. **Retention.** How long traces are kept, and where. This depends on the destination's plan and on SOC 2 evidence needs.
6. **License note** for the open-source production profile. The Logfire SDK is MIT, which is compatible with this repository's GPL-2.0-only license. The hosted Logfire service is proprietary and sits outside that profile, like the existing hosted benchmark.
