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
| `--update-baseline` | off | Writes this run's scores into the baseline file. Commit that change through a pull request. |
| `--baseline-modules` | off | Measures every module the baseline file lists. |
| `--show-survivors N` | 20 | Survivors printed per module. The report has all of them. |

Exit codes: 0 when the run finished and, with `--check-baseline`, no score dropped; 1 when a score dropped below its baseline, or a module with a baseline couldn't be scored at all; 2 on a usage error, or when the harness couldn't measure, for example because the module's tests fail without any mutant; 130 when interrupted with Ctrl-C or SIGTERM. Each module's report is written as soon as that module is measured, so an interrupted run keeps the modules it finished.

## How it works

1. **One coverage run.** The harness copies the repository to a temporary directory and runs the whole suite there once, under `coverage`, with one coverage context per test. Many tests run `run_next.py` and `evaluate_handoff.py` as scripts, so the run also measures subprocesses: the suite runs under a throwaway virtual environment whose `.pth` file starts coverage in every Python process, labelled with the test that was running. The result maps each line of the target modules to the tests that execute it, and records how long each test took. Nothing is installed in the repository or in its `.venv`.
2. **Mutants.** The harness parses the module and makes one mutant per change below. Each mutant changes one AST node, and is written by replacing that node's exact span in the source text, so comments and formatting elsewhere stay as they are and the diff is a line or two.
3. **Runs.** For each mutant it writes the mutated file into one of `--jobs` disposable copies of the repository (a Git clone that shares the original's objects, plus the working tree's files) and runs only the tests that execute the mutated line, fastest first, with `python -m pytest -x`. Because the mutant is on disk, scripts the tests start as subprocesses load it too.
4. **Timeouts.** A run that passes its time limit is killed with its whole process group, so no test process or child is left behind.
5. **Clean-up.** Every copy, the throwaway environment and every log live under one temporary directory, which is removed at the end, including after Ctrl-C or SIGTERM, which also kill every running test. The repository itself changes only in the report files below.

Before running a module's mutants, the harness runs that module's tests once, unmutated, in a copy. If they fail, it stops with exit 2 rather than count every mutant as killed. That run also proves the tests' node ids select, so any failure under a mutant, including pytest's usage error or "no tests ran", counts as a kill: the mutant caused it, for example by breaking an import in `conftest.py`. Tests that fail in the coverage run are left out of every selection and listed in the report as `excluded_tests`.

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
module                                 total  killed  timeout  survived  no_cov  allowlisted  error   score
-------------------------------------  -----  ------  -------  --------  ------  -----------  -----  ------
automation/supervisor/runner_profile.py  ...
```

| Column | Meaning |
| --- | --- |
| `killed` | A test failed. |
| `timeout` | The tests ran past the time limit, usually an endless loop. It counts as killed. |
| `survived` | Every test that executes the line passed. |
| `no_cov` | No test executes the line at all. A survivor, flagged separately because the fix is a test that reaches the line. |
| `allowlisted` | Excluded by an exact entry in the allowlist, below. |
| `error` | The mutant didn't compile. Not counted either way. |
| `score` | (killed + timeout) / (total - allowlisted - error) |

Each survivor is printed as `file:line operator` and a unified diff. The full result is in `quality/mutation-report-<module>.json`, where `<module>` is the path with dots for slashes, such as `mutation-report-automation.supervisor.policy.json`. It lists every mutant with its status, the number of tests run, the time taken and the time limit, then the survivors with their diffs.

A survivor means one of three things:

- **A gap in the tests.** Write a test that fails on the mutant and passes on the real code. This is the usual case.
- **An equivalent mutant.** The change can't alter behaviour, such as `n < 0` becoming `n <= 0` where both branches return 0 at `n == 0`. Add an allowlist entry.
- **Code not worth testing.** Say so in the pull request; don't hide it with an allowlist entry.

## The baseline: a ratchet

`quality/mutation-baseline.json` records each measured module's score. `--check-baseline` fails, with exit 1, when a module scores lower than its baseline, so a change that weakens the tests is caught. Raise a module's baseline when its tests improve, by running with `--update-baseline` and committing the result in the pull request that improved them. Modules the harness hasn't measured yet are listed under `not_yet_measured`.

The advisory workflow `.github/workflows/mutation.yml` runs the harness weekly and on demand, over every module in the baseline. It is not a required check, so it doesn't block a merge.

## The allowlist

`quality/mutation-allowlist.json` holds equivalent mutants. Each entry names the `module`, `line`, `operator`, the node's `original` text and its `mutant` text, copied from the report, plus a `reason` and `reviewed_by`. An entry excludes a mutant only when all five of the first fields match exactly, so an entry can't hide a different mutant on the same line. When the code changes and an entry matches nothing, the harness prints it as a stale entry; remove or update it.

Each entry is a claim that a bug can't exist, so a person other than the change's author checks the reason before it merges, and names themselves in `reviewed_by`.

## Limits

- **Selection depends on coverage.** A mutant only runs against tests that execute its line. A subprocess the test starts with an environment that drops `PATH`, or with an interpreter other than the one running the suite, isn't measured, so a line only it executes reads as `no_cov`. Code that runs at import time (module level, class bodies, default values, decorators) runs once per process, under whichever test imported the module first, so its mutants run against every test that covers the module.
- **Equivalent mutants are counted as survivors** until someone allowlists them, so a score below 100% doesn't by itself mean a test is missing.
- **It is slow by nature.** Each surviving mutant reruns every test that executes its line.
- **Timeouts assume a steady machine.** A timeout is counted as a kill, so a machine that is heavily loaded, or sleeps and wakes, during a run can turn slow tests into false kills. A run of `timeout` results on code that can't loop is the sign; measure that module again on a quiet machine before recording its baseline.
