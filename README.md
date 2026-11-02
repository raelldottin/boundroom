# repo-automation

Reusable supervised automation for bounded agent work.

**Architecture direction (proposed):** The proposed top layer is a canonical **Hermes Kanban board**, **self-hosted Hindsight external project memory**, and a generated **Swarm Wiki** powered by [hermes-swarm-protocol](https://github.com/raelldottin/hermes-swarm-protocol). The existing supervisor stays beneath this layer and retains sole authority over slice execution and continuation. See [SPEC-001](docs/architecture/control-plane.md) and the [sequenced roadmap](docs/roadmap.md). These are target contracts, not yet implemented integrations. The existing Swarm Wiki requires an upstream source-adapter migration before it can read Hindsight data. Both project repositories are now `GPL-2.0-only` (this repository's license is below). The fully open-source, local-inference profile still requires audited model/dependency licenses.

`repo-automation` packages a slice-based supervisor harness: queue records, context building, prompt fragments, validation ownership, handoff schemas, and stop rules for keeping AI-assisted repository work scoped and reviewable.

## What It Provides

- A machine-readable queue model for pre-classified implementation slices.
- A context builder that gives each run only the slice metadata and nearby evidence it needs.
- Supervisor policy checks for dependencies, allowed paths, diff budgets, and validation ownership.
- JSON handoff contracts that preserve what changed, what passed, and what risk remains.
- Prompt fragments for fresh slice runs and manual review.
- Sync tooling for keeping this reusable package current while extraction from Owlory continues.

## What It Does Not Provide

- Repo-specific product rules, build commands, or release policy.
- A live work queue for your project.
- Silent commits or remote pushes for consumer repositories.
- Recursive self-spawning from inside an agent run.

Each consuming repository owns its queue, validation commands, domain docs, handoff history, and local safety policy. The reusable policy expects each task to finish with a clean GitHub stop: all touched repositories committed, pushed to their GitHub upstreams, locally clean, and even with upstream. Consumers may decide how to automate that requirement, but they should not claim a clean stop until `git status --short` is empty and `git rev-list --left-right --count HEAD...@{u}` returns `0 0`.

## Layout

- `automation/README.md` - detailed harness model and operating rules.
- `automation/context/` - bounded context bundle generation.
- `automation/examples/` - example queue and handoff payloads.
- `automation/prompts/` - base, slice, and review prompt fragments.
- `automation/schemas/` - Pydantic contract models for queues and handoffs, plus the JSON Schemas generated from them.
- `automation/supervisor/` - queue selection, stop policy, validation replay, and agent launch orchestration.
- `automation/tests/` - harness regression tests.
- `docs/workflows/repo-automation.md` - extraction, sync, and consumer-adoption contract.
- `Tools/repo-automation-sync.sh` - manifest-owned sync/check/update tool.

## Quick Start

Clone the package:

```bash
git clone https://github.com/raelldottin/repo-automation.git
cd repo-automation
```

Run lint and the standalone type check:

```bash
uv run ruff check .
uv run ty check automation/ Tools/
```

Build context for the example queue:

```bash
python3 automation/context/build_context.py \
  --queue automation/examples/example-slices.json \
  --slice-id today-continue-ui-regression-coverage
```

For real adoption, copy the example queue shape into your repository, replace the example slices with project-specific work, and set validation commands that actually exist in that repository.

## Adoption Checklist

1. Add `automation/queue/slices.json` for your repository.
2. Add `automation/handoffs/` for run artifacts.
3. Define allowed paths, validation commands, and proof levels for each slice.
4. Point `agent_command_template` at your local agent launcher.
5. Run `python3 automation/supervisor/run_next.py --dry-run` before launching a real slice.

## Continuous Integration

The GitHub Actions configuration in `.github/` includes:

- **Lint** (`.github/workflows/lint.yml`) — `uv`-driven `ruff check`,
  `ruff format --check`, and `ty check` across `automation/` and `Tools/` on
  Python 3.11 and 3.12. Ruff and ty configuration lives in `pyproject.toml`;
  the toolchain is pinned in `uv.lock`.
- **Coverage** (`.github/workflows/coverage.yml`) — runs `test_harness.py`
  under `pytest --cov`, deselects two Owlory-specific tests that depend on
  live queue state, and comments the report on pull requests via
  `MishaKav/pytest-coverage-comment`.
- **Dependency Check** (`.github/workflows/dependency-check.yml`) —
  `pip-audit` against the full package set exported from `uv.lock` on push,
  PR, and a weekly schedule. Fails when a known CVE matches a locked runtime or
  dev dependency.
- **Dependency Graph Review** (`.github/workflows/dependency-review.yml`) —
  `actions/dependency-review-action` runs on pull requests and fails the PR
  if it introduces a high-severity vulnerability.
- **Publish Documentation** (`.github/workflows/publish-docs.yml`) — builds
  the MkDocs Material site from `mkdocs.yml` + `docs/` on every push to
  `main` (or on `docs/` changes). Deploy step probes the Pages API and
  skips with a notice until Pages is enabled.
- **Dependabot** (`.github/dependabot.yml`) — weekly updates for the
  `github-actions` and `pip` ecosystems with labeled PRs.

### Manual setup that remains

The build artifacts on `main` are green by default once these files land,
but one integration requires a repository-settings action before it goes
live end-to-end:

1. **GitHub Pages**: under **Settings -> Pages**, set **Source** to
   **GitHub Actions**. The publish-docs workflow will then deploy the
   MkDocs site on the next push.

## Agent Plugin

`plugins/repo-automation/` is one plugin for Claude Code, Codex and Hermes, specified in [SPEC-002](docs/architecture/agent-plugin.md). It was first released in `v0.2.0`. It holds four skills and no supervisor code: status, run-next, evaluate-handoff and sync. Each skill drives the repo-automation copy already synced into the repository you work in, and stops if there isn't one. The run-next skill runs `run_next.py --single-slice`, which runs at most one slice.

Install from a release tag, such as `v0.2.1`, not from `main`:

```bash
claude plugin marketplace add raelldottin/repo-automation@<tag>
claude plugin install repo-automation@repo-automation
```

```bash
codex plugin marketplace add raelldottin/repo-automation@<tag>
codex plugin add repo-automation@repo-automation
```

Hermes takes only a full commit SHA, so look up the commit the tag points to first:

```bash
git ls-remote https://github.com/raelldottin/repo-automation.git 'refs/tags/<tag>^{}'
```

```bash
hermes plugins install raelldottin/repo-automation#plugins/repo-automation --ref <commit-sha>
hermes plugins enable repo-automation
```

## Status

This repository is the canonical reusable automation distribution extracted from Owlory. Its existing supervisor harness is present. The P0 Kanban / external-memory / wiki integration is specified but not yet implemented; consumer smoke proof and other supervisor roadmap work follow the P0 acceptance gate. Benchmark/effectiveness work is exempt from that ordering and continues in parallel under explicit owner authorization. Treat project-specific examples as templates, not production configuration.

## License

repo-automation is free software under the GNU General Public License, version 2 only (`GPL-2.0-only`). The full text is in [`COPYING`](COPYING). It comes with ABSOLUTELY NO WARRANTY, as `COPYING` explains.

Copyright (C) 2026 Raell Dottin.

The license covers the original code and documentation in this repository. That includes every file `automation/reusable-manifest.json` distributes to consumer repositories, among them `Tools/repo-automation-sync.sh` and `docs/workflows/`. A sync places the license text beside them as `automation/COPYING`, and the release archive carries it too. Synced files keep this license inside a consumer repository. The license of the consumer's own code is the consumer's decision.

Material adapted from third parties keeps its original license and attribution; see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
