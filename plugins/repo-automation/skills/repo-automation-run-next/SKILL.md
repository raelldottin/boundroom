---
name: repo-automation-run-next
description: Run exactly one queued slice through this repository's repo-automation supervisor, then stop and report the handoff and the stop reason. Use when asked to run the next slice, advance the queue by one, or start supervised work in a repository that has adopted repo-automation.
license: GPL-2.0-only
---

# Run the next slice

This skill drives the supervisor that `Tools/repo-automation-sync.sh` copied into the repository you are working in. That copy, its queue and its gates decide what runs. This skill only starts it.

## Before you run anything

Run each check as its own command. Don't join them with `&&`, `;` or a pipe: a harness with a narrow command allowlist can refuse the combined form.

1. Work from the repository root. Run `test -f automation/supervisor/run_next.py`. If it fails, stop and say: "This repository has not adopted repo-automation."
2. Run `python3 automation/supervisor/run_next.py --help` and read its output for `--single-slice`. If it isn't listed, stop and say the synced copy is too old for this plugin and needs a sync to a newer repo-automation release.

## The one supported command

```bash
python3 automation/supervisor/run_next.py --single-slice
```

You may append these flags when the user asks for them: `--dry-run`, `--include-blocked`, `--queue <path>`, `--handoff-dir <path>`, `--kanban-board <dir> --kanban-overlay <file>`, `--memory-config <file> --memory-project <name>`.

Never add `--autonomous-limit`, in any form. With `--single-slice`, the supervisor refuses it with exit 64 and runs nothing. Don't work around that refusal; report it.

## After it returns

Report:

- the exit code;
- the slice it ran, from the JSON decision report on stdout;
- the decision and stop reason;
- the handoff file under `automation/handoffs/`.

Then stop. Do not run the command again, start the next slice, schedule a later run, or loop. Only the supervisor decides whether work continues, and with `--single-slice` it has already decided to stop. If the user wants another slice, they invoke this skill again.

## What this skill does not guarantee

These instructions guide you; they don't enforce anything. The enforcement is `run_next.py --single-slice` itself. A plain `run_next.py` without the flag can run several slices, up to the queue's `consecutive_autonomous_limit`, and a caller can raise that limit. Don't run it that way from this skill.
