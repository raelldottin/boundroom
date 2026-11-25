"""The imperative shell: disposable copies, the coverage map, and running tests against a mutant.

Nothing here writes inside the project being measured. Every copy, the throwaway virtual
environment that measures subprocesses, and every log live under one temporary directory
that is removed at the end.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Environment variables from an outer coverage run that must not leak into ours.
SCRUBBED_PREFIXES = ("COVERAGE_", "COV_CORE_")

# Names never copied into a disposable copy of a project that isn't a Git checkout.
COPY_IGNORE = (".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".ty_cache", ".mypy_cache", ".git")

# coverage reads `include` as comma- or newline-separated fnmatch patterns.
_PATTERN_CHARACTERS = re.compile(r"[,\[\]*?\n]")

_BOOT = '''\
"""Started by a .pth file in the mutation harness's throwaway venv: measure this process."""
import atexit
import hashlib
import json
import os
import site
import sys
import uuid

SITE_DIRS = {site_dirs!r}
for _directory in SITE_DIRS:
    site.addsitedir(_directory)


_COVERAGE = None


def _record_copies():
    # Tests copy modules to temporary directories and run them there. Coverage records those
    # copies under their own paths, and the directories are gone by the time the map is read,
    # so note each copy's content now, while it still exists. The names come from what coverage
    # measured in this process: a script run as __main__ may have lost its __file__ by now.
    names = set()
    if _COVERAGE is not None:
        try:
            names.update(_COVERAGE.get_data().measured_files())
        except Exception:
            pass
    for module in list(sys.modules.values()):
        name = getattr(module, "__file__", None)
        if isinstance(name, str):
            names.add(name)
    found = {{}}
    for name in names:
        real = os.path.realpath(name)
        if real.endswith({targets!r}):
            try:
                with open(real, "rb") as handle:
                    found[real] = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                pass
    if found:
        path = os.path.join({hash_dir!r}, "%d-%s.json" % (os.getpid(), uuid.uuid4().hex))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(found, handle)


def _start():
    try:
        import coverage
    except ImportError:
        return
    try:
        with open({context_file!r}, encoding="utf-8") as handle:
            context = handle.read().strip()
    except OSError:
        context = ""
    cov = coverage.Coverage(config_file={config_file!r}, data_suffix=True, context=context or None)
    # As coverage.process_startup does: save at exit and on SIGTERM, and stay quiet.
    cov._auto_save = True
    cov._warn_no_data = False
    cov._warn_unimported_source = False
    cov._warn_preimported_source = False
    cov.start()
    global _COVERAGE
    _COVERAGE = cov
    # Registered after coverage's own exit handler, so it runs first, while the data is live.
    atexit.register(_record_copies)


_start()
'''

_PLUGIN = '''\
"""pytest plugin for the coverage run: one coverage context per test, shared with subprocesses."""
import json

import pytest

_durations = {{}}
_failed = set()


def _switch(context):
    # Subprocesses read the file when they start; this process switches in place.
    with open({context_file!r}, "w", encoding="utf-8") as handle:
        handle.write(context)
    try:
        import coverage

        current = coverage.Coverage.current()
        if current is not None:
            current.switch_context(context)
    except Exception:
        pass


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _switch(item.nodeid)
    try:
        return (yield)
    finally:
        _switch("")


def pytest_runtest_logreport(report):
    _durations[report.nodeid] = _durations.get(report.nodeid, 0.0) + report.duration
    if report.failed:
        _failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    with open({results_file!r}, "w", encoding="utf-8") as handle:
        json.dump({{"durations": _durations, "failed": sorted(_failed), "exitstatus": int(exitstatus)}}, handle)
'''


class HarnessError(RuntimeError):
    """The harness could not measure: a copy, the coverage run or the unmutated tests failed."""


# Process groups still running, so an interrupted harness can kill them all.
_active: set[int] = set()
_active_lock = threading.Lock()
_stopping = threading.Event()
_SIGNALS = {signal.SIGINT, signal.SIGTERM}


def reset() -> None:
    """Allow runs again after ``kill_all``, for a caller that runs the harness twice in one process."""
    _stopping.clear()


def kill_all() -> None:
    """Kill every running test process group, and refuse to start more."""
    _stopping.set()
    with _active_lock:
        for pgid in list(_active):
            _kill_group(pgid)


@dataclass
class RunResult:
    returncode: int | None
    duration: float
    timed_out: bool
    log: Path
    output: str = ""  # the log's tail, kept when the log itself won't outlive the run


@dataclass
class CoverageMap:
    """For each module, the test node ids that execute each line, plus each test's duration."""

    lines: dict[str, dict[int, set[str]]] = field(default_factory=dict)
    # Per module: copies run from other paths that were credited, and same-named files that weren't.
    copies: dict[str, dict[str, Any]] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    _module_tests: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def module_tests(self, module: str) -> frozenset[str]:
        """Every test that executes any line of ``module``."""
        if module not in self._module_tests:
            tests: set[str] = set()
            for contexts in self.lines.get(module, {}).values():
                tests |= contexts
            tests.discard("")
            self._module_tests[module] = frozenset(tests - self.failed)
        return self._module_tests[module]

    def tests_for(self, module: str, lines: Iterable[int], *, import_time: bool = False) -> set[str]:
        """The tests that execute any of ``lines``.

        Code that runs when the module is imported (module level, class bodies, default
        values, decorators) runs once per process, under whichever test imported it first,
        or under no test during collection. Any test that uses the module may depend on it,
        so it selects every test that covers the module, provided some test runs one of its
        lines at all.
        """
        by_line = self.lines.get(module, {})
        contexts: set[str] = set()
        for line in lines:
            contexts |= by_line.get(line, set())
        if contexts and (import_time or "" in contexts):
            contexts |= self.module_tests(module)
        contexts.discard("")
        return contexts - self.failed


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(SCRUBBED_PREFIXES)}
    env.update(extra or {})
    return env


def run_group(command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float, log: Path) -> RunResult:
    """Run ``command`` in a new process group, with output to ``log``.

    On a timeout, or when the command ends and leaves children behind, the whole group is
    killed. Output goes to a file, not a pipe, so a leftover child holding the pipe open
    can't keep the wait going until the timeout. The leader is reaped only after the group
    is killed, so its process id, which names the group, can't be reused in between.
    """
    start = time.monotonic()
    # Block Ctrl-C and SIGTERM between starting the group and recording it, so an interrupt
    # can't leave a group running that kill_all doesn't know about.
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, _SIGNALS)
    try:
        with log.open("wb") as output, _active_lock:
            if _stopping.is_set():
                raise HarnessError("interrupted")
            process = subprocess.Popen(
                list(command), cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
            )
            _active.add(process.pid)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    timed_out = False
    try:
        timed_out = not _wait_for_exit(process, timeout)
    finally:
        _kill_group(process.pid)
        process.wait()
        with _active_lock:
            _active.discard(process.pid)
    return RunResult(None if timed_out else process.returncode, time.monotonic() - start, timed_out, log)


def _wait_for_exit(process: subprocess.Popen[bytes], timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for the leader to exit. True if it exited.

    Where ``os.waitid`` exists (Linux, and macOS from Python 3.13) the leader is left
    unreaped, so its id, which names the group, can't be reused before the group is killed.
    Elsewhere it is reaped here; the id still can't name another group while any member of
    this one lives, which is the only case the kill matters for.
    """
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True
    flags = os.WEXITED | os.WNOHANG | getattr(os, "WNOWAIT", 0)
    deadline = time.monotonic() + timeout
    delay = 0.005
    while True:
        try:
            if waitid(getattr(os, "P_PID"), process.pid, flags) is not None:
                return True
        except ChildProcessError:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.1)


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def is_git_checkout(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--show-toplevel").strip() == str(root.resolve())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def provenance(root: Path, exclude: Sequence[str] = ()) -> tuple[str | None, bool, dict[str, str]]:
    """What a measurement measured: HEAD, whether the working tree differs, and the code's trees.

    ``trees`` maps each top-level directory that holds Python to its Git tree id at HEAD. A
    squash merge makes a new commit but keeps unchanged trees, so the ids still name the code
    measured once the branch commit is gone. ``exclude`` lists paths, relative to ``root``,
    whose changes don't count as dirty: the harness's own reports and baseline.
    """
    if not is_git_checkout(root):
        return None, False, {}
    commit = _git(root, "rev-parse", "HEAD").strip()
    pathspec = ["--", ".", *(f":(exclude){path}" for path in exclude)] if exclude else []
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=normal", *pathspec).strip())
    python_dirs = {
        name.split("/", 1)[0]
        for name in _git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        if "/" in name and name.endswith(".py")
    }
    trees = {}
    for line in _git(root, "ls-tree", "HEAD").splitlines():
        meta, name = line.split("\t", 1)
        _mode, kind, object_id = meta.split()
        if kind == "tree" and name in python_dirs:
            trees[name] = object_id
    return commit, dirty, trees


def entry_authors(path: Path, entries: list[dict[str, Any]], select: Callable[[Any], Any]) -> list[tuple[str, str] | None]:
    """For each entry, the (name, email) of the author of the first commit whose ``path`` holds it.

    ``select`` picks the entries out of a parsed version of the file. An entry no commit holds
    exactly, such as one added or edited in the working tree, gets None.
    """
    directory = path.resolve().parent
    try:
        top = Path(_git(directory, "rev-parse", "--show-toplevel").strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError) as error:
        raise HarnessError(f"{path} isn't in a Git repository, so who added its entries can't be checked") from error
    relative = path.resolve().relative_to(top).as_posix()
    wanted: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        wanted.setdefault(json.dumps(entry, sort_keys=True), []).append(index)
    authors: list[tuple[str, str] | None] = [None] * len(entries)
    for line in _git(top, "log", "--reverse", "--format=%H%x09%an%x09%ae", "--", relative).splitlines():
        sha, name, email = line.split("\t", 2)
        try:
            version = json.loads(_git(top, "show", f"{sha}:{relative}"))
        except (subprocess.CalledProcessError, ValueError):
            continue  # deleted, or not JSON, at that commit
        held = {json.dumps(item, sort_keys=True) for item in (select(version) or []) if isinstance(item, dict)}
        for key, indexes in wanted.items():
            if key in held:
                for index in indexes:
                    if authors[index] is None:
                        authors[index] = (name, email)
    return authors


def copy_project(root: Path, destination: Path) -> None:
    """A disposable copy of ``root``'s working tree at ``destination``.

    A Git checkout becomes a clone sharing the original's objects, with HEAD at the same
    commit and the working tree's tracked and untracked (not ignored) files copied over, so
    tests that call Git still work. The clone's index is at HEAD, so a change staged in the
    original shows there as unstaged. Nothing is written to the original's ``.git``.
    """
    try:
        if not is_git_checkout(root):
            shutil.copytree(root, destination, ignore=shutil.ignore_patterns(*COPY_IGNORE), symlinks=True)
            return
        listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        _git(root, "clone", "--quiet", "--shared", "--no-checkout", str(root), str(destination))
        _git(destination, "reset", "--quiet", _git(root, "rev-parse", "HEAD").strip())
    except (OSError, subprocess.CalledProcessError) as error:
        raise HarnessError(f"could not copy {root} to {destination}: {error}") from error
    for name in dict.fromkeys(name for name in listed if name):
        source = root / name
        if not source.exists() and not source.is_symlink():
            continue  # deleted in the working tree
        if source.is_dir() and not source.is_symlink():
            continue  # a submodule or nested checkout; not copied
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


class CopyPool:
    """Up to ``size`` disposable copies, made when first needed, each held by one run at a time."""

    def __init__(self, root: Path, state: Path, size: int, sources: dict[str, str]) -> None:
        self.root = root
        self.state = state
        self.size = size
        self.sources = sources
        self.free: queue.Queue[Path] = queue.Queue()
        self.made = 0
        self.lock = threading.Lock()

    def get(self) -> Path:
        try:
            return self.free.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self.made < self.size:
                self.made += 1
                copy = self.state / f"copy-{self.made}"
                copy_project(self.root, copy)
                # Every copy holds the module text the coverage run measured, whatever the
                # working tree holds now.
                for module, text in self.sources.items():
                    write_mutant(copy, module, text)
                return copy
        return self.free.get()

    def put(self, copy: Path) -> None:
        self.free.put(copy)


class CoverageMapper:
    """Runs the whole suite once in a copy, measuring the target modules per test.

    Subprocesses are measured too: the suite runs under a throwaway virtual environment
    whose ``.pth`` file starts coverage in every Python process it launches, with the
    context of the test running at the time.
    """

    def __init__(self, state: Path, copy: Path, modules: list[str]) -> None:
        self.state = state
        self.copy = copy.resolve()
        self.modules = modules
        self.venv = state / "venv"
        self.data_dir = state / "coverage"
        self.context_file = state / "context"
        self.results_file = state / "results.json"
        self.config_file = state / "coveragerc"
        self.hash_dir = state / "copies"
        self.copies_credited: dict[str, int] = {}
        self.copies_ignored: dict[str, list[dict[str, str]]] = {}

    def build(self, *, pytest_args: Sequence[str], timeout: float) -> CoverageMap:
        if _PATTERN_CHARACTERS.search(str(self.copy)):
            raise HarnessError(f"the work directory's path holds a character coverage reads as a pattern: {self.copy}")
        python = self._make_venv()
        log = self.state / "coverage-run.log"
        env = clean_env({"PATH": f"{self.venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"})
        env.pop("VIRTUAL_ENV", None)
        command = [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "_mutate_plugin", *pytest_args]
        result = run_group(command, cwd=self.copy, env=env, timeout=timeout, log=log)
        if result.timed_out:
            raise HarnessError(f"the coverage run took longer than {timeout:.0f}s:\n{tail(log)}")
        if result.returncode not in (0, 1) or not self.results_file.exists():
            raise HarnessError(f"the coverage run failed with exit {result.returncode}:\n{tail(log)}")
        results = json.loads(self.results_file.read_text(encoding="utf-8"))
        mapping = CoverageMap(durations=dict(results["durations"]), failed=set(results["failed"]))
        mapping.lines = self._read_contexts()
        for module in self.modules:
            mapping.copies[module] = {
                "credited": self.copies_credited.get(module, 0),
                "ignored": self.copies_ignored.get(module, []),
            }
        return mapping

    def _make_venv(self) -> Path:
        try:
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.venv)], check=True, capture_output=True)
            python = self.venv / "bin" / "python"
            # Not `-S`: without the site module, sys.prefix is the base Python's, not the venv's.
            purelib = subprocess.run(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise HarnessError(f"could not make the throwaway venv for the coverage run: {error}") from error
        site_packages = Path(purelib).resolve()
        if not site_packages.is_relative_to(self.venv.resolve()):
            # The boot file must only ever land in the throwaway venv, never in a shared install.
            raise HarnessError(f"the throwaway venv's site-packages is outside it: {site_packages}")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.hash_dir.mkdir(parents=True, exist_ok=True)
        # The copy's own module, and any copy of it a test makes elsewhere (credited by content below).
        include = "\n".join(f"    {pattern}" for module in self.modules for pattern in (self.copy / module, f"*/{module}"))
        self.config_file.write_text(
            "[run]\n"
            f"data_file = {self.data_dir / '.coverage'}\n"
            "sigterm = true\n"
            # switch_context needs a tracer core; sys.monitoring, the default on 3.14, has none.
            "core = ctrace\n"
            "disable_warnings = no-data-collected, module-not-measured, module-not-imported, include-ignored, no-ctracer\n"
            f"include =\n{include}\n",
            encoding="utf-8",
        )
        (site_packages / "_mutate_boot.py").write_text(
            _BOOT.format(
                site_dirs=_real_site_dirs(),
                context_file=str(self.context_file),
                config_file=str(self.config_file),
                targets=tuple(f"/{module}" for module in self.modules),
                hash_dir=str(self.hash_dir),
            ),
            encoding="utf-8",
        )
        (site_packages / "_mutate_plugin.py").write_text(
            _PLUGIN.format(context_file=str(self.context_file), results_file=str(self.results_file)), encoding="utf-8"
        )
        (site_packages / "00_mutate_boot.pth").write_text("import _mutate_boot\n", encoding="utf-8")
        return python

    def _read_contexts(self) -> dict[str, dict[int, set[str]]]:
        import coverage
        from coverage.exceptions import NoDataError

        combined = coverage.Coverage(data_file=str(self.data_dir / ".coverage"), config_file=False)
        try:
            combined.combine([str(self.data_dir)])
        except NoDataError:
            return {}
        data = combined.get_data()
        copies: dict[str, str] = {}
        for record in self.hash_dir.glob("*.json"):
            try:
                copies.update(json.loads(record.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        lines: dict[str, dict[int, set[str]]] = {}
        for module in self.modules:
            target = (self.copy / module).resolve()
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            merged: dict[int, set[str]] = {}
            for name in data.measured_files():
                real = os.path.realpath(name)
                credited = Path(real) == target
                if not credited and real.endswith(f"/{module}"):
                    # A copy elsewhere counts only if its content is the module's, byte for byte.
                    recorded = copies.get(real)
                    credited = recorded == digest
                    if credited:
                        self.copies_credited[module] = self.copies_credited.get(module, 0) + 1
                    else:
                        reason = "no content hash recorded" if recorded is None else "content differs"
                        self.copies_ignored.setdefault(module, []).append({"path": real, "reason": reason})
                if credited:
                    for line, contexts in data.contexts_by_lineno(name).items():
                        merged.setdefault(line, set()).update(contexts)
            if merged:
                lines[module] = merged
        return lines


def _real_site_dirs() -> list[str]:
    """The site-packages directories of the Python running the harness.

    When the harness itself runs inside another harness's throwaway venv (its own tests do,
    during a coverage run of the repository), that venv's directories would start the outer
    coverage again, so the directories the outer boot file added are used instead.
    """
    boot = sys.modules.get("_mutate_boot")
    if boot is not None and isinstance(getattr(boot, "SITE_DIRS", None), list):
        return list(boot.SITE_DIRS)
    return list(dict.fromkeys([*site.getsitepackages(), sysconfig.get_paths()["purelib"]]))


def tail(log: Path, lines: int = 40) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def write_mutant(copy: Path, module: str, text: str) -> None:
    """Put ``text`` at ``module`` in ``copy`` and drop its bytecode, so no stale .pyc is used."""
    path = copy / module
    path.write_text(text, encoding="utf-8")
    for cached in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
        cached.unlink(missing_ok=True)


def run_tests(copy: Path, tests: Sequence[str], *, timeout: float, log: Path, pytest_args: Sequence[str] = ()) -> RunResult:
    command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *pytest_args, *tests]
    # `python3` on PATH is the harness's own interpreter, as `uv run` arranges and as the
    # coverage run has it, so a test that runs a tool through PATH sees the same packages.
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    env = clean_env({"PYTHONDONTWRITEBYTECODE": "1", "PATH": path})
    return run_group(command, cwd=copy, env=env, timeout=timeout, log=log)
