# CodSpeed removal roadmap

Status: **done in the repository** (2026-11-01). Steps 1 to 3 have landed. Step 4, the CodSpeed GitHub App and codspeed.io project, is the owner's. This page is kept as the record of why and how CodSpeed was removed.

## Why

CodSpeed measures the CPU cost of the six registered performance-critical paths in `AGENTS.md`. Those paths run in milliseconds. A single phase of the effectiveness benchmark spends minutes waiting on the model, so a regression CodSpeed could catch would not be visible in any real run. CodSpeed is required only by policy: the `main` ruleset lists it as a required check, and `AGENTS.md` makes registered-path coverage a hard rule. Outside CodSpeed's own files listed below, no code, test, release or consumer depends on it.

It costs a CI job on every pull request, a check that can block merges, and a benchmark for every new hot path. Its simulation mode also leaves out system-call time: on raelldottin/repo-automation#133, CodSpeed itself warned that three of the six benchmarks are dominated by system calls and that the reported values understate their real cost.

Correctness stays covered by the unit tests and `make reusable-check`. Agent effectiveness is measured by the [effectiveness benchmark](workflows/effectiveness-benchmark.md), which CodSpeed never measured.

## What CodSpeed touches

| Where | What |
| --- | --- |
| `main` ruleset | `Run CodSpeed benchmarks` is one of six required checks |
| `AGENTS.md` | "Performance Evidence and CodSpeed" section and its registry; the GitOps section's list of six required checks |
| `.github/workflows/codspeed.yml` | The job, with `id-token: write` for CodSpeed's OIDC login |
| `automation/tests/test_benchmarks.py` | Six benchmarks; each also asserts its result. `make reusable-check` runs them as ordinary tests (`pytest automation/tests`); the coverage workflow does not, because it runs only `test_harness.py` |
| `pyproject.toml`, `uv.lock` | `pytest-codspeed==5.0.3`, which provides the `benchmark` fixture |
| `README.md` | Badge and the CodSpeed bullet under CI |
| `docs/roadmap.md` | "Performance" under Existing work retained and the P1 exit evidence (current rules); the RA-CP-002 and RA-CP-003 acceptance text (records of finished slices) |
| `docs/architecture/*.md` | Statements that a path is, or is not, registered for CodSpeed (control-plane, kanban-projection, kanban-cutover, memory-interface, swarm-wiki-compiler) |
| `docs/workflows/effectiveness-benchmark.md` | Its opening line contrasts itself with "the CodSpeed micro-benchmark" |
| CodSpeed GitHub App, codspeed.io project | Outside the repository |

Consumers are unaffected: `automation/reusable-manifest.json` distributes neither the workflow nor `test_benchmarks.py`, and `release.yml` reruns `make reusable-check`, not CodSpeed. The `release tags` ruleset requires no status checks, and no code or test reads the check's name.

## Steps

The order matters. Deleting the workflow while its check is still required would block every pull request, because a required check that never reports never passes.

| Step | Who | Change | Done when |
| --- | --- | --- | --- |
| 1 | Owner | Remove `Run CodSpeed benchmarks` from the `main` ruleset's required checks. Agents may not weaken a required check, so this is the owner's step. | `gh api repos/raelldottin/repo-automation/rulesets/25254426` lists five required status checks; the CodSpeed job still runs on pull requests without blocking them |
| 2 | PR, `AGENTS.md` only | Replace "Performance Evidence and CodSpeed" with a short rule: a change that adds file, network or subprocess work to the supervisor, policy, validation or context-building paths says so in its pull request, and the reviewer decides whether it needs a measurement. Drop the registry. Change the GitOps list to the five remaining required checks. Land it soon after step 1, because until then `AGENTS.md` names a check the ruleset no longer requires. | Merged with its required checks passing; `AGENTS.md` is the only file in the diff |
| 3 | PR | Delete `codspeed.yml`. For each of the six benchmarks, keep its assertion as an ordinary test unless an existing test already makes it, then delete `test_benchmarks.py`. Remove `pytest-codspeed` and refresh `uv.lock`. Remove the README badge and bullet. Rewrite the current-rule mentions in `docs/roadmap.md`, the architecture pages and `effectiveness-benchmark.md`; leave the RA-CP-002 and RA-CP-003 acceptance text as the record of what those slices required. Mark this page done. | All required checks pass; the pull request maps each of the six assertions to the test that now makes it; `git grep -in codspeed` matches only this page, its `mkdocs.yml` nav entry, any link to this page, and the RA-CP-002 and RA-CP-003 acceptance text in `docs/roadmap.md`; `git grep -in performance-critical` matches only this page and that same acceptance text |
| 4 | Owner | Remove this repository from the CodSpeed GitHub App's access, or uninstall the App if no other repository uses it. Optionally delete the codspeed.io project, which also deletes its measurement history. | The App's settings no longer list this repository |

Steps 2 and 3 are separate pull requests because agent-instruction files never ride along with code changes.

## Rolling back

Every step can be undone: give the App access again if step 4 removed it, revert the pull requests from steps 2 and 3, then put the check back in the ruleset. Do it in that order, because a required check that cannot report blocks every pull request. Keeping the codspeed.io project keeps the measurement history; deleting it means the restored benchmarks start without a recorded baseline.

## Out of scope

This roadmap removes a performance counter. It does not change the effectiveness benchmark, its workflow or its authorization rules, and it does not change the other five required checks.
