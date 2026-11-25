# Track R pilot (SPEC-012)

Track R compares an unbounded Claude Code run (arm U) with a supervised slice (arm S) on the same
brownfield tasks. An independent oracle scores both arms. This directory holds phase R0: the frozen
task set, the driver, the oracle and the pilot runner. R0 runs no model and spends nothing.

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

1. **Each task's scope is filled in blind** (`task_statement`, `allowed_paths`,
   `required_validations`, `max_files_changed`) from the pull request's title and body only, without
   looking at its diff or files, and `scope_provenance` records who did it and when. For the pilot
   the owner chose a blinded agent per task, one that could read only the description. The pilot
   refuses to start until every task has a scope.
2. **The dry run passes:** `python -m automation.benchmark.trackr.pilot --dry-run --out <dir> --cache-dir <dir>`.
   A fake Claude Code applies each human solution. Everything else runs for real.
3. **The pilot is started from a plain terminal**, not from inside a sandboxed shell. Claude Code's
   sandbox must be able to start, because both arms run it fail-closed.

## What the setup holds constant

- **The overlay.** Every clone, in both arms, has `automation/supervisor/`, `automation/schemas/` and
  `automation/context/` replaced by their contents at the overlay commit recorded in
  `tasks.pilot.json` (v0.5.3). The pilot therefore measures today's supervisor, not the one each
  task's base shipped with. The oracle check during selection uses the same overlay.
- **The cap.** Both arms get the same `--max-budget-usd`, 4.50 by default. `run_next` always asks for
  its own default of $5, whatever the caller exports. In arm S, a wrapper (`BOUNDROOM_CLAUDE_BIN`)
  replaces that value with the driver's cap. Each session record keeps the value it replaced, under
  `supervised.supervisor_budget_args`.
- **The environment.** Both arms share one inherited environment. GitHub credentials, the SSH agent,
  Claude Code's markers and every `BOUNDROOM_*` and `GIT_*` variable are removed from it. Both arms
  load only project settings and no MCP server. Managed settings still load, in both arms alike.
- **The clone.** Each clone holds no Git object from after the task's base, so the human solution
  can't be found in it.

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
