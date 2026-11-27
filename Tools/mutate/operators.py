"""Mutation operators: which mutants a module's source yields, and how to apply one.

This is the harness's functional core. It reads source text and returns plain values; it
never touches a file or runs a process. Each mutant changes one AST node, and is applied by
replacing that node's exact byte span in the source, so comments and formatting elsewhere
survive and the diff stays small.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# The largest integer literal the number operators touch. It covers the exit codes the
# supervisor uses (64, 78) and small counts, and leaves timeouts and sizes alone.
SMALL_INT = 255

# Calls to these methods on these names are logging, so their arguments are not mutated.
LOGGER_NAMES = frozenset({"logging", "logger", "log", "LOGGER", "LOG", "_logger", "_log"})
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})

COMPARISON_SWAPS: dict[type[ast.cmpop], tuple[str, str]] = {
    ast.Lt: (r"(?<![<>=!])<(?![<=])", "<="),
    ast.LtE: (r"<=", "<"),
    ast.Gt: (r"(?<![<>=!-])>(?![>=])", ">="),
    ast.GtE: (r">=", ">"),
    ast.Eq: (r"==", "!="),
    ast.NotEq: (r"!=", "=="),
    ast.In: (r"\bin\b", "not in"),
    ast.NotIn: (r"\bnot\s+in\b", "in"),
    ast.Is: (r"\bis\b", "is not"),
    ast.IsNot: (r"\bis\s+not\b", "is"),
}


@dataclass(frozen=True)
class Mutant:
    """One change to one node.

    ``line`` is the node's first line. ``original`` and ``mutant`` are the node's source text
    before and after the change; the allowlist matches on them. ``start`` and ``end`` are the
    byte offsets of the replaced span, and ``replacement`` is what goes there.
    ``import_time`` is true for code that runs when the module is imported: module level,
    class bodies, default values and decorators, but not function bodies.
    """

    line: int
    operator: str
    original: str
    mutant: str
    start: int
    end: int
    replacement: str
    end_line: int
    statement_line: int
    import_time: bool = False


class _Source:
    """Byte offsets for AST positions. AST columns count UTF-8 bytes, not characters."""

    def __init__(self, text: str) -> None:
        self.data = text.encode("utf-8")
        self.line_starts = [0]
        for line in self.data.splitlines(keepends=True):
            self.line_starts.append(self.line_starts[-1] + len(line))

    def offset(self, lineno: int, col: int) -> int:
        return self.line_starts[lineno - 1] + col

    def span(self, node: ast.AST) -> tuple[int, int]:
        start = self.offset(getattr(node, "lineno"), getattr(node, "col_offset"))
        end = self.offset(getattr(node, "end_lineno"), getattr(node, "end_col_offset"))
        return start, end

    def text(self, start: int, end: int) -> str:
        return self.data[start:end].decode("utf-8")


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_logging_or_print(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        owner = func.value
        if isinstance(owner, ast.Name):
            return owner.id in LOGGER_NAMES
        if isinstance(owner, ast.Attribute):
            return owner.attr in LOGGER_NAMES  # self.logger.info(...)
        if isinstance(owner, ast.Call):
            return _call_name(owner.func) == "getLogger"
    return False


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _is_dunder_all(node: ast.stmt) -> bool:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets)


def _is_docstring(node: ast.stmt, parent: ast.AST) -> bool:
    body = getattr(parent, "body", None)
    return (
        isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and isinstance(body, list)
        and bool(body)
        and body[0] is node
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


class _Generator:
    def __init__(self, text: str) -> None:
        self.src = _Source(text)
        self.found: list[Mutant] = []
        self.function_depth = 0

    def add(self, node: ast.AST, operator: str, start: int, end: int, replacement: str, statement: ast.stmt) -> None:
        node_start, node_end = self.src.span(node)
        original = self.src.text(node_start, node_end)
        mutated = (self.src.data[node_start:start] + replacement.encode("utf-8") + self.src.data[end:node_end]).decode("utf-8")
        if mutated == original:
            return
        self.found.append(
            Mutant(
                line=getattr(node, "lineno"),
                operator=operator,
                original=original,
                mutant=mutated,
                start=start,
                end=end,
                replacement=replacement,
                end_line=getattr(node, "end_lineno"),
                statement_line=statement.lineno,
                import_time=self.function_depth == 0,
            )
        )

    def replace_node(self, node: ast.AST, operator: str, replacement: str, statement: ast.stmt) -> None:
        start, end = self.src.span(node)
        self.add(node, operator, start, end, replacement, statement)

    # Walking ---------------------------------------------------------------------------

    def visit_body(self, statements: list[ast.stmt], parent: ast.AST) -> None:
        for statement in statements:
            if _is_docstring(statement, parent) or _is_dunder_all(statement):
                continue
            self.visit_statement(statement)

    def visit_statement(self, node: ast.stmt) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            self.visit_body(node.orelse, node)
            return
        self.statement_mutants(node)
        for field, value in ast.iter_fields(node):
            if field in ("returns", "type_comment") or (isinstance(node, ast.AnnAssign) and field == "annotation"):
                continue
            if field == "args" and isinstance(value, ast.arguments):
                self.visit_arguments(value, node)
            elif field == "body" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.function_depth += 1
                self.visit_body(value, node)
                self.function_depth -= 1
            elif isinstance(value, list):
                if value and all(isinstance(item, ast.stmt) for item in value):
                    self.visit_body(value, node)
                else:
                    for item in value:
                        if isinstance(item, ast.stmt):
                            self.visit_statement(item)
                        elif isinstance(item, ast.AST):
                            self.visit_node(item, node)
            elif isinstance(value, ast.stmt):
                self.visit_statement(value)
            elif isinstance(value, ast.AST):
                self.visit_node(value, node)

    def visit_arguments(self, arguments: ast.arguments, statement: ast.stmt) -> None:
        # Annotations are skipped; default values are code and are mutated.
        for default in [*arguments.defaults, *arguments.kw_defaults]:
            if default is not None:
                self.visit_node(default, statement)

    def visit_node(self, node: ast.AST, statement: ast.stmt) -> None:
        if isinstance(node, ast.JoinedStr):
            return  # f-string interiors are formatting, and their positions vary by Python version
        if isinstance(node, ast.Call) and _is_logging_or_print(node):
            return
        if isinstance(node, ast.arg):
            return  # only its annotation would be left, and annotations are skipped
        if isinstance(node, ast.arguments):
            self.visit_arguments(node, statement)
            return
        if isinstance(node, ast.expr):
            self.expression_mutants(node, statement)
        for name, child in ast.iter_fields(node):
            if isinstance(node, ast.Lambda) and name == "body":
                self.function_depth += 1
                self.visit_node(child, statement)
                self.function_depth -= 1
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, ast.stmt):
                        self.visit_statement(item)
                    elif isinstance(item, ast.AST):
                        self.visit_node(item, statement)
            elif isinstance(child, ast.AST):
                self.visit_node(child, statement)

    # Operators -------------------------------------------------------------------------

    def statement_mutants(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.Return, ast.Raise)):
            self.replace_node(node, "delete_statement", "pass", node)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            name = _call_name(node.value.func) or ""
            if name.startswith(("_fail", "fail")):
                self.replace_node(node, "delete_statement", "pass", node)
        if isinstance(node, (ast.Continue, ast.Break)):
            self.replace_node(node, "loop_control", "pass", node)
        if isinstance(node, ast.Return) and node.value is not None:
            value = node.value
            if not (isinstance(value, ast.Constant) and value.value is None):
                self.replace_node(node, "return_none", "return None", node)

    def expression_mutants(self, node: ast.expr, statement: ast.stmt) -> None:
        if isinstance(node, ast.Compare):
            self.comparison(node, statement)
        elif isinstance(node, ast.BoolOp):
            self.boolean_operator(node, statement)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            start, _ = self.src.span(node)
            operand_start, _ = self.src.span(node.operand)
            gap = self.src.text(start, operand_start)
            keyword = re.match(r"not\s*", gap)
            if keyword:
                self.add(node, "remove_not", start, start + len(keyword.group(0).encode("utf-8")), "", statement)
        elif isinstance(node, ast.Constant):
            self.constant(node, statement)
        elif isinstance(node, ast.Call):
            self.swap_arguments(node, statement)

    def comparison(self, node: ast.Compare, statement: ast.stmt) -> None:
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            _, gap_start = self.src.span(operands[index])
            gap_end, _ = self.src.span(operands[index + 1])
            pattern, replacement = COMPARISON_SWAPS[type(op)]
            found = self._unique(pattern, gap_start, gap_end)
            if found is not None:
                self.add(node, "comparison", found[0], found[1], replacement, statement)

    def boolean_operator(self, node: ast.BoolOp, statement: ast.stmt) -> None:
        word, other = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
        spans: list[tuple[int, int]] = []
        for left, right in zip(node.values, node.values[1:]):
            _, gap_start = self.src.span(left)
            gap_end, _ = self.src.span(right)
            found = self._unique(rf"\b{word}\b", gap_start, gap_end)
            if found is None:
                return
            spans.append(found)
        # One node, one change: every `and` of this BoolOp becomes `or`, or the other way.
        start, end = spans[0][0], spans[-1][1]
        pieces = []
        cursor = start
        for span_start, span_end in spans:
            pieces.append(self.src.text(cursor, span_start))
            pieces.append(other)
            cursor = span_end
        self.add(node, "boolean_operator", start, end, "".join(pieces), statement)

    def constant(self, node: ast.Constant, statement: ast.stmt) -> None:
        value = node.value
        if isinstance(value, bool):
            self.replace_node(node, "boolean_constant", "False" if value else "True", statement)
        elif isinstance(value, int) and 0 <= value <= SMALL_INT:
            self.replace_node(node, "number_increment", str(value + 1), statement)
            if value != 0:
                self.replace_node(node, "number_zero", "0", statement)

    def swap_arguments(self, node: ast.Call, statement: ast.stmt) -> None:
        if len(node.args) < 2:
            return
        first, second = node.args[0], node.args[1]
        if not (isinstance(first, ast.Name) and isinstance(second, ast.Name)) or first.id == second.id:
            return
        first_start, first_end = self.src.span(first)
        second_start, second_end = self.src.span(second)
        between = self.src.text(first_end, second_start)
        replacement = self.src.text(second_start, second_end) + between + self.src.text(first_start, first_end)
        self.add(node, "swap_arguments", first_start, second_end, replacement, statement)

    def _unique(self, pattern: str, start: int, end: int) -> tuple[int, int] | None:
        """The byte span of the one match of ``pattern`` between two operands, or None."""
        gap = self.src.text(start, end)
        code = re.sub(r"#[^\n]*", lambda comment: " " * len(comment.group(0)), gap)  # same offsets, no comments
        matches = list(re.finditer(pattern, code))
        if len(matches) != 1:
            return None
        match = matches[0]
        before = len(gap[: match.start()].encode("utf-8"))
        length = len(match.group(0).encode("utf-8"))
        return start + before, start + before + length


def generate_mutants(source: str) -> list[Mutant]:
    """Every mutant of ``source``, in source order. Raises SyntaxError if it doesn't parse."""
    tree = ast.parse(source)
    generator = _Generator(source)
    generator.visit_body(tree.body, tree)
    order = {name: index for index, name in enumerate(OPERATORS)}
    return dedupe(sorted(generator.found, key=lambda m: (m.start, -m.end, order[m.operator], m.mutant)))


def dedupe(mutants: list[Mutant]) -> list[Mutant]:
    """``mutants`` without repeats of the same edit (start, end, replacement), first kept.

    An edit fixes the mutated program, so this drops exact duplicates without holding a copy
    of the program for each mutant.
    """
    unique: list[Mutant] = []
    seen: set[tuple[int, int, str]] = set()
    for mutant in mutants:
        edit = (mutant.start, mutant.end, mutant.replacement)
        if edit not in seen:
            seen.add(edit)
            unique.append(mutant)
    return unique


def apply_mutant(source: str, mutant: Mutant) -> str:
    """``source`` with ``mutant`` applied: only its span changes."""
    data = source.encode("utf-8")
    return (data[: mutant.start] + mutant.replacement.encode("utf-8") + data[mutant.end :]).decode("utf-8")


OPERATORS = (
    "delete_statement",
    "return_none",
    "loop_control",
    "comparison",
    "boolean_operator",
    "remove_not",
    "boolean_constant",
    "number_increment",
    "number_zero",
    "swap_arguments",
)
