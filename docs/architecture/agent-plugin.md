# SPEC-002: One plugin for Claude Code, Codex and Hermes

Status: **proposed design; nothing here is implemented**  
Owner decisions recorded here: build the plugin (2026-11-01); license it the same way as the Linux kernel (2026-11-01, see [License](#license)).  
Authority: until the rollout below lands, the only way repo-automation reaches a consumer repository is `Tools/repo-automation-sync.sh`, as `AGENTS.md` says.

## Decision

repo-automation ships one plugin that installs in Claude Code, Codex and Hermes. The plugin is **a set of skills and three manifests, with no supervisor code in it**. Its skills drive the supervisor copy that `Tools/repo-automation-sync.sh` already placed in the repository being worked on. The synced copy stays the authority for the queue, schemas, policy and validation. The plugin is a front end for it, not a second distribution of it.

## Why a front end, not a second copy

repo-automation's state is per repository: `automation/queue/slices.json`, the schemas, the manifest-owned files and each consumer's own validations. A plugin installs per user, into a harness. If the plugin carried the supervisor, a repository could run code at the plugin's version while its queue and schemas sat at the synced version, and nothing would say which one was in charge. Keeping the plugin code-free removes that split. One version of the supervisor runs per repository, and the sync script and its pull request set it.

## What one plugin directory holds

All three harnesses read the same `skills/<name>/SKILL.md` layout. Each wants its own manifest. These were checked against the versions installed on the owner's machine on 2026-11-01:

| Harness | Manifest | Install |
| --- | --- | --- |
| Claude Code | `.claude-plugin/plugin.json`, plus `.claude-plugin/marketplace.json` listing the plugin | `claude plugin marketplace add raelldottin/repo-automation`, then `claude plugin install` |
| Codex 0.166 | `.codex-plugin/plugin.json` with `"skills": "./skills/"` | `codex plugin marketplace add raelldottin/repo-automation@<tag>`, then `codex plugin add` |
| Hermes (`~/.hermes/hermes-agent` at `31bde0d`) | `plugin.json` at the root in the portable **Agent Plugins v1** format (`$schema` `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`) | `hermes plugins install raelldottin/repo-automation`, then `hermes plugins enable`; Hermes installs portable packages disabled |

Hermes's native `plugin.yaml` and `register(ctx)` format is not needed. Agent Plugins v1 discovers `skills/` with no Python, and Hermes already ignores `.claude-plugin/` and `.codex-plugin/` during discovery. Superpowers ships the same Claude and Codex manifests side by side in one repository.

The plugin root is the repository root, because `hermes plugins install <owner/repo>` reads `plugin.json` there. The repository gains `plugin.json`, `.claude-plugin/`, `.codex-plugin/` and `skills/` at the top level. Nothing in `automation/` moves.

## Skills

Each skill first finds the synced copy at `<repository>/automation/`. If there isn't one, it stops and says that the repository has not adopted repo-automation. It never falls back to anything bundled.

| Skill | Does | Writes |
| --- | --- | --- |
| `repo-automation-status` | Loads the queue with `policy.load_queue`, names the next slice with `policy.select_next_slice`, and, given a board and overlay, prints the Kanban projection's shadow diff | nothing |
| `repo-automation-run-next` | Runs `python3 automation/supervisor/run_next.py` **once** with the flags the user gives, then reports the handoff path and the stop reason | what `run_next` writes, under its own gates |
| `repo-automation-evaluate-handoff` | Runs `automation/supervisor/evaluate_handoff.py` on a handoff the user names | nothing |
| `repo-automation-sync` | In a consumer repository, runs `Tools/repo-automation-sync.sh` against a released tag or pinned commit, with `--check` first | only after the user says go, and then only through a branch and a pull request in that consumer |

## Rules the plugin must keep

1. **Only the supervisor decides to continue.** `repo-automation-run-next` runs one slice and stops. It does not loop, schedule itself or chain to the next slice. That is the existing rule that no worker or sub-agent may start the next slice.
2. **The runner rule stands.** `run_next` already picks the runner from the nearest enclosing harness and refuses a mismatch (raelldottin/repo-automation#68). The plugin never sets `REPO_AUTOMATION_AGENT_RUNNER`, so Claude Code runs Claude, Codex runs Codex and Hermes runs Hermes.
3. **No hooks, MCP servers or daemons.** Nothing runs at session start or on a tool call, and there is no server process. An MCP server would edge toward the "no coordination daemon" rule in SPEC-001 and would need its own design decision.
4. **No credentials.** The plugin holds no keys. The Hindsight and Kanban flags of `run_next` keep reading their credentials the way they do now.
5. **Consumers still change only through a pull request.** `repo-automation-sync` is the existing sync script with a confirmation in front of it. It never commits to a consumer's default branch.

## Versions and releases

The plugin's version is the release tag. A release is still a `vX.Y.Z` tag on a commit already on `main`, built by `release.yml`. All three marketplaces install from the GitHub repository at a ref, so no new publishing workflow is needed and nothing is uploaded anywhere else. The three manifests must carry the same `name` and `version`, and a unit test enforces that.

The plugin and a consumer's synced copy can be at different versions. The skills read only stable command-line interfaces (`run_next.py`, `evaluate_handoff.py`, `kanban_projection.py`, the sync script) and report the synced copy's version when one is recorded. A skill that needs a flag the synced copy lacks says so and stops.

## License

The owner chose **the Linux kernel's license**: the GNU General Public License, version 2 only, SPDX `GPL-2.0-only`. The kernel adds a `Linux-syscall-note` exception for user-space programs that call the kernel. It concerns kernel interfaces, so it is not carried over. Agents do not choose or change licenses. This section records the decision and its consequences for the owner.

- **Scope to confirm.** The plugin is the repository root, so its license is the repository's license. This spec assumes the decision covers all of repo-automation. The owner should confirm that before the license pull request lands.
- **Synced files carry it into consumers.** `Tools/repo-automation-sync.sh` copies `automation/` into each consumer, Owlory included. Those files would be `GPL-2.0-only` in every consumer. A consumer that distributes them, or a program built on them, takes on GPL-2.0 obligations for that work. Before the license lands, the owner should check each consumer's own license against this.
- **Inbound code is compatible today.** The only runtime dependency, `pydantic`, is MIT. The concepts adapted from `mattpocock/skills` in `THIRD-PARTY-NOTICES.md` are MIT. Hindsight and Hermes are MIT and are invoked, not bundled. MIT is GPL-2.0-compatible.
- **It limits what can be added later.** Apache-2.0 code and GPLv3-only code cannot be incorporated into a `GPL-2.0-only` work. Invoking such a program as a separate process, as `run_agent.sh` invokes Codex (Apache-2.0), is unaffected. A future dependency review should check licenses. `dependency-review.yml` can take a license allow-list, and changing that required check needs its own pull request.
- **The other project is separate.** `hermes-swarm-protocol` still has no license. This decision does not cover it.
- **Where it gets written.** One pull request adds `COPYING` (the GPL-2.0 text), `license = "GPL-2.0-only"` in `pyproject.toml`, a license section in `README.md`, and `"license": "GPL-2.0-only"` in the plugin manifests when they exist. The copyright line names the owner. That pull request is reviewed by the owner and carries no other change.

## Rollout

Each step is its own pull request through the required checks.

1. This spec.
2. The license pull request described above.
3. `AGENTS.md`: name the plugin as a supported front end in "Consumer Repositories". State that it carries no supervisor code and that consumers still change only through the sync script and a pull request. Agent-instruction files go in their own pull request.
4. The plugin: the three manifests, the four skills and two tests. One test checks that every manifest parses and that `name` and `version` match the tag format. The other checks that every skill has the frontmatter all three harnesses require. Install it locally in each harness and record the result. `hermes plugins validate` and `claude plugin validate` check the manifests.
5. A release tag by the maintainer, then a canary: install in one harness, then run `repo-automation-status` and `repo-automation-run-next` against one consumer.

## Performance

The plugin adds Markdown and JSON only. It changes no registered performance-critical path. The skills call the same `policy.load_queue`, `policy.select_next_slice` and `kanban_projection` code that the existing benchmarks cover.

## Open questions for implementation

- Where Codex expects a marketplace file in a repository added with `codex plugin marketplace add`. Check it against the installed CLI rather than assume Claude's layout.
- Whether `hermes plugins install` can take a subdirectory. If it can, the plugin could live in `plugin/` and keep the repository root uncluttered.
- Whether Claude Code's skill frontmatter, Codex's and Agent Plugins v1's agree on required fields. The `SKILL.md` test should enforce whatever set satisfies all three.
