"""What the agent command receives from ``run_next``: its prompt and context files, and its environment.

The consumer repository and its queue come from ``test_run_next_single_slice``; the slice titles
carry non-ASCII text so the JSON the agent reads can be checked character for character. The
agent is a short Python script that copies what it was given before writing its handoff.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from automation.supervisor import run_next
from automation.tests import test_run_next_single_slice
from automation.tests.test_run_next_single_slice import FIRST, SECOND, spans, statuses, supervise, trace_flags

consumer = test_run_next_single_slice.consumer
queue = test_run_next_single_slice.queue
traced = test_run_next_single_slice.traced

TITLES = {FIRST: "Café façade", SECOND: "Zoë's naïve résumé"}
MARKER = "RUN_NEXT_TEST_AGENT_MARKER"

SNOOPING_AGENT = """
import json, os, pathlib, shutil, sys
prompt, context, handoff, slice_id, seen = sys.argv[1:6]
seen = pathlib.Path(seen)
seen.mkdir(exist_ok=True)
shutil.copy(prompt, seen / "prompt.md")
shutil.copy(context, seen / "context.json")
(seen / "paths.json").write_text(json.dumps([prompt, context]))
(seen / "marker").write_text(os.environ.get(sys.argv[6], "<unset>"))
json.dump({"slice_id": slice_id, "status": "done", "summary": "Nothing to change.", "files_touched": [],
           "validations_passed": [], "validations_failed": [], "proof_level": "doc-only", "missing_proof_levels": [],
           "contract_status_changes": [], "residual_risks": ["None known."], "recommended_next_slice": "",
           "recommended_next_reason": "", "repo_clean_status": "clean", "git_mirror_status": "not-checked",
           "dirty_paths_outside_scope": [], "timestamp": "2026-11-26T00:00:00Z"}, open(handoff, "w"))
"""


@pytest.fixture
def snooped(queue, monkeypatch, tmp_path):
    """The queue with accented titles, and an --agent-cmd whose agent leaves what it saw in ``seen``."""
    path, _ = queue
    monkeypatch.delenv(MARKER, raising=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["slices"]:
        item["title"] = TITLES[item["slice_id"]]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    git = ["git", "-C", str(path.parents[2]), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "commit", "-qam", "accented titles"], check=True)
    agent = tmp_path / "snooping_agent.py"
    agent.write_text(SNOOPING_AGENT, encoding="utf-8")
    seen = tmp_path / "seen"
    command = " ".join(
        [shlex.quote(sys.executable), shlex.quote(str(agent)), "{prompt_file} {context_file} {handoff_file} {slice_id}"]
        + [shlex.quote(str(seen)), MARKER]
    )
    return path, command, seen


def test_the_prompt_and_context_files_exist_while_the_agent_runs_and_are_removed_after(snooped, monkeypatch, capsys):
    path, command, seen = snooped

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", command) == 0

    assert statuses(path)[FIRST] == "done"
    assert (seen / "prompt.md").read_text(encoding="utf-8").startswith("#")
    assert json.loads((seen / "context.json").read_text(encoding="utf-8"))["slice"]["slice_id"] == FIRST
    prompt_file, context_file = json.loads((seen / "paths.json").read_text(encoding="utf-8"))
    assert not Path(prompt_file).exists() and not Path(context_file).exists()


def test_an_agent_that_removes_its_prompt_and_context_files_is_still_judged_on_its_handoff(queue, monkeypatch, capsys):
    path, log = queue
    agent = log.parent / "agent.py"  # the queue fixture's passing agent
    command = f"rm {{prompt_file}} {{context_file}} && {shlex.quote(sys.executable)} {shlex.quote(str(agent))} {{handoff_file}} {{slice_id}} 0"

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", command) == 0

    assert json.loads(capsys.readouterr().out)["queue_status"] == "done"
    assert statuses(path)[FIRST] == "done"


def test_the_context_file_is_indented_json_that_keeps_non_ascii_text(snooped, monkeypatch, capsys):
    _, command, seen = snooped

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", command) == 0

    text = (seen / "context.json").read_text(encoding="utf-8")
    assert TITLES[FIRST] in text
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def test_the_prompt_carries_the_queue_the_context_and_the_handoff_template_as_indented_json(snooped, monkeypatch, capsys):
    _, command, seen = snooped

    assert supervise(monkeypatch, "--single-slice", "--agent-cmd", command) == 0

    prompt = (seen / "prompt.md").read_text(encoding="utf-8")
    bundle = json.loads((seen / "context.json").read_text(encoding="utf-8"))
    assert TITLES[SECOND] in json.dumps(bundle["queue"], ensure_ascii=False)  # the adjacent slice
    assert TITLES[FIRST].lower() in json.dumps(bundle["handoff_template"], ensure_ascii=False)
    for block in (bundle["queue"], bundle, bundle["handoff_template"]):
        assert json.dumps(block, indent=2, ensure_ascii=False) in prompt


def test_captured_content_keeps_the_context_bundle_text_as_written(snooped, traced, monkeypatch, capsys):
    _, command, _ = snooped
    flags = ("--single-slice", "--agent-cmd", command, *trace_flags(traced), "--telemetry-capture", "content")

    assert supervise(monkeypatch, *flags) == 0

    (context,) = [record["attributes"] for record in spans(traced) if record["name"] == "boundroom.context"]
    assert TITLES[FIRST] in context["bundle_text"]
    assert context["bundle_sha256"] == hashlib.sha256(context["bundle_text"].encode("utf-8")).hexdigest()
    # The digest is of the canonical text, keys sorted, so the same bundle always has the same digest.
    assert context["bundle_text"] == json.dumps(json.loads(context["bundle_text"]), sort_keys=True, ensure_ascii=False)


@pytest.mark.parametrize("profiled", [True, False], ids=["runner-profile", "no-profile"])
def test_the_agent_runs_in_the_environment_passed_in(snooped, traced, monkeypatch, capsys, profiled):
    """main(environ=...) reaches the agent, with a runner profile and without one, never os.environ instead."""
    # ``traced`` only clears the caller's BOUNDROOM_TELEMETRY*/OTEL_* variables before the copy below.
    path, command, seen = snooped
    environ = {**os.environ, MARKER: "injected"}
    if not profiled:  # the mode override set alone is the one way to run with no profile (SPEC-008)
        environ["BOUNDROOM_CLAUDE_PERMISSION_MODE"] = "bypassPermissions"

    assert run_next.main(["--single-slice", "--agent-cmd", command], repo_root=path.parents[2], environ=environ) == 0

    assert (seen / "marker").read_text(encoding="utf-8") == "injected"
    assert statuses(path)[FIRST] == "done"
    assert ("## What you may run" in (seen / "prompt.md").read_text(encoding="utf-8")) is profiled
