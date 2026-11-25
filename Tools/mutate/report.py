"""Scores, the allowlist, the baseline ratchet and the printed table.

Pure functions over plain values: they read nothing from disk and run nothing, so the CLI
is the only place that does I/O.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from .operators import OPERATORS, Mutant, apply_mutant

# Run outcomes, in the order the table prints them.
KILLED = "killed"
TIMEOUT = "timeout"
SURVIVED = "survived"
NO_COVERAGE = "no_coverage"
ALLOWLISTED = "allowlisted"
ERROR = "error"
STATUSES = (KILLED, TIMEOUT, SURVIVED, NO_COVERAGE, ALLOWLISTED, ERROR)

ALLOWLIST_FIELDS: dict[str, type] = {
    "module": str,
    "line": int,
    "operator": str,
    "original": str,
    "mutant": str,
    "reason": str,
    "reviewed_by": str,
}


class AllowlistError(ValueError):
    """The allowlist file doesn't match its schema."""


@dataclass(frozen=True)
class Outcome:
    """What happened to one mutant."""

    mutant: Mutant
    status: str
    tests: int = 0
    duration: float = 0.0
    timeout: float | None = None


def report_name(module: str) -> str:
    """The report's file stem: ``automation/supervisor/policy.py`` -> ``automation.supervisor.policy``."""
    stem = module[:-3] if module.endswith(".py") else module
    return stem.replace("/", ".")


def score(counts: dict[str, int]) -> float | None:
    """(killed + timeout) / (total - allowlisted - error), rounded to 4 places; None with nothing to measure."""
    measured = counts["total"] - counts[ALLOWLISTED] - counts[ERROR]
    if measured <= 0:
        return None
    return round((counts[KILLED] + counts[TIMEOUT]) / measured, 4)


def count(outcomes: list[Outcome]) -> dict[str, int]:
    counts = {"total": len(outcomes), **{status: 0 for status in STATUSES}}
    for outcome in outcomes:
        counts[outcome.status] += 1
    return counts


def validate_allowlist(document: Any) -> list[dict[str, Any]]:
    """The entries of an allowlist document, checked against the schema."""
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        raise AllowlistError('the allowlist must be a JSON object with an "entries" list')
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(document["entries"]):
        if not isinstance(entry, dict):
            raise AllowlistError(f"allowlist entry {index} is not an object")
        missing = [key for key in ALLOWLIST_FIELDS if key not in entry]
        if missing:
            raise AllowlistError(f"allowlist entry {index} lacks {', '.join(missing)}")
        for key, kind in ALLOWLIST_FIELDS.items():
            value = entry[key]
            if not isinstance(value, kind) or isinstance(value, bool):
                raise AllowlistError(f"allowlist entry {index}: {key} must be a {kind.__name__}")
        if entry["operator"] not in OPERATORS:
            raise AllowlistError(f"allowlist entry {index}: unknown operator {entry['operator']!r}")
        for key in ("reason", "reviewed_by"):
            if not entry[key].strip():
                raise AllowlistError(f"allowlist entry {index}: {key} is empty")
        entries.append(entry)
    return entries


def _key(module: str, line: int, operator: str, original: str, mutant: str) -> tuple[str, int, str, str, str]:
    return (module, line, operator, original, mutant)


def match_allowlist(module: str, mutants: list[Mutant], entries: list[dict[str, Any]]) -> tuple[set[int], list[dict[str, Any]]]:
    """The indexes of ``mutants`` an entry excludes, and the module's entries that match none.

    An entry excludes a mutant only when module, line, operator, original and mutated text
    are all exactly equal.
    """
    by_key: dict[tuple[str, int, str, str, str], list[int]] = {}
    for index, mutant in enumerate(mutants):
        by_key.setdefault(_key(module, mutant.line, mutant.operator, mutant.original, mutant.mutant), []).append(index)
    excluded: set[int] = set()
    stale: list[dict[str, Any]] = []
    for entry in entries:
        if entry["module"] != module:
            continue
        hits = by_key.get(_key(entry["module"], entry["line"], entry["operator"], entry["original"], entry["mutant"]))
        if hits:
            excluded.update(hits)
        else:
            stale.append(entry)
    return excluded, stale


def unified_diff(module: str, source: str, mutant: Mutant) -> str:
    mutated = apply_mutant(source, mutant)
    return "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            mutated.splitlines(keepends=True),
            fromfile=f"a/{module}",
            tofile=f"b/{module}",
            n=1,
        )
    )


def module_report(
    module: str,
    source: str,
    outcomes: list[Outcome],
    stale: list[dict[str, Any]],
    *,
    commit: str | None = None,
    dirty: bool = False,
    excluded_tests: list[str],
) -> dict[str, Any]:
    counts = count(outcomes)
    survivors = [
        {
            "location": f"{module}:{outcome.mutant.line}",
            "line": outcome.mutant.line,
            "operator": outcome.mutant.operator,
            "original": outcome.mutant.original,
            "mutant": outcome.mutant.mutant,
            "status": outcome.status,
            "diff": unified_diff(module, source, outcome.mutant),
        }
        for outcome in outcomes
        if outcome.status in (SURVIVED, NO_COVERAGE)
    ]
    return {
        "module": module,
        "commit": commit,
        "dirty": dirty,
        **counts,
        "score": score(counts),
        "survivors": survivors,
        "stale_allowlist": stale,
        "excluded_tests": excluded_tests,
        "mutants": [
            {
                "line": outcome.mutant.line,
                "end_line": outcome.mutant.end_line,
                "operator": outcome.mutant.operator,
                "original": outcome.mutant.original,
                "mutant": outcome.mutant.mutant,
                "status": outcome.status,
                "tests": outcome.tests,
                "duration": round(outcome.duration, 3),
                "timeout": outcome.timeout,
            }
            for outcome in outcomes
        ],
    }


def baseline_failures(reports: list[dict[str, Any]], baseline: dict[str, Any]) -> list[str]:
    """One message per module whose score fell below its baseline score, or couldn't be scored."""
    modules = baseline.get("modules", {}) if isinstance(baseline, dict) else {}
    failures = []
    for report in reports:
        recorded = modules.get(report["module"])
        floor = recorded.get("score") if isinstance(recorded, dict) else None
        if not isinstance(floor, (int, float)) or isinstance(floor, bool):
            continue
        current = report["score"]
        if current is None:
            failures.append(f"{report['module']}: no score (nothing measured) but the baseline is {float(floor):.1%}")
        elif current < round(float(floor), 4):
            failures.append(f"{report['module']}: score {current:.1%} is below the baseline {float(floor):.1%}")
    return failures


def updated_baseline(baseline: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    """``baseline`` with each reported module's numbers replaced by this run's."""
    updated = dict(baseline)
    modules = dict(updated.get("modules", {}))
    for report in reports:
        modules[report["module"]] = {key: report[key] for key in ("score", "total", *STATUSES, "commit", "dirty")}
    updated["modules"] = dict(sorted(modules.items()))
    if isinstance(updated.get("not_yet_measured"), list):
        updated["not_yet_measured"] = [module for module in updated["not_yet_measured"] if module not in modules]
    return updated


def render_table(reports: list[dict[str, Any]]) -> str:
    headers = ["module", "total", "killed", "timeout", "survived", "no_cov", "allowlisted", "error", "score"]
    rows = [
        [
            report["module"],
            *(str(report[key]) for key in ("total", *STATUSES)),
            "n/a" if report["score"] is None else f"{report['score']:.1%}",
        ]
        for report in reports
    ]
    widths = [max(len(row[column]) for row in [headers, *rows]) for column in range(len(headers))]

    def line(cells: list[str]) -> str:
        first = cells[0].ljust(widths[0])
        return "  ".join([first, *(cell.rjust(width) for cell, width in zip(cells[1:], widths[1:]))]).rstrip()

    return "\n".join([line(headers), line(["-" * width for width in widths]), *(line(row) for row in rows)])
