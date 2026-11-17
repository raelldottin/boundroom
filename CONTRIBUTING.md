# Contributing to Boundroom

Boundroom is a supervisor for bounded agent work. Its own changes follow the controls it asks of others. Every change reaches `main` through a pull request, and a person other than its author approves it. This guide says how to make a change that can be approved. [`AGENTS.md`](AGENTS.md) is the full rule set, and the [control map](docs/compliance/controls.md) says how each rule is held and what evidence it leaves.

## Before you start

- **Read the specification for the area you're changing.** The specs in [`docs/architecture/`](docs/architecture/) hold the decisions. A change that contradicts one changes the spec first, in its own reviewed pull request.
- **Open an issue first for anything larger than a fix.** Say what problem it solves, and which spec and slice it belongs to. Small fixes and documentation corrections can go straight to a pull request.
- **Some decisions belong to the owner, not to a pull request:** the license, spending (benchmark runs, models, providers, budgets), release tags, cutovers and deployments, and making a check required. A pull request can prepare one of these with evidence, but a person makes the decision as a separate recorded action.

## Set up

You need Python 3.11 or later, Git and [uv](https://docs.astral.sh/uv/). Then, from the repository root:

```bash
uv sync
make reusable-check
```

`make reusable-check` is the quality gate. CI runs it on every pull request, a consumer's sync runs it before importing, and `release.yml` runs it before publishing. It runs ruff, ruff's format check, ty, the schema check, the settings reference check and the tests.

### Optional: pre-commit hooks

[`.pre-commit-config.yaml`](.pre-commit-config.yaml) runs the same checks before code leaves your machine. Every hook is local and runs this repository's own pinned tools through `uv` and `make`, so pre-commit downloads no hook code. With [pre-commit](https://pre-commit.com/) installed:

```bash
pre-commit install
```

That installs a pre-commit hook, which runs ruff on the files you commit, and a pre-push hook, which runs the attribution-trailer check on the commits you push and `make reusable-check` on your working tree. The gate sees uncommitted edits too, so commit or stash them before you push. The hooks are a convenience. CI's result on the pull request is what counts.

## Make the change

- **Keep it to one purpose.** One slice, one fix or one spec change per pull request.
- **Test what you change.** Code changes come with tests in `automation/tests/`. A test for a rule should fail when the rule is broken. Where it helps, say in the pull request how you checked that.
- **Regenerate what is generated.** After changing a schema model, run `uv run python -m automation.schemas.generate`. After changing a setting, run `uv run python automation/supervisor/settings_view.py --docs`. The gate fails if either is stale.
- **Check the docs build** when you change `docs/` or `mkdocs.yml`: `uv run mkdocs build --strict`.
- **Follow the [compatibility policy](docs/reference/compatibility.md)** for anything a consumer depends on: settings, flags, exit codes, schemas, the decision report, the sync and telemetry. A new exit code also goes in the [exit-code reference](docs/reference/exit-codes.md), and a test checks it.
- **Add a changelog line** under **Unreleased** in [`CHANGELOG.md`](CHANGELOG.md) when a consumer would notice the change.
- **Say so if the supervisor gets slower.** A change that adds file, network or subprocess work to the supervisor, policy, validation or context-building paths, or makes their cost grow with queue size, handoff count, document size or repository size, must say so in the pull request description. The reviewer decides whether it needs a measurement.

## Commit

- Write the subject as `type(scope): what changed`, for example `fix(sync): refuse a symlinked manifest`. Recent history shows the types in use.
- **No agent attribution trailers.** The required check `No agent attribution trailers` fails a pull request with a commit that carries `Co-Authored-By: ... Claude ...` or `Claude-Session:`. Run `Tools/check-commit-trailers.sh` to check your branch before you push. An agent's authorship is shown by the identity that makes the commit, not by a trailer.

## Open a pull request

The [pull request template](.github/pull_request_template.md) asks for what a reviewer needs: what changed and why, what a consumer sees, the gate's result, the tests, and any slower supervisor path. Fill in each section, or say why it doesn't apply.

These checks must pass before a merge. The `main` ruleset requires them:

- `make reusable-check (3.11)` and `make reusable-check (3.12)`
- `Tests + coverage comment`, with an 88% coverage floor
- `No agent attribution trailers`
- `Review dependency graph diff`

CI also runs the gate on Python 3.13, 3.14 and macOS, and runs `pip-audit`. Those aren't required, but a reviewer will ask about a failure.

## Review and merge: segregation of duties

- **The author never approves their own change.** A pull request needs an approving review from a person other than its author, and from a code owner ([`.github/CODEOWNERS`](.github/CODEOWNERS)). GitHub enforces this, and it doesn't count an author's own review.
- **An approval covers one head commit.** A push dismisses earlier approvals, and the new head needs approving again.
- **Merge only with the required checks green on the approved head.** Pull requests merge by squash or rebase, because `main` keeps a linear history. Nobody pushes to `main` directly, force-pushes it or deletes it, and the ruleset has no bypass.
- **Nobody weakens a control to make progress.** That includes disabling or editing a check so it passes without doing its work.

### Agents

An agent can write code, tests and docs, run the checks and open pull requests. It can't approve anything. On GitHub it acts as `boundroom-agent[bot]`, a GitHub App with no administration permission, and never with a person's credentials. Outside GitHub, it names itself as the actor, for example `--actor "claude-code (for <person>)"`. After a person approves a pull request, the agent may merge it, pinned to the approved head:

```bash
gh pr merge <number> --squash --match-head-commit <approved-sha>
```

If the head changes after the approval, the agent asks for a new approval. The full rules are in [`AGENTS.md`](AGENTS.md#agent-authority-and-human-approval-cis-and-soc-2-controls-2026-11-10).

## Releases and consumer syncs

A person creates each release tag, or approves a named tag on a named commit, after `python3 Tools/check_plugin_version.py <tag>` passes and the commit's required checks are green. `release.yml` builds, attests and publishes the archive from the tag. Nobody hand-builds or edits a release asset.

Consumers receive changes only through `Tools/boundroom-sync.sh` and a pull request in the consumer repository, which the consumer's owner approves. See [`AGENTS.md`](AGENTS.md#consumer-repositories).

## Security

Report a suspected vulnerability privately, as [`SECURITY.md`](SECURITY.md) describes, not in an issue or pull request.

## Conduct

Boundroom hasn't adopted a code of conduct yet. Choosing its text, including how to report a problem, is the owner's decision ([SPEC-009](docs/architecture/enterprise-readiness.md), RA-ENT-012).

## License

The repository carries the GNU General Public License, version 2 only (`GPL-2.0-only`), in [`COPYING`](COPYING). The license, and any terms for contributions such as a sign-off, are the owner's decisions ([SPEC-009](docs/architecture/enterprise-readiness.md), RA-ENT-015). A pull request doesn't choose or change them.
