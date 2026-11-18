# Compatibility and deprecation policy

This is SPEC-009's RA-ENT-007. It says what each kind of release may change in the things a consumer depends on, how something is retired, and how long an old name keeps working. The [upgrade guide](../workflows/upgrading.md) says how to move between releases; this page says what a release is allowed to put in your way.

## Release numbers

A release is a `vMAJOR.MINOR.PATCH` tag on `main`. A person creates the tag, or approves a named tag on a named commit (AGENTS.md, Releases), so a person also decides whether a release is a patch or a minor. The agent proposes the number and checks it against this page.

Boundroom is before `1.0`. Until then:

- **A patch release** (`0.4.0` to `0.4.1`) fixes things. It renames, removes and changes the default of nothing a consumer depends on.
- **A minor release** (`0.4.x` to `0.5.0`) may also add, change defaults, and remove what this page let it remove.

Release `1.0` will tighten this, so that only a major release removes anything. This page is revised by a reviewed pull request before then.

## What a consumer depends on

| Surface | What counts | A patch may | A minor may also |
| --- | --- | --- | --- |
| **Settings** | Each setting's name, its `BOUNDROOM_*` variable, its operator-file key, its repository field, its allowed values and its default ([settings reference](settings.md)) | Add a setting that is off by default. Add a warning. | Change a default, with the old value still selectable and the change in the release notes. Remove a setting or name that finished its deprecation. |
| **Command-line flags** | Every flag of `run_next.py`, `evaluate_handoff.py`, `operator_status.py`, `settings_view.py`, `handoff_summary.py`, `run_agent.sh` and `Tools/boundroom-sync.sh` | Add a flag whose absence keeps today's behavior. | Change what a flag does by default. Remove a flag that finished its deprecation. |
| **Exit codes** | The number each entry point exits with, and what it means, as the [exit-code reference](exit-codes.md) lists them | Nothing. | Add a code for a new condition. A number never gets a new meaning, even in a minor release. |
| **The handoff and slice schemas** | `automation/schemas/*.json`: the fields, which are required, and the allowed values, such as `proof_level` | Accept more input: a new optional field, or a new allowed value the supervisor tolerates. | Add a required field or remove an allowed value, after one minor release that accepts both the old and the new form. Bump the queue's `version` when an old queue file can't be read. |
| **The decision report** | The fields `run_next.py` prints and appends to `--report-file` | Add a field. | Rename or remove a field, after one minor release that writes both names. |
| **The sync** | The flags of `Tools/boundroom-sync.sh`, the places it may write, and the paths it treats as the consumer's (the queue, handoffs, proofs, the lock, `automation/runner-rules.md`) | Refuse more, for safety. | Allow a new place to write, or stop treating a path as the consumer's, with the change in the release notes. |
| **Prompt templates** | The placeholders `run_next.py` substitutes in `automation/prompts/slice.md` | Nothing. | Rename or remove a placeholder. A consumer's customized copy is a template the sync keeps, so the release notes say what to change in it. |
| **Telemetry** | Span names, metric names, units, label names and label values in the metric catalogue, and the resource attributes | Add a span, a metric or a label value. | Rename or remove one, or change a unit. |
| **The support matrix** | The Python versions and operating systems in the README's support matrix | Add a version or an operating system. | Drop one, after a release that said it would. |
| **The plugin's skills** | Each skill's name and what it asks the synced copy for | Change a skill's wording. | Rename or remove a skill. A skill that needs something new checks for it and stops with a clear message when the copy is older ([upgrade guide](../workflows/upgrading.md#which-combinations-work)). |

Some things are not part of the surface: the supervisor's Python modules and their functions, test files, the wording of a message, the layout of human-readable output, and anything a page calls experimental. Scripts should read the JSON outputs, the exit codes and the schemas, not parse prose.

The sync manifest, `automation/reusable-manifest.json`, isn't a consumer surface either. The sync script reads the manifest from the same checkout it ships in, so they always change together.

**Security exception.** A patch release may remove or restrict a behavior to fix a security problem. Its release notes say what changed and what to do instead.

## How something is retired

1. **Deprecate it, with a warning.** Using the old name or behavior prints a warning on stderr. The warning names the replacement and the release that stops accepting it. When a warning can't be printed, as with a variable the supervisor sets for its runner, the upgrade guide says so instead.
2. **Record it.** The upgrade guide lists the old name and its replacement, and the table below lists the deprecation and its removal release.
3. **Keep it working for at least one minor release.** A minor release may remove something only when a release before it, with a lower minor number, shipped the warning. If no release carried the warning before the planned minor, the removal moves to the following minor.
4. **Remove it in a minor release**, and say so in that release's notes.

When the old and the new name are both set, the new one wins, and the warning says the old one is ignored.

## Current deprecations

| What | Replacement | Warns | Stops working |
| --- | --- | --- | --- |
| The `REPO_AUTOMATION_*` variables an operator sets, from before 0.3.0 | The `BOUNDROOM_*` variable of the same setting | Yes, from `run_next.py` and `run_agent.sh` | v0.6.0 |
| `OWLORY_CODEX_BIN` | `BOUNDROOM_CODEX_BIN` | Yes, from `run_next.py` and `run_agent.sh` | v0.6.0 |
| `REPO_AUTOMATION_SUPERVISOR_*` and `OWLORY_SUPERVISOR_*`, which the supervisor sets for its runner | `BOUNDROOM_SUPERVISOR_*` | No; documented in the upgrade guide | v0.6.0 |
| The manifest key `allow_owlory_specific`, which no longer opts out of anything | None: remove the key | Yes, from `Tools/boundroom-sync.sh` | v0.6.0 |
| `Tools/repo-automation-sync.sh` | `Tools/boundroom-sync.sh` | A notice that it was renamed, every time it runs | Not decided |
| The lock name `automation/repo-automation.lock` | `automation/boundroom.lock` | No; renaming it is the consumer owner's change | Not decided |

The warnings for the first four rows ship in the first release after v0.4.0, planned as v0.5.0. No release before it carries them, so step 3 moves their removal from v0.5.0 to v0.6.0. A person chooses each release's number; if the next one isn't v0.5.0, this table and the upgrade guide change with it.
