# SPEC-008: Plugin settings you can see

Status: **accepted (raelldottin/boundroom#198, merged 2026-11-12 after the owner's approval).** Each delivery slice below is still its own pull request with its own approval. RA-SET-001, the registry (`automation/supervisor/settings.py`) and its completeness tests, RA-SET-002, its resolver, and RA-SET-008, the Claude runner profiles with the ceiling, the per-slice settings file, the `runner_profile` record and the nested-run guard (`automation/supervisor/runner_profile.py`), are implemented. Profiles apply only when one is chosen; without one, the runner still starts in `bypassPermissions` until RA-SET-010. The other slices aren't yet.

Owner request recorded here (2026-11-12): give the user a quick or an extensive look at every option the Boundroom plugin can be configured with, and decide which options and settings should be surfaced to the user.

## Problem

The plugin's skills drive the synced supervisor, and the supervisor reads its settings from six places. No single place lists them, says which value is in effect, or says where that value came from:

| Where | Count today | Examples |
| --- | --- | --- |
| Command-line flags | `run_next.py` 13, `evaluate_handoff.py` 6, `kanban_projection.py` 3, `Tools/boundroom-sync.sh` 11 | `--single-slice`, `--board-backend`, `--allow-unverified-source` |
| Environment variables read by `run_agent.sh` | 10 settings, plus legacy `REPO_AUTOMATION_*` and `OWLORY_*` aliases | `BOUNDROOM_AGENT_RUNNER`, `BOUNDROOM_CLAUDE_PERMISSION_MODE` |
| Queue policy (`automation/queue/slices.json`) | 4 fields | `consecutive_autonomous_limit`, `agent_command_template` |
| Kanban overlay | `backend`, `board` and the policy again | `"backend": "local"` |
| Memory configuration | endpoint, credential variable, timeout, 8 budgets, project grants | `api_key_env`, `endpoint` |
| Accepted, not yet implemented: [SPEC-006](agent-telemetry.md) telemetry and [SPEC-007](knowledge-loop-platforms.md) knowledge platforms | about 15 more | `BOUNDROOM_TELEMETRY`, `platforms` |

Some of these matter a great deal and are invisible today. The clearest case, for which this spec proposes [a safer default](#a-safer-default-for-the-claude-code-runner): when the runner is Claude Code, `run_agent.sh` passes `--permission-mode "${BOUNDROOM_CLAUDE_PERMISSION_MODE:-bypassPermissions}"`. Every supervised Claude Code session runs with permission checks bypassed unless the user knows to set that variable, and nothing tells them.

## Decision (proposed)

1. **One registry declares every setting.** `automation/supervisor/settings.py` holds one entry per setting. The entry records its id, type, default, allowed values, every source it can come from, its surfacing class, whether it's a secret, and a one-line description. Code reads settings through the registry's resolver, so a new flag or variable can't exist without an entry. Tests enforce that.
2. **Two read-only views, generated from the registry:**
   - **Quick** (`boundroom settings`): the dozen settings a user should know about, plus anything not at its default and any warnings.
   - **Extensive** (`boundroom settings --all`, `--json`, `--explain <id>`): every setting with its effective value, its source and its full precedence chain.

   Neither view writes, connects to the network, or prints a secret.
3. **A fixed rubric decides what surfaces.** It isn't a judgment call per setting. The rubric is in [Surfacing rules](#surfacing-rules), and its result for every setting known today is in [Classification](#classification).
4. **The plugin gets a fifth skill, `boundroom-settings`,** which runs the synced supervisor's settings command. As [SPEC-002](agent-plugin.md) requires, it's text only. `boundroom-status` prints the quick view's warnings and points to the skill.
5. **Claude Code's `userConfig` is an optional convenience for a short allowlist, not the source of truth.** Codex and Hermes have no equivalent the spec can rely on, so the supervisor's own sources stay authoritative. A `userConfig` option is just another way of supplying a value the registry already defines.
6. **The registry generates a reference page** at `docs/reference/settings.md`, and CI fails when it's out of date.

## Sources and precedence

These are the same for every setting. A setting's registry entry lists which of them it accepts.

1. **Kill switches**, where a setting has one, such as SPEC-006's `--no-telemetry`. They always win.
2. **Command-line flags.** The plugin's skills pass these, including values from Claude Code `userConfig`.
3. **`BOUNDROOM_*` environment variables.** A legacy alias is read only when its new name is unset, as SPEC-005 decided.
4. **The operator's files** under `~/.config/boundroom/`. SPEC-006's `telemetry.json` and SPEC-007's `knowledge.json` live there.
5. **The repository's policy:** the queue policy, the Kanban overlay and the memory configuration.
6. **The registry's default.**

**Who may set what.** Each entry declares what the repository may do with it: `any`, `narrow-only` or `never`. `narrow-only` means the repository can lower or turn off the setting but not raise or redirect it, as SPEC-006 and SPEC-007 already require for telemetry and platforms. A repository value that breaks its rule is refused with a named reason, and both views show the refusal.

## Surfacing rules

Each question below is asked in order. The first "yes" decides the class.

| # | Question | Class | How it's shown |
| --- | --- | --- | --- |
| 1 | Is it a credential, or a reference to one? | **secret** | Never the value. Only *set* or *not set*, and the name of the variable or secret reference that holds it. Never a `userConfig` option. |
| 2 | Is it set by the supervisor for the agent, or is it a test or diagnostic hook, rather than something a user configures? | **internal** | Extensive view only, under "Set by Boundroom, not by you", for transparency. Not configurable through the plugin. |
| 3 | Does it change what the agent may do, how much it runs unattended, what code runs, or where data leaves the machine? | **security** | Always in the quick view, with its value, its source and whether it's the default. A risky value gets a warning. |
| 4 | Does a user have to set it, or recognize it, to use a skill? | **essential** | In the quick view when the skill that uses it applies. For example, board settings appear when an overlay is configured. |
| 5 | Is it reachable from the plugin at all? | **advanced** if yes; **outside the plugin** if no | Extensive view only. Settings outside the plugin, such as the benchmark's, are grouped separately. |

**Also in the quick view, whatever the class:**
- any setting not at its default;
- any legacy alias in effect, with the new name to use;
- any repository value that was refused;
- any secret that a configured feature needs but that isn't set.

**What qualifies for Claude Code `userConfig`.** Only a setting that meets all of these:
- its class is **security** or **essential**;
- it's per-user, not per-repository;
- it's not a secret;
- passing it through skill text is safe, because Claude Code substitutes only non-sensitive values into skills;
- the same setting is reachable without Claude Code, so Codex and Hermes users aren't left without it.

## Classification

Every setting known today. The registry is built from this table in RA-SET-001, and its completeness tests catch anything missed here.

### Surfaced in the quick view

| Setting | Source | Class | Why it surfaces |
| --- | --- | --- | --- |
| Runner (`BOUNDROOM_AGENT_RUNNER`) | env | security | Decides which agent executes the slice |
| Claude Code runner profile and ceiling (`BOUNDROOM_CLAUDE_PROFILE`, `BOUNDROOM_CLAUDE_PROFILE_CEILING`, and `BOUNDROOM_CLAUDE_PERMISSION_MODE` as an override) | env, userConfig | security | **Today the runner defaults to `bypassPermissions`.** [A safer default](#a-safer-default-for-the-claude-code-runner) proposes `parent`, capped at `edits`. The quick view always shows the chain from the parent's mode to the effective profile, and warns when it's `unbounded`. |
| One slice or several (`--single-slice`, `--autonomous-limit`, policy `consecutive_autonomous_limit`) | flag, policy | security | How much runs unattended. As AGENTS.md says, the policy value is a default a caller can override, so both views show which one won. |
| Agent command (`--agent-cmd`, policy `agent_command_template`) | flag, policy | security | What actually runs. Shown as *default* or *custom*, with the custom command printed. |
| Supervisor-owned paths (policy `supervisor_owned_paths`) | policy | security | The files no slice may change |
| Work source (`--queue`, or `--board`, `--kanban-overlay`, `--board-backend`) | flag, overlay | essential | Where the next slice comes from, and which backend |
| Memory endpoint (`endpoint` in the memory configuration) | memory config | security | Data leaves the machine when it's set |
| Memory credential (`api_key_env`) | memory config | secret | Shown as the variable's name, and set or not set |
| Sync provenance (`--pin`, `--expect-remote`) | flag | security | What a consumer receives, and from where |
| Unverified sync (`--allow-unverified-source`) | flag | security | Always a warning when set. AGENTS.md forbids agents from using it. |
| Template re-baselining (`--force-templates`) | flag | security | It overwrites a consumer's local template changes |
| Telemetry and knowledge platforms, once SPEC-006 and SPEC-007 land | various | security | Off, or the destination host and the enabled platforms |

### Extensive view only

| Settings | Class |
| --- | --- |
| `--handoff-dir`, `--include-blocked`, `--dry-run`, `--hermes`, `--memory-config`, `--memory-project`; `evaluate_handoff.py`'s `--slice-id`, `--handoff`, `--base-sha`, `--head-sha`, `--queue`, `--dry-run`; the projection's `--board-dir`, `--overlay`, `--shadow-queue`; sync's `--check`, `--sync`, `--target`, `--source`, `--manifest`, `--quality-command`, `--runtime-python` | advanced. A skill's own required arguments are shown with that skill, not in the quick view. |
| `BOUNDROOM_CLAUDE_BIN`, `BOUNDROOM_CODEX_BIN`, `BOUNDROOM_HERMES_BIN`, `BOUNDROOM_HERMES_TRANSPORT`, `BOUNDROOM_HERMES_MAX_TURNS`, `BOUNDROOM_HERMES_PYTHON`, `BOUNDROOM_HERMES_USAGE_DIR`, policy `handoff_timeout_seconds`, memory `timeout_seconds`, memory budgets, memory project grants | advanced |
| `BOUNDROOM_HERMES_SANDBOX_IMAGE` | security, but shown only when the runner is Hermes |
| `BOUNDROOM_SUPERVISOR_CONTEXT_FILE`, `_HANDOFF_FILE`, `_SLICE_ID` and their legacy names | internal: exported to the agent |
| `BOUNDROOM_SANDBOX_PROBE_COMMAND`, `_PROBE_BARRIER`, `_PROBE_BARRIER_SECONDS`, `BOUNDROOM_UNLISTED_SENTINEL` | internal: test and diagnostic hooks |
| `REPO_AUTOMATION_*` and `OWLORY_*` aliases | legacy aliases. Warned about in the quick view when in effect. |
| The effectiveness benchmark's flags and settings | outside the plugin |

**Not settings at all:** slice records, overlay approvals and handoffs are work items and records. The settings views don't list them.

### Proposed Claude Code `userConfig`

Applying the rule above, two settings qualify today, and two more once SPEC-006 lands:

| Option | Maps to | When |
| --- | --- | --- |
| `claude_profile` and `claude_profile_ceiling` | `BOUNDROOM_CLAUDE_PROFILE` and `BOUNDROOM_CLAUDE_PROFILE_CEILING` | RA-SET-006, after RA-SET-008. Neither can make `unbounded` reachable without the isolation declaration or an explicit ceiling. |
| `telemetry` (`off` and nothing else at first) | SPEC-006's kill switch | After SPEC-006's RA-TEL-001 |
| `telemetry_endpoint` | SPEC-006's `endpoint` | After SPEC-006, and only if the owner wants destinations settable from Claude Code. Headers stay out, because they're secrets. |

No `userConfig` option has a `default`, so leaving one empty changes nothing and the supervisor's own sources apply. Fixed-choice `options` need Claude Code 2.1.271 or later. The spec avoids them unless the owner accepts that minimum version.

## The views

**Quick view.** About 15 lines, grouped and ending with warnings:

```text
Boundroom 0.3.0 settings (quick view)          run `boundroom settings --all` for everything
  runner             claude                      env BOUNDROOM_AGENT_RUNNER
  claude profile     parent → unbounded → edits (ceiling)   ! parent runs bypassPermissions
  slices per run     1                           flag --single-slice
  agent command      default
  work source        local board ~/.boards/demo  overlay ./automation/overlay.json
  memory             off
  telemetry          off                         default
Warnings
  ! REPO_AUTOMATION_AGENT_RUNNER is set; use BOUNDROOM_AGENT_RUNNER
```

**Extensive view.** Every setting, grouped by area (runner, work source, policy, sync, memory, telemetry, knowledge, internal, outside the plugin). Each row shows the id, effective value, source, default, allowed values, class and the repository rule. `--json` gives the same data for tools. `--explain <id>` shows one setting's full precedence chain: each source, whether it was set, and which won and why.

**Shared rules for both views:**
- Read-only: no network, no writes, no lock or board access beyond what reading the overlay needs.
- Secrets appear only as *set* or *not set* plus their reference name.
- URL paths, query strings and headers are never printed.
- An unreadable or invalid source is reported in the view, not fatal.

## A safer default for the Claude Code runner

Owner decisions recorded here (2026-11-12): **propose a safer default** than `bypassPermissions` for Claude Code runners. Then: **the runner's profile must match the parent process, or be configurable by the user.** The profiles below do both: `parent` matches the parent, and every profile, including `parent`, is a user setting with a user-set ceiling.

### Why change it

- **Claude Code's own guidance.** Its permissions documentation warns that `bypassPermissions` skips prompts, "including for writes to protected paths such as `.git` and `.claude`", and should be used "only … in isolated environments like containers or VMs". A supervised slice runs in the operator's own checkout, on the operator's own machine.
- **The other runners are already contained.**
  - Codex runs with `--sandbox workspace-write`.
  - Hermes runs with `HERMES_SAFE_MODE=1` and its own sandbox options.

  Claude Code is the one runner with no boundary.
- **The supervisor's gates check results, not actions.** Gates such as the scope check, the validation replay and the committed-path check catch out-of-scope *changes* after the run. They don't stop a command from reading `~/.ssh`, calling the network, or writing outside the repository while it runs.
- **It's been tested.** The RA-BD-004 canary (2026-11-10) ran its Claude runner with `acceptEdits`, a `--settings` file allowing only the slice's git and validation commands, and a budget cap. The slice reached review cleanly. This spec's earlier note that the gates were "measured with" `bypassPermissions` was wrong; the only Claude canary didn't use it.

### Profiles

A new setting, `BOUNDROOM_CLAUDE_PROFILE`, chooses one of five profiles. `BOUNDROOM_CLAUDE_PERMISSION_MODE` stays as an explicit override of the mode alone, for compatibility, and the quick view shows when it's in effect.

| Profile | Permission mode | Allowed actions | Sandbox | Budget |
| --- | --- | --- | --- | --- |
| **`parent` (proposed default)** | Resolved from the parent process, as in [Matching the parent](#matching-the-parent) | As resolved | Whatever the parent's own settings enable | `--max-budget-usd`, default **$5** per slice |
| `bounded` (the fallback when the parent's mode is unknown) | `dontAsk`: anything not pre-approved is denied, never prompted, which suits a headless session | The slice allowlist below | On, with `failIfUnavailable: true` and `allowUnsandboxedCommands: false`. Network closed. | `--max-budget-usd`, default **$5** per slice |
| `edits` | `acceptEdits` | The slice allowlist below | Off | As above |
| `unbounded` | `bypassPermissions` | Everything | Off | As above |
| `custom` | The operator's own `--settings` file (`BOUNDROOM_CLAUDE_SETTINGS`) | As the file says | As the file says | As above |

`edits` reproduces the RA-BD-004 canary's setup for machines where the sandbox can't start. `unbounded` keeps today's behavior. Every profile, including the one `parent` resolves to, is then limited by the [ceiling](#choosing-a-profile-and-its-ceiling). The default ceiling admits `unbounded` only with `BOUNDROOM_CLAUDE_ISOLATED=1`, the operator's declaration that the run is in a container or VM. The quick view warns whenever the effective profile is `unbounded`.

### Matching the parent

**What a runner already shares with its parent.**
- **The harness.** In `auto` mode, `run_agent.sh` launches the harness the call came from.
- **The parent's standing settings.** A `claude -p` child loads the same managed (organization) and user settings files as its parent, and the project's settings when the folder is trusted. Deny rules there, a managed setting that disables `bypassPermissions`, and sandbox settings all bind the runner automatically.

**What a runner can't share.**
- **The parent session's prompt route.** An interactive or desktop session answers prompts through its user. The desktop app, for example, launches Claude Code with `--permission-prompt-tool=stdio`. A runner is headless, so there's no one to ask: a call that would prompt is simply denied.
- **Approvals given during the session,** such as "yes, don't ask again", which live only in the parent session.
- **A mode the person switched to mid-session.** The supervisor can see only how the parent was *launched*.

**How `parent` resolves.** The supervisor walks up its process tree to the nearest Claude Code process, which `run_agent.sh` already does to pick the harness. It then reads that process's launch flags: only `--permission-mode` and `--dangerously-skip-permissions`, nothing else. It maps them as follows:

| Parent launched with | Runner gets, before the ceiling | Why |
| --- | --- | --- |
| `bypassPermissions` or `--dangerously-skip-permissions` | `unbounded` | The same mode |
| `acceptEdits` | `edits` | The same mode. The slice allowlist pre-approves the commands the parent's user would have been asked about, because no one is there to ask. |
| `default` (Manual) or `dontAsk` | `dontAsk` with the slice allowlist, and the sandbox if the parent's settings enable it | The headless equivalent of "ask first": every action is either approved in advance, by the person who approved the slice, or denied |
| `auto` | `auto`, if the runner's Claude Code accepts it in `-p` (checked in RA-SET-009). Otherwise `edits`, with the reason recorded. | The same mode, where available |
| `plan` | Nothing. The run is refused before the claim. | Plan mode means no edits, so starting a slice would contradict it. |
| No Claude Code parent (a shell, Codex or Hermes, a forced `BOUNDROOM_AGENT_RUNNER=claude`), or flags that can't be read | The fallback profile, `bounded` by default | Nothing to match |

A worked example: this specification was written in a desktop session launched with `--permission-mode=bypassPermissions`. Under `parent`, its runner resolves to `unbounded`, and the default ceiling then lowers it to `edits`, with a warning saying so, unless the operator declares isolation.

### Choosing a profile and its ceiling

**Two user settings,** both in the SPEC-008 registry:
- **`claude_profile`:** `parent`, `bounded`, `edits`, `unbounded` or `custom`. The default is `parent`.
- **`claude_profile_ceiling`:** the most a runner may get, whatever the profile asks for or resolves to. The default is `edits`, or `unbounded` when `BOUNDROOM_CLAUDE_ISOLATED=1` is set.

Profiles are ordered by what runs without anyone's approval: `bounded` (and the `dontAsk` mapping), then `edits`, then `unbounded`. The effective profile is the lower of the requested or resolved profile and the ceiling. `custom` is the operator's own settings file, and isn't capped, because the operator wrote it.

**As built (RA-SET-008):** asking for `unbounded` with neither `BOUNDROOM_CLAUDE_ISOLATED=1` nor an explicit ceiling is refused before the claim (PS-13). With an explicit lower ceiling, it is lowered like any other request (PS-18). The flag and the variables are implemented; the `userConfig`, operator-file and repository sources below follow in RA-SET-006 and with the operator decisions.

**Where the settings come from,** in the usual order:
1. the flag `--runner-profile`;
2. `BOUNDROOM_CLAUDE_PROFILE` and `BOUNDROOM_CLAUDE_PROFILE_CEILING`;
3. the Claude Code `userConfig` options `claude_profile` and `claude_profile_ceiling`;
4. the operator's file;
5. the repository's policy.

The repository's rule is **narrow-only**: a repository may ask for a lower profile or ceiling than the user's, never a higher one. `BOUNDROOM_CLAUDE_ISOLATED` comes only from the environment, because it's a statement about the machine, not a preference.

**Every run records its choice.** The decision report gets a `runner_profile` block: the requested profile, the parent's observed launch mode, the resolved profile, the ceiling, the effective profile and the reason for each step. The quick view shows the same in one line, for example `claude profile  parent → unbounded → edits (ceiling)`.

### No nested runs

Matching the parent can give a runner wide permissions. So the rule that no worker may start the next slice must hold by mechanism, not by the runner's permissions alone. Today it holds only partly: `run_next.py` refuses while the same queue has a slice `in_progress`, but nothing stops a run against another queue or board.

- **The mechanism:**
  - `run_next.py`, `evaluate_handoff.py` without `--dry-run`, and the board's write commands refuse to start when `BOUNDROOM_SUPERVISOR_SLICE_ID`, or one of its legacy names, is set. `run_agent.sh` already exports it to every runner.
  - The outer run also holds an exclusive run lock outside the repository, and a nested start refuses while that lock is held.
- **As built (RA-SET-008):** the run lock is one per user, at `$XDG_STATE_HOME/boundroom/run.lock` (default `~/.local/state`), so it also refuses a second, unrelated supervised run started by the same user while the first is running. `run_next.py --dry-run` takes no lock, but the variable check refuses it inside a slice like any other start. The board's `list`, `show` and `snapshot` stay available inside a slice.
- **Its limit, stated plainly:** an `unbounded` runner can unset a variable or remove a lock file. That is why the default ceiling admits `unbounded` only on a declared isolated machine.

### The slice allowlist

The supervisor writes a per-slice settings file **outside the repository** (in the run's temporary directory) and passes it with `--settings`. Claude Code ignores an untrusted project's own `.claude/settings.json`, as the canary found, so this file is the only source of permissions. It's built from the slice record, the policy and the handoff path the supervisor chose, not from anything the agent wrote:

- **Allow:**
  - `Edit` for each of the slice's `allowed_paths`, and for the slice's handoff file. Claude Code applies only `Edit` rules to its file tools, and `Edit` covers every one of them, so there are no `Write` rules;
  - `Bash` for each of the slice's `required_validations`, exactly as written;
  - `git status`, `git diff`, `git rev-list`, `git add` for the allowed paths, `git commit`, and `git push` to the current branch's upstream. The runner prompt requires committing and pushing before a clean stop.
- **Deny, which wins over allow:**
  - `Edit` for the policy's `supervisor_owned_paths`, `.git/**` and `.claude/**`;
  - `Read` for `~/.ssh/**`, `~/.aws/**` and `~/.config/**`;
  - `WebFetch` and `WebSearch`.
- **Reads** inside the repository need no rule.
- **The sandbox:**
  - Writes are limited to the repository and the per-user temp directory.
  - The network allowlist is empty.
  - `git push` is listed in `excludedCommands`: Claude Code's sandbox proxy doesn't carry plain SSH, and the push still needs its allow rule.
  - `git add *` and `git commit *` are listed in `excludedCommands` too. The sandbox turns the `.git/**` deny rule into a write block, so inside it a commit can't create `.git/index.lock`. An entry without a wildcard matches only the bare command, hence the ` *`. Outside the sandbox, the allow rules still limit these commands to the slice's paths, and the file tools still can't touch `.git`. An excluded command is not a sandbox boundary: `git commit` runs the consumer's own hooks unsandboxed, which the runner can't edit because `.git/**` is denied to it.
- **The runner prompt** gains a short "What you may run" list generated from the same file, so the agent doesn't spend turns on calls that will be denied.

A denied call isn't a supervisor error. If the agent can't finish within its allowlist, its handoff reports the blocker, and the slice fails or blocks through the existing gates. That's the safe outcome.

### Rollout

1. **RA-SET-008:** generate the per-slice settings file, and add the profiles, `parent` resolution, the ceiling, the `runner_profile` record and the nested-run guard. `unbounded` stays the default, so nothing changes unless the operator opts in, apart from the guard, which only refuses nested runs.
2. **RA-SET-009:** canary slices on a disposable local board, on the owner's machine with Claude Code only: `bounded`; `edits`; and `parent` under parents launched in Manual, `acceptEdits` and `bypassPermissions`, with the default ceiling. Also check whether `auto` works in `-p`. Record any call that was denied but needed, and adjust the allowlist rules in the spec before the switch.
3. **RA-SET-010:** switch the default to `parent` with the default ceiling, in a minor release (`v0.4.0`), with release notes naming `BOUNDROOM_CLAUDE_PROFILE=unbounded` and `BOUNDROOM_CLAUDE_ISOLATED=1` as the way back.

The switch is reversible: `unbounded` stays available for isolated environments, and the previous default can be restored by one setting.

**The benchmark is unaffected.** Its lanes set `BOUNDROOM_AGENT_RUNNER: hermes`, and the Claude profile applies only to the Claude runner.

## Requirements

| Id | Requirement | Acceptance test |
| --- | --- | --- |
| PS-1 | Every flag of every plugin-driven entry point is in the registry | Builds each argument parser and compares its options with the registry. This needs `run_next.py`, `evaluate_handoff.py` and the projection to expose `build_parser()`. |
| PS-2 | Every `BOUNDROOM_*`, `REPO_AUTOMATION_*` and `OWLORY_*` variable the code reads is in the registry | Scans `automation/` and `Tools/` for the variable names |
| PS-3 | Every field of the policy, overlay and memory models is in the registry, or marked as a work item | Walks the pydantic models' fields |
| PS-4 | Every flag a skill's text mentions is in the registry, and every `userConfig` key maps to a setting that qualifies | Scans `plugins/boundroom/` |
| PS-5 | The views never print a secret | Plants marker values in every secret, then scans the quick, extensive, `--json` and `--explain` output |
| PS-6 | The views are read-only | Sockets blocked, and the file system compared before and after |
| PS-7 | Source and precedence are reported correctly | For each setting, every combination of sources yields the expected value and the source that won |
| PS-8 | A repository value that breaks its rule is refused and shown | Tests for each `narrow-only` and `never` setting |
| PS-9 | The reference page matches the registry | `boundroom settings --docs --check` in `make reusable-check` |
| PS-10 | Defaults are unchanged until RA-SET-010 | The registry's defaults equal today's behavior through RA-SET-009, so introducing it changes nothing a user sees except the new views |
| PS-11 | The slice allowlist comes from the slice record only | The generated settings file is a pure function of the slice record and policy. A handoff or repository file can't add a rule. |
| PS-12 | Deny wins | Edits to `supervisor_owned_paths`, `.git/**` and `.claude/**`, and reads of `~/.ssh/**`, are denied even when an allow rule also matches |
| PS-13 | `unbounded` needs the isolation declaration | `unbounded` without `BOUNDROOM_CLAUDE_ISOLATED=1` is refused before the claim, and the board's bytes are unchanged |
| PS-14 | `bounded` fails closed | If the sandbox can't start, the run stops before the agent runs; it isn't silently unsandboxed |
| PS-15 | The settings file stays outside the repository | Written under the run's temporary directory, and removed afterwards. The worktree is unchanged by it. |
| PS-16 | `parent` resolves as specified | With a fake process tree, each launch flag in the mapping table yields its row's profile. Only `--permission-mode` and `--dangerously-skip-permissions` are read from the parent's arguments. |
| PS-17 | An unknown parent falls back | With no Claude Code ancestor, or unreadable flags, the fallback profile applies and the reason is recorded |
| PS-18 | The ceiling always applies | For every pair of requested profile and ceiling, the effective profile is the lower one. `unbounded` is reached only with an explicit ceiling or `BOUNDROOM_CLAUDE_ISOLATED=1`. |
| PS-19 | A plan-mode parent starts nothing | Refused before the claim, and the board's bytes are unchanged |
| PS-20 | No nested runs | With `BOUNDROOM_SUPERVISOR_SLICE_ID` set, or the run lock held, `run_next`, a non-dry-run `evaluate_handoff` and board writes refuse, against any queue or board |
| PS-21 | The choice is recorded | The decision report's `runner_profile` block names each step, and the quick view shows the same chain |

## Effects on other specs and rules

- **[SPEC-002](agent-plugin.md):** a fifth skill and, in RA-SET-006, a `userConfig` block in the Claude Code manifest. Both are skills and manifests, with no hook, MCP server or background process. A plugin version bump ships with a release a person approves.
- **[SPEC-005](boundroom-rename.md):** legacy aliases become registry entries, so removing them later is a registry change with a known blast radius.
- **SPEC-006 and SPEC-007:** their settings join the registry when they're implemented. Their repository rules become `narrow-only` entries.
- **Performance (AGENTS.md):** the resolver replaces scattered `os.environ` and argument reads with one lookup per setting. It adds no file or network work to a slice beyond the files the supervisor already reads. The views run only on request.

## Delivery slices

Each slice is one pull request by the agent, approved by a person.

| Slice | Delivers | Acceptance |
| --- | --- | --- |
| RA-SET-001 | The registry with every setting in [Classification](#classification), `build_parser()` for the three entry points, and the completeness tests | PS-1 to PS-4 and PS-10. No behavior change. |
| RA-SET-002 | The resolver: effective value, source and precedence chain, including legacy aliases and the repository rules. Entry points read through it. | PS-7, PS-8 and PS-10 |
| RA-SET-003 | The views: `settings`, `--all`, `--json` and `--explain`, with redaction | PS-5 and PS-6, plus golden-output tests for the quick view |
| RA-SET-004 | The generated reference page and its CI check | PS-9 |
| RA-SET-005 | The `boundroom-settings` skill, and quick-view warnings in `boundroom-status`, as text only | PS-4. The skill contains no code. |
| RA-SET-006 | The `userConfig` block for `claude_profile`, after the owner decides below | PS-4. `claude plugin validate` passes. |
| RA-SET-007 | SPEC-006 and SPEC-007 settings, as each lands | PS-1 to PS-9 for the new entries |
| RA-SET-008 | The Claude runner profiles, `parent` resolution, the ceiling, the per-slice settings file, the `runner_profile` record and the nested-run guard, with `unbounded` still the default | PS-11 to PS-21, using a fake process tree and a fake `claude` that records its arguments and settings file |
| RA-SET-009 | Canary: `bounded`, `edits`, and `parent` under Manual, `acceptEdits` and `bypassPermissions` parents; plus `auto` in `-p`. Claude Code only, on a disposable local board. | Each run reaches review or is refused exactly as specified. Denied-but-needed calls are recorded and the allowlist rules are adjusted. Recorded in this spec. |
| RA-SET-010 | Switch the default to `parent`, with the default ceiling, in `v0.4.0`, with release notes | PS-10 is updated to the new default. The quick view shows the chain. |

## Alternatives considered

- **Document the settings by hand.** That's what exists now, spread across six places, and nothing keeps it current. A registry with completeness tests can't drift.
- **Make `userConfig` the source of truth.** It exists only in Claude Code, and its values reach the supervisor only through skill text. Codex, Hermes and plain shell users would be left out.
- **Surface everything.** Sixty rows is the extensive view's job. The quick view is useful only if it's short.

## Open decisions for the owner

**Decided by the owner (2026-11-12):**
- **Decision 1, the runner's default:** **`bounded`**, not `parent`. From `v0.4.0` (RA-SET-010), a supervised Claude Code runner starts in `bounded`: `dontAsk`, the slice allowlist, and the sandbox with `failIfUnavailable`, whatever the parent session runs, unless the user chooses another profile. `BOUNDROOM_CLAUDE_PERMISSION_MODE` stays an explicit override, and `BOUNDROOM_CLAUDE_PROFILE=unbounded` with `BOUNDROOM_CLAUDE_ISOLATED=1` is the way back to today's behavior. The rest of decision 1 (the `edits` ceiling, the $5 cap, `git push`) stands as proposed.
- **Decision 2, `userConfig`:** every setting the user may configure is configurable, and telemetry above all is configured only by the user and is **off by default**. RA-SET-006 ships `userConfig` options for the runner profile, its ceiling and each non-secret telemetry setting (mode, `file_dir`, endpoint, capture, runner telemetry), none with a default, so an empty option changes nothing. Headers stay out, because they are secrets.
- **Canaries:** RA-SET-009 and SPEC-006's RA-TEL-007 may run on the owner's machine with Claude Code on a disposable local board, capped at $5 per slice, **after** their pull requests are merged.

Still open: decision 3 (requiring Claude Code 2.1.271 for fixed-choice pickers; until then the options are free text and the supervisor validates them) and decision 4 as far as hosted destinations go.


1. **The runner profile's details,** proposed in [A safer default](#a-safer-default-for-the-claude-code-runner): `parent` as the default after the canary; the default ceiling of `edits` unless isolation is declared, which means a bypass-mode parent's runner gets `edits`; `bounded` as the fallback for an unknown parent; the $5 per-slice cap; allowing `git push`; and `v0.4.0` for the switch.
2. **Which `userConfig` options to ship:** `claude_profile` after RA-SET-008, and the telemetry options after SPEC-006.
3. **Whether to require Claude Code 2.1.271** for fixed-choice `options` pickers.
4. **Whether destinations,** such as a telemetry endpoint, should ever be settable from Claude Code's `/config`, or only from the operator's files.
