"""``python -m Tools.mutate``: measure how many mutants of each module the tests kill.

Exit codes: 0 when the run finished (and, with ``--check-baseline``, no score dropped);
1 when ``--check-baseline`` found a score below its baseline, or a module with a baseline
that couldn't be scored; 2 on a usage error or when the harness could not measure, such as
a coverage run that failed; 130 when interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import report, runner
from .operators import Mutant, apply_mutant, generate_mutants
from .runner import CopyPool, CoverageMap, CoverageMapper, HarnessError, copy_project, run_tests, tail, write_mutant

DEFAULT_BASELINE = "quality/mutation-baseline.json"
DEFAULT_ALLOWLIST = "quality/mutation-allowlist.json"
DEFAULT_REPORT_DIR = "quality"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m Tools.mutate", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("modules", nargs="*", help="module paths relative to --root; commas also separate them")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the project to measure (default: here)")
    parser.add_argument("--baseline-modules", action="store_true", help="measure every module in the baseline file")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2), help="parallel test runs")
    parser.add_argument("--nice", action="store_true", help="run at lowered CPU priority")
    parser.add_argument("--min-timeout", type=float, default=30.0, help="the shortest timeout for one mutant (s)")
    parser.add_argument("--timeout-factor", type=float, default=3.0, help="timeout = factor x the tests' baseline time")
    parser.add_argument("--coverage-timeout", type=float, default=3600.0, help="limit for the coverage run (s)")
    parser.add_argument("--pytest-args", default="", help="extra pytest arguments for every run, shell-quoted")
    parser.add_argument("--report-dir", type=Path, default=Path(DEFAULT_REPORT_DIR))
    parser.add_argument("--allowlist", type=Path, default=Path(DEFAULT_ALLOWLIST))
    parser.add_argument("--baseline", type=Path, default=Path(DEFAULT_BASELINE))
    parser.add_argument("--check-baseline", action="store_true", help="exit 1 if a score is below its baseline")
    parser.add_argument("--update-baseline", action="store_true", help="write this run's scores into the baseline")
    parser.add_argument("--workdir", type=Path, default=None, help="where temporary copies go (default: system temp)")
    parser.add_argument("--show-survivors", type=int, default=20, help="survivors printed per module (all are in the report)")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    try:
        args.pytest_args = shlex.split(args.pytest_args)
    except ValueError as error:
        parser.error(f"--pytest-args: {error}")
    return args


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _modules(args: argparse.Namespace, root: Path, baseline: dict[str, Any]) -> list[str]:
    names = [name.strip() for value in args.modules for name in value.split(",") if name.strip()]
    if args.baseline_modules:
        names += list(baseline.get("modules", {}))
    modules = []
    for name in dict.fromkeys(names):
        path = (root / name).resolve()
        if not path.is_file() or path.suffix != ".py" or not path.is_relative_to(root):
            raise HarnessError(f"not a Python file under {root}: {name}")
        modules.append(path.relative_to(root).as_posix())
    if not modules:
        raise HarnessError("name at least one module, or pass --baseline-modules with a baseline that lists modules")
    return modules


def order_tests(module: str, tests: set[str] | frozenset[str], durations: dict[str, float]) -> list[str]:
    """The module's own test files first, then fastest first: with ``-x``, a kill comes sooner."""
    stem = Path(module).stem
    own = (f"test_{stem}", f"test_{stem}_")

    def key(test: str) -> tuple[bool, float, str]:
        test_file = Path(test.split("::", 1)[0]).stem
        return (not (test_file == own[0] or test_file.startswith(own[1])), durations.get(test, 0.0), test)

    return sorted(tests, key=key)


class Measurement:
    """One harness run over a set of modules."""

    def __init__(
        self,
        args: argparse.Namespace,
        root: Path,
        modules: list[str],
        allowlist: list[dict[str, Any]],
        on_report: Callable[[dict[str, Any]], None],
    ) -> None:
        self.args = args
        self.root = root
        self.modules = modules
        self.allowlist = allowlist
        self.on_report = on_report
        self.print_lock = threading.Lock()
        self.commit: str | None = None
        self.dirty = False

    def run(self, state: Path) -> list[dict[str, Any]]:
        self.commit, self.dirty = runner.provenance(self.root)
        copy = state / "coverage-copy"
        copy_project(self.root, copy)
        # The text measured is the coverage copy's, read once, so edits made to the working tree
        # during a long run don't mix into it.
        sources = {module: (copy / module).read_text(encoding="utf-8") for module in self.modules}
        mutants = {module: self._generate(module, text) for module, text in sources.items()}
        mapping = self._coverage_map(state, copy)
        shutil.rmtree(copy, ignore_errors=True)
        copies = CopyPool(self.root, state, self.args.jobs, sources)
        reports = []
        for module in self.modules:
            module_report = self._measure(module, sources[module], mutants[module], mapping, copies, state)
            self.on_report(module_report)
            reports.append(module_report)
        return reports

    @staticmethod
    def _generate(module: str, text: str) -> list[Mutant]:
        try:
            return generate_mutants(text)
        except SyntaxError as error:
            raise HarnessError(f"{module} doesn't parse: {error}") from error

    def _coverage_map(self, state: Path, copy: Path) -> CoverageMap:
        self._say(f"coverage run: the whole suite once, measuring {', '.join(self.modules)}")
        mapping = CoverageMapper(state, copy, self.modules).build(
            pytest_args=self.args.pytest_args, timeout=self.args.coverage_timeout
        )
        if mapping.failed:
            self._say(f"warning: {len(mapping.failed)} tests failed in the coverage run and are not used:")
            for test in sorted(mapping.failed):
                self._say(f"  {test}")
        return mapping

    def _measure(
        self, module: str, source: str, mutants: list[Mutant], mapping: CoverageMap, copies: CopyPool, state: Path
    ) -> dict[str, Any]:
        excluded, stale = report.match_allowlist(module, mutants, self.allowlist)
        self._check_tests_pass(module, mapping, copies, state)
        outcomes: list[report.Outcome | None] = [None] * len(mutants)
        pending = []
        for index, mutant in enumerate(mutants):
            if index in excluded:
                outcomes[index] = report.Outcome(mutant, report.ALLOWLISTED)
                continue
            tests = mapping.tests_for(module, self._lines(mutant), import_time=mutant.import_time)
            if not tests:
                outcomes[index] = report.Outcome(mutant, report.NO_COVERAGE)
                continue
            pending.append((index, mutant, order_tests(module, tests, mapping.durations)))
        self._say(f"{module}: {len(mutants)} mutants, {len(pending)} to run with {self.args.jobs} jobs")
        done = [0]

        def work(item: tuple[int, Mutant, list[str]]) -> None:
            index, mutant, tests = item
            outcomes[index] = self._run_one(module, source, mutant, tests, mapping, copies, state)
            with self.print_lock:
                done[0] += 1
                step = max(1, len(pending) // 10)
                if done[0] % step == 0 or done[0] == len(pending):
                    print(f"  {module}: {done[0]}/{len(pending)}", file=sys.stderr, flush=True)

        pool = ThreadPoolExecutor(max_workers=self.args.jobs)
        try:
            list(pool.map(work, pending))
        except BaseException:
            runner.kill_all()  # an interrupt, or a failed run: stop the others now rather than wait for them
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown()
        finished = [outcome for outcome in outcomes if outcome is not None]
        return report.module_report(
            module, source, finished, stale, commit=self.commit, dirty=self.dirty, excluded_tests=sorted(mapping.failed)
        )

    @staticmethod
    def _lines(mutant: Mutant) -> set[int]:
        return {*range(mutant.line, mutant.end_line + 1), mutant.statement_line}

    def _timeout(self, tests: list[str], mapping: CoverageMap) -> float:
        baseline = sum(mapping.durations.get(test, 0.0) for test in tests)
        return round(max(self.args.min_timeout, self.args.timeout_factor * baseline), 1)

    def _check_tests_pass(self, module: str, mapping: CoverageMap, copies: CopyPool, state: Path) -> None:
        """The module's tests must pass, unmutated, in a copy, or every mutant would look killed.

        This also proves the node ids select, so a pytest usage error under a mutant is the
        mutant's doing.
        """
        tests = order_tests(module, mapping.module_tests(module), mapping.durations)
        if not tests:
            return
        copy = copies.get()
        log = state / f"{report.report_name(module)}.unmutated.log"
        try:
            result = run_tests(copy, tests, timeout=self._timeout(tests, mapping), log=log, pytest_args=self.args.pytest_args)
        finally:
            copies.put(copy)
        if result.returncode != 0:
            detail = "timed out" if result.timed_out else f"exit {result.returncode}"
            raise HarnessError(f"{module}: its tests fail without any mutant ({detail}):\n{tail(log, 30)}")

    def _run_one(
        self,
        module: str,
        source: str,
        mutant: Mutant,
        tests: list[str],
        mapping: CoverageMap,
        copies: CopyPool,
        state: Path,
    ) -> report.Outcome:
        mutated = apply_mutant(source, mutant)
        try:
            compile(mutated, module, "exec")
        except SyntaxError:
            return report.Outcome(mutant, report.ERROR)
        timeout = self._timeout(tests, mapping)
        copy = copies.get()
        try:
            write_mutant(copy, module, mutated)
            log = state / f"{copy.name}.log"
            result = run_tests(copy, tests, timeout=timeout, log=log, pytest_args=self.args.pytest_args)
        finally:
            write_mutant(copy, module, source)
            copies.put(copy)
        if result.timed_out:
            status = report.TIMEOUT
        elif result.returncode == 0:
            status = report.SURVIVED
        else:
            # Any failure is a kill, including pytest's usage error (4) or no tests (5): the
            # unmutated run proved these tests select and pass, so the mutant broke them, for
            # example by failing an import in conftest.py or changing a parametrized test's id.
            status = report.KILLED
        return report.Outcome(mutant, status, tests=len(tests), duration=result.duration, timeout=timeout)

    def _say(self, message: str) -> None:
        with self.print_lock:
            print(message, file=sys.stderr, flush=True)


def print_results(reports: list[dict[str, Any]], show: int, out: Callable[[str], None] = print) -> None:
    out(report.render_table(reports))
    for module_report in reports:
        survivors = module_report["survivors"]
        if survivors:
            out(f"\nsurvivors in {module_report['module']} ({len(survivors)}):")
            for survivor in survivors[:show]:
                flag = " [no test executes this line]" if survivor["status"] == report.NO_COVERAGE else ""
                out(f"{survivor['location']} {survivor['operator']}{flag}")
                out(survivor["diff"].rstrip("\n"))
            if len(survivors) > show:
                out(f"... {len(survivors) - show} more in the report")
        for entry in module_report["stale_allowlist"]:
            out(
                f"stale allowlist entry: {entry['module']}:{entry['line']} {entry['operator']} "
                f"{entry['original']!r} -> {entry['mutant']!r} matches no mutant"
            )


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    try:
        baseline_path = _resolve(root, args.baseline)
        baseline = _read_json(baseline_path, {})
        allowlist = report.validate_allowlist(_read_json(_resolve(root, args.allowlist), {"entries": []}))
        modules = _modules(args, root, baseline)
        workdir = args.workdir.resolve() if args.workdir else None
        state = Path(tempfile.mkdtemp(prefix="mutate-", dir=workdir)).resolve()
    except (HarnessError, report.AllowlistError, ValueError, OSError) as error:
        print(f"mutate: {error}", file=sys.stderr)
        return 2
    if args.nice:
        os.nice(10)
    report_dir = _resolve(root, args.report_dir)

    def write_report(module_report: dict[str, Any]) -> None:
        # Each module's report lands as soon as it is measured, so a cancelled run keeps them.
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"mutation-report-{report.report_name(module_report['module'])}.json"
        path.write_text(json.dumps(module_report, indent=2) + "\n", encoding="utf-8")

    runner.reset()
    signal.signal(signal.SIGTERM, _interrupt)  # a cancelled CI job: clean up as for Ctrl-C
    try:
        reports = Measurement(args, root, modules, allowlist, write_report).run(state)
    except HarnessError as error:
        print(f"mutate: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        runner.kill_all()
        print("mutate: interrupted; reports of modules already measured were written", file=sys.stderr)
        return 130
    except Exception:
        # Exit 1 means a score dropped; a crash is "could not measure".
        traceback.print_exc()
        return 2
    finally:
        shutil.rmtree(state, ignore_errors=True)
    print_results(reports, args.show_survivors)
    if args.update_baseline:
        baseline_path.write_text(json.dumps(report.updated_baseline(baseline, reports), indent=2) + "\n", encoding="utf-8")
    if args.check_baseline:
        failures = report.baseline_failures(reports, baseline)
        for failure in failures:
            print(failure)
        if failures:
            return 1
    return 0
