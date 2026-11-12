---
name: boundroom-settings
description: Show every option Boundroom can be configured with, its value in this repository and where that value came from, as a quick view or an extensive one. Read-only; it never changes a setting or prints a secret. Use when asked what Boundroom is configured to do, which settings exist, why a setting has its value, or whether anything risky is enabled.
license: GPL-2.0-only
---

# Settings

This skill reads the supervisor copy that `Tools/boundroom-sync.sh` placed in the repository. It changes nothing: the views neither write nor connect to the network, and they show a secret only as set or not set.

## Before you run anything

Work from the repository root. Run `test -f automation/supervisor/settings_view.py` on its own, not joined to another command. If it fails, run `test -f automation/supervisor/run_next.py`. If that fails too, stop and say: "This repository has not adopted Boundroom." If only the first fails, stop and say the synced copy is too old for settings views and needs a sync.

## Which view

Pick the view from what the user asked:

- **A quick look**, or no preference:

  ```bash
  python3 automation/supervisor/settings_view.py
  ```

  It shows the runner, the Claude runner profile, slices per run, the agent command, the work source, memory and telemetry, then every setting that isn't at its default, then warnings.

- **Everything:** add `--all`. It lists every setting by area, with what Boundroom sets for the agent shown separately.

- **One setting, and why it has its value:** add `--explain <id>`, using an id from `--all`. It shows each source in precedence order, whether it was set, and which one won.

- **For a tool or a table:** add `--json`.

If the user named a Kanban overlay or a memory configuration, also pass `--kanban-overlay <path>` or `--memory-config <path>`, so their settings are included.

## Reporting

Report what the command printed. Always repeat its warnings, word for word. Don't add values it didn't print: in particular, don't read a secret's variable or file yourself to show its value.

To change a setting, tell the user which flag, variable or file the view names as its source, so they can change it. Don't change it yourself.

## Boundaries

Don't edit the queue, the overlay, the operator's files under `~/.config/boundroom/`, or any environment file. Don't run a slice; that is `boundroom-run-next`.
