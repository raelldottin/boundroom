# SPEC-002: One plugin for Claude Code, Codex and Hermes

Status: **proposed design; nothing here is implemented**  
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
| Claude Code | `.claude-plugin/plugin.json`, plus `.claude-plugin/marketplace.json` listing the plugin | `claude plugin marketplace add raelldottin/repo-automation`, then `claude plugin install` |
| Codex 0.166 | The root `plugin.json` below. Per the owner's review of OpenAI's documentation, Codex reads a portable Agent Plugins manifest at the package root, with `.codex-plugin/plugin.json` (`"skills": "./skills/"`) as a compatibility fallback. Its repository marketplace lives at `.agents/plugins/marketplace.json`. | `codex plugin marketplace add raelldottin/repo-automation@<tag>`, then `codex plugin add` |
| Hermes (`~/.hermes/hermes-agent` at `31bde0d`) | `plugin.json` at the root in the portable **Agent Plugins v1** format (`$schema` `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`) | `hermes plugins install raelldottin/repo-automation`, then `hermes plugins enable`; Hermes installs portable packages disabled |

Hermes's native `plugin.yaml` and `register(ctx)` format is not needed. Agent Plugins v1 discovers `skills/` with no Python, and Hermes already ignores `.claude-plugin/` and `.codex-plugin/` during discovery. Superpowers ships the same Claude and Codex manifests side by side in one repository.

The plugin root is the repository root, because `hermes plugins install <owner/repo>` reads `plugin.json` there. The repository gains `plugin.json`, `.claude-plugin/`, `.codex-plugin/`, `.agents/plugins/marketplace.json` and `skills/` at the top level. Nothing in `automation/` moves. Installing from a subdirectory is an optional later optimization, not a prerequisite.

**Marketplaces.** `.agents/plugins/marketplace.json` is the canonical repository marketplace for Codex. Codex also recognizes `.claude-plugin/marketplace.json` as a legacy location, so one catalog may serve both hosts. The plugin pull request installs from each host and records which catalog each one resolved. It doesn't assume they resolve the same way.

## Skills

Each skill first finds the synced copy at `<repository>/automation/`. If there isn't one, it stops and says that the repository has not adopted repo-automation. It never falls back to anything bundled.

| Skill | Does | Writes |
| --- | --- | --- |
| `repo-automation-status` | Loads the queue with `policy.load_queue`, names the next slice with `policy.select_next_slice`, and, given a board and overlay, prints the Kanban projection's shadow diff | nothing |
| `repo-automation-run-next` | Runs `python3 automation/supervisor/run_next.py <user flags> --autonomous-limit 1`, then reports the handoff path and the stop reason. It refuses user flags that contain `--autonomous-limit`, and it appends its own last, so the user can't raise it. | what `run_next` writes, under its own gates |
| `repo-automation-evaluate-handoff` | Runs `automation/supervisor/evaluate_handoff.py` on a handoff the user names | nothing |
| `repo-automation-sync` | In a consumer repository, runs `Tools/repo-automation-sync.sh` against a released tag or pinned commit, with `--check` first | only after the user says go, and then only through a branch and a pull request in that consumer |

## Rules the plugin must keep

1. **One slice per invocation, enforced.** A bare `run_next.py` is a loop. It keeps selecting slices until the queue's `policy.consecutive_autonomous_limit` stops it, so one call can run several slices. The skill therefore always passes `--autonomous-limit 1` and refuses a user-supplied limit. The skill itself does not loop, schedule itself or chain to the next slice. A regression test in the plugin pull request queues two eligible slices, invokes the skill's command once, and asserts that the agent ran exactly once and that the second slice is still `queued`. This is the existing rule that no worker or sub-agent may start the next slice.
2. **The runner rule stands.** `run_next` already picks the runner from the nearest enclosing harness and refuses a mismatch (raelldottin/repo-automation#68). The plugin never sets `REPO_AUTOMATION_AGENT_RUNNER`, so Claude Code runs Claude, Codex runs Codex and Hermes runs Hermes.
3. **No hooks, MCP servers or daemons.** Nothing runs at session start or on a tool call, and there is no server process. An MCP server would edge toward the "no coordination daemon" rule in SPEC-001 and would need its own design decision.
4. **No credentials.** The plugin holds no keys. The Hindsight and Kanban flags of `run_next` keep reading their credentials the way they do now.
5. **Consumers still change only through a pull request.** `repo-automation-sync` is the existing sync script with a confirmation in front of it. It never commits to a consumer's default branch.

## Versions and releases

A release is still a `vX.Y.Z` tag on a commit already on `main`, built by `release.yml`. The manifests carry the same number without the `v`: tag `v0.2.0` means `"version": "0.2.0"` in every manifest. All marketplaces install from the GitHub repository at a ref, so no new publishing workflow is needed and nothing is uploaded anywhere else. A unit test checks that every manifest has the same `name` and a `version` in `X.Y.Z[-prerelease]` form. `release.yml` refuses a tag whose number differs from the manifests'.

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
4. The plugin: the manifests, the marketplace catalogs, the four skills and three tests. The first test checks that every manifest parses and agrees on `name` and `version`. The second checks that every skill has the frontmatter all three harnesses require. The third is the one-slice regression test above. Install it locally in each harness and record the result. `hermes plugins validate` and `claude plugin validate` check the manifests.
5. A release, only with the owner's explicit approval, then a canary: install in one harness, then run `repo-automation-status` and `repo-automation-run-next` against one consumer that has passed the audit.

## Performance

The plugin adds Markdown and JSON only. It changes no registered performance-critical path. The skills call the same `policy.load_queue`, `policy.select_next_slice` and `kanban_projection` code that the existing benchmarks cover.

## Open questions for implementation

- Whether Claude Code's skill frontmatter, Codex's and Agent Plugins v1's agree on required fields. The `SKILL.md` test should enforce whatever set satisfies all three.
- Which marketplace catalog each host resolves when both `.agents/plugins/marketplace.json` and `.claude-plugin/marketplace.json` exist. Record it from a real install.
