# SPEC-014: supervised lanes for dynamic workflows

Status: **proposed** in the pull request that adds it. Merging that pull request, with a person's approving review, accepts the plan and the decisions listed under "Decisions this spec asks for". Each delivery slice below is still its own pull request with its own approval. A slice marked *owner gate* needs a person's decision before the agent starts it.

Related: [SPEC-001](control-plane.md) section D (delegation), [the Symphony decision](symphony-decision.md) (RA-SYM-002, RA-SYM-003, RA-SYM-007), [SPEC-008](agent-agnostic-board.md) (the board is a store, not a scheduler), [SPEC-012](effectiveness-proof.md) (how a treatment is measured).

## The request

The owner asked (2026-11-26) that Boundroom's lanes supervise dynamic workflows, and that the number of lanes scale with demand. Asked which lanes, the owner chose both:

- **Live lanes.** The supervisor runs several slices at once, each in its own lane, and the number of lanes follows the work that is ready. A dynamic workflow can drive it, and can split its own work into lanes the supervisor checks.
- **A benchmark lane.** The effectiveness benchmark measures that against today's one-slice-at-a-time supervisor.

What this does and doesn't mean for a dynamic workflow, plainly:

- The supervisor supervises at the **lane boundary**. It decides what may run, gives each lane a fresh runner in its own worktree, and judges what comes back. It doesn't watch the inside of a Claude Code Workflow script, and it can't.
- A workflow either **calls** the supervisor (`run_lanes`), or, inside a slice that allows it, **hands the supervisor a plan** that the supervisor checks and runs as child lanes. In both cases the workflow stops picking work and starting agents itself.
- A runner can still launch its own sub-agents inside one lane (agent-native delegation). The supervisor doesn't see those, as today, and they are bound by that lane's scope and gates.

## Terms

"Lane" already names the benchmark's treatment arms (A to E). In this spec, **lane** alone means the live one below, and the benchmark's arms are always called **benchmark lanes**.

- **Dynamic workflow.** A parent agent's orchestration that decides at run time what to split off and to how many sub-agents: a Claude Code Workflow script, Hermes delegation, or a plain script.
- **Lane.** One attempt at one slice. It has its own Git worktree, its own runner process, its own handoff, its own base and head commits, and its own verdict. Today `run_next` runs one lane at a time, in the repository's own worktree.
- **Lane controller.** The new command `automation/supervisor/run_lanes.py`. It is the only process that writes the queue, starts runners and integrates their commits. It runs in the foreground, waits on its runner processes, and returns when nothing is left to admit and no lane is running. It is not a daemon, and it doesn't poll the queue.
- **Child lane.** A lane the controller runs for one slice, from that slice's lane plan.

## What stays true

Each slice below tests the rules it touches.

1. **One supervised run per user per host.** The controller holds `runner_profile.run_lock` for the whole run, as `run_next` does. Runners run under it, so no runner can start `run_next` or `run_lanes` (SPEC-008 PS-20).
2. **Only the supervisor continues.** No runner, sub-agent or workflow starts a slice, admits a lane or marks one done.
3. **Every lane gets every gate.** Each lane is judged by the same `judge_step` and `policy.evaluate_completion` as `run_next`, in its own worktree: scope, file budget, proof, handoff and validation replay. A lane's verdict never reads another lane's handoff.
4. **The queue stays the authority.** No new queue or task database. Child lanes aren't queue entries. Their plan, handoffs and verdicts are attempt evidence, kept in the parent's handoff directory beside the parent's handoff, as handoffs are today. Merging this spec is the explicit design decision AGENTS.md requires for that.
5. **`run_next` is unchanged.** Its flags, exit codes and decision report stay as the [compatibility policy](../reference/compatibility.md) fixes them.
6. **One lane is today's behavior.** With a ceiling of 1, `run_lanes` produces the same queue writes, decision reports and exit codes as `run_next` over the same queue, except for the fields that name the worktree. RA-LANE-004 proves that with golden outputs before a second lane can start.
7. **No judged commit is rewritten.** The commits a lane was judged on are the commits that land, with the same SHAs, so a handoff's `verified_commit_sha` and proof commits stay reachable from the branch.

## How many lanes run

The controller recomputes the lane count each time a lane starts or ends. The rule is a pure function, `lane_admission.admit(queue, running, held, limits)`, with no clock, environment, Git or file access, so every case is an output-based test.

```
lanes wanted = min(ceiling,
                   slices admissible now,
                   slices left in this run's autonomous budget)
```

- **Ceiling.** The queue's new `policy.max_lanes`, default 1. `--max-lanes` may lower it but never raise it, the same refusal of privilege widening the runner profile uses. Raising it above 1 in a repository is a reviewed change to that repository's queue (*owner gate*). The schema caps it at 8.
- **Eligible.** `policy.select_next_slice` returns one slice, so admission needs a predicate per slice: queued, dependencies `done`, and passing `policy.next_slice_eligibility`. A test proves that the first slice the predicate admits is always the slice `select_next_slice` returns.
- **Admissible.** An eligible slice is admissible when its `allowed_paths` overlap neither a running lane's nor a **held** slice's. Two prefixes overlap when either contains the other after `policy.normalize_repo_path`, by the rule `policy.path_matches_prefix` uses. An empty list or `.` claims the whole repository.
- **No starving.** A slice that is eligible but blocked by an overlap is *held*, and it reserves its paths: no slice after it in queue order may start on paths it needs. A slice that claims the whole repository therefore lets running lanes drain, then runs alone.
- **Exclusive slices.** Paths aren't the only thing slices share. Fixed ports, `/tmp` files, caches, containers and databases collide too, and the overlap rule can't see them. A slice marked with the new optional `"exclusive": true` runs alone. A repository whose validations share such resources marks those slices, or keeps `max_lanes` at 1.
- **Budget.** `consecutive_autonomous_limit` counts every lane started, child lanes included.
- **Stopping.** A lane that ends `failed` or `blocked`, or with `stop_for_review` (the run limit, or a handoff recommending a slice other than the queue's next), closes admission. Lanes already running finish and are judged. This is where several lanes differ from `run_next`: a `done` lane that finishes after the stop is still integrated, because its verdict depends on nothing the stopped lane did. Decision 4 below asks the owner to confirm that.

`run_lanes --dry-run` prints what it would admit, which slices are held and by what, and the worst-case session time: each admitted lane's timeout, plus `max_children` × `child_timeout_seconds` for a slice that delegates.

**How often this helps.** Only when slices are ready together and touch different paths. The example queue is one dependency chain, so it never runs more than one lane. RA-LANE-003's pull request reports, for the example queue and for any consumer queue the owner names, how many lanes each step of the queue would have admitted.

Rejected for now: scaling by host load, and lowering the ceiling after a provider rate limit. Boundroom can't tell a rate limit from other runner failures today.

## A lane's workspace

This builds on RA-SYM-002 (per-slice workspaces), which has not shipped. RA-LANE-002 delivers it, one workspace at a time first.

- Each lane runs in `git worktree add -b boundroom/lane/<slice_id>/<attempt_id> <lane-root>/<attempt_id> <integration head>`. The lane root defaults to `$XDG_STATE_HOME/boundroom/lanes/<repository id>/`, outside every worktree of the repository. The controller resolves canonical paths and refuses a lane root inside the repository or reached through a symlink.
- **Provisioning.** A fresh worktree has no virtual environment, `node_modules` or build output, and a bounded runner has no network. Before the runner starts, the controller runs the queue's new `policy.lane_setup` commands in the worktree, outside the runner's sandbox. Those commands are part of the reviewed queue, like `agent_command_template`. RA-LANE-002's test shows the example validations passing in a fresh lane.
- The runner's working directory is the lane's worktree, and its prompt is the one `run_next` renders, with that worktree's paths. Its runner profile is built against that worktree, so its sandboxed tools can write only there.
- What the sandbox doesn't cover is unchanged from today, and the spec doesn't claim more. `git add` and `git commit` run outside the sandbox, as they do now, and commit hooks run with them. The `.git` directory is shared by every worktree, so a commit can write any ref. The controller records each lane's base and head commits itself and integrates those commits, never a branch name.
- **No push from a lane.** A lane's profile leaves out the `git push` allow rule and its sandbox exception. A lane branch has no upstream, but `push.autoSetupRemote` or `push.default=current` would still push it with the person's credentials.
- **A lane ends clean.** The judged tree must be the shipped tree. A lane whose worktree has uncommitted changes when its runner exits, apart from its handoff, fails with the new reason `lane_uncommitted_changes`.
- A worktree is removed only after its slice is `done` and integrated. Anything else keeps it, and the decision report names its path.
- The controller refuses to start if the repository's own worktree has changes outside supervisor-owned paths, the same allowance `plan_step` makes, because integration moves that worktree's branch.

## Integration

Lanes start from the same integration head and finish at different times. The controller integrates finished lanes one at a time, in the order they finish:

1. A verdict other than `done` is recorded as `run_next` would record it, and the lane isn't integrated.
2. If the integration head hasn't moved since the lane started, the branch is fast-forwarded to the lane's head. That is always the case with one lane, so with one lane nothing else in this list runs, and the branch ends where `run_next` would leave it.
3. Otherwise the controller merges the lane's recorded head into the integration branch with a merge commit it authors (`--no-ff`). The lane's own commits keep their SHAs (invariant 7). A conflict fails the slice with the new reason `lane_integration_conflict`, and nothing is merged.
4. At the merge commit, the controller runs the queue's new `policy.integration_validations`, an allowlist the repository's owner reviews, such as its test suite. A failure fails the slice with `lane_integration_validation`, and the merge is undone. The validations the supervisor replays today (`make architecture` and `git diff --check`) don't run tests, so without this list two slices that pass alone could land broken together. A queue that leaves the list empty is refused for `max_lanes` above 1.
5. Only then is the slice marked `done`.

The branch the lanes land on is the repository's own branch, and it reaches `main` the way that repository's changes do today. In Boundroom and any repository with the same rules, that is a pull request with required checks and a person's approval, so CI stays the final integration gate. Decision 5 asks whether each lane should instead get its own pull request.

The three new reasons are additions to the failure reasons in [the exit code reference](../reference/exit-codes.md), which the compatibility policy allows in a minor release.

## Recovery

`run_next` refuses to start while any slice is `in_progress`, and a person resets it by hand. With several lanes, one interrupted run can leave several slices `in_progress`, several worktrees, and a slice whose code landed before its status was written. So RA-SYM-003 (durable attempts and reconciliation) is a prerequisite, and lane state is recoverable from Git:

- Each lane's attempt record holds its slice, base, recorded head and worktree, and is written before the runner starts.
- `run_lanes --reconcile` reads those records. A slice whose recorded head is already an ancestor of the integration branch is marked `done`. Every other `in_progress` lane is marked `failed` with `supervisor_error`, and its worktree is kept.
- `run_lanes` refuses to start while an unreconciled attempt exists, as `run_next` refuses today. `KeyboardInterrupt` is handled: runners are stopped by process group, and their slices are recorded as failed.

## Board support

The first version works with the JSON queue only. Board claims can't be renewed today and expire after `handoff_timeout_seconds` plus a margin. A lane that waits for integration, or a slice that runs children, would outlive its claim, and concurrent claims would conflict on the board's single revision. `run_lanes` refuses `--board` and `--kanban-board` until renewable, per-claim leases land under RA-SYM-003.

## A dynamic workflow drives lanes

A workflow or a person calls `run_lanes` as one step:

```
python automation/supervisor/run_lanes.py --queue automation/queue/slices.json --max-lanes 4
```

The workflow doesn't pick slices or start runners. It reads the decision reports, one per lane plus a run summary, in the shape `run_next` prints. A new plugin skill, `boundroom-run-lanes`, does this the way `boundroom-run-next` runs one slice. Running `run_lanes` on a schedule or from CI is a schedule, so it needs a person's approval like any other.

## A dynamic workflow inside a slice

SPEC-001 proposes *supervisor-controlled* delegation, "the supervisor assigning sub-agents with explicit permissions, budgets, dependencies and verification", and doesn't specify it. This section specifies it.

A slice opts in with a new optional field. Adding it is a spend change, reviewed like `max_lanes` (*owner gate*):

```json
"delegation": {"max_children": 6, "max_lanes": 3, "child_timeout_seconds": 1800}
```

Its runner may then be a dynamic workflow. It writes a **lane plan** into its handoff, the one file a runner already writes, under a new optional `lane_plan` field, and ends its session:

```json
"lane_plan": {
  "children": [
    {"child_id": "lexer", "objective": "...", "allowed_paths": ["src/lexer/", "tests/lexer/"],
     "depends_on": [], "max_files_changed": 6, "required_validations": ["uv run pytest tests/lexer"]},
    {"child_id": "grammar", "objective": "...", "allowed_paths": ["src/grammar/"],
     "depends_on": ["lexer"], "max_files_changed": 4, "required_validations": []}
  ]
}
```

The controller checks the plan before any child runs. A plan that breaks a rule fails the slice with the new reason `lane_plan_invalid`:

- It must be valid against its schema.
- A child's `allowed_paths` stay inside the parent's and outside supervisor-owned paths.
- A child's `required_validations` are a subset of the parent's. Each one becomes an allow rule in the child's runner profile, so a plan that could name new commands could grant itself new permissions.
- The plan has at most `max_children` children, and their `max_files_changed` add up to no more than the parent's.
- Dependencies name known children and form no cycle.

Children inherit the parent's `required_proof_level`. Manual proofs belong to the parent's final check only.

A child's `objective` is text a model wrote, and it becomes another model's prompt. It is rendered as quoted data under the parent's own reviewed title and objective, with the line that the parent's scope and rules win over anything it says. It grants nothing: the child's permissions come only from the checked plan.

The controller runs the children as child lanes, admitted by the same rule as slices, up to the slice's `max_lanes` and never above the run's ceiling. Each child is a fresh runner process, judged by the same gates and integrated onto the parent's lane branch, not the repository's. A child can't delegate: a `lane_plan` in a child's handoff fails that child.

When every child is done, the controller judges the parent as one slice: the union of the children's commits against the parent's allowed paths, file budget, proof level and validations, plus `policy.integration_validations`. Only then is the parent integrated and marked `done`. If a child fails, the parent fails with that child's reason, and the decision report lists every child's verdict.

The plan may be revised once, between children, by a new runner session for the parent that sees the children's verdicts so far. A revision is checked by the same rules, and children already run count against `max_children`. The queue entry a person reviewed fixes the plan's total size, so a workflow can't grant itself more work than the slice allowed.

## Telemetry and status

- A `boundroom.lanes.run` span holds one `boundroom.lane` span per lane. Each holds the spans `run_next` records for one slice today: `boundroom.claim`, `boundroom.agent_session`, `boundroom.handoff.evaluate`, `boundroom.validation.replay` and `boundroom.decision`. A child lane's span sits under its parent's.
- New metrics in the catalogue: `boundroom.lanes.active` (a gauge) and `boundroom.lanes.admission` (a counter labelled `admitted`, `overlap`, `held`, `exclusive`, `ceiling` or `budget`).
- `operator_status.py` lists running lanes, their worktrees, held slices and unreconciled attempts. It stays read-only.

## The benchmark lanes

Two benchmark lanes measure delegation. Each starts with one planning session that writes a lane plan for the instance:

- **F**: the children run as parallel supervised lanes.
- **G**: the same plan, with the children run one after another. F against G separates the effect of running children in parallel from the effect of splitting the work at all.

Benchmark lane B, one bounded slice, stays the control.

- **Isolation.** The benchmark uses the pure `lane_admission` rule and the plan checks, with its own workspaces under the run directory. It doesn't run the live `run_lanes` command or take the live run lock, so benchmark runs stay apart from live control-plane integrations, as AGENTS.md requires.
- **Measured.** ProgramBench correctness stays the primary metric. F and G also record true wall time (not the sum of phase times that `wall_clock_seconds` holds today), total session time, how often the plan was valid, and how often children were held back by overlaps. ProgramBench builds a program from scratch, so a planner has to guess paths before the code exists. A low plan-valid rate is a finding, not noise.
- **Compute.** F and G get the same wall-clock ceiling as B, and more session time. That is reported, not controlled. A comparison that controls it, B given the same total session time as F, can be added if the first result is worth chasing.
- **Concurrency confound.** Benchmark cells stay one at a time, so provider load can't pass for a lane effect. F's parallel children bring that confound back inside F, which is one more reason G exists.
- Every F or G run is spending, so each needs a person's approval.

## Delivery slices

| Slice | What | Needs |
| --- | --- | --- |
| RA-LANE-001 | This spec | — |
| RA-LANE-002 | RA-SYM-002 delivered: one lane workspace at a time, provisioning, the clean-end check, canonical paths, retention | 001 |
| RA-LANE-003 | `lane_admission`: eligibility per slice, overlap, holding, exclusive slices; output-based tests; the admission count over real queues | 001 |
| RA-LANE-004 | RA-SYM-003 for the JSON queue: attempt records, `--reconcile`, interrupt handling | 002 |
| RA-LANE-005 | `run_lanes` with a ceiling of 1, matching `run_next`'s golden outputs; `max_lanes`, `exclusive`, `lane_setup` and `integration_validations` in the schema | 003, 004 |
| RA-LANE-006 | Several lanes: merge integration, integration validations, the new failure reasons | 005 |
| RA-LANE-007 | Lane plans: `delegation`, `lane_plan`, plan checks, child lanes, the parent's final check, the one revision | 006 |
| RA-LANE-008 | The `boundroom-run-lanes` skill and a Claude Code Workflow recipe in the workflow guide | 006 |
| RA-LANE-009 | Telemetry spans and metrics, and lanes in operator status | 006 |
| RA-LANE-010 | Benchmark lanes F and G | 003, 007 |
| RA-LANE-011 | *Owner gate:* a canary with `max_lanes` of 2 in a disposable repository, then a consumer's own pull request raising its ceiling | 006, 009 |
| RA-LANE-012 | *Owner gate:* the first F and G benchmark run | 010 |

Merging RA-LANE-002 to 010 changes nothing in any repository until its queue raises `max_lanes` or adds `delegation`. When RA-LANE-007 lands, SPEC-001 section D, the roadmap and AGENTS.md change "supervisor-controlled delegation is proposed and not implemented" to say what is implemented.

## Performance

`run_lanes` adds work per slice that `run_next` doesn't: a `git worktree add` and removal, the `lane_setup` commands, and, with more than one lane, a merge and the integration validations. Those are subprocess calls, and the setup and integration validations can cost as much as a test run. RA-LANE-006's pull request measures them on a fixture queue. `run_next` gains no work.

## Decisions this spec asks for

Merging this spec records these decisions. A reviewer who disagrees with one should say so in review.

1. **Ordering.** AGENTS.md puts the P0 control-plane gate before expanding the supervisor. Benchmark lanes F and G are exempt, but live lanes are a supervisor expansion. Merging this spec puts RA-LANE-002 to 009 ahead of the open P0 work, at the owner's request of 2026-11-26.
2. **Concurrency.** This reverses the Symphony decision's "Remote workers, concurrency limits: **Reject now**" for local concurrency only. Remote workers stay rejected.
3. **Delegation.** Supervisor-controlled delegation as specified above, with child lanes as attempt evidence, not queue entries.
4. **After a stop.** A `done` lane that finishes after another lane stopped the run is still integrated (proposed). The alternative is to keep it unintegrated and leave its slice for a person.
5. **How lanes reach `main`.** On the repository's own branch, through that repository's usual pull request (proposed). The alternative is one pull request per lane from `boundroom-agent[bot]`, which gives a separate approval and CI run per slice but needs the bot's push access in every repository that uses lanes.

## Open questions for the owner

1. The example queue's `max_lanes`: keep it at 1 (proposed), so a reader opts in deliberately.
2. The default lane root under `$XDG_STATE_HOME`, or a path the queue names.
3. One revision of a lane plan per parent (proposed), none, or a number the slice sets.
