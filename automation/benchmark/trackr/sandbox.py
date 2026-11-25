"""Contain the checker: run a target's checks and hidden tests under ``/usr/bin/sandbox-exec`` (Track R v2).

Every command the oracle or the green-base check runs in a target clone is code the agent may have
written: a test, a conftest, a module the tests import. It runs under a Seatbelt profile with:

- **no network** at all;
- **writes only** to the session's clone, its private TMPDIR and its temporary HOME, plus the
  ``/dev`` nodes a process needs. Nothing else under ``/private/tmp`` or ``/var/folders`` is
  writable, because those are writable only through the session's own directories;
- **no reads** of ``~/.ssh``, ``~/.aws`` or ``~/.config/gh``;
- ``env -i`` with an allowlist: a fixed ``PATH``, the temporary ``HOME`` and ``TMPDIR``, the locale,
  and Git pointed at an empty config.

The model's own session is contained by Claude Code's sandbox, not this one: this one is for the
code that runs with no agent present, so a hostile test can't reach the network or the operator's
files while it is being scored.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .common import CommandRunner, Completed

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
ENV = "/usr/bin/env"

PROFILE = """(version 1)
(deny default)
; Running at all: processes, signals within the sandbox, system information and the services a
; dynamically linked program looks up at start.
(allow process-fork process-exec)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-ioctl)
; Reads anywhere, except the operator's credentials.
(allow file-read*)
(deny file-read*
  (subpath (string-append (param "REAL_HOME") "/.ssh"))
  (subpath (string-append (param "REAL_HOME") "/.aws"))
  (subpath (string-append (param "REAL_HOME") "/.config/gh")))
; Writes only to the session's own directories and the device nodes a process needs.
(allow file-write*
  (subpath (param "CLONE"))
  (subpath (param "TMP"))
  (subpath (param "HOME"))
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/random")
  (literal "/dev/urandom")
  (literal "/dev/dtracehelper")
  (regex #"^/dev/tty")
  (regex #"^/dev/fd/"))
; No network, of any kind.
(deny network*)
"""

# Variables a checked command may see; everything else is dropped by ``env -i``.
ENV_ALLOWLIST = ("LANG", "LC_ALL")


@dataclass(frozen=True)
class Box:
    """One sandbox: the clone it may write, and its own TMPDIR and HOME (both outside the clone)."""

    clone: Path
    tmp: Path
    home: Path
    path: str = "/usr/bin:/bin:/usr/sbin:/sbin"
    extra_env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def beside(cls, clone: Path, private: Path, extra_env: Mapping[str, str] | None = None) -> "Box":
        """A box for ``clone`` whose TMPDIR and HOME live under ``private``, outside the clone."""
        tmp, home = private / "sandbox-tmp", private / "sandbox-home"
        tmp.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        gitconfig = private / "sandbox-gitconfig"
        gitconfig.touch()
        venv_bin = clone / ".venv" / "bin"
        env = {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        env.update(extra_env or {})
        return cls(
            clone=clone.resolve(),
            tmp=tmp.resolve(),
            home=home.resolve(),
            path=f"{venv_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            extra_env=env,
        )

    def environment(self, inherited: Mapping[str, str]) -> dict[str, str]:
        env = {name: inherited[name] for name in ENV_ALLOWLIST if name in inherited}
        env.update({"PATH": self.path, "HOME": str(self.home), "TMPDIR": f"{self.tmp}/"})
        env.update(self.extra_env)
        return env

    def argv(self, command: Sequence[str], inherited: Mapping[str, str], real_home: str) -> list[str]:
        params = {"CLONE": self.clone, "TMP": self.tmp, "HOME": self.home, "REAL_HOME": Path(real_home).resolve()}
        wrapped = [SANDBOX_EXEC, "-p", PROFILE]
        for name, value in params.items():
            wrapped += ["-D", f"{name}={value}"]
        wrapped += [ENV, "-i", *(f"{name}={value}" for name, value in sorted(self.environment(inherited).items()))]
        return wrapped + list(command)


def available() -> bool:
    return sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


def run_contained(
    runner: CommandRunner, box: Box, command: Sequence[str], environ: Mapping[str, str], timeout: float
) -> Completed:
    """Run ``command`` in ``box.clone`` under the sandbox. Refuses to run uncontained."""
    if not available():
        raise RuntimeError("sandbox-exec is not available on this machine; the checker refuses to run uncontained")
    real_home = environ.get("HOME") or str(Path.home())
    return runner(box.argv(command, environ, real_home), box.clone, {"PATH": "/usr/bin:/bin"}, timeout)


def unwrap(argv: Sequence[str]) -> tuple[list[str], dict[str, str]] | None:
    """The command and environment inside a sandboxed argv, or None if it isn't one (for fakes)."""
    argv = list(argv)
    if not argv or argv[0] != SANDBOX_EXEC or ENV not in argv:
        return None
    rest = argv[argv.index(ENV) + 2 :]  # skip "env -i"
    env: dict[str, str] = {}
    while rest and "=" in rest[0] and not rest[0].startswith("/"):
        name, value = rest.pop(0).split("=", 1)
        env[name] = value
    return rest, env


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def run_validations_contained(
    commands: Sequence[str], box: Box, environ: Mapping[str, str], runner: CommandRunner, timeout: float = 1800
) -> dict:
    """Run each required check in ``box.clone`` under the sandbox, with nothing else changed."""
    results = []
    for command in commands:
        completed = run_contained(runner, box, ["/bin/sh", "-c", command], environ, timeout)
        output = completed.stdout + completed.stderr
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "timed_out": completed.timed_out,
                "passed": completed.returncode == 0,
                "wall_seconds": completed.wall_seconds,
                "failures": [line for line in output.splitlines() if line.startswith(("FAILED", "ERROR"))][:30],
                "output_tail": "\n".join(output.rstrip().splitlines()[-12:]),
                "contained": True,
            }
        )
    return {"green": all(item["passed"] for item in results), "validations": results}
