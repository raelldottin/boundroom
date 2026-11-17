<!--
Fill in each section, or write why it doesn't apply. CONTRIBUTING.md explains each one.
Report a vulnerability privately, as SECURITY.md describes, not in a pull request.
-->

## What and why

<!-- What this changes, the problem it solves, and the spec, slice or issue it belongs to. -->

## What a consumer sees

<!--
Anything a consumer depends on that changes: settings, flags, exit codes, schemas, the decision
report, the sync or telemetry (docs/reference/compatibility.md). Name the CHANGELOG.md line under
Unreleased, or write "Nothing a consumer sees".
-->

## Evidence

- [ ] `make reusable-check` passes locally. Python version:
- [ ] Tests added or changed:
- [ ] Generated files are current (`automation.schemas.generate`, `settings_view.py --docs`), or none changed
- [ ] `uv run mkdocs build --strict` passes, or no docs changed
- [ ] A new exit code, failure reason or decision is in `docs/reference/exit-codes.md`, or there is none

<!-- How you know the change works: a test that fails without it, a canary, a mutation check, a manual proof. -->

## Supervisor cost

<!--
Does this add file, network or subprocess work to the supervisor, policy, validation or
context-building paths, or make their cost grow with queue size, handoff count, document size or
repository size (AGENTS.md, Performance)? Write "No", or say what and where.
-->

## Approval

These must pass before a merge: `make reusable-check (3.11)`, `make reusable-check (3.12)`,
`Tests + coverage comment`, `No agent attribution trailers` and `Review dependency graph diff`.
A person other than the author approves the head commit; a push needs a new approval.

- [ ] No commit carries an agent attribution trailer (`Tools/check-commit-trailers.sh`)
- [ ] This doesn't choose or change a license, spend money, create a release tag, or weaken a check
- Author (a person, or the agent's identity, such as `boundroom-agent[bot]`):
