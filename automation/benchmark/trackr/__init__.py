"""Track R of the effectiveness proof (SPEC-012): brownfield tasks, arms U and S, and an independent oracle.

``select`` freezes the task set, ``driver`` runs one (task, arm) session in a disposable clone,
``oracle`` scores a session's final commit with no agent present, and ``pilot`` runs the R1
pilot and exports its diffs for blind review. Nothing here is imported by the supervisor, and
every model session it can start is spending that needs a person's approval.
"""
