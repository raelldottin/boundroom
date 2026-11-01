# CodSpeed removal roadmap

Status: **proposed** (2026-11-01). Nothing here is in effect until each step lands. Until step 2 merges, the Performance Evidence rules in `AGENTS.md` still apply, and `Run CodSpeed benchmarks` stays a required check until the owner removes it in step 1.

## Why

CodSpeed measures how much CPU six supervisor functions use (the registered performance-critical paths in `AGENTS.md`). Those paths run in milliseconds. A benchmark phase spends minutes waiting on the model, so a regression CodSpeed could catch would not be visible in any real run. CodSpeed is required only by policy: the `main` ruleset lists it as a required check, and `AGENTS.md` makes registered-path coverage a hard rule. No code, test, release or consumer depends on it.

It costs a CI job on every pull request, a check that can block merges, and a benchmark for every new hot path. Its simulation mode also leaves out system-call time, and four of the six benchmarks spend most of their time in system calls.

Correctness stays covered by the unit tests and `make reusable-check`. Agent effectiveness is measured by the [effectiveness benchmark](workflows/effectiveness-benchmark.md), which CodSpeed never measured.

## What CodSpeed touches

| Where | What |
| --- | --- |
| `main` ruleset | `Run CodSpeed benchmarks` is one of six required checks |
| `AGENTS.md` | "Performance Evidence and CodSpeed" section and its registry; the GitOps section's list of six required checks |
| `.github/workflows/codspeed.yml` | The job, with `id-token: write` for CodSpeed's OIDC login |
| `automation/tests/test_benchmarks.py` | Six benchmarks; each also asserts its result, and it runs in the ordinary pytest run because `testpaths` is `automation/tests` |
| `pyproject.toml`, `uv.lock` | `pytest-codspeed==5.0.3`, which provides the `benchmark` fixture |
| `README.md` | Badge and the CodSpeed bullet under CI |
| `docs/roadmap.md` | "Performance" under Existing work retained; the P1 exit evidence |
| `docs/architecture/*.md` | Statements that a path is, or is not, registered for CodSpeed (control-plane, kanban-projection, kanban-cutover, memory-interface, swarm-wiki-compiler) |
| CodSpeed GitHub App, codspeed.io project | Outside the repository |

Consumers are unaffected: `automation/reusable-manifest.json` distributes neither the workflow nor `test_benchmarks.py`, and `release.yml` reruns `make reusable-check`, not CodSpeed.

## Steps

The order matters. Deleting the workflow while its check is still required would block every pull request, because a required check that never reports never passes.

| Step | Who | Change | Done when |
| --- | --- | --- | --- |
| 1 | Owner | Remove `Run CodSpeed benchmarks` from the `main` ruleset's required checks. Agents may not weaken a required check, so this is the owner's step. | The ruleset lists five required checks, and the CodSpeed job still runs without blocking merges |
| 2 | PR, `AGENTS.md` only | Replace "Performance Evidence and CodSpeed" with a short rule: a change that adds file, network or subprocess work to the supervisor, policy, validation or context-building paths says so in its pull request, and the reviewer decides whether it needs a measurement. Drop the registry. Change the GitOps list to the five remaining required checks. | Merged with its required checks passing; no other file in the diff |
| 3 | PR | Delete `codspeed.yml`. Move each asserted result in `test_benchmarks.py` to a plain unit test unless an existing test already covers it, then delete the file. Remove `pytest-codspeed` and refresh `uv.lock`. Remove the README badge and bullet. Update `docs/roadmap.md` and the architecture pages so none of them describes a CodSpeed registry, and mark this page done. | `make reusable-check` and `Tests + coverage comment` pass; coverage does not drop; `git grep -i codspeed` matches only this page |
| 4 | Owner | Uninstall the CodSpeed GitHub App from the repository. Optionally delete the codspeed.io project, which also deletes its measurement history. | The App no longer lists the repository |

Steps 2 and 3 are separate pull requests because agent-instruction files never ride along with code changes.

## Rolling back

Until step 4 deletes the codspeed.io project, every step can be undone: revert the pull requests from steps 2 and 3, then put the check back in the ruleset. The recorded baseline survives on codspeed.io, so the first run afterwards is compared against it rather than starting from nothing. Keep the project if you want that option.

## Out of scope

This roadmap removes a performance counter. It does not change the effectiveness benchmark, its workflow or its authorization rules, and it does not change the other five required checks.
