# Mutation report, November 2026

This report re-measures Boundroom's mutation baseline after the batch "Make Boundroom's tests catch bugs". The batch is the harness (#298) and the test rewrites in #300 to #313. It compares the baseline before the batch with a new measurement of every module the baseline lists.

A mutation score says how many planted bugs of the [harness's kinds](mutation-testing.md#mutation-operators) the tests detect. It is evidence about the tests, not proof that the code is correct. A survivor can be a missing test, an equivalent mutant, or code not worth a test.

## Headline

All 44 modules the baseline lists were measured at one commit, `b5b6e1b` (`main` after #316), from a clean tree. 42 have mutants, and 36 of those meet their target. The baseline before this change held 5 modules, measured at `dee1aed`, before the rewrites.

The targets come from the plan for the batch: 90% for pure decision logic, 65% for I/O-heavy shells and 80% for everything else. They aren't recorded anywhere else in the repository, and nothing enforces them. The harness enforces only the per-module ratchet in `quality/mutation-baseline.json`.

This report is a snapshot at `b5b6e1b`. The baseline file is the live record. A later `--update-baseline` changes the file, not this page.

| Group | Target | At or above target | Below target |
| --- | ---: | ---: | --- |
| Pure decision logic | 90% | 5 of 6 | `judge.py` |
| I/O-heavy shells | 65% | 4 of 4 | none |
| Other supervisor, context and schema modules | 80% | 14 of 17 | `build_context.py` †, `telemetry_settings.py` †, `sync_pin.py` |
| Other benchmark modules | 80% | 9 of 11 | `adapter.py` †, `__main__.py` |
| Release and sample tools | 80% | 4 of 4 | none |

† Measured under heavy load. Its score is a lower bound; see [Load](#load).

The five modules the old baseline measured all rose past the 90% target:

| Module | Before (`dee1aed`) | After (`b5b6e1b`) |
| --- | ---: | ---: |
| `automation/supervisor/policy.py` | 71.9% | 96.6% |
| `automation/supervisor/evaluate_handoff.py` | 35.1% (77 not covered) | 91.0% (1 not covered) |
| `automation/supervisor/run_next.py` | 71.6% | 97.0% |
| `automation/supervisor/runner_profile.py` | 79.0% | 98.7% |
| `automation/benchmark/scoring.py` | 74.2% | 95.9% |

The full suite took 8 min 10 s in one timed run, on a loaded machine. Branch coverage is 88.9% of branches, and 92.2% combined with statements. See [Suite runtime and coverage](#suite-runtime-and-coverage).

## How it was measured

- **Command:** `uv run --frozen python -m Tools.mutate --update-baseline --nice --jobs 6 --timeout-factor 6 --min-timeout 60 -- <modules>`. It ran three times from the same clean tree, each time with a comma-separated list of modules:
  - the five modules of the old baseline;
  - the 23 supervisor, context, schema and `Tools/` modules;
  - the 16 `automation/benchmark/` modules.

  To measure all 44 in one run, pass `--baseline-modules` in place of the list.
- **Time:** about 4 h 9 min for the five decision modules, 4 h 26 min for the 23 supervisor, context, schema and tool modules, and 1 h 55 min for the 16 benchmark modules.
- **Provenance:** every entry in `quality/mutation-baseline.json` records `commit` `b5b6e1b09924b207aed6676501f23bd10868ca9e`, `dirty: false`, the `automation` and `Tools` tree ids and the module's blob id.
- **One exception:** `telemetry_metrics.py` comes from a second, complete run at the same commit, with `--jobs 4 --timeout-factor 6 --min-timeout 300`; see [Load](#load).
- **The rewrite PRs used other flags.** Their own before and after figures are shown for context. They are not exactly comparable with this run.
- **The agent guard worked.** The only commands it refused were `docker create`, `docker image inspect` and `docker ps` calls made by benchmark tests (`adapter`, `cleanroom`, `probe`, `run`, `witness`). No test reached `claude`, `codex`, `hermes` or `uvx`.

### Load

The machine has 18 CPUs and was shared. Other work, including an iOS Simulator test run with hundreds of processes, pushed the one-minute load average from about 7 to peaks of 239 in the first run, 573 in the second and 453 in the third.

Load cannot create a kill: the harness reruns the tests without the mutant before it counts one. It can lower a score in two ways:

- **`unreliable`:** a mutant is killed, and the recheck without it then fails or times out.
- **`unreliable_selection`:** the tests that would judge a group of mutants time out, or fail, without any mutant.

Both count as survived. So a score measured under load errs low, and a later run on a quiet machine can only raise the baseline.

Ten modules were hit. The per-module tables mark them † and give, beside the score, the share of mutants that got a reliable verdict:

| Module | Score | Unreliable | Unreliable selection | Of mutants with a verdict |
| --- | ---: | ---: | ---: | ---: |
| `automation/context/build_context.py` | 34.8% | 0 | 76 | 97.6% (41 of 42) |
| `automation/supervisor/telemetry_settings.py` | 68.8% | 0 | 46 | 98.1% (106 of 108) |
| `automation/benchmark/adapter.py` | 73.1% | 3 | 2 | 75.5% (114 of 151) |
| `automation/supervisor/telemetry_metrics.py` | 80.0% | 3 | 0 | 94.1% (16 of 17) |
| `automation/benchmark/instances.py` | 84.2% | 2 | 4 | 100% (32 of 32) |
| `automation/benchmark/run.py` | 89.5% | 0 | 4 | 93.4% (85 of 91) |
| `automation/benchmark/strategies.py` | 91.0% | 2 | 11 | 96.6% (201 of 208) |
| `automation/supervisor/telemetry.py` | 91.3% | 8 | 0 | 94.8% (200 of 211) |
| `automation/benchmark/lanes.py` | 92.4% | 9 | 0 | 96.3% (206 of 214) |
| `automation/benchmark/diagnosis.py` | 92.9% | 3 | 14 | 99.2% (249 of 251) |

None of these ten had a `timeout`, so their killed-only figure equals the harness's score. Only three modules had timeouts, which the harness counts as kills: `local_board.py` 2, `memory.py` 1 and `witness.py` 1. Their killed-only figures (97.4%, 94.8% and 95.7%) still meet their targets.

The failed selections were checked once:

- **Timeouts.** Most failed selections timed out: the exit code was empty. That is the time limit under load.
- **Exit 1 without a mutant: 14 selections.**
  - **The 10 with 70 tests or fewer** (5 from `diagnosis`, 1 from `instances`, 3 from `run`, 1 from `strategies`) were rerun in the same order without a mutant, at a load of about 20. All 10 passed.
  - **Three large ones were not rerun:** 198 tests from `diagnosis`, 213 from `instances` and 275 from `strategies`.
  - **The 14th is the `telemetry_metrics` selection,** in the next bullet.
- **The large `telemetry_metrics` selection.** This 249-test selection failed once at load 64. The failure was in `test_benchmark_probe.py::TransportContractTests::test_the_stream_transport_is_launched_without_the_approval_difference`: `fake-hermes.env.json` was missing. It passed on the next run at load 42, and that test passes alone.
- **A likely cause.** That test gives the fake Hermes a 2-second phase budget (`PHASE_BUDGET_SECONDS`), so a slow start under load can end the session before the fake writes its file. It is a load-sensitive test, a follow-up for the owner of `test_benchmark_probe.py`.
- **The second `telemetry_metrics` run.** Its 3 `unreliable` mutants at line 130 were killed in about 5 seconds each. Each recheck then failed in that same selection.

A second run of all ten modules at `--jobs 4 --min-timeout 300` was started. Only `telemetry_metrics.py` finished: 80.0%, against 5.0% in the first run, where 18 of its 20 mutants were `unreliable_selection`. The baseline records that second run. The rest was stopped, because at this load it would have taken many more hours. The other nine keep their first-run numbers.

**Until a re-measure, the ratchet is weak for these ten.** The check fails only when a score falls below its recorded floor, and these floors are low. For example, the tests for `build_context.py` could get much weaker, from about 97% to 40%, without failing `--check-baseline`.

**Follow-up:** re-measure the ten † modules on a quiet machine with `--min-timeout 300 --timeout-factor 6`, and raise their baselines with `--update-baseline`.

**Two limits of the advisory workflow** (`.github/workflows/mutation.yml`), for whoever runs it next:

- **Its default run won't finish in time.** With no modules named, it measures every module in the baseline, which is now 44, at `--jobs 2` under a 300-minute timeout. This run took about 10.5 hours at `--jobs 6`, so name modules instead.
- **Its timeouts are shorter.** It uses the default factor 3 and minimum 30 s, shorter than the limits these floors were measured with. On a slow runner, a timeout-sensitive module such as `telemetry_metrics.py` could fall below its floor without any regression.

## Per-module results

"Before (baseline)" is the old `quality/mutation-baseline.json`. "Rewrite PR" is the before and after figure that PR reported on its own branch, with its own flags. "Unreliable" adds `unreliable` and `unreliable_selection`. "Met" compares the score with the group's target.

### Pure decision logic (target 90%)

| Module | Before (baseline) | Rewrite PR: its before → after | After (this run) | Killed | Timeout | Survived | No coverage | Unreliable | Met |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `automation/benchmark/judge.py` | not measured | none | 81.9% | 154 | 0 | 32 | 2 | 0 | **no** |
| `automation/benchmark/scoring.py` | 74.2% | #312: 74.2% → 95.9% | 95.9% | 93 | 0 | 4 | 0 | 0 | yes |
| `automation/supervisor/evaluate_handoff.py` | 35.1% | #301: 81.0% → 91.0% | 91.0% | 193 | 0 | 18 | 1 | 0 | yes |
| `automation/supervisor/policy.py` | 71.9% | #302: 72.6% → 96.1% | 96.6% | 427 | 0 | 10 | 5 | 0 | yes |
| `automation/supervisor/run_next.py` | 71.6% | #311: 71.6% → 96.7% | 97.0% | 319 | 0 | 10 | 0 | 0 | yes |
| `automation/supervisor/runner_profile.py` | 79.0% | #305: 79.0% → 98.7% | 98.7% | 152 | 0 | 2 | 0 | 0 | yes |

### I/O-heavy shells (target 65%)

| Module | Before (baseline) | Rewrite PR: its before → after | After (this run) | Killed | Timeout | Survived | No coverage | Unreliable | Met |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `automation/benchmark/cleanroom.py` | not measured | #310: 61.5% → 92.8% | 92.8% | 77 | 0 | 0 | 6 | 0 | yes |
| `automation/benchmark/probe.py` | not measured | #306: 68.4% → 97.3% | 97.3% | 216 | 0 | 6 | 0 | 0 | yes |
| `automation/benchmark/witness.py` | not measured | #310: 78.2% → 96.7% | 96.7% (killed only: 95.7%) | 88 | 1 | 3 | 0 | 0 | yes |
| `automation/supervisor/telemetry.py` † | not measured | #313: 82.0% → 95.0% | 91.3% (94.8% of 211 with a verdict) | 200 | 0 | 11 | 0 | 8 | yes |

### Other supervisor, context and schema modules (target 80%)

| Module | Before (baseline) | Rewrite PR: its before → after | After (this run) | Killed | Timeout | Survived | No coverage | Unreliable | Met |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `automation/context/build_context.py` † | not measured | #307: 49.6% → 96.6% | 34.8% (97.6% of 42 with a verdict) | 41 | 0 | 1 | 0 | 76 | **no** |
| `automation/context/memory.py` | not measured | #307: 81.6% → 95.3% | 95.3% (killed only: 94.8%) | 202 | 1 | 9 | 1 | 0 | yes |
| `automation/context/swarm_wiki.py` | not measured | #307: 77.8% → 95.6% | 95.6% | 43 | 0 | 2 | 0 | 0 | yes |
| `automation/schemas/__init__.py` | not measured | none | no mutants | 0 | 0 | 0 | 0 | 0 | n/a |
| `automation/schemas/generate.py` | not measured | #307: 34.8% → 100% | 100.0% | 23 | 0 | 0 | 0 | 0 | yes |
| `automation/schemas/models.py` | not measured | #307: 79.3% → 82.8% | 82.8% | 24 | 0 | 3 | 2 | 0 | yes |
| `automation/supervisor/board_backend.py` | not measured | #304: 0% → 100% | 100.0% | 1 | 0 | 0 | 0 | 0 | yes |
| `automation/supervisor/egress.py` | not measured | #303: 100% → 100% | 100.0% | 11 | 0 | 0 | 0 | 0 | yes |
| `automation/supervisor/handoff_summary.py` | not measured | #305: 83.1% → 96.4% | 96.9% | 187 | 0 | 6 | 0 | 0 | yes |
| `automation/supervisor/kanban_projection.py` | not measured | #304: 84.4% → 99.2% | 99.2% | 127 | 0 | 1 | 0 | 0 | yes |
| `automation/supervisor/kanban_writer.py` | not measured | #304: 81.9% → 98.6% | 98.6% | 71 | 0 | 1 | 0 | 0 | yes |
| `automation/supervisor/local_board.py` | not measured | #304: 46.4% → 98.0% | 98.5% (killed only: 97.4%) | 191 | 2 | 3 | 0 | 0 | yes |
| `automation/supervisor/operator_status.py` | not measured | #307: 84.5% → 97.1% | 97.1% | 101 | 0 | 2 | 1 | 0 | yes |
| `automation/supervisor/settings.py` | not measured | #303: 59.7% → 99.2% | 99.2% | 123 | 0 | 1 | 0 | 0 | yes |
| `automation/supervisor/settings_view.py` | not measured | #303: 78.5% → 96.2% | 96.2% | 125 | 0 | 2 | 3 | 0 | yes |
| `automation/supervisor/sync_pin.py` | not measured | none | 53.8% | 14 | 0 | 10 | 2 | 0 | **no** |
| `automation/supervisor/telemetry_metrics.py` † | not measured | #313: 95.0% → 95.0% | 80.0% (94.1% of 17 with a verdict) | 16 | 0 | 1 | 0 | 3 | yes |
| `automation/supervisor/telemetry_settings.py` † | not measured | #313: 87.7% → 98.7% | 68.8% (98.1% of 108 with a verdict) | 106 | 0 | 2 | 0 | 46 | **no** |

### Other benchmark modules (target 80%)

| Module | Before (baseline) | Rewrite PR: its before → after | After (this run) | Killed | Timeout | Survived | No coverage | Unreliable | Met |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `automation/benchmark/__init__.py` | not measured | none | no mutants | 0 | 0 | 0 | 0 | 0 | n/a |
| `automation/benchmark/__main__.py` | not measured | none | 0.0% | 0 | 0 | 0 | 1 | 0 | **no** |
| `automation/benchmark/adapter.py` † | not measured | none | 73.1% (75.5% of 151 with a verdict) | 114 | 0 | 25 | 12 | 5 | **no** |
| `automation/benchmark/diagnosis.py` † | not measured | #310: 83.7% → 98.5% | 92.9% (99.2% of 251 with a verdict) | 249 | 0 | 2 | 0 | 17 | yes |
| `automation/benchmark/evalrunner.py` | not measured | #312: 38.9% → 88.9% | 88.9% | 16 | 0 | 2 | 0 | 0 | yes |
| `automation/benchmark/instances.py` † | not measured | #306: 31.6% → 100% | 84.2% (100.0% of 32 with a verdict) | 32 | 0 | 0 | 0 | 6 | yes |
| `automation/benchmark/jspace.py` | not measured | #310: 93.8% → 100% | 100.0% | 34 | 0 | 0 | 0 | 0 | yes |
| `automation/benchmark/lanes.py` † | not measured | #308: 63.6% → 96.4% | 92.4% (96.3% of 214 with a verdict) | 206 | 0 | 8 | 0 | 9 | yes |
| `automation/benchmark/last_build.py` | not measured | #306: 83.6% → 96.4% | 96.4% | 53 | 0 | 2 | 0 | 0 | yes |
| `automation/benchmark/run.py` † | not measured | #312: 50.0% → 93.7% | 89.5% (93.4% of 91 with a verdict) | 85 | 0 | 2 | 4 | 4 | yes |
| `automation/benchmark/strategies.py` † | not measured | #308: 78.6% → 95.9% | 91.0% (96.6% of 208 with a verdict) | 201 | 0 | 7 | 0 | 13 | yes |
| `automation/benchmark/tracing.py` | not measured | #310: 90.0% → 96.7% | 96.7% | 29 | 0 | 1 | 0 | 0 | yes |

### Release and sample tools (target 80%)

| Module | Before (baseline) | Rewrite PR: its before → after | After (this run) | Killed | Timeout | Survived | No coverage | Unreliable | Met |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `Tools/changelog_section.py` | not measured | #309: 94.6% → 100% | 100.0% | 37 | 0 | 0 | 0 | 0 | yes |
| `Tools/check_plugin_version.py` | not measured | #309: 89.2% → 100% | 100.0% | 37 | 0 | 0 | 0 | 0 | yes |
| `Tools/release_sbom.py` | not measured | #309: 81.4% → 94.9% | 94.9% | 56 | 0 | 3 | 0 | 0 | yes |
| `Tools/telemetry_sample.py` | not measured | #313: 78.9% → 91.9% | 91.9% | 102 | 0 | 4 | 5 | 0 | yes |

`automation/benchmark/__init__.py` and `automation/schemas/__init__.py` have no mutants. They are recorded with `score: null`.

## What each rewrite PR changed

- **#298**: the harness, `make mutation`, the baseline and allowlist files and [the reference page](mutation-testing.md). No module changed.
- **#300** (a fix, not a test rewrite): a handoff that lists the supervisor's own queue write in `dirty_paths_outside_scope` no longer fails its slice. The reported paths are judged by the same scope rule as `files_touched`.
- **#301 `evaluate_handoff`:** the 18 gates moved into a pure `decide(facts)` with `next_stage(facts)`, behind a thin shell that gathers each fact only when asked. It added 108 in-memory tests and kept the 32 subprocess tests.
- **#302 `policy`:** the proof check split into a shell (`observe_proof_files`) and a pure core (`check_manual_proofs`), so `evaluate_completion` can take its proof facts as an argument. A mutable default became `None`. Some internal rewrites removed mutants no test could kill, with identical outputs.
- **#303 `settings`:** test-only. Output tables pin the precedence chain that `--explain` prints, the deprecation strings, narrow-only choices and the kill switch.
- **#304 boards:** the projection and shadow diff are tested on in-memory snapshots, and the Hermes writer only through a stub `hermes kanban` CLI. The local board is tested on real SQLite with an injected clock and no mocks. It removed the mock of the module's own `kw._task`, and deleted two tests after moving their coverage.
- **#305 `runner_profile` and `handoff_summary`:** `next_action()` split into `decide_next_action()` and `render_next_action()`, with exact-bound tests. `parent_launch_mode()` takes its `ps` reader as an argument, replacing a test that patched the module's own function.
- **#306 `probe`, `last_build`, `instances`:** seams only (`make_probe_dirs`, keyword factories on `run_probe` and `main`), replacing `mock.patch.object` of the module's own attributes. It added a new `test_benchmark_instances.py` and output-based probe tests.
- **#307 context, memory, schemas, operator status:** keyword-only `environ`, `now` and `repo_root` seams. Hindsight is stubbed only at HTTP, git runs for real and the Swarm Wiki compiler is a stand-in subprocess. It deleted nine loose memory tests in favour of whole-value tables.
- **#308 `strategies` and `lanes`:** an injected clock, environment and commit. `run_experiment` now reads the commit once, which is one intended behaviour change, noted in the changelog. It added `test_benchmark_strategies.py` and `test_benchmark_lanes_core.py`.
- **#309 release tools:** `Tools/*.py` are now measured by coverage. The three release gates are tested by their exit codes and exact output, in process, with one subprocess test each. No production change.
- **#310 `diagnosis` and the sandbox:** a pure `diagnose(evidence)` behind a `gather_evidence` shell, and the sandbox tested at Docker's seam.
- **#311 `run_next`:** `_supervise` split into a pure functional core (`plan_step`, `judge_step` and others) and an imperative shell. It replaced tests that mocked the supervisor's own collaborators with integration tests on a real git repository, queue and fake agent.
- **#312 `scoring`, `run`, `evalrunner`:** `lanes_usage_error` moved out of `main` as a pure function. The tests are tables of eval data in and scores out, and every CLI subcommand runs end to end with fake commands.
- **#313 telemetry:** an injected clock and environment mapping, and removal of an `except` that could never run. It removed patches of the module's own functions, and kept one patch that stops real agent sessions.

## Suite runtime and coverage

| Measure | Result | Target |
| --- | --- | --- |
| Full suite wall time, `uv run --frozen pytest automation/tests -q`, one run | 490 s (8 min 10 s): 2,484 passed, 5 skipped | about 4 min: **not met** |
| Branch coverage, `uv run --frozen pytest automation/tests --cov=automation` | 88.9% of branches (2,833 of 3,188) | 88%: met |
| Combined coverage, as `fail_under = 88` checks it | 92.2% (statements 93.2%) | 88%: met |

The suite was timed once, not three times, because the machine was shared all day.

- **Load during the timed run:** the one-minute load average was 8.8 at the start and 7.3 at the end, on 18 CPUs. The 15-minute average was still falling from a peak, 72 to 45.
- **This is not a quiet-machine number.** A quiet machine would likely be faster, but this run gives no evidence that it would reach 4 minutes.
- **The coverage run** took 458 s, at a one-minute load of about 7.

The suite runs in one process, so its time is roughly the sum of its tests' times. Running it in parallel, or speeding up the slowest subprocess tests, is a separate decision.

## Below target: the next work list

Survivors are listed as `file:line`. Each report in `quality/reports/` has the diffs.

- **`automation/benchmark/judge.py`: 81.9%, target 90%.** No rewrite PR covered it. 32 survived and 2 had no coverage. The clusters:
  - range checks on the verdict score and the stop gate (`:140`, `:358`, `:360`);
  - the submission limits (`:215` to `:221`) and `timeout_seconds < 1` (`:239`);
  - limit constants (`:74`, `:76`, `:78`);
  - the repeat loop (`:454`, `:460`);
  - rounding and JSON formatting (`:385`, `:443`);
  - clean-up and `mkdir` flags (`:209`, `:257`, `:310`, `:311`);
  - the unreadable-verdict return (`:122`), which no test runs.
- **`automation/benchmark/adapter.py`: 73.1% (75.5% of mutants with a verdict), target 80%.** No rewrite PR covered it. 25 survived and 12 had no coverage. The clusters:
  - the link-safe copy: the special-file and refusal returns (`:326`, `:374` to `:396`), and the per-entry error handling (`:435` to `:469`), whose `errno` branches (`:444` to `:452`) no test reaches;
  - the bounded read (`:352`, `:353`, `:367`);
  - the queue template's literals (`:147`, `:159`, `:161`);
  - the argument order passed to `SandboxWitness` (`:584`, `:713`);
  - the clean-room receipt (`:700` to `:712`).
- **`automation/supervisor/sync_pin.py`: 53.8%, target 80%.** No rewrite PR covered it. The lock-file line parser survives almost whole: blank and comment lines (`:29`, `:30`) and the choice of separator (`:31`, `:32`). The empty-result returns (`:49`, `:51`) also survive.
- **`automation/context/build_context.py` †: 34.8%, target 80%.** 76 of 118 mutants were `unreliable_selection`, all from selections that timed out. Of the 42 that got a verdict, 41 were killed. The one survivor is `required=True` on `--slice-id` (`:43`). #307 measured 96.6% on its branch. The fix is a quiet-machine re-measure, not new tests.
- **`automation/supervisor/telemetry_settings.py` †: 68.8%, target 80%.** 46 mutants were `unreliable_selection`, from timeouts. Of the 108 with a verdict, 106 were killed. The survivors are `return False` at `:89`. #313 measured 98.7%. Again, re-measure.
- **`automation/benchmark/__main__.py`: 0%, target 80%.** It has one mutant, `if __name__ == "__main__"` (`:7`), and no test runs `python -m automation.benchmark`. One subprocess test would kill it. A reviewer may judge it not worth a test.

Close to target, with survivors worth a look: `evaluate_handoff.py` (91.0%) has 18 survivors. Most are subprocess flags (`:78` to `:182`), and the CLI's exit path (`:643` to `:652`) is untested.

Not measured here: 13 Python modules that were missing from the baseline's lists when this work began. They are `Tools/lane_admission_report.py`, `automation/supervisor/lane_admission.py`, `automation/supervisor/lane_workspace.py`, `automation/examples/demo_reject.py` and the nine `automation/benchmark/trackr/` modules. They are now listed under `not_yet_measured`, so the gap is recorded. Measuring them is a separate change.

## Allowlist candidates

No allowlist entries were added. An entry is a claim that a bug can't exist, so it needs an independent reviewer and a commit by a person. These survivors look equivalent and are worth that review:

1. **A `return None` or bare `return` at the end of a function, replaced by `pass`.** An AST check confirmed that control then falls through to the function's end, which returns `None` anyway. The survivors: `policy.py:396`, `policy.py:403`, `evaluate_handoff.py:510`, `operator_status.py:139`, `memory.py:203`, `swarm_wiki.py:96`, `probe.py:200`, `probe.py:314`, `tracing.py:116`, `adapter.py:329` and `telemetry.py:446`.
2. **`sys.path.insert(0, …)` becoming `insert(1, …)` in script bootstraps:** `evaluate_handoff.py:38`, `run_next.py:22`, `handoff_summary.py:38`, `settings_view.py:25` and `Tools/release_sbom.py:22`. Each is equivalent while nothing earlier on `sys.path` shadows the module. #309 and #311 reached the same view.
3. **`evalrunner.py:36`, both mutants.** `max(1, min(os.cpu_count() or 1, MAX_DOCKER_CPUS))` is at least 1 either way, because `MAX_DOCKER_CPUS` is 10 (#312).
4. **`scoring.py:61` and `scoring.py:65`, `total_tests > 0` becoming `>= 0`.** With no tests, `pass_fraction` is 0.0, below both thresholds (1.0 and 0.95), so the result is the same.
5. **`policy.py:232`, `return ""` becoming `pass` or `return None`.** This is equivalent only if every caller tests the result for truth, as #302 found.
6. **`@dataclass(frozen=True)` becoming `frozen=False`** on result types that nothing assigns to or hashes. The survivors: nine in `run_next.py` (`:288` to `:355`), `policy.py:101`, `evaluate_handoff.py:218`, `scoring.py:28`, `scoring.py:82`, `handoff_summary.py:310`, `judge.py:90`, `judge.py:342`, `telemetry.py:604` and `telemetry_metrics.py:61`. These aren't strictly equivalent: `frozen=False` drops `__hash__` and allows assignment. A reviewer may prefer a test that pins immutability.

These are not candidates:

- **JSON `indent` survivors:** `runner_profile.py:410`, `Tools/release_sbom.py:103`, `judge.py:443` and `adapter.py:702`. They change the output bytes, so they aren't equivalent. Whether they are worth a test is a reviewer's call.
- **Constants moved by one,** such as timeouts and limits. These are gaps at exact bounds.
