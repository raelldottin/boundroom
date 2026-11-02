## Performance

The supervisor, policy, validation and context-building paths run once or a few times per slice, and their cost is small next to a model session. No benchmark is required for them.

A change that adds file, network or subprocess work to those paths, or makes their cost grow with queue size, handoff count, document size or repository size, MUST say so in its pull request description. The reviewer decides whether it needs a measurement before it merges.

## Control-Plane Architecture and Roadmap Priority

Before architecture or new feature work, read [SPEC-001](docs/architecture/control-plane.md) and [the roadmap](docs/roadmap.md). The proposed top layer is **Hermes Kanban**, **self-hosted Hindsight memory** and a **generated Swarm Wiki**, with distinct canonical owners and the deterministic repo-automation supervisor beneath it. **This is a proposed target, not an installed integration.**

- Prioritize the P0 adapter/inventory/canary acceptance gate before expanding the supervisor roadmap.
- **Benchmark exemption.** Benchmark and effectiveness work is exempt from the P0 ordering by owner decision. Do not defer, pause or refuse authorized benchmark work because P0 is incomplete. Each run, model, provider or budget change still needs its own authorization.
- Do not add a parallel queue, task store, memory database, coordination daemon or wiki write authority without a separate explicit design decision.
- Today the file-based slice queue remains operational authority; do not silently migrate or dual-write it and Kanban. A future cutover requires explicit approval, independent verification, provenance and rollback.
- Never treat wiki/memory summaries as independent live verification; do not allow workers to self-chain or bypass supervisor gates.
- **Delegation.** Agent-native delegation (a runner such as Claude Code launching its own sub-agents within one slice run) is available today. Supervisor-controlled delegation is proposed in SPEC-001 and not implemented; do not describe or rely on it as current behavior. No worker or sub-agent may start the next slice.
- The deployed Swarm Wiki currently reads ByteRover-specific `.brv/context-tree` files. Do not claim the wiki is Hindsight-compatible until a separately reviewed upstream migration adapter preserves source hashes and evidence.
- **Open-source production profile (owner gate).** A fully open-source production profile is a target the owner decides, not an instruction for agents to act on. Agents MUST NOT choose, add or change a license. Record license gaps and dependency or model license risks for the owner instead. Proprietary development assistants and the existing hosted benchmark sit outside that production profile.
- Keep benchmarks and pinned experiment settings isolated from live control-plane integrations.

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

A release is a `vMAJOR.MINOR.PATCH` tag (optionally `-prerelease`) on a commit already on `main`, built and published by `.github/workflows/release.yml`, which reruns the required gate and publishes the archive and its `.sha256`. The maintainer creates the tag after the change merges.

Agents MUST NOT hand-build, upload, or edit a release asset, and MUST NOT create or move a release tag, unless the maintainer has explicitly authorized that action for that specific tag. Until `release.yml` exists on `main`, no release may be published.

### Consumer Repositories

Consumers receive changes only through `Tools/repo-automation-sync.sh`, driven by `automation/reusable-manifest.json`, from a released tag or a pinned commit. The result lands through a pull request in the consumer repository.

Agents MUST NOT commit or push to a consumer repository on this repository's behalf. Automatic consumer updates, including Owlory, are not implemented.

### Agent-Harness Plugin

[SPEC-002](docs/architecture/agent-plugin.md) specifies one optional plugin for Claude Code, Codex and Hermes. It lives in `plugins/repo-automation/`, with its catalogs at `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`. It is **implemented on `main` and not released**. Until a release tag carries it, do not describe it as released or available to install from a release.

The plugin is a front end, not another supervisor or authority. It is skills and manifests only. Its skills drive the supervisor copy that `Tools/repo-automation-sync.sh` placed in the repository being worked on. That synced copy, its queue and the supervisor's gates remain the authority. A repository without a synced copy has nothing for the plugin to run.

Agents MUST NOT:

- put supervisor, policy, queue or schema code in the plugin, or let it fall back to a bundled copy;
- use the plugin to deliver repo-automation to a consumer, or to change a consumer except through the sync script and a pull request;
- add a hook, MCP server or background process to the plugin without a separate design decision;
- describe a skill's instructions as enforcement. One slice per invocation is enforced only by `run_next.py --single-slice` when it is used, and the queue's `consecutive_autonomous_limit` is a default a caller can override, not a ceiling.

A plugin release follows the Releases rules above. It also needs the pre-release check from SPEC-002, which requires every plugin manifest to declare the tag's version.

### Out of Scope

Runtime deployment, including the Hermes runtime and benchmark execution environments, is not covered by this policy.
