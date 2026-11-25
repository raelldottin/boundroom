# SPEC-013 (proposed): Adoption and evals

Status: **proposed.** Nothing in this document is implemented. Every claim about current behavior cites the code at `219cbc0`, the head of #294 when this was written (v0.5.3 plus that pull request's benchmark changes) (`file:line`). Hermes Kanban, Hindsight, the Swarm Wiki and supervisor-controlled delegation are target architecture ([SPEC-001](control-plane.md)), not shipped. This spec does not depend on any of them. Line citations are to the code at the time of writing and will be updated to the merge commits when #294 merges.

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
| 3 | §3 Supervisor canary: scripted stops in CI, a real runner on the owner's machine | M | High credibility: every stop is checked end to end on every pull request, and the Claude Code canary becomes repeatable | Not P0's canary (P0.4 and P0.5 canary the Kanban-composed supervisor). The scripted part is test coverage. The real-runner part runs on the owner's machine under AGENTS.md. A CI version needs an owner decision to amend AGENTS.md. |
| 4 | §4 Structured reviewer verdict and optional LLM judge | M | Medium-high: makes "what risk remains" measurable over time | A person's verdict is a GitHub review, checked read-only. The block schema and the read-only flag are a small supervisor addition. The judge reuses benchmark code, is spending, and never counts as a review. |
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

## 3. Supervisor canary: scripted stops in CI, a real runner on the owner's machine (rank 3)

### What ships today

- **A real runner already runs in CI, but only as the benchmark.** `effectiveness-benchmark.yml` is `workflow_dispatch` only (`.github/workflows/effectiveness-benchmark.yml:24-25`), runs Hermes against NVIDIA with `NVIDIA_API_KEY` (`:93-108`), and has `permissions: contents: read` (`:77-78`). It runs benchmark lanes, not `run_next` on a queue. That is why the support matrix still says Hermes is "fakes only" for the supervisor path (`README.md:164`).
- **The Claude runner has a budget cap.** `--max-budget-usd`, default 5, is passed when a profile is chosen (`run_agent.sh:290-302`; `runner_profile.py:35`).
- **The `bounded` and `edits` profiles keep a compliant agent in scope.** They give Claude Code a per-slice allow list of the slice's `allowed_paths`, its validations and a few `git` commands, and deny the supervisor-owned paths, `.git`, `.claude`, `~/.ssh`, `~/.aws`, `~/.config` and the web tools (`runner_profile.py:320-341`; modes at `:34`). An edit outside `allowed_paths` is refused before it reaches the disk.
- **A judged stop exits 0.** `run_next.py` records `stop_failed`, `stop_blocked` or `stop_for_review` in the decision report and the queue, then exits 0 (`docs/reference/exit-codes.md:9`; `run_next.py:582`). A failure report, such as a missing handoff, exits 2 and marks the slice `failed` (`run_next.py:404-415`).
- **The gate tests call `evaluate_completion` directly** (`automation/tests/test_harness.py:1419`, `:1441`). No test runs `run_next.py` end to end against a freshly synced consumer and checks every stop.
- **The RA-BD-004 Claude Code canary is done (2026-11-10); the Codex canary is deferred** (`docs/product-roadmap.md:172-173`).

### Why the stop cases use a script, not a model

An earlier draft asked a real agent for a file outside `allowed_paths` and expected `scope_violation`. That asserts the misbehaviour the supervisor exists to prevent. Under `bounded` the edit is refused, so a compliant agent never produces a violation, and the canary would fail when the agent behaves well. A model's exact stop code also isn't repeatable from run to run. So the canary has two parts: a scripted runner exercises every stop, and a real runner covers only a slice that should succeed.

### Part A: scripted runner, in CI on every pull request

- **The runner is a fixed script.** `automation/tests/fixtures/scripted_runner.py` stands in for the agent. `run_next.py` starts it through `--agent-cmd`, with the placeholders the example template uses (`{repo_root}`, `{handoff_file}`, `{slice_id}`; `automation/examples/example-slices.json:6`). It reads the slice id and makes exactly the commits and the handoff that case calls for. It uses no model, no network, no secret and no money.
- **Setup.** Each test builds a throwaway consumer in a temporary directory from this checkout with `Tools/boundroom-sync.sh --sync --target`, following the smoke steps already tested (`docs/workflows/boundroom.md:403-450`), and writes a fixture queue. It points `XDG_STATE_HOME` at a temporary directory, so the per-user run lock (`runner_profile.py:425-439`) can't collide with a real run.
- **Cases.** Each is one `run_next.py --single-slice --report-file <tmp>/report.jsonl`. The supervisor checks validation, then scope, then the diff budget, then proof (`policy.py:985`, `:1002`, `:1019`, `:1038`), so each case passes every gate before the one it targets.

  | Case | What the script does | Decision | `stop_code` | Queue status | Exit |
  | --- | --- | --- | --- | --- | --- |
  | positive | Edits one allowed file, commits it, writes a valid `done` handoff | `continue` or `stop_for_review`, fixed by the fixture's policy and pinned by the test | the matching `next_slice_eligibility` code | `done` | 0 |
  | scope | Commits one allowed file and one file outside `allowed_paths`, reports `done` | `stop_failed` | `scope_violation` | `failed` | 0 |
  | budget | Commits two allowed files under `max_files_changed: 1`, reports `done` | `stop_failed` | `diff_budget_exceeded` | `failed` | 0 |
  | proof | Commits in scope and reports `done`, without the slice's `manual_proof` entry or `required_proof_level` | `stop_failed` | `proof_missing` | `failed` | 0 |
  | no handoff | Commits in scope and exits 0 without writing a handoff | failure report | `handoff_missing` | `failed` | 2 |

- **What the tests assert are supervisor invariants:** the decision, the stop code, the slice's status in `slices.json` after the run, the exit code, and the report's structured fields (`files_touched_outside_scope`, `changed_file_count`, `missing_manual_proofs`, `missing_proof_level`). They never read the prose `reason` (`compatibility.md:31`). Until the `stop_code` prerequisite lands, the tests assert the other four, and that prerequisite's pull request adds the code to them.
- **Where it runs.** `automation/tests/test_supervisor_canary.py`, an ordinary test module, so the required test checks run it on every pull request. That adds no workflow, no required check and no ruleset change.

### Part B: a real runner on the positive slice, on the owner's machine

- **Runner: Claude Code only.** AGENTS.md allows canaries in disposable environments "on the owner's machine only with Claude Code", and Claude Code is the only approved tool on that machine. The Codex canary stays deferred (`docs/product-roadmap.md:173`).
- **An owner-run command.** `Tools/runner_canary.sh --out <directory outside the repository>`. The owner runs it, or asks Claude Code on the same machine to run it, which AGENTS.md allows for a canary in a disposable environment. It builds a throwaway consumer as Part A does, with no remote, queues one tiny in-scope slice (append a line to one Markdown file, with `git diff --check` as the required validation), and runs `run_next.py --single-slice --runner-profile bounded` with the real `run_agent.sh`. It keeps the profile's default cap. A different cap is a budget change and needs a person's approval. Each run is a Claude Code session on the owner's account.
- **What it checks.** These hold whatever the model writes. It never compares the model's output with an expected text:
  1. the run produced exactly one decision report or failure report, and the report parses;
  2. the decision is one of the documented values (`docs/reference/exit-codes.md`), and the slice's queue status matches it;
  3. if a handoff exists, it validates against `handoff.schema.json`;
  4. nothing is out of scope. The script computes this itself in the throwaway repository, from `git diff --name-only base_sha..head_sha` and `git status --porcelain`, and every path must be in `allowed_paths` or be the handoff, unless the decision is `stop_failed` and the report lists that path;
  5. the cap held. The supervisor passed `BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD` to the runner, and once §1's transcript exists, the session's reported cost is at or under it.
- **Result.** `pass` when 1-5 hold and the decision is `continue` or `stop_for_review`. `runner_incomplete` when 1-5 hold but the model stopped, failed, timed out or wrote no handoff. That is a model outcome, recorded but not a supervisor failure. `invariant_broken` when any of 1-5 fails, which is a supervisor bug and gets an issue.
- **Recorded output.** `<out>/canary-record.json` holds the date, the Boundroom commit, `claude --version`, the profile, the cap, the decision, the stop code, the result, the actor (the owner, or `claude-code (for raelldottin)` when Claude Code ran it), and the sha256 of the report and the handoff. It holds no transcript, prompt or source text. After a `pass`, a pull request a person approves changes the README support row to "canaried on the owner's machine on <date>, <version>".

### Not proposed: a real runner in CI

A GitHub-hosted runner with an API key is not "the owner's machine with Claude Code", so a real-runner job in CI is outside what AGENTS.md allows today. It would need all of these, each a separate recorded decision by a person:

1. an owner decision to amend AGENTS.md's canary rule, through a pull request a person approves;
2. spending approval for each run, and a per-run budget;
3. secrets in a protected GitHub Environment with required reviewers, which the agent never reads or handles;
4. a decision on running Claude Code, or another runner, with a credential away from the owner's machine.

Until then there is no `runner-canary.yml`. If the owner decides for one, it reuses Part B's script and checks unchanged, as `workflow_dispatch` only, with `permissions: contents: read` and `persist-credentials: false`.

### Compatibility

No consumer surface changes. Part A adds a test module and a fixture. Part B adds `Tools/runner_canary.sh`, which is not added to `automation/reusable-manifest.json`, so consumers don't receive it. The support matrix row changes only after a recorded `pass`, and `test_support_matrix.py` (`automation/tests/test_support_matrix.py:45-68`) gains a check that the row names a date and a version.

### Tests

- Part A is itself the test. It also checks that the fixture queue validates against `slice.schema.json` and that the scripted runner refuses an unknown case.
- Part B is tested with a fake `claude` on `PATH`, as `test_runner_selection.py` does with fake CLIs. An in-scope fake gives `pass`. A fake that writes no handoff gives `runner_incomplete`. A doctored report whose queue status disagrees with its decision gives `invariant_broken`. No test calls a real model.

### Risks

- **Provider outages look like agent failures.** The benchmark already learned this (`run_agent.sh:641-646`). Part B classes `agent_exit_nonzero` and `agent_timeout` as `runner_incomplete`, never as `invariant_broken`.
- **Part B runs a real agent on the owner's machine.** `bounded` limits its edits to the slice's allowed paths and denies the credential directories and web tools (`runner_profile.py:320-341`). The throwaway repository has no remote, so the `git push` in the allow list has nowhere to go.
- **Part A depends on `boundroom-sync.sh`.** If the sync changes, these tests change with it. That is intended: they test what a consumer receives.

### P0 interaction

P0's canaries are the end-to-end one-slice canary of the Kanban-composed supervisor (P0.4) and the canary of each opted-in consumer during rollout (P0.5) (`docs/roadmap.md:22-23`). This canary is neither. It exercises today's file-queue supervisor and does not satisfy or replace either gate, though P0.4 can reuse its checks later. Part A is test coverage of existing gates, a bounded change the reviewer can accept before P0. Part B repeats the RA-BD-004 Claude Code canary, done once on 2026-11-10, in a repeatable form.

---

## 4. Structured reviewer verdict and optional LLM judge (rank 4)

### What ships today

- **A reviewer prompt with fixed output headings**, but no schema: Stop reason, Changed scope, Cleanup required, Proof level, Contract status, Residual risk, Repo state, Approval required, Queue recommendation (`automation/prompts/review.md`).
- **`residual_risks` is a required non-empty list of strings** (`models.py:188`), seeded with `"<remaining risk or 'No known residual risk.'>"` (`automation/context/build_context.py:202`).
- **An LLM judge already exists** in the benchmark (`automation/benchmark/judge.py:1-24`, `:243-314`). It runs through the same runner seam in a fresh `git init` workspace (`:258-262`). Its verdict is capped at 64 KiB, and anything malformed is an error, never a pass (`:62-79`, `:145-176`). It is blind to the lane (`:340`) and copies session files without following links (`:299-301`). It can never pass failing tests (`:12-13`).
- **GitHub already authenticates pull request reviews.** Each review records the account that submitted it, its state (`APPROVED`, `CHANGES_REQUESTED` or `COMMENTED`), the commit it was submitted on (`commit_id`) and its body. GitHub doesn't count an author's review of their own pull request, and the `main` ruleset requires a code owner's approval ([control map](../compliance/controls.md), change management).

### Design decision: a person's verdict is a GitHub review, never a file

The request asks for the verdict "in the handoff". The handoff is written by the runner, the change's author. A reviewer verdict inside it would let the author write its own review, which breaks segregation of duties (AGENTS.md). `Handoff` already rejects unknown keys (`models.py:177`, `extra="forbid"`), so a runner can't slip a `review` key in today. Keep it that way.

An earlier draft moved the verdict to a sidecar file, `automation/reviews/<handoff-stem>.review.json`, with `reviewer: {kind: human|llm_judge, actor}`. That fails for the same reason. `actor` is an unauthenticated string, so an agent could write `{kind: human, actor: raelldottin}` and present its own output as a person's review, which AGENTS.md forbids. `automation/reviews/` would be supervisor-owned only in consumers that `boundroom init` set up, so in existing consumers a runner could commit its own review. So:

- **A person's verdict is a pull request review on GitHub**, submitted from that person's own account. GitHub authenticates it. Boundroom keeps no verdict that a tool trusts. It keeps at most a reference, `{repository, pr_number, review_id, reviewer_login, reviewer_id, commit_id}`, and a tool re-checks that reference through the GitHub API, read-only, every time it counts it. No field in any file can assert that a verdict is a person's.
- **An LLM judge's verdict is a model's output.** It is recorded separately, labelled with its model id and prompt version, and never counts as a review or an approval (see [The LLM judge, kept apart](#the-llm-judge-kept-apart)).

### The verdict block

The structured fields go in one fenced block in the review body, with the info string `boundroom-review`. The block is JSON, so the parser needs only the standard library:

````markdown
```boundroom-review
{
  "verdict": "rework",
  "intent_match": 6,
  "scope_respected": true,
  "risks_confirmed": ["No test covers an empty table."],
  "risks_found": ["The migration locks the table during the backfill."],
  "risks_dismissed": [],
  "lines_edited": 40,
  "notes": "Split the backfill into batches."
}
```
````

Fields:

- `verdict` (required): `accept|accept_with_followup|rework|reject`;
- `intent_match`: 0-10;
- `scope_respected`: bool;
- `risks_confirmed`: the handoff's `residual_risks` the reviewer agrees with;
- `risks_found`: new risks;
- `risks_dismissed`;
- `lines_edited`: lines the reviewer changed or asked to have changed, 0 or more;
- `slice_id`: required only when the pull request changes more than one handoff;
- `notes`;
- `experiment`: optional, used only by [SPEC-012](effectiveness-proof.md)'s blinded review: `{minutes, reasons, arm_guess}`.

The block carries no reviewer, time or commit. Those come from GitHub's record of the review, never from text the reviewer or anyone else typed. The earlier `rework_required` field is dropped, because `verdict` already says it.

The block describes a review. It doesn't make one. GitHub's `APPROVED` state is the approval the ruleset counts, and the block can't turn a `COMMENTED` review into one. `accept` and `accept_with_followup` are valid only in an `APPROVED` review. `rework` and `reject` are valid only in a `CHANGES_REQUESTED` or `COMMENTED` review.

`risks_found` minus `risks_dismissed`, over time, is the measurable "what risk remains".

### How a review is checked, and what happens when the block is missing or malformed

The tool reads the reviews of one named pull request and gives each review one outcome:

- **`counted`**: exactly one well-formed block, a state that matches the verdict, and a reviewer who passes the identity checks below;
- **`no_verdict`**: no block. The review still exists, and still counts for merging under GitHub's rules, but the metrics count it as missing, never as `accept`;
- **`invalid: <reason>`**: more than one block, a block that isn't JSON, an unknown key, a value out of range or not in the list, a block over 8 KiB, a verdict that doesn't match the review state, or a body edited by someone other than its author. It is not counted, and nothing is filled in with a default;
- **`not_a_person`**: the reviewer's account type is `Bot`, the reviewer is the pull request's author, or the review was submitted on a commit that doesn't hold the handoff being reviewed. It is not counted.

The tool never edits, comments on or resubmits a review. A reviewer fixes a missing or malformed block by submitting a new review. When one reviewer submits several reviews, the latest `counted` one on the commit that holds the handoff is the one used.

The review is bound to the handoff through Git, read-only: the tool reads the handoff at the review's `commit_id` (`git show <commit_id>:<handoff path>`) and records its sha256. If the pull request's head changes after the review, the review covers the old head only, as GitHub's stale-approval rule already treats it.

### Why the runner can't write or change a verdict

- **The verdict exists only as a review on GitHub.** Nothing the runner writes is read as a verdict: not the handoff, not a file under `automation/reviews/`, not a commit message, not a pull request comment. The tool reads only the reviews of the pull request it is given.
- **A person creates the reference by submitting a review on GitHub.** Boundroom doesn't create it. A tool may cache what it found outside the repository, but every count re-reads the API.
- **The agent's own reviews don't count.** If the runner or the agent posts a review, it does so as `boundroom-agent[bot]` (bot user id `354874784`), whose account type is `Bot`. The agent usually authored the pull request too, and GitHub doesn't count an author's approval either.
- **Edits are checked.** On GitHub, anyone with write access can edit another person's comment, and the agent's app has write access to pull requests. So the tool reads each review's edit history (GraphQL `userContentEdits`, a read-only query) and refuses a review whose body was edited by anyone other than its author, or whose editors it can't read.
- **What remains.** The check proves which GitHub account submitted the review, not who was at the keyboard. An agent using the owner's own `gh` login on the owner's machine could submit a review as the owner. The control map records that same gap for the agent's identity: "Nothing technically stops the agent from using the owner's own `gh` login on the same machine" ([control map](../compliance/controls.md)). It applies to merge approvals equally. A verdict is exactly as strong as the pull request approval control, and no stronger.

### The LLM judge, kept apart

`automation/benchmark/intent_judge.py` is optional and is spending. It adapts `Judge` (`judge.py:243`). Its input is the slice's stated intent (`title`, `notes`, `allowed_paths`, `required_validations` from `SliceRecord`, `models.py:130-144`), the `git diff base_sha..head_sha` and the handoff's claims. It reads its verdict file with the same size limit, no-symlink read and "malformed is an error" rule as the benchmark judge, through `read_agent_file` in `automation/benchmark/adapter.py` and `_read_verdict` in `automation/benchmark/judge.py` (as of #294's head, `3c07095`). It runs blind in a fresh workspace with no route to the consumer repository.

Its output is a separate record with its own model and schema, sharing no type with anything a review produces:

- `kind`: always `model_output`;
- `model`: the model id the session reported, or the one the run pinned. A record without one is refused;
- `runner`;
- `prompt_version`: the sha256 of the judge prompt;
- `verdict`, `intent_match`, `risks_found`, using the review block's values;
- `created_at`.

It is written to the benchmark's output directory, outside the consumer repository, and is never posted to GitHub. The rules:

- It never counts as a review or an approval. No tool adds it to a count of people's verdicts, and every report labels it "model output (<model>, prompt <version>)".
- It is never shown to a reviewer as a draft, a pre-filled block or a suggestion before they submit their own review. That would anchor the reviewer and turn a model's verdict into a person's. §2 compares the two only after both exist.
- It is advisory. It never changes the queue, never approves a board task, never stands in for a person's approval, and is not "independent live verification" (AGENTS.md).

### Scope

1. **Schema and model for the block.** `automation/schemas/review-block.schema.json`, generated from a `ReviewBlock` model added to `CONTRACTS` (`models.py:200-204`), with `extra="forbid"`. It describes the block only. It has no `reviewer` or `kind` field, because identity comes from GitHub.
2. **`handoff_summary.py --review-pr <owner/repo>#<number>`.** It reads the pull request's reviews and their edit history with `gh api`, read-only, through `egress.guard_egress` (`egress.py:47`) so `BOUNDROOM_OFFLINE` refuses it. It binds each review to the handoff as above and renders the outcomes beside agent claims and supervisor evidence (`handoff_summary.py:214-290`), with the reviewer's login and the review's URL. It never writes to GitHub or to the repository.
3. **The intent judge**, as above.
4. **§2 integration.** `replay.py` counts only reviews the same check passes. It reports `risks_found` per `stop_code` and how often the judge and people agree, with the two in separate columns.

### Compatibility

- New schema file: patch ("Accept more input", `compatibility.md:23`). The `Handoff` schema does not change, which avoids a trap. A new optional handoff field is classed as patch, but an older synced supervisor would *reject* handoffs that carry it, because `extra="forbid"` (`models.py:177`). That makes it effectively breaking across mixed versions.
- `handoff_summary.py --review-pr`: patch, as a new flag whose absence keeps today's behavior (`compatibility.md:21`). Without the flag it makes no network call.
- No exit-code changes.

### Tests

- Generated-schema check (`automation/schemas/generate.py --check`, run by `make reusable-check`).
- The block parser, on recorded review bodies: a missing block gives `no_verdict`. Two blocks, bad JSON, an unknown key, a value out of range, an oversized block and a verdict that contradicts the review state each give `invalid` with its reason, and none is counted.
- Identity, on recorded API responses with no network: a `Bot` reviewer, the author reviewing their own pull request, a review on a commit without the handoff, a body edited by another account, and unreadable edit history are each not counted. A file in the repository that claims a person's verdict, such as `automation/reviews/x.review.json` with `kind: human`, is ignored.
- With a fake `gh` on `PATH`: the argv is only `api` reads, and the GraphQL query contains no `mutation`. With `BOUNDROOM_OFFLINE` set, the tool refuses before the subprocess runs.
- The judge with a fake runner. The cases are verdict not JSON, score out of range, oversized verdict and a symlinked verdict, each yielding an error and never a pass (mirror `automation/tests/test_benchmark_judge.py`). A record without a model id is refused. Judge output never appears in the people's section of the `--review-pr` rendering.
- The judge workspace contains no path to the consumer repository.

### Risks

- **The judge can be argued around** by a diff written to persuade it. Mitigation: advisory only, plus §2 agreement tracking.
- **Reviewer burden.** The block is optional. A review without it still counts for merging and shows as `no_verdict` in the metrics. A GitHub saved reply can hold the empty template. The judge never drafts it.
- **Only GitHub-hosted pull requests are covered.** For a consumer hosted elsewhere, the tool reports `unsupported_host` and counts nothing.
- **Identity rests on the GitHub account**, with the gap described above.

### P0 interaction

The schema and the `handoff_summary` flag are supervisor-adjacent feature expansion, and the flag adds the first `gh` call to the supervisor package (§5 notes there is none today). The reviewer decides whether a small additive slice may proceed before P0. The judge is benchmark-side and spending.

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
  - **Policy:** `agent_command_template` = the `run_agent.sh` template from the example. `supervisor_owned_paths` = the queue and handoffs. `consecutive_autonomous_limit: 1`, `handoff_timeout_seconds: 1800`.
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

- §2 `rules --replay` adds subprocess work in benchmark tooling, not in the supervisor's per-slice path.
- §3 Part A adds a few `git` repositories and `run_next.py` runs per test run, in the test suite only. Part B runs only when the owner starts it.
- §1 adds a file write in `run_agent.sh`, only when the operator sets `BOUNDROOM_SESSION_DIR`.
- §4 adds `gh api` network reads in `handoff_summary.py`, only with `--review-pr`, growing with the number of reviews on one pull request.
- §5 runs `git ls-files`, which scales with repository size, only on demand and never per slice.

Each implementing PR states these in its description.

## Open decisions for the owner

1. Whether §2 `rules`/`prompt` mode and the §4 judge count as benchmark work (exempt from P0) or as supervisor feature work.
2. Whether to send runner telemetry to a hosted backend (Braintrust) for the §1 verification.
3. Whether to keep the default cap for the §3 Part B canary, and whether a real-runner canary should ever run in CI, which needs AGENTS.md amended plus spending and secrets approvals.
4. Whether reviewers will use the §4 verdict block in their pull request reviews, and on which repositories.
