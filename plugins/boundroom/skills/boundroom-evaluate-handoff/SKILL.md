---
name: boundroom-evaluate-handoff
description: Evaluate a Boundroom handoff for a queued slice whose work was finished outside run_next, and report the supervisor's decision without changing the queue. Use when asked to check or close a queued slice from its handoff file. It does not re-check a slice that is already done.
license: GPL-2.0-only
---

# Evaluate a handoff

This skill runs the evaluator in the supervisor copy that `Tools/boundroom-sync.sh` placed in the repository.

## Before you run anything

1. Work from the repository root. Run `test -f automation/supervisor/evaluate_handoff.py` on its own, not joined to another command. If it fails, stop and say: "This repository has not adopted Boundroom."
2. Get from the user, or from the handoff and git history:
   - the slice id;
   - the handoff file;
   - the commit before the work (base);
   - the commit that holds the verified work (head).

   Use full commit SHAs, not branch names. If any of these is unclear, ask.
3. Check the slice in `automation/queue/slices.json`. The evaluator accepts only a slice whose status is `queued`, and only if the slice was already in the queue at the base commit. It rejects a `done` slice, and a slice with no contract at the base. If either applies, say so and stop. Don't edit the queue to get past it.

## The command

```bash
python3 automation/supervisor/evaluate_handoff.py --slice-id <slice-id> --handoff <handoff.json> --base-sha <base-sha> --head-sha <head-sha> --dry-run
```

Keep `--dry-run`. It reports the decision without changing the queue. Run without it only if the user explicitly asks you to record the result in the queue, and say that you are doing so.

## After it returns

Report the decision, the stop reason, and any validation, scope, diff-budget or proof failures it lists. A reject never changes the queue, with or without `--dry-run`; an accept without `--dry-run` marks the slice `done`. Then stop. Don't start a slice.
