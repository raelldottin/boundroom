"""The knowledge service (SPEC-003, amended by SPEC-007): opt-in, separately deployed, not a runner.

Nothing in this package may reach ``run_next``, a shell, the supervisor's policy, queue or schemas,
Kanban writes, GitHub writes or consumer sync (AGENTS.md, "Knowledge Loop on Discord or Slack").
``automation/tests/test_knowledge_platform.py`` scans every module here for such imports.
"""
