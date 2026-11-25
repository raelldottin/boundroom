# Mutation testing

Coverage says a line ran during the tests. It doesn't say a test would notice if the line were wrong. The mutation harness in `Tools/mutate/` measures that. It plants one small bug at a time in a module, a *mutant*, and runs the tests that execute the changed line. A mutant the tests fail on is *killed*. A mutant they pass with *survived*: it is a bug the suite would have let through.

The harness uses the standard library plus `pytest` and `coverage`, which are already development dependencies. It adds no package.

## Run it

```sh
make mutation MODULE=automation/supervisor/runner_profile.py
make mutation MODULE=automation/supervisor/policy.py,automation/supervisor/evaluate_handoff.py
make mutation MODULE=automation/supervisor/policy.py MUTATION_ARGS="--jobs 4"
```

`make mutation` runs `uv run python -m Tools.mutate --check-baseline --nice -- <modules>`, adding whatever `MUTATION_ARGS` holds. It isn't part of `make reusable-check`, because it reruns tests once per mutant. A small module takes a few minutes. A large one, such as `run_next.py`, can take an hour or more.

Options worth knowing:

| Option | Default | What it does |
| --- | --- | --- |
| `--jobs N` | CPU count minus 2 | Test runs in parallel, each in its own copy of the repository. |
| `--nice` | off (always on in `make mutation`) | Runs at lowered CPU priority, so other work on the machine keeps its speed. |
| `--min-timeout S` | 30 | The shortest time limit for one mutant's test run. |
| `--timeout-factor F` | 3 | A mutant's limit is the larger of `--min-timeout` and F times the time its tests took unmutated. |
| `--check-baseline` | off (on in `make mutation`) | Exits 1 if a module's score is below its score in `quality/mutation-baseline.json`. |
| `--update-baseline` | off | Records this run's numbers for each module whose score held or rose. A lower score is kept out. Refused, with exit 2, from a working tree with changes other than the harness's own reports and baseline. Commit the result through a pull request. |
| `--lower-baseline MODULE` | none | Lets that module's recorded score go down (repeatable). Needs `--reason`, which is recorded in the baseline file. Implies `--update-baseline`. |
| `--baseline-modules` | off | Measures every module the baseline file lists. |
| `--show-survivors N` | 20 | Survivors printed per module. The report has all of them. |
| `--report-dir DIR` | `quality/reports` | Where the per-mutant reports go. Git ignores the default directory. |

Exit codes: 0 when the run finished and, with `--check-baseline`, no score dropped; 1 when a score dropped below its baseline, or a module with a baseline couldn't be scored at all; 2 on a usage error, an `--update-baseline` from a working tree with changes, or when the harness couldn't measure, for example because none of a module's test selections passes without a mutant; 130 when interrupted with Ctrl-C or SIGTERM. Each module's report is written as soon as that module is measured, so an interrupted run keeps the modules it finished.

## How it works

1. **One coverage run.** The harness copies the repository to a temporary directory and runs the whole suite there once, under `coverage`, with one coverage context per test. Many tests run `run_next.py` and `evaluate_handoff.py` as scripts, so the run also measures subprocesses: the suite runs under a throwaway virtual environment whose `.pth` file starts coverage in every Python process, labelled with the test that was running. Many tests also copy `automation/supervisor` into a temporary Git repository and run the copy. Coverage records a copy under its own path, so each process notes, as it exits, the content hash of every loaded file whose path ends in a target module's path. A copy at any path is credited to the module when its content matches the module byte for byte. The result maps each line of the target modules to the tests that execute it, and records how long each test took. Nothing is installed in the repository or in its `.venv`.
2. **Mutants.** The harness parses the module and makes one mutant per change below. Each mutant changes one AST node, and is written by replacing that node's exact span in the source text, so comments and formatting elsewhere stay as they are and the diff is a line or two.
3. **Runs.** For each mutant it writes the mutated file into one of `--jobs` disposable copies of the repository (a Git clone that shares the original's objects, plus the working tree's files) and runs only the tests that execute the mutated line, the module's own test files first, then fastest first, with `python -m pytest -x`. Because the mutant is on disk, scripts the tests start as subprocesses load it too, and so do copies a test makes of the module's directory.
4. **A failure is checked before it counts.** When the run under a mutant fails or times out, the same tests run again at once without the mutant. Only if they pass is the mutant `killed` (or `timeout`); if they fail too, the failure belongs to a flaky test, and the mutant is `unreliable`, which counts as survived.
5. **Timeouts.** A run that passes its time limit is killed with its whole process group, so no test process or child is left behind.
6. **Clean-up.** Every copy, the throwaway environment and every log live under one temporary directory, which is removed at the end, including after Ctrl-C or SIGTERM, which also kill every running test. The repository itself changes only in the files the run writes: the reports, which Git ignores, and the baseline with `--update-baseline`.

Before running any mutant, the harness takes each distinct set of tests that some mutant will run and runs it once without a mutant, exactly as the mutants will run it: the same tests, in the same order, with `-x` and the same options, alone in a copy. A set that doesn't pass that way, such as a test that only passes after another test has run, or a node id that collects nothing on its own, would fail under every mutant whatever the mutant did. Its mutants are marked `unreliable_selection`, which counts as survived, and the report lists each such set with its exit code under `unreliable_selections`. If no set passes, the harness stops with exit 2. Each set is checked once per run, however many mutants share it. Tests that fail in the coverage run are left out of every selection and listed in the report as `excluded_tests`.

The module text measured is the one copied for the coverage run, so editing the working tree during a run doesn't change what is measured. `--pytest-args` (shell-quoted) goes to the coverage run and to every test run.

## Mutation operators

| Operator | Change |
| --- | --- |
| `comparison` | `<` and `<=`, `>` and `>=`, `==` and `!=`, `in` and `not in`, `is` and `is not`, each way |
| `boolean_operator` | `and` and `or`, each way, for the whole expression |
| `remove_not` | `not x` becomes `x` |
| `boolean_constant` | `True` and `False`, each way |
| `number_increment` | an integer literal from 0 to 255 becomes n + 1 |
| `number_zero` | an integer literal from 1 to 255 becomes 0 |
| `delete_statement` | a `return` or `raise` statement, or a call statement whose function name starts with `_fail` or `fail`, becomes `pass` |
| `loop_control` | `continue` or `break` becomes `pass` |
| `return_none` | `return <expr>` becomes `return None` |
| `swap_arguments` | the first two positional arguments of a call swap, when both are names |

The harness doesn't mutate docstrings, type annotations, `__all__`, calls to `print` or to a logging method (`logger.info(...)`, `logging.warning(...)` and the like), code under `if TYPE_CHECKING:`, or the inside of f-strings. Two mutants that produce the same program are counted once. It mutates Python only: the shell scripts, such as `boundroom-sync.sh` and `run_agent.sh`, are out of its reach.

## Read the result

The table has one row per module:

```text
module                                   total  killed  timeout  survived  no_cov  allowlisted  error  unreliable_sel  unreliable  score
---------------------------------------  -----  ------  -------  --------  ------  -----------  -----  --------------  ----------  -----
automation/supervisor/runner_profile.py  ...
```

| Column | Meaning |
| --- | --- |
| `killed` | The tests failed under the mutant, whatever the exit code: failed tests, or a broken import or collection (exit 2) and the like, and passed again without it. |
| `timeout` | The tests ran past the time limit, usually an endless loop, and passed again without the mutant. It counts as killed. |
| `survived` | Every test that executes the line passed. |
| `no_cov` | No test executes the line at all. A survivor, flagged separately because the fix is a test that reaches the line. |
| `allowlisted` | Excluded by an exact entry in the allowlist, below. |
| `error` | The mutant didn't compile. The only status left out of the score, with `allowlisted`. |
| `unreliable_sel` | `unreliable_selection`: the tests that would judge the mutant don't pass without it when run alone. Counted as survived, so an order-dependent test lowers the score. |
| `unreliable` | The tests failed under the mutant and failed again without it: a flaky test. Counted as survived, so a flaky test lowers the score. |
| `score` | (killed + timeout) / (total - allowlisted - error) |

Each survivor, meaning any measured mutant that wasn't killed, is printed as `file:line operator` and a unified diff. The full result is in `quality/reports/mutation-report-<module>.json`, where `<module>` is the path with dots for slashes, such as `mutation-report-automation.supervisor.policy.json`. Git ignores that directory: the reports are large and change on every run, so only the baseline is committed, and the workflow uploads the reports as an artifact. A report lists every mutant with its status, the number of tests run, the time taken and the time limit, then the survivors with their diffs.

A survivor means one of three things:

- **A gap in the tests.** Write a test that fails on the mutant and passes on the real code. This is the usual case.
- **An equivalent mutant.** The change can't alter behaviour, such as `n < 0` becoming `n <= 0` where both branches return 0 at `n == 0`. Add an allowlist entry.
- **Code not worth testing.** Say so in the pull request; don't hide it with an allowlist entry.

## The baseline: a ratchet

`quality/mutation-baseline.json` records each measured module's score. `--check-baseline` fails, with exit 1, when a module scores lower than its baseline, so a change that weakens the tests is caught. Modules the harness hasn't measured yet are listed under `not_yet_measured`.

The ratchet only goes up:

- Raise a module's baseline when its tests improve, by running with `--update-baseline` and committing the result in the pull request that improved them. An update records a module's numbers only when its score held or rose.
- With `--check-baseline` too, the check runs first, against the baseline as it was, and a run that fails it records nothing.
- Lowering a score takes `--lower-baseline <module> --reason "<why>"`. The file keeps each lowering in its `lowered` list, with the old and new score, the reason and the commit, so a reviewer sees it in the diff. A lowering counts only once a person commits it: `--check-baseline` and `--update-baseline` stop with exit 2 if the commit that first holds a `lowered` record was authored by the agent or any other bot, or if no commit holds it yet. So a lowering reaches `main` only in a pull request the owner approves.

Each entry says what was measured, so anyone can reproduce it:

- `commit`: HEAD when the run started.
- `trees`: the Git tree id of each top-level directory holding Python (`automation`, `Tools`) at that commit. The tests and the harness are part of what a score measures, so they are recorded with the module. A squash merge makes a new commit, but it keeps unchanged trees, so these ids still name the code after the branch commit is gone. Check one with `git rev-parse <commit>:automation`.
- `module_blob`: the Git blob id of the module file measured, as `git hash-object <module>` prints it.
- `dirty`: whether the working tree had changes. An update is refused when it does, so a recorded entry says `false`.

A baseline measured on a branch describes that branch's code. It describes `main` only once the trees it records are the trees on `main`, or once it has been measured again there after the merge.

The advisory workflow `.github/workflows/mutation.yml` runs the harness over every module in the baseline, or the modules given, when a person starts it (`workflow_dispatch`). It has no schedule: a schedule needs the owner's approval as its own recorded action, and is added only once the owner approves one. It is not a required check, so it doesn't block a merge.

## The allowlist

`quality/mutation-allowlist.json` holds equivalent mutants. Each entry names the `module`, `line`, `operator`, the node's `original` text and its `mutant` text, copied from the report, plus a `reason`. Any other field is refused. An entry excludes a mutant only when all five of the first fields match exactly, so an entry can't hide a different mutant on the same line. When the code changes and an entry matches nothing, the harness prints it as a stale entry; remove or update it.

Each entry is a claim that a bug can't exist, so the harness accepts it only from a person. It reads the file's Git history, finds the first commit that holds the entry exactly, and stops with exit 2 if that commit's author is the agent or any other bot (a name ending in `[bot]`, or the agent's GitHub user id `354874784`), if no commit holds the entry yet, or if the file isn't in a Git repository. So an entry reaches `main` only in a commit a person authored, through a pull request the owner approves. A proposed `CODEOWNERS` line, `/quality/ @raelldottin`, would make that review explicit; adding it is the owner's change.

## Limits

- **Selection depends on coverage.** A mutant only runs against tests that execute its line. A subprocess the test starts with an environment that drops `PATH`, or with an interpreter other than the one running the suite, isn't measured, and neither is a copy whose content differs from the module or a process that exits without running its exit handlers. A line only such a run executes reads as `no_cov`. Code that runs at import time (module level, class bodies, default values, decorators) runs once per process, under whichever test imported the module first, so its mutants run against every test that covers the module.
- **Equivalent mutants are counted as survivors** until someone allowlists them, so a score below 100% doesn't by itself mean a test is missing.
- **It is slow by nature.** Each mutant reruns every test that executes its line, and each failing run is repeated once without the mutant.
- **Timeouts assume a steady machine.** A heavily loaded machine, or one that sleeps and wakes, slows tests. The recheck without the mutant keeps such a slowdown from counting as a kill, but it turns it into `unreliable` instead, which lowers the score. Measure on a quiet machine before recording a baseline.
