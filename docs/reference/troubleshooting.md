# Troubleshooting

What to do when the supervisor or the sync tool stops. Find the message you saw, then read across. The [exit-code reference](exit-codes.md) says what each code means. Message text can change between releases, so scripts should use the codes and the JSON reports.

Before anything else, two read-only commands show the state:

```bash
python3 automation/supervisor/operator_status.py
python3 automation/supervisor/run_next.py --dry-run --include-blocked
```

The first reports the next slice, what would stop a run, the sync pin and the telemetry settings. The second lists blocked and deferred slices with their entry conditions and unblockers.

## Before a slice starts

These stop the run before it claims its next slice. That slice is unchanged, so fix the cause and run again. In a run of several slices, any slice judged earlier in the same run keeps its verdict, and its decision report is printed above the `stop:` line.

| Message | Exit | Cause | What to do |
| --- | --- | --- | --- |
| `stop: queue already has in_progress slice <id>` | 2 | Another run is working on that slice, or an earlier run was killed before it judged the slice. | See [a slice stuck in progress](#a-slice-stuck-in-progress). |
| `stop: repo is dirty outside the next slice scope:` and a list of paths | 2 | Uncommitted changes outside the next slice's `allowed_paths`. | Commit, stash or remove the listed paths. If the slice needs them, change its `allowed_paths` in a reviewed change. |
| `stop: no agent command template configured.` | 2 | No `--agent-cmd` and no `policy.agent_command_template`. | Set one. The shipped command is `automation/supervisor/run_agent.sh`. |
| `stop: queue file not found: <path>` | 2 | The repository has no queue yet. | Follow the `hint:` line: copy `automation/examples/example-slices.json` and edit it. |
| `stop: not a Git repository: <path>` | 2 | The supervisor runs only in a Git working tree. | Follow the `hint:` line. |
| `cannot read HEAD in <path>` | 2 | The repository has no commits. | Commit something first. |
| `git executable not found on PATH` | 2 | Git isn't installed, or isn't on this process's `PATH`. | Install Git, or fix `PATH`. |
| `stop: the board <dir> is inside the repository` | 2 | A Kanban board must live outside the repository it schedules. | Move the board. |
| `stop: --report-file must be outside the repository` | 2 | The report would become part of the slice's changes. | Write it elsewhere. |
| `stop: no eligible queued slice found.` | 0 | Nothing is `queued` with its dependencies `done`. | Run with `--dry-run --include-blocked` to see what is waiting and why. |
| `stop: run_next refused inside a supervised slice (BOUNDROOM_SUPERVISOR_SLICE_ID is set)` | 78 | It was run from inside an agent session, or the variable leaked into your shell. | Run it from a shell outside any session. In a plain shell, `unset BOUNDROOM_SUPERVISOR_SLICE_ID`. |
| `stop: another supervised run holds <path>` | 78 | A run is already going for this user on this machine. | Wait for it. The lock is released when that process exits, however it exits. |
| `stop: the unbounded profile needs BOUNDROOM_CLAUDE_ISOLATED=1 ...` | 78 | `unbounded` was asked for without declaring isolation or raising the ceiling. | Use `bounded` or `edits`, or run in a container or VM and set `BOUNDROOM_CLAUDE_ISOLATED=1`. See [runner permissions](security-model.md#runner-permissions). |
| `stop: the parent Claude Code session is in plan mode ...` | 78 | Plan mode means no edits, so no slice can run. | Leave plan mode, or run from a shell. |
| `stop: unknown Claude runner profile ...` or `... ceiling ...` | 78 | A misspelled profile or ceiling. | Use one the message lists. |
| `stop: telemetry: required, but turned off by ...` | 78 | `--telemetry-required` with telemetry off, for example by `BOUNDROOM_OFFLINE` or a repository block. | Turn telemetry on, or drop the requirement. |
| `stop: telemetry: the endpoint at <host> didn't answer` | 78 | Required OTLP telemetry can't reach its endpoint. | Check the endpoint, or drop the requirement. Without it, the run continues with telemetry off. |
| `stop: telemetry: file_dir <dir> isn't writable` | 78 | Required file telemetry can't write its directory. | Fix the directory's permissions. |
| `stop: --single-slice runs one slice and refuses --autonomous-limit.` | 64 | The two flags contradict each other. | Pass one. |

A warning that names a `REPO_AUTOMATION_*` or `OWLORY_*` variable doesn't stop the run. That name stops working in v0.5.0. The [upgrade guide](../workflows/upgrading.md) gives its replacement.

## During a slice

These happen after the slice is claimed. The supervisor marks the slice `failed`, prints a failure report, and exits as shown. To try again, fix the cause, then set the slice back to `queued` in a reviewed change to the queue.

| Failure reason, or what you see | Exit | Cause | What to do |
| --- | --- | --- | --- |
| `agent_exit_nonzero`, with `Agent command exited with code 69` | 69 | The runner isn't installed, or a Hermes file is missing. | Install it, or set `BOUNDROOM_CLAUDE_BIN`, `BOUNDROOM_CODEX_BIN` or `BOUNDROOM_HERMES_BIN`. |
| `agent_exit_nonzero`, with `Refusing BOUNDROOM_AGENT_RUNNER=<x> inside <harness>` | 78 | The requested runner isn't the harness the run started from. | Unset `BOUNDROOM_AGENT_RUNNER`, or run outside any harness. See [runner selection](../workflows/boundroom.md#runner-selection-outcomes). |
| `agent_exit_nonzero`, with `No enclosing agent harness` | 78 | A headless run left the runner as `auto`. | Set `BOUNDROOM_AGENT_RUNNER` to `claude`, `codex` or `hermes`. |
| `agent_exit_nonzero`, any other code | the runner's | The runner itself failed. | Read the runner's output above the failure report. |
| `agent_timeout` | 124 | The agent command timed itself out. | Split the slice, or give the command more time. |
| `supervisor_error`, with a timeout | 2 | The session ran past `policy.handoff_timeout_seconds`. | Split the slice, or raise the timeout in a reviewed change. |
| `supervisor_error`, with `runner_rules_...` | 2 | `automation/runner-rules.md` is a symbolic link, not a file, over 32 KiB or not UTF-8. | Make it a regular UTF-8 file under 32 KiB. |
| `supervisor_error`, with `unknown_prompt_placeholder` | 2 | A customized prompt fragment uses a placeholder the supervisor doesn't fill. | See [customizing prompt fragments](../workflows/boundroom.md#customizing-prompt-fragments). |
| `handoff_missing` | 2 | The session ended without writing its handoff, for example because it reached its budget or turn limit. | Read the runner's output. Check that the prompt names the handoff path. |
| `handoff_invalid` | 2 | The handoff isn't valid JSON or doesn't match the schema. | Check it against `automation/schemas/handoff.schema.json`. |
| `handoff_slice_mismatch` | 2 | The handoff names another slice. | Check the runner's prompt and the handoff. |
| `git_error`, with `HEAD no longer descends from the commit the slice started at` | 2 | The session reset, rebased or switched branches, which hides what it changed. | Inspect the branch. Runners must add commits, not rewrite them. |
| `git_error`, with `Git error after slice <id> ran` | 2 | Git failed while the supervisor measured the slice after the session: listing dirty paths, listing its commits or reading HEAD. | Run `git status` in the repository and fix what it reports, such as a lock file left by a killed Git process. |
| `supervisor_error`, with `Supervisor error while evaluating slice` | 2 | The supervisor met an error while it replayed validations or judged the handoff. | Read the error in the reason. If it is a supervisor bug, report it with the failure report. |

## After a slice is judged

The run exits 0. The decision report's `decision` and `reason` say what happened.

| Decision | What to do |
| --- | --- |
| `stop_failed`, with missing or failed validations | Read `required_validation_failures` and the replays. Run the command yourself, fix the cause, and requeue the slice in a reviewed change. |
| `stop_failed`, with paths outside scope | Read `dirty_paths_outside_scope` and `files_touched_outside_scope`. Either the slice's `allowed_paths` were too narrow, or the session strayed. Revert the stray change, or widen the scope in a reviewed change. |
| `stop_failed`, with too many files changed | The slice is bigger than `max_files_changed`. Split it. |
| `stop_failed`, with missing manual proofs | Read `missing_manual_proofs`. A proof must exist under the proof root and name the slice and the commit the supervisor observed. |
| `stop_blocked` | The session reported it couldn't proceed. Read its handoff, then do the unblocker it names, or queue one. |
| `stop_for_review` | Review the slice, as the reason says: the autonomous limit was reached, or the handoff recommended something other than the next eligible slice. Then run again. |

`handoff_summary.py --handoff <file> --report <report>` prints a short summary that keeps the session's claims apart from the supervisor's evidence.

## A slice stuck in progress

A slice stays `in_progress` when a run stops before it judges the slice: the process is killed or its machine stops. An error the supervisor meets after the claim doesn't do this: it marks the slice `failed` and prints a failure report. The supervisor has no reset command, on purpose: deciding what happened to half-finished work is a person's call.

1. Make sure no run is going: look for a `run_next.py` process, for example with `pgrep -fl run_next.py`. A dry run doesn't take the run lock, so it can't tell you.
2. Look at what the slice left: its commits since it started, any handoff in `automation/handoffs/`, and the working tree.
3. Decide. To retry, revert or keep its partial work, then set the slice back to `queued`. To give up, set it to `failed` with a note. Make the change in a commit a person reviews.

On a local Kanban board, a claim has a time limit. Any write to the board releases expired claims, and `local_board.py expire` releases them without changing anything else.

## Sync

The sync tool exits 2 and prints `boundroom-sync: error:` with the reason. It changed nothing.

| Message starts with | Cause | What to do |
| --- | --- | --- |
| `refusing to import over locally modified files` | A file the sync would replace has uncommitted changes in the consumer. | Commit, stash or revert them. |
| `refusing to import into ...` | The target is the canonical repository, inside it, contains it, or is another checkout of it. | Point `--target` at the consumer. |
| `refusing to import without --pin` | `--sync` needs the full commit to import. | Pass `--pin <full-sha>`, from a release tag. |
| `source repository identity mismatch` or `... cannot be established` | The source's `origin` isn't `--expect-remote`, or it has none. | Sync from a clone of the canonical repository. |
| `canonical source has uncommitted changes` or `source is not at the pinned commit` | The source checkout isn't clean at the pin. | Check out the pinned commit in a clean clone. |
| `canonical source failed its own quality gate` | The source doesn't pass `make reusable-check`. | Sync from a released tag, which passed it. |
| `missing_runtime_dependency: ...` | The consumer's Python lacks a package the synced code needs. | Install it, or pass `--runtime-python` to name the right interpreter. |
| `forbidden import destination ...` or `forbidden source ...` | The manifest names a place the sync may never write, or a file it never ships. | Use the release's own manifest. |
| `manifest ...` | The manifest is missing or invalid. | Use the release's own manifest. |

`--check` exits 1 with `result: drift found` when the consumer's copy doesn't match its pin. Run the sync from the pinned release and deliver it as a pull request. A consumer whose pre-push hook runs the same check, as Owlory's does, can't push until the drift is fixed. `--status` exits 1 with `result: attention needed` and says what needs it.

## Reporting a problem

Include the exit code, the `stop:` or `error:` line, the decision or failure report, and the output of `operator_status.py --json`, which never shows a telemetry endpoint or header. Leave out handoffs and prompts that hold your repository's content unless you mean to share it. Report a suspected vulnerability privately, as [`SECURITY.md`](https://github.com/raelldottin/boundroom/blob/main/SECURITY.md) describes. Other issues go to [the issue tracker](https://github.com/raelldottin/boundroom/issues).
