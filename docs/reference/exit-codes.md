# Exit codes and errors

Every exit code the supervisor, its helper commands and the sync tool use, what each one means, and what to do. Scripts should branch on these codes and on the JSON reports, not on message text. The [compatibility policy](compatibility.md) says a code never gets a new meaning, even in a minor release.

`automation/tests/test_operator_docs.py` reads each command's source and fails if it can exit with a code this page doesn't list, if this page lists a code it can't exit with, if a command-line entry point has no section here, or if a failure reason or decision below is missing.

## How to read a stop

**A slice that fails its checks is not a non-zero exit.** When `run_next.py` judges a slice, it records the verdict in the decision report and in the queue, then exits 0. That includes `stop_failed`, `stop_blocked` and `stop_for_review`. Read the decision report's `decision` and `reason` to learn what happened to the slice. A non-zero exit means the supervisor refused to start, or could not judge the slice at all.

Some codes come from [sysexits.h](https://man.freebsd.org/cgi/man.cgi?query=sysexits): 64 is a usage error, 66 a missing input, 69 a missing program, 73 an output that can't be created, and 78 a configuration that refuses the run.

Two codes come from Python itself, for every Python command here:

- **2** when the command line is wrong. `argparse` prints the usage and exits 2 before the command does anything.
- **1** with a traceback when the command meets an error it doesn't handle. That is a bug; report it with the traceback.

## `automation/supervisor/run_next.py`

Selects, claims, runs and judges slices.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | The run ended at a stop. It printed a decision report for each slice it judged, whatever the verdict. Also: nothing was eligible, or `--dry-run`. | Read the last decision report's `decision` and `reason`. |
| 2 | Refused before claiming the next slice: a slice already `in_progress`, a dirty path outside the next slice's scope, no agent command, a missing or invalid queue, not a Git repository, a context bundle its configuration refuses, such as a memory recall the grant doesn't allow, a board inside the repository, a `--report-file` inside the repository, a prompt it can't render (see [prompt errors](#prompt-and-rules-errors)), or flags that go together given alone. **Or** a claimed slice failed under the supervisor, and the queue now shows it `failed` with a failure report: see [failure reasons](#failure-reasons). That covers any error from the claim until the verdict is recorded: Git failing while the supervisor measures the slice (`git_error`), and anything else, including a failed write of the verdict to the queue or board (`supervisor_error`). If the queue or board refuses the failure too, the report's `queue_status` is `in_progress`. | Without a failure report, read the `stop:` line and its `hint:`, if any. With one, read its `reason`. See [troubleshooting](troubleshooting.md), and for a report whose `queue_status` is `in_progress`, [a slice stuck in progress](troubleshooting.md#a-slice-stuck-in-progress). |
| 64 | `--single-slice` was given with `--autonomous-limit`, and nothing was claimed. **Or** the agent command exited 64: see the last row. | Pass one or the other. For the agent's 64, see [`run_agent.sh`](#automationsupervisorrun_agentsh). |
| 78 | Refused before claiming anything, by configuration: a run inside a supervised slice, another run holding the run lock, a runner profile the environment doesn't allow, or telemetry that is required but can't be turned on. Nothing in the queue changed. **Or** the agent command exited 78: see the last row. | Read the `stop:` line. For the agent's 78, see [`run_agent.sh`](#automationsupervisorrun_agentsh). |
| Any other | The agent command exited with this code. The slice is marked `failed` with reason `agent_exit_nonzero`, or `agent_timeout` for 124. With the shipped wrapper, the code is one of [`run_agent.sh`'s](#automationsupervisorrun_agentsh). | Read the agent's output above the failure report. |

**64 and 78 have two sources.** `run_next.py` uses them for its own refusals, and passes them through when the agent command exits with them, which `run_agent.sh` does. A printed failure report tells them apart. With one, a slice was claimed and is now `failed`. With only a `stop:` line, nothing was claimed.

**Earlier slices keep their verdicts.** In a run of several slices, any exit, 0 or not, leaves the slices judged earlier in the same run as their decision reports say.

A slice that runs past the policy's `handoff_timeout_seconds` is stopped by the supervisor, marked `failed` with reason `supervisor_error`, and the run exits 2. Exit 124 means the agent command timed itself out, for example under `timeout(1)`.

### Failure reasons

When a claimed slice fails under the supervisor, the failure report's `reason` says why, and the trace's decision span carries one of these codes.

| Reason code | Meaning |
| --- | --- |
| `agent_exit_nonzero` | The agent command exited with a code other than 0 or 124. |
| `agent_timeout` | The agent command exited 124. |
| `supervisor_error` | The supervisor failed while running or judging the slice: the handoff timeout passed, the command could not start, or any other error before the verdict was recorded, such as one in the validation replay, in the judgement of the handoff, or in writing the verdict to the queue or board. |
| `handoff_missing` | The agent exited 0 but wrote no handoff at the expected path. |
| `handoff_invalid` | The handoff can't be read, isn't valid JSON, or doesn't match the handoff schema. |
| `handoff_slice_mismatch` | The handoff names a different slice from the one in progress. |
| `git_error` | Git failed while the supervisor measured the slice after the session: listing dirty paths, listing the slice's commits or reading HEAD. Or HEAD no longer descends from the commit the slice started at. |

### Decisions

Every judged slice gets one decision in its decision report. Only `continue` lets the run go on to the next slice.

| Decision | Queue status | Meaning |
| --- | --- | --- |
| `continue` | `done` | The slice passed, and the next eligible slice may start in this run. |
| `stop` | `done` | The slice passed, and no eligible slice remains. |
| `stop_for_review` | `done` | The slice passed, but policy refused to continue: the autonomous limit was reached, or the handoff recommended a slice that isn't the next eligible one. A person reviews. |
| `stop_blocked` | `blocked` | The handoff reported `blocked`. |
| `stop_failed` | `failed` | The handoff reported `failed`, or the supervisor rejected it: a required validation missing or failed, a replay that failed, a path outside scope, or too many files changed. |

### Prompt and rules errors

These stop `run_next` with exit 2 before it claims a slice, so the slice stays queued and no agent starts. `--dry-run` stops the same way. The message is `stop: ` and then the code.

| Code | Meaning |
| --- | --- |
| `runner_rules_symlink` | `automation/runner-rules.md` is a symbolic link. |
| `runner_rules_not_file` | `automation/runner-rules.md` exists but isn't a regular file. |
| `runner_rules_too_large` | `automation/runner-rules.md` is larger than 32 KiB. |
| `runner_rules_not_utf8` | `automation/runner-rules.md` isn't UTF-8. |
| `unknown_prompt_placeholder` | A prompt fragment uses a `__PLACEHOLDER__` the supervisor doesn't fill. |
| `prompt_fragment_unreadable` | `automation/prompts/base.md` or `automation/prompts/slice.md` is missing, can't be read, or isn't UTF-8. |

## `automation/supervisor/run_agent.sh`

The shipped agent command. It chooses the runner, checks its inputs, then hands over to the runner, whose exit code it returns.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | `--help`, or the runner exited 0. | |
| 1 | `--sandbox-probe` ran and a probe call failed. | Read the probe's JSON lines. |
| 64 | An unknown argument, a missing launch argument, a `BOUNDROOM_AGENT_RUNNER` other than `auto`, `codex`, `claude` or `hermes`, an unknown `BOUNDROOM_HERMES_TRANSPORT`, or a `BOUNDROOM_HERMES_MAX_TURNS` that isn't a positive integer or is set without the `stream-json` transport. | Fix the value the message names. |
| 66 | The repository root isn't a Git checkout, or the prompt or context file is missing. | Run it from `run_next.py`, which creates both. |
| 69 | The runner's command isn't installed, or a Hermes configuration file or interpreter is missing. | Install the runner, or set `BOUNDROOM_CODEX_BIN`, `BOUNDROOM_CLAUDE_BIN` or `BOUNDROOM_HERMES_BIN`. |
| 73 | The handoff file already exists, and the wrapper won't overwrite it. | Move the old handoff away; each run gets a new name. |
| 78 | The requested runner contradicts the agent harness the run was started from, a headless run left the runner as `auto`, a cleanroom workspace path contains a colon, or `--sandbox-probe` has no sandbox image. | See [runner selection](../workflows/boundroom.md#agent-runner-selection). |
| Any other | The runner's own exit code. | Read the runner's output. |

## `Tools/boundroom-sync.sh`

The manifest-owned sync, check and status tool.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | `--sync` finished; `--check` found the target current; `--status` found nothing that needs attention. | |
| 1 | `--check` found drift, including a missing target, or `--status` found something that needs attention. Nothing was written. | Read the listed issues. |
| 2 | Refused, with `boundroom-sync: error:` and the reason, before writing anything: any [import protection](../workflows/boundroom.md#import-protections), an invalid manifest, a source that fails its quality gate, or a missing runtime dependency. Also a wrong command line. | Read the error. The consumer is unchanged. |

## `automation/supervisor/evaluate_handoff.py`

Judges a handoff for a slice finished outside `run_next.py` and prints the report. It changes the queue only without `--dry-run`.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | The slice's queue status is `done`. | |
| 1 | The slice's queue status is `blocked` or `failed`, and the report was printed. **Or**, with a traceback and no report, the command couldn't read an input, such as a missing or invalid handoff or queue, and reached no verdict. | With a report, read its `decision` and `reason`. With a traceback, fix the input it names. |
| 78 | Refused: run without `--dry-run` inside a supervised slice, or telemetry is required but can't be turned on. | |

## `automation/supervisor/operator_status.py`

Reports what the supervisor would do next. Writes nothing.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Printed the report. | |
| 1 | Printed the report, but couldn't read the queue, the board or Git, so it is incomplete. | Read the report's refusals. |

## `automation/supervisor/handoff_summary.py`

Prints a review summary of a handoff and, optionally, the supervisor's report.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Printed the summary. | |
| 2 | An input is unreadable or malformed. | Check the paths, and that `--report` holds one JSON object. |

## `automation/supervisor/settings_view.py`

Shows each setting, its value and where the value came from.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Printed the view, or wrote or checked the settings reference page. | |
| 1 | `--docs --check` found the settings reference page out of date. | Run `settings_view.py --docs`. |
| 64 | `--explain` named a setting that doesn't exist. | `--all` lists them. |

## `automation/supervisor/local_board.py`

The local Kanban board tool.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | The command finished. | |
| 2 | The board refused the command, for example a task that isn't in the state the command needs, or a link that would make a cycle. | Read the `stop:` line. |
| 78 | A command that changes the board was run inside a supervised slice. | Only `list`, `show` and `snapshot` run there. |

## `automation/supervisor/kanban_projection.py`

Projects a board snapshot into the queue's shape.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Printed the projection. | |
| 1 | The overlay, the snapshot or the shadow queue is invalid. | Read the `error:` line. |

## `automation/context/build_context.py`

Builds the bounded context bundle for a slice.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Printed or wrote the bundle. | |
| 2 | A configuration error, such as a missing queue. | Read the `stop:` line and its `hint:`. |

## `automation/context/memory.py`

Exports a granted memory bank to a snapshot.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Wrote the snapshot. | |
| 1 | The memory service is unavailable. | Try again when it is up. |
| 2 | Refused: the memory map is unreadable or doesn't grant the request, a credential is missing or refused, a budget is exceeded, or the snapshot directory isn't usable. | Read the `stop:` line. |

## `automation/context/swarm_wiki.py`

Runs the pinned Swarm Wiki compiler.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | The compiler exited 0. | |
| 2 | Refused: the compiler checkout isn't a clean checkout of the pinned commit, an argument the wrapper owns was passed through, the compiler didn't finish in time, or the interpreter can't run. | Read the `stop:` line. |
| Any other | The compiler's own exit code, also recorded as `exit_code` in the evidence. | Read the compiler's output. |

## Release checks

A person runs these before creating a release tag, and `release.yml` runs them again.

### `Tools/check_plugin_version.py`

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Every plugin manifest and `pyproject.toml` declare the tag's version. | |
| 1 | A manifest is missing, unreadable or declares another version, or `pyproject.toml` has no `[project]` version or another one. | Fix the named file. After bumping `pyproject.toml`, run `uv lock`. |
| 64 | No tag was given. | Pass the tag. |

### `Tools/changelog_section.py`

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Printed the tag's changelog section. | |
| 1 | The section is missing, empty, duplicated or has a malformed heading, or the changelog can't be read. | Fix `CHANGELOG.md`. |
| 64 | No tag was given, or it isn't `vMAJOR.MINOR.PATCH`. | Pass a valid tag. |

### `Tools/release_sbom.py`

Writes the release's CycloneDX SBOM from `uv.lock` with `uv export`: Boundroom's dependencies and extras, without the dev group. `release.yml` runs it.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Wrote the SBOM. It lists exactly the packages uv's requirements export lists, and the tag's version. | |
| 1 | uv couldn't run or failed (including a `uv.lock` that no longer matches `pyproject.toml`), its output wasn't a CycloneDX document, the tag isn't `vMAJOR.MINOR.PATCH`, the project's version isn't the tag's, the packages don't match, or the file couldn't be written. stderr says which. | Fix what stderr names: bump `pyproject.toml` and run `uv lock` for a version mismatch or a stale lock. A package mismatch means uv's preview export changed: don't publish without the SBOM. |
| 64 | The output path or the tag is missing. | Pass both. |

### `Tools/telemetry_sample.py`

Writes the sample metrics the example dashboard is checked against (`automation/examples/telemetry/`).

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Wrote the sample, or with `--check`, the sample is current. | |
| 1 | With `--check`, the sample is stale. Or telemetry didn't start, or reported a problem while recording, and stderr says what. | Run `uv run python Tools/telemetry_sample.py`, or fix what stderr names. |
