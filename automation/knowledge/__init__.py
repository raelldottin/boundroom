"""The knowledge service (SPEC-003, amended by SPEC-007): opt-in, separately deployed, not a runner.

Nothing in this package may reach ``run_next``, a shell, the supervisor's policy, queue or schemas,
Kanban writes, GitHub writes or consumer sync (AGENTS.md, "Knowledge Loop on Discord or Slack").
``automation/tests/test_knowledge_platform.py`` scans every module here for such imports.
"""

import os as _os

from pydantic import BaseModel as _BaseModel


def _without_logfire_plugin() -> None:
    """Keep pydantic from importing Logfire just because the telemetry extra is installed.

    Logfire registers a pydantic plugin, and pydantic loads every installed plugin the first time a
    model is built. Logfire imports ``subprocess``, which this package must never load. The plugin
    is switched off only while pydantic loads its plugins, and the environment is restored. The
    supervisor's models do the same in ``automation/schemas/models.py``.
    """
    key = "PYDANTIC_DISABLE_PLUGINS"
    previous = _os.environ.get(key)
    if previous in ("__all__", "1", "true"):
        return
    _os.environ[key] = ",".join(filter(None, (previous, "logfire-plugin")))
    try:

        class _LoadsPluginsOnce(_BaseModel):
            pass

    finally:
        if previous is None:
            _os.environ.pop(key, None)
        else:
            _os.environ[key] = previous


_without_logfire_plugin()
