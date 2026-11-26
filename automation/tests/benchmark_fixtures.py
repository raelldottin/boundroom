"""Fixtures shared by the benchmark's scoring, eval-runner and CLI tests."""

from __future__ import annotations

from typing import Any, Optional

from automation.benchmark import scoring

DEFAULT_THRESHOLDS = (scoring.RESOLVED_THRESHOLD, scoring.NEAR_RESOLVED_THRESHOLD) == (1.0, 0.95)
THRESHOLD_SKIP_REASON = "the expected values assume ProgramBench's thresholds (1.0, 0.95)"

# A stand-in for an out-of-process CLI (ProgramBench): appends its argv (one list per call) to
# a JSON file and exits with a fixed code.
RECORDER = """import json, sys
from pathlib import Path
out = Path({out!r})
calls = json.loads(out.read_text()) if out.exists() else []
calls.append(sys.argv[1:])
out.write_text(json.dumps(calls))
sys.exit({code})
"""


def ev(passed: int, total: int, error_code: Optional[str] = None, branch: Optional[str] = None) -> dict[str, Any]:
    """Eval data with ``total`` results named t0..t<total-1>, the first ``passed`` passing."""
    results = []
    for i in range(total):
        result: dict[str, Any] = {"name": f"t{i}", "status": "passed" if i < passed else "failed"}
        if branch is not None:
            result["branch"] = branch
        results.append(result)
    data: dict[str, Any] = {"test_results": results}
    if error_code is not None:
        data["error_code"] = error_code
    return data


def rounding_fixture() -> dict[str, dict[str, Any]]:
    """Three instances: resolved, exactly near-resolved, and split over two branches with an error."""
    c = ev(2, 3, branch="x")
    c["test_results"] += ev(0, 4, branch="y")["test_results"]
    c["error_code"] = "compile_failed"
    return {"a": ev(3, 3), "b": ev(19, 20), "c": c}


# EffectivenessReport.summary_table() for rounding_fixture(), line by line.
ROUNDING_SUMMARY = [
    "Harness Effectiveness (ProgramBench)",
    "  instances        : 3",
    "  resolved         : 1 (33.3%)",
    "  near-resolved    : 2 (66.7%)",
    "  mean pass frac.  : 74.5%",
    "  branch-macro     : 76.1%",
    "  executions       : 30 over 27 unique tests",
    "  errors           : compile_failed=1",
    "",
    "  instance                                            pass  resolved  error",
    "  a                                                100.0%       yes  ",
    "  b                                                 95.0%      near  ",
    "  c                                                 28.6%        no  compile_failed",
]
