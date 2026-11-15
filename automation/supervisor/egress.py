"""The one place Boundroom decides whether it may open a network connection (SPEC-006 RA-TEL-011).

``BOUNDROOM_OFFLINE`` turns every network connection Boundroom itself would make into a refusal,
made before the connection is tried: an ``otlp`` telemetry destination, its ``required`` startup
probe, and the Hindsight memory client. Any value but empty, ``0`` or ``false`` turns it on, so a
typo fails closed. File-mode telemetry needs no network and stays allowed.

The switch covers Boundroom's own Python code. A child process Boundroom starts, such as the
runner talking to its model provider, ``git``, ``hermes`` or the sync tool's quality command,
does its own networking and isn't covered.

Every network path in ``automation/`` calls ``guard_egress`` first.
``automation/tests/test_egress.py`` fails on a module that opens a connection without it.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

OFFLINE_ENV = "BOUNDROOM_OFFLINE"
_OFF_VALUES = ("", "0", "false")
REASON = "offline"


class EgressRefused(Exception):
    """A network connection Boundroom would make was refused before it was tried."""

    def __init__(self, purpose: str) -> None:
        self.purpose = purpose
        self.reason = REASON
        super().__init__(refusal_message(purpose))


def offline(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether ``BOUNDROOM_OFFLINE`` is on in ``environ`` (the process environment by default)."""
    value = (os.environ if environ is None else environ).get(OFFLINE_ENV)
    return value is not None and value.strip().lower() not in _OFF_VALUES


def refusal_message(purpose: str) -> str:
    """The refusal, naming the purpose and the reason. Never the URL: it may carry a tenant or token."""
    return f"{purpose}: network egress refused ({REASON}): {OFFLINE_ENV} is set"


def guard_egress(purpose: str, url: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """``url`` unchanged when Boundroom may connect to it; ``EgressRefused`` when offline.

    Call sites pass the URL through this function on its way to the client, so the check sits in
    the data path rather than beside it.
    """
    if offline(environ):
        raise EgressRefused(purpose)
    return url
