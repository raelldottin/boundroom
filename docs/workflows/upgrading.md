# Upgrading, version skew and rolling back

This page is the product roadmap's RA-PROD-106. A consumer runs three pieces of Boundroom, each pinned separately, and they can be at different versions. It explains how to see which versions you have, which combinations work, how to move to a new release and how to go back.

## The three pieces

| Piece | Where its version comes from | How to see it |
| --- | --- | --- |
| **The synced copy** in the consumer: `automation/supervisor/`, the schemas, the prompts and the other manifest-owned files | The commit it was synced from, recorded in the consumer's lock (`automation/boundroom.lock`, or `automation/repo-automation.lock` in a consumer that adopted before 0.3.0) | The lock's `commit`, compared with `git -C <boundroom-checkout> rev-parse '<tag>^{commit}'` |
| **The plugin** in each person's Claude Code, Codex or Hermes | The release tag its marketplace was added from | `claude plugin list`, which shows the plugin name and version |
| **The canonical checkout** you sync from | The tag or commit you checked out | `git describe --tags` in that checkout |

The synced copy is the authority, as [SPEC-002](../architecture/agent-plugin.md) says: the plugin's skills run whatever copy the repository has. So the plugin and the copy can differ, and the skills are written to cope.

## Which combinations work

| Plugin | Synced copy | What happens |
| --- | --- | --- |
| Newer than the copy | Older | Each skill checks for what it needs and stops with a clear message instead of guessing: `boundroom-run-next` stops if `run_next.py --help` doesn't list `--single-slice`, and the settings skill proposed in SPEC-008 stops if `settings_view.py` isn't there. Sync the copy forward. |
| Older than the copy | Newer | The skills are written to keep working: a release that removes a flag or behavior an earlier skill relies on is a minor release and says so in its notes. A newer copy's additions are simply unused until the plugin is updated. |
| `repo-automation@repo-automation` (before 0.3.0) | 0.3.0 or later | Works. 0.3.0 kept the old names working (see [SPEC-005](../architecture/boundroom-rename.md)). Move the plugin when convenient. |
| `boundroom@boundroom` (0.3.0 or later) | Before 0.3.0 | Works for the skills both versions share. The copy ignores `BOUNDROOM_*` variables, so set the old `REPO_AUTOMATION_*` names or, better, sync the copy forward. |

A **stale pin** is a lock whose `commit` is older than the latest release. It isn't an error, and nothing updates it automatically: every sync starts from a person's request (AGENTS.md, Consumer Repositories).

## Moving to a new release

1. **Read the release notes** for every tag between your pin and the target. A minor release (`0.x.0`) may rename or remove something; a patch release doesn't.
2. **Check out the tag** in a clean Boundroom checkout and take its full commit SHA.
3. **Check, then sync, through a pull request** in the consumer, with the plugin's `boundroom-sync` skill or the same commands by hand. `--check` first; `--sync` only after a person says go; the lock's `commit` and `imported_at` updated in the same commit, and nothing else in the lock.
4. **Run the consumer's own check** (Owlory: `make repo-automation-check`) and let its required checks run on the pull request. The consumer's owner approves and merges it.
5. **Update the plugin** to the same tag, if you use it: change the marketplace source to the new tag, then update the plugin. Only a rename, as in 0.3.0, needs an uninstall and reinstall.

### From 0.2.x to 0.3.0 (the rename)

0.3.0 renamed the product from repo-automation to Boundroom. Everything a consumer already depended on keeps working under its old name in 0.3.0:

- `Tools/repo-automation-sync.sh` is a stub that runs `Tools/boundroom-sync.sh`, so a consumer `Makefile` that names the old path keeps working.
- `REPO_AUTOMATION_*` variables are read when their `BOUNDROOM_*` names are unset.
- The supervisor still exports `REPO_AUTOMATION_SUPERVISOR_*` to agents.
- The consumer's lock keeps its old name, `automation/repo-automation.lock`. Renaming it is the consumer owner's change.

Two steps need a person:

- **The consumer's lock `remote`.** The repository is now `raelldottin/boundroom`, and the sync compares `--expect-remote` with the source's origin exactly, so a lock that still names `raelldottin/repo-automation` is refused. The consumer's owner updates that field; agents may not.
- **The plugin.** `repo-automation@repo-automation` keeps working at its old tag. To move: `claude plugin uninstall repo-automation@repo-automation`, `claude plugin marketplace remove repo-automation`, `claude plugin marketplace add raelldottin/boundroom@v0.3.0`, `claude plugin install boundroom@boundroom`. SPEC-005 lists the Codex and Hermes steps.

## Rolling back

A rollback is an ordinary sync to an older tag, through the same pull request path:

1. Check out the previous tag in a clean Boundroom checkout and take its full SHA.
2. `--check`, then `--sync` against the consumer on a new branch, and set the lock's `commit` back to that SHA.
3. Run the consumer's check and land the pull request after its owner approves.

Rolling back across 0.3.0 removes `Tools/boundroom-sync.sh` from the manifest, but the consumer keeps the file: the sync never deletes a single-file entry. That is harmless, and the consumer's owner may remove it.

For the plugin, add the marketplace from the older tag and update or reinstall the plugin. Released tags and their assets are never moved or edited, so an older tag always means the same files (see `docs/workflows/boundroom.md`, Releases).

## What to report when something doesn't match

If a skill stops with "the synced copy is too old", or the consumer's check reports drift you didn't expect, report:

- the lock's `commit`;
- the plugin's version from `claude plugin list`;
- the tag you meant to sync;
- the output of `Tools/boundroom-sync.sh --check` against that tag.

Those four facts are enough to tell skew from a real defect.
