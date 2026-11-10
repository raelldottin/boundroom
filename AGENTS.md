## Performance

The supervisor, policy, validation and context-building paths run once or a few times per slice, and their cost is small next to a model session. No benchmark is required for them.

A change that adds file, network or subprocess work to those paths, or makes their cost grow with queue size, handoff count, document size or repository size, MUST say so in its pull request description. The reviewer decides whether it needs a measurement before it merges.

## Delegated Operator Authority (owner decision, 2026-11-10)

The owner has delegated to the agent every decision this file and the roadmap used to reserve for a person, so the roadmap can be completed without operator intervention. Where another section of this file, the roadmap or a specification says the owner, maintainer or a person must approve, decide or act, the agent does it and records it. The delegation covers:

- merging this repository's pull requests, and consumer sync pull requests, once their required checks pass;
- creating release tags;
- approving reviewed tasks on a board;
- starting benchmark runs and choosing their model, provider and budget;
- cutovers, deployments and schedules;
- the roadmap's open technical decisions, such as models, hosting, retention and canary targets.

Delegation removes the person, not the safeguards. Every delegated action follows these rules:

- **Checks first.** A merge or release happens only after every required check has passed at the exact head, pinned (for example `gh pr merge --match-head-commit <sha>`), without an admin override or any bypass.
- **Separate roles.** The agent that approves a slice, a handoff or a board task is never the runner that did that slice's work. No worker or sub-agent approves its own work or starts the next slice.
- **Recorded.** Every delegated decision is recorded in the pull request, commit, board event or release that carries it, with the agent named as the actor (for example `--actor "claude-code (delegated by raelldottin)"`), so it can be audited and reverted.
- **Reversible first.** Prefer the reversible step. A cutover keeps the previous authority restorable until its canary passes.

Some steps need something only a person can supply: creating accounts, entering credentials or API keys, spending through a payment method, or installing software and model weights on the owner's machine. The agent prepares everything around such a step, asks for the one input it needs, and carries on with other work meanwhile.

One decision is not delegated: choosing or changing a license stays the owner's, as the open-source profile item below says. The repository's license is already decided, and no roadmap step needs a change.

The owner can withdraw or narrow this delegation at any time by changing this section.

## Control-Plane Architecture and Roadmap Priority

Before architecture or new feature work, read [SPEC-001](docs/architecture/control-plane.md) and [the roadmap](docs/roadmap.md). The proposed top layer is **Hermes Kanban**, **self-hosted Hindsight memory** and a **generated Swarm Wiki**, with distinct canonical owners and the deterministic repo-automation supervisor beneath it. **This is a proposed target, not an installed integration.**

- Prioritize the P0 adapter/inventory/canary acceptance gate before expanding the supervisor roadmap.
- **Benchmark exemption.** Benchmark and effectiveness work is exempt from the P0 ordering by owner decision. Do not defer, pause or refuse benchmark work because P0 is incomplete. The agent decides each run, model, provider and budget change under the delegated authority above, and records it in a pull request.
- Do not add a parallel queue, task store, memory database, coordination daemon or wiki write authority without a separate explicit design decision.
- Today the file-based slice queue remains operational authority; do not silently migrate or dual-write it and Kanban. A future cutover requires independent verification, provenance and rollback; the agent approves it under the delegated authority above.
- Never treat wiki/memory summaries as independent live verification; do not allow workers to self-chain or bypass supervisor gates.
- **Delegation.** Agent-native delegation (a runner such as Claude Code launching its own sub-agents within one slice run) is available today. Supervisor-controlled delegation is proposed in SPEC-001 and not implemented; do not describe or rely on it as current behavior. No worker or sub-agent may start the next slice.
- The deployed Swarm Wiki currently reads ByteRover-specific `.brv/context-tree` files. Do not claim the wiki is Hindsight-compatible until a separately reviewed upstream migration adapter preserves source hashes and evidence.
- **Open-source production profile (owner gate).** A fully open-source production profile is a target the owner decides, not an instruction for agents to act on. Agents MUST NOT choose, add or change a license. Record license gaps and dependency or model license risks for the owner instead. Proprietary development assistants and the existing hosted benchmark sit outside that production profile.
- Keep benchmarks and pinned experiment settings isolated from live control-plane integrations.

## Discord Knowledge Loop (SPEC-003: planned)

[The Discord Knowledge Loop](docs/architecture/discord-knowledge-loop.md) is an **opt-in, separately deployed knowledge service**, not a runner, queue, controller, worker, or additional supervisor. It is not currently implemented. It may capture only explicitly approved sources, organize evidence with Hindsight and Swarm Wiki, retrieve authorized knowledge and publish only to permitted Discord audiences. Scheduled knowledge processing is enabled by the agent under the delegated authority above, once its gates pass. Its credentials come from the owner.

Agents MUST NOT give the knowledge service execution credentials or code paths to `run_next`, shell/agent execution, supervisor policy/queue/schema updates, Kanban task writes, GitHub repository write/merge APIs, or consumer synchronization. No Discord request, knowledge summary, scheduled ingestion or wiki page may dispatch/approve a slice or bypass the supervisor's gates. This is a required **process, permission, API and negative-test boundary**, not just a prompt rule. Do not treat capture or publishing consent as implied by channel visibility.

## GitOps: Changes, Deployment, and Releases

`main` is the only source of deployed or released state. Git history on `main` is the record of what was shipped and why.

### Hard Rule

Every change MUST reach `main` through a pull request whose required checks have passed. Nothing deploys or releases except a workflow running from `main` or from a tag whose commit is on `main`.

### Changes to `main`

The `main` ruleset requires a pull request, the five required checks (`make reusable-check (3.11)`, `make reusable-check (3.12)`, `Tests + coverage comment`, `No agent attribution trailers`, `Review dependency graph diff`), and linear history, and it forbids force-pushes and deletion. Pull requests merge by squash or rebase. Approving reviews are not required yet.

Agents MUST NOT:

- push directly to `main`, force-push it, or delete it;
- disable, bypass, or weaken the ruleset or a required check, including by editing a workflow so a check passes without doing its work;
- merge a pull request whose required checks have not passed.

### Documentation Deployment

The documentation site deploys to GitHub Pages only from `.github/workflows/publish-docs.yml` running on a push to `main`. Agents MUST NOT publish the site any other way, including a manual dispatch from another branch.

### Releases

A release is a `vMAJOR.MINOR.PATCH` tag (optionally `-prerelease`) on a commit already on `main`, built and published by `.github/workflows/release.yml`, which reruns the required gate and publishes the archive and its `.sha256`. The agent creates the tag on the merged commit under the delegated authority above, after `python3 Tools/check_plugin_version.py <tag>` passes and the commit's required checks are green.

Agents MUST NOT hand-build, upload, or edit a release asset, and MUST NOT move or delete an existing release tag. Until `release.yml` exists on `main`, no release may be published.

### Consumer Repositories

Consumers receive changes only through `Tools/repo-automation-sync.sh`, driven by `automation/reusable-manifest.json`, from a released tag or a pinned commit. The result lands through a pull request in the consumer repository.

An agent MAY deliver a sync to a consumer, but only as a reviewable pull request: run the sync script from a released tag or a pinned commit, commit exactly its result, and the consumer's lock pin as described below, on a new branch, push that branch, and open a pull request against the consumer's default branch. This is what the plugin's `repo-automation-sync` skill does, after the user's go-ahead.

Agents MUST NOT:

- merge that pull request before the consumer's required checks pass, as described below;
- push to, force-push or delete a consumer's default branch;
- commit anything to a consumer branch beyond the sync result and that lock pin, or use `--allow-unverified-source`.

**Merging a sync pull request.** The agent merges it under the delegated authority above when all of these hold:

- the consumer's required checks have passed at the current head, or it has none. A failing check that is not required is recorded in the pull request before the merge;
- the merge is pinned to that head (for example `gh pr merge --match-head-commit <sha>`) and uses a merge method the consumer allows, without an admin override or any other bypass of the consumer's branch protection.

If the head changes, the agent checks again before merging.

**The consumer's lock.** The sync script refuses to write `automation/repo-automation.lock`, because the consumer's `make` runs what its pin names. A consumer that keeps a lock checks its vendored files against that pin before a push: Owlory's pre-push hook runs `make repo-automation-check`, so a sync committed under the old pin cannot be pushed. In the same commit as the sync result, an agent MAY set the lock's `commit` to the full SHA it passed as `--pin`, and `imported_at` to the import time, and records both values in the pull request. It MUST NOT change any other field of the lock, including `remote` and `repository`, and MUST NOT skip the consumer's hooks to push.

Automatic consumer updates, including Owlory, are not implemented as a background process. Under the delegated authority above, the agent may start a sync itself; each one is still a reviewable pull request.

### Agent-Harness Plugin

[SPEC-002](docs/architecture/agent-plugin.md) specifies one optional plugin for Claude Code, Codex and Hermes. It lives in `plugins/repo-automation/`, with its catalogs at `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`. It is **released**, first in `v0.2.0`. Install it from a release tag, not from an untagged commit on `main`.

The plugin is a front end, not another supervisor or authority. It is skills and manifests only. Its skills drive the supervisor copy that `Tools/repo-automation-sync.sh` placed in the repository being worked on. That synced copy, its queue and the supervisor's gates remain the authority. A repository without a synced copy has nothing for the plugin to run.

Agents MUST NOT:

- put supervisor, policy, queue or schema code in the plugin, or let it fall back to a bundled copy;
- use the plugin to deliver repo-automation to a consumer, or to change a consumer except through the sync script and a pull request;
- add a hook, MCP server or background process to the plugin without a separate design decision;
- describe a skill's instructions as enforcement. One slice per invocation is enforced only by `run_next.py --single-slice` when it is used, and the queue's `consecutive_autonomous_limit` is a default a caller can override, not a ceiling.

A plugin release follows the Releases rules above. It also needs the pre-release check from SPEC-002, which requires every plugin manifest to declare the tag's version.

### Out of Scope

Runtime deployment, including the Hermes runtime and benchmark execution environments, is not covered by this policy.
