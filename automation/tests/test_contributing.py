"""The contribution guide, the templates and the pre-commit hooks ask for what the gate checks (SPEC-009 RA-ENT-012).

The required checks are read from AGENTS.md and the gate's commands from the Makefile, so when
either changes, the guide, the pull request template or the hooks fail here until they follow.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "AGENTS.md"
MAKEFILE = REPO_ROOT / "Makefile"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
PR_TEMPLATE = REPO_ROOT / ".github/pull_request_template.md"
ISSUE_TEMPLATES = REPO_ROOT / ".github/ISSUE_TEMPLATE"
BUG_REPORT = ISSUE_TEMPLATES / "bug_report.yml"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
TROUBLESHOOTING = REPO_ROOT / "docs/reference/troubleshooting.md"
TRAILER_CHECK = "Tools/check-commit-trailers.sh"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def required_checks() -> list[str]:
    """The check names AGENTS.md says the main ruleset requires."""
    # Check names hold parentheses, as in `make reusable-check (3.11)`, so match the backticked list.
    match = re.search(r"the \w+ required checks \(((?:`[^`]+`(?:, and |, )?)+)\)", read(AGENTS))
    assert match, "AGENTS.md no longer lists the required checks as 'the <n> required checks (`a`, `b`, ...)'"
    return re.findall(r"`([^`]+)`", match.group(1))


def gate_commands() -> list[str]:
    """The commands make reusable-check runs, with $(UV) spelled uv."""
    body = read(MAKEFILE).split("\nreusable-check:\n", 1)[1]
    commands = []
    for line in body.splitlines():
        if not line.startswith("\t"):
            break
        commands.append(line.strip().replace("$(UV)", "uv"))
    return commands


def hooks() -> list[dict[str, str]]:
    """Each pre-commit hook's scalar keys, read without a YAML parser."""
    found: list[dict[str, str]] = []
    for line in read(PRE_COMMIT).splitlines():
        item = re.match(r"^\s+- id:\s*(\S+)\s*$", line)
        if item:
            found.append({"id": item.group(1)})
            continue
        key = re.match(r"^\s+(\w+):\s*(.+?)\s*$", line)
        if key and found:
            found[-1][key.group(1)] = key.group(2)
    return found


def section(title: str) -> str:
    """The text under a ## heading of the pull request template, up to the next one."""
    parts = re.split(r"^## (.+)$", read(PR_TEMPLATE), flags=re.MULTILINE)
    found = dict(zip(parts[1::2], parts[2::2]))
    assert title in found, f"the pull request template has no '## {title}' section"
    return found[title]


def form_ids(path: Path) -> set[str]:
    return set(re.findall(r"^\s+id:\s*(\w+)\s*$", read(path), re.MULTILINE))


class RequiredCheckTests(unittest.TestCase):
    def test_agents_md_names_the_five_required_checks(self) -> None:
        # A guard on the parse, so an empty list can't pass the tests below.
        self.assertEqual(len(required_checks()), 5)
        self.assertIn("No agent attribution trailers", required_checks())

    def test_the_guide_and_the_pull_request_template_name_every_required_check(self) -> None:
        for path in (CONTRIBUTING, PR_TEMPLATE):
            text = read(path)
            for check in required_checks():
                with self.subTest(file=path.name, check=check):
                    self.assertIn(check, text)


class PullRequestTemplateTests(unittest.TestCase):
    def test_it_asks_for_the_gate_and_the_evidence_around_it(self) -> None:
        # Each item must be asked for as a checkbox in its own section, so a name mentioned
        # elsewhere, such as a required check's, doesn't stand in for the question.
        asked = {
            "Evidence": (
                "`make reusable-check` passes locally",  # the gate itself
                "automation.schemas.generate",  # the gate's schema check
                "settings_view.py --docs",  # the gate's settings reference check
                "Tests added or changed",  # the gate's tests
                "mkdocs build --strict",  # what publish-docs.yml runs on main
                "docs/reference/exit-codes.md",  # what test_operator_docs.py checks
            ),
            "Approval": (TRAILER_CHECK,),  # the attribution-trailer check
        }
        for title, items in asked.items():
            boxes = "\n".join(line for line in section(title).splitlines() if line.startswith("- [ ] "))
            for item in items:
                with self.subTest(section=title, item=item):
                    self.assertIn(item, boxes)

    def test_it_asks_what_a_consumer_sees_and_what_the_supervisor_costs(self) -> None:
        self.assertIn("CHANGELOG.md", section("What a consumer sees"))  # the compatibility policy
        self.assertIn("file, network or subprocess work", section("Supervisor cost"))  # AGENTS.md, Performance

    def test_it_says_a_person_other_than_the_author_approves(self) -> None:
        self.assertIn("A person other than the author approves the head commit", read(PR_TEMPLATE))


class IssueTemplateTests(unittest.TestCase):
    def test_the_forms_and_the_hook_config_are_valid_yaml(self) -> None:
        # GitHub drops an issue form it can't parse, without saying so. PyYAML comes with mkdocs,
        # a dev dependency.
        try:
            import yaml
        except ImportError:  # pragma: no cover - only outside the dev environment
            self.skipTest("PyYAML isn't installed")
        for path in [*ISSUE_TEMPLATES.glob("*.yml"), PRE_COMMIT]:
            with self.subTest(file=path.name):
                self.assertIsInstance(yaml.safe_load(read(path)), dict)

    def test_a_bug_report_asks_for_what_troubleshooting_says_to_include(self) -> None:
        reporting = read(TROUBLESHOOTING).split("## Reporting a problem", 1)[1]
        for phrase in ("exit code", "`stop:`", "decision or failure report", "operator_status.py --json"):
            # The guide still asks for it, so the form must too.
            self.assertIn(phrase, reporting)
        self.assertTrue({"version", "command", "exit_code", "stop_line", "report", "operator_status"} <= form_ids(BUG_REPORT))

    def test_vulnerabilities_are_sent_to_private_reporting_not_an_issue(self) -> None:
        config = read(ISSUE_TEMPLATES / "config.yml")
        self.assertIn("blank_issues_enabled: false", config)
        self.assertIn("/security/advisories/new", config)
        for form in ISSUE_TEMPLATES.glob("*.yml"):
            if form.name != "config.yml":
                with self.subTest(form=form.name):
                    self.assertIn("SECURITY.md", read(form))


class PreCommitTests(unittest.TestCase):
    def test_every_hook_is_local(self) -> None:
        # No remote hook repository, so pre-commit downloads no code and there is nothing to pin.
        repos = re.findall(r"^\s*- repo:\s*(\S+)\s*$", read(PRE_COMMIT), re.MULTILINE)
        self.assertEqual(repos, ["local"])
        for hook in hooks():
            with self.subTest(hook=hook["id"]):
                self.assertEqual(hook.get("language"), "system")

    def test_every_hook_runs_a_command_from_the_gate(self) -> None:
        allowed = [*gate_commands(), "make reusable-check", TRAILER_CHECK]
        self.assertIn("uv run ruff check .", allowed)  # a guard on the Makefile parse
        for hook in hooks():
            with self.subTest(hook=hook["id"]):
                entry = hook["entry"]
                # pre-commit appends file names, so a hook runs a gate command on fewer paths.
                self.assertTrue(
                    any(command == entry or command.removesuffix(" .") == entry for command in allowed),
                    f"{hook['id']} runs {entry!r}, which isn't a gate command",
                )

    def test_a_push_runs_the_whole_gate_and_the_trailer_check(self) -> None:
        self.assertRegex(read(PRE_COMMIT), r"(?m)^default_install_hook_types:\s*\[[^\]]*\bpre-push\b")
        on_push = {hook["entry"] for hook in hooks() if "pre-push" in hook.get("stages", "")}
        self.assertTrue({"make reusable-check", TRAILER_CHECK} <= on_push)


class ContributingTests(unittest.TestCase):
    def test_it_carries_the_segregation_of_duties_rules(self) -> None:
        text = read(CONTRIBUTING)
        for rule in (
            "The author never approves their own change",
            "An approval covers one head commit",
            "--match-head-commit",
            "boundroom-agent[bot]",
            "Nobody weakens a control",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, text)

    def test_relative_links_resolve(self) -> None:
        for path in (CONTRIBUTING, PR_TEMPLATE):
            for target in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", read(path)):
                with self.subTest(file=path.name, target=target):
                    self.assertTrue((path.parent / target).exists(), f"{path.name} links to missing {target}")

    def test_the_readme_links_the_guide(self) -> None:
        self.assertIn("](CONTRIBUTING.md)", read(REPO_ROOT / "README.md"))


if __name__ == "__main__":
    unittest.main()
