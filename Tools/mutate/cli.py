"""``python -m Tools.mutate``: measure how many mutants of each module the tests kill.

Exit codes: 0 when the run finished (and, with ``--check-baseline``, no score dropped);
1 when ``--check-baseline`` found a score below its baseline, or a module with a baseline
that couldn't be scored; 2 on a usage error, an update from a tree with changes, or when
the harness could not measure, such as a coverage run that failed; 130 when interrupted.
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
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import report, runner
from .operators import Mutant, apply_mutant, generate_mutants
from .runner import CopyPool, CoverageMap, CoverageMapper, HarnessError, copy_project, run_tests, tail, write_mutant

DEFAULT_BASELINE = "quality/mutation-baseline.json"
DEFAULT_ALLOWLIST = "quality/mutation-allowlist.json"
# Per-mutant reports are large and change on every run, so they stay out of Git (.gitignore).
DEFAULT_REPORT_DIR = "quality/reports"


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
    parser.add_argument(
        "--update-baseline", action="store_true", help="record scores that held or rose; refused from a tree with changes"
    )
    parser.add_argument(
        "--lower-baseline",
        action="append",
        default=[],
        metavar="MODULE",
        help="allow this module's recorded score to go down (repeatable); needs --reason; implies --update-baseline",
    )
    parser.add_argument("--reason", default="", help="why the --lower-baseline modules' scores may go down")
    parser.add_argument("--workdir", type=Path, default=None, help="where temporary copies go (default: system temp)")
    parser.add_argument("--show-survivors", type=int, default=20, help="survivors printed per module (all are in the report)")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    try:
        args.pytest_args = shlex.split(args.pytest_args)
    except ValueError as error:
        parser.error(f"--pytest-args: {error}")
    if args.lower_baseline and not args.reason.strip():
        parser.error("--lower-baseline needs --reason, which is recorded in the baseline file")
    args.lower = {module: args.reason.strip() for module in args.lower_baseline}
    if args.lower:
        args.update_baseline = True
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
        not_code: Sequence[str] = (),
    ) -> None:
        self.args = args
        self.root = root
        self.modules = modules
        self.allowlist = allowlist
        self.on_report = on_report
        self.not_code = not_code
        self.trees: dict[str, str] = {}
        self.print_lock = threading.Lock()
        self.commit: str | None = None
        self.dirty = False
        # Each distinct test selection's unmutated run, shared by every mutant that selects it.
        self.selections: dict[tuple[str, ...], runner.RunResult] = {}

    def run(self, state: Path) -> list[dict[str, Any]]:
        self.commit, self.dirty, self.trees = runner.provenance(self.root, self.not_code)
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
        mapper = CoverageMapper(state, copy, self.modules)
        mapping = mapper.build(pytest_args=self.args.pytest_args, timeout=self.args.coverage_timeout)
        for module, count in sorted(mapper.copies_credited.items()):
            self._say(f"coverage run: {module}: credited {count} copies that tests ran from other paths")
        if mapping.failed:
            self._say(f"warning: {len(mapping.failed)} tests failed in the coverage run and are not used:")
            for test in sorted(mapping.failed):
                self._say(f"  {test}")
        return mapping

    def _measure(
        self, module: str, source: str, mutants: list[Mutant], mapping: CoverageMap, copies: CopyPool, state: Path
    ) -> dict[str, Any]:
        excluded, stale = report.match_allowlist(module, mutants, self.allowlist)
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
        unreliable = self._check_selections(module, [tests for _, _, tests in pending], mapping, copies, state)
        reliable = []
        for index, mutant, tests in pending:
            if tuple(tests) in unreliable:
                outcomes[index] = report.Outcome(mutant, report.UNRELIABLE_SELECTION, tests=len(tests))
            else:
                reliable.append((index, mutant, tests))
        pending = reliable
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

        self._parallel(work, pending)
        finished = [outcome for outcome in outcomes if outcome is not None]
        return report.module_report(
            module,
            source,
            finished,
            stale,
            commit=self.commit,
            dirty=self.dirty,
            trees=self.trees,
            excluded_tests=sorted(mapping.failed),
            copies=mapping.copies.get(module),
            unreliable_selections=[
                {"tests": list(tests), "returncode": self.selections[tests].returncode} for tests in sorted(unreliable)
            ],
        )

    def _parallel(self, work: Callable[[Any], None], items: list[Any]) -> None:
        pool = ThreadPoolExecutor(max_workers=self.args.jobs)
        try:
            list(pool.map(work, items))
        except BaseException:
            runner.kill_all()  # an interrupt, or a failed run: stop the others now rather than wait for them
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown()

    @staticmethod
    def _lines(mutant: Mutant) -> set[int]:
        return {*range(mutant.line, mutant.end_line + 1), mutant.statement_line}

    def _timeout(self, tests: list[str], mapping: CoverageMap) -> float:
        baseline = sum(mapping.durations.get(test, 0.0) for test in tests)
        return round(max(self.args.min_timeout, self.args.timeout_factor * baseline), 1)

    def _check_selections(
        self, module: str, selections: list[list[str]], mapping: CoverageMap, copies: CopyPool, state: Path
    ) -> set[tuple[str, ...]]:
        """Run each distinct test selection once without a mutant, exactly as its mutants will run.

        Same tests, same order, same ``-x`` and options, alone in a copy. A selection that
        doesn't pass that way (a test that needs another test to run first, or a node id that
        collects nothing alone) would "kill" every mutant it judges, so its mutants are marked
        ``unreliable_selection`` instead. Results are kept for the whole run. Returns the
        selections that don't pass.
        """
        distinct = list(dict.fromkeys(tuple(tests) for tests in selections))
        new = [tests for tests in distinct if tests not in self.selections]

        def check(tests: tuple[str, ...]) -> None:
            copy = copies.get()
            try:
                log = state / f"{copy.name}.unmutated.log"
                result = run_tests(
                    copy, list(tests), timeout=self._timeout(list(tests), mapping), log=log, pytest_args=self.args.pytest_args
                )
            finally:
                copies.put(copy)
            if result.returncode != 0:
                result.output = tail(log, 30)
            self.selections[tests] = result

        self._parallel(check, new)
        failing = {tests for tests in distinct if self.selections[tests].returncode != 0}
        if distinct and len(failing) == len(distinct):
            sample = self.selections[next(iter(failing))]
            raise HarnessError(
                f"{module}: none of its test selections passes without a mutant; "
                f"the first failed with exit {sample.returncode}:\n{sample.output}"
            )
        if failing:
            noun = "selection fails" if len(failing) == 1 else "selections fail"
            self._say(
                f"warning: {module}: {len(failing)} test {noun} without any mutant; "
                "their mutants are unreliable_selection (listed in the report)"
            )
        return failing

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
        unmutated_passes = None
        try:
            write_mutant(copy, module, mutated)
            log = state / f"{copy.name}.log"
            result = run_tests(copy, tests, timeout=timeout, log=log, pytest_args=self.args.pytest_args)
            write_mutant(copy, module, source)
            if result.timed_out or result.returncode != 0:
                # Trust the kill only if the same tests pass without the mutant right now.
                again = run_tests(copy, tests, timeout=timeout, log=log, pytest_args=self.args.pytest_args)
                unmutated_passes = not again.timed_out and again.returncode == 0
        finally:
            write_mutant(copy, module, source)
            copies.put(copy)
        status = report.outcome_status(result.returncode, result.timed_out, unmutated_passes)
        return report.Outcome(mutant, status, tests=len(tests), duration=result.duration, timeout=timeout)

    def _say(self, message: str) -> None:
        with self.print_lock:
            print(message, file=sys.stderr, flush=True)


SURVIVOR_FLAGS = {
    report.NO_COVERAGE: " [no test executes this line]",
    report.UNRELIABLE_SELECTION: " [its tests fail without the mutant when run alone]",
    report.UNRELIABLE: " [flaky: its tests failed again without the mutant]",
}


def print_results(reports: list[dict[str, Any]], show: int, out: Callable[[str], None] = print) -> None:
    out(report.render_table(reports))
    for module_report in reports:
        survivors = module_report["survivors"]
        if survivors:
            out(f"\nsurvivors in {module_report['module']} ({len(survivors)}):")
            for survivor in survivors[:show]:
                flag = SURVIVOR_FLAGS.get(survivor["status"], "")
                out(f"{survivor['location']} {survivor['operator']}{flag}")
                out(survivor["diff"].rstrip("\n"))
            if len(survivors) > show:
                out(f"... {len(survivors) - show} more in the report")
        for entry in module_report["stale_allowlist"]:
            out(
                f"stale allowlist entry: {entry['module']}:{entry['line']} {entry['operator']} "
                f"{entry['original']!r} -> {entry['mutant']!r} matches no mutant"
            )


def _refuse_agent_entries(path: Path, entries: list[Any], select: Callable[[Any], Any], verb: str) -> None:
    """Refuse entries a person didn't commit: an allowlist entry, or a lowered baseline score.

    Each one weakens the measurement, so it counts only when the commit that first holds it
    is authored by someone other than the agent (or any bot), and so reaches main through a
    pull request a person approves.
    """
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries:
        return
    for entry, author in zip(entries, runner.entry_authors(path, entries, select)):
        where = f"{path.name}: {entry.get('module')}:{entry.get('line', '')}".rstrip(":")
        if author is None:
            raise HarnessError(f"{where} is not committed; an entry counts only once a person commits it")
        if report.is_agent(*author):
            raise HarnessError(f"{where} was {verb} by {author[0]}; only a person's commit can do that")


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    try:
        baseline_path = _resolve(root, args.baseline)
        baseline = _read_json(baseline_path, {})
        allowlist_path = _resolve(root, args.allowlist)
        allowlist = report.validate_allowlist(_read_json(allowlist_path, {"entries": []}))
        _refuse_agent_entries(allowlist_path, allowlist, lambda document: document.get("entries"), "added")
        if args.check_baseline or args.update_baseline:
            lowered = baseline.get("lowered", []) if isinstance(baseline, dict) else []
            _refuse_agent_entries(baseline_path, lowered, lambda document: document.get("lowered"), "lowered")
        modules = _modules(args, root, baseline)
        unmeasured = sorted(set(args.lower) - set(modules))
        if unmeasured:
            raise HarnessError(f"--lower-baseline names a module this run doesn't measure: {', '.join(unmeasured)}")
        report_dir = _resolve(root, args.report_dir)
        # The harness's own output doesn't make the tree dirty; everything else, the allowlist included, does.
        not_code = [
            path.resolve().relative_to(root).as_posix()
            for path in (report_dir, baseline_path)
            if path.resolve().is_relative_to(root)
        ]
        if args.update_baseline and runner.provenance(root, not_code)[1]:
            raise HarnessError(
                "refusing to update the baseline from a working tree with changes; commit them first, "
                "so the recorded commit and trees name the code measured"
            )
        workdir = args.workdir.resolve() if args.workdir else None
        state = Path(tempfile.mkdtemp(prefix="mutate-", dir=workdir)).resolve()
    except (HarnessError, report.AllowlistError, ValueError, OSError) as error:
        print(f"mutate: {error}", file=sys.stderr)
        return 2
    if args.nice:
        os.nice(10)

    def write_report(module_report: dict[str, Any]) -> None:
        # Each module's report lands as soon as it is measured, so a cancelled run keeps them.
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"mutation-report-{report.report_name(module_report['module'])}.json"
        path.write_text(json.dumps(module_report, indent=2) + "\n", encoding="utf-8")

    runner.reset()
    signal.signal(signal.SIGTERM, _interrupt)  # a cancelled CI job: clean up as for Ctrl-C
    try:
        reports = Measurement(args, root, modules, allowlist, write_report, not_code).run(state)
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
    # The check comes first, against the baseline as it was, and a run that fails it records nothing.
    if args.check_baseline:
        checked = {**baseline, "modules": {k: v for k, v in baseline.get("modules", {}).items() if k not in args.lower}}
        failures = report.baseline_failures(reports, checked)
        for failure in failures:
            print(failure)
        if failures:
            if args.update_baseline:
                print("baseline not updated: the check failed", file=sys.stderr)
            return 1
    if args.update_baseline:
        updated = report.updated_baseline(baseline, reports, args.lower)
        for module_report in reports:
            kept = updated["modules"].get(module_report["module"], {}).get("score")
            if kept != module_report["score"]:
                print(
                    f"baseline kept for {module_report['module']}: {kept} (this run {module_report['score']}); "
                    "lower it only with --lower-baseline and --reason",
                    file=sys.stderr,
                )
        baseline_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    return 0
