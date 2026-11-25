"""Every rejection gate in evaluate_handoff.py decides something a test can see.

Each test builds the smallest diff or handoff that trips one gate, runs the CLI in a subprocess,
and checks its JSON report and exit code (0 done, 1 otherwise, 78 a refusal;
docs/reference/exit-codes.md). The gate tests use ``--dry-run``; the queue write and the
nested-run refusal are tested without it, the queue write in a repository of its own. Git and the
queue file are real: a temporary repository with a one-slice queue at a base commit and scripted
work commits after it, so nothing in the supervisor is replaced.

The temporary repository gets a copy of this checkout's ``automation/supervisor``,
``automation/schemas`` and ``automation/context``, because ``evaluate_handoff.py`` evaluates the
repository it sits in. That is also how a consumer runs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

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
) -> Result:
    """Write *document* to *handoff_path* and run the CLI on it."""
    base = base or repo.base
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_text(document), encoding="utf-8")
    argv = ["--slice-id", slice_id, "--handoff", str(handoff_path), "--base-sha", base, "--head-sha", head]
    argv += ["--queue", queue, "--no-telemetry"] + (["--dry-run"] if dry_run else [])
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
    return Result(completed.returncode, report, completed.stderr, slice_id, base, head)


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


def test_a_done_handoff_marks_the_slice_done_in_the_queue(tmp_path):
    repo = Repo(tmp_path / "repo")
    [head] = repo.commits(({"docs/a.md": "a\n"}, None))
    report = assert_accepted(run(repo, tmp_path / "h.json", handoff(head), head, dry_run=False))
    assert report["dry_run"] is False
    assert repo.queue_status() == "done"


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
