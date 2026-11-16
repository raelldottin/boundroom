"""The operator docs stay linked, and the exit-code reference matches the code (SPEC-009 RA-ENT-011).

Each command's exit codes are read from its source, without running it, so a new code, a new
command-line entry point, a new failure reason or a new decision fails here until
docs/reference/exit-codes.md lists it.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "docs/reference/exit-codes.md"
RUN_NEXT = REPO_ROOT / "automation/supervisor/run_next.py"
POLICY = REPO_ROOT / "automation/supervisor/policy.py"
SYNC = REPO_ROOT / "Tools/boundroom-sync.sh"
OPERATOR_PAGES = ("architecture.md", "security-model.md", "exit-codes.md", "troubleshooting.md")

# Shell scripts are named, not discovered: Tools/ also holds CI checks and the old-name shim.
SHELL_COMMANDS = ("automation/supervisor/run_agent.sh",)
# Directories whose Python command-line entry points the reference must cover.
PYTHON_COMMAND_DIRS = ("automation/supervisor", "automation/context", "Tools")

SHELL_EXIT = re.compile(r"^\s*(?:exit|return) (\d+)\s*$", re.MULTILINE)
TABLE_CODE = re.compile(r"^\| (\d+) \|", re.MULTILINE)
FIRST_CELL = re.compile(r"^\| `([a-z0-9_]+)` \|(.*)$", re.MULTILINE)


def python_entry_points() -> list[str]:
    found = []
    for directory in PYTHON_COMMAND_DIRS:
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            if '__name__ == "__main__"' in path.read_text(encoding="utf-8"):
                found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


def commands() -> list[str]:
    return [*python_entry_points(), *SHELL_COMMANDS, "Tools/boundroom-sync.sh"]


def python_source(relative: str) -> str:
    text = (REPO_ROOT / relative).read_text(encoding="utf-8")
    if relative == "Tools/boundroom-sync.sh":
        # A shell launcher around one Python heredoc; the heredoc is the program.
        match = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.DOTALL)
        assert match, "the sync tool no longer wraps one <<'PY' heredoc"
        return match.group(1)
    return text


def int_constants(node: ast.AST | None) -> set[int]:
    """The integers an expression can evaluate to, when they are written as literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return int_constants(node.body) | int_constants(node.orelse)
    return set()


def python_exit_codes(relative: str) -> set[int]:
    """Every literal a Python command returns or exits with, including EX_* constants it returns."""
    tree = ast.parse(python_source(relative))
    constants: dict[str, set[int]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            constants[node.targets[0].id] = int_constants(node.value)
    codes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            codes |= int_constants(node.value)
            values = [node.value.body, node.value.orelse] if isinstance(node.value, ast.IfExp) else [node.value]
            for value in values:
                if isinstance(value, ast.Name) and value.id.startswith("EX_"):
                    codes |= constants.get(value.id, set())
        elif isinstance(node, ast.Call) and node.args:
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in ("exit", "SystemExit"):
                codes |= int_constants(node.args[0])
    for function in ast.walk(tree):
        # A default exit code, as in run_next's fail(reason, code, exit_code=2).
        if isinstance(function, ast.FunctionDef):
            names = [arg.arg for arg in function.args.args]
            defaults = function.args.defaults
            for name, default in zip(names[len(names) - len(defaults) :], defaults):
                if name == "exit_code":
                    codes |= int_constants(default)
    return codes


def exit_codes(relative: str) -> set[int]:
    if relative in SHELL_COMMANDS:
        return {int(code) for code in SHELL_EXIT.findall((REPO_ROOT / relative).read_text(encoding="utf-8"))}
    return python_exit_codes(relative)


def sections() -> dict[str, str]:
    """Each heading of the reference, by the path it names in backticks, to the text under it."""
    found: dict[str, str] = {}
    parts = re.split(r"^#{2,3} (.*)$", REFERENCE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    for heading, body in zip(parts[1::2], parts[2::2]):
        match = re.search(r"`([^`]+)`", heading)
        if match:
            found[match.group(1)] = body
    return found


def table_after(title: str) -> list[tuple[str, str]]:
    """The first cell and the rest of each row of the table under a ### heading."""
    text = REFERENCE.read_text(encoding="utf-8")
    body = text.split(f"### {title}\n", 1)[1].split("\n#", 1)[0]
    return FIRST_CELL.findall(body)


def failure_reasons() -> set[str]:
    """The reason codes run_next passes to fail(), and the ones session_error() returns."""
    tree = ast.parse(RUN_NEXT.read_text(encoding="utf-8"))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "fail" and len(node.args) >= 2:
            code = node.args[1]
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                reasons.add(code.value)
        if isinstance(node, ast.FunctionDef) and node.name == "session_error":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and re.fullmatch(r"[a-z_]+", inner.value):
                    reasons.add(inner.value)
    return reasons


def decisions() -> dict[str, set[str]]:
    """Each decision policy.py can return, to the queue statuses it pairs with."""
    tree = ast.parse(POLICY.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CompletionDecision":
            keywords = {k.arg: k.value for k in node.keywords}
            decision, status = keywords.get("decision"), keywords.get("queue_status")
            if isinstance(decision, ast.Constant) and isinstance(status, ast.Constant):
                found.setdefault(str(decision.value), set()).add(str(status.value))
    return found


def prompt_error_codes() -> set[str]:
    text = RUN_NEXT.read_text(encoding="utf-8")
    return set(re.findall(r'ConfigError\(\s*f?"([a-z]+(?:_[a-z0-9]+)+):', text))


class EntryPointTests(unittest.TestCase):
    def test_every_command_has_a_section(self) -> None:
        documented = sections()
        for command in commands():
            with self.subTest(command=command):
                self.assertIn(command, documented, f"{command} has no `{command}` heading in {REFERENCE.name}")

    def test_the_discovery_finds_the_commands_it_should(self) -> None:
        # A guard on the discovery itself, so an empty glob can't pass the test above.
        found = commands()
        for command in (
            "automation/supervisor/run_next.py",
            "automation/supervisor/evaluate_handoff.py",
            "automation/context/build_context.py",
            "Tools/changelog_section.py",
            "Tools/boundroom-sync.sh",
            "automation/supervisor/run_agent.sh",
        ):
            self.assertIn(command, found)


class ExitCodeTests(unittest.TestCase):
    def test_every_exit_code_in_the_source_is_documented(self) -> None:
        documented = sections()
        for command in commands():
            with self.subTest(command=command):
                listed = {int(code) for code in TABLE_CODE.findall(documented.get(command, ""))}
                missing = exit_codes(command) - listed
                self.assertFalse(missing, f"{command} can exit {sorted(missing)}, which {REFERENCE.name} doesn't list")

    def test_the_extraction_sees_the_codes_it_should(self) -> None:
        # Known codes from each kind of source, so a broken extractor can't pass vacuously.
        self.assertTrue({0, 2, 64, 78} <= exit_codes("automation/supervisor/run_next.py"))
        self.assertTrue({64, 66, 69, 73, 78} <= exit_codes("automation/supervisor/run_agent.sh"))
        self.assertTrue({0, 1, 2} <= exit_codes("Tools/boundroom-sync.sh"))
        self.assertTrue({0, 1, 64} <= exit_codes("Tools/check_plugin_version.py"))
        self.assertIn(1, exit_codes("automation/supervisor/operator_status.py"))

    def test_a_shell_exit_inside_a_quoted_string_is_not_counted(self) -> None:
        # run_agent.sh builds a sandbox command containing "exit 75"; the wrapper never exits 75.
        self.assertIn("exit 75", (REPO_ROOT / "automation/supervisor/run_agent.sh").read_text(encoding="utf-8"))
        self.assertNotIn(75, exit_codes("automation/supervisor/run_agent.sh"))


class ErrorTableTests(unittest.TestCase):
    def test_the_failure_reasons_match_run_next(self) -> None:
        documented = {code for code, _ in table_after("Failure reasons")}
        self.assertEqual(failure_reasons(), documented)

    def test_the_decisions_match_policy(self) -> None:
        rows = {code: rest for code, rest in table_after("Decisions")}
        found = decisions()
        self.assertEqual(set(found), set(rows))
        for decision, statuses in found.items():
            with self.subTest(decision=decision):
                for status in statuses:
                    self.assertIn(f"`{status}`", rows[decision])

    def test_the_prompt_errors_match_run_next(self) -> None:
        documented = {code for code, _ in table_after("Prompt and rules errors")}
        self.assertEqual(prompt_error_codes(), documented)


class LinkTests(unittest.TestCase):
    def test_every_operator_page_is_in_the_site_navigation(self) -> None:
        nav = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        for page in OPERATOR_PAGES:
            with self.subTest(page=page):
                self.assertIn(f"reference/{page}", nav)

    def test_the_readme_and_the_compatibility_policy_link_the_reference(self) -> None:
        self.assertIn("](docs/reference/exit-codes.md)", (REPO_ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("](exit-codes.md)", (REPO_ROOT / "docs/reference/compatibility.md").read_text(encoding="utf-8"))

    def test_relative_links_between_operator_pages_resolve(self) -> None:
        for page in OPERATOR_PAGES:
            path = REPO_ROOT / "docs/reference" / page
            for target in re.findall(r"\]\(([^)#:]+\.md)(?:#[^)]*)?\)", path.read_text(encoding="utf-8")):
                with self.subTest(page=page, target=target):
                    self.assertTrue((path.parent / target).resolve().is_file(), f"{page} links to missing {target}")


if __name__ == "__main__":
    unittest.main()
