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

## 3. Update the consumer's lock, if it keeps one

The script never writes `automation/repo-automation.lock`. It refuses that destination, because the consumer's `make` reads the pin from it and runs what the pin names. A consumer that keeps a lock expects the new pin committed together with the synced files.

If the lock exists, show the user the change: `commit` becomes the full SHA you passed as `--pin`, and `imported_at` becomes today's date in the format the file already uses. Change those two fields only, and only after the user says yes. Then run the consumer's own check, such as `make repo-automation-check`, and report what it says.

## 4. Land it through a pull request

In the consumer, commit the synced files, and the lock if you updated it, on the new branch and open a pull request against its default branch. Name the repo-automation tag or commit in the description. Don't merge it; that is the consumer owner's decision. Don't push to the consumer's default branch.

Synced files include `automation/COPYING`, the GPL-2.0 text that covers them. Leave it in place.
