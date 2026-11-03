---
name: repo-automation-sync
description: Update a consumer repository's synced copy of repo-automation from a released tag or pinned commit, through the existing sync script and a pull request in that consumer. Checks for drift first and changes nothing without the user's go-ahead. Use when asked to adopt, update or sync repo-automation in a repository.
license: GPL-2.0-only
---

# Sync a consumer

repo-automation reaches a consumer only through `Tools/repo-automation-sync.sh`, from a released tag or a pinned commit, and the result lands through a pull request in the consumer. This skill follows that path. It never delivers files any other way.

## What you need

- A clean local checkout of repo-automation, at the release tag or commit the user names. This is the source. Run the script from that checkout, not from the consumer's copy.
- The full 40-character commit SHA of that tag or commit.
- The consumer repository's path, on a new branch, never its default branch.

If any of these is missing or unclear, ask.

## 1. Check first

From the repo-automation checkout:

```bash
Tools/repo-automation-sync.sh --check --target <consumer-path> --pin <full-sha> --expect-remote https://github.com/raelldottin/repo-automation.git
```

`--check` reports drift and changes nothing. The script refuses a dirty source, a source at a different commit than the pin, a source with the wrong origin, or a source that fails its own quality gate. Report what it says. Don't bypass a refusal, and never use `--allow-unverified-source` here.

## 2. Sync only after the user says go

Show the user the drift report and wait for an explicit yes. Then run the same command with `--sync` in place of `--check`.

## 3. Update the consumer's lock pin, if it keeps one

The script never writes `automation/repo-automation.lock`. It refuses that destination, because the consumer's `make` reads the pin from it and runs what the pin names. A consumer that keeps a lock checks its vendored files against that pin before a push, so the sync can't be pushed under the old pin.

If the lock exists, show the user the change: `commit` becomes the full SHA you passed as `--pin`, and `imported_at` becomes the import time, in the format the file already uses. Make the change only after the user says yes. Change those two fields and nothing else; never `remote` or `repository`. Then run the consumer's own check, such as `make repo-automation-check`, and report what it says.

## 4. Land it through a pull request

In the consumer, commit exactly the synced files, and the lock if you updated it, in one commit on the new branch and open a pull request against its default branch. Name the repo-automation tag or commit in the description. Don't push to the consumer's default branch.

Merging is the consumer owner's decision. Report the pull request's number, its head commit and its checks, and say which failing checks are not required. Merge it only if the owner then approves merging that pull request by number, and only once its required checks have passed. Pin the merge to the approved head with `gh pr merge <number> --match-head-commit <sha>`, use a merge method the consumer allows, and never use an admin override. If the head changes after the approval, ask again.

Synced files include `automation/COPYING`, the GPL-2.0 text that covers them. Leave it in place.
