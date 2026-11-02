---
name: repo-automation-evaluate-handoff
description: Evaluate an existing repo-automation handoff artifact against its slice and report the supervisor's decision without changing the queue. Use when asked to check, review or re-evaluate a handoff file or a finished slice.
license: GPL-2.0-only
---

# Evaluate a handoff

This skill runs the evaluator in the supervisor copy that `Tools/repo-automation-sync.sh` placed in the repository.

## Before you run anything

1. Work from the repository root. Confirm `automation/supervisor/evaluate_handoff.py` exists there. If it doesn't, stop and say: "This repository has not adopted repo-automation."
2. Get from the user, or from the handoff and git history:
   - the slice id;
   - the handoff file;
   - the commit before the work (base);
   - the commit that holds the verified work (head).

   Use full commit SHAs, not branch names. If any of these is unclear, ask.

## The command

```bash
python3 automation/supervisor/evaluate_handoff.py --slice-id <slice-id> --handoff <handoff.json> --base-sha <base-sha> --head-sha <head-sha> --dry-run
```

Keep `--dry-run`. It reports the decision without changing the queue. Run without it only if the user explicitly asks you to record the result in the queue, and say that you are doing so.

## After it returns

Report the decision, the stop reason, and any validation, scope, diff-budget or proof failures it lists. Then stop. Don't start a slice.
