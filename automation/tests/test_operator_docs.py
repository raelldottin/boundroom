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
SHELL_COMMANDS = ("automation/supervisor/run_agent.sh", "Tools/boundroom-sync.sh")
# Directories whose Python command-line entry points the reference must cover.
PYTHON_COMMAND_DIRS = ("automation/supervisor", "automation/context", "Tools")
# Documented codes no literal in the source shows, because the command passes on the exit code
# of a program it runs.
PASSED_THROUGH = {"automation/context/swarm_wiki.py": {0}}  # the pinned compiler's own exit 0

# A heredoc: the line that opens it, its delimiter, the rest of that line, and its body.
HEREDOC = re.compile(
    r"^(?P<prefix>[^\n]*?)(?<!<)<<(?!<)-?[ \t]*(?P<quote>['\"]?)(?P<delimiter>\w+)(?P=quote)(?P<rest>[^\n]*)\n"
    r"(?P<body>.*?)\n[ \t]*(?P=delimiter)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
# Quoted strings and comments, which can mention exit without exiting.
SHELL_NOISE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|(?<![\w${#])#[^\n]*")
SHELL_EXIT = re.compile(r"(?<![\w$-])(?:exit|return)[ \t]+(\d+)\b")
TABLE_CODE = re.compile(r"^\| (\d+) \|", re.MULTILINE)
FIRST_CELL = re.compile(r"^\| `([a-z0-9_]+)` \|(.*)$", re.MULTILINE)


def is_entry_point(source: str) -> bool:
    """Whether a module has an ``if __name__ == "__main__":`` block, quoted either way."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            sides = [node.test.left, *node.test.comparators]
            if any(isinstance(side, ast.Name) and side.id == "__name__" for side in sides) and any(
                isinstance(side, ast.Constant) and side.value == "__main__" for side in sides
            ):
                return True
    return False


def python_entry_points() -> list[str]:
    found = []
    for directory in PYTHON_COMMAND_DIRS:
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            if is_entry_point(path.read_text(encoding="utf-8")):
                found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


def commands() -> list[str]:
    return [*python_entry_points(), *SHELL_COMMANDS]


def shell_parts(relative: str) -> tuple[str, list[str]]:
    """A shell command's own text, without heredoc bodies, and the Python programs its heredocs feed an interpreter."""
    programs: list[str] = []

    def keep_opening_line(match: re.Match[str]) -> str:
        if "python" in match.group("prefix").lower():
            programs.append(match.group("body"))
        return f"{match.group('prefix')}{match.group('rest')}"

    text = HEREDOC.sub(keep_opening_line, (REPO_ROOT / relative).read_text(encoding="utf-8"))
    return text, programs


def int_constants(node: ast.AST | None) -> set[int]:
    """The integers an expression can evaluate to, when they are written as literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return int_constants(node.body) | int_constants(node.orelse)
    return set()


def python_exit_codes(source: str) -> set[int]:
    """Every literal a Python program returns or exits with, directly or through a name assigned one."""
    tree = ast.parse(source)
    assigned: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        # EX_CONFIG = 78 at module level, or code = 0 if done else 1 before return code.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, set()).update(int_constants(node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.setdefault(node.target.id, set()).update(int_constants(node.value))

    def exits(value: ast.AST | None) -> set[int]:
        branches = [value.body, value.orelse] if isinstance(value, ast.IfExp) else [value]
        codes = int_constants(value)
        for branch in branches:
            if isinstance(branch, ast.Name):
                codes |= assigned.get(branch.id, set())
        return codes

    codes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            codes |= exits(node.value)
        elif isinstance(node, ast.Call) and node.args:
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in ("exit", "SystemExit"):
                codes |= exits(node.args[0])
        elif isinstance(node, ast.FunctionDef):
            # A default exit code, as in run_next's fail(reason, code, exit_code=2).
            names = [arg.arg for arg in node.args.args]
            defaults = node.args.defaults
            for name, default in zip(names[len(names) - len(defaults) :], defaults):
                if name == "exit_code":
                    codes |= int_constants(default)
    return codes


def exit_codes(relative: str) -> set[int]:
    if relative in SHELL_COMMANDS:
        # The shell's own exits, then each Python program it runs from a heredoc, whose status
        # it passes on, as run_agent.sh's sandbox probe and the sync tool's whole program do.
        shell, programs = shell_parts(relative)
        codes = {int(code) for code in SHELL_EXIT.findall(SHELL_NOISE.sub("", shell))}
        for program in programs:
            codes |= python_exit_codes(program)
        return codes
    return python_exit_codes((REPO_ROOT / relative).read_text(encoding="utf-8"))


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

    def test_every_documented_code_is_one_the_source_can_exit_with(self) -> None:
        # The other direction, so a row for a code a command no longer uses fails too.
        documented = sections()
        for command in commands():
            with self.subTest(command=command):
                listed = {int(code) for code in TABLE_CODE.findall(documented.get(command, ""))}
                stale = listed - exit_codes(command) - PASSED_THROUGH.get(command, set())
                self.assertFalse(stale, f"{REFERENCE.name} lists {sorted(stale)} for {command}, which it can't exit with")

    def test_the_extraction_sees_the_codes_it_should(self) -> None:
        # Known codes from each kind of source, so a broken extractor can't pass vacuously.
        self.assertTrue({0, 2, 64, 78} <= exit_codes("automation/supervisor/run_next.py"))
        # 1 comes from the sandbox probe's Python heredoc, which the wrapper passes on.
        self.assertTrue({0, 1, 64, 66, 69, 73, 78} <= exit_codes("automation/supervisor/run_agent.sh"))
        self.assertTrue({0, 1, 2} <= exit_codes("Tools/boundroom-sync.sh"))
        self.assertTrue({0, 1, 64} <= exit_codes("Tools/check_plugin_version.py"))
        self.assertIn(1, exit_codes("automation/supervisor/operator_status.py"))
        # 0 and 1 are assigned to a name, then returned.
        self.assertTrue({0, 1, 78} <= exit_codes("automation/supervisor/evaluate_handoff.py"))

    def test_a_shell_exit_inside_a_quoted_string_is_not_counted(self) -> None:
        # run_agent.sh builds a sandbox command containing "exit 75"; the wrapper never exits 75.
        self.assertIn("exit 75", (REPO_ROOT / "automation/supervisor/run_agent.sh").read_text(encoding="utf-8"))
        self.assertNotIn(75, exit_codes("automation/supervisor/run_agent.sh"))

    def test_the_shell_pattern_finds_exits_mid_line_but_not_in_strings_or_comments(self) -> None:
        def found(text: str) -> set[int]:
            return {int(code) for code in SHELL_EXIT.findall(SHELL_NOISE.sub("", text))}

        self.assertEqual(found("command -v x >/dev/null || exit 70\n"), {70})
        self.assertEqual(found("if bad; then echo no; exit 71; fi\n"), {71})
        self.assertEqual(found("  exit 72  # a reason\n"), {72})
        self.assertEqual(found("echo \"then exit 73\" 'or exit 74'  # exit 75\n"), set())
        self.assertEqual(found("echo ${#args} $# ; exit 76\n"), {76})

    def test_an_entry_point_is_found_whichever_quotes_it_uses(self) -> None:
        self.assertTrue(is_entry_point("if __name__ == '__main__':\n    pass\n"))
        self.assertTrue(is_entry_point('if "__main__" == __name__:\n    pass\n'))
        self.assertFalse(is_entry_point('NAME = "__main__"\n'))


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
