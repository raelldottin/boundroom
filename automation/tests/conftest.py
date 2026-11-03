"""Make every test's process ancestry deterministic under pytest.

The ``ps`` double lives in ``fake_ps`` so the synced test modules that run ``run_agent.sh``
can import it themselves under ``unittest``. Importing it here covers the whole pytest session.
"""

import automation.tests.fake_ps  # noqa: F401
