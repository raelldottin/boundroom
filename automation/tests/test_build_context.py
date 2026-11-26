"""The context bundle ``build_context.py`` hands a runner, checked output by output.

Every test compares a whole value: the bundle, a section of it, a rendered summary or the
command's output. The pure pieces take hand-built queue documents. The bundle and the command
run against a real temporary Git repository; the clock is injected, so the bundle is exact.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from automation.context import build_context as bc
from automation.context import memory
from automation.supervisor import policy
from automation.tests.hindsight_fake import closed_port

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
POLICY_SENTENCE = (
    "A run may recommend the next slice, but only the supervisor may continue, "
    "and only into a pre-classified adjacent slice that passes scope, validation, "
    "and diff-budget gates."
)
CORE_SPECS = [
    ("AGENTS.md", "repo operating contract"),
    ("automation/README.md", "automation harness loop"),
    ("Gymphant/Docs/ARCHITECTURE.md", "boundary model"),
    ("docs/architecture/boundaries.md", "boundary model"),
    ("docs/workflows/validation.md", "validation workflow"),
    ("docs/workflows/agent-handoff.md", "handoff workflow"),
]


def record(slice_id: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slice_id": slice_id,
        "title": f"Title {slice_id}",
        "status": "queued",
        "priority": 50,
        "domain": "today",
        "allowed_paths": ["src/"],
        "required_validations": ["git diff --check"],
        "depends_on": [],
        "max_files_changed": 5,
        "notes": f"Notes {slice_id}.",
    }
    return {**base, **fields}


def queue(*slices: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 2,
            "handoff_timeout_seconds": 1800,
            "agent_command_template": "agent {slice_id}",
            "supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"],
        },
        "slices": list(slices),
    }


def legacy_handoff(slice_id: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slice_id": slice_id,
        "status": "done",
        "summary": f"Finished {slice_id}.",
        "files_touched": [],
        "validations_passed": [],
        "validations_failed": [],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "timestamp": "2026-01-01T00:00:00Z",
        "risks": [],
    }
    return {**base, **fields}


def write_handoff(handoff_dir: Path, slice_id: str, **fields: Any) -> None:
    handoff_dir.mkdir(parents=True, exist_ok=True)
    (handoff_dir / f"20260101T000000Z-{slice_id}.json").write_text(
        json.dumps(legacy_handoff(slice_id, **fields)), encoding="utf-8"
    )


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the queue section ------------------------------------------------------------------------


def test_queue_metadata_lists_dependencies_and_the_five_nearest_adjacent_queued_slices():
    target = record("T", priority=50, depends_on=["D1", "C", "ghost"])
    data = queue(
        record("D1", status="done", domain="other", priority=1),
        record("G3", priority=70),
        record("A", priority=30),
        target,
        record("C", domain="other", priority=40),  # adjacent only because T depends on it
        record("G1", priority=30),  # ties with A, which comes first in the queue
        record("B", domain="other", priority=10, depends_on=["T"]),  # adjacent only because it depends on T
        record("E", domain="other", priority=0),  # unrelated
        record("F", status="in_progress", priority=0),  # same domain, not queued
        record("G2", priority=60),
    )

    def adjacent(slice_id: str, priority: int, domain: str = "today", depends_on: tuple[str, ...] = ()) -> dict[str, Any]:
        return {
            "slice_id": slice_id,
            "title": f"Title {slice_id}",
            "priority": priority,
            "domain": domain,
            "depends_on": list(depends_on),
            "notes": f"Notes {slice_id}.",
        }

    assert bc.build_queue_metadata(data, target) == {
        "version": 1,
        "policy": {"consecutive_autonomous_limit": 2, "handoff_timeout_seconds": 1800},
        "dependency_status": [
            {"slice_id": "D1", "title": "Title D1", "status": "done"},
            {"slice_id": "C", "title": "Title C", "status": "queued"},
            {"slice_id": "ghost", "title": "", "status": "missing"},
        ],
        "adjacent_queued_slices": [
            adjacent("B", 10, "other", ("T",)),
            adjacent("A", 30),
            adjacent("G1", 30),
            adjacent("C", 40, "other"),
            adjacent("G2", 60),
        ],
    }


def test_validation_ownership_names_each_command_tier():
    assert bc.summarize_validation_ownership(record("T", required_validations=["git diff --check", "make test"])) == [
        {
            "command": "git diff --check",
            "tier": "supervisor_replayable",
            "reason": "The supervisor replays this exact allowlisted command before continuation.",
        },
        {
            "command": "make test",
            "tier": "run_report_only",
            "reason": "This command is currently run-reported only and not replayed by the supervisor.",
        },
    ]


# --- the previous handoff ------------------------------------------------------------------------


def test_the_last_listed_dependency_with_a_handoff_wins_over_the_queue_order(tmp_path):
    for slice_id in ("P", "D1", "D2"):
        write_handoff(tmp_path, slice_id)
    target = record("T", depends_on=["D1", "D2"])
    data = queue(record("D1"), record("D2"), record("P"), target)

    assert bc.find_previous_handoff(data, target, tmp_path) == legacy_handoff("D2")


def test_a_dependency_without_a_handoff_falls_back_to_the_queue_order(tmp_path):
    write_handoff(tmp_path, "P")
    target = record("T", depends_on=["D1"])
    data = queue(record("P"), record("D1"), target)

    assert bc.find_previous_handoff(data, target, tmp_path) == legacy_handoff("P")


def test_the_nearest_earlier_slice_with_a_handoff_is_used_never_the_slice_itself(tmp_path):
    for slice_id in ("P0", "P2", "P1", "T", "L"):
        write_handoff(tmp_path, slice_id)

    two = record("T")
    assert bc.find_previous_handoff(queue(record("P1"), two), two, tmp_path) == legacy_handoff("P1")
    assert bc.find_previous_handoff(queue(record("P2"), record("P1"), two), two, tmp_path) == legacy_handoff("P1")
    assert bc.find_previous_handoff(queue(two, record("L")), two, tmp_path) is None


def test_the_search_reaches_the_first_slice_in_the_queue(tmp_path):
    write_handoff(tmp_path, "P0")
    target = record("T")

    assert bc.find_previous_handoff(queue(record("P0"), record("P1"), target), target, tmp_path) == legacy_handoff("P0")


def test_a_compact_legacy_handoff_fills_the_fields_it_predates():
    handoff = legacy_handoff("P", files_touched=["b.py", "a.py", "b.py"], risks=["r1"], timestamp="T0")

    assert bc.compact_handoff(handoff) == {
        "slice_id": "P",
        "status": "done",
        "summary": "Finished P.",
        "files_touched": ["a.py", "b.py"],
        "validations_passed": [],
        "validations_failed": [],
        "proof_level": "legacy-unknown",
        "missing_proof_levels": [],
        "contract_status_changes": [],
        "residual_risks": ["r1"],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "repo_clean_status": "legacy-unknown",
        "git_mirror_status": "legacy-unknown",
        "timestamp": "T0",
    }
    assert bc.compact_handoff(None) is None


def test_a_current_handoff_keeps_its_own_fields():
    handoff = legacy_handoff(
        "P",
        proof_level="unit",
        missing_proof_levels=["ui"],
        contract_status_changes=[{"contract": "C"}],
        residual_risks=["new"],
        risks=["old"],
        repo_clean_status="clean",
        git_mirror_status="mirrored",
    )

    assert bc.compact_handoff(handoff) == {
        "slice_id": "P",
        "status": "done",
        "summary": "Finished P.",
        "files_touched": [],
        "validations_passed": [],
        "validations_failed": [],
        "proof_level": "unit",
        "missing_proof_levels": ["ui"],
        "contract_status_changes": [{"contract": "C"}],
        "residual_risks": ["new"],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "repo_clean_status": "clean",
        "git_mirror_status": "mirrored",
        "timestamp": "2026-01-01T00:00:00Z",
    }


def test_the_previous_handoff_summary_lists_every_field_present():
    handoff = {
        "slice_id": "P",
        "status": "done",
        "summary": "Did it.",
        "files_touched": ["a.py", "b.py"],
        "validations_passed": ["make a", "make b"],
        "validations_failed": [],
        "proof_level": "unit",
        "missing_proof_levels": ["ui", "device"],
        "contract_status_changes": [{"contract": "C1", "before": "x", "after": "y", "proof": ["p1", "p2"]}, {}],
        "residual_risks": ["r1", "r2"],
        "recommended_next_slice": "N",
        "recommended_next_reason": "it is next",
        "repo_clean_status": "clean",
        "git_mirror_status": "mirrored",
        "timestamp": "T0",
    }

    assert bc.render_previous_handoff_summary(handoff) == (
        "- Previous slice `P` ended with `done`.\n"
        "- Did it.\n"
        "- Files touched: `a.py`, `b.py`\n"
        "- Validations passed: `make a`, `make b`\n"
        "- Proof level: `unit`\n"
        "- Missing proof levels: `ui`, `device`\n"
        "- Contract status changes: C1: x -> y (`p1`, `p2`); unknown contract: unknown -> unknown (`no proof listed`)\n"
        "- Residual risks: r1; r2\n"
        "- Repo clean status: `clean`\n"
        "- Git mirror status: `mirrored`\n"
        "- Recommended next slice: `N` because it is next"
    )


def test_the_previous_handoff_summary_leaves_out_empty_fields():
    handoff = {
        "slice_id": "P",
        "status": "blocked",
        "summary": "Nothing.",
        "files_touched": [],
        "validations_passed": [],
        "validations_failed": ["make a"],
        "proof_level": "",
        "missing_proof_levels": [],
        "contract_status_changes": [],
        "residual_risks": [],
        "recommended_next_slice": "",
        "recommended_next_reason": "ignored without a slice",
        "repo_clean_status": "",
        "git_mirror_status": "",
        "timestamp": "T0",
    }

    assert bc.render_previous_handoff_summary(handoff) == "- Previous slice `P` ended with `blocked`.\n- Nothing."
    assert bc.render_previous_handoff_summary(None) == "No previous handoff is available for this slice."


# --- documents ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("abcde", 5, ("abcde", False)),
        ("abcdef", 5, ("abcde\n...[truncated]\n", True)),
        ("abc  def", 5, ("abc\n...[truncated]\n", True)),
        ("\n\n  first   \n  indented\t\nlast\n\n", 100, ("first\n  indented\nlast", False)),
    ],
)
def test_a_document_excerpt_is_trimmed_and_cut_at_the_limit(text, limit, expected):
    assert bc.compact_document_excerpt(text, max_doc_chars=limit) == expected


@pytest.fixture
def docs_repo(tmp_path) -> Path:
    for name in (
        "AGENTS.md",
        "automation/README.md",
        "docs/workflows/validation.md",
        "docs/product/domain-index.md",
        "docs/product/domains/today.md",
        "SecondBrain/x.md",
        "README.md",
        "notes.md",
    ):
        write(tmp_path / name, f"{name} text\n")
    (tmp_path / "src").mkdir()
    return tmp_path


def test_a_slice_outside_the_known_domains_gets_the_domain_index_its_docs_and_its_domain_contract(docs_repo):
    slice_record = record(
        "T",
        domain="today",
        allowed_paths=[
            "src",
            "SecondBrain/x.md",
            "notes.md",
            "missing.md",
            "README.md",
            "docs/product/domains/today.md",
            "AGENTS.md",
        ],
    )

    assert bc.resolve_document_specs(slice_record, docs_repo) == [
        *CORE_SPECS,
        ("docs/product/domain-index.md", "owner lookup"),
        ("notes.md", "slice-scoped maintained doc"),
        ("README.md", "slice-scoped maintained doc"),
        ("docs/product/domains/today.md", "slice-scoped maintained doc"),
    ]


def test_a_domain_contract_not_in_the_slice_scope_is_added_last(docs_repo):
    assert bc.resolve_document_specs(record("T", domain="today"), docs_repo) == [
        *CORE_SPECS,
        ("docs/product/domain-index.md", "owner lookup"),
        ("docs/product/domains/today.md", "domain-specific contract"),
    ]


def test_a_known_domain_gets_its_own_docs_and_no_domain_index(docs_repo):
    assert bc.resolve_document_specs(record("T", domain="automation"), docs_repo) == CORE_SPECS


def test_without_a_domain_index_none_is_listed(tmp_path):
    assert bc.resolve_document_specs(record("T", domain="today"), tmp_path) == CORE_SPECS


def test_only_documents_that_exist_are_loaded_each_within_the_limit(tmp_path):
    write(tmp_path / "a.md", "short\n")
    write(tmp_path / "b.md", "0123456789\n")

    documents = bc.load_documents(tmp_path, [("a.md", "first"), ("gone.md", "missing"), ("b.md", "second")], max_doc_chars=5)

    assert documents == [
        {"path": "a.md", "reason": "first", "truncated": False, "excerpt": "short"},
        {"path": "b.md", "reason": "second", "truncated": True, "excerpt": "01234\n...[truncated]\n"},
    ]


# --- the whole bundle ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path) -> Path:
    """A consumer repository with a three-slice queue, one handoff and three documents."""
    path = tmp_path / "consumer"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    write(path / "AGENTS.md", "x" * 2501)
    write(path / "automation/README.md", "# Automation\n\nCafé loop.  \n")
    write(path / "docs/product/domains/today.md", "Today rules.\n")
    data = queue(
        record("P", status="done", priority=10),
        record(
            "T",
            title="Build The Thing",
            priority=20,
            depends_on=["P"],
            allowed_paths=["src/", "docs/product/domains/today.md"],
            required_validations=["git diff --check", "make test"],
            max_files_changed=3,
            notes="Do T.",
        ),
        record("Q", priority=30, depends_on=["T"]),
    )
    write(path / "automation/queue/slices.json", json.dumps(data, indent=2) + "\n")
    write_handoff(path / "automation/handoffs", "P", files_touched=["src/p.py"], risks=["none"])
    git(path, "add", "-A")
    git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return path


def expected_bundle(repo: Path) -> dict[str, Any]:
    return {
        "generated_at": "2026-01-02T03:04:05+00:00",
        "policy_sentence": POLICY_SENTENCE,
        "repo": {"root": str(repo), "branch": "main", "head": git(repo, "rev-parse", "HEAD")},
        "slice": {
            "slice_id": "T",
            "title": "Build The Thing",
            "status": "queued",
            "priority": 20,
            "domain": "today",
            "allowed_paths": ["src/", "docs/product/domains/today.md"],
            "required_validations": ["git diff --check", "make test"],
            "depends_on": ["P"],
            "max_files_changed": 3,
            "notes": "Do T.",
        },
        "queue": {
            "version": 1,
            "policy": {"consecutive_autonomous_limit": 2, "handoff_timeout_seconds": 1800},
            "dependency_status": [{"slice_id": "P", "title": "Title P", "status": "done"}],
            "adjacent_queued_slices": [
                {"slice_id": "Q", "title": "Title Q", "priority": 30, "domain": "today", "depends_on": ["T"], "notes": "Notes Q."}
            ],
        },
        "validation_ownership": [
            {
                "command": "git diff --check",
                "tier": "supervisor_replayable",
                "reason": "The supervisor replays this exact allowlisted command before continuation.",
            },
            {
                "command": "make test",
                "tier": "run_report_only",
                "reason": "This command is currently run-reported only and not replayed by the supervisor.",
            },
        ],
        "previous_handoff": {
            "slice_id": "P",
            "status": "done",
            "summary": "Finished P.",
            "files_touched": ["src/p.py"],
            "validations_passed": [],
            "validations_failed": [],
            "proof_level": "legacy-unknown",
            "missing_proof_levels": [],
            "contract_status_changes": [],
            "residual_risks": ["none"],
            "recommended_next_slice": "",
            "recommended_next_reason": "",
            "repo_clean_status": "legacy-unknown",
            "git_mirror_status": "legacy-unknown",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        "previous_handoff_summary": (
            "- Previous slice `P` ended with `done`.\n"
            "- Finished P.\n"
            "- Files touched: `src/p.py`\n"
            "- Proof level: `legacy-unknown`\n"
            "- Residual risks: none\n"
            "- Repo clean status: `legacy-unknown`\n"
            "- Git mirror status: `legacy-unknown`"
        ),
        "execution_constraints": [
            "Stay within allowed_paths: src/, docs/product/domains/today.md",
            "Keep changed files at or below max_files_changed=3",
            "Preserve ownership boundaries for domain, application, and adapter code",
            "Run required validations exactly: git diff --check, make test",
            "Name the highest proof level reached and any relevant missing proof levels",
            "Write one honest JSON handoff before exiting",
        ],
        "handoff_template": {
            "slice_id": "T",
            "status": "done",
            "summary": "<describe what changed for build the thing>",
            "files_touched": ["<repo-relative-path>"],
            "validations_passed": ["git diff --check", "make test"],
            "validations_failed": [],
            "proof_level": "<highest proof level reached>",
            "missing_proof_levels": ["<proof levels still missing, if relevant>"],
            "contract_status_changes": [
                {
                    "contract": "<contract or product/workflow rule>",
                    "before": "<status before this slice>",
                    "after": "<status after this slice>",
                    "proof": ["<validation or artifact supporting the change>"],
                }
            ],
            "residual_risks": ["<remaining risk or 'No known residual risk.'>"],
            "recommended_next_slice": "",
            "recommended_next_reason": "",
            "repo_clean_status": "<clean|dirty|unknown>",
            "git_mirror_status": "<mirrored|not-mirrored|not-relevant|not-checked>",
            "dirty_paths_outside_scope": [],
            "timestamp": "<UTC ISO-8601 timestamp>",
        },
        "documents": [
            {
                "path": "AGENTS.md",
                "reason": "repo operating contract",
                "truncated": True,
                "excerpt": "x" * 2500 + "\n...[truncated]\n",
            },
            {
                "path": "automation/README.md",
                "reason": "automation harness loop",
                "truncated": False,
                "excerpt": "# Automation\n\nCafé loop.",
            },
            {
                "path": "docs/product/domains/today.md",
                "reason": "slice-scoped maintained doc",
                "truncated": False,
                "excerpt": "Today rules.",
            },
        ],
    }


def test_the_bundle_for_a_queued_slice(repo):
    bundle = bc.build_context_bundle(
        repo_root=repo,
        queue_path=repo / "automation/queue/slices.json",
        handoff_dir=repo / "automation/handoffs",
        slice_id="T",
        now=lambda: NOW,
    )

    assert bundle == expected_bundle(repo)


def test_a_caller_holding_the_queue_needs_no_queue_file(repo):
    data = json.loads((repo / "automation/queue/slices.json").read_text(encoding="utf-8"))

    bundle = bc.build_context_bundle(
        repo_root=repo,
        queue_path=repo / "no-such-queue.json",
        handoff_dir=repo / "automation/handoffs",
        slice_id="T",
        queue_data=data,
        now=lambda: NOW,
    )

    assert bundle == expected_bundle(repo)


def test_an_unknown_slice_is_refused(repo):
    with pytest.raises(KeyError, match="Unknown slice_id: nope"):
        bc.build_context_bundle(repo, repo / "automation/queue/slices.json", repo / "automation/handoffs", "nope")


def test_a_memory_map_without_a_project_is_a_config_error(repo):
    config = memory.load_config(REPO_ROOT / "automation/examples/memory-config.json")

    with pytest.raises(policy.ConfigError, match="^a memory config needs the project whose bank to recall from$") as raised:
        bc.build_context_bundle(
            repo, repo / "automation/queue/slices.json", repo / "automation/handoffs", "T", memory_config=config
        )

    assert type(raised.value) is policy.ConfigError


def test_git_output_outside_a_repository_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    with pytest.raises(subprocess.CalledProcessError):
        bc.git_output(tmp_path, ["git", "rev-parse", "HEAD"])


# --- the command --------------------------------------------------------------------------------


def test_the_command_prints_the_bundle_with_its_defaults(repo, capsys):
    assert bc.main(["--slice-id", "T"], repo_root=repo, now=lambda: NOW) == 0

    out = capsys.readouterr().out
    assert out == json.dumps(expected_bundle(repo), indent=2, ensure_ascii=False) + "\n"


def test_the_command_writes_the_bundle_to_a_new_or_existing_directory(repo, capsys):
    output = repo / "out/nested/bundle.json"

    for _ in range(2):
        assert bc.main(["--slice-id", "T", "--output", "out/nested/bundle.json"], repo_root=repo, now=lambda: NOW) == 0
        assert capsys.readouterr().out == f"{output}\n"

    assert output.read_text(encoding="utf-8") == json.dumps(expected_bundle(repo), indent=2, ensure_ascii=False) + "\n"


def test_the_command_reads_the_memory_map_from_the_repository(repo, capsys):
    port = closed_port()
    write(
        repo / "memory.json",
        json.dumps(
            {
                "version": 1,
                "endpoint": f"http://127.0.0.1:{port}",
                "timeout_seconds": 5,
                "budgets": {
                    "max_request_bytes": 10000,
                    "max_response_bytes": 10000,
                    "recall_max_tokens": 100,
                    "reflect_max_tokens": 100,
                    "max_results": 5,
                    "max_result_chars": 100,
                    "max_total_chars": 100,
                },
                "projects": {"consumer": {"bank_id": "bank-1", "board": "consumer", "operations": ["recall"]}},
            }
        ),
    )
    code = bc.main(
        ["--slice-id", "T", "--memory-config", "memory.json", "--memory-project", "consumer"],
        repo_root=repo,
        now=lambda: NOW,
        environ={},
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["memory"] == {
        "source": "hindsight",
        "project": "consumer",
        "query": "Build The Thing",
        "bank_id": "bank-1",
        "status": "unavailable",
        "error": "connection refused",
        "results": [],
    }


def test_the_script_runs_as_a_command():
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "automation/context/build_context.py"),
            "--slice-id",
            "x",
            "--memory-config",
            "no-such-map.json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT.parent,
        env=env,
    )

    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"stop: cannot read memory config {REPO_ROOT / 'no-such-map.json'}: No such file or directory\n"
