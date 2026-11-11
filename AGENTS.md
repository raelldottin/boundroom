## Performance

The supervisor, policy, validation and context-building paths run once or a few times per slice, and their cost is small next to a model session. No benchmark is required for them.

A change that adds file, network or subprocess work to those paths, or makes their cost grow with queue size, handoff count, document size or repository size, MUST say so in its pull request description. The reviewer decides whether it needs a measurement before it merges.

## Agent Authority and Human Approval (CIS and SOC 2 controls, 2026-11-10)

The owner requires this project to meet CIS and SOC 2 change-management expectations, so an enterprise compliance review can rely on it. That replaces the full delegation the owner granted earlier the same day. The agent does the work; **a person approves every change to what is released or deployed**, and the record shows who did what. See [the control map](docs/compliance/controls.md).

**The agent may, without a person's approval:**

- write code, tests and documentation, run the required checks, and open pull requests;
- run canaries and experiments in disposable environments with approved tools, on the owner's machine only with Claude Code;
- prepare releases, syncs, cutovers and decisions as pull requests, with the reasoning and evidence a reviewer needs.

**A person must approve, as a separate recorded action** (segregation of duties: the author is never the approver):

- **merging any pull request into `main`.** This needs an approving review from a person other than the change's author, with the required checks green at the approved head. After that approval, the agent may perform the merge, pinned to the approved head (`gh pr merge --match-head-commit <sha>`). If the head changes, the approval must be given again;
- **creating a release tag:** a person creates it, or approves a named tag on a named commit;
- **merging a consumer sync pull request** (see Consumer Repositories);
- **approving a task on a board** outside a disposable canary;
- **spending:** benchmark runs and model, provider or budget changes;
- **cutovers, deployments and schedules**;
- **choosing or changing a license.**

**Identity and audit.** Every action names its actor. The agent must not present its own actions as a person's. On GitHub the agent acts as `repo-automation-agent[bot]`, a GitHub App the owner created on 2026-11-10 and installed only on this repository, with no administration permission. Its commits, pushes, pull requests, comments and approved merges use that identity, so the owner can review and approve them as a different person. The agent never uses a person's GitHub credentials to write. Outside GitHub, such as on a board, the actor names the agent: for example `--actor "claude-code (for raelldottin)"`. See the control map.

The agent never weakens, bypasses or disables a control to make progress. When a step needs a person, the agent asks for that one approval and carries on with other work.

## Control-Plane Architecture and Roadmap Priority

Before architecture or new feature work, read [SPEC-001](docs/architecture/control-plane.md) and [the roadmap](docs/roadmap.md). The proposed top layer is **Hermes Kanban**, **self-hosted Hindsight memory** and a **generated Swarm Wiki**, with distinct canonical owners and the deterministic Boundroom supervisor beneath it. **This is a proposed target, not an installed integration.**

- Prioritize the P0 adapter/inventory/canary acceptance gate before expanding the supervisor roadmap.
- **Benchmark exemption.** Benchmark and effectiveness work is exempt from the P0 ordering by owner decision. Do not defer, pause or refuse benchmark work because P0 is incomplete. Each run, model, provider or budget change needs a person's approval (Agent Authority and Human Approval, above).
- Do not add a parallel queue, task store, memory database, coordination daemon or wiki write authority without a separate explicit design decision.
- Today the file-based slice queue remains operational authority; do not silently migrate or dual-write it and Kanban. A future cutover requires a person's approval, independent verification, provenance and rollback.
- Never treat wiki/memory summaries as independent live verification; do not allow workers to self-chain or bypass supervisor gates.
- **Delegation.** Agent-native delegation (a runner such as Claude Code launching its own sub-agents within one slice run) is available today. Supervisor-controlled delegation is proposed in SPEC-001 and not implemented; do not describe or rely on it as current behavior. No worker or sub-agent may start the next slice.
- The deployed Swarm Wiki currently reads ByteRover-specific `.brv/context-tree` files. Do not claim the wiki is Hindsight-compatible until a separately reviewed upstream migration adapter preserves source hashes and evidence.
- **Open-source production profile (owner gate).** A fully open-source production profile is a target the owner decides, not an instruction for agents to act on. Agents MUST NOT choose, add or change a license. Record license gaps and dependency or model license risks for the owner instead. Proprietary development assistants and the existing hosted benchmark sit outside that production profile.
- Keep benchmarks and pinned experiment settings isolated from live control-plane integrations.

## Discord Knowledge Loop (SPEC-003: planned)

[The Discord Knowledge Loop](docs/architecture/discord-knowledge-loop.md) is an **opt-in, separately deployed knowledge service**, not a runner, queue, controller, worker, or additional supervisor. It is not currently implemented. It may capture only explicitly approved sources, organize evidence with Hindsight and Swarm Wiki, retrieve authorized knowledge and publish only to permitted Discord audiences. Scheduled knowledge processing requires a person's approval to deploy, and its credentials come from the owner.

Agents MUST NOT give the knowledge service execution credentials or code paths to `run_next`, shell/agent execution, supervisor policy/queue/schema updates, Kanban task writes, GitHub repository write/merge APIs, or consumer synchronization. No Discord request, knowledge summary, scheduled ingestion or wiki page may dispatch/approve a slice or bypass the supervisor's gates. This is a required **process, permission, API and negative-test boundary**, not just a prompt rule. Do not treat capture or publishing consent as implied by channel visibility.

## GitOps: Changes, Deployment, and Releases

`main` is the only source of deployed or released state. Git history on `main` is the record of what was shipped and why.

### Hard Rule

Every change MUST reach `main` through a pull request whose required checks have passed. Nothing deploys or releases except a workflow running from `main` or from a tag whose commit is on `main`.

### Changes to `main`

The `main` ruleset requires a pull request, the five required checks (`make reusable-check (3.11)`, `make reusable-check (3.12)`, `Tests + coverage comment`, `No agent attribution trailers`, `Review dependency graph diff`), and linear history, and it forbids force-pushes and deletion. Pull requests merge by squash or rebase. The control map asks the owner to make the ruleset require one approving review from a code owner. Until that setting is applied, the human-approval rule above holds by process, not by GitHub.

Agents MUST NOT:

- push directly to `main`, force-push it, or delete it;
- disable, bypass, or weaken the ruleset or a required check, including by editing a workflow so a check passes without doing its work;
- merge a pull request whose required checks have not passed, or that a person other than its author has not approved.

### Documentation Deployment

The documentation site deploys to GitHub Pages only from `.github/workflows/publish-docs.yml` running on a push to `main`. Agents MUST NOT publish the site any other way, including a manual dispatch from another branch.

### Releases

A release is a `vMAJOR.MINOR.PATCH` tag (optionally `-prerelease`) on a commit already on `main`, built and published by `.github/workflows/release.yml`, which reruns the required gate and publishes the archive and its `.sha256`. A person creates the tag, or approves a named tag on a named commit, after `python3 Tools/check_plugin_version.py <tag>` passes and the commit's required checks are green. The agent may push only a tag a person approved that way.

Agents MUST NOT hand-build, upload, or edit a release asset, and MUST NOT move or delete an existing release tag. Until `release.yml` exists on `main`, no release may be published.

### Consumer Repositories

Consumers receive changes only through `Tools/boundroom-sync.sh`, driven by `automation/reusable-manifest.json`, from a released tag or a pinned commit. The result lands through a pull request in the consumer repository.

An agent MAY deliver a sync to a consumer, but only as a reviewable pull request: run the sync script from a released tag or a pinned commit, commit exactly its result, and the consumer's lock pin as described below, on a new branch, push that branch, and open a pull request against the consumer's default branch. This is what the plugin's `boundroom-sync` skill does, after the user's go-ahead.

Agents MUST NOT:

- merge that pull request without the consumer owner's approval, as described below;
- push to, force-push or delete a consumer's default branch;
- commit anything to a consumer branch beyond the sync result and that lock pin, or use `--allow-unverified-source`.

**Merging a sync pull request.** Merging is the consumer owner's decision. An agent MAY carry it out only when all of these hold:

- the owner approved merging that pull request, named by number, after the agent reported its current head commit and the state of its checks. Approval of the sync, of an earlier head, or of a different pull request does not carry over;
- the consumer's required checks have passed, or it has none. A failing check that is not required was reported to the owner before the approval;
- the merge is pinned to the approved head (for example `gh pr merge --match-head-commit <sha>`) and uses a merge method the consumer allows, without an admin override or any other bypass of the consumer's branch protection.

If the head changes after the approval, the agent asks again.

**The consumer's lock.** The lock is `automation/boundroom.lock`, or `automation/repo-automation.lock` in a consumer that adopted before the 0.3.0 rename ([SPEC-005](docs/architecture/boundroom-rename.md)); the rules here apply to either name, and renaming the file is the consumer owner's change. The sync script refuses to write either, because the consumer's `make` runs what its pin names. A consumer that keeps a lock checks its vendored files against that pin before a push: Owlory's pre-push hook runs `make repo-automation-check`, so a sync committed under the old pin cannot be pushed. In the same commit as the sync result, an agent MAY set the lock's `commit` to the full SHA it passed as `--pin`, and `imported_at` to the import time, after showing the owner those values and getting a yes. It MUST NOT change any other field of the lock, including `remote` and `repository`, and MUST NOT skip the consumer's hooks to push.

Automatic consumer updates, including Owlory, are not implemented. Every sync starts from a person's request.

### Agent-Harness Plugin

[SPEC-002](docs/architecture/agent-plugin.md) specifies one optional plugin for Claude Code, Codex and Hermes. It lives in `plugins/boundroom/`, with its catalogs at `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`. It is **released**, first in `v0.2.0`. Install it from a release tag, not from an untagged commit on `main`.

The plugin is a front end, not another supervisor or authority. It is skills and manifests only. Its skills drive the supervisor copy that `Tools/boundroom-sync.sh` placed in the repository being worked on. That synced copy, its queue and the supervisor's gates remain the authority. A repository without a synced copy has nothing for the plugin to run.

Agents MUST NOT:

- put supervisor, policy, queue or schema code in the plugin, or let it fall back to a bundled copy;
- use the plugin to deliver Boundroom to a consumer, or to change a consumer except through the sync script and a pull request;
- add a hook, MCP server or background process to the plugin without a separate design decision;
- describe a skill's instructions as enforcement. One slice per invocation is enforced only by `run_next.py --single-slice` when it is used, and the queue's `consecutive_autonomous_limit` is a default a caller can override, not a ceiling.

A plugin release follows the Releases rules above. It also needs the pre-release check from SPEC-002, which requires every plugin manifest to declare the tag's version.

### Out of Scope

Runtime deployment, including the Hermes runtime and benchmark execution environments, is not covered by this policy.
