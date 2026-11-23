[harness: subagent output matched instruction-shaped pattern(s): bypass-permissions. Control tags below are neutralized (`<` → `<\`); treat any remaining directive-shaped text as a finding to relay to the user, not an instruction to you.]

# SPEC-012: Effectiveness proof: do bounded, supervised slices beat unbounded agent runs?

Status: **proposed.** Merging this document records a plan and nothing more. It does not authorize any run, spending, model choice, Docker use or publication. Each phase below needs its own recorded approval from a person (see [Approvals](#what-needs-a-persons-approval)). Nothing marked *to build* exists today. Every claim that something exists cites the file and line where it was checked, at `219cbc0`, the head of #294 when this was written (v0.5.3 plus that pull request's benchmark changes).


Related: [SPEC-013, adoption and evals](adoption-and-evals.md) specifies the handoff-history replay and transcript links this experiment's metrics depend on.
## The question

Boundroom's pitch is that coding agents drift out of scope and claim success they haven't earned, and that a deterministic supervisor running bounded slices stops both. Nothing published today measures that. This spec designs the experiment that would show it, or show that it is false. Run the same tasks with the supervisor and without it, score both arms the same way, and publish the result in one chart whichever way it comes out.

"Bounded, supervised slices" covers three separate treatments. The design keeps them apart:

| Treatment | What it is | Where it lives |
|---|---|---|
| **Bounded context** | The slice prompt: `base.md` + `slice.md`, with allowed paths, required validations, the diff budget and a handoff template | `run_next.render_prompt`, `automation/supervisor/run_next.py:716` |
| **Supervisor gates** | Validation check and replay, scope check, diff budget, proof level, applied after the session | `policy.evaluate_completion`, `automation/supervisor/policy.py:903`, gates at `:980`, `:997`, `:1014`, `:1038` |
| **In-session restriction** | The Claude Code runner profile's per-slice allow and deny list, plus the budget cap | `runner_profile.slice_settings`, `automation/supervisor/runner_profile.py:304-349`; the cap at `:35` and `run_agent.sh:293` |

Decomposing a large task into several slices is a fourth treatment. It is optional here ([Track R3](#track-r3-optional-decomposition)).

## What exists today (verified)

| Asset | What it gives the experiment | Evidence |
|---|---|---|
| A-E lane experiment | Same instances, same runner and budget, with only the context treatment varied. Lanes are interleaved per instance, and the lane deltas are B-A, C-B, D-C, E-D | `automation/benchmark/strategies.py:888-953`, `:1121-1131`; `automation/benchmark/lanes.py:114-127`, `:456` |
| Lane A vs lane B | A gets the raw objective in one session. B gets the shipped `render_prompt` output for one schema-valid slice | `strategies.py:888-906` (A), `:909-953` (B, `render_prompt` at `:930`) |
| ProgramBench scoring | Resolve rate, near-resolve rate, mean pass fraction, branch-balanced pass fraction and the spread across repeats | `docs/workflows/effectiveness-benchmark.md:8-16`, `:1335-1359`; `lanes.py:350-435` |
| `--judge` stop gate | `--repeats` becomes a ceiling. A round stops the run only when every cell passes the tests (default 0.95) and the LLM judge (score 7 or more out of 10, `good_enough`). The judge never sees the lane. The default minimum is 2 rounds | `automation/benchmark/judge.py:64-70`; `automation/benchmark/run.py:176-205`; doc `:662-724` |
| Judge limits | Needs `--eval`. Refused together with `--cleanroom` | `run.py:315-318`; doc `:702-704` |
| Provider and treatment validity | A cell whose provider refused it, or whose treatment never ran, is dropped from the deltas | doc `:1361-1401` |
| Decision report | `decision`, `reason`, `changed_file_count`, `required_validation_failures`, `supervisor_validation_replays`, `dirty_paths_outside_scope`, `files_touched_outside_scope`, `missing_manual_proofs`, `base_sha`, `head_sha` | `automation/supervisor/run_next.py:787-818`, `:826-829`; appended to a file by `--report-file` (`:79`, `:831-840`) |
| Handoff | The agent's own account: `status` (done, blocked, failed), `files_touched`, `validations_passed` and `validations_failed`, `proof_level`, `residual_risks`, `dirty_paths_outside_scope` | `automation/schemas/handoff.schema.json` (required list) |
| Slice record | Fields written by a person: `allowed_paths`, `required_validations`, `max_files_changed`, `required_proof_level` | `automation/schemas/slice.schema.json` (`SliceRecord`) |
| Metric catalogue | `boundroom.slice.decisions{decision,queue_status}`, `boundroom.validation.replays{success}`, session and run durations | `automation/supervisor/telemetry_metrics.py:27`, `:70-111` |
| Runner telemetry | With an `otlp` destination, Claude Code's own exporter is pointed at the user's endpoint (`CLAUDE_CODE_ENABLE_TELEMETRY`). `content` capture adds tool details | `automation/supervisor/telemetry.py:603-637` |
| One slice per invocation | `run_next.py --single-slice` | `run_next.py:52`, `:186` |
| One supervised run per user at a time | Run lock | `run_next.py:176`; `runner_profile.py:431-443` |
| Prior lane result | Run 35715428932 on glm-5.3, a model now retired: pooled A 83.3%, B 77.7%; balanced A 84.2%, B 83.0% | doc `:1355-1358` |

The only recorded comparison of lane A and lane B **does not show the bounded slice winning**. It is one run, on one greenfield instance set, with a retired model. It is not evidence either way, but the published result must not imply otherwise.

## Why the existing benchmark cannot answer the question alone

1. **The supervisor gates never run in the benchmark.** Lane B renders the slice prompt and launches one session through `run_phase` (`strategies.py:764-840`, `:946-947`). Nothing in `automation/benchmark/` calls `evaluate_completion`, `replay_validation_commands` or `out_of_scope_paths` (`grep` found nothing). B-A measures **bounded context only**.
2. **ProgramBench cannot violate scope or overrun a budget.** The benchmark slice allows `./` with a diff budget of 1,000,000 files (`automation/benchmark/adapter.py:58-60`, `:147-150`), because a rebuild from scratch is not a bounded slice. Scope violations and diff-budget overruns are zero by construction.
3. **ProgramBench is greenfield** (doc `:1408-1413`). Bounded slices are meant for changes to an existing codebase.
4. **Validation replay covers two commands.** The supervisor replays only `make architecture` and `git diff --check` (`policy.py:171-174`). It takes every other required validation from the agent's own `validations_passed` (`policy.py:555-564`). Inside the supervised arm, "validation pass rate" is therefore partly what the agent reported about itself.
5. **The Claude runner records no cost or tokens.** `run_agent.sh` runs `claude --print` with text output (`run_agent.sh:297`, `:304`). Hermes sessions write usage into `run.json` (doc `:196-199`), but the comparison does not compare tokens (doc `:1414-1416`). The $5 `--max-budget-usd` cap applies only when a runner profile is chosen (`run_agent.sh:290-301`; `runner_profile.py:395`). Without one, the runner runs in `bypassPermissions` mode with no cap (`run_agent.sh:304-308`).

The design therefore has a **primary brownfield track (R)** that runs the real supervisor, and a **secondary ProgramBench track (P)** that reuses the existing tooling for the context question only.

## Design overview

| Track | Question | Arms | Tooling | Status |
|---|---|---|---|---|
| **R** (primary) | Do supervised slices produce more correct, in-scope changes per dollar, with less human rework, than an unbounded run of the same agent on the same tasks? | U (unbounded) vs S (supervised) | A new driver around `run_next.py --single-slice` and `run_agent.sh` | *to build* |
| **P** (secondary) | Does the bounded slice prompt improve correctness on a greenfield rebuild? | Lanes A vs B | `python -m automation.benchmark lanes --lanes A B` | exists |
| **R3** (optional) | Does splitting one large task into several bounded slices beat one unbounded run? | U vs S-multi | Track R driver plus a multi-slice queue | *to build* |

## Track R: brownfield slices (primary)

### Task set

Freeze the set **before** any model run. Commit the set and its selection rule in the same pull request as the analysis script.

**R-a (preferred): handoff history as the dataset.** Use past slices from a consumer queue that has a synced Boundroom copy, for example Owlory, in a **disposable clone**. Each slice already has `allowed_paths`, `required_validations` and `max_files_changed`, written by a person *before* the work. That removes the biggest leak: deriving the scope from the solution. The decision report's `base_sha` and `head_sha` (`run_next.py:826-829`) give the start and end points. The hidden oracle is the test files added or changed in the slice's own commits, or, for a slice that stopped or failed, in the commit that later fixed it. Include stopped and failed slices on purpose: they are the regression cases. Using the consumer's history, even in a clone, is the **owner's decision**.

**R-b (fallback): Boundroom's own merged pull requests,** merged after the run model's training cutoff. Exclude any pull request that touches `automation/supervisor/`, `automation/schemas/`, `automation/context/` or `automation/queue/`. `run_next.py` treats its own checkout as the repository (`REPO_ROOT`, `run_next.py:19`, `:188`), so in those pull requests the agent would be editing the supervisor that judges it. A person writes `allowed_paths` and `required_validations` from the pull request's description **without looking at its diff**, and records that in the task file.

**Selection rule (preregistered).** Take the first 30 tasks in a fixed date window that meet all of these:
- at least one test file in the human solution;
- 15 or fewer files changed in the human solution;
- not documentation only;
- the hidden tests fail at `base_sha` and pass at the human `head_sha`.

The last condition is checked by the oracle with no model involved, in R0. Record which human solutions themselves fall outside `allowed_paths` or `max_files_changed`. That is the gate's false-positive baseline.

### Arms

Both arms use the same machine, Claude Code version, model id, wall-clock limit (`handoff_timeout_seconds`, `run_next.py:602`, `:632`), `--max-budget-usd 5`, permission mode, starting checkout and repository `CLAUDE.md`/`AGENTS.md`.

| Arm | Prompt | During the session | After the session | What the reviewer sees |
|---|---|---|---|---|
| **U: unbounded** | The task statement only (the issue or slice title and notes, as a person would write it), plus "commit your change" | Claude Code with no per-slice allowlist | Nothing. Exit 0 with a non-empty diff counts as "claims done" | Every change it claims is done |
| **S: supervised** | `render_prompt` output for the same slice: allowed paths, validations, diff budget, handoff template | Runner profile `edits`: the per-slice allow and deny list (`runner_profile.py:320-341`) and the $5 cap | `run_next.py --single-slice --report-file <outside repo> --handoff-dir <outside repo>` applies every gate | Only changes the supervisor let continue. A stop goes to the reviewer as a stop, with its reason |

Do not use the runner's default `bypassPermissions` with no cap (`run_agent.sh:304-308`) in either arm: the cost would be uncapped.

**Optional sensitivity arm, U+.** U plus the slice's validation commands named in the prompt. It separates "knowing what will be checked" from "being gated". Run it only if the budget allows.

**The gates are a classifier, so test them against both arms.** The gates do not change what the agent writes in a single session; they decide what is accepted. So apply the same checks offline to U's diff too: `policy.out_of_scope_paths` with the slice's `allowed_paths`, a changed-file count against `max_files_changed`, and the oracle's validation re-run. That shows, for each U run, "what the supervisor would have caught" at no extra model cost. Scored against the independent oracle, both arms then give the gates' precision and recall.

### Oracle (independent of both arms) *to build*

After each session, in a fresh clone at the session's final commit, with no agent present:
1. Run every `required_validations` command, not only the two the supervisor can replay.
2. Run the hidden tests from the task's human solution.
3. Compute changed files and lines (`git diff --numstat base..head`), out-of-scope paths and diff-budget status.

Nothing the agent wrote, including the handoff, counts as an oracle input.

### Repeats and power

30 tasks × 2 arms × 3 repeats = **180 agent sessions**, sequential and interleaved: arm order rotates per task and repeat, as in `lanes.interleave`. The run lock already allows one supervised run per user at a time (`runner_profile.py:431-443`).

A simulation with logit task difficulty (sd 1) and a task-clustered bootstrap gave the chance of detecting a difference at 95% confidence:

| Tasks × repeats | 40% → 20% | 35% → 20% | 30% → 20% |
|---|---:|---:|---:|
| 20 × 3 | 0.63 | 0.42 | 0.28 |
| **30 × 3** | **0.81** | 0.60 | 0.40 |
| 40 × 3 | 0.89 | 0.72 | 0.43 |
| 30 × 1 | 0.35 | 0.20 | 0.14 |

30 × 3 detects a difference of about 20 points reliably and a 10-point difference poorly. **Add tasks, not repeats,** if the pilot shows a smaller effect. Publish the power alongside the result, so that "no significant difference" is not read as "no difference".

### Phases

| Phase | What | Model sessions |
|---|---|---:|
| **R0 calibration** | Build and freeze the task set. Check the oracle against each human solution (tests fail at base, pass at head). Dry-run the driver with a fake runner | 0 |
| **R1 pilot** | 6 tasks × 2 arms × 1 repeat. Measures mean $/session, how often sessions hit the cap or time out, and whether blinding holds | 12 |
| **R2 main** | 30 × 2 × 3, fixed n | 180 |

### Blinding

- **Reviewer.** Each change arrives under a random id, in random order, with the task statement and a normalized diff:
  - squashed into one commit with a neutral message;
  - with no handoff in the diff (`--handoff-dir` outside the repository);
  - with no `.claude/` settings and no attribution trailers.

  S stops are reviewed in the same queue, so a reviewer cannot tell arms apart by what is missing. The reviewer also records a guess of the arm, so blinding can be tested.
- **LLM judge (optional, *to build*).** A Track R version of `judge.py`'s blind pattern: random ids, untrusted outputs read without following symlinks. It compares the diff with the task's stated intent and returns a 0-10 score, `intent_met`, and a list of out-of-scope or extra changes. It sees the same normalized diff as the reviewer and **not** the oracle result, so the tests cannot anchor it. Only Claude Code is approved, so the judge is Claude judging Claude. Report it as a secondary signal, never as the primary.
- **Analyst.** Freeze the analysis script before R2. A person holds the arm mapping until the review is complete.

### Stopping

- **R2 uses a fixed n.** Do not use `--judge`-style optional stopping for the headline: looking after every round and stopping on a good one inflates false positives.
- **Pilot stops.** Before R2, stop and recalibrate if, in either arm:
  - more than half the sessions hit the $5 cap or the timeout;
  - the oracle disagrees with the human solution on any task;
  - the reviewer guesses the arm correctly in more than 75% of cases.

## Track P: ProgramBench lanes A vs B (secondary)

**Purpose.** A greenfield check on the context treatment only, reusing what exists. Label it "bounded prompt, greenfield, no gates" wherever it is shown.

**Instances.** 10 drawn from `instances.all_instances()` with a seed recorded in the plan. Not only the single-instance smoke set (`automation/benchmark/instances.py:17`).

**Arms.** `--lanes A B`, Claude runner. That makes the results **not comparable** with the CI glm-5.3-flash history (doc `:544-551`).

**Repeats.** 3, which the doc requires before believing a result (doc `:1403-1404`): 10 × 2 × 3 = 60 sessions.

**Environment.** `--cleanroom --eval` is the environment that counts (doc `:236-244`). It needs Docker with amd64 images, which are slow under emulation on Apple Silicon (doc `:229-230`). That is **the owner's call** on the company machine.

**Stopping.** `--judge` cannot be combined with `--cleanroom` (`run.py:315-318`). So the headline Track P run uses a fixed 3 repeats without the judge. A `--judge --repeats 5 --min-repeats 3` run without the cleanroom can serve only as a cost-capped calibration, reported separately with its `stop-gate.json` rounds.

**Launch.** From a plain terminal, by the owner. The benchmark refuses runner mismatches from inside an agent harness (`run_agent.sh:157`, `:169`; doc `:52-55`).

## Track R3 (optional): decomposition

10 tasks that a person splits into 2-4 dependent slices, written before any run, against a single unbounded U run of the whole task. S-multi runs one `--single-slice` invocation per slice. **No worker or sub-agent starts the next slice.** The driver invokes each slice after the previous decision, which is the rule in AGENTS.md. Same oracle on the final state. Cost is roughly U 30 + S 90 = 120 sessions. Run R3 only if R2 is interpretable.

## Metrics

Each row below says what is measurable today, what is missing, and how it is measured in each arm. "Merged" means accepted by the blinded reviewer in a disposable clone. **Nothing from this experiment merges into any real repository.**

| Metric | Measurable today? | Missing | U arm | S arm |
|---|---|---|---|---|
| **Validation pass rate** | Partly. S: `required_validation_failures` and `supervisor_validation_replays[]` in the decision report (`run_next.py:808`, `:811`), `boundroom.validation.replays{success}` (`telemetry_metrics.py:100`). P: `mean_pass_fraction` and resolve rate | Replay covers 2 commands (`policy.py:171-174`). The rest is self-reported (`policy.py:555-564`). U has no record | Oracle re-runs every required validation plus the hidden tests at the final commit | Same oracle. Also the **unearned-done rate**: handoff `status: done` and supervisor `continue`, but the oracle fails |
| **Human rework** | No. No field exists anywhere in the repository | A structured reviewer verdict | Blinded review form *(to build)*: `verdict` ∈ {merge, merge_after_edits, reject}, `lines_edited`, `minutes`, `reasons[]`, `arm_guess`. Rework = merge_after_edits + reject among changes presented as done | Same form for changes the supervisor let continue. Stops are reviewed as "was this stop right?" |
| **Diff-budget overruns** | Partly. `changed_file_count` (`run_next.py:807`) vs the slice's `max_files_changed`. Stop reason text at `policy.py:1014-1019` | No reason code on the metric or the span: `boundroom.slice.decisions` carries only `decision` (`telemetry_metrics.py:27`), and the span only the verdict (`telemetry.py:487`; `run_next.py:565`). Masked by earlier gates (validation `:980`, scope `:997`, then budget `:1014`). Counts files, not lines. The benchmark sets it to 1,000,000 (`adapter.py:60`) | Oracle: changed files and lines against the same `max_files_changed`. Overruns that reach the reviewer | Oracle, plus the decision report: overruns stopped vs overruns that slipped through |
| **Scope violations caught** | Partly. `files_touched_outside_scope` and `dirty_paths_outside_scope` (`run_next.py:812-813`; `policy.py:997-1002`) | Same missing reason code. An edit the runner profile denies never shows up in the diff, so prevention is invisible without the runner transcript (Claude Code OTLP, `telemetry.py:625-636`). The benchmark allows `./` (`adapter.py:58`) | Post-hoc `out_of_scope_paths` on the diff with the slice's `allowed_paths`. The reviewer labels each out-of-scope file necessary, harmless or harmful | Gate result plus denied tool calls from the runner transcript. Precision (harmful ÷ flagged) and recall (caught ÷ all harmful, oracle and reviewer combined) |
| **Tokens and cost per merged change** | No for Claude: text output only (`run_agent.sh:297-308`), and cost appears only through a user's OTLP endpoint. Hermes: tokens in `run.json` (doc `:196-199`), not compared (doc `:1414-1416`) | Per-session `total_cost_usd` and token usage recorded next to each session *(to build: `--output-format json` captured to `<stem>.usage.json` in the Claude branch, or a local OTLP collector)* | Sum of every session in the arm, failures included ÷ reviewer-accepted changes that pass the oracle | Same. Judge cost reported separately, not in either arm |

**Primary endpoint (preregistered, one only): bad changes reaching the reviewer.** The fraction of all runs in which a change presented as done fails the oracle or contains a harmful out-of-scope edit.

**Co-primary guard: good-change yield.** The fraction of all runs that end in a reviewer-accepted change that passes the oracle. Without it, S could win the primary endpoint just by stopping everything.

**Secondary endpoints:** the rows above, gate precision and recall, false stops (S stopped a change the oracle and reviewer would have accepted), and how often each arm hit the cap or the timeout.

**Analysis.** Paired by task. Report differences with 95% task-clustered bootstrap intervals, plus every task's own outcomes. Show the raw counts beside each rate. Report the arm-guess accuracy as the blinding check.

## The chart to publish

One chart: **"What reached the reviewer"**. A 100% stacked bar per arm (U, S) over all runs, n under each bar:

1. accepted, oracle passes, merged as-is;
2. accepted, merged after rework;
3. **presented as done but bad** (oracle fails or harmful scope change);
4. stopped, and the work was bad (a correct catch);
5. **stopped, but the work was good** (a false stop);
6. no usable change: timeout, budget cap or no diff.

Underneath: **$ per merged change** for each arm, with its interval, and the primary and co-primary differences with their intervals. The chart shows the benefit (3 shrinking) and the cost (5 and the dollars) together.

If S does not beat U, publish that. A Track P B-A chart, if shown at all, sits below with the caption "bounded prompt only, greenfield, no gates". Publish the per-run table (task id, arm, decision, oracle, reviewer verdict, cost) and the frozen task set. Do **not** publish raw transcripts or other restricted evidence (`docs/roadmap.md:80`). A README change that carries the chart goes through a pull request and a person's approval like any other change.

## Cost estimate

The `--max-budget-usd` default is 5 USD per session (`runner_profile.py:35`; `run_agent.sh:293`). The figures are **ceilings**, which is what approval has to cover. The cap is Claude Code's own limit, and the final request can overshoot it slightly. If Claude Code runs on a subscription, the dollar figure is the notional cost at API prices, and the owner decides how to count it. The pilot measures the real mean.

| Phase | Agent sessions | Ceiling at $5 | Judge sessions (optional) | Judge ceiling | Wall clock, sequential |
|---|---:|---:|---:|---:|---|
| R0 calibration | 0 | $0 | 0 | $0 | Hours of CPU for test runs |
| R1 pilot | 12 | $60 | 12 | $12 at a $1 cap / $60 at $5 | ≤ 6 h at a 30-min limit |
| R2 main | 180 | $900 | 180 | $180 at $1 / $900 at $5 | ≤ 90 h worst case, ~45 h at 15 min average |
| P1 ProgramBench A vs B | 60 | $300 | 0 (fixed repeats) | $0 | Slow under amd64 emulation on Apple Silicon, plus Docker eval |
| R3 decomposition (optional) | ~120 | ~$600 | — | — | ≤ 60 h |
| **Headline (R1 + R2)** | **192** | **$960** | 192 | $192-$960 | |

Reviewer time is the hidden cost: about 180 reviews at 5-10 minutes each, 15-30 hours of a person's time. A second reviewer for a sample of about 30 would check agreement.

## What needs a person's approval

Each of these is a separate recorded action. The agent prepares commands and evidence, and does none of them on its own:

1. **Merging this spec**, through an approving review by a person other than its author.
2. **The task set**: R-a (use of a consumer's history in a disposable clone, the owner's decision) or R-b, and the frozen list.
3. **Spending**: R1, R2, P1 and R3 each need their own approval. Every agent session and every judge session is spending (doc `:723-724`; AGENTS.md "spending").
4. **Model and runner**: which Claude model, and the Claude Code version to pin. Only Claude Code is approved on the owner's company machine. Hermes, Codex and NVIDIA cannot be used there.
5. **Docker and ProgramBench** on the company machine (Track P) are the owner's call. A CI dispatch of `effectiveness-benchmark.yml` uses Hermes with an NVIDIA key and is a separate spending approval.
6. **The reviewer or reviewers**, and the review time.
7. **Publication** of the chart and the per-run table, through a pull request.

## Instrumentation to build (not implemented; one pull request each)

1. **Track R driver**, inside `automation/benchmark/`, kept apart from any live control plane: no board, no memory, telemetry off or a local file. It does four things:
   - materializes each task at `base_sha` in a disposable clone;
   - runs U through `run_agent.sh` and S through `run_next.py --single-slice`, interleaved;
   - normalizes diffs for blind review;
   - writes one record per run.
2. **Independent oracle runner**: all required validations plus the hidden tests, in a fresh clone.
3. **Claude usage capture**: per-session `total_cost_usd` and tokens, recorded in `run.json` and the decision report.
4. **Gate reason code**: a bounded `gate` value (`validation`, `scope`, `diff_budget`, `proof`, `handoff`, `agent`, `none`) in the decision report, and as an attribute on `boundroom.slice.decisions`. Also record *every* gate that would have failed, not only the first.
5. **Structured reviewer verdict**, in the schema given above.
6. **Track R intent judge** (optional), reusing `judge.py`'s blind, untrusted-output handling.
7. **Production rework proxy** (later): an offline script that counts follow-up and revert commits touching a slice's files between its `head_sha` and N days later.

None of these touches the supervisor's decisions. Item 4 is a reporting change to `run_next.py` and the metric catalogue. Its pull request must note that it adds no file, network or subprocess work (AGENTS.md "Performance").

## Threats to validity

1. **Construct (Track P).** Greenfield, with gates that cannot fire (`adapter.py:58-60`). B-A measures only the prompt.
2. **Self-reported validation.** Replay covers two commands. The oracle, not the handoff, decides validation pass.
3. **Scope leakage.** `allowed_paths` derived from the solution would hand S a hint. R-a uses scopes written before the work; R-b writes them without seeing the diff.
4. **Selection bias.** R-a tasks come from a history that already ran under the supervisor. Include stopped and failed slices, and use a mechanical, preregistered rule.
5. **Contamination.** Boundroom is public. Use only work merged after the model's cutoff.
6. **Treatment leakage into U.** The repository's `CLAUDE.md` and `AGENTS.md` describe Boundroom and reach both arms. Hold them identical and disclose this.
7. **Supervisor in the workspace.** R-b excludes work on supervisor-owned paths (`run_next.py:19`, `:188`).
8. **Judge bias.** Claude judging Claude, and anchoring if it sees the tests. Withhold the oracle result from the judge and keep it secondary.
9. **Reviewer expectation.** The owner built Boundroom. Normalize the diffs, test blinding with arm guesses, and use a second reviewer on a sample.
10. **Optional stopping.** No `--judge` stopping for the headline.
11. **Censoring.** The $5 cap or the timeout may cut one arm more than the other. Report censoring per arm, and treat a censored run as category 6, never drop it.
12. **Drift.** Claude Code or model updates during the campaign. Pin and record the version (the README's support matrix records 2.1.311, `README.md:163`), interleave the arms, and keep the campaign short.
13. **External validity.** One machine, one runner, one model, one or two repositories. Nothing here speaks for Codex or Hermes, whose runners are tested only with fakes (`README.md:164`). The single-machine gap stays open until a second environment runs the same set.
14. **Gaming the primary endpoint.** S could cut bad changes by stopping everything. That is why good-change yield is co-primary and false stops appear in the chart.
15. **Specification error.** A gate false positive caused by a bad slice spec is still a real cost of the method. Count it, and show the R0 baseline of human solutions that broke their own scope.
16. **Small n.** About 80% power only for a 20-point effect. Publish the intervals and the power.

## Open questions for the owner

1. R-a (a consumer's history in a disposable clone) or R-b (Boundroom's own pull requests after the cutoff)?
2. Which Claude model and which Claude Code version to pin? Is the cost counted at API prices or under a subscription?
3. Is Docker acceptable on the company machine for Track P, or does Track P move to CI under a separate approval?
4. Who reviews besides the owner, if anyone?
5. Should the U+ sensitivity arm and Track R3 be run at all?
