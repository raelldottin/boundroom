#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 - "$ROOT" "$@" <<'PY'
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


FORBIDDEN_SOURCE_PREFIXES = (
    ".githooks/",
    "SecondBrain/",
    "automation/handoffs/",
    "automation/proofs/",
    "automation/queue/",
    "automation/smoke/",
    "docs/product/",
    "docs/runtime/",
    "localization/",
    "owlory_xcode/",
)

FORBIDDEN_SOURCE_FILES = {
    ".githooks/pre-push",
    "Makefile",
    "Tools/bump-version.sh",
    "Tools/generate-build-info.sh",
    "Tools/release-preflight.sh",
    "Tools/set-build-number.sh",
    "Tools/verify-build-provenance.sh",
}

# Destinations a consumer owns outright. The canonical repository supplies both the file
# list and this tool, so without a destination check an upstream entry could aim reusable
# content at the consumer's hook, Makefile or live queue - and delete_stale could remove
# them. Unlike the source check there is no manifest opt-out: these are never importable.
#
# The lock file is here because the consumer's Makefile reads it and passes the values to
# this tool: whoever writes the lock chooses what a consumer's `make` executes. It is a
# statement about the canonical repo, written by the consumer at import time, never
# imported.
FORBIDDEN_DESTINATION_PREFIXES = FORBIDDEN_SOURCE_PREFIXES
# Both names are refused: a consumer synced before 0.3.0 keeps automation/repo-automation.lock
# until its owner renames it (SPEC-005).
FORBIDDEN_DESTINATION_FILES = FORBIDDEN_SOURCE_FILES | {"automation/boundroom.lock", "automation/repo-automation.lock"}

# Compared casefolded: on APFS and NTFS a destination of "makefile" or ".GitHooks/pre-push"
# is the same file as the one being protected, and an exact-match check would wave it past.
FOLDED_DESTINATION_FILES = {path.casefold() for path in FORBIDDEN_DESTINATION_FILES}
FOLDED_DESTINATION_PREFIXES = tuple(prefix.casefold() for prefix in FORBIDDEN_DESTINATION_PREFIXES)

SKIP_NAMES = {"__pycache__", ".DS_Store"}


@dataclass(frozen=True)
class Entry:
    source: str
    destination: str
    kind: str
    preserve_executable: bool
    delete_stale: bool
    template: bool
    allow_owlory_specific: bool


@dataclass(frozen=True)
class RuntimeRequirement:
    """A Python distribution the imported code needs in the *consumer's* interpreter.

    Deliberately not PEP 508: majors only. Enough to say "pydantic 2, not 1 and not 3",
    without making the dependency checker depend on a dependency parser.
    """

    distribution: str
    import_name: str
    minimum_major: int
    maximum_major_exclusive: int

    @property
    def spec(self) -> str:
        return f"{self.distribution}>={self.minimum_major},<{self.maximum_major_exclusive}"


class SyncError(Exception):
    pass


def fail(message: str) -> int:
    print(f"boundroom-sync: error: {message}", file=sys.stderr)
    return 2


def normalize_relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SyncError(f"{field_name} must be a non-empty string")

    path = PurePosixPath(value)
    if path.is_absolute():
        raise SyncError(f"{field_name} must be repo-relative, got absolute path {value!r}")

    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SyncError(f"{field_name} must not contain '.', '..', or empty path parts: {value!r}")

    return path.as_posix()


def bool_field(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise SyncError(f"manifest entry {raw.get('source', '<unknown>')!r} has non-boolean {key}")
    return value


def parse_entry(raw: object) -> Entry:
    if not isinstance(raw, dict):
        raise SyncError("manifest entries must be objects")

    source = normalize_relative_path(raw.get("source"), "source")
    destination = normalize_relative_path(raw.get("destination"), "destination")

    kind = raw.get("kind")
    if kind not in {"file", "directory"}:
        raise SyncError(f"manifest entry {source!r} has unsupported kind {kind!r}")

    allow_owlory_specific = bool(raw.get("allow_owlory_specific", False))
    if not allow_owlory_specific:
        if source in FORBIDDEN_SOURCE_FILES:
            raise SyncError(f"forbidden Owlory-specific source requires explicit approval: {source}")
        for prefix in FORBIDDEN_SOURCE_PREFIXES:
            if source == prefix.rstrip("/") or source.startswith(prefix):
                raise SyncError(f"forbidden Owlory-specific source requires explicit approval: {source}")

    folded_destination = destination.casefold()
    if folded_destination in FOLDED_DESTINATION_FILES:
        raise SyncError(f"forbidden import destination is owned by the consumer: {destination}")
    for prefix in FOLDED_DESTINATION_PREFIXES:
        if folded_destination == prefix.rstrip("/") or folded_destination.startswith(prefix):
            raise SyncError(f"forbidden import destination is owned by the consumer: {destination}")

    # An ancestor is just as dangerous: a directory entry rooted at "automation" with
    # delete_stale set would sweep automation/queue, automation/handoffs and automation/proofs
    # without ever naming them.
    owned = sorted(FOLDED_DESTINATION_FILES) + [prefix.rstrip("/") for prefix in FOLDED_DESTINATION_PREFIXES]
    for consumer_path in owned:
        if consumer_path.startswith(folded_destination + "/"):
            raise SyncError(
                f"forbidden import destination {destination!r} contains consumer-owned {consumer_path!r}"
            )

    return Entry(
        source=source,
        destination=destination,
        kind=kind,
        preserve_executable=bool_field(raw, "preserve_executable"),
        delete_stale=bool_field(raw, "delete_stale"),
        template=bool_field(raw, "template"),
        allow_owlory_specific=allow_owlory_specific,
    )


RUNTIME_DEPENDENCY_FIELDS = {"distribution", "import_name", "minimum_major", "maximum_major_exclusive"}


def parse_runtime_requirements(data: dict[str, Any]) -> list[RuntimeRequirement]:
    """Read the optional manifest `runtime.python.dependencies` block.

    Absent block means no declared requirements, which is the behaviour every manifest had
    before this existed.
    """
    runtime = data.get("runtime")
    if runtime is None:
        return []
    if not isinstance(runtime, dict):
        raise SyncError("manifest runtime must be an object")

    python = runtime.get("python")
    if python is None:
        return []
    if not isinstance(python, dict):
        raise SyncError("manifest runtime.python must be an object")

    raw_dependencies = python.get("dependencies", [])
    if not isinstance(raw_dependencies, list):
        raise SyncError("manifest runtime.python.dependencies must be a list")

    requirements: list[RuntimeRequirement] = []
    for raw in raw_dependencies:
        if not isinstance(raw, dict):
            raise SyncError("each runtime dependency must be an object")
        unknown = sorted(set(raw) - RUNTIME_DEPENDENCY_FIELDS)
        if unknown:
            raise SyncError(f"unknown runtime dependency fields: {', '.join(unknown)}")

        distribution = raw.get("distribution")
        import_name = raw.get("import_name", distribution)
        minimum_major = raw.get("minimum_major")
        maximum_major_exclusive = raw.get("maximum_major_exclusive")

        if not isinstance(distribution, str) or not distribution.strip():
            raise SyncError("runtime dependency needs a non-empty distribution")
        if not isinstance(import_name, str) or not import_name.strip():
            raise SyncError(f"runtime dependency {distribution} needs a non-empty import_name")
        if not isinstance(minimum_major, int) or isinstance(minimum_major, bool):
            raise SyncError(f"runtime dependency {distribution} needs an integer minimum_major")
        if not isinstance(maximum_major_exclusive, int) or isinstance(maximum_major_exclusive, bool):
            raise SyncError(f"runtime dependency {distribution} needs an integer maximum_major_exclusive")
        if maximum_major_exclusive <= minimum_major:
            raise SyncError(
                f"runtime dependency {distribution} has an empty version range "
                f"({minimum_major} >= {maximum_major_exclusive})"
            )

        requirements.append(
            RuntimeRequirement(
                distribution=distribution,
                import_name=import_name,
                minimum_major=minimum_major,
                maximum_major_exclusive=maximum_major_exclusive,
            )
        )
    return requirements


def load_manifest(path: Path) -> tuple[list[Entry], list[RuntimeRequirement]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SyncError(f"manifest not found at {path}") from error
    except json.JSONDecodeError as error:
        raise SyncError(f"manifest is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise SyncError("manifest root must be an object")
    if data.get("version") != 1:
        raise SyncError("manifest version must be 1")

    # A tool whose whole job is writing files has no safe default destination. The field
    # this replaces named the canonical repository itself - correct before the ownership
    # flip, and afterwards a --sync away from writing outward. Rejecting it rather than
    # ignoring it keeps it from drifting back in from an upstream manifest.
    if "default_target" in data:
        raise SyncError(
            "manifest must not carry default_target: the destination is the caller's to name. "
            "Pass --target explicitly."
        )

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SyncError("manifest entries must be a non-empty list")

    return [parse_entry(raw) for raw in raw_entries], parse_runtime_requirements(data)


def resolve_under(base: Path, relative: str, *, must_exist: bool) -> Path:
    base_resolved = base.resolve(strict=False)
    candidate = base.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as error:
        raise SyncError(f"source path does not exist: {relative}") from error

    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise SyncError(f"path escapes root: {relative}")
    return resolved


def should_skip(path: Path) -> bool:
    return any(part in SKIP_NAMES for part in path.parts)


def source_files(source_root: Path, target_root: Path, entry: Entry) -> list[tuple[Path, Path, str]]:
    source = resolve_under(source_root, entry.source, must_exist=True)
    destination = resolve_under(target_root, entry.destination, must_exist=False)

    if source.is_symlink():
        raise SyncError(f"symlink sources are not supported: {entry.source}")

    if entry.kind == "file":
        if not source.is_file():
            raise SyncError(f"manifest entry {entry.source} is not a file")
        return [(source, destination, entry.destination)]

    if not source.is_dir():
        raise SyncError(f"manifest entry {entry.source} is not a directory")

    pairs: list[tuple[Path, Path, str]] = []
    for child in sorted(source.rglob("*")):
        if should_skip(child):
            continue
        if child.is_symlink():
            raise SyncError(f"symlink sources are not supported: {child.relative_to(source_root)}")
        if child.is_dir():
            continue
        relative = child.relative_to(source)
        target = destination / relative
        display = PurePosixPath(entry.destination, relative.as_posix()).as_posix()
        pairs.append((child, target, display))
    return pairs


def executable_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode) & 0o111


def set_target_mode(source: Path, target: Path, *, preserve_executable: bool) -> None:
    if preserve_executable:
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    else:
        os.chmod(target, 0o644)


def file_drift(source: Path, target: Path, *, preserve_executable: bool) -> str | None:
    if not target.exists():
        return "missing"
    if not target.is_file():
        return "type"
    if source.read_bytes() != target.read_bytes():
        return "changed"
    if preserve_executable and executable_bits(source) != executable_bits(target):
        return "mode"
    return None


def stale_files(target_root: Path, entry: Entry, expected: set[Path]) -> list[tuple[Path, str]]:
    if entry.kind != "directory" or not entry.delete_stale or entry.template:
        return []

    destination = resolve_under(target_root, entry.destination, must_exist=False)
    if not destination.exists():
        return []

    stale: list[tuple[Path, str]] = []
    for child in sorted(destination.rglob("*")):
        if should_skip(child):
            continue
        if child.is_dir():
            continue
        if child not in expected:
            display = child.relative_to(target_root).as_posix()
            stale.append((child, display))
    return stale


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for child in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass


def locally_modified_targets(target_root: Path) -> set[Path]:
    """Tracked files the consumer has edited but not committed.

    Overwriting one of these destroys work that exists nowhere else - not in the consumer's
    history and not upstream. Untracked files are excluded: a brand new file in a vendored
    directory is stale, which is a different decision handled by delete_stale.
    """
    toplevel_result = git_result(target_root, "rev-parse", "--show-toplevel")
    if toplevel_result.returncode != 0:
        # A consumer that is not under version control has no uncommitted work to lose, so
        # there is nothing for this protection to do. Every other failure - dubious
        # ownership, a missing git, an unreadable index - must not silently switch it off.
        if "not a git repository" in toplevel_result.stderr.lower():
            return set()
        raise SyncError(
            f"could not determine whether {target_root} has uncommitted changes, so the import "
            f"cannot promise not to overwrite them: {toplevel_result.stderr.strip() or 'git failed'}"
        )

    # Status paths are relative to the repository root, not to -C, so a consumer vendored in
    # a subdirectory needs the toplevel to rebuild them.
    toplevel = Path(toplevel_result.stdout.strip())

    status = git_result(target_root, "status", "--porcelain", "-z", "--untracked-files=no")
    if status.returncode != 0:
        raise SyncError(f"could not read consumer status: {status.stderr.strip() or 'git failed'}")

    # -z emits "XY <path>\0", and for a rename or copy a second "\0<source path>" follows.
    # It is the one format that does not quote or escape unusual bytes in a filename.
    fields = status.stdout.split("\0")
    modified: set[Path] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        states, entry_path = field[:2], field[3:]
        if "R" in states or "C" in states:
            index += 1
        modified.add((toplevel / entry_path).resolve(strict=False))
    return modified


@dataclass(frozen=True)
class Plan:
    writes: list[tuple[Path, Path, str, Entry]]
    deletions: list[tuple[Path, str]]
    issues: list[str]
    file_count: int = 0


def plan_entries(
    source_root: Path,
    target_root_resolved: Path,
    entries: list[Entry],
    *,
    force_templates: bool = False
) -> Plan:
    """Compare the source with the consumer, entry by entry, without touching either.

    --check and --status report `issues`; --sync carries out `writes` and `deletions`.
    """
    writes: list[tuple[Path, Path, str, Entry]] = []
    deletions: list[tuple[Path, str]] = []
    issues: list[str] = []
    file_count = 0

    for entry in entries:
        pairs = source_files(source_root, target_root_resolved, entry)
        file_count += len(pairs)
        expected = {target for _, target, _ in pairs}

        for source, target, display in pairs:
            if entry.template and target.exists() and not force_templates:
                continue
            drift = file_drift(source, target, preserve_executable=entry.preserve_executable)
            if drift is None:
                continue
            issues.append(f"{drift}: {display}")
            writes.append((source, target, display, entry))

        for stale, display in stale_files(target_root_resolved, entry, expected):
            issues.append(f"stale: {display}")
            deletions.append((stale, display))

    return Plan(writes=writes, deletions=deletions, issues=sorted(issues), file_count=file_count)


def sync_entries(
    source_root: Path,
    target_root: Path,
    entries: list[Entry],
    *,
    check: bool,
    force_templates: bool = False,
    tracer: Any = None,
) -> int:
    tracer = tracer or NoTelemetry()
    target_root_resolved = target_root.resolve(strict=False)

    # Plan the whole import before touching anything, so a refusal leaves the consumer
    # byte-for-byte unchanged rather than half-imported.
    with traced(tracer, "boundroom.sync.plan", check=check) as span:
        plan = plan_entries(source_root, target_root_resolved, entries, force_templates=force_templates)
        writes, deletions, issues = plan.writes, plan.deletions, plan.issues
        tracer.annotate(span, file_count=plan.file_count, drift_count=len(issues) if check else len(writes) + len(deletions))

        if not check:
            # Part of the plan rather than the write: it is the last refusal before anything changes.
            modified = locally_modified_targets(target_root_resolved)
            clobbered = sorted(
                display
                for path, display in [(target, display) for _, target, display, _ in writes] + deletions
                if path.resolve(strict=False) in modified
            )
            if clobbered:
                raise SyncError(
                    "refusing to import over locally modified files: "
                    + ", ".join(clobbered)
                    + ". Commit, stash or revert them first - an import overwrites them silently otherwise."
                )
        tracer.annotate(span, result="planned")

    if check:
        if issues:
            for issue in issues:
                print(issue)
            print(f"result: drift found ({len(issues)} issue(s))")
            return 1
        print("result: target is current")
        return 0

    changes = 0
    with traced(tracer, "boundroom.sync.write") as write:
        for source, target, display, entry in writes:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            set_target_mode(source, target, preserve_executable=entry.preserve_executable)
            print(f"synced: {display}")
            changes += 1

        for stale, display in deletions:
            stale.unlink()
            print(f"removed stale: {display}")
            changes += 1

        for entry in entries:
            if entry.kind == "directory" and entry.delete_stale:
                remove_empty_dirs(resolve_under(target_root_resolved, entry.destination, must_exist=False))
        tracer.annotate(write, written=len(writes), removed=len(deletions))

    if changes:
        print(f"result: synced {changes} change(s)")
    else:
        print("result: already current")
    return 0


def git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git without raising. Callers that must tell one failure from another read stderr."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def git_output(repo: Path, *args: str) -> tuple[int, str]:
    result = git_result(repo, *args)
    return result.returncode, result.stdout.strip()


def ensure_target_is_not_canonical(source_root: Path, target_root: Path, expect_remote: str | None) -> None:
    """Protection 1: an import has no write path back into the canonical repository.

    The old --auto-update mode wrote outward and once resolved to deleting 1937 lines of
    committed canonical work. There is no flag to re-enable that: files move canonical ->
    consumer only.

    Path containment alone does not establish that. Two checkouts of the canonical
    repository sitting side by side contain neither the other, so the destructive case
    looks like an ordinary import right up until delete_stale runs. What makes a
    destination unsafe is its identity, not its location.
    """
    source = source_root.resolve(strict=False)
    target = target_root.resolve(strict=False)

    if source == target:
        raise SyncError(f"refusing to import into the canonical source itself: {target}")
    if source in target.parents:
        raise SyncError(f"refusing to import into {target}, which lives inside the canonical source {source}")
    if target in source.parents:
        raise SyncError(f"refusing to import into {target}, which contains the canonical source {source}")

    # Worktrees of one repository share a common git dir, and that holds even when neither
    # has a remote configured. This is the sibling-checkout case.
    source_code, source_common = git_output(source, "rev-parse", "--git-common-dir")
    target_code, target_common = git_output(target, "rev-parse", "--git-common-dir")
    if source_code == 0 and target_code == 0:
        if (source / source_common).resolve(strict=False) == (target / target_common).resolve(strict=False):
            raise SyncError(
                f"refusing to import into {target}: it is a checkout of the canonical source {source}"
            )

    # Independent clones do not share a git dir, so fall back to what the repository says it
    # is. Protection 3 already applies this to the source; the destination needs it more,
    # because that is the side that gets written to.
    target_remote_code, target_remote = git_output(target, "remote", "get-url", "origin")
    if target_remote_code != 0 or not target_remote:
        return

    source_remote_code, source_remote = git_output(source, "remote", "get-url", "origin")
    canonical_remotes = {remote for remote in (expect_remote, source_remote if source_remote_code == 0 else None) if remote}
    if target_remote in canonical_remotes:
        raise SyncError(
            f"refusing to import into {target}: its 'origin' is the canonical repository {target_remote}"
        )


def verify_source_provenance(source_root: Path, pin: str | None, expect_remote: str | None) -> None:
    """Protections 2, 3 and 4: the snapshot must be traceable to a published commit.

    Cleanliness is not provenance. The destructive 2026-09-14 run was against a clean
    worktree on the correct branch; what it lacked was any statement of which commit it
    was meant to be. A checkout that merely resembles the canonical repository is refused.
    """
    if not pin:
        raise SyncError(
            "refusing to import without --pin: the canonical source commit must be explicit. "
            "Pass --pin <full-sha> (Owlory reads it from automation/repo-automation.lock), "
            "or --allow-unverified-source for local development only."
        )

    is_repo, _ = git_output(source_root, "rev-parse", "--show-toplevel")
    if is_repo != 0:
        raise SyncError(f"source is not a Git checkout, so its identity cannot be verified: {source_root}")

    # Protection 3. An absent origin is a failure, not a pass - a source that cannot state
    # what repository it is, is exactly the case this check exists for.
    remote_code, remote_url = git_output(source_root, "remote", "get-url", "origin")
    if remote_code != 0 or not remote_url:
        raise SyncError(
            f"source repository identity cannot be established: {source_root} has no 'origin' remote"
        )
    if expect_remote and remote_url != expect_remote:
        raise SyncError(
            f"source repository identity mismatch: expected {expect_remote}, found {remote_url}"
        )

    # Protection 4. Uncommitted content is unpublished by definition.
    status_code, status = git_output(source_root, "status", "--porcelain", "--untracked-files=all")
    if status_code != 0:
        raise SyncError(f"could not read source status: {source_root}")
    if status:
        raise SyncError(
            f"canonical source has uncommitted changes, so the import would vendor unpublished content:\n{status}"
        )

    head_code, head = git_output(source_root, "rev-parse", "HEAD")
    if head_code != 0:
        raise SyncError(f"could not resolve source HEAD: {source_root}")
    if head != pin:
        raise SyncError(
            f"source is not at the pinned commit: pinned {pin}, source HEAD {head}"
        )


DEFAULT_QUALITY_COMMAND = "make reusable-check"


def verify_source_quality(source_root: Path, command: str) -> None:
    """Protection 8: provenance is identity, not quality.

    A source can sit at the pinned commit, on the right remote, with a clean worktree, and
    still be lint-red or schema-drifted - canonical main was exactly that for two commits
    on 2026-09-15. Pinning says which snapshot; this says the snapshot is one the canonical
    repository considers releasable.

    The gate is named, not enumerated: the tool asks the source to run its own contract
    (`make reusable-check`) rather than learning which linters exist this month.
    """
    argv = shlex.split(command)
    if not argv:
        raise SyncError("empty --quality-command: the canonical quality gate must be a runnable command")

    try:
        result = subprocess.run(argv, cwd=source_root, capture_output=True, text=True, check=False)
    except OSError as error:
        raise SyncError(
            f"could not run the canonical quality gate {command!r} in {source_root}: {error}"
        ) from error

    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
        raise SyncError(
            f"canonical source failed its own quality gate ({command}), so the import would "
            f"vendor content the canonical repository does not consider releasable:\n"
            + "\n".join(tail)
        )


def consumer_versions(python_executable: str, distributions: list[str]) -> dict[str, str | None]:
    """Ask the consumer's interpreter which versions it actually has installed.

    The canonical checkout's own environment says nothing about what the consumer will run
    the imported harness with, so the probe runs in that interpreter rather than this one.
    """
    program = (
        "import json, sys\n"
        "from importlib.metadata import PackageNotFoundError, version\n"
        "found = {}\n"
        "for distribution in sys.argv[1:]:\n"
        "    try:\n"
        "        found[distribution] = version(distribution)\n"
        "    except PackageNotFoundError:\n"
        "        found[distribution] = None\n"
        "print(json.dumps(found))\n"
    )
    try:
        result = subprocess.run(
            [python_executable, "-c", program, *distributions],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise SyncError(f"could not run the consumer interpreter {python_executable!r}: {error}") from error

    if result.returncode != 0:
        raise SyncError(
            f"consumer interpreter {python_executable!r} could not report installed versions: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SyncError(
            f"consumer interpreter {python_executable!r} returned unreadable version output: {error}"
        ) from error
    return parsed


def major_of(version: str) -> int | None:
    head = version.split(".", 1)[0].strip()
    return int(head) if head.isdigit() else None


def verify_consumer_runtime(requirements: list[RuntimeRequirement], python_executable: str) -> None:
    """Protection 9: the consumer must be able to execute what it is about to vendor.

    Source provenance and source quality are facts about the canonical repository. This is a
    fact about the destination, so --allow-unverified-source does not waive it: trusting an
    unpublished source is not the same as installing code that cannot run.
    """
    if not requirements:
        return

    installed = consumer_versions(python_executable, [requirement.distribution for requirement in requirements])

    problems = []
    for requirement in requirements:
        version = installed.get(requirement.distribution)
        if version is None:
            problems.append(f"missing_runtime_dependency: {requirement.spec}")
            continue
        major = major_of(version)
        if major is None or not (requirement.minimum_major <= major < requirement.maximum_major_exclusive):
            problems.append(f"missing_runtime_dependency: {requirement.spec} (found {version})")

    if problems:
        raise SyncError(
            "\n".join(problems)
            + f"\nchecked with {python_executable}; pass --runtime-python to name the interpreter "
            "the consumer's harness actually runs under."
        )


# --- --status: read-only pin report (RA-PROD-103) ---------------------------------------------
#
# Everything below only reads. It never writes the lock or any other file, and it prints the
# update and rollback commands as text without running them. Git is called with
# --no-optional-locks so that even `git status` leaves the consumer's index alone.

LOCK_NAMES = ("automation/boundroom.lock", "automation/repo-automation.lock")
LOCK_FIELDS = ("commit", "remote", "repository", "imported_at")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
LOCK_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*(?::=|\?=|=|:)\s*(.*)$")
LOCK_HISTORY_DEPTH = 50


def parse_lock(text: str) -> dict[str, str] | None:
    """Read a lock written as a JSON object or as `key=value` / `key: value` lines.

    The consumer owns the lock's format, so this accepts the common shapes rather than one.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return {str(key).lower(): str(value) for key, value in data.items() if isinstance(value, (str, int))}

    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LOCK_LINE.match(stripped)
        if match:
            fields[match.group(1).lower()] = match.group(2).strip().strip("\"'")
    return fields or None


def read_only_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return git_result(repo, "--no-optional-locks", *args)


def git_value(repo: Path, *args: str) -> str | None:
    result = read_only_git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def tags_at(source_root: Path, sha: str) -> str:
    tags = git_value(source_root, "tag", "--points-at", sha)
    return f" (tag {', '.join(tags.split())})" if tags else ""


def previous_pin(target_root: Path, lock_relative: str, current: str | None) -> str | None:
    """The most recent earlier `commit` the lock held, from the consumer's own history."""
    log = git_value(target_root, "log", "-n", str(LOCK_HISTORY_DEPTH), "--follow", "--format=%H", "--", lock_relative)
    if not log:
        return None
    for commit in log.splitlines():
        for name in LOCK_NAMES:
            shown = read_only_git(target_root, "show", f"{commit}:{name}")
            if shown.returncode != 0:
                continue
            value = (parse_lock(shown.stdout) or {}).get("commit")
            if value and value != current:
                return value
    return None


def script_at(source_root: Path, sha: str) -> str:
    """The sync script's name at `sha`: tags before 0.3.0 carry only the old name (SPEC-005)."""
    if read_only_git(source_root, "cat-file", "-e", f"{sha}:Tools/boundroom-sync.sh").returncode == 0:
        return "Tools/boundroom-sync.sh"
    return "Tools/repo-automation-sync.sh"


def sync_commands(source_root: Path, target_root: Path, sha: str, remote: str | None) -> list[str]:
    script = script_at(source_root, sha)
    common = f"--target {shlex.quote(str(target_root))} --pin {sha} --expect-remote {shlex.quote(remote) if remote else '<remote-url>'}"
    return [
        f"cd {shlex.quote(str(source_root))} && git checkout --detach {sha}",
        f"{script} --check {common}",
        f"{script} --sync {common}   # only after the owner says go, on a new consumer branch",
        f"then set the lock's commit to {sha} and imported_at to the import time; change no other field",
    ]


def pin_status(
    source_root: Path,
    target_root: Path,
    entries: list[Entry],
    runtime_requirements: list[RuntimeRequirement],
    *,
    desired: str | None,
    expect_remote: str | None,
    runtime_python: str,
) -> int:
    attention: list[str] = []
    target = target_root.resolve(strict=False)
    print(f"target: {target}")
    if not target.exists():
        print("result: attention needed: target missing")
        return 1

    # The lock: which file, and what it pins.
    present = [name for name in LOCK_NAMES if (target / name).is_file()]
    lock: dict[str, str] = {}
    lock_name: str | None = None
    if not present:
        print(f"lock: none ({' or '.join(LOCK_NAMES)} not found)")
        attention.append("no lock")
    else:
        lock_name = present[0]
        also = f" ({present[1]} also exists)" if len(present) > 1 else ""
        print(f"lock: {lock_name}{also}")
        parsed = parse_lock((target / lock_name).read_text(encoding="utf-8", errors="replace"))
        if parsed is None:
            print("lock fields: unreadable (neither a JSON object nor key=value lines)")
            attention.append("lock unreadable")
        else:
            lock = parsed
        for field in LOCK_FIELDS:
            print(f"lock {field}: {lock.get(field, '(missing)')}")

    pin = lock.get("commit")
    if lock_name and lock and not (pin and FULL_SHA.fullmatch(pin)):
        attention.append("lock commit is not a full SHA")
        pin = None

    # The source checkout the comparison runs against.
    source_head = git_value(source_root, "rev-parse", "HEAD")
    source_origin = git_value(source_root, "remote", "get-url", "origin")
    source_dirty = git_value(source_root, "status", "--porcelain", "--untracked-files=all")
    print(f"source: {source_root}")
    print(f"source head: {source_head + tags_at(source_root, source_head) if source_head else '(not a Git checkout)'}")
    print(f"source origin: {source_origin or '(none)'}")
    if source_dirty:
        print(f"source worktree: dirty ({len(source_dirty.splitlines())} path(s))")
        attention.append("source has uncommitted changes")

    if pin:
        if read_only_git(source_root, "cat-file", "-e", f"{pin}^{{commit}}").returncode == 0:
            print(f"pin: {pin}{tags_at(source_root, pin)}")
        else:
            print(f"pin: {pin} (not in the source checkout; fetch it first)")
            attention.append("pinned commit not in the source checkout")

    lock_remote = lock.get("remote")
    remote = expect_remote or lock_remote or source_origin
    if lock_remote and source_origin and lock_remote != source_origin:
        print(
            f"remote: the lock names {lock_remote} but the source's origin is {source_origin}; "
            "a pinned check refuses until the consumer's owner updates the lock's remote"
        )
        attention.append("lock remote differs from the source origin")

    # Desired ref, resolved in the source checkout.
    desired_sha: str | None = None
    if desired:
        desired_sha = git_value(source_root, "rev-parse", "--verify", "--quiet", f"{desired}^{{commit}}")
        if not desired_sha:
            print(f"desired: {desired} (not found in the source checkout; try git fetch --tags)")
            attention.append("desired ref not found")
        else:
            print(f"desired: {desired} -> {desired_sha}{tags_at(source_root, desired_sha)}")
            if not pin:
                print("pin vs desired: no pin to compare")
            elif pin == desired_sha:
                print("pin vs desired: same")
            else:
                print("pin vs desired: differs")
                attention.append("desired differs from the pin")

    # Drift: the same manifest comparison --check makes, against the source as checked out.
    if source_head and pin == source_head:
        print(f"drift against: source head {source_head} (the pinned commit)")
    elif source_head:
        print(
            f"drift against: source head {source_head}, not the pinned commit; "
            "these are the files a sync to that head would change"
        )
        if pin:
            attention.append("source is not at the pinned commit")
    if source_dirty:
        print("drift note: the source has uncommitted changes, so this compares its working tree, not a commit")
    issues = plan_entries(source_root, target, entries).issues
    for issue in issues:
        print(f"  {issue}")
    if issues:
        print(f"drift: found ({len(issues)} issue(s))")
        attention.append("drift found")
    else:
        print("drift: none")

    # The consumer's own Git state.
    status = read_only_git(target, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        print("consumer worktree: not a Git repository")
        attention.append("consumer is not a Git repository")
    elif status.stdout.strip():
        lines = status.stdout.rstrip("\n").splitlines()
        print(f"consumer worktree: dirty ({len(lines)} path(s))")
        for line in lines:
            print(f"  {line}")
        attention.append("consumer worktree is not clean")
    else:
        print("consumer worktree: clean")

    # Whether the consumer can run what the manifest declares.
    if not runtime_requirements:
        print("runtime: nothing declared")
    else:
        try:
            verify_consumer_runtime(runtime_requirements, runtime_python)
            print(f"runtime: ok ({runtime_python})")
        except SyncError as error:
            print(f"runtime: {str(error).splitlines()[0]}")
            attention.append("runtime dependency missing")

    # Commands, as text only.
    print("commands (printed only; --status runs none of them):")
    if pin:
        print(f"  reproduce the check of the current pin {pin}:")
        for line in sync_commands(source_root, target, pin, remote)[:2]:
            print(f"    {line}")
    if desired_sha and desired_sha != pin:
        print(f"  update to {desired} ({desired_sha}):")
        for line in sync_commands(source_root, target, desired_sha, remote):
            print(f"    {line}")
    previous = previous_pin(target, lock_name, pin) if lock_name else None
    if previous and previous == desired_sha:
        print(f"  roll back to the previous pin {previous}: the update above is that rollback.")
    elif previous:
        print(f"  roll back to the previous pin {previous}{tags_at(source_root, previous)} (from the lock's Git history):")
        for line in sync_commands(source_root, target, previous, remote):
            print(f"    {line}")
    elif pin:
        print("  roll back: the lock's Git history holds no earlier pin.")
    if pin and desired_sha and desired_sha != pin:
        print(f"  to undo an update to {desired}, sync back to the current pin {pin} the same way.")

    if attention:
        print(f"result: attention needed: {'; '.join(attention)}")
        return 1
    print("result: current")
    return 0


# --- telemetry (SPEC-006) -----------------------------------------------------------------


class NoTelemetry:
    """The handle when telemetry is off or can't load: every call costs nothing."""

    run_id = ""
    messages: list[str] = []

    def span(self, name: str, **attributes: Any) -> Any:
        return nullcontext()

    def mark(self, name: str, **attributes: Any) -> None:
        return None

    def annotate(self, span: Any, **attributes: Any) -> None:
        return None

    def flush(self) -> bool:
        return True


def load_telemetry(tool_root: Path) -> tuple[Any, Any, str]:
    """The supervisor's telemetry modules, from the checkout this tool runs from.

    Never from --source: that is whatever path the caller named, and importing from it would
    run its code before provenance has checked it. First on sys.path, so an ``automation``
    directory in the working directory, such as a consumer's vendored copy, can't stand in.

    Until SPEC-006 this tool needed only the standard library, and a consumer's make may still
    run it under an interpreter without pydantic. That leaves the tool working with telemetry
    off, rather than broken.
    """
    sys.path.insert(0, str(tool_root))
    try:
        from automation.supervisor import telemetry, telemetry_settings
    except ImportError as error:
        return None, None, str(error)
    return telemetry, telemetry_settings, ""


def without_telemetry(arguments: list[str], problem: str) -> list[str]:
    """The arguments to parse when the telemetry modules can't load, or a refusal.

    Telemetry asked for by flag, or required, must not quietly not happen. --no-telemetry asks
    for what is already true, so it is dropped rather than refused.
    """
    asked = [argument for argument in arguments if argument.startswith("--telemetry")]
    if asked or os.environ.get("BOUNDROOM_TELEMETRY_REQUIRED") == "true":
        raise SyncError(
            f"telemetry was asked for, but this interpreter can't load it ({problem}). "
            "Run the tool under Boundroom's Python environment, or without telemetry."
        )
    chosen = [name for name in os.environ if name.startswith("BOUNDROOM_TELEMETRY") and os.environ[name] not in ("", "off")]
    if chosen:
        print(f"telemetry: off, because this interpreter can't load it ({problem})", file=sys.stderr)
    return [argument for argument in arguments if argument != "--no-telemetry"]


def start_telemetry(telemetry: Any, telemetry_settings: Any, args: argparse.Namespace, roots: tuple[Path, ...]) -> Any:
    """Configure telemetry from the user's settings; off, the default, imports no SDK (UD-1).

    ``roots`` are the consumer first, then the source and this tool's own checkout: a spans
    file in the consumer would land in the sync's pull request, and one in the source would
    fail the next sync's cleanliness check.
    """
    if telemetry is None:
        return NoTelemetry()
    settings = telemetry_settings.resolve(flags=vars(args), environ=os.environ)
    for message in settings.messages:
        print(message, file=sys.stderr)
    if settings.refusal:
        raise SyncError(settings.refusal)
    try:
        tracer = telemetry.configure(
            settings,
            repo_root=roots[0],
            outside=roots[1:],
            run_id=telemetry.new_run_id(),
            service_version=telemetry.version(roots[-1]),
        )
    except telemetry.TelemetryRefused as error:
        raise SyncError(str(error)) from error
    for message in tracer.messages:
        print(message, file=sys.stderr)
    return tracer


# A remote URL can carry a token as its user part; neither the expected remote nor a refusal
# that quotes one records it.
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/@\s]+@")
_REASON_CHARS = 200


def without_credentials(text: str) -> str:
    return _URL_CREDENTIALS.sub("", text)


@contextmanager
def traced(tracer: Any, name: str, **attributes: Any) -> Iterator[Any]:
    """A span that records a refusal as its result and reason, then lets it propagate.

    The refusal leaves the span before it is raised again, so the span carries the scrubbed
    first line only, never an exception message or traceback that may quote a remote URL.
    """
    refusal = None
    with tracer.span(name, **{key: value for key, value in attributes.items() if value is not None}) as span:
        try:
            yield span
        except SyncError as error:
            refusal = error
            tracer.annotate(span, result="refused", reason=without_credentials(str(error).splitlines()[0])[:_REASON_CHARS])
    if refusal is not None:
        raise refusal


def main(argv: list[str]) -> int:
    repo_root = Path(argv[0]).resolve()
    telemetry, telemetry_settings, problem = load_telemetry(repo_root)
    arguments = argv[1:] if telemetry_settings is not None else without_telemetry(argv[1:], problem)

    parser = argparse.ArgumentParser(description="Import reusable automation from the canonical Boundroom checkout into a consumer.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Report drift without changing files.")
    mode.add_argument("--sync", action="store_true", help="Update the target to match the reusable manifest.")
    mode.add_argument(
        "--status",
        action="store_true",
        help="Read-only. Report the consumer's lock and pin, drift against the source as checked out, "
        "and whether the consumer's worktree is clean, then print the update and rollback commands "
        "without running them. Exits 1 when anything needs attention.",
    )
    parser.add_argument(
        "--desired",
        help="With --status: the tag or commit you want the consumer on, resolved in the source checkout "
        "and compared with the lock's pin.",
    )
    parser.add_argument("--target", required=True, help="Destination consumer folder. There is no default.")
    parser.add_argument("--source", default=str(repo_root), help="Canonical source repository root. Defaults to this checkout.")
    parser.add_argument("--manifest", help="Manifest path. Defaults to <source>/automation/reusable-manifest.json.")
    parser.add_argument("--pin", help="Full commit SHA the canonical source must be checked out at.")
    parser.add_argument("--expect-remote", help="Remote URL the canonical source's 'origin' must match.")
    parser.add_argument(
        "--quality-command",
        default=DEFAULT_QUALITY_COMMAND,
        help=f"Canonical quality gate the source must pass before its content is imported. Default: {DEFAULT_QUALITY_COMMAND!r}."
    )
    parser.add_argument(
        "--runtime-python",
        default=sys.executable,
        help="Interpreter the consumer's harness runs under, checked against the manifest's declared "
        "runtime dependencies. Defaults to the interpreter running this tool."
    )
    parser.add_argument(
        "--allow-unverified-source",
        action="store_true",
        help="Development only. Skip the pin, identity, cleanliness and quality checks on the source."
    )
    parser.add_argument(
        "--force-templates",
        action="store_true",
        help="Re-baseline template entries to source content even when destination files already exist. "
        "Consumer-added files in template directories still survive."
    )
    if telemetry_settings is not None:
        telemetry_settings.add_flags(parser)
    args = parser.parse_args(arguments)
    if args.desired and not args.status:
        parser.error("--desired is used only with --status")
    if args.status and args.pin:
        parser.error("--status reads the pin from the consumer's lock; use --desired for the ref you want")

    source_root = Path(args.source).expanduser().resolve(strict=True)
    target_root = Path(args.target).expanduser()
    tracer = start_telemetry(telemetry, telemetry_settings, args, (target_root, source_root, repo_root))
    # Flushed however the run ends; the flush is bounded and never changes the exit code.
    try:
        mode = "sync" if args.sync else "status" if args.status else "check"
        with traced(tracer, "boundroom.run", entry="sync", run_id=tracer.run_id, mode=mode) as run:
            code = sync_run(args, source_root, target_root, tracer)
            tracer.annotate(run, exit_code=code)
            return code
    finally:
        tracer.flush()


def sync_run(args: argparse.Namespace, source_root: Path, target_root: Path, tracer: Any) -> int:
    manifest = Path(args.manifest).expanduser() if args.manifest else source_root / "automation/reusable-manifest.json"
    entries, runtime_requirements = load_manifest(manifest)

    expected_remote = without_credentials(args.expect_remote) if args.expect_remote else None
    with traced(tracer, "boundroom.sync.verify", pin=args.pin, expected_remote=expected_remote) as verify:
        ensure_target_is_not_canonical(source_root, target_root, args.expect_remote)

        if args.status:
            # Read-only: --status skips the source checks and reports what it finds instead.
            tracer.annotate(verify, result="status only")
            return pin_status(
                source_root,
                target_root,
                entries,
                runtime_requirements,
                desired=args.desired,
                expect_remote=args.expect_remote,
                runtime_python=args.runtime_python,
            )

        # --check answers "does the snapshot match the commit it is pinned to", so it needs the
        # same provenance as --sync whenever a pin is supplied. Without a pin it stays a plain
        # drift report, which is what the development fixtures use.
        result = "unpinned drift report"
        if args.sync or args.pin:
            if args.allow_unverified_source:
                print(
                    "boundroom-sync: warning: reading from an unverified source "
                    "(--allow-unverified-source); the result is neither traceable to a published "
                    "commit nor known to pass the canonical quality gate.",
                    file=sys.stderr,
                )
                result = "unverified source"
            else:
                verify_source_provenance(source_root, args.pin, args.expect_remote)
                verify_source_quality(source_root, args.quality_command)
                result = "verified"

        # Identity and quality describe the source; this describes the destination, so it runs
        # for every mode and is not covered by --allow-unverified-source.
        verify_consumer_runtime(runtime_requirements, args.runtime_python)
        tracer.annotate(verify, result=result)

    if args.sync:
        target_root.mkdir(parents=True, exist_ok=True)
    elif not target_root.exists():
        tracer.mark("boundroom.sync.plan", check=True, result="target missing")
        print(f"missing target: {target_root}")
        print("result: drift found (target missing)")
        return 1

    return sync_entries(
        source_root,
        target_root,
        entries,
        check=args.check,
        force_templates=args.force_templates,
        tracer=tracer,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SyncError as error:
        raise SystemExit(fail(str(error)))
PY
