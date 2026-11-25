# SPEC-013 (proposed): Adoption and evals

Status: **proposed.** Nothing in this document is implemented. Every claim about current behavior cites the code at `219cbc0`, the head of #294 when this was written (v0.5.3 plus that pull request's benchmark changes) (`file:line`). Hermes Kanban, Hindsight, the Swarm Wiki and supervisor-controlled delegation are target architecture ([SPEC-001](control-plane.md)), not shipped. This spec does not depend on any of them.

Related: [SPEC-012, effectiveness proof](effectiveness-proof.md) is the experiment these features make measurable.

## Problem

Boundroom can't yet show that bounded slices do better than unbounded agent runs, and it is slow to adopt. The evidence it needs is mostly recorded already, but tools can't read it:

- **Stop reasons are prose.** The decision report gives `decision` and a free-text `reason` (`automation/supervisor/run_next.py:787-817`). Several different stops share one verdict. `stop_failed` covers validation failure, scope violation, diff-budget overrun and missing proof (`automation/supervisor/policy.py:985`, `:1002`, `:1019`, `:1038`). The compatibility policy says scripts must not parse prose (`docs/reference/compatibility.md:31`). So diff-budget overruns and caught scope violations can't be counted today without breaking that rule.
- **The path a run took is lost for two of the three runners.** The Claude runner runs with `--print --no-session-persistence` and keeps no transcript (`automation/supervisor/run_agent.sh:297-308`). Codex is `exec`'d with no capture (`run_agent.sh:280-283`). Only Hermes writes a session record, and only when `BOUNDROOM_HERMES_USAGE_DIR` is set (`run_agent.sh:570-580`, `:629-650`).
- **Adoption is manual.** The checklist asks people to write `slices.json` by hand (`README.md:69-75`). The bootstrap tells them to copy an Owlory-specific example (`docs/workflows/boundroom.md:434-436`; `automation/examples/example-slices.json` lists `owlory_xcode/...` paths).
- **Only one real-runner proof exists, from one machine.** Claude Code was "not run in CI" and was recorded once on the owner's machine. Codex and Hermes are "fakes only" (`README.md:163-164`).

## Ordering by value per effort

| Rank | Feature | Effort | Value | P0 interaction |
| --- | --- | --- | --- | --- |
| 1 | §1 Record the path (transcript link and OTLP to any backend) | S | High: every stopped slice becomes inspectable, and Braintrust-style backends receive it through the OTLP export that already exists | Mostly SPEC-006 delivery work (accepted). The supervisor edits are small and additive. A hosted destination needs an owner decision. |
| 2 | §2 Handoff history as an eval dataset | M | High: regression evals for stop rules, plus the metrics behind the headline chart | Lives in `automation/benchmark/` under the benchmark exemption. One additive supervisor field (`stop_code`) needs reviewer sign-off as a bounded change. |
| 3 | §3 CI canary against a real runner | M (code), high approval cost | High credibility: turns "supervised" from a single-machine claim into something CI repeats | Lines up with P0, which is an "adapter/inventory/canary acceptance gate" (`AGENTS.md`). Spending and secrets need a person's approval. |
| 4 | §4 Structured reviewer verdict and optional LLM judge | M | Medium-high: makes "what risk remains" measurable over time | The verdict schema is a small supervisor addition. The judge reuses benchmark code and is spending. |
| 5 | §5 `boundroom init` | L | Highest adoption value, but the largest surface | Supervisor feature expansion (roadmap P1, "consumer adoption"). It waits behind P0 unless the owner reorders. |

## Cross-cutting prerequisite: a machine-readable stop code

§2, §4 and the headline metrics all need this.

- **Change.** Add `stop_code` to `CompletionDecision` (`policy.py:47-60`), set by each branch of `evaluate_completion` (`policy.py:903-1064`). Values: `reported_blocked` (`:951`), `reported_failed` (`:968`), `validation_failed` (`:985`), `scope_violation` (`:1002`), `diff_budget_exceeded` (`:1019`), `proof_missing` (`:1038`), plus `continue` / `limit_reached` / `review_required` for the `next_slice_eligibility` branches (`policy.py:681-835`). [SPEC-012](effectiveness-proof.md)'s coarse `gate` grouping (validation, scope, diff budget, proof, handoff, agent, none) is derived from `stop_code` by a fixed table in the benchmark, not stored as a second field. `make_decision_report` (`run_next.py:787`) and `make_failure_report` (`run_next.py:855`) emit it. The failure report takes the reason code `fail()` already gives telemetry (`run_next.py:404-413`, for example `handoff_missing`, `handoff_invalid`, `agent_timeout`).
- **Compatibility.** Patch: "The decision report: Add a field" (`compatibility.md:24`). The exit codes and `decision` values do not change.
- **Tests.** One case per branch in `automation/tests/test_harness.py`. Assert that `stop_code` matches the existing `decision`/`reason` pair.

---

## 1. Record the path, not just the result (rank 1)

### What already ships (verified)

- **OTLP/HTTP trace export to an endpoint and headers the user chooses.** `OTLPSpanExporter(endpoint=..., headers=...)` is at `automation/supervisor/telemetry.py:312-315`. A bare host gets `/v1/traces` (`telemetry.py:319-324`). Settings are `BOUNDROOM_TELEMETRY`, `_ENDPOINT`, `_HEADERS`, `_CAPTURE` and `_RUNNER` (`automation/supervisor/telemetry_settings.py:45-55`). The endpoint may not carry credentials (`telemetry_settings.py:77`). Ambient `OTEL_*` and `LOGFIRE_*` variables are hidden from the SDK so they can't redirect it (`telemetry.py:9-11`, `:280`). `BOUNDROOM_OFFLINE` refuses the export before connecting (`automation/supervisor/egress.py:46-55`).
- **Spans emitted by `run_next`.** The root is `boundroom.run` (`run_next.py:234-238`). Every other span is created with `tracer.mark`, which opens and closes a span (`telemetry.py:95-103`). They are:
  - `boundroom.select` (`run_next.py:320`, `:336`);
  - `boundroom.context`, with the bundle digest and the text only with `capture=content` (`:358`);
  - `boundroom.claim` (`:445`);
  - `boundroom.agent_session`, with `slice_id`, `boundroom.runner.requested`, `boundroom.runner.profile` and `exit_code` (`:449-475`);
  - `boundroom.handoff.evaluate`, with the digest and status (`:505-511`);
  - `boundroom.validation.replay`, one per replayed command (`:532`);
  - `boundroom.decision` (`:413`, `:563`);
  - `boundroom.board.record` (`:422`, `:576`).
- **The runner joins the trace.** The runner gets `TRACEPARENT` and `OTEL_RESOURCE_ATTRIBUTES=boundroom.run_id=…,boundroom.slice_id=…` (`telemetry.py:603-616`). With `runner: metadata` and an OTLP destination, Claude Code is pointed at the same endpoint, and `OTEL_LOG_RAW_API_BODIES` is never set (`telemetry.py:621-637`). Tests are at `automation/tests/test_telemetry_spans.py:140-168`.
- **The decision report links to the trace.** It carries `telemetry.trace_id` (`run_next.py:238`, through `_with_telemetry`, `:820-823`).

**What this means for Braintrust or any OTLP backend.** No new exporter is needed. Point `BOUNDROOM_TELEMETRY_ENDPOINT` at the backend's OTLP/HTTP traces URL and put its auth and project headers in `BOUNDROOM_TELEMETRY_HEADERS`, set in the operator file or environment. The repository file can't set them (`automation/schemas/models.py:88-94`). Three things must be confirmed in a canary before docs claim them:

- the exact Braintrust OTLP path and header names;
- that it accepts `http/protobuf`;
- what happens to metrics. `metrics_url` derives a `/v1/metrics` sibling (`telemetry.py:327-338`), which a traces-only backend may reject. The flush is bounded and never changes the verdict (`run_next.py:231`).

### What's missing (propose only these)

1. **The span attributes SPEC-006 promised but `run_next` doesn't set.** SPEC-006 lists transport, model, budget and session stem on `boundroom.agent_session` (`docs/architecture/agent-telemetry.md:92`). `run_next.py:449-454` sets none of them. Add the ones the supervisor actually knows:
   - `boundroom.runner.budget_usd`, from the profile env (`automation/supervisor/runner_profile.py:35`, `:44`, `:395`);
   - `boundroom.session.record_sha256`, from item 2 below.
   
   Model and transport stay unset unless the runner reports them (`agent-telemetry.md:162-166`).
2. **A transcript capture setting, operator-only and off by default.** A new setting `session_dir` (`BOUNDROOM_SESSION_DIR`), registered in `automation/supervisor/settings.py` and rendered by `settings_view.py --docs --check` (the `Makefile` `reusable-check` target). When it is set, `run_agent.sh` does three things:
   - **Claude:** adds `--output-format stream-json --verbose` and tees stdout to `<session_dir>/<stem>.stream.jsonl`, following the Hermes stream-json pattern (`run_agent.sh:632-640`). It keeps `--no-session-persistence`, so nothing lands in `~/.claude`.
   - **Codex:** tees stdout and stderr to `<stem>.log`.
   - **Hermes:** reuses `BOUNDROOM_HERMES_USAGE_DIR` as it already works.
   
   `session_dir` must lie outside the repository. Reuse the check in `report_file_for` (`run_next.py:843-853`), because a write inside would dirty the worktree and trip the scope check.
3. **A link from the record to the transcript.** It goes in the supervisor's decision report, not in the agent-written handoff. The handoff is authored by the runner, so a runner-written pointer would be unverified. Add `session_record: {"name": "<stem>.stream.jsonl", "sha256": "...", "bytes": N}`, with no absolute path, because paths name the user (see the RA-TEL-007 finding, `agent-telemetry.md:297`). `handoff_summary.py` already reads the decision report (`automation/supervisor/handoff_summary.py:149`, `:255`) and gains one rendered line.
4. **SPEC-006's handoff `telemetry.trace_id`** (`agent-telemetry.md:170`) is not implemented. The `Handoff` model has no such field (`models.py:176-197`). This spec drops that half. The decision report already carries the trace id, and putting supervisor provenance in a runner-authored file is the wrong trust boundary.
5. **A docs recipe**, "Send Boundroom traces to an OTLP backend", with a Braintrust example marked "verified on <date>" only after the canary.

### Compatibility

- Span attributes: patch ("Add a span, metric or label value", `compatibility.md:27`).
- The new setting, off by default: patch (`compatibility.md:20`).
- The decision-report field: patch (`compatibility.md:24`).
- `run_agent.sh` behavior without the new variable is unchanged.

### Tests

- `test_telemetry_spans.py`: the new attributes appear on `boundroom.agent_session`.
- `test_runner_selection.py`: with fake CLIs, `BOUNDROOM_SESSION_DIR` produces the file. A path inside the repository is refused. Without the variable, the argv is byte-identical to today's.
- `test_harness.py`: `session_record` carries a digest and a name, and no absolute path.

### Risks

- **Transcripts hold source code, prompts and possibly secrets the agent read.** The setting is off by default and operator-only, never readable from the repository file (UD-7 pattern, `models.py:88-94`). Runner telemetry to a hosted destination is already an owner decision (`agent-telemetry.md:227`).
- **Claude stream-json changes what appears on stdout.** The report already goes to `--report-file` for tools (`run_next.py:831-840`).

### P0 interaction

This is delivery work under accepted SPEC-006. The supervisor edits are small and additive. The reviewer decides whether it waits for P0. Sending to a hosted backend (Braintrust) needs an owner decision.

---

## 2. Handoff history as an eval dataset (rank 2)

### What ships today, and what "validation replay" actually is

- **Validation replay is not history replay.** It re-runs an allowlist of only `make architecture` and `git diff --check` (`policy.py:171-174`) at the slice's head, for commands the handoff claims passed (`policy.py:631-669`). Every other command is `run_report_only` (`policy.py:567-588`).
- **`evaluate_handoff.py` replays in a detached temporary worktree** at a given head (`automation/supervisor/evaluate_handoff.py:130-176`) and reads the slice record as of a commit (`slice_at_sha`, `:103`). It refuses a slice that isn't `queued` (`:206`), so it can't re-judge history as it stands.
- **`evaluate_completion` is a pure function** of the queue, the slice, the handoff, the dirty-path lists, committed paths and replay results (`policy.py:903-916`). It is the natural unit to re-score.
- **The decision report holds `base_sha` and `head_sha`** (`run_next.py:826-828`) and is appended as JSONL to `--report-file` (`:831-840`).
- **What is not recorded:** `dirty_paths_before_run` and `dirty_paths_after_run` (`run_next.py:337`, `:517`). Committed paths can be recomputed from `base_sha..head_sha` (`policy.git_committed_paths`, `policy.py:442`). Uncommitted dirt can't be.

### Scope

New `automation/benchmark/replay.py` (`python -m automation.benchmark replay`). It is read-only toward the queue, handoffs and Git refs.

1. **Build the dataset.** Read handoffs from a consumer's `automation/handoffs/` history, using `git log` over that path, plus decision reports from a JSONL file. Each case has:
   - `slice_id`, the slice record at `base_sha` (`slice_at_sha`), the handoff bytes and their sha256;
   - `base_sha` and `head_sha`, recomputed committed paths, and recorded `stop_code`/`decision`;
   - a `label`: `regression` for `stop_failed`/`stop_blocked`/failure reports, and `accepted` for `continue`/`stop_for_review` that a person later merged. Merge status is read from Git only, with no GitHub writes.
2. **Mode `rules` (offline, deterministic, $0).** Re-run `policy.evaluate_completion` from a candidate checkout of the policy (`--policy-ref <git-ref>`) over every case. Replays are taken as recorded by default, or re-run with `--replay` in a detached worktree, reusing `replay_validations_in_worktree`. Output is a confusion matrix of baseline vs candidate `stop_code`. Report flips: a regression the new rule now passes (dangerous) and an accepted case it now rejects (noisy).
3. **Mode `prompt` (offline, render-only, $0).** Render `render_prompt` (`run_next.py:716-748`) for each case under the baseline and the candidate fragments (`automation/prompts/base.md`, `slice.md`, `automation/runner-rules.md`). Report unknown placeholders (`run_next.py:740-742`), size deltas, and a diff. This does **not** score a prompt's effect. That needs agents re-run.
4. **Mode `rerun` (spending; needs a person's approval for each run).** For selected regression cases, check out `base_sha` in a disposable worktree and run one slice with the candidate prompt through the lanes' existing session seam (`automation/benchmark/strategies.py:764`, `run_phase`). Score it with the existing `evaluate_completion` and the replay allowlist. This is the only mode that measures a prompt or stop-rule change on agent behavior. It is benchmark spending under AGENTS.md.
5. **Headline metrics from the same dataset.** These cover the requested chart:
   - validation pass rate: share of `done` handoffs with no `validation_failed`;
   - diff-budget overruns: `diff_budget_exceeded`;
   - scope violations caught: `scope_violation`;
   - human rework: later commits touching the same `allowed_paths` before the next slice, from Git;
   - tokens and cost per merged change: only where a runner reports usage. Hermes `--usage-file` exists (`run_agent.sh:576-580`). Claude and Codex usage is unavailable until §1's transcripts land.
   
   The supervised vs unsupervised comparison stays in the existing A/B lanes. Lane A gets no harness context and lane B is the shipped harness (`strategies.py:7-14`).

### Schema and CLI

- New subcommand in the benchmark package. The benchmark package is not on the compatibility table's CLI list (`compatibility.md:21`), so it starts as **experimental**.
- The dataset file format is versioned and owned by `replay.py`, not a consumer contract.
- Supervisor change: only the `stop_code` prerequisite (patch).
- Optional and patch: record `dirty_paths_before_run`/`after_run` in the decision report so `rules` mode is exact for uncommitted dirt (`compatibility.md:24`).

### Files

- `automation/benchmark/replay.py`, plus `__main__` wiring, and a manifest entry under the `benchmark` component, as for `automation/benchmark` today (`automation/reusable-manifest.json` entries).
- Tests in `automation/tests/test_benchmark_replay.py`.

### Tests

- A synthetic Git fixture repo with three handoffs: one `done` in scope, one out of scope, one over budget.
- `rules` mode with an unchanged policy reproduces every recorded `stop_code`.
- A candidate that drops the `max_files_changed` check flags the over-budget case as a dangerous flip.
- `prompt` mode reports an unknown placeholder.
- `rerun` refuses without an explicit `--approved-run <id>` flag and never runs in `make reusable-check`.

### Risks

- **Selection bias.** Handoffs exist only for runs that wrote one. `handoff_missing` failures exist only in decision reports, so both sources are needed.
- **Re-running a slice at an old `base_sha` may need network or dependencies that are gone.** `rerun` records such failures as `env_unavailable`, never as an agent regression.
- **Consumer handoffs may hold private data.** The tool reads only what the consumer points it at, and writes outside the repository.

### P0 interaction

The `rules` and `prompt` modes are offline tooling for harness effectiveness. Put under `automation/benchmark/`, they fall under the benchmark exemption. That classification is the owner's call. `rerun` is benchmark spending and needs a person's approval for each run, model or budget.

---

## 3. CI canary against a real runner (rank 3)

### What ships today

- **A real runner already runs in CI, but only as the benchmark.** `effectiveness-benchmark.yml` is `workflow_dispatch` only (`.github/workflows/effectiveness-benchmark.yml:24-25`), runs Hermes against NVIDIA with `NVIDIA_API_KEY` (`:93-108`), and has `permissions: contents: read` (`:77-78`). It runs benchmark lanes, not `run_next` on a queue. That is why the support matrix still says Hermes is "fakes only" for the supervisor path (`README.md:164`).
- **The Claude runner has a budget cap.** `--max-budget-usd`, default 5, is passed when a profile is chosen (`run_agent.sh:290-302`; `runner_profile.py:35`).
- **The RA-BD-004 Claude Code canary is done (2026-11-10); the Codex canary is deferred** (`docs/product-roadmap.md:172-173`).

### Scope

New workflow `.github/workflows/runner-canary.yml`:

- **Trigger:** `workflow_dispatch` only. No `push`, `pull_request` or `schedule`, matching the benchmark's reasoning (`effectiveness-benchmark.yml:13-22`).
- **Inputs:** `runner` (`claude|codex|hermes`), `ref`, `budget_usd` (default 1, maximum 5).
- **Job:**
  1. Build a throwaway consumer in `$RUNNER_TEMP` from this checkout with `Tools/boundroom-sync.sh --sync --target`, following the smoke steps already tested (`docs/workflows/boundroom.md:403-450`).
  2. Write a fixture queue of three slices: a **positive** slice (a one-file doc edit, `git diff --check` replayed), a **scope negative** whose notes ask for a file outside `allowed_paths`, and a **budget negative** with `max_files_changed: 1` and a two-file task.
  3. Run `run_next.py --single-slice --report-file "$RUNNER_TEMP/report.jsonl"` once per slice.
  4. Assert the stop codes: the positive slice gets `continue` or `review_required`, the negatives get `scope_violation` and `diff_budget_exceeded`, and the throwaway repository's scope check holds.
  5. Upload the reports, the handoffs and (after §1) the session records as an artifact.
- **Telemetry:** file mode only (`BOUNDROOM_TELEMETRY=file`). No hosted destination without a separate owner decision.

**Needs from a person**, each a separate recorded approval under AGENTS.md:

1. **Secrets** in a GitHub Environment `runner-canary` with required reviewers. This is the first use of a protected environment for spending; `publish-docs.yml:59` uses one for Pages. Possible secrets are `ANTHROPIC_API_KEY` (or an approved Claude Code credential), `OPENAI_API_KEY` for Codex, or the existing `NVIDIA_API_KEY` for Hermes. The agent never reads or handles them.
2. **Budget:** a per-run cap, enforced by `--max-budget-usd` for Claude. Codex and Hermes have no equivalent cap in `run_agent.sh` today. Hermes has only `BOUNDROOM_HERMES_MAX_TURNS`, on stream-json (`run_agent.sh:376-395`). For those runners the job `timeout-minutes` is the cap. Expected cost per run is under the cap; each run is spending.
3. **Merging the workflow PR** (code-owner approval, under the `main` ruleset).
4. **Each dispatch** is a run started by a person. The agent may prepare it but not start it.

### Compatibility

Patch. It adds a workflow and touches nothing in the consumer surface. The support matrix row changes from "fakes only" to "canaried in CI on <date>, <version>" only after a green run. `test_support_matrix.py` (`automation/tests/test_support_matrix.py:45-68`) gains a check that the README row names the workflow.

### Tests

The workflow is not a required check. A unit test confirms that the fixture queue validates against `slice.schema.json` and that the assertion script maps each expected `stop_code`. An `actionlint`-style test confirms the triggers include no `push` or `pull_request`.

### Risks

- **Provider outages look like agent failures.** The benchmark already learned this (`run_agent.sh:641-646`). Classify `agent_exit_nonzero` separately from stop codes.
- **A real agent with `bypassPermissions`** (`run_agent.sh:304-308`) on a GitHub runner touches only a throwaway repository with no push credentials. Set `permissions: contents: read` and do not persist checkout credentials (`persist-credentials: false`).

### P0 interaction

This is a canary, which P0 asks for. The Codex canary is already a deferred RA-BD-004 item. It is the strongest case for doing this early, but approvals gate it.

---

## 4. Structured reviewer verdict and optional LLM judge (rank 4)

### What ships today

- **A reviewer prompt with fixed output headings**, but no schema: Stop reason, Changed scope, Cleanup required, Proof level, Contract status, Residual risk, Repo state, Approval required, Queue recommendation (`automation/prompts/review.md`).
- **`residual_risks` is a required non-empty list of strings** (`models.py:188`), seeded with `"<remaining risk or 'No known residual risk.'>"` (`automation/context/build_context.py:202`).
- **An LLM judge already exists** in the benchmark (`automation/benchmark/judge.py:1-24`, `:243-314`). It runs through the same runner seam in a fresh `git init` workspace (`:258-262`). Its verdict is capped at 64 KiB, and anything malformed is an error, never a pass (`:62-79`, `:145-176`). It is blind to the lane (`:340`) and copies session files without following links (`:299-301`). It can never pass failing tests (`:12-13`).

### Design decision: where the verdict lives

The request asks for the verdict "in the handoff". The handoff is written by the runner, the change's author. A reviewer verdict inside it would let the author write its own review, which breaks segregation of duties (AGENTS.md). `Handoff` already rejects unknown keys (`models.py:177`, `extra="forbid"`), so a runner can't slip a `review` key in today. Keep it that way.

The verdict goes in a **sidecar record bound to the handoff's digest**: `automation/reviews/<handoff-stem>.review.json`. Its new schema is `automation/schemas/review.schema.json`, generated from a `Review` model added to `CONTRACTS` (`models.py:200-204`). Fields:

- `handoff_sha256`;
- `slice_id`;
- `reviewer`: `{kind: human|llm_judge, actor}`. For a person, `actor` is their login. For the judge it is `"<runner> (llm_judge)"`. The judge is never presented as a person.
- `verdict`: `accept|accept_with_followup|rework|reject`;
- `intent_match`: 0-10;
- `scope_respected`: bool;
- `risks_confirmed`: the handoff's `residual_risks` the reviewer agrees with;
- `risks_found`: new risks;
- `risks_dismissed`;
- `rework_required`: bool;
- `notes`;
- `created_at`.

`risks_found` minus `risks_dismissed`, over time, is the measurable "what risk remains".

### Scope

1. **Schema and model.** Add `automation/reviews/` to the default `supervisor_owned_paths` in `boundroom init` (§5). Today the example lists only the queue and handoffs (`automation/examples/example-slices.json` policy). Existing consumers opt in by editing their queue.
2. **`handoff_summary.py --review <path>`** reads and checks the sidecar: digest match, schema. It renders it beside agent claims and supervisor evidence (`handoff_summary.py:214-290`). It never writes.
3. **`automation/benchmark/intent_judge.py`** (optional, spending). Adapt `Judge` (`judge.py:243`). Its input is the slice's stated intent (`title`, `notes`, `allowed_paths`, `required_validations` from `SliceRecord`, `models.py:130-144`), the `git diff base_sha..head_sha` and the handoff's claims. Its output is a `Review` with `reviewer.kind=llm_judge`. It reuses `_verdict_bytes` limits and its "malformed is error" rule, and runs blind in a fresh workspace with no route to the consumer repository. Its verdict is advisory. It never changes the queue, never approves a board task, never stands in for the person's approval, and is not "independent live verification" (AGENTS.md).
4. **§2 integration.** `replay.py` reports how often judge and human agree, and `risks_found` per `stop_code`.

### Compatibility

- New schema file and optional sidecar: patch ("Accept more input", `compatibility.md:23`). The `Handoff` schema does not change, which avoids a trap. A new optional handoff field is classed as patch, but an older synced supervisor would *reject* handoffs that carry it, because `extra="forbid"` (`models.py:177`). That makes it effectively breaking across mixed versions.
- `handoff_summary.py --review`: patch, as a new flag whose absence keeps today's behavior (`compatibility.md:21`).
- No exit-code changes.

### Tests

- Generated-schema check (`automation/schemas/generate.py --check`, run by `make reusable-check`).
- `handoff_summary --review` rejects a digest mismatch and an unknown key.
- The judge with a fake runner. The cases are verdict not JSON, score out of range, oversized verdict and a symlinked verdict, each yielding an error and never a pass (mirror `automation/tests/test_benchmark_judge.py`).
- The judge workspace contains no path to the consumer repository.

### Risks

- **The judge can be argued around** by a diff written to persuade it. Mitigation: advisory only, plus §2 agreement tracking.
- **Reviewer burden.** The sidecar is optional. A reviewer who already writes PR reviews can let the judge draft one, then edit and sign it as themselves.

### P0 interaction

The schema and `handoff_summary` flag are supervisor-adjacent feature expansion. The reviewer decides whether a small additive slice may proceed before P0. The judge is benchmark-side and spending.

---

## 5. `boundroom init` (rank 5)

### What ships today

- **The checklist and bootstrap are manual** (`README.md:69-75`; `docs/workflows/boundroom.md:399-450`). The only template is Owlory-specific.
- **The queue contract.** `SliceRecord` and `QueuePolicy` fields are at `models.py:122-144`. Proof levels are at `models.py:53-62`.
- **Runner rules.** `automation/runner-rules.md` is consumer-owned, capped at 32 KiB, refused if it is a symlink, and never written by the sync (`run_next.py:673-705`).
- **Inferred commands will mostly be agent-reported.** Only `make architecture` and `git diff --check` are supervisor-replayed (`policy.py:171-174`, `:567-588`). `init` must say so, not imply that inferred commands are independently verified.
- **No `gh` usage exists in the supervisor today.** `operator_status.py` notes its only subprocesses are read-only `git` (`automation/supervisor/operator_status.py:19`).

### Scope

New `automation/supervisor/init.py`. It ships to consumers automatically, because the manifest syncs `automation/supervisor` as a directory (`automation/reusable-manifest.json`). An optional plugin skill `boundroom-init` drives it. The skill holds instructions only, per SPEC-002 rules.

- **`init.py plan` (default; read-only, writes nothing):**
  - **Validations, in order:**
    - `Makefile` targets named `test`, `check`, `lint`, `typecheck`, `architecture`;
    - `pyproject.toml`: `[tool.pytest.ini_options]` gives `python -m pytest`, `[tool.ruff]` gives `ruff check .`, and `[tool.uv]` or `uv.lock` adds a `uv run` prefix;
    - `package.json`: the `test`, `lint` and `typecheck` scripts, with the package manager taken from the lockfile.
    
    Always add `git diff --check`, the replayable one. Each command is labeled with its tier from `classify_validation_ownership`.
  - **`allowed_paths`:** top-level source directories from `git ls-files`, grouped by language. `tests/`, `docs/` and generated directories are marked separately. It never proposes `.github/`, `automation/queue/`, `automation/handoffs/` or lock files.
  - **Policy:** `agent_command_template` = the `run_agent.sh` template from the example. `supervisor_owned_paths` = the queue, handoffs and (with §4) reviews. `consecutive_autonomous_limit: 1`, `handoff_timeout_seconds: 1800`.
  - **Output:** a proposed `slices.json` with one `status: "deferred"` sample slice, a starter `runner-rules.md`, and a `.gitignore` line for `__pycache__/` (the known failure mode, `docs/workflows/boundroom.md:438-442`). Printed as JSON and a diff.
- **`init.py write`:** writes only files that don't exist, refuses to overwrite, and refuses outside the repository root. It leaves the bootstrap commit to the person, matching the existing contract (`docs/workflows/boundroom.md:420-432`). It validates the result with `policy.load_queue` (`policy.py:252`).
- **`init.py from-issue <number|url>`:**
  - runs `gh issue view --json number,title,body,labels,url`, read-only, and never writes to GitHub;
  - produces a draft slice with `status: "deferred"`, `entry_condition: "person reviews scope"`, notes that quote the issue URL, and `allowed_paths` inferred from paths mentioned in the body and intersected with tracked files;
  - prints it, and appends it to the queue only with `--append`, which never sets `queued`;
  - passes the `gh` call through `egress.guard_egress` (`egress.py:46-55`) so `BOUNDROOM_OFFLINE` refuses it;
  - treats issue text as untrusted data: never executed, length-capped and quoted.

### Compatibility

Patch: a new entry point and flags whose absence changes nothing (`compatibility.md:21`). Add `init.py` to the compatibility table's list of CLIs in the same PR, so its flags become surface. No schema change.

### Tests

- Fixture trees for a Python-only, Node-only and Make-only repository give the expected commands and tiers.
- `write` refuses to overwrite and refuses symlinks, following the runner-rules symlink handling (`run_next.py:686-687`).
- The generated queue passes `load_queue` and `run_next.py --dry-run`.
- `from-issue` with a fake `gh` on `PATH` never passes a write subcommand. Assert that the argv is exactly `issue view`.
- Offline mode refuses before the subprocess runs.

### Risks

- **Over-broad `allowed_paths` weakens the scope check.** Mitigations: the conservative defaults above, and the sample slice stays `deferred`.
- **People may read inferred commands as supervisor-verified.** The tier label and the README must say otherwise.
- **The issue body can carry prompt injection** into slice notes, which reach the agent prompt (`run_next.py:731`). Mitigation: the slice stays `deferred` until a person edits it, and the notes are quoted and capped.

### P0 interaction

This is feature expansion of the supervisor's adoption path, roadmap P1 (`docs/roadmap.md:24`). Under AGENTS.md it waits until the P0 gate unless the owner reorders. It is the right next item once P0 clears.

---

## Performance note (AGENTS.md)

- §2 `rules --replay` and §3 add subprocess work in benchmark and CI tooling, not in the supervisor's per-slice path.
- §1 adds a file write in `run_agent.sh`, only when the operator sets `BOUNDROOM_SESSION_DIR`.
- §4 adds a file read in `handoff_summary.py`, only with `--review`.
- §5 runs `git ls-files`, which scales with repository size, only on demand and never per slice.

Each implementing PR states these in its description.

## Open decisions for the owner

1. Whether §2 `rules`/`prompt` mode and the §4 judge count as benchmark work (exempt from P0) or as supervisor feature work.
2. Whether to send runner telemetry to a hosted backend (Braintrust) for the §1 verification.
3. Which runner and secret the §3 canary uses first, and the per-run budget.
4. Whether `automation/reviews/` should become a default `supervisor_owned_paths` entry for new consumers.
