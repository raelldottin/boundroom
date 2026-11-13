---
name: boundroom-run-next
description: Run exactly one queued slice through this repository's Boundroom supervisor, then stop and report the handoff and the stop reason. Use when asked to run the next slice, advance the queue by one, or start supervised work in a repository that has adopted Boundroom.
license: GPL-2.0-only
---

# Run the next slice

This skill drives the supervisor that `Tools/boundroom-sync.sh` copied into the repository you are working in. That copy, its queue and its gates decide what runs. This skill only starts it.

## Before you run anything

Run each check as its own command. Don't join them with `&&`, `;` or a pipe: a harness with a narrow command allowlist can refuse the combined form.

1. Work from the repository root. Run `test -f automation/supervisor/run_next.py`. If it fails, stop and say: "This repository has not adopted Boundroom."
2. Run `python3 automation/supervisor/run_next.py --help` and read its output for `--single-slice`. If it isn't listed, stop and say the synced copy is too old for this plugin and needs a sync to a newer Boundroom release.

## The one supported command

```bash
python3 automation/supervisor/run_next.py --single-slice
```

You may append these flags when the user asks for them: `--dry-run`, `--include-blocked`, `--queue <path>`, `--handoff-dir <path>`, `--board <dir> --kanban-overlay <file>` (also spelled `--kanban-board`), `--board-backend local` or `--board-backend hermes`, `--memory-config <file> --memory-project <name>`.

Your plugin options, set in Claude Code's `/config`, come next. For each line below whose value isn't empty, add its flag with exactly that value. Leave out any line whose value is empty: the supervisor's own default then applies, and telemetry stays off.

- `--runner-profile` `${user_config.claude_profile}`
- `--runner-profile-ceiling` `${user_config.claude_profile_ceiling}`
- `--telemetry` `${user_config.telemetry}`
- `--telemetry-capture` `${user_config.telemetry_capture}`
- `--telemetry-runner` `${user_config.telemetry_runner}`

These are the user's own standing choices, so passing them is the user asking. Don't add any other telemetry or profile flag unless the user asks for it in this request, and never put a header or token on the command line. If the supervisor refuses a value, report the refusal and don't change the value.

A Kanban board can be a local board, which needs no Hermes, or a Hermes board. The overlay's `backend` says which, and the supervisor refuses a `--board-backend` that disagrees. Report that refusal; don't change the flag to get past it.

Telemetry is off unless the user turns it on. Add a telemetry flag (`--telemetry`, `--telemetry-file-dir`, `--telemetry-endpoint`, `--telemetry-capture`, `--telemetry-runner`, `--telemetry-required` or `--no-telemetry`) only when the user asked for telemetry in this request, with exactly the values they gave. Never choose a destination for them, and never put a header or token on the command line: those come only from the user's own environment or `~/.config/boundroom/telemetry.json`.

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
