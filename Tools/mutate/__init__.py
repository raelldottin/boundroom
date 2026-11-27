"""A mutation testing harness for Boundroom's Python code.

It needs the standard library, plus pytest and coverage, which are already development
dependencies; it adds no package of its own.

It answers one question: when a line of a module is broken, does a test notice? See
``docs/reference/mutation-testing.md`` for how to run it and read its report.
"""

from __future__ import annotations

from .operators import OPERATORS, Mutant, apply_mutant, generate_mutants

__all__ = ["OPERATORS", "Mutant", "apply_mutant", "generate_mutants"]
