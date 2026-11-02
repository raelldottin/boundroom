# SPEC-002: One plugin for Claude Code, Codex and Hermes

Status: **accepted design; the plugin, its skills and `run_next.py --single-slice` are implemented and were first released in `v0.2.0`, behind the release version gate.**  
Owner decisions recorded here (2026-11-01): build the plugin; the single portable-plugin architecture is approved in principle; license the repository `GPL-2.0-only` (see [License](#license)). Implementation waits for this spec to be accepted.  
Authority: until the rollout below lands, the only way repo-automation reaches a consumer repository is `Tools/repo-automation-sync.sh`, as `AGENTS.md` says.

## Decision

repo-automation ships one plugin that installs in Claude Code, Codex and Hermes. The plugin is **a set of skills and three manifests, with no supervisor code in it**. Its skills drive the supervisor copy that `Tools/repo-automation-sync.sh` already placed in the repository being worked on. The synced copy stays the authority for the queue, schemas, policy and validation. The plugin is a front end for it, not a second distribution of it.

## Why a front end, not a second copy

repo-automation's state is per repository: `automation/queue/slices.json`, the schemas, the manifest-owned files and each consumer's own validations. A plugin installs per user, into a harness. If the plugin carried the supervisor, a repository could run code at the plugin's version while its queue and schemas sat at the synced version, and nothing would say which one was in charge. Keeping the plugin code-free removes that split. One version of the supervisor runs per repository, and the sync script and its pull request set it.

## What one plugin directory holds

All three harnesses read the same `skills/<name>/SKILL.md` layout. Each wants its own manifest. These were checked against the versions installed on the owner's machine on 2026-11-01:

| Harness | Manifest | Install |
| --- | --- | --- |
| Claude Code | `plugins/repo-automation/.claude-plugin/plugin.json`, listed by `.claude-plugin/marketplace.json` at the repository root | `claude plugin marketplace add raelldottin/repo-automation`, then `claude plugin install repo-automation@repo-automation` |
| Codex 0.166 | `plugins/repo-automation/.codex-plugin/plugin.json` (`"skills": "./skills/"`) beside the portable `plugin.json`, listed by `.agents/plugins/marketplace.json` at the repository root with a `local` source | `codex plugin marketplace add raelldottin/repo-automation@<tag>`, then `codex plugin add repo-automation@repo-automation` |
| Hermes (`~/.hermes/hermes-agent` at `31bde0d`) | `plugins/repo-automation/plugin.json` in the portable **Agent Plugins v1** format (`$schema` `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`) | `hermes plugins install raelldottin/repo-automation#plugins/repo-automation --ref <sha>`, then `hermes plugins enable repo-automation`; Hermes installs portable packages disabled |

Hermes's native `plugin.yaml` and `register(ctx)` format is not needed. Agent Plugins v1 discovers `skills/` with no Python, and Hermes already ignores `.claude-plugin/` and `.codex-plugin/` during discovery. Superpowers ships the same Claude and Codex manifests side by side in one repository.

The plugin lives in `plugins/repo-automation/`, not at the repository root. This spec first chose the root and called a subdirectory an optional later optimization. The implementation found that the root cannot install in Hermes. `hermes plugins install` security-scans every file in the plugin directory and always blocks a `dangerous` verdict. At the root, that scan covers the whole repository and flags two files the plugin doesn't use: a benchmark fixture's `rm -rf /workspace/*` (a cleanup inside the benchmark's container) and a CI workflow's authenticated `curl` to the model provider. Editing those files to satisfy a scanner would change pinned benchmark settings, and would be the wrong reason to touch them. Hermes installs a subdirectory with a sparse checkout (`owner/repo#path`), and its install name is the last path component, so `plugins/repo-automation` installs as `repo-automation`. Nothing in `automation/` moves.

**What the plugin pull request verified (2026-11-02).** Each tool installed from the pull request's branch into a throwaway home (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `HERMES_HOME`), so the operator's real configuration was untouched.

| Tool | Validation | Install |
| --- | --- | --- |
| Claude Code | `claude plugin validate` passes for the plugin and the root marketplace | installed and enabled at `0.2.0` with all four skills |
| Codex 0.166 | no validate command | `codex plugin marketplace add <checkout>` resolved `.agents/plugins/marketplace.json`; installed and enabled at `0.2.0` with all four skills |
| Hermes | `hermes plugins validate` passes for `plugins/repo-automation` (and fails the security scan for the repository root) | installed from `file://<checkout>#plugins/repo-automation` at a pinned commit, then enabled, with all four skills |

**Marketplaces.** `.agents/plugins/marketplace.json` is the canonical repository marketplace for Codex, and Codex resolved it when both catalogs existed. A Codex `url` source is cloned with git, which works only for a repository root. So the Codex catalog uses a `local` source with a path, the form Codex's own curated catalog uses for subdirectories. Claude Code reads `.claude-plugin/marketplace.json`, whose `source` is the relative path.

## Skills

Each skill first finds the synced copy at `<repository>/automation/`. If there isn't one, it stops and says that the repository has not adopted repo-automation. It never falls back to anything bundled.

| Skill | Does | Writes |
| --- | --- | --- |
| `repo-automation-status` | Loads the queue with `policy.load_queue`, names the next slice with `policy.select_next_slice`, and, given a board and overlay, prints the Kanban projection's shadow diff | nothing |
| `repo-automation-run-next` | Runs the documented command `python3 automation/supervisor/run_next.py --single-slice <user flags>`, then reports the handoff path and the stop reason. If the synced copy doesn't know `--single-slice`, it stops and says the copy is too old. | what `run_next` writes, under its own gates |
| `repo-automation-evaluate-handoff` | Runs `automation/supervisor/evaluate_handoff.py` on a handoff the user names | nothing |
| `repo-automation-sync` | In a consumer repository, runs `Tools/repo-automation-sync.sh` against a released tag or pinned commit, with `--check` first | only after the user says go, and then only through a branch and a pull request in that consumer |

## Rules the plugin must keep

1. **One slice per invocation, enforced in the supervisor, not in prose.** A bare `run_next.py` is a loop. It keeps selecting slices until the queue's `policy.consecutive_autonomous_limit` stops it, so one call can run several slices. A skill is Markdown. It can instruct the agent to run a command, but it can't validate flags or guarantee which command the model actually runs. So the limit lives in trusted code, in three layers:
    - **The supervisor's own gate (exists today).** `run_next` enforces an *effective* limit: `--autonomous-limit` when the caller passes one, otherwise the queue's `policy.consecutive_autonomous_limit`. The queue's value is therefore a default, not a ceiling. An off-contract `run_next.py --autonomous-limit 1000` runs past it, though the scope, handoff and validation gates still apply to every slice. Nothing today puts a non-overridable upper bound on a caller that ignores the skill.
    - **`run_next.py --single-slice` (new, in the plugin pull request).** It runs at most one slice. It exits 64 before claiming anything if `--autonomous-limit` appears at all, in any form or position: `--autonomous-limit 2`, `--autonomous-limit=2`, before or after `--single-slice`, or repeated. This is the enforcement point for invocations that use it.
    - **The skill (guidance).** It documents the one supported invocation, `run_next.py --single-slice`, in a single fenced command, and tells the agent not to add a limit. It doesn't loop, schedule itself or chain to the next slice.

    Tests in the plugin pull request:
    - On a queue with two eligible slices, `--single-slice` runs the agent exactly once and leaves the second slice `queued`.
    - Each `--autonomous-limit` variant above, combined with `--single-slice`, exits 64 with no agent run and no status change.
    - A test reads the fenced command out of the run-next `SKILL.md` and runs exactly that command on the two-slice queue. That checks the invocation the skill tells agents to use, not a paraphrase of it.

    `--single-slice` enforces one slice only when it is used. This spec doesn't claim that a model can't run `run_next.py` some other way, or that anything caps such a run below what its caller asks for. If an untrusted worker needs a globally immutable upper bound, that is a separate trusted policy change with its own review. This spec doesn't supply or imply one. The existing rule that no worker or sub-agent may start the next slice still holds as a rule. It isn't a technical guarantee.
2. **The runner rule stands.** `run_next` already picks the runner from the nearest enclosing harness and refuses a mismatch (raelldottin/repo-automation#68). The plugin never sets `REPO_AUTOMATION_AGENT_RUNNER`, so Claude Code runs Claude, Codex runs Codex and Hermes runs Hermes.
3. **No hooks, MCP servers or daemons.** Nothing runs at session start or on a tool call, and there is no server process. An MCP server would edge toward the "no coordination daemon" rule in SPEC-001 and would need its own design decision.
4. **No credentials.** The plugin holds no keys. The Hindsight and Kanban flags of `run_next` keep reading their credentials the way they do now.
5. **Consumers still change only through a pull request.** `repo-automation-sync` is the existing sync script with a confirmation in front of it. It never commits to a consumer's default branch.

## Versions and releases

A release is still a `vX.Y.Z` tag on a commit already on `main`, built by `release.yml`. The manifests carry the same number without the `v`: tag `v0.2.0` means `"version": "0.2.0"` in every manifest. All marketplaces install from the GitHub repository at a ref, so no new publishing workflow is needed and nothing is uploaded anywhere else. A unit test checks that every manifest has the same `name` and a `version` in `X.Y.Z[-prerelease]` form.

**The release gate.** `release.yml` checks the tag's syntax and that its commit is on `main`. Its first job also runs `Tools/check_plugin_version.py <tag>`, before the gate matrix spends a run, so the comparison is testable outside a release run (`automation/tests/test_check_plugin_version.py`). The script reads the required manifests (`plugin.json`, `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` under `plugins/repo-automation/`) and refuses if any is missing, so an empty or partial set can't pass. It requires that they all declare the same `version`, and that this version equals the tag without its `v`. Otherwise it refuses, naming each file and its version. The existing tag-syntax and ancestry guards stay as they are. Its tests cover these refusals, plus an unreadable manifest and the real manifests passing for their own version:
- a `v0.2.0` tag over manifests that declare `0.1.0` is refused;
- two manifests that disagree are refused, whatever the tag;
- a missing required manifest is refused, even when the ones present match the tag;
- a `v0.2.0-rc.1` tag passes only over manifests that declare `0.2.0-rc.1`.

The plugin and a consumer's synced copy can be at different versions. The skills read only stable command-line interfaces (`run_next.py`, `evaluate_handoff.py`, `kanban_projection.py`, the sync script) and report the synced copy's version when one is recorded. A skill that needs a flag the synced copy lacks says so and stops.

## License

The owner confirmed these decisions in reviewing this spec. Agents do not choose or change licenses; this section records the owner's decisions.

| Question | Decision |
| --- | --- |
| Scope | Repository-wide: the plugin, the supervisor, the tools and original documentation |
| Version | `GPL-2.0-only`, not "or later" |
| Linux `syscall-note` exception | Not included |
| Existing MIT components | Keep their original notices and attribution (`THIRD-PARTY-NOTICES.md`) |
| Consumer repositories | Audit distribution and integration before rollout |
| Apache-2.0 or GPLv3-only code | Not incorporated into a distributed `GPL-2.0-only` combined work |
| `hermes-swarm-protocol` | A separate decision; not covered by this one |

**The license has to travel with every distributed copy.** `automation/reusable-manifest.json` ships automation code and `THIRD-PARTY-NOTICES.md` but no license text today. The license pull request does the following:

- It adds `COPYING`, the GPL-2.0 text, and has the manifest ship it to consumers as `automation/COPYING`. It stays out of the consumer's root, so it can't be read as the license of the consumer's own application. The sync keeps it current, and the release archive includes it, because `release.yml` stages the archive with the same sync.
- It states which files the GPL covers in `README.md` and in the distributed `automation/README.md`: the files repo-automation ships through its manifest, including those outside `automation/` (`Tools/repo-automation-sync.sh`, `docs/workflows/`). It adds the copyright line.
- It adds a test that a consumer export holds `automation/COPYING`, byte-identical to `COPYING`.

**A tool next to an application is not the application.** The sync places GPL-covered development tools in a consumer repository. That alone does not make the consumer's application a derivative of them. Running the supervisor as a separate tool is different from importing its code into the application, or packaging and distributing it as part of a product. The consumer audit establishes which of these each consumer does. For each consumer it checks which synced files are shipped, imported, executed or packaged in a released product. Owlory and Gymphant are private and, as of the owner's review, have no root `LICENSE` or `COPYING`. That shows nothing about their eventual distribution licenses.

**Inbound code is compatible today.** The only runtime dependency, `pydantic`, is MIT. The concepts adapted from `mattpocock/skills` are MIT. Hindsight and Hermes are MIT and are invoked, not bundled. Running a separate program at arm's length is unaffected, as `run_agent.sh` invokes Codex (Apache-2.0). Later dependencies need a license check; `dependency-review.yml` can take an allow-list, and that change to a required check needs its own pull request.

**Authorship.** Git history shows one human author, the owner. Dependabot commits only bump version pins. CodSpeed's one commit edits workflow setup and a badge. Neither adds original work that would need another contributor's agreement.

## Rollout

Each step is its own pull request through the required checks.

1. This spec.
2. The license pull request described above. It may be prepared now. It merges only after the authorship, third-party and consumer-distribution checks pass, and no GPL release is published before then.
3. `AGENTS.md`: name the plugin as a supported front end in "Consumer Repositories". State that it carries no supervisor code and that consumers still change only through the sync script and a pull request. Agent-instruction files go in their own pull request.
4. The plugin: the manifests, the marketplace catalogs, the four skills, `run_next.py --single-slice`, and their tests. The tests check that every manifest parses and agrees on `name` and `version`, and that every skill has the frontmatter all three harnesses require. They also include the one-slice tests above. Install it locally in each harness and record the result. `hermes plugins validate` and `claude plugin validate` check the manifests.
5. The release version gate described under [Versions and releases](#versions-and-releases), with its negative tests. It is a prerequisite of step 6.
6. A release, only with the owner's explicit approval, then a canary: install in one harness, then run `repo-automation-status` and `repo-automation-run-next` against one consumer that has passed the audit.

## Performance

The plugin adds Markdown and JSON only. It adds no file, network or subprocess work to the supervisor, policy, validation or context paths, and does not make their cost grow with any workload. Under the performance rule in `AGENTS.md`, it needs no measurement. The skills call the same code a manual `run_next` run does.

## Settled during implementation

- **Skill frontmatter.** `name` (matching the directory, lowercase words joined by hyphens, at most 64 characters) and `description` (1 to 1024 characters), plus an optional `license`, satisfy all three tools. `automation/tests/test_plugin_manifests.py` enforces that set.
- **Catalog resolution.** See Marketplaces above.
