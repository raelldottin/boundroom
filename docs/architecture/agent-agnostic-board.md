# SPEC-004: A Kanban board that works with any parent agent

Status: **proposed design; nothing here is implemented.** It amends [SPEC-001](control-plane.md) contract A once accepted.
Owner decision recorded here (2026-11-10): the Kanban board must work with any parent agent process, such as Claude Code, Codex, Hermes or a plain shell, **with no Hermes dependency**. This spec proposes how to meet that requirement. Implementation waits for it to be accepted.
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
2. **A local backend as the default board.** repo-automation ships a small board store: a versioned SQLite file, accessed with the standard library only, plus a command-line tool for people and agents. It needs no Hermes, no daemon and no network.
3. **Hermes becomes an optional backend.** The existing adapters move behind the interface unchanged. Hermes is no longer the target named by SPEC-001.
4. **Exactly one canonical board per consumer.** A consumer picks one backend. Nothing mirrors or dual-writes between the local board, a Hermes board and the JSON queue. Board mode already ignores `--queue`, and that stays.

The local board is a **store, not a scheduler**. It has no dispatcher, no worker pool and no background process. Only `run_next` starts work, under the same rules as today. SPEC-001's rule "do not build a second scheduler or board database" is amended for this one store: this design replaces Hermes as the board, so it is not a second one.

## The interface

```python
class BoardBackend(Protocol):
    slug: str
    def snapshot(self) -> BoardSnapshot: ...            # tasks, links, creators, revision; read-only
    def claim(self, task_id: str, expected_revision: str, ttl_seconds: int) -> Claim: ...
    def renew(self, claim: Claim, ttl_seconds: int) -> Claim: ...
    def record(self, claim: Claim, verdict: Verdict, reason: str, handoff_sha256: str | None) -> bool: ...
```

- `BoardSnapshot` and the revision keep their current meaning: the SHA-256 of what the projection reads. `kanban_projection.project` and `load_overlay` take a snapshot from any backend.
- **Every write is conditional.** `claim` refuses a stale revision, a task that isn't `ready` or one with an assignee. `record` and `renew` refuse a run that is no longer the task's current run. A refusal raises, and nothing is retried or guessed. These are the semantics [the write-back contract](kanban-cutover.md) already defines.
- **The supervisor never writes `done`.** A `done` verdict becomes `review`. Only a person's approval moves a task to `done`.
- **Slice ids say which backend they came from:** `kanban.<slug>.<task>` stays for Hermes, so existing handoffs still parse, and the local backend uses `board.<slug>.<task>`. The overlay gains a `backend` field, and a mismatch is refused.
- **Contract tests run against every backend.** One parameterized suite runs against the local backend and the Hermes stand-in, so the two can't drift apart.

## The local backend

**Store.** One SQLite file per board at an explicit path: `--board <dir>` holds `board.db`. There is no implicit default and no lookup from the current directory. `run_next` refuses a board inside the consumer's worktree, because writing there would break the clean Git stop.

**Schema.** Versioned (`PRAGMA user_version`), and the backend refuses a version it doesn't know.

| Table | Holds |
| --- | --- |
| `meta` | Schema version and board slug |
| `tasks` | `id`, `title`, `status`, `priority`, `created_at`, `assignee`, `current_run_id` |
| `task_links` | `parent_id`, `child_id`: hard dependencies, as in Hermes |
| `task_runs` | `id`, `task_id`, `status`, `outcome`, `claimed_by`, `claim_expires_at`, `metadata` (slice id, handoff digest) |
| `events` | Append-only: `task_id`, `run_id`, `kind`, `actor`, `at`, `payload`, including `created` with `creator_task_id` for provenance |

Statuses and transitions are the subset of Hermes's that the projection already maps: `triage`, `todo`, `ready`, `running`, `review`, `blocked`, `done` and `archived`. Run outcomes are `review_requested`, `blocked`, `reclaimed` and `expired`. Any status the projection doesn't map is refused.

**Atomic check-and-write.** Every write opens `BEGIN IMMEDIATE`, recomputes the revision **inside the transaction**, compares it, writes and commits. That closes the gap the projection contract notes, where the snapshot check and the write aren't atomic across processes. Run in WAL mode with a busy timeout.

**Claim renewal.** A claim belongs to its run id, not to a process, so `renew` can extend a long slice. That removes the Hermes backend's two-hour TTL limit. An expired claim is reclaimed lazily, by the next write that sees it, and the run's outcome becomes `expired`. A late `record` for that run is then refused.

**Command-line tool.** `python -m automation.board --board <dir> <command>`:

| Command | Who uses it | Effect |
| --- | --- | --- |
| `init --slug` | Person | Creates the board |
| `add`, `link`, `priority`, `archive` | Person | Edits tasks. Each write is checked against the revision. |
| `list`, `show`, `snapshot --json` | Anyone | Read-only. `snapshot --json` is the input for the wiki and for the shadow diff. |
| `approve <task>` | Person | Moves `review` to `done` and promotes children whose parents are all `done` or `archived` from `todo` to `ready` |
| `unblock <task>` | Person | Moves `blocked` to `ready` |
| `claim`, `record`, `renew` | **Not exposed** | Library calls only, used by the supervisor |

Every event records an `actor`, taken from `--actor` or the OS user.

**What this does not enforce.** Any process that can write the file can run `approve`, so a worker could in principle approve its own task. Hermes has the same property today: a worker can run `hermes kanban` too. This spec makes it auditable, because every event has an actor, and testable, because a test asserts that the supervisor and the runner environment never call `approve`. Real separation needs OS permissions, such as a board file the runner's user can't write, and is listed under open decisions. Don't describe it as enforced.

**Limits.** One host only. SQLite on a network filesystem is not supported. No UI beyond the CLI: a board view is the Wiki's or a later tool's job, read-only.

## How any parent agent uses it

| Parent | Path |
| --- | --- |
| Claude Code, Codex, Hermes | The plugin's `repo-automation-run-next` skill passes `--board-backend local --board <dir> --kanban-overlay <file>` to `run_next.py --single-slice`. `run_agent.sh` picks the runner from the originating harness, as today. The skills change only in prose, with no code (SPEC-002). |
| Plain shell or CI | The same `run_next.py` flags. Headless runs name a runner explicitly (SPEC-001 D). |
| A person | `python -m automation.board` for edits and approvals |

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
| RA-BD-004 | Claude Code parent canary on a disposable local board | One real slice with Claude Code as parent and runner ends in `review`, a person approves it, and the second slice stays queued. **Needs owner authorization for the model run.** |
| RA-BD-005 | Wiki input from `snapshot --json` (upstream) and a pin bump here | A deterministic build and `--check` from a local board snapshot |

The performance rule in AGENTS.md applies. RA-BD-002 and RA-BD-003 add file I/O to the supervisor path, which grows with board size, and their PR descriptions must say so.

## Alternatives considered

| Option | Why not as the default |
| --- | --- |
| Keep Hermes Kanban as the only board | Fails the owner's requirement: a Hermes install is mandatory. |
| GitHub Issues or Projects as the board | A proprietary hosted service, against SPEC-001's local-first, open-source profile. It needs network access, and its API has no conditional writes, so a claim race can't fail closed. Possible later as a read-only mirror, never a second writer. |
| The JSON queue alone | Already agent-agnostic and stays the default without board mode. It has no claims, runs, review state, human approval or event history, which are what the board is for. |
| Write Hermes's SQLite directly, without its CLI | Forbidden by SPEC-001. It also keeps Hermes's unversioned schema as a dependency. |
| A board server or daemon | Adds a process and a network surface, which SPEC-002 and SPEC-001 rule out without a separate decision. SQLite transactions give the needed atomicity on one host. |

## Open decisions for the owner

1. Is the local backend the SPEC-001 target, with Hermes optional, as proposed? Or are the two equal options, with each consumer choosing?
2. Where do boards live by convention? An explicit path is always required, but should the docs suggest `~/.local/state/repo-automation/boards/<slug>/`?
3. Is auditable approval enough for now, or should RA-BD-002 include OS-level separation so the runner can't approve?
4. Should existing Hermes boards be migrated to a local board with a one-way, provenance-preserving import? Or should Hermes users stay on the Hermes backend?
5. Approval of the upstream hermes-swarm-protocol change in RA-BD-005.
