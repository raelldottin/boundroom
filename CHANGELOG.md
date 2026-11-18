# Changelog

What changed in each Boundroom release, newest first. A pull request that changes what a consumer sees adds a line under **Unreleased**. The release pull request renames that heading to the new version and its date, and `release.yml` publishes the section as the release notes. The [upgrade guide](docs/workflows/upgrading.md) says how to move between releases, and the [compatibility policy](docs/reference/compatibility.md) says what each kind of release may change.

Headings are `## <version> (<YYYY-MM-DD>)`, and `Tools/changelog_section.py` reads them.

## Unreleased

## 0.5.0 (2026-11-18)

### Added

- `CODE_OF_CONDUCT.md`: the Contributor Covenant 2.1, with how to report a problem (#264).
- Telemetry metrics from one catalogue with bounded labels, standard resource attributes, error status on failed spans, a telemetry block in operator status, and `BOUNDROOM_OFFLINE`, which stops every network call Boundroom makes (#240).
- An example Grafana dashboard for the metric catalogue, in `automation/examples/telemetry/`, with sample data and the queries a Prometheus-compatible backend needs. Duration histograms are exported with explicit buckets from 1 second to 2 hours, so any such backend stores them as classic histograms (#261).
- A repository's own runner rules in `automation/runner-rules.md`, which the sync never writes and the supervisor adds to every runner prompt (#241).
- Signed build provenance for each release archive, checked with `gh attestation verify` (#244).
- A signed CycloneDX SBOM with each release, listing Boundroom's dependencies and extras at their locked versions, and attested against the archive. `Tools/check_plugin_version.py` now also requires `pyproject.toml` to declare the tag's version, and CI refuses a `uv.lock` that no longer matches `pyproject.toml` (#262).
- CodeQL analysis of the Python code and the workflows, on pull requests, pushes to `main` and weekly. It reports to code scanning and isn't a required check (#263).
- The compatibility and deprecation policy (#243) and this changelog (#245).
- A support matrix in the README. CI now also tests Python 3.13 and 3.14, and macOS (#246).
- Optional components in the sync manifest. A consumer can leave out the effectiveness benchmark with `Tools/boundroom-sync.sh --omit benchmark`. That removes what an earlier sync left of it, and the consumer's synced tests still pass. Pass the flag to every `--check`, `--sync` and `--status` (#260).
- Operator docs: an architecture overview, a security model, a reference of every exit code, failure reason and decision, and a troubleshooting guide. A test fails if a command can exit with a code the reference doesn't list (#255).

### Changed

- The shipped runner prompt names no consumer. A consumer that wants the new `base.md` moves its own layer rules into `automation/runner-rules.md` first (#241).
- The sync writes only under `automation/` and `docs/workflows/`, and to `THIRD-PARTY-NOTICES.md` and its own scripts. That replaces a list of one consumer's paths (#242).
- Telemetry's service name is `boundroom`, and a sync drift check marks its run span as an error (#240).

### Deprecated

- Each `REPO_AUTOMATION_*` variable and `OWLORY_CODEX_BIN` now prints a warning naming its replacement. They stop working in v0.6.0, as do the `REPO_AUTOMATION_SUPERVISOR_*` and `OWLORY_SUPERVISOR_*` names the supervisor sets for its runner (#242).
- The manifest key `allow_owlory_specific` no longer opts out of anything, and v0.6.0 refuses it (#242).

### Removed

- Discord and Slack support, and the knowledge-loop specs SPEC-003 and SPEC-007 (#238).

### Fixed

- OTLP headers set in the operator's shell no longer reach the configured telemetry endpoint (#240).
- An error after `run_next.py` claims a slice, up to and including the write of its verdict, now marks the slice `failed` with reason `git_error` or `supervisor_error` and prints a failure report. Before, a Git failure, an error while replaying validations or judging the handoff, or a failed verdict write left the slice `in_progress` and the next run refused to start. If the queue or board refuses the failure too, the report says the slice is still `in_progress` (#256).
- A handoff that isn't valid JSON or can't be read fails the claimed slice as `handoff_invalid`. A bad handoff left in `automation/handoffs/` no longer stops every later run before it claims a slice (#258).

## 0.4.0 (2026-11-13)

### Changed

- A Claude Code runner starts in the `bounded` profile by default: `dontAsk`, the slice's own allowlist, and a sandbox that fails closed (#229). The upgrade guide lists three ways to keep the old behavior.

### Added

- Telemetry, off unless chosen: traces of supervised runs, syncs and benchmark lanes, passed on to the runner, with file or OTLP export (#205, #215, #222, #223, #224, #226).
- Settings you can read: the registry, resolution with sources, `settings_view.py`, the generated settings reference, the `boundroom-settings` skill, and `userConfig` options (#203, #206, #210, #211, #212, #230).
- Claude runner profiles and the nested-run guard (#209).
- Read-only reports: `operator_status.py` (#233), `Tools/boundroom-sync.sh --status` (#232) and `handoff_summary.py` (#220). `run_next.py` records the slice's base and head commits and can append reports to `--report-file` (#221).
- The knowledge-loop service for Discord and Slack (#207, #208, #213, #214, #216, #217, #225, #227, #231). It was removed after this release (#238).

### Fixed

- What the RA-SET-009 and RA-TEL-007 canaries found in the bounded runner and telemetry (#234).

## 0.3.0 (2026-11-12)

### Changed

- The product is Boundroom (SPEC-005). The plugin is `boundroom@boundroom`, the variables are `BOUNDROOM_*`, the sync script is `Tools/boundroom-sync.sh` and the repository is `raelldottin/boundroom`. The old names keep working, and a moved plugin needs an uninstall and reinstall (#194, #195).

## 0.2.5 (2026-11-10)

### Added

- A Kanban board that works with any parent agent: the `BoardBackend` interface, a local board with no Hermes dependency, and `run_next.py` taking slices from it (#182, #183, #184).
- CIS and SOC 2 change controls (#192).

### Fixed

- The supervisor checks what a slice committed, not only what it left uncommitted (#186).

## 0.2.4 (2026-11-03)

### Changed

- The plugin's sync skill may merge a consumer's sync pull request only after the consumer owner approves it.

## 0.2.3 (2026-11-03)

### Fixed

- Synced tests are isolated from the consumer's agent harness.

## 0.2.2 (2026-11-02)

### Fixed

- The pyright errors consumers reported in synced files.

## 0.2.1 (2026-11-02)

### Changed

- The plugin's sync skill updates the consumer's lock pin in the same commit, after the consumer owner agrees, and the evaluate-handoff skill closes only queued slices.

## 0.2.0 (2026-11-02)

### Added

- One plugin for Claude Code, Codex and Hermes (SPEC-002), and a release check that every plugin manifest declares the tag's version.
- Kanban support: a read-only projection of the JSON queue, write-back through `hermes kanban`, and `run_next.py` taking slices from a board (#110).
- The Hindsight memory adapter, memory recall in each context bundle, and Hindsight snapshots for the Swarm Wiki, with its compiler pinned.
- Coverage measured over the full suite, failing below 88% (#139).

### Changed

- Licensed under GPL-2.0-only.

### Removed

- CodSpeed (#135).

## 0.1.0 (2026-10-20)

### Added

- The first tagged release of the reusable automation: the supervisor, the context builder, the schemas, the prompts and the consumer sync tool, published as `repo-automation-v0.1.0.tar.gz` with its SHA-256 checksum.
