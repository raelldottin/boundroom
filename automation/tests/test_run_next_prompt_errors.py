"""A prompt the supervisor can't render stops ``run_next`` before the claim, and a dry run reports it.

The prompt used to be rendered inside ``run_agent``, after the slice was claimed, so a consumer's
customized ``slice.md`` with a placeholder a release had renamed failed every slice it reached,
one per run, and a dry run never rendered the prompt, so it passed. Now the run stops before the
claim, the slice stays queued, no agent starts, and ``--dry-run`` fails the same way.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from automation.tests import test_run_next_single_slice
from automation.tests.test_run_next_single_slice import FIRST, SECOND, runs, statuses, supervise

consumer = test_run_next_single_slice.consumer
queue = test_run_next_single_slice.queue


def commit(repo: Path) -> None:
    """Commit the change, so the dirty-tree check doesn't stop the run before the prompt is rendered."""
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "change"], check=True)


def stale_placeholder(repo: Path) -> str:
    template = repo / "automation/prompts/slice.md"
    text = template.read_text(encoding="utf-8")
    assert "__EXECUTION_CONSTRAINTS__" in text
    template.write_text(text.replace("__EXECUTION_CONSTRAINTS__", "__ACCEPTANCE_CHECKS__"), encoding="utf-8")
    return "stop: unknown_prompt_placeholder: __ACCEPTANCE_CHECKS__"


def symlinked_rules(repo: Path) -> str:
    target = repo / "docs/rules.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("rules\n", encoding="utf-8")
    (repo / "automation/runner-rules.md").symlink_to(target)
    return "stop: runner_rules_symlink"


def missing_fragment(repo: Path) -> str:
    (repo / "automation/prompts/base.md").unlink()
    return "stop: prompt_fragment_unreadable: automation/prompts/base.md"


BREAKAGES = [stale_placeholder, symlinked_rules, missing_fragment]


@pytest.mark.parametrize("break_prompt", BREAKAGES, ids=[breakage.__name__ for breakage in BREAKAGES])
def test_a_prompt_that_cannot_be_rendered_stops_before_the_claim(queue, consumer, monkeypatch, capsys, break_prompt):
    path, log = queue
    message = break_prompt(consumer)
    commit(consumer)

    assert supervise(monkeypatch, "--single-slice") == 2

    assert statuses(path) == {FIRST: "queued", SECOND: "queued"}
    assert runs(log) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


@pytest.mark.parametrize("break_prompt", BREAKAGES, ids=[breakage.__name__ for breakage in BREAKAGES])
def test_a_dry_run_reports_a_prompt_that_cannot_be_rendered(queue, consumer, monkeypatch, capsys, break_prompt):
    path, _ = queue
    message = break_prompt(consumer)
    commit(consumer)

    assert supervise(monkeypatch, "--dry-run") == 2

    assert statuses(path) == {FIRST: "queued", SECOND: "queued"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_the_agent_gets_the_prompt_rendered_before_the_claim(queue, consumer, monkeypatch):
    """The prompt file handed to the agent command is the rendered prompt."""
    path, _ = queue
    seen = consumer.parent / "prompt-seen.md"
    agent = consumer.parent / "agent.py"  # the queue fixture's passing agent
    agent_cmd = (
        f"cp {{prompt_file}} {shlex.quote(str(seen))} && "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} 0"
    )

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", agent_cmd) == 0

    assert statuses(path)[FIRST] == "done"
    prompt = seen.read_text(encoding="utf-8")
    assert f"`slice_id`: `{FIRST}`" in prompt
    assert re.search(r"__[A-Z0-9_]+__", prompt) is None
