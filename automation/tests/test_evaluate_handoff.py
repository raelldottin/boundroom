"""Every rejection gate in evaluate_handoff.py decides something a test can see.

Two kinds of test, for the two halves of the module:

- **The CLI, end to end** (``TestGates``, ``TestManualProof``, ``TestExitCodes`` and the tests
  between them). Each builds the smallest diff or handoff that trips one gate, runs the CLI in a
  subprocess, and checks its JSON report and exit code (0 done, 1 otherwise, 78 a refusal;
  docs/reference/exit-codes.md). The gate tests use ``--dry-run``; ``TestExitCodes`` runs without
  it, each in a repository of its own. Git and the queue file are real: a temporary repository with
  a one-slice queue at a base commit and scripted work commits after it, so nothing in the
  supervisor is replaced. The temporary repository gets a copy of this checkout's
  ``automation/supervisor``, ``automation/schemas`` and ``automation/context``, because
  ``evaluate_handoff.py`` evaluates the repository it sits in. That is also how a consumer runs it.
- **The decision core, in memory** (``TestDecide*``, ``TestNextStage``). ``decide`` and
  ``next_stage`` are pure, so these build ``Facts`` directly, with made-up SHAs and no git, and check
  the report ``decide`` returns. They cover every gate, boundary and gate order in a fraction of a
  second; the CLI tests above show that the shell gathers those facts from a real repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional, cast

import pytest

from automation.supervisor import evaluate_handoff
from automation.supervisor.policy import ValidationReplayResult

SOURCE_ROOT = Path(__file__).resolve().parents[2]
QUEUE = "automation/queue/slices.json"
SLICE_ID = "S-1"
HANDOFF_IN_REPO = f"automation/handoffs/{SLICE_ID}.json"
MISSING_SHA = "0" * 40
# The caller's git state, telemetry settings and supervised-slice markers stay out of the subprocess.
SCRUBBED_ENV_PREFIXES = ("GIT_", "BOUNDROOM_", "REPO_AUTOMATION_", "OWLORY_", "OTEL_")

SLICE: dict[str, Any] = {
    "slice_id": SLICE_ID,
    "title": "Edit the docs",
    "status": "queued",
    "priority": 10,
    "domain": "docs",
    "allowed_paths": ["docs/"],
    "required_validations": ["git diff --check"],
    "depends_on": [],
    "max_files_changed": 2,
    "notes": "Test slice: up to two files under docs/.",
    "required_proof_level": "domain-tested",
}

POLICY: dict[str, Any] = {
    "consecutive_autonomous_limit": 1,
    "handoff_timeout_seconds": 1800,
    "agent_command_template": "automation/supervisor/run_agent.sh --repo-root {repo_root} --prompt-file {prompt_file} "
    "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}",
    "supervisor_owned_paths": [QUEUE, "automation/handoffs/"],
}

SUPERVISOR_DIRS = ("automation/supervisor", "automation/schemas", "automation/context")
EVALUATE_HANDOFF = "automation/supervisor/evaluate_handoff.py"


def isolated_env(**extra: str) -> dict[str, str]:
    """The caller's environment without its git state, global git config, telemetry settings or
    supervised-slice markers, plus *extra*."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(SCRUBBED_ENV_PREFIXES)}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.update(extra)
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=isolated_env(), check=True, capture_output=True, text=True).stdout.strip()


def queue_text(**slice_overrides: Any) -> str:
    return json.dumps({"version": 1, "policy": POLICY, "slices": [{**SLICE, **slice_overrides}]}, indent=2) + "\n"


class Repo:
    """A repository whose ``root`` commit holds the supervisor but no queue, whose ``base`` queues
    the slice, and whose other commits are scripted work. The working tree stays at ``base``."""

    def __init__(self, path: Path, **slice_overrides: Any) -> None:
        self.path = path
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        for relative in SUPERVISOR_DIRS:
            shutil.copytree(SOURCE_ROOT / relative, path / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.root = self._commit({"README.md": "# Project\n", "docs/guide.md": "# Guide\n"})
        self.base = self._commit({QUEUE: queue_text(**slice_overrides)})

    def _commit(self, files: dict[str, Optional[str]]) -> str:
        for name, text in files.items():
            target = self.path / name
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", ", ".join(files))
        return git(self.path, "rev-parse", "HEAD")

    def commits(self, *changes: tuple[dict[str, Optional[str]], Optional[str]]) -> list[str]:
        """One commit per (files, parent) pair, on *parent* (default ``base``), that writes, or with
        None deletes, *files*. The working tree returns to ``base``."""
        shas = []
        for files, parent in changes:
            git(self.path, "checkout", "-q", "--detach", parent or self.base)
            shas.append(self._commit(files))
        git(self.path, "checkout", "-q", "--detach", self.base)
        return shas

    def queue_status(self) -> str:
        return json.loads((self.path / QUEUE).read_text(encoding="utf-8"))["slices"][0]["status"]


def handoff(verified_sha: str, **overrides: Any) -> dict[str, Any]:
    """The example handoff, for one in-scope file that passed its validation."""
    document = json.loads((SOURCE_ROOT / "automation/examples/example-handoff.json").read_text(encoding="utf-8"))
    document.update(
        slice_id=SLICE_ID,
        files_touched=["docs/a.md"],
        validations_passed=["git diff --check"],
        validations_failed=[],
        proof_level="domain-tested",
        verified_commit_sha=verified_sha,
    )
    document.update(overrides)
    return document


def handoff_text(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


@dataclass
class Result:
    """What one CLI run printed and returned, and the slice and commits it was asked about."""

    code: int
    report: Optional[dict[str, Any]]
    stdout: str
    stderr: str
    slice_id: str
    base: str
    head: str


def run(
    repo: Repo,
    handoff_path: Path,
    document: dict[str, Any],
    head: str,
    *,
    base: Optional[str] = None,
    slice_id: str = SLICE_ID,
    queue: str = QUEUE,
    dry_run: bool = True,
    env: Optional[dict[str, str]] = None,
    flags: tuple[str, ...] = (),
) -> Result:
    """Write *document* to *handoff_path* and run the CLI on it, with *flags* added."""
    base = base or repo.base
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_text(document), encoding="utf-8")
    argv = ["--slice-id", slice_id, "--handoff", str(handoff_path), "--base-sha", base, "--head-sha", head]
    argv += ["--queue", queue, "--no-telemetry", *flags] + (["--dry-run"] if dry_run else [])
    completed = subprocess.run(
        [sys.executable, str(repo.path / EVALUATE_HANDOFF), *argv],
        cwd=repo.path,  # manual proofs are looked up under the working directory
        env=env or isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = None
    if completed.stdout.strip():
        try:
            report = json.loads(completed.stdout)
        except ValueError:
            raise AssertionError(
                f"exit {completed.returncode} without a JSON report\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            ) from None
    return Result(completed.returncode, report, completed.stdout, completed.stderr, slice_id, base, head)


def checked_report(result: Result) -> dict[str, Any]:
    """The report, which names the slice and commits it was asked about."""
    report = result.report
    assert report is not None, result.stderr
    assert (report["slice_id"], report["base_sha"], report["head_sha"]) == (result.slice_id, result.base, result.head)
    return report


def assert_rejected(result: Result, reason: str, *, missing_proof_level: bool = False) -> dict[str, Any]:
    report = checked_report(result)
    assert (result.code, report["decision"], report["queue_status"]) == (1, "reject", "failed"), report["reason"]
    assert reason in report["reason"]
    assert report["missing_proof_level"] is missing_proof_level
    return report


def assert_accepted(result: Result) -> dict[str, Any]:
    report = checked_report(result)
    assert (result.code, report["decision"], report["queue_status"]) == (0, "accept", "done"), report["reason"]
    assert (report["out_of_scope_files"], report["files_touched_outside_scope"]) == ([], [])
    assert (report["required_validation_failures"], report["missing_manual_proofs"]) == ([], [])
    assert report["missing_proof_level"] is False
    return report


@pytest.fixture(scope="class")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("evaluate-handoff") / "repo")


@pytest.fixture(scope="class")
def heads(repo: Repo) -> dict[str, str]:
    """The scripted work commits, each the smallest change for one gate."""
    [one] = repo.commits(({"docs/a.md": "a\n"}, None))
    committed_handoff = handoff_text(handoff(one))  # what the handoff-commit tests evaluate
    changes: dict[str, tuple[dict[str, Optional[str]], Optional[str]]] = {
        "two": ({"docs/a.md": "a\n", "docs/b.md": "b\n"}, None),
        "three": ({"docs/a.md": "a\n", "docs/b.md": "b\n", "docs/c.md": "c\n"}, None),
        "readme": ({"README.md": "# Project, changed\n"}, None),
        "sibling": ({"docs-private/x.md": "x\n"}, None),
        "whitespace": ({"docs/a.md": "a \n"}, None),
        "drift": ({QUEUE: queue_text(max_files_changed=3)}, None),
        "widened": ({QUEUE: queue_text(allowed_paths=["docs/", "README.md"]), "README.md": "# Project, changed\n"}, None),
        "no-slice": ({QUEUE: None}, None),
        "broken-queue": ({QUEUE: "{not json\n"}, None),
        "handoff-only": ({HANDOFF_IN_REPO: committed_handoff}, one),
        "handoff-and-work": ({HANDOFF_IN_REPO: committed_handoff, "docs/a.md": "changed after verifying\n"}, one),
        # The run's handoff stays uncommitted in automation/handoffs/; another file lands beside it.
        "stray": ({"docs/a.md": "a\n", "automation/handoffs/S-2.json": "{}\n"}, None),
    }
    return {"one": one, **dict(zip(changes, repo.commits(*changes.values())))}


@pytest.fixture
def in_repo_handoff(repo: Repo):
    """The run's handoff at automation/handoffs/S-1.json in the working tree, removed afterwards."""
    path = repo.path / HANDOFF_IN_REPO
    yield path
    path.unlink(missing_ok=True)


class TestGates:
    # The input gates, checked before any git work.

    def test_a_slice_missing_from_the_queue_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["one"], slice_id="S-9"), heads["one"], slice_id="S-9")
        assert_rejected(result, "not found in queue")

    def test_a_slice_that_is_not_queued_is_rejected(self, repo, heads, tmp_path):
        done_queue = repo.path / "automation/queue/done.json"
        done_queue.write_text(queue_text(status="done"), encoding="utf-8")
        try:
            result = run(repo, tmp_path / "h.json", handoff(heads["one"]), heads["one"], queue="automation/queue/done.json")
        finally:
            done_queue.unlink()
        assert_rejected(result, "expected 'queued'")

    def test_a_handoff_for_another_slice_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["one"], slice_id="S-2"), heads["one"])
        assert_rejected(result, "does not match --slice-id")

    def test_a_handoff_without_a_verified_commit_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["one"])
        del document["verified_commit_sha"]
        assert_rejected(run(repo, tmp_path / "h.json", document, heads["one"]), "does not declare verified_commit_sha")

    def test_a_handoff_from_a_dirty_repository_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["one"], repo_clean_status="dirty"), heads["one"])
        assert_rejected(result, "repo_clean_status is 'dirty'")

    def test_a_handoff_declaring_dirty_paths_outside_scope_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["one"], dirty_paths_outside_scope=["README.md"])
        assert_rejected(run(repo, tmp_path / "h.json", document, heads["one"]), "dirty paths outside slice scope")

    # The commits.

    def test_a_base_sha_missing_from_the_repository_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["one"]), heads["one"], base=MISSING_SHA)
        assert_rejected(result, f"Base SHA '{MISSING_SHA}' does not exist")

    def test_a_head_sha_missing_from_the_repository_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(MISSING_SHA), MISSING_SHA)
        assert_rejected(result, f"Head SHA '{MISSING_SHA}' does not exist")

    def test_a_verified_commit_other_than_head_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(repo.base), heads["one"])
        assert_rejected(result, "does not match --head-sha")

    def test_the_commit_before_a_handoff_only_head_is_accepted_as_verified(self, repo, heads, in_repo_handoff):
        result = run(repo, in_repo_handoff, handoff(heads["one"]), heads["handoff-only"])
        assert assert_accepted(result)["changed_files"] == [HANDOFF_IN_REPO, "docs/a.md"]

    def test_the_commit_before_a_head_that_also_changes_work_is_rejected(self, repo, heads, in_repo_handoff):
        result = run(repo, in_repo_handoff, handoff(heads["one"]), heads["handoff-and-work"])
        assert_rejected(result, "does not match --head-sha")

    # The contract frozen at base.

    def test_a_base_without_the_slice_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["one"]), heads["one"], base=repo.root)
        assert_rejected(result, "has no contract at base SHA")

    def test_a_head_without_the_slice_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["no-slice"]), heads["no-slice"])
        assert_rejected(result, "absent from the queue at head SHA")

    def test_a_head_whose_queue_is_not_json_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["broken-queue"]), heads["broken-queue"])
        assert_rejected(result, "absent from the queue at head SHA")

    def test_a_head_that_changes_the_slice_contract_is_rejected(self, repo, heads, tmp_path):
        result = run(repo, tmp_path / "h.json", handoff(heads["drift"]), heads["drift"])
        assert_rejected(result, "changed its own enforcement contract between base and head: max_files_changed")

    def test_a_head_that_widens_its_own_allowed_paths_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["widened"], files_touched=["README.md"])
        result = run(repo, tmp_path / "h.json", document, heads["widened"])
        assert_rejected(result, "changed its own enforcement contract between base and head: allowed_paths")

    # Scope.

    def test_a_diff_outside_allowed_paths_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["readme"], files_touched=["README.md"])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, heads["readme"]), "outside allowed_paths")
        assert (report["out_of_scope_files"], report["changed_files"]) == (["README.md"], ["README.md"])

    def test_an_allowed_directory_does_not_admit_a_sibling_with_its_name_as_prefix(self, repo, heads, tmp_path):
        document = handoff(heads["sibling"], files_touched=["docs-private/x.md"])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, heads["sibling"]), "outside allowed_paths")
        assert report["out_of_scope_files"] == ["docs-private/x.md"]

    def test_another_file_beside_the_handoff_in_a_supervisor_owned_directory_is_rejected(self, repo, heads, in_repo_handoff):
        report = assert_rejected(run(repo, in_repo_handoff, handoff(heads["stray"]), heads["stray"]), "outside allowed_paths")
        assert report["out_of_scope_files"] == ["automation/handoffs/S-2.json"]

    # The file budget, on all-in-scope diffs so the scope gate can't decide first.

    def test_a_diff_at_max_files_changed_is_accepted(self, repo, heads, tmp_path):
        document = handoff(heads["two"], files_touched=["docs/a.md", "docs/b.md"])
        assert assert_accepted(run(repo, tmp_path / "h.json", document, heads["two"]))["changed_file_count"] == 2

    def test_a_diff_over_max_files_changed_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["three"], files_touched=["docs/a.md", "docs/b.md", "docs/c.md"])
        assert_rejected(run(repo, tmp_path / "h.json", document, heads["three"]), "(3) exceeds max_files_changed (2)")

    def test_a_handoff_claiming_more_files_than_the_budget_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["one"], files_touched=["docs/a.md", "docs/b.md", "docs/c.md"])
        assert_rejected(run(repo, tmp_path / "h.json", document, heads["one"]), "(3) exceeds max_files_changed (2)")

    # What the handoff reports.

    def test_a_handoff_reporting_a_file_outside_allowed_paths_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["one"], files_touched=["docs/a.md", "README.md"])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, heads["one"]), "files_touched contains paths")
        assert report["files_touched_outside_scope"] == ["README.md"]

    def test_a_required_validation_the_handoff_did_not_pass_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["one"], validations_passed=[])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, heads["one"]), "missing or failed")
        assert report["required_validation_failures"] == ["git diff --check"]

    def test_a_validation_the_supervisor_replays_and_fails_is_rejected(self, repo, heads, tmp_path):
        report = assert_rejected(run(repo, tmp_path / "h.json", handoff(heads["whitespace"]), heads["whitespace"]), "replay")
        assert report["required_validation_failures"] == ["git diff --check"]
        assert [replay["success"] for replay in report["supervisor_validation_replays"]] == [False]

    def test_a_proof_level_below_the_required_one_is_rejected(self, repo, heads, tmp_path):
        document = handoff(heads["one"], proof_level="doc-only")
        assert_rejected(run(repo, tmp_path / "h.json", document, heads["one"]), "proof level was", missing_proof_level=True)

    # Done.

    def test_a_handoff_that_passes_every_gate_is_done_and_a_dry_run_leaves_the_queue(self, repo, heads, tmp_path):
        report = assert_accepted(run(repo, tmp_path / "h.json", handoff(heads["one"]), heads["one"]))
        assert (report["changed_files"], report["dry_run"]) == (["docs/a.md"], True)
        assert [replay["success"] for replay in report["supervisor_validation_replays"]] == [True]
        assert repo.queue_status() == "queued"

    def test_a_run_inside_a_supervised_slice_is_refused_without_dry_run(self, repo, heads, tmp_path):
        env = isolated_env(BOUNDROOM_SUPERVISOR_SLICE_ID="S-0")
        result = run(repo, tmp_path / "h.json", handoff(heads["one"]), heads["one"], dry_run=False, env=env)
        assert (result.code, result.report) == (78, None)
        assert "refused inside a supervised slice" in result.stderr
        assert repo.queue_status() == "queued"


class TestExitCodes:
    """One run per exit code without --dry-run, each in a repository of its own, so the queue it
    leaves behind is its own doing: 0 marks the slice done, and 1 and 78 leave the queue as it was."""

    def test_a_done_handoff_marks_the_slice_done_in_the_queue(self, tmp_path):
        repo = Repo(tmp_path / "repo")
        [head] = repo.commits(({"docs/a.md": "a\n"}, None))
        result = run(repo, tmp_path / "h.json", handoff(head), head, dry_run=False)
        report = assert_accepted(result)
        assert report["dry_run"] is False
        assert result.stdout == json.dumps(report, indent=2) + "\n"  # the report and nothing else
        assert json.loads((repo.path / QUEUE).read_text(encoding="utf-8")) == json.loads(queue_text(status="done"))

    def test_a_rejected_handoff_leaves_the_queue_as_it_was(self, tmp_path):
        repo = Repo(tmp_path / "repo")
        [head] = repo.commits(({"README.md": "changed\n"}, None))
        before = (repo.path / QUEUE).read_bytes()
        document = handoff(head, files_touched=["README.md"])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, head, dry_run=False), "outside allowed_paths")
        assert report["dry_run"] is False
        assert (repo.path / QUEUE).read_bytes() == before

    def test_telemetry_required_but_turned_off_is_refused_before_any_evaluation(self, tmp_path):
        repo = Repo(tmp_path / "repo")
        [head] = repo.commits(({"docs/a.md": "a\n"}, None))
        before = (repo.path / QUEUE).read_bytes()
        result = run(repo, tmp_path / "h.json", handoff(head), head, dry_run=False, flags=("--telemetry-required",))
        assert (result.code, result.report) == (78, None)
        assert re.search(r"^stop: telemetry: .*required", result.stderr, re.MULTILINE), result.stderr
        assert (repo.path / QUEUE).read_bytes() == before


REQUIRED_FLAGS = ("--slice-id", "--handoff", "--base-sha", "--head-sha")


@pytest.mark.parametrize("missing", REQUIRED_FLAGS)
def test_each_required_flag_is_required(missing, capsys):
    argv = [part for flag in REQUIRED_FLAGS if flag != missing for part in (flag, "x")]
    with pytest.raises(SystemExit) as raised:
        evaluate_handoff.build_parser().parse_args(argv)
    assert raised.value.code == 2  # argparse's usage error, before any evaluation
    assert missing in capsys.readouterr().err


def test_the_queue_defaults_to_the_supervisor_queue_and_dry_run_is_off():
    args = evaluate_handoff.build_parser().parse_args([part for flag in REQUIRED_FLAGS for part in (flag, "x")])
    assert (args.queue, args.dry_run) == (QUEUE, False)


class TestQueueOutsideTheRepository:
    """The queue's history is read from the repository, so a queue outside it has none. That
    only matters once the gates reach the contract frozen at base."""

    @pytest.fixture
    def outside(self, tmp_path: Path) -> tuple[Repo, Path, str]:
        repo = Repo(tmp_path / "repo")
        [head] = repo.commits(({"docs/a.md": "a\n"}, None))
        queue = tmp_path / "queue.json"
        queue.write_text(queue_text(), encoding="utf-8")
        return repo, queue, head

    def evaluate(self, repo: Repo, queue: Path, head: str, document: dict[str, Any]) -> evaluate_handoff.EvaluationReport:
        handoff_path = repo.path.parent / "h.json"
        handoff_path.write_text(handoff_text(document), encoding="utf-8")
        return evaluate_handoff.evaluate(repo.path, queue, handoff_path, SLICE_ID, repo.base, head, dry_run=True)

    def test_a_handoff_rejected_by_an_input_gate_is_reported(self, outside):
        repo, queue, head = outside
        report = self.evaluate(repo, queue, head, handoff(head, slice_id="S-2"))
        assert (report.decision, report.reason) == ("reject", "Handoff slice_id 'S-2' does not match --slice-id 'S-1'.")

    def test_a_handoff_that_reaches_the_contract_raises(self, outside, monkeypatch):
        for key in os.environ:
            if key.startswith(SCRUBBED_ENV_PREFIXES):
                monkeypatch.delenv(key)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        repo, queue, head = outside
        with pytest.raises(ValueError):
            self.evaluate(repo, queue, head, handoff(head))


PROOF = "automation/proofs/S-1/screenshot.txt"
PROOF_BYTES = b"what the screen showed\n"


@pytest.fixture(scope="class")
def proven(tmp_path_factory: pytest.TempPathFactory) -> tuple[Repo, str]:
    """A slice that requires a screenshot proof, its work, and the proof file (untracked)."""
    repo = Repo(tmp_path_factory.mktemp("evaluate-handoff-proof") / "repo", manual_proof=["screenshot"])
    [head] = repo.commits(({"docs/a.md": "a\n"}, None))
    (repo.path / PROOF).parent.mkdir(parents=True)
    (repo.path / PROOF).write_bytes(PROOF_BYTES)
    return repo, head


class TestManualProof:
    @staticmethod
    def proof(head: str, sha256: str) -> dict[str, str]:
        return {
            "proof_type": "screenshot",
            "path": PROOF,
            "commit_sha": head,
            "created_at": "2026-11-25T00:00:00Z",
            "slice_id": SLICE_ID,
            "sha256": sha256,
        }

    def test_a_proof_whose_file_matches_its_sha256_is_accepted(self, proven, tmp_path):
        repo, head = proven
        document = handoff(head, provided_proofs=[self.proof(head, hashlib.sha256(PROOF_BYTES).hexdigest())])
        assert_accepted(run(repo, tmp_path / "h.json", document, head))

    def test_a_proof_whose_file_does_not_match_its_sha256_is_rejected(self, proven, tmp_path):
        repo, head = proven
        document = handoff(head, provided_proofs=[self.proof(head, hashlib.sha256(b"something else").hexdigest())])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, head), "manual proof")
        assert report["missing_manual_proofs"] == ["screenshot (sha256 mismatch)"]

    def test_a_proof_recorded_at_another_commit_is_rejected(self, proven, tmp_path):
        repo, head = proven
        document = handoff(head, provided_proofs=[self.proof(repo.base, hashlib.sha256(PROOF_BYTES).hexdigest())])
        report = assert_rejected(run(repo, tmp_path / "h.json", document, head), "manual proof")
        assert report["missing_manual_proofs"] == ["screenshot"]

    def test_a_missing_required_proof_is_rejected(self, proven, tmp_path):
        repo, head = proven
        report = assert_rejected(run(repo, tmp_path / "h.json", handoff(head), head), "manual proof")
        assert report["missing_manual_proofs"] == ["screenshot"]


# ---------------------------------------------------------------------------
# The decision core: decide() on facts built in memory, checked by its report
# ---------------------------------------------------------------------------

BASE_SHA, HEAD_SHA, PARENT_SHA = "b" * 40, "c" * 40, "d" * 40
PASSED_REPLAY = ValidationReplayResult(command="git diff --check", success=True, exit_code=0, reason="Supervisor replay passed.")
FAILED_REPLAY = ValidationReplayResult(
    command="git diff --check", success=False, exit_code=2, reason="Supervisor replay exited with code 2."
)
PASSED_REPLAY_DICT = {"command": "git diff --check", "success": True, "exit_code": 0, "reason": "Supervisor replay passed."}
FAILED_REPLAY_DICT = {
    "command": "git diff --check",
    "success": False,
    "exit_code": 2,
    "reason": "Supervisor replay exited with code 2.",
}
Facts = evaluate_handoff.Facts
Stage = evaluate_handoff.Stage
UNKNOWN = evaluate_handoff.UNKNOWN


def facts(*, handoff_fields: Optional[dict[str, Any]] = None, **changes: Any) -> Facts:
    """Facts that pass every gate: one file under docs/ in the diff and the handoff, its validation
    passed and replayed, no proof required. *changes* replaces fields; *handoff_fields* changes the
    handoff."""
    base = Facts(
        slice_id=SLICE_ID,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        dry_run=True,
        queue={"version": 1, "policy": POLICY, "slices": [dict(SLICE)]},
        handoff=handoff(HEAD_SHA, **(handoff_fields or {})),
        base_exists=True,
        head_exists=True,
        handoff_relative=None,
        handoff_commit_parent=None,
        base_slice=dict(SLICE),
        head_slice=dict(SLICE),
        changed_files=["docs/a.md"],
        replays=[PASSED_REPLAY],
        proofs=([], False),
    )
    return replace(base, **changes)


def contract(**fields: Any) -> dict[str, Any]:
    return {**SLICE, **fields}


def decided(f: Facts) -> dict[str, Any]:
    return evaluate_handoff.report_to_dict(evaluate_handoff.decide(f))


NO_DETAILS = {
    "changed_files": [],
    "changed_file_count": 0,
    "out_of_scope_files": [],
    "files_touched_outside_scope": [],
    "required_validation_failures": [],
    "supervisor_validation_replays": [],
    "missing_manual_proofs": [],
    "missing_proof_level": False,
}


def rejected(f: Facts, reason: str, **details: Any) -> None:
    """decide() rejects *f* with exactly *reason* and *details*, and echoes the request."""
    assert decided(f) == {
        "slice_id": f.slice_id,
        "queue_status": "failed",
        "decision": "reject",
        "reason": reason,
        **NO_DETAILS,
        **details,
        "head_sha": f.head_sha,
        "base_sha": f.base_sha,
        "dry_run": f.dry_run,
    }


def accepted(f: Facts) -> dict[str, Any]:
    report = decided(f)
    assert (report["decision"], report["queue_status"]) == ("accept", "done"), report["reason"]
    return report


class TestDecideAccepts:
    def test_facts_that_pass_every_gate_are_accepted(self):
        assert decided(facts()) == {
            "slice_id": SLICE_ID,
            "queue_status": "done",
            "decision": "accept",
            "reason": "Historical handoff evaluation passed all checks.",
            **NO_DETAILS,
            "changed_files": ["docs/a.md"],
            "changed_file_count": 1,
            "supervisor_validation_replays": [PASSED_REPLAY_DICT],
            "head_sha": HEAD_SHA,
            "base_sha": BASE_SHA,
            "dry_run": True,
        }

    def test_an_accept_says_whether_it_was_a_dry_run(self):
        assert accepted(facts(dry_run=False))["dry_run"] is False

    def test_a_reject_says_whether_it_was_a_dry_run(self):
        rejected(facts(dry_run=False, base_exists=False), f"Base SHA '{BASE_SHA}' does not exist in the repository.")

    def test_an_accept_counts_the_files_the_handoff_names_when_they_are_more(self):
        assert accepted(facts(handoff_fields={"files_touched": ["docs/a.md", "docs/b.md"]}))["changed_file_count"] == 2

    def test_an_accept_counts_the_in_scope_diff_when_it_is_more(self):
        assert accepted(facts(changed_files=["docs/a.md", "docs/b.md"]))["changed_file_count"] == 2

    def test_a_handoff_naming_one_file_twice_counts_it_once(self):
        f = facts(handoff_fields={"files_touched": ["docs/a.md", "docs/a.md", "docs/a.md"]})
        assert accepted(f)["changed_file_count"] == 1

    def test_supervisor_owned_files_are_in_scope_and_outside_the_budget(self):
        report = accepted(facts(changed_files=[QUEUE, "docs/a.md", "docs/b.md"]))
        assert (report["changed_files"], report["changed_file_count"]) == ([QUEUE, "docs/a.md", "docs/b.md"], 2)

    def test_the_handoff_is_in_scope_wherever_it_is(self):
        accepted(facts(handoff_relative="notes/S-1.json", changed_files=["docs/a.md", "notes/S-1.json"]))

    def test_the_commit_before_a_handoff_only_head_may_be_the_verified_one(self):
        accepted(facts(handoff_commit_parent=PARENT_SHA, handoff_fields={"verified_commit_sha": PARENT_SHA}))

    def test_head_stays_a_verified_commit_when_it_changed_only_the_handoff(self):
        accepted(facts(handoff_commit_parent=PARENT_SHA))

    def test_no_replay_is_no_replay_failure(self):
        assert accepted(facts(replays=[]))["supervisor_validation_replays"] == []

    def test_a_handoff_may_name_supervisor_owned_files(self):
        accepted(facts(handoff_fields={"files_touched": ["docs/a.md", QUEUE]}))

    def test_fields_outside_the_contract_may_change_between_base_and_head(self):
        accepted(facts(head_slice=contract(title="retitled", notes="new notes", status="done", priority=1)))


class TestDecideRejects:
    """One test per gate, on facts that pass every other gate. A reject echoes the request and
    carries only the details of the gate that failed."""

    def test_a_slice_missing_from_the_queue(self):
        rejected(facts(slice_id="S-9", handoff_fields={"slice_id": "S-9"}), "Slice 'S-9' not found in queue.")

    @pytest.mark.parametrize("status", ["done", "in_progress", "blocked", "failed"])
    def test_a_slice_that_is_not_queued(self, status):
        f = facts(queue={"version": 1, "policy": POLICY, "slices": [contract(status=status)]})
        rejected(f, f"Slice status is '{status}', expected 'queued'.")

    def test_a_handoff_for_another_slice(self):
        rejected(facts(handoff_fields={"slice_id": "S-2"}), "Handoff slice_id 'S-2' does not match --slice-id 'S-1'.")

    def test_a_handoff_without_a_verified_commit(self):
        f = facts()
        document = {key: value for key, value in f.handoff.items() if key != "verified_commit_sha"}
        rejected(replace(f, handoff=document), "Handoff does not declare verified_commit_sha.")
        rejected(facts(handoff_fields={"verified_commit_sha": ""}), "Handoff does not declare verified_commit_sha.")

    def test_a_handoff_from_a_repository_that_was_not_clean(self):
        rejected(facts(handoff_fields={"repo_clean_status": "dirty"}), "Handoff repo_clean_status is 'dirty', expected 'clean'.")

    def test_a_handoff_declaring_dirty_paths_outside_its_scope(self):
        f = facts(handoff_fields={"dirty_paths_outside_scope": ["README.md", "x"]})
        rejected(f, "Handoff declares dirty paths outside slice scope: README.md, x.")

    def test_a_base_commit_that_does_not_exist(self):
        rejected(facts(base_exists=False), f"Base SHA '{BASE_SHA}' does not exist in the repository.")

    def test_a_head_commit_that_does_not_exist(self):
        rejected(facts(head_exists=False), f"Head SHA '{HEAD_SHA}' does not exist in the repository.")

    def test_a_verified_commit_other_than_head_or_the_parent_of_a_handoff_only_head(self):
        f = facts(handoff_commit_parent=PARENT_SHA, handoff_fields={"verified_commit_sha": BASE_SHA})
        rejected(
            f,
            f"Handoff verified_commit_sha '{BASE_SHA}' does not match --head-sha '{HEAD_SHA}', "
            "or the commit before it when --head-sha changes only the handoff.",
        )

    def test_the_parent_of_head_is_not_verified_unless_head_changed_only_the_handoff(self):
        rejected(
            facts(handoff_fields={"verified_commit_sha": PARENT_SHA}),
            f"Handoff verified_commit_sha '{PARENT_SHA}' does not match --head-sha '{HEAD_SHA}', "
            "or the commit before it when --head-sha changes only the handoff.",
        )

    def test_a_base_without_the_slice(self):
        rejected(
            facts(base_slice=None),
            f"Slice 'S-1' has no contract at base SHA '{BASE_SHA}'; there is nothing to evaluate the work against.",
        )

    def test_a_head_without_the_slice(self):
        rejected(facts(head_slice=None), f"Slice 'S-1' is absent from the queue at head SHA '{HEAD_SHA}'.")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("allowed_paths", ["docs/", "README.md"]),
            ("required_validations", []),
            ("max_files_changed", 3),
            ("manual_proof", ["screenshot"]),
            ("required_proof_level", "doc-only"),
            ("depends_on", ["S-0"]),
        ],
    )
    def test_a_head_that_changes_a_frozen_contract_field(self, field, value):
        rejected(
            facts(head_slice=contract(**{field: value})),
            f"Candidate changed its own enforcement contract between base and head: {field}.",
        )

    def test_a_drift_names_every_changed_field_in_contract_order(self):
        f = facts(head_slice=contract(depends_on=["S-0"], allowed_paths=["x/"]))
        rejected(f, "Candidate changed its own enforcement contract between base and head: allowed_paths, depends_on.")

    def test_the_contract_at_base_governs_not_the_queue_as_loaded(self):
        # The queue as loaded allows README.md; the contract at base and head does not.
        queue = {"version": 1, "policy": POLICY, "slices": [contract(allowed_paths=["docs/", "README.md"])]}
        f = facts(queue=queue, changed_files=["README.md"], handoff_fields={"files_touched": ["README.md"]})
        rejected(
            f,
            "Historical diff contains files outside allowed_paths.",
            changed_files=["README.md"],
            changed_file_count=1,
            out_of_scope_files=["README.md"],
        )

    def test_the_contract_at_base_sets_the_budget(self):
        f = facts(base_slice=contract(max_files_changed=1), head_slice=contract(max_files_changed=1))
        rejected(
            replace(f, changed_files=["docs/a.md", "docs/b.md"]),
            "Changed file count (2) exceeds max_files_changed (1).",
            changed_files=["docs/a.md", "docs/b.md"],
            changed_file_count=2,
        )

    def test_the_contract_at_base_names_the_required_validations(self):
        f = facts(base_slice=contract(required_validations=["make architecture"]))
        f = replace(f, head_slice=f.base_slice)
        rejected(
            f,
            "Required validations were missing or failed.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            required_validation_failures=["make architecture"],
        )

    def test_the_queue_as_loaded_names_the_supervisor_owned_paths(self):
        owned = {**POLICY, "supervisor_owned_paths": ["notes/"]}
        f = facts(queue={"version": 1, "policy": owned, "slices": [dict(SLICE)]}, changed_files=["docs/a.md", "notes/x.md"])
        accepted(f)

    def test_a_diff_outside_allowed_paths(self):
        rejected(
            facts(changed_files=["README.md", "docs/a.md"]),
            "Historical diff contains files outside allowed_paths.",
            changed_files=["README.md", "docs/a.md"],
            changed_file_count=2,
            out_of_scope_files=["README.md"],
        )

    def test_an_allowed_directory_does_not_admit_a_sibling_with_its_name_as_prefix(self):
        rejected(
            facts(changed_files=["docs-private/x.md"]),
            "Historical diff contains files outside allowed_paths.",
            changed_files=["docs-private/x.md"],
            changed_file_count=1,
            out_of_scope_files=["docs-private/x.md"],
        )

    def test_another_file_beside_the_handoff_in_a_supervisor_owned_directory(self):
        changed = [HANDOFF_IN_REPO, "automation/handoffs/S-2.json", "docs/a.md"]
        rejected(
            facts(handoff_relative=HANDOFF_IN_REPO, changed_files=changed),
            "Historical diff contains files outside allowed_paths.",
            changed_files=changed,
            changed_file_count=3,
            out_of_scope_files=["automation/handoffs/S-2.json"],
        )

    def test_out_of_scope_and_stray_files_are_reported_together_once_each_and_sorted(self):
        changed = ["automation/handoffs/S-2.json", "README.md", "README.md"]
        rejected(
            facts(handoff_relative=HANDOFF_IN_REPO, changed_files=changed),
            "Historical diff contains files outside allowed_paths.",
            changed_files=changed,
            changed_file_count=3,
            out_of_scope_files=["README.md", "automation/handoffs/S-2.json"],
        )

    def test_a_diff_one_file_over_the_budget(self):
        changed = ["docs/a.md", "docs/b.md", "docs/c.md"]
        rejected(
            facts(changed_files=changed),
            "Changed file count (3) exceeds max_files_changed (2).",
            changed_files=changed,
            changed_file_count=3,
        )

    def test_a_diff_at_the_budget_passes_it(self):
        accepted(facts(changed_files=["docs/a.md", "docs/b.md"]))

    def test_a_handoff_naming_more_files_than_the_budget(self):
        # A reject counts the diff in changed_file_count, not the budget's count.
        rejected(
            facts(handoff_fields={"files_touched": ["docs/a.md", "docs/b.md", "docs/c.md"]}),
            "Changed file count (3) exceeds max_files_changed (2).",
            changed_files=["docs/a.md"],
            changed_file_count=1,
        )

    def test_a_zero_budget_rejects_one_file(self):
        f = facts(base_slice=contract(max_files_changed=0), head_slice=contract(max_files_changed=0))
        rejected(
            replace(f, handoff={**f.handoff, "files_touched": []}),
            "Changed file count (1) exceeds max_files_changed (0).",
            changed_files=["docs/a.md"],
            changed_file_count=1,
        )

    def test_a_handoff_reporting_a_file_outside_allowed_paths(self):
        rejected(
            facts(handoff_fields={"files_touched": ["docs/a.md", " README.md "]}),
            "Handoff files_touched contains paths outside allowed_paths.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            files_touched_outside_scope=["README.md"],
        )

    def test_a_required_validation_the_handoff_did_not_pass(self):
        rejected(
            facts(handoff_fields={"validations_passed": []}),
            "Required validations were missing or failed.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            required_validation_failures=["git diff --check"],
        )

    def test_a_required_validation_the_handoff_reports_failed(self):
        rejected(
            facts(handoff_fields={"validations_failed": ["git diff --check"]}),
            "Required validations were missing or failed.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            required_validation_failures=["git diff --check"],
        )

    def test_a_validation_the_supervisor_replays_and_fails(self):
        rejected(
            facts(replays=[PASSED_REPLAY, FAILED_REPLAY]),
            "Supervisor validation replay failed at verified SHA.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            required_validation_failures=["git diff --check"],
            supervisor_validation_replays=[PASSED_REPLAY_DICT, FAILED_REPLAY_DICT],
        )

    def test_a_missing_manual_proof(self):
        rejected(
            facts(proofs=(["screenshot"], False)),
            "Required manual proof or proof level was missing.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            missing_manual_proofs=["screenshot"],
            supervisor_validation_replays=[PASSED_REPLAY_DICT],
        )

    def test_a_proof_level_below_the_required_one(self):
        rejected(
            facts(proofs=([], True)),
            "Required manual proof or proof level was missing.",
            changed_files=["docs/a.md"],
            changed_file_count=1,
            missing_proof_level=True,
            supervisor_validation_replays=[PASSED_REPLAY_DICT],
        )


def _handoff(**fields: Any) -> Callable[[Facts], Facts]:
    return lambda f: replace(f, handoff={**f.handoff, **fields})


def _more_changed(*paths: str) -> Callable[[Facts], Facts]:
    return lambda f: replace(f, changed_files=[*cast(list[str], f.changed_files), *paths])


def _set(**fields: Any) -> Callable[[Facts], Facts]:
    return lambda f: replace(f, **fields)


# Each gate in order, the smallest change to passing facts that fails it, and the start of its reason.
GATES: list[tuple[str, Callable[[Facts], Facts], str]] = [
    ("slice", _set(slice_id="S-9"), "Slice 'S-9' not found"),
    ("status", lambda f: replace(f, queue={**f.queue, "slices": [contract(status="done")]}), "Slice status is"),
    ("handoff slice", _handoff(slice_id="S-2"), "Handoff slice_id"),
    ("verified declared", _handoff(verified_commit_sha=""), "Handoff does not declare"),
    ("clean", _handoff(repo_clean_status="dirty"), "Handoff repo_clean_status"),
    ("dirty paths", _handoff(dirty_paths_outside_scope=["README.md"]), "Handoff declares dirty paths"),
    ("base exists", _set(base_exists=False), "Base SHA"),
    ("head exists", _set(head_exists=False), "Head SHA"),
    ("verified commit", _handoff(verified_commit_sha=BASE_SHA), "Handoff verified_commit_sha"),
    ("base contract", _set(base_slice=None), "Slice 'S-1' has no contract"),
    ("head contract", _set(head_slice=None), "Slice 'S-1' is absent"),
    ("drift", _set(base_slice=contract(max_files_changed=3)), "Candidate changed"),
    ("scope", _more_changed("README.md"), "Historical diff"),
    ("budget", _more_changed("docs/b.md", "docs/c.md"), "Changed file count"),
    ("files touched", _handoff(files_touched=["docs/a.md", "README.md"]), "Handoff files_touched"),
    ("validations", _handoff(validations_passed=[]), "Required validations"),
    ("replay", _set(replays=[FAILED_REPLAY]), "Supervisor validation replay"),
    ("proofs", _set(proofs=(["screenshot"], True)), "Required manual proof"),
]
ADJACENT = list(zip(GATES, GATES[1:]))


class TestDecideGateOrder:
    """The gates run in a fixed order: when a gate and the next would both fail, the first decides."""

    @pytest.mark.parametrize(("name", "trip", "reason"), GATES, ids=[gate[0] for gate in GATES])
    def test_each_change_fails_its_own_gate(self, name, trip, reason):
        report = decided(trip(facts()))
        assert (report["decision"], report["reason"].startswith(reason)) == ("reject", True), report["reason"]

    @pytest.mark.parametrize(("earlier", "later"), ADJACENT, ids=[f"{a[0]} before {b[0]}" for a, b in ADJACENT])
    def test_a_gate_decides_before_the_next(self, earlier, later):
        report = decided(earlier[1](later[1](facts())))
        assert report["reason"].startswith(earlier[2]), report["reason"]


ALL_UNKNOWN: dict[str, Any] = {stage.value: UNKNOWN for stage in Stage}


class TestNextStage:
    """The shell gathers a fact only once every gate before it has passed, so the gates name the
    facts they need, in order, and a rejection needs nothing after it."""

    def test_each_stage_names_a_fact_that_starts_unknown(self):
        assert {stage.value for stage in Stage} <= {field.name for field in fields(Facts)}
        bare = Facts(SLICE_ID, BASE_SHA, HEAD_SHA, True, {}, {}, None)
        assert all(getattr(bare, stage.value) is UNKNOWN for stage in Stage)

    def test_the_facts_are_needed_one_stage_at_a_time_in_gate_order(self):
        f, passing, needed = facts(**ALL_UNKNOWN), facts(), []
        while (stage := evaluate_handoff.next_stage(f)) is not None:
            needed.append(stage)
            with pytest.raises(evaluate_handoff.IncompleteFacts, match=f"needs the {stage.value} fact") as raised:
                evaluate_handoff.decide(f)
            assert raised.value.stage is stage
            f = replace(f, **{stage.value: getattr(passing, stage.value)})
        assert needed == list(Stage)
        accepted(f)

    @pytest.mark.parametrize(("name", "trip", "reason"), GATES, ids=[gate[0] for gate in GATES])
    def test_a_rejection_needs_no_fact_gathered_after_its_gate(self, name, trip, reason):
        # Gather nothing past the stages up to and including the gate's own.
        tripped = trip(facts())
        stages = list(Stage)
        own = {
            "base exists": Stage.BASE_EXISTS,
            "head exists": Stage.HEAD_EXISTS,
            "verified commit": Stage.HANDOFF_COMMIT_PARENT,
            "base contract": Stage.BASE_SLICE,
            "head contract": Stage.HEAD_SLICE,
            "drift": Stage.HEAD_SLICE,
            "scope": Stage.CHANGED_FILES,
            "budget": Stage.CHANGED_FILES,
            "files touched": Stage.CHANGED_FILES,
            "validations": Stage.CHANGED_FILES,
            "replay": Stage.REPLAYS,
            "proofs": Stage.PROOFS,
        }.get(name)
        later = stages[stages.index(own) + 1 :] if own else stages
        f = replace(tripped, **{stage.value: UNKNOWN for stage in later})
        assert evaluate_handoff.next_stage(f) is None
        assert decided(f)["reason"].startswith(reason)
