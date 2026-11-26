# Track R pilot (SPEC-012)

Track R compares one coding agent run two ways on the same brownfield tasks, scored by an
independent oracle: **U+**, unbounded but told the scope, and **S**, supervised by Boundroom. This
directory holds the frozen task set, the driver, the oracle, the containment and the pilot runner.
Building and checking the task set runs no model and spends nothing.

| File | What it is |
|---|---|
| `tasks.v2.json` | The v2 task set: what a blinded scope writer may read, plus the scopes once written, and the audit. |
| `solutions.v2.json` | The human solutions, hidden tests and oracle results. Scope writers never see it. |
| `wheelhouse.v2.json` | Every package installed anywhere, with its version and sha256. |
| `select.py` | Freezes the task set and runs the sandboxed oracle and green-base checks. |
| `driver.py` | Runs one (task, arm) session in a disposable clone. |
| `oracle.py` | Scores a session's final commit with no agent present. |
| `sandbox.py` | The `sandbox-exec` profile every checked command runs under. |
| `target.py` | The sync, the offline venvs and the test-harness restoration. |
| `wheelhouse.py` | Builds and checks the offline wheelhouse. |
| `pilot.py` | Runs the pilot: 6 tasks × 2 arms, the budget stop, the summary and the blind-review export. |
| `tasks.pilot.json` | **v1, void.** Kept as the record (see the end of this file). |

Run each one as a module, for example `python -m automation.benchmark.trackr.pilot --help`. Don't
run them as scripts: run as a script, `select.py` would shadow the standard library's `select`.

## v2 design (owner decisions, 2026-11-25)

- **Target: python-semver** (github.com/python-semver/python-semver, BSD-3-Clause). The candidates
  are six pull requests merged after the model's training cutoff, found by a research agent and
  chosen by the owner: #516 (issue #515), #508, #507, #489, #485 and #469. The selection rule
  checks them and doesn't widen the pool.
- **Arms: U+ against S**, one session each per task: 12 sessions.
  - **U+** gets the task statement plus the allowed paths, required checks and file budget, in
    exactly the words the supervisor's slice prompt gives S. The driver takes those lines from the
    synced `automation/prompts/slice.md` and fills them with the supervisor's own
    `run_next.format_value_list`; a test renders S's real prompt and checks each segment appears in
    it word for word. U+ runs with arm U's settings (Claude Code's Bash sandbox, fail-closed,
    `acceptEdits`) and no gates.
  - **S** runs `run_next.py --single-slice` with its default `bounded` profile. Its per-slice
    settings (`dontAsk`) allow edits only inside the slice's `allowed_paths` and its handoff, so
    tool-level edits outside them are denied, not asked about. Each S session records those rules
    (`supervised.edit_rules`).
- **Scopes** were written by blinded agents, by the owner's decision, from the linked issue or, where
  none is linked, the pull request's title only. Each writer also saw a file listing of
  `src/semver/`, `tests/`, `changelog.d/` and `docs/usage/` at its own task's base.
  - A first round was discarded unused. It had given every writer one listing, taken from #516's base,
    which showed older tasks test files that their own fixes add.
  - Each task's `scope_provenance` records this.
  - Every task's `required_validations` is `git diff --check` and `.venv/bin/python -m pytest -q`.
  - `tasks.v2.json` is `"status": "frozen"` with its `frozen_at` date. The pilot and the driver refuse
    a real run unless the task set is frozen and every scope is filled in.
- **The model is pinned:** both arms run with `--model claude-opus-5-5`. Each record keeps the model
  it was configured with and the model in the argv that actually ran (`observed_model`).

## Setup: the same in both arms but for S's queue

1. A fresh clone of python-semver at the task's `base_sha`, outside any repository, with no remote,
   and with every object after the base pruned, so the human solution can't be found in it.
2. The clone's own `.venv`, installed offline from the wheelhouse (below), excluded from Git through
   `.git/info/exclude`.
3. **Boundroom v0.5.3 plus #300 beside semver's code.** Boundroom's own `Tools/boundroom-sync.sh --sync` runs
   once per pilot or selection, from a clean checkout of `main` at #300's merge,
   `2136eb910780c1e79c6598c84ddf734c95a5217c` (v2.1; v2 used the v0.5.3 release commit `b193297`), into a staging directory, with `--pin`,
   `--expect-remote` and the source's quality gate (`make reusable-check`). The staged files are
   copied into each clone, and the script's own `--check` confirms the clone matches the manifest.
   The optional `benchmark` component is left out (`--omit benchmark`). v0.5.3 already has #279's
   fix for a handoff outside the repository, so no patch is applied.
4. Arm S only: `automation/queue/slices.json`, one queued slice built from the task's scope.
5. One setup commit holds all of it. The sync and the queue are the only difference between the arms.

**A green base.** Before any model session, every required check runs at the setup commit, under
`sandbox-exec`. If one fails, the session is recorded as `base_not_green`, no model starts, and it
costs nothing. Selection applies the same check, so a task whose base is red is never selected.

## The gates' false-positive baseline

For each task, `solutions.v2.json` records whether the human solution fits its own scope
(`false_positive_baseline`). `select.false_positive_baseline` computes this from the solution's files
and the task's `allowed_paths` and `max_files_changed`, using the same check the oracle applies. The
pilot's summary prints the result:

**S gate would reject the human solution: 2/6.**
- **#469** touches `docs/usage/increase-parts-of-a-version_prereleases.rst`, which is outside its
  scope.
- **#516** changes 7 files against a budget of 4. Three of them are outside its scope:
  `.github/workflows/python-testing.yml`, `pyproject.toml` and `uv.lock`.
- The other four fit.

**The scopes were not adjusted after this was seen.** The baseline is part of the result: when a
scope stops a correct change, that is a real cost of the method.

## The offline wheelhouse

PyPI is reached once, by `wheelhouse.py build`, outside any sandbox. After that, every install is
`uv pip install --offline --no-index --find-links <wheelhouse> --require-hashes`. The hashes below
are the ones in semver's `uv.lock` at the tasks' bases and in Boundroom's `uv.lock` at b193297, unchanged at 2136eb9; a
test checks `wheelhouse.v2.json` pins exactly these.

| Group | Package | Version | Wheel | sha256 |
|---|---|---|---|---|
| target | pytest | 9.1.1 | `pytest-9.1.1-py3-none-any.whl` | `37a86b45efb9a47a…` |
| target | pytest-cov | 7.1.0 | `pytest_cov-7.1.0-py3-none-any.whl` | `a0461110b7865f9a…` |
| target | coverage | 7.14.3 | `coverage-7.14.3-cp312-cp312-macosx_11_0_arm64.whl` | `96150a9cf3468ea2…` |
| target | packaging | 26.3 | `packaging-26.3-py3-none-any.whl` | `d7193f7c8e4e93f4…` |
| target | pluggy | 1.6.0 | `pluggy-1.6.0-py3-none-any.whl` | `e920276dd6813095…` |
| target | iniconfig | 2.3.0 | `iniconfig-2.3.0-py3-none-any.whl` | `f631c04d2c48c52b…` |
| target | pygments | 2.20.0 | `pygments-2.20.0-py3-none-any.whl` | `81a9e26dd42fd28a…` |
| supervisor | pydantic | 2.14.3 | `pydantic-2.14.3-py3-none-any.whl` | `3f7b347782ab7e4c…` |
| supervisor | pydantic-core | 2.50.4 | `pydantic_core-2.50.4-cp312-cp312-macosx_11_0_arm64.whl` | `1e20cbde2b6deb33…` |
| supervisor | typing-extensions | 4.16.0 | `typing_extensions-4.16.0-py3-none-any.whl` | `481caa481374e813…` |
| supervisor | annotated-types | 0.8.0 | `annotated_types-0.8.0-py3-none-any.whl` | `f072f4d804ea359e…` |
| supervisor | typing-inspection | 0.4.4 | `typing_inspection-0.4.4-py3-none-any.whl` | `65b8397ba37ccbce…` |

The full hashes are in `wheelhouse.v2.json`. The **target** group is what semver's test
configuration needs: `.pytest.ini` adds `--cov`, and `tests/conftest.py` imports `packaging`. On
CPython 3.12, pytest needs neither `exceptiongroup` nor `tomli`. The **supervisor** group is
Boundroom's runtime dependency, installed into one interpreter outside every clone.

## Containment

The checker and the green-base check run every command under `/usr/bin/sandbox-exec` with this
profile (`sandbox.PROFILE`; the parameters are the session's own directories):

```scheme
(version 1)
(deny default)
(allow process-fork process-exec)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-ioctl)
(allow file-read*)
(deny file-read*
  (subpath (string-append (param "REAL_HOME") "/.ssh"))
  (subpath (string-append (param "REAL_HOME") "/.aws"))
  (subpath (string-append (param "REAL_HOME") "/.config/gh")))
(allow file-write*
  (subpath (param "CLONE"))      ; the session's clone
  (subpath (param "TMP"))        ; its private TMPDIR, outside the clone
  (subpath (param "HOME"))       ; its temporary HOME, outside the clone
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random") (literal "/dev/urandom")
  (literal "/dev/dtracehelper") (regex #"^/dev/tty") (regex #"^/dev/fd/"))
(deny network*)
```

- Writes are denied by default and allowed only to the session's own directories. `/private/tmp` and
  `/var/folders` are therefore writable only where the session's own directories sit inside them.
- The command runs under `env -i` with an allowlist: a fixed `PATH` (the clone's `.venv/bin`, then
  the system), the temporary `HOME` and `TMPDIR`, `LANG` and `LC_ALL`, and Git pointed at an empty
  config. No credential or token variable reaches it.
- Tests run the real profile on toy commands (macOS only). They check that the network is blocked,
  writes to HOME and to the rest of the temporary directory are blocked, the clone and the private
  TMPDIR are writable, and only the allowlisted variables arrive.
- The model's own session is contained by Claude Code's sandbox, not by this profile. This profile
  is for the code that runs with no agent present.

## What the checker won't trust

- **The test harness.** Before running any check or hidden test, the oracle restores every
  `conftest.py`, `pytest.ini`, `.pytest.ini`, `setup.cfg`, `tox.ini`, `noxfile.py`, `.coveragerc`
  and pyproject's `[tool.pytest]` tables to the setup commit's version. Where the human pull request
  changed one, it uses the head version instead. Only then are the hidden tests and their support
  files copied in. Whether the session changed any harness file is recorded
  (`harness_changed_by_session`), and such a change counts as out of scope even inside
  `allowed_paths`.
- **`.git`.** `git add` and `git commit` run outside Claude Code's sandbox, so a session that planted
  a hook or `core.fsmonitor` could run code with the network. To stop that:
  - S's settings, from the supervisor, deny edits under `.git/`. U+'s deny edits to `.git/config`,
    `.git/hooks` and `.git/info`: a chained `git add … && git commit …` runs inside Claude Code's
    sandbox, and denying all of `.git` there blocked every U+ commit in run r1 (see v2.1 below);
  - every git command the driver runs uses `-c core.hooksPath=/dev/null -c core.fsmonitor=false`;
  - after the session, `.git/config`, `.git/hooks` and `.git/info` are compared with their state at
    setup. A change is recorded as an `escape_attempt`, and the oracle never runs that session's code.
- **The configured cap.** `run_next` always asks for its own default of $5. In arm S, a wrapper
  (`BOUNDROOM_CLAUDE_BIN`) replaces it with the driver's cap, adds the model pin, and writes the exact
  argv it runs to `runner-argv.json` just before it starts Claude Code. Each S record keeps the
  observed cap and model from that argv (`observed_cap_usd`, `observed_model`) and the value it
  replaced (`supervisor_budget_args`). The wrapper also drops `--no-session-persistence`, so S
  leaves a transcript as U+ does.
- **The gate's name.** `gate_of` names what stopped an S session by matching the wording of
  `run_next`'s reason. Each record keeps the raw reason beside it (`supervised.raw_reason`). The name
  depends on that wording, because `run_next` has no `stop_code` yet.
- **Paths.** numstat, name-status and status are read with `-z`, so quoted, spaced and renamed paths
  parse.

## Blinding, failures and cleanup

- When a session ends, its change gets a random review id and its normalized diff is held in a
  private staging directory. `mapping.json` is rewritten after every session, and each results row
  records its review id. The reviewer's `review/` directory is written once at the end, in a
  shuffled batch, with every file time set to the epoch.
- A session, oracle or export error is recorded against its session, and the session is charged
  what it cost: the full cap if the cost is unknown, or nothing if it stopped before the model
  started. The pilot then goes on to the next session. Each session's clone is removed in a
  `finally`, and the mapping and summary are always written.
- On SIGTERM, SIGINT or SIGHUP, the running session's process group is killed and the session is
  recorded as `interrupted`. `subprocess_runner` writes output to files and waits on the process, so
  a background child holding a pipe doesn't turn a finished command into a timeout.

## Before a real run

1. Done: the blinded scopes are written, and `select.py --recheck --freeze <date>` reran the sandboxed
   oracle and green-base checks with each task's own `required_validations`. All six passed, and the
   task set was frozen. `--recheck` writes only `solutions.v2.json`; it reads the scopes and doesn't
   change them.
2. The owner approves the frozen design and the spending.
3. The dry run passes:
   `python -m automation.benchmark.trackr.pilot --dry-run --out <dir> --cache-dir <dir> --wheelhouse <dir>`.
4. The pilot is started from a plain terminal, where both Claude Code's sandbox and `sandbox-exec`
   can start, from a clean checkout of this branch:

   ```sh
   uv sync --frozen
   uv run python -m automation.benchmark.trackr.wheelhouse build --dest ~/trackr-v2/wheelhouse
   uv run python -m automation.benchmark.trackr.pilot --yes \
     --out ~/trackr-v2/pilot-r1 --cache-dir ~/trackr-v2/cache --wheelhouse ~/trackr-v2/wheelhouse \
     --budget-total 53.34 --cap-usd 4.50 --timeout 1800 --model claude-opus-5-5
   ```

   `wheelhouse build` reaches PyPI once and fails unless every wheel matches `wheelhouse.v2.json`.
   The pilot refuses to start if the task set isn't frozen, if a scope is missing, if `--model` isn't
   the frozen model, if `--out` isn't empty, or if `claude --help` lacks a flag the arms use.

## Known limits

- **Untested against a real `claude`:** the sandbox keys, `--setting-sources`, `--strict-mcp-config`,
  `--model`, how a budget stop is reported, and finding the transcripts. Before a real run, the
  pilot checks that `claude --help` lists every flag the arms use.
- **Six tasks with one repeat** measure cost, censoring and blinding, not effectiveness.
- **One machine:** the wheelhouse is for CPython 3.12 on macOS arm64.

## v2.1: what changed after run r1 (owner decisions, 2026-11-26)

Run r1 was stopped by the agent after 6 sessions and $2.65, and it is void: two defects biased it, one
against each arm. `tasks.v2.json` records both amendments, the void run and the budget.

- **U+ couldn't commit.** U+ chains commands (`pytest … ; git add … && git commit …`). A chained
  command doesn't match the sandbox's `excludedCommands`, so it ran inside the sandbox, where
  `Edit(.git/**)` denied `.git/index.lock`. U+ committed nothing in its 3 sessions, and the checker
  scores the final commit. U+ now denies only `.git/config`, `.git/hooks` and `.git/info`. A $0.10
  probe with real Claude Code confirmed that a chained commit goes through and writes to hooks and
  config are still denied. S keeps the supervisor's own rule; its allowlist runs git on its own.
- **S failed an honest handoff.** v0.5.3 failed a slice whose handoff listed the supervisor's own
  queue write in `dirty_paths_outside_scope`, as `base.md` asks. On pr485 that was an in-scope fix
  that passed the hidden tests. #300 fixed it, and S now runs `main` at #300's merge. The sandboxed
  oracle and green-base checks were rerun with the new sync: 6 of 6 pass, and the gates still reject
  the same 2 human solutions. The pilot and the driver refuse a real run with a task set checked
  against another supervisor than the one they sync.
- **Budget.** Of the $60, v1 spent $3.90, run r1 $2.65 and the probe $0.10, so the rerun's budget
  stop is $53.34. The cap per session, the timeout and the model are unchanged.

## v1: void

The v1 task set (`tasks.pilot.json`, Boundroom's own pull requests) is void. Its hidden tests assert
literal documentation and YAML text that the blinded scopes left out, and the sessions ran with the
network off, so neither arm could pass: a floor effect with no signal. Three real sessions were made
from it, two complete and one interrupted, about $3.90, and all were discarded. The pilot and the
driver refuse a void task set except in a dry run.

The v1 record also holds its amendments, each made after real sessions had run, so none was
preregistered:
- a v0.5.3 overlay;
- a green-base check;
- replacing the overlay with a patch of only #279's fix.

The v1 scopes were written by blinded subagents, by the owner's decision, not by a person.
