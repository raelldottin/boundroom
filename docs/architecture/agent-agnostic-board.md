# SPEC-004: A Kanban board that works with any parent agent

Status: **accepted (raelldottin/boundroom#181).** It amends [SPEC-001](control-plane.md) contract A. RA-BD-001 (the interface), RA-BD-002 (the local board, `automation/supervisor/local_board.py`) and RA-BD-003 (`run_next` and the plugin on a local board) are implemented. The RA-BD-004 Claude Code canary passed on 2026-11-10 ([record](#ra-bd-004-claude-code-canary-2026-11-10)). RA-BD-004 is accepted on that canary. The Codex canary is deferred: Codex is not approved on the only machine available (see the record). RA-BD-005 (the Wiki) remains.
Owner decision recorded here (2026-11-10): the Kanban board must work with any parent agent process, such as Claude Code, Codex, Hermes or a plain shell, **with no Hermes dependency**. This spec proposes how to meet that requirement. Implementation waits for it to be accepted.  
Second owner decision (2026-11-10): **keep Hermes as an optional backend.** The local backend is the default, Hermes Kanban stays available to consumers that choose it, and no Hermes board has to be migrated. This settles open decisions 1 and 4. Third owner decision (2026-11-10): **an auditable approval is enough for now**, which settles decision 3.
Authority: until the cutover described below lands, the JSON queue (`automation/queue/slices.json`) stays the operational authority, and the Hermes adapters stay as they are.

## Problem

The parent agent can already be anything. `run_agent.sh` launches the harness the call came from, so Claude Code starts Claude Code, and the plugin's skills pass the Kanban flags through from any harness. The board can't be anything:

| Board operation | Today | Hermes coupling |
| --- | --- | --- |
| Read | `kanban_projection.read_board` opens `<board>/kanban.db` read-only | Hermes's unversioned SQLite schema (`tasks`, `task_links`, `task_runs`, `task_events`) and `boards/<slug>/` layout |
| Claim, record | `kanban_writer` runs `hermes kanban --board <slug> …` | A `hermes` binary on `PATH`, Hermes's `expected_run_id` environment variables, and its claim lock tied to `host:pid` |
| Approve, unblock, add, link | A person in Hermes's UI or CLI | Hermes |
| Wiki | The pinned Swarm Wiki compiler reads `kanban.db` | Hermes's schema |

So a Claude Code parent can use Kanban mode only on a machine with Hermes installed. Every claim is a Hermes CLI subprocess, and the board's schema can change under the adapter without notice, because it is unversioned.

## Decision (proposed)

1. **One board interface.** The supervisor talks to a board only through a `BoardBackend` protocol. The projection, the approval overlay and the supervisor's rules don't know which backend is behind it.
2. **A local backend as the default board.** Boundroom ships a small board store: a versioned SQLite file, accessed with the standard library only, plus a command-line tool for people and agents. It needs no Hermes, no daemon and no network.
3. **Hermes becomes an optional backend.** The existing adapters move behind the interface unchanged. Hermes is no longer the target named by SPEC-001.
4. **Exactly one canonical board per consumer.** A consumer picks one backend. Nothing mirrors or dual-writes between the local board, a Hermes board and the JSON queue. Board mode already ignores `--queue`, and that stays.

The local board is a **store, not a scheduler**. It has no dispatcher, no worker pool and no background process. Only `run_next` starts work, under the same rules as today. SPEC-001's rule "do not build a second scheduler or board database" is amended for this one store: this design replaces Hermes as the board, so it is not a second one.

## The interface

```python
class BoardBackend(Protocol):
    slug: str

    def snapshot(self) -> BoardSnapshot: ...  # tasks, links, creators, revision; read-only
    def claim(self, task_id: str, expected_revision: str, ttl_seconds: int) -> Claim: ...
    def renew(self, held: Claim, ttl_seconds: int) -> Claim: ...
    def record(self, held: Claim, verdict: str, reason: str, handoff_path: Path | None) -> bool: ...
```

- `BoardSnapshot` and the revision keep their current meaning: the SHA-256 of what the projection reads. Every backend builds its snapshot with `kanban_projection.build_snapshot`, so `project` and `load_overlay` take a snapshot from any backend.
- **Hermes can't renew.** `HermesBackend.renew` always refuses, because a Hermes CLI claim is locked to a process that has already exited.
- **Every write is conditional.** `claim` refuses a stale revision, a task that isn't `ready` or one with an assignee. `record` and `renew` refuse a run that is no longer the task's current run. A refusal raises, and nothing is retried or guessed. These are the semantics [the write-back contract](kanban-cutover.md) already defines.
- **The supervisor never writes `done`.** A `done` verdict becomes `review`. Only a person's approval moves a task to `done`.
- **Slice ids say which backend they came from:** `kanban.<slug>.<task>` stays for Hermes, so existing handoffs still parse, and the local backend uses `board.<slug>.<task>`. The overlay gains a `backend` field (`local` or `hermes`), and every backend has a `kind`. `run_next` takes the backend from the overlay, `--board-backend` must agree with it, and `KanbanQueue` refuses an overlay for another kind of board. An overlay without the field means `hermes`, because every overlay written before the local board was for a Hermes board; an overlay for a local board must say `"backend": "local"`.
- **Contract tests run against every backend.** One parameterized suite runs against the local backend and the Hermes stand-in, so the two can't drift apart.

## The local backend

**Store.** One SQLite file per board at an explicit path: `--board <dir>` holds `board.db`. There is no implicit default and no lookup from the current directory. `run_next` refuses a board inside the consumer's worktree, because writing there would break the clean Git stop.

**Schema.** Versioned (`PRAGMA user_version`), and the backend refuses a version it doesn't know.

| Table | Holds |
| --- | --- |
| `meta` | Schema version and board slug |
| `tasks` | `id`, `title`, `status`, `priority`, `created_at`, `assignee`, `current_run_id` |
| `task_links` | `parent_id`, `child_id`: hard dependencies, as in Hermes |
| `task_runs` | `id`, `task_id`, `status`, `outcome`, `claimed_by`, `claimed_at`, `claim_expires_at`, `ended_at`, `metadata` (slice id, handoff digest, reason, block kind) |
| `task_events` | Append-only: `task_id`, `run_id`, `kind`, `actor`, `at`, `payload`. A task's `created` event can carry `creator_task_id` for provenance, as in Hermes. |

Task statuses are a subset of the ones the projection already maps: `todo`, `ready`, `running`, `review`, `blocked`, `done` and `archived`. A new task is `ready`, or `todo` while a parent is open. Run outcomes are `review_requested`, `blocked` and `expired`. Table and column names match Hermes's where they mean the same thing, but the file is `board.db`, not `kanban.db`, and it is not a Hermes board.

**Atomic check-and-write.** Every write opens `BEGIN IMMEDIATE`. A supervisor claim, and any person's write given `--expect-revision`, recomputes the revision **inside that transaction**, compares it, writes and commits. That closes the gap the projection contract notes, where the snapshot check and the write aren't atomic across processes. Run in WAL mode with a busy timeout.

**Claim renewal.** A claim belongs to its run id, not to a process, so `renew` can extend a long slice. That removes the Hermes backend's two-hour TTL limit. Every write first releases expired claims in a transaction of its own: the run ends `expired` and the task goes back to `ready`, or `todo` while a parent is open. `expire` does only that. A late `record` or `renew` for an expired run is refused. A snapshot never writes, so an expired claim shows as running until the next write.

**Command-line tool.** `python -m automation.supervisor.local_board --board <dir> [--actor <name>] [--expect-revision <rev>] <command>`:

| Command | Who uses it | Effect |
| --- | --- | --- |
| `init` | Person | Creates the board. The slug is the directory's name. |
| `add`, `link`, `priority`, `archive` | Person | Edits tasks. `link` refuses a cycle and moves a ready child behind an open parent to `todo`. `archive` refuses a running task. |
| `list`, `show`, `snapshot` | Anyone | Read-only. `snapshot` prints the board as JSON with its revision: the input for the wiki and for the shadow diff. |
| `expire` | Anyone | Releases expired claims now |
| `approve <task>` | Person | Moves `review` to `done` and promotes children whose parents are all `done` or `archived` from `todo` to `ready` |
| `unblock <task>` | Person | Moves `blocked` to `ready`, or `todo` while a parent is open |
| `claim`, `record`, `renew` | **Not exposed** | Library calls only, used by the supervisor |

Every event records an `actor`, taken from `--actor` or the OS user.

**What this does not enforce.** Any process that can write the file can run `approve`, so a worker could in principle approve its own task. Hermes has the same property today: a worker can run `hermes kanban` too. This spec makes it auditable, because every event has an actor, and testable, because a test asserts that the supervisor and the runner environment never call `approve`. Real separation needs OS permissions, such as a board file the runner's user can't write. The owner decided an auditable approval is enough for now (decision 3), so RA-BD-002 doesn't add that separation. Don't describe approval as enforced.

**Limits.** One host only. SQLite on a network filesystem is not supported. No UI beyond the CLI: a board view is the Wiki's or a later tool's job, read-only.

## How any parent agent uses it

| Parent | Path |
| --- | --- |
| Claude Code, Codex, Hermes | The same four plugin skills in every harness. `boundroom-status` reads the board and names the next eligible slice. The `boundroom-run-next` skill passes `--board-backend local --board <dir> --kanban-overlay <file>` to `run_next.py --single-slice`. `run_agent.sh` picks the runner from the originating harness, as today. The skills change only in prose, with no code (SPEC-002). |
| Plain shell or CI | The same `run_next.py` flags. Headless runs name a runner explicitly (SPEC-001 D). |
| A person | `python -m automation.supervisor.local_board` for edits and approvals |

No backend imports, calls or assumes a particular agent harness. A test runs the whole Kanban flow with `hermes` absent from `PATH` and with no Hermes environment variables set.

## Effects on SPEC-001 and the roadmap

- **Ownership table.** "Task identity, status, … dispatch → Hermes Kanban per-project board" becomes "→ the consumer's configured board backend (local by default; Hermes optional)". "Dispatch" is removed, because nothing dispatches except `run_next`.
- **Swarm Wiki.** The pinned compiler reads Hermes's `kanban.db`. For a local board, it needs to read `snapshot --json` instead. That is an upstream change to hermes-swarm-protocol, reviewed separately and pinned like the Hindsight snapshot adapter. Until it lands, a local board has no Wiki view. Never write pseudo-Hermes tables to satisfy the compiler.
- **Hermes-specific roadmap items.** The gateway-race and goal-mode work in RA-CP-002R applies only to the Hermes backend. It stops blocking H2 when a consumer uses the local backend.
- **AGENTS.md.** Its "Hermes Kanban" wording changes in its own reviewed PR after this spec is accepted, not here.

## Delivery slices

Each slice is a separate pull request with its own allowed paths, validations and review.

| Slice | Deliverable | Acceptance |
| --- | --- | --- |
| RA-BD-001 | Extract `BoardBackend`; move `read_board` and `kanban_writer` behind it as the Hermes backend | No behavior change. Every existing Kanban test passes unchanged. |
| RA-BD-002 | Local backend store, schema and CLI | The contract suite passes on both backends. Includes stale-revision, two-process claim race, expired claim, superseded run, unknown schema version and foreign-board refusals. |
| RA-BD-003 | `run_next --board-backend`, the overlay `backend` field, and plugin skill prose | The end-to-end `run_next` tests run against the local backend with no `hermes` on `PATH`. A board path inside the worktree is refused. |
| RA-BD-004 | Claude Code and Codex parent canaries on a disposable local board | For each of Claude Code and Codex, as both parent and runner: one real slice ends in `review`, a person approves it, and the second slice stays queued. The runner in each handoff is the parent's harness. **Each model run needs owner authorization.** Claude Code: passed 2026-11-10. Codex: deferred until a machine where Codex is approved. |
| RA-BD-005 | Wiki input from `snapshot --json` (upstream) and a pin bump here | A deterministic build and `--check` from a local board snapshot |

The performance rule in AGENTS.md applies. RA-BD-002 and RA-BD-003 add file I/O to the supervisor path, which grows with board size, and their PR descriptions must say so.

## RA-BD-004: Claude Code canary (2026-11-10)

**Result: passed.** A real slice ran on a disposable local board, with no Hermes installed, and Claude Code as both parent and runner. The owner authorized the run. **The Codex canary is deferred (decided 2026-11-10).** The owner's machine is a company computer, and Codex is not allowed or approved on it. Codex as parent and runner therefore stays unproven with a real model. The harness-neutral path is covered without a model: `run_agent.sh` picks Codex from the process tree in `automation/tests/test_runner_selection.py`, and the board, `run_next` and the plugin contain no harness-specific code. Run the Codex canary, unchanged, on a machine where Codex is approved.

**Setup.**

| | |
| --- | --- |
| Source | repo-automation `aff3070`, synced into a throwaway consumer with `Tools/repo-automation-sync.sh --sync --pin`, which ran the source's quality gate. Python 3.13.18, pydantic 2.14.2. |
| Consumer | A fresh git repository with a local bare repository as `origin`, so the runner's push had an upstream. |
| Board | Local board `ra-bd-004-claude`, outside the consumer. T1 `t_4e656615` "Create hello.txt containing the single line hello", `ready`. T2 `t_cddc492c` "Create bye.txt …", a child of T1, `todo`. |
| Overlay | `backend: local`. Each task: one allowed path, `max_files_changed: 1`, validations `git diff --check` (supervisor-replayed) and `grep -qx hello hello.txt` (run-reported). |
| Harness | Claude Code 2.1.311. Parent: `claude -p --permission-mode default --settings <allow only run_next.py> --max-budget-usd 1`. Runner: `run_agent.sh` in `auto` mode, `REPO_AUTOMATION_CLAUDE_PERMISSION_MODE=acceptEdits`, and `REPO_AUTOMATION_CLAUDE_BIN` pointing at a wrapper that logged each launch, then ran Claude Code with `--settings <allow only the slice's git and validation commands> --max-budget-usd 2`. `REPO_AUTOMATION_AGENT_RUNNER` was unset. |

**What happened.**

- **One slice.** The parent ran `run_next.py --single-slice --board … --kanban-overlay …` once. It exited 0 with `stop_for_review`: `queue_status: done`, one changed file, no validation failures and no out-of-scope paths. The parent took 63 s and cost $0.16. The runner's cost isn't reported; it was capped at $2.
- **The runner was the parent's harness.** The wrapper logged exactly one launch: `runner=claude`, with `--print --input-format text --no-session-persistence --permission-mode acceptEdits`. `run_agent.sh` chose Claude from the process tree.
- **The work.** `hello.txt` contains exactly `hello\n` at HEAD and on `origin`, in one commit. The consumer was clean and even with its upstream afterwards.
- **The board.** T1 went to `review` and T2 stayed `todo`. The events were `created` (raelldottin), `claimed` and `review_requested` (`repo-automation-supervisor`), and no `approved`. The run recorded the slice id `board.ra-bd-004-claude.t_4e656615` and a handoff SHA-256 that matches the handoff file.
- **Refusals with no model, each leaving the board's bytes unchanged.**
  - `--single-slice --autonomous-limit 2` exited 64.
  - `--board-backend hermes` exited 2.
  - A dry run while T1 waited for review found no eligible slice and named both blockers.
- **A person's approval.** The owner ran `local_board … --actor raelldottin approve t_4e656615`, which printed `ready: t_cddc492c`. T1 became `done` with `approved` by raelldottin, and T2 became `ready` with `promoted`. A dry run then selected T2 without running it, and the runner log still held one launch.

**Attempts that ran no slice.** None of these touched the board:

1. The parent's prompt was swallowed by `--allowedTools`, which takes several values. `claude -p` exited with no input.
2. A stray `--` in the prompt reached `run_next`, and argparse refused it. That session cost $0.38.
3. Claude Code ignores an untrusted project's `.claude/settings.json`. Both sessions therefore take their permissions from explicit `--settings` files. The ignored file stayed in the consumer.

**Findings.**

1. **The supervisor's replay proved nothing here.** The runner committed before handing off, so the supervisor's `git diff --check` examined an empty diff. The runner said so in its own report. A replay should check the slice's changes from the base commit to the head commit, not only what is uncommitted.
2. **A handoff doesn't name its runner.** Only the canary's wrapper showed it was Claude Code. The handoff's `verified_commit_sha` was empty, which `run_next` accepted, though `evaluate_handoff` would refuse it.
3. **Both sessions loaded the owner's user-level Claude Code configuration**, including plugins and hooks. `run_agent.sh` has no way to isolate a run from it.
4. **Approval isn't enforced, as decision 3 accepted.** The supervisor never approved anything. The approval came from a command a person ran.

## Alternatives considered

| Option | Why not as the default |
| --- | --- |
| Keep Hermes Kanban as the only board | Fails the owner's requirement: a Hermes install is mandatory. |
| GitHub Issues or Projects as the board | A proprietary hosted service, against SPEC-001's local-first, open-source profile. It needs network access, and its API has no conditional writes, so a claim race can't fail closed. Possible later as a read-only mirror, never a second writer. |
| The JSON queue alone | Already agent-agnostic and stays the default without board mode. It has no claims, runs, review state, human approval or event history, which are what the board is for. |
| Write Hermes's SQLite directly, without its CLI | Forbidden by SPEC-001. It also keeps Hermes's unversioned schema as a dependency. |
| A board server or daemon | Adds a process and a network surface, which SPEC-002 and SPEC-001 rule out without a separate decision. SQLite transactions give the needed atomicity on one host. |

## Open decisions for the owner

1. ~~Is the local backend the SPEC-001 target, with Hermes optional?~~ **Decided 2026-11-10:** yes. The local backend is the default and Hermes is optional.
2. Where do boards live by convention? An explicit path is always required, but should the docs suggest `~/.local/state/boundroom/boards/<slug>/`?
3. ~~Is auditable approval enough for now?~~ **Decided 2026-11-10:** yes. Every approval records its actor, and a test checks that the supervisor and the runner environment never call `approve`. OS-level separation can be proposed later as its own change.
4. ~~Should existing Hermes boards be migrated?~~ **Decided 2026-11-10:** no migration is required. Hermes boards stay on the Hermes backend. A one-way import can be proposed later if a consumer wants one.
5. Approval of the upstream hermes-swarm-protocol change in RA-BD-005.
