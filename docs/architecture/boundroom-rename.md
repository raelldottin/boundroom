# SPEC-005: Rename repo-automation to Boundroom

Status: **accepted and released** (raelldottin/boundroom#195, merged 2026-11-11 after the owner's approval; released in [`v0.3.0`](https://github.com/raelldottin/boundroom/releases/tag/v0.3.0) on 2026-11-12). It amends the names in [SPEC-002](agent-plugin.md) and in the [workflow](../workflows/boundroom.md). It doesn't change any rule, gate or control.

Owner decisions recorded here (2026-11-11): the GitHub repository was renamed `raelldottin/boundroom` (raelldottin/boundroom#194). The owner then asked for the product itself to be renamed: the plugin, the tools and the identifiers, not only the URL.

## Decision

1. **The product is Boundroom.** In prose it is *Boundroom*. As an identifier (a package, plugin, path or prefix) it is `boundroom`.
2. **The release that carries the rename is `v0.3.0`.** Renaming the plugin breaks every existing install, so it can't be a patch release.
3. **Everything a consumer or an operator already depends on keeps working under its old name in 0.3.0.** Each old name is either an alias for the new one or a stub that points to it. Removing the old names is a later, separate decision, no earlier than `v0.4.0`.
4. **Some names stay**, because they belong to something other than this repository or are already stored in records. The list is under [What keeps its name](#what-keeps-its-name).

## What changes

| Before | After | Old name in 0.3.0 |
| --- | --- | --- |
| Product name `repo-automation` | Boundroom, `boundroom` | none |
| Plugin `repo-automation@repo-automation` | `boundroom@boundroom` | none, because Claude Code, Codex and Hermes see a new plugin; see [Moving an installed plugin](#moving-an-installed-plugin) |
| `plugins/repo-automation/` | `plugins/boundroom/` | none |
| Skills `repo-automation-status`, `-run-next`, `-evaluate-handoff`, `-sync` | `boundroom-status`, `boundroom-run-next`, `boundroom-evaluate-handoff`, `boundroom-sync` | none; they come with the plugin |
| Marketplace catalogs named `repo-automation` | `boundroom` | none |
| `Tools/repo-automation-sync.sh` | `Tools/boundroom-sync.sh` | `Tools/repo-automation-sync.sh` stays as a stub that runs the new script with the same arguments, and the manifest still delivers it |
| `docs/workflows/repo-automation.md` | `docs/workflows/boundroom.md` | the old path stays as a short page that points to the new one, and the manifest still delivers it, so a consumer's old copy doesn't go stale |
| Environment variables `REPO_AUTOMATION_*` | `BOUNDROOM_*` | `run_agent.sh` reads each old name when its new name is unset; the new name wins when both are set |
| Exported to the agent: `REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE`, `_HANDOFF_FILE`, `_SLICE_ID` | `BOUNDROOM_SUPERVISOR_*` | the supervisor exports both, as it already does for `OWLORY_SUPERVISOR_*` |
| Consumer lock `automation/repo-automation.lock` | `automation/boundroom.lock` for a consumer that adopts from 0.3.0 | the sync script refuses to write either name; an existing consumer keeps its file until its owner renames it |
| Python project `repo-automation` in `pyproject.toml` and `uv.lock` | `boundroom` | none; it isn't published as a package |
| Release asset `repo-automation-<tag>.tar.gz` | `boundroom-<tag>.tar.gz`, from `v0.3.0` | earlier releases keep their asset names; nothing is re-uploaded |
| Docs site name | Boundroom | none |

The benchmark's environment allowlists (`automation/benchmark/adapter.py` and `lanes.py`) list the old names next to the new ones. Otherwise an operator's old settings would be dropped silently, because the allowlist is by exact name.

## What keeps its name

| Name | Why it stays |
| --- | --- |
| The GitHub App `repo-automation-agent` and its bot `repo-automation-agent[bot]` | The owner controls the app's settings. Renaming it changes the bot's login, which the [control map](../compliance/controls.md), the audit trail and the agent's local configuration all name. That is the owner's change, and it can come later. **Done:** renamed to `bound-room-agent`, then to `boundroom-agent` (2026-11-12). The App ID and bot user id are unchanged. |
| Board actors `repo-automation-supervisor` and `repo-automation-board` in `local_board.py` | They are written into each board's event log. Changing them would split one actor's history in two. |
| The benchmark's provenance key `repo_automation_sha` | It is stored in every result file, and comparisons across runs read it. |
| Owlory's `make repo-automation-check` and `make repo-automation-import` | Owlory's `Makefile` defines them, and the sync script never writes a consumer's `Makefile`. Owlory's owner can rename them in an Owlory pull request. |
| Dated records: rollout trials, canaries, evidence and bootstrap history | They describe what ran under the names in use at that time. Rewriting them would make the record inaccurate. |
| Past release tags, assets and notes | A release tag is never moved, and its assets are never edited. |

## Compatibility

A consumer that synced before 0.3.0, and then syncs `v0.3.0`, gets:

- `Tools/boundroom-sync.sh` and `docs/workflows/boundroom.md` as new files.
- `Tools/repo-automation-sync.sh` as the stub. Its `Makefile` keeps working without a change.
- The pointer page in place of its old `docs/workflows/repo-automation.md`.
- A supervisor that still honours its `REPO_AUTOMATION_*` settings and still exports the old names to its agents.

Its lock keeps its old name, and the agent rules for the lock in `AGENTS.md` apply to either name. The sync can't rename or remove a consumer-owned file, and an agent may change only the lock's `commit` and `imported_at`. So renaming the lock, the make targets or the hook belongs in a separate pull request by the consumer's owner.

One step is needed regardless of this spec. The repository rename changed the canonical source's `origin` to `https://github.com/raelldottin/boundroom`. The sync script compares `--expect-remote` with that `origin` as exact strings, so a consumer whose lock `remote` still names `raelldottin/repo-automation` is refused until its owner updates that field. Agents may not edit it.

## Moving an installed plugin

An install of `repo-automation@repo-automation` keeps working at the tag it came from. To move to `boundroom@boundroom` once `v0.3.0` exists:

```bash
claude plugin uninstall repo-automation@repo-automation
claude plugin marketplace remove repo-automation
claude plugin marketplace add raelldottin/boundroom@v0.3.0
claude plugin install boundroom@boundroom
```

The Claude Code commands were checked against Claude Code's `plugin` help. Codex and Hermes aren't approved on the machine this spec was written on, so their steps weren't run:

- **Codex:** remove `repo-automation@repo-automation`, run `codex plugin marketplace add raelldottin/boundroom@v0.3.0`, then `codex plugin add boundroom@boundroom`.
- **Hermes:** run `hermes plugins install raelldottin/boundroom#plugins/boundroom --ref <the tag's full commit SHA>` and `hermes plugins enable boundroom`, then disable `repo-automation`.

The plugin's skills are named `boundroom-*` from then on. The plugin carries no supervisor code, so moving it changes nothing in any repository.

## Rollout

1. This pull request: the rename, the compatibility layer and this spec, with the version set to `0.3.0`. A person other than the author approves it, and the required checks pass at the approved head.
2. A person creates the `v0.3.0` tag on the merge commit, or approves a named tag, after `python3 Tools/check_plugin_version.py v0.3.0` passes. **Done 2026-11-12:** the owner pushed `v0.3.0` on `3524379`, a later commit on `main` that includes #195. The bot's push of the approved tag was refused, because the release-tag ruleset admits only the admin role. `release.yml` published `boundroom-v0.3.0.tar.gz` and its `.sha256`, and the checksum verifies.
3. Each install moves as shown above. The owner's own Claude Code install moves first. **Done 2026-11-12 for the owner's Claude Code:** `boundroom@boundroom` 0.3.0 is installed from the `v0.3.0` marketplace, and `repo-automation@repo-automation` is removed.
4. Owlory: the owner updates the lock's `remote`. Then a sync pull request from `v0.3.0` follows, under the usual consumer rules. Renaming Owlory's lock and make targets, if wanted, is Owlory's own pull request.
5. No earlier than `v0.4.0`, and by a separate decision: remove the old environment names, the stub script and the pointer page.

## Tests

- `test_runner_selection.py`: an old `REPO_AUTOMATION_AGENT_RUNNER` still chooses the runner, and `BOUNDROOM_AGENT_RUNNER` wins when both are set.
- `test_harness.py`: the agent sees `REPO_AUTOMATION_SUPERVISOR_SLICE_ID` as well as the new name.
- `test_boundroom_sync.py`: the sync refuses both lock names as destinations; the old script path runs the new script; the manifest still delivers both script paths and both workflow pages.
- `test_plugin_manifests.py` and `test_check_plugin_version.py` check the renamed plugin's manifests, skills and version.

## Performance

The aliasing is one loop over eleven names in `run_agent.sh`, done once per agent launch. It adds no file, network or subprocess work, and its cost doesn't grow with the queue, the handoffs, the documents or the repository.

## Not in scope

- Renaming the GitHub App, which the owner does in the app's settings.
- Trademark, domain and package-name availability checks for "Boundroom". The owner decides these.
- Any change in Owlory.
- Changing any rule, control or gate. The rename changes names only.
