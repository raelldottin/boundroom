# Track R pilot (SPEC-012)

Track R compares an unbounded Claude Code run (arm U) with a supervised slice (arm S) on the same
brownfield tasks. An independent oracle scores both arms. This directory holds phase R0: the frozen
task set, the driver, the oracle and the pilot runner. R0 runs no model and spends nothing.

> **This task set is void (2026-11-25).** An adversarial review found a floor effect with no
> signal. The hidden tests assert literal documentation and YAML text that the blinded scopes
> deliberately left out, and sessions run with the network off, so neither arm can pass them.
> R1 pilot v1 is void, and no real session may start from `tasks.pilot.json`. The pilot and the
> driver refuse it except in a dry run. Three real sessions were made from it before that: two
> complete and one interrupted, about $3.90, all discarded. `provenance.model_sessions` records
> them. A v2 design follows: behavioural tasks, a contained checker, U+ against S, and a pinned
> model. The driver, oracle and pilot code here carry into v2.

| Module | What it does |
|---|---|
| `select.py` | Freezes the task set by the preregistered R-b rule and writes `tasks.pilot.json`, including an audit of every candidate. |
| `driver.py` | Runs one (task, arm) session in a disposable clone. |
| `oracle.py` | Scores a session's final commit with no agent present. |
| `pilot.py` | Runs R1: 6 tasks × 2 arms, the budget stop, the summary and the blind-review export. |

Run each one as a module, for example `python -m automation.benchmark.trackr.pilot --help`. Don't
run them as scripts: run as a script, `select.py` would shadow the standard library's `select`.

## Before a real run

A real run starts model sessions, which is spending. It needs a person's approval and `--yes`.
Before that:

1. **A blinded scope writer fills in each task's scope** (`task_statement`, `allowed_paths`,
   `required_validations`, `max_files_changed`) from the pull request's title and body only, without
   looking at its diff or files. By the owner's decision (2026-11-24), the v1 scopes were written by
   blinded subagents, one per task, not by a person. The writer records `by`, `when`, `inputs` and
   `leakage_ignored` in `scope_provenance`. The pilot
   refuses to start until every task has a scope. `provenance.task_changes.need_blinded_scope` lists
   the tasks still waiting for one.
2. **The dry run passes:** `python -m automation.benchmark.trackr.pilot --dry-run --out <dir> --cache-dir <dir>`.
   A fake Claude Code applies each human solution. Everything else runs for real.
3. **The pilot is started from a plain terminal**, not from inside a sandboxed shell. Claude Code's
   sandbox must be able to start, because both arms run it fail-closed.

## What the setup holds constant

- **The base patch.** `run_next.py` judges its own checkout, so each session runs the supervisor
  its task's base shipped with. Before #279, that supervisor gave a handoff outside the repository the
  rule `Edit(/abs)`, which Claude Code reads as relative to the settings file, so arm S could never
  write its handoff. Every clone, in both arms, therefore gets #279's fix to
  `automation/supervisor/runner_profile.py` and nothing else:
  - the one line that builds the handoff rule is rewritten, so an outside handoff gets `Edit(//abs)`;
  - #279's tests and changelog are not applied.

  The patch is then checked by calling the clone's own `slice_settings`. An outside handoff must get
  the absolute rule and no `git add` rule, and an inside handoff must be unchanged. A base that
  already has the fix is left as it is. A base the rewrite doesn't fit, or where the check fails, is
  dropped at selection, and a session there stops as `patch_failed` before any model starts.
  `tasks.pilot.json` (`base_patch`) and every session record (`base_patch`) record the patch.
- **A green base.** Before any model session, each session runs every one of the task's
  `required_validations` at its setup commit. If one fails, the session is recorded as
  `base_not_green`, no model starts, and it costs nothing. Every record keeps these results under
  `setup_validations`. Selection applies the same check, so a task whose base is red is never
  selected.
- **Interrupts.** Each session runs in its own process group. On SIGTERM, SIGINT or SIGHUP, the
  pilot kills that group (SIGTERM, then SIGKILL after five seconds), records the session as
  `interrupted` with whatever cost its transcript shows, writes the summary, and exits with
  128 plus the signal number.
- **The cap.** Both arms get the same `--max-budget-usd`, 4.50 by default. `run_next` always asks for
  its own default of $5, whatever the caller exports. In arm S, a wrapper (`BOUNDROOM_CLAUDE_BIN`)
  replaces that value with the driver's cap. Each session record keeps the value it replaced, under
  `supervised.supervisor_budget_args`.
- **The environment.** Both arms share one inherited environment. GitHub credentials, the SSH agent,
  Claude Code's markers and every `BOUNDROOM_*` and `GIT_*` variable are removed from it, and no
  `BOUNDROOM_*` variable is added back. Both arms load only project settings and no MCP server.
  Managed settings still load, in both arms alike.
- **The clone.** Each clone holds no Git object from after the task's base, so the human solution
  can't be found in it.

## Scoring, blinding and failures

- **The oracle exempts no path.** An agent's edit to the supervisor's queue or handoff directory
  counts as out of scope, in either arm, and the reviewer sees it. Arm S's queue file is in the setup
  commit, so it shows up in a diff only if the agent changed it. Only `.claude/` is left out of the
  reviewer's diff.
- **The hidden tests' support files** (helpers and fixtures under `automation/tests/` that the pull
  request added or changed) are installed with the hidden tests, at selection and in the oracle.
- **Review directories are written in one batch** after every session has finished, in a shuffled
  order, with every file's modification time set to the epoch. Neither creation order nor file times
  can then give an arm away.
- **A failure is recorded and the pilot goes on.** A session, oracle or export that raises is
  recorded against its session, and the session is charged what it cost: the full cap if the cost is
  unknown, or nothing if it stopped before the model started. The reviews, `mapping.json` and the
  summary are always written, even when a run stops early.
- **The driver CLI** checks `claude --help` for every flag the arms use before a real session, and it
  scores the session with the oracle before removing the clone.
- **A scope counts only with its provenance:** `scope_provenance` must carry `by`, `when`, `inputs`
  and `leakage_ignored`, plus `owner_decision` optionally. A scope whose `inputs` don't say it came
  from the title and body only, or say the diff was seen, is refused.

## Amendments

`tasks.pilot.json` records each amendment under `provenance.amendments`, the earlier selections
under `provenance.earlier_selections`, and the tasks dropped, added and needing a scope under
`provenance.task_changes`.

0. **The overlay (2026-11-24, after the R0 dry run, before any model session).** The dry run showed
   that the bases' own pre-#279 supervisor denies arm S's handoff. Every clone got
   `automation/supervisor/`, `automation/schemas/` and `automation/context/` replaced by their
   v0.5.3 contents, and the oracle check was redone with that overlay. Superseded by amendment 2.
1. **A green base (2026-11-25, after the first 2 real R1 sessions).** This was made after the R1
   pilot had run 2 sessions and part of a third, about $3.90. Those results are discarded.
   - **What went wrong:** with the overlay, the base was red before the agent changed anything.
     `UV_PYTHON=3.12 make reusable-check` failed at base+overlay, first at
     `settings_view.py --docs --check`. Arm S's agent saw a required validation fail at baseline and
     honestly stopped as `blocked`, with no commit. Every task would have hit the same failure,
     biasing the experiment against S.
   - **The change:** a new check, described above. It was added after real sessions had run, so it
     is not preregistered. Tasks that still qualify keep
     their blinded scopes unchanged, and no task's `required_validations` was changed.
   - **New candidates:** a candidate with no blinded scope yet is checked against the validations
     every blinded scope so far chose (`DEFAULT_REQUIRED_VALIDATIONS`).
   - **A second cause, fixed at the same time:** the driver had set `BOUNDROOM_TELEMETRY=off` in
     every environment it built. That kill switch breaks the repository's own telemetry tests inside
     `make reusable-check`, for the agent, the green-base check and the oracle alike.
2. **The base patch instead of the overlay (2026-11-25, also after those 2 sessions, by the owner's
   decision).**
   - **Why:** even with regeneration, the overlay left every base's own tests disagreeing with the
     overlaid v0.5.3 supervisor. Those tests cover the legacy-variable removal version, `run_next`'s
     warnings and prompt errors, and the telemetry exporter. As a result, only one task in the window
     had a green base.
   - **The change:** the overlay is dropped in favour of the base patch above. Selection was rerun
     over the same window, in the same merge order, with the oracle check and the green-base check
     made on base+patch.

## Known limits of the task set

- **Docs-heavy tasks.** The R-b rule keeps any pull request that touches a test file. Several of the
  selected tasks are mostly documentation, release or CI changes whose hidden tests check exact
  wording or file layout. A correct change in different words fails those tests, in both arms alike.
  So hidden-test pass rates will be low in both arms, and the rule is kept as preregistered. Read
  the pass rates alongside the blinded reviewer's verdicts, not on their own.
- **Small set.** Six tasks with one repeat measure cost, censoring and blinding, not effectiveness.
- **Untested against a real `claude`.** The sandbox keys, `--setting-sources`,
  `--strict-mcp-config`, how a budget stop is reported, and finding the transcripts under
  `~/.claude/projects/` were checked only with a fake. Before a real run, the pilot checks that
  `claude --help` lists every flag the arms use, and refuses if one is missing.
