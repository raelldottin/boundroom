---
name: repo-automation-status
description: Show what repo-automation would do next in this repository without running anything. Names the next eligible slice, lists blocked slices, and with a Kanban board compares the board's projection with the JSON queue. Read-only. Use when asked what is next, what is queued or blocked, or whether the board and queue agree.
license: GPL-2.0-only
---

# Queue status

This skill reads the supervisor copy that `Tools/repo-automation-sync.sh` placed in the repository. It changes nothing.

## Before you run anything

Work from the repository root. Run `test -f automation/supervisor/run_next.py` on its own, not joined to another command. If it fails, stop and say: "This repository has not adopted repo-automation."

## Next slice

```bash
python3 automation/supervisor/run_next.py --dry-run --include-blocked
```

`--dry-run` selects and packages the next slice without launching an agent or changing the queue. Report the slice it selected, or that none is eligible. If none is eligible, also report the blocked or deferred slices and their recommended unblockers.

## Board against queue (only if the user names a board)

If the user gives a Hermes Kanban board directory and an approval overlay, compare them with the JSON queue:

```bash
python3 -m automation.supervisor.kanban_projection --board-dir <board-dir> --overlay <overlay.json> --shadow-queue automation/queue/slices.json
```

Report the `shadow` section: unmapped tasks, slices missing on either side, field mismatches, and whether both sides would pick the same next slice. The projection writes nothing.

## Boundaries

Don't edit the queue, the board or any handoff. Don't run a slice; that is `repo-automation-run-next`.
