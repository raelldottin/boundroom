"""Make every test's process ancestry deterministic under pytest.

The ``ps`` double lives in ``fake_ps`` so the synced test modules that run ``run_agent.sh``
can import it themselves under ``unittest``. Importing it here covers the whole pytest session.
"""

import os
import tempfile

import automation.tests.fake_ps  # noqa: F401

# The supervisor's run lock lives under XDG_STATE_HOME (SPEC-008); keep the suite's out of $HOME.
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="boundroom-test-state-")
