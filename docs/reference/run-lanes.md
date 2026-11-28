# Running slices in lanes

`automation/supervisor/run_lanes.py` runs queued slices the way `run_next.py` does, with each slice in its own Git worktree, a **lane** ([SPEC-014](../architecture/supervised-lanes.md)). This version runs **one lane at a time** and works with the **JSON queue only**. Several lanes at once come with RA-LANE-006.

With one lane, `run_lanes` makes the same queue writes, prints the same decision reports and exits with the same codes as `run_next` over the same queue. Only the fields that name the worktree differ (SPEC-014 invariant 6). `automation/tests/test_run_lanes_parity.py` checks that on a corpus of queues, from a clean `done` to a refusal. The [differences](#how-it-differs-from-run_next) below are the ones the spec asks for.

```
python automation/supervisor/run_lanes.py --queue automation/queue/slices.json --single-slice
python automation/supervisor/run_lanes.py --queue automation/queue/slices.json --dry-run
python automation/supervisor/run_lanes.py --queue automation/queue/slices.json --reconcile
```

## Flags

`run_lanes` takes every `run_next` flag, with the same meaning, and two more. The [settings reference](settings.md) lists them all.

| Flag | What it does |
| --- | --- |
| `--max-lanes N` | Lowers the queue's `policy.max_lanes` for this run. It can't raise it: a value above the queue's exits 78, and one below 1 exits 64. This version runs one lane whatever the ceiling, and says so on stderr when more was asked for. |
| `--reconcile` | Resolves the slices an interrupted run left `in_progress`, from the lane attempt records, then stops without running a slice. With `--dry-run`, it prints what it would record and changes nothing. |
| `--board`, `--kanban-board` | Refused with exit 2. Board claims can't be renewed per lane yet (SPEC-014 "Board support"). |

## What a lane does

1. **Before anything**, `run_lanes` refuses a run inside a supervised slice and a board, takes the run lock, and refuses to start while a slice is `in_progress` or a lane attempt record is open or unreadable. It refuses a detached `HEAD`, because a lane lands on a branch.
2. **It picks the slice** with the lane admission rule. With no lane running, that is the slice `run_next` would pick.
3. **Before the claim**, it refuses a lane worktree kept from an earlier slice, and a repository worktree with changes outside the supervisor's own paths. Then it writes the attempt record.
4. **After the claim**, it makes the lane's worktree from the commit `run_next` would start at, on a branch `boundroom/lane/<slice id>/<attempt id>`, and runs the queue's `policy.lane_setup` commands in it, outside the runner's sandbox.
5. **The runner runs in the lane**, in its own process group, with the prompt, runner profile and paths `run_next` would give it in that worktree. The slice is measured, replayed and judged there by `run_next`'s own code.
6. **A `done` verdict lands.** If the lane left nothing uncommitted apart from its handoff, the repository's branch is fast-forwarded to the lane's judged head. The branch ends where `run_next` would leave it, at the same commits. Then the queue records `done`, the attempt record is closed, the handoff is copied to the repository's handoff directory, where `run_next` leaves it, and the lane's worktree and branch are removed.
7. **Any other verdict** is recorded as `run_next` records it, and the lane is kept.

Each report about a claimed slice carries a `lane` block naming the attempt, the worktree, its branch and whether the worktree is kept. The decision span's `report_sha256` is the digest of the report without that block.

The lane root, where worktrees and attempt records live, is `$XDG_STATE_HOME/boundroom/lanes/<repository id>/`, or `~/.local/state/boundroom/lanes/<repository id>/`. It must be outside the repository and not reached through a symbolic link.

## How it differs from `run_next`

| Situation | `run_next` | `run_lanes` |
| --- | --- | --- |
| The repository's worktree has a change inside the slice's scope before the run | runs the slice | refuses, exit 2: integration moves the branch |
| The handoff says `done` but the runner left changes uncommitted | records `done` | fails the slice with `lane_uncommitted_changes`: integration takes commits only |
| A slice fails | its commits stay on the repository's branch | its commits stay on the lane's branch, in the kept worktree |
| The worktree a failed slice ran in | is the repository's | is kept, and blocks the next lane until a person removes it |
| `HEAD` is detached | runs | refuses, exit 2 |
| The branch moved while the slice ran, or Git refuses the fast-forward | — | fails the slice with `supervisor_error`; merging is RA-LANE-006 |
| Ctrl-C, `SIGTERM` or `SIGHUP` | the slice stays `in_progress` | the runner's process group is stopped, the slice recorded `failed`, exit 130, 143 or 129. Once integration has started, the slice stays `in_progress` for `--reconcile`, which can tell whether it landed |
| The handoff is committed, then edited again | records the verdict | fails the slice with `lane_uncommitted_changes`: the edit wouldn't land |
| A slice runs past `handoff_timeout_seconds` | its shell is killed | its whole process group is killed |

Telemetry is `run_next`'s: the same spans and metrics, recorded under the entry `run_next`. Lane spans and metrics come with RA-LANE-009.

## Queue fields

These are optional. A queue without them runs as before, under `run_next` and `run_lanes` alike.

| Field | Where | What it does |
| --- | --- | --- |
| `max_lanes` | `policy` | The most lanes `run_lanes` may run at once, 1 to 8, 1 when absent. Raising it above 1 is a reviewed change to the queue. This version runs one lane. |
| `lane_setup` | `policy` | Commands run in a fresh lane worktree before its runner starts, such as installing dependencies. A failing command fails the slice with `supervisor_error` before the runner starts, and so does a command that leaves files Git doesn't ignore: the judged tree must be the tree that lands, so ignore what setup makes. |
| `integration_validations` | `policy` | Commands run at a lane's merge commit before its slice is marked `done`. Merges come with RA-LANE-006, so this version doesn't run them. |
| `exclusive` | each slice | `true` runs the slice in a lane alone, for resources the path overlap rule can't see, such as a fixed port. |

## Recovering from an interrupted run

A run that dies, for example by `kill -9`, can leave a slice `in_progress` and its attempt record open. `run_lanes` then refuses to start and says to reconcile. `run_lanes --reconcile` marks the slice `done` only when its record shows the supervisor's own `done` verdict and that judged head is already on the branch, and removes its worktree. Every other `in_progress` slice becomes `failed` with `supervisor_error`, and its worktree is kept. The JSON report lists each slice it resolved, the worktrees it removed and any it had to keep.

## Exit codes

See [exit codes](exit-codes.md#automationsupervisorrun_lanespy).

## Cost

Each lane adds, to what `run_next` does for a slice: a `git worktree add` and, for a landed slice, a `git worktree remove` and a branch deletion; the `lane_setup` commands; a `git merge --ff-only`; a few `git` calls to check the worktree and the branch; and a few small attempt-record writes, each flushed to disk. The start check lists the attempt records, which a lane root keeps as evidence, so it grows with the number of attempts. `run_next` gains no work.
