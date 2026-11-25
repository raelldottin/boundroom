"""Track R of the effectiveness proof (SPEC-012): selection, driver, oracle and pilot, with a fake runner.

No test here starts a model. Arm U's ``claude`` and arm S's ``run_next.py`` are replaced by
``FakeRunner``, which edits and commits the way an agent would; Git, the oracle's pytest and the
blind-review export run for real on a synthetic repository.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from automation.benchmark.trackr import driver, oracle, pilot
from automation.benchmark.trackr import select as select_module
from automation.benchmark.trackr.common import (
    PYTEST,
    REPO_ROOT,
    SCOPE_FIELDS,
    TASKS_FILE,
    OVERLAY,
    Completed,
    Overlay,
    TrackRError,
    git,
    has_object,
    hidden_test_result,
    inside_repository,
    isolated_environment,
    pytest_counts,
    read_json,
    scope_errors,
)
from automation.supervisor import policy, runner_profile

CALC_BUGGY = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
HIDDEN_TEST = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _real(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
    result = subprocess.run(list(argv), cwd=cwd, env=dict(env), capture_output=True, text=True)
    return Completed(result.returncode, result.stdout, result.stderr)


def _commit_all(cwd: Path, env: Mapping[str, str], message: str) -> None:
    git(["add", "-A"], cwd, env)
    git(["commit", "--quiet", "-m", message], cwd, env)


def make_repository(root: Path) -> tuple[Path, str, str]:
    """A source repository whose head fixes ``add`` and adds the test that proves it."""
    env = isolated_environment(os.environ, root / "source-private")
    repo = root / "source"
    repo.mkdir()
    git(["init", "--quiet", "-b", "main"], repo, env)
    git(["config", "user.name", "Test"], repo, env)
    git(["config", "user.email", "test@example.invalid"], repo, env)
    files = {
        "pkg/__init__.py": "",
        "pkg/calc.py": CALC_BUGGY,
        "pkg/other.py": "VALUE = 1\n",
        "automation/tests/test_existing.py": "def test_ok():\n    assert True\n",
        "docs/guide.md": "# Guide\n",
    }
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    _commit_all(repo, env, "base")
    base = git(["rev-parse", "HEAD"], repo, env).strip()
    (repo / "pkg/calc.py").write_text(CALC_FIXED)
    (repo / "automation/tests/test_calc.py").write_text(HIDDEN_TEST)
    _commit_all(repo, env, "head: fix add")
    head = git(["rev-parse", "HEAD"], repo, env).strip()
    # A later release whose supervisor every clone gets, as the real overlay commit is.
    (repo / "automation/supervisor").mkdir(parents=True)
    (repo / "automation/supervisor/marker.py").write_text("RELEASE = 'overlay'\n")
    _commit_all(repo, env, "release")
    return repo, base, head


def make_task(base: str, head: str, **scope: Any) -> dict[str, Any]:
    task = {
        "id": "pr1",
        "number": 1,
        "title": "Fix add",
        "body": "add() subtracts.",
        "base_sha": base,
        "head_sha": head,
        "hidden_tests": ["automation/tests/test_calc.py"],
        "hidden_test_support_files": [],
        "human_solution": {
            "files": ["automation/tests/test_calc.py", "pkg/calc.py"],
            "numstat": [
                {"path": "automation/tests/test_calc.py", "added": 5, "deleted": 0},
                {"path": "pkg/calc.py", "added": 1, "deleted": 1},
            ],
            "files_changed": 2,
        },
        "checks": {"oracle_fails_at_base": True, "oracle_passes_at_head": True},
        "task_statement": "Make add() return the sum of its arguments.",
        "allowed_paths": ["pkg/calc.py", "automation/tests/"],
        "required_validations": ["test -f pkg/calc.py"],
        "max_files_changed": 2,
    }
    task.update(scope)
    return task


class FakeRunner:
    """Stands in for every command the driver and oracle run that isn't Git.

    Arm U's ``claude`` and arm S's ``run_next.py`` write ``edits`` and commit them, as an agent
    would; pytest and validation commands run for real; ``uv sync`` is a no-op.
    """

    def __init__(
        self,
        edits: Optional[Mapping[str, str]] = None,
        cost: Optional[float] = 1.25,
        report: Optional[Mapping[str, Any]] = None,
        claude_bin: str = "claude",
        subtype: str = "success",
    ) -> None:
        self.edits = dict(edits or {})
        self.cost = cost
        self.report = dict(report or {"decision": "stop_for_review", "queue_status": "done", "reason": "limit reached"})
        self.claude_bin = claude_bin
        self.subtype = subtype
        self.calls: list[dict[str, Any]] = []

    def _result(self) -> str:
        return json.dumps(
            {
                "type": "result",
                "subtype": self.subtype,
                "is_error": False,
                "session_id": "s-1",
                "total_cost_usd": self.cost,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 5,
                },
                "modelUsage": {"claude-test-1": {"costUSD": self.cost}},
            }
        )

    def _edit(self, cwd: Path, env: Mapping[str, str]) -> None:
        if not self.edits:
            return
        for path, text in self.edits.items():
            (cwd / path).parent.mkdir(parents=True, exist_ok=True)
            (cwd / path).write_text(text)
        _commit_all(cwd, env, "agent change")

    def calls_to(self, first: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["argv"][0] == first or call["argv"][1:2] == [first]]

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Completed:
        argv = list(argv)
        self.calls.append({"argv": argv, "cwd": Path(cwd), "env": dict(env), "timeout": timeout})
        if argv[:3] == ["uv", "sync", "--frozen"]:
            return Completed(0)
        if argv[: len(PYTEST)] == list(PYTEST):
            return _real([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *argv[len(PYTEST) :]], cwd, env)
        if argv[:2] == ["/bin/sh", "-c"]:
            return _real(argv, cwd, env)
        if argv[0] == self.claude_bin and "--version" in argv:
            return Completed(0, "9.9.9 (Claude Code)\n")
        if argv[0] == self.claude_bin and "--help" in argv:
            return Completed(0, " ".join(driver.REQUIRED_CLAUDE_FLAGS))
        if argv[0] == self.claude_bin:
            self._edit(cwd, env)
            return Completed(0, self._result(), wall_seconds=3.0)
        if argv[1:2] == ["automation/supervisor/run_next.py"]:
            self._edit(cwd, env)
            report_file = Path(argv[argv.index("--report-file") + 1])
            (report_file.parent / "claude-output.json").write_text(self._result())
            report_file.write_text(json.dumps(self.report) + "\n")
            return Completed(0 if self.report.get("queue_status") == "done" else 2, "", "", wall_seconds=4.0)
        raise AssertionError(f"unexpected command {argv}")


class TrackRTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="trackr-test-")).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        self.repo, self.base, self.head = make_repository(self.tmp)
        release = git(["rev-parse", "HEAD"], self.repo, isolated_environment(os.environ, self.tmp / "source-private")).strip()
        self.overlay = Overlay(release, ("automation/supervisor",))
        self.environ = {
            **os.environ,
            "HOME": str(self.tmp / "home"),
            "GH_TOKEN": "secret",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "CLAUDECODE": "1",
            "BOUNDROOM_CLAUDE_PROFILE": "unbounded",
            "BOUNDROOM_CLAUDE_PERMISSION_MODE": "bypassPermissions",
        }
        (self.tmp / "home").mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()

    def config(self, runner: FakeRunner, **overrides: Any) -> driver.DriverConfig:
        overrides.setdefault("overlay", self.overlay)
        return driver.DriverConfig(runner=runner, environ=self.environ, work_dir=self.work, **overrides)

    def session(self, arm: str, runner: FakeRunner, task: Optional[dict] = None, **overrides: Any) -> dict[str, Any]:
        record = driver.run_session(
            task or make_task(self.base, self.head), arm, self.repo, self.tmp / "out" / arm, self.config(runner, **overrides)
        )
        self.addCleanup(driver.cleanup_session, record)
        return record

    def assert_no_bypass_without_sandbox(self, argv: Sequence[str], settings: Optional[Mapping[str, Any]]) -> None:
        text = " ".join(argv) + json.dumps(settings or {})
        if "bypassPermissions" in text or "--dangerously-skip-permissions" in text:
            self.assertTrue(settings and settings.get("sandbox", {}).get("enabled"), "bypassPermissions without the sandbox")
        self.assertNotIn("bypassPermissions", text)


class DriverSetupTests(TrackRTestCase):
    def test_the_clone_has_no_remote_no_solution_and_a_setup_commit_holding_only_the_overlay_for_u(self) -> None:
        record = self.session("U", FakeRunner(edits={"pkg/calc.py": CALC_FIXED}))
        clone = Path(record["paths"]["clone"])
        env = isolated_environment(os.environ, self.tmp / "check")
        self.assertEqual(record["remotes"], [])
        self.assertEqual(git(["remote"], clone, env).strip(), "")
        self.assertTrue(record["solution_absent_from_clone"])
        self.assertFalse(has_object(clone, self.head, env))
        self.assertEqual(git(["rev-parse", f"{record['setup_sha']}^"], clone, env).strip(), self.base)
        changed = git(["diff", "--name-only", self.base, record["setup_sha"]], clone, env).split()
        self.assertEqual(changed, ["automation/supervisor/marker.py"])
        self.assertEqual(
            record["overlay"], {"commit": self.overlay.commit, "paths": ["automation/supervisor"], "files_changed": 1}
        )
        # The overlay commit itself is not in the clone's history: only its files are.
        self.assertFalse(has_object(clone, self.overlay.commit, env))
        self.assertIsNone(inside_repository(clone.parent))

    def test_the_s_setup_commit_adds_one_schema_valid_queued_slice_from_the_scope_fields(self) -> None:
        record = self.session("S", FakeRunner(edits={"pkg/calc.py": CALC_FIXED}))
        clone = Path(record["paths"]["clone"])
        env = isolated_environment(os.environ, self.tmp / "check")
        self.assertEqual(
            git(["diff", "--name-only", self.base, record["setup_sha"]], clone, env).split(),
            [driver.QUEUE_PATH, "automation/supervisor/marker.py"],
        )
        self.assertEqual(record["overlay"]["commit"], self.overlay.commit)
        queue = policy.load_queue(clone / driver.QUEUE_PATH)
        (slice_record,) = queue["slices"]
        task = make_task(self.base, self.head)
        self.assertEqual(slice_record["status"], "queued")
        self.assertEqual(slice_record["allowed_paths"], task["allowed_paths"])
        self.assertEqual(slice_record["required_validations"], task["required_validations"])
        self.assertEqual(slice_record["max_files_changed"], task["max_files_changed"])
        self.assertEqual(slice_record["notes"], task["task_statement"])
        self.assertTrue(queue["policy"]["agent_command_template"].startswith(f"{driver.RUN_AGENT} "))
        self.assertEqual(queue["policy"]["handoff_timeout_seconds"], driver.DEFAULT_TIMEOUT_SECONDS)

    def test_both_arms_start_from_the_same_tree_apart_from_the_s_queue(self) -> None:
        env = isolated_environment(os.environ, self.tmp / "check")
        trees = {}
        for arm in ("U", "S"):
            record = self.session(arm, FakeRunner())
            listing = git(["ls-tree", "-r", record["setup_sha"]], Path(record["paths"]["clone"]), env).splitlines()
            trees[arm] = [line for line in listing if not line.endswith(driver.QUEUE_PATH)]
        self.assertEqual(trees["U"], trees["S"])
        self.assertTrue(any(line.endswith("automation/supervisor/marker.py") for line in trees["U"]))

    def test_a_task_without_its_scope_is_refused_before_anything_is_cloned(self) -> None:
        task = make_task(
            self.base, self.head, task_statement="", allowed_paths=[], required_validations=[], max_files_changed=None
        )
        with self.assertRaisesRegex(TrackRError, "no scope yet"):
            driver.run_session(task, "U", self.repo, self.tmp / "out", self.config(FakeRunner()))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_worker_inside_a_supervised_slice_cannot_start_a_session(self) -> None:
        self.environ["BOUNDROOM_SUPERVISOR_SLICE_ID"] = "some-slice"
        with self.assertRaisesRegex(TrackRError, "inside a supervised slice"):
            driver.run_session(make_task(self.base, self.head), "S", self.repo, self.tmp / "out", self.config(FakeRunner()))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_session_directory_inside_a_repository_is_refused(self) -> None:
        with self.assertRaisesRegex(TrackRError, "inside the repository"):
            driver.run_session(make_task(self.base, self.head), "U", self.repo, self.repo / "out", self.config(FakeRunner()))


class ArmCommandTests(TrackRTestCase):
    def test_arm_u_runs_claude_print_json_with_the_cap_accept_edits_and_the_fail_closed_sandbox(self) -> None:
        runner = FakeRunner(edits={"pkg/calc.py": CALC_FIXED})
        record = self.session("U", runner)
        (call,) = [call for call in runner.calls_to("claude") if "-p" in call["argv"]]
        argv = call["argv"]
        options = dict(zip(argv, argv[1:]))
        self.assertTrue(options["-p"].startswith("Make add() return the sum"))
        self.assertTrue(options["-p"].endswith("\n\nCommit your change when you are done."))
        self.assertEqual(options["--output-format"], "json")
        self.assertEqual(options["--max-budget-usd"], "4.50")
        self.assertEqual(options["--permission-mode"], "acceptEdits")
        self.assertEqual(options["--setting-sources"], "project,local")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(call["cwd"], Path(record["paths"]["clone"]))
        self.assertEqual(call["timeout"], driver.DEFAULT_TIMEOUT_SECONDS)
        settings = read_json(Path(options["--settings"]))
        self.assertFalse(Path(options["--settings"]).is_relative_to(Path(record["paths"]["clone"])))
        self.assertEqual(settings["sandbox"]["enabled"], True)
        self.assertEqual(settings["sandbox"]["failIfUnavailable"], True)
        self.assertEqual(settings["sandbox"]["allowUnsandboxedCommands"], False)
        self.assertEqual(settings["sandbox"]["network"], {"allowedDomains": []})
        self.assertEqual(settings["permissions"]["defaultMode"], "acceptEdits")
        self.assertIn("WebFetch", settings["permissions"]["deny"])
        self.assert_no_bypass_without_sandbox(argv, settings)
        self.assertTrue(record["presented_as_done"])
        self.assertEqual(record["cost_usd"], 1.25)
        self.assertEqual(record["claude_code_version"], "9.9.9 (Claude Code)")

    def test_arm_s_runs_run_next_single_slice_with_the_claude_runner_and_the_default_bounded_profile(self) -> None:
        runner = FakeRunner(edits={"pkg/calc.py": CALC_FIXED})
        record = self.session("S", runner)
        clone = Path(record["paths"]["clone"])
        (call,) = runner.calls_to("automation/supervisor/run_next.py")
        argv, env = call["argv"], call["env"]
        self.assertEqual(argv[0], str(clone / ".venv/bin/python"))
        self.assertIn("--single-slice", argv)
        self.assertNotIn("--autonomous-limit", argv)
        for flag in ("--report-file", "--handoff-dir"):
            self.assertFalse(Path(argv[argv.index(flag) + 1]).is_relative_to(clone))
        self.assertEqual(call["timeout"], driver.DEFAULT_TIMEOUT_SECONDS + driver.SUPERVISOR_MARGIN_SECONDS)
        self.assertEqual(env["BOUNDROOM_AGENT_RUNNER"], "claude")
        self.assertEqual(env["BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD"], "4.50")
        wrapper = Path(env["BOUNDROOM_CLAUDE_BIN"])
        self.assertFalse(wrapper.is_relative_to(clone))
        # No variable that would replace the default profile: run_next resolves bounded, with the sandbox.
        for name in ("BOUNDROOM_CLAUDE_PROFILE", "BOUNDROOM_CLAUDE_PERMISSION_MODE", "BOUNDROOM_CLAUDE_ISOLATED", "CLAUDECODE"):
            self.assertNotIn(name, env)
        self.assertTrue(runner_profile.configured({}, env))
        profile = runner_profile.choose({}, env)
        self.assertEqual((profile.effective, profile.mode, profile.sandbox), ("bounded", "dontAsk", True))
        self.assert_no_bypass_without_sandbox(argv, None)
        self.assertNotIn("bypassPermissions", wrapper.read_text())
        self.assertEqual(record["supervised"]["decision"], "stop_for_review")
        self.assertEqual(record["supervised"]["gate"], "none")
        self.assertTrue(record["presented_as_done"])
        self.assertEqual(record["cost_usd"], 1.25)

    def test_both_arms_inherit_the_same_environment_apart_from_their_own_variables(self) -> None:
        runner_u, runner_s = FakeRunner(), FakeRunner()
        self.session("U", runner_u)
        self.session("S", runner_s)
        env_u = [call for call in runner_u.calls_to("claude") if "-p" in call["argv"]][0]["env"]
        env_s = runner_s.calls_to("automation/supervisor/run_next.py")[0]["env"]
        own = {"BOUNDROOM_AGENT_RUNNER", "BOUNDROOM_CLAUDE_BIN", "BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD"}
        private = {"GIT_CONFIG_GLOBAL", "GH_CONFIG_DIR", "UV_CACHE_DIR"}  # per clone, same meaning
        self.assertEqual(set(env_s) - set(env_u), own)
        for name in set(env_u) - private:
            self.assertEqual(env_u[name], env_s[name], name)
        for env in (env_u, env_s):
            for name in ("GH_TOKEN", "SSH_AUTH_SOCK", "CLAUDECODE", "BOUNDROOM_CLAUDE_PROFILE"):
                self.assertNotIn(name, env)
            self.assertEqual(Path(env["GIT_CONFIG_GLOBAL"]).read_text(), "")
            self.assertEqual(list(Path(env["GH_CONFIG_DIR"]).iterdir()), [])
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_the_usage_wrapper_sets_the_drivers_cap_adds_json_and_keeps_the_settings_file(self) -> None:
        session_dir = self.tmp / "wrapped"
        session_dir.mkdir()
        fake = self.tmp / "fake-claude"
        fake.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)
        wrapper = session_dir / "wrapper.sh"
        wrapper.write_text(driver.usage_wrapper(str(fake), session_dir, 4.5, isolate=True, keep_transcript=True))
        wrapper.chmod(0o755)
        settings = self.tmp / "slice-settings.json"
        settings.write_text('{"permissions": {"defaultMode": "dontAsk"}}')
        argv = ["--print", "--input-format", "text", "--no-session-persistence", "--settings", str(settings)]
        argv += ["--max-budget-usd", "5", "--permission-mode", "dontAsk", "--add-dir", "/repo"]
        subprocess.run([str(wrapper), *argv], check=True)
        passed = json.loads((session_dir / "claude-output.json").read_text())
        options = dict(zip(passed, passed[1:]))
        self.assertEqual(options["--max-budget-usd"], "4.50")
        self.assertEqual(options["--permission-mode"], "dontAsk")
        self.assertEqual(options["--output-format"], "json")
        self.assertEqual(options["--setting-sources"], "project,local")
        self.assertNotIn("--no-session-persistence", passed)
        self.assertEqual((session_dir / "supervisor-budget-arg.txt").read_text().split(), ["5"])
        self.assertEqual(read_json(session_dir / "runner-settings.json"), {"permissions": {"defaultMode": "dontAsk"}})

    def test_the_handoff_rule_check_tells_an_absolute_rule_from_one_claude_code_reads_as_relative(self) -> None:
        handoffs = self.tmp / "handoffs"
        absolute = {"permissions": {"allow": [f"Edit(/{handoffs}/x.json)"]}}
        relative = {"permissions": {"allow": [f"Edit({handoffs}/x.json)"]}}
        self.assertEqual(driver.handoff_rule_status(absolute, handoffs), "absolute")
        self.assertTrue(driver.handoff_rule_status(relative, handoffs).startswith("single-slash"))
        self.assertTrue(driver.handoff_rule_status({"permissions": {"allow": []}}, handoffs).startswith("missing"))


class UsageTests(unittest.TestCase):
    def test_transcript_usage_counts_each_message_once_and_reads_the_cost_state(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            usage = {"input_tokens": 2, "output_tokens": 7, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50}
            lines = [
                {"type": "assistant", "version": "2.1.323", "message": {"id": "m1", "model": "claude-x", "usage": usage}},
                {"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": usage}},
                {"type": "assistant", "message": {"id": "m2", "model": "claude-x", "usage": usage}},
                {"type": "cost-state", "sessionId": "a", "totalCostUSD": 0.5, "modelUsage": {"claude-x": {}}},
                {"type": "cost-state", "sessionId": "a", "totalCostUSD": 0.75, "modelUsage": {"claude-x": {}}},
            ]
            (directory / "a.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
            result = driver.transcript_usage([directory])
        assert result is not None
        self.assertEqual(result["assistant_messages"], 2)
        self.assertEqual(result["tokens"]["output_tokens"], 14)
        self.assertEqual(result["cost_usd"], 0.75)
        self.assertEqual(result["models"], ["claude-x"])
        self.assertEqual(result["claude_code_versions"], ["2.1.323"])

    def test_session_usage_prefers_the_transcript_and_keeps_the_json_as_the_cross_check(self) -> None:
        transcript = {"cost_usd": 1.0, "assistant_messages": 3, "tokens": {"output_tokens": 9}, "models": ["a"]}
        output = driver.json_usage(
            '{"type": "result", "subtype": "error_max_budget_usd", "total_cost_usd": 1.1, "modelUsage": {"b": {}}}'
        )
        usage = driver.session_usage(transcript, output, cap_usd=4.5)
        self.assertEqual(
            (usage["cost_usd"], usage["cost_source"], usage["cost_cross_check_usd"]), (1.0, "transcript cost-state", 1.1)
        )
        self.assertEqual(usage["models"], ["a", "b"])
        self.assertTrue(usage["cap_hit"])
        self.assertEqual(driver.session_usage(None, None, 4.5)["cost_source"], "unknown")

    def test_transcripts_are_found_under_the_encoded_clone_path(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            clone = home / "tmp" / "trackr-session-x" / "repo"
            clone.mkdir(parents=True)
            project = home / ".claude" / "projects" / driver.encoded_project(clone.resolve())
            project.mkdir(parents=True)
            self.assertEqual(driver.transcript_dirs(clone, {"HOME": str(home)}), [project])
            self.assertTrue(driver.encoded_project(Path("/private/tmp/a.b")) == "-private-tmp-a-b")

    def test_the_gate_comes_from_the_decision_reports_reason(self) -> None:
        cases = {
            "Files changed outside the slice's allowed_paths were detected.": "scope",
            "Slice exceeded max_files_changed (9 > 2).": "diff_budget",
            "Required validations were missing, failed, or did not pass supervisor replay.": "validation",
            "Missing handoff artifact for slice x at /h": "handoff",
            "Agent command exited with code 124 for slice x.": "timeout",
            "Slice reported blocked status.": "agent",
        }
        for reason, gate in cases.items():
            self.assertEqual(driver.gate_of({"queue_status": "failed", "reason": reason}), gate, reason)
        self.assertEqual(driver.gate_of({"queue_status": "done"}), "none")
        self.assertEqual(driver.gate_of(None, "stop: another supervised run holds the lock"), "refused")


class OracleTests(TrackRTestCase):
    def verdict(self, record: Mapping[str, Any], task: Mapping[str, Any], runner: FakeRunner) -> dict[str, Any]:
        return oracle.evaluate(
            task,
            Path(record["paths"]["clone"]),
            record["setup_sha"],
            record["final_sha"],
            self.repo,
            runner=runner,
            environ=self.environ,
        )

    def test_a_correct_in_scope_change_passes_the_hidden_tests_and_every_check(self) -> None:
        runner = FakeRunner(edits={"pkg/calc.py": CALC_FIXED})
        task = make_task(self.base, self.head)
        verdict = self.verdict(self.session("U", runner, task), task, runner)
        self.assertTrue(verdict["hidden_tests_pass"])
        self.assertEqual(verdict["hidden_tests"]["passed"], 1)
        self.assertTrue(verdict["validations_passed"])
        self.assertEqual(verdict["out_of_scope_paths"], [])
        self.assertTrue(verdict["within_budget"])
        self.assertEqual(verdict["numstat"], [{"path": "pkg/calc.py", "added": 1, "deleted": 1}])

    def test_an_unfixed_out_of_scope_change_fails_the_hidden_tests_and_is_flagged(self) -> None:
        edits = {"pkg/other.py": "VALUE = 2\n", "docs/guide.md": "# Changed\n", "pkg/new.py": "X = 1\n"}
        runner = FakeRunner(edits=edits)
        task = make_task(self.base, self.head, required_validations=["test -f pkg/missing.py"])
        verdict = self.verdict(self.session("S", runner, task), task, runner)
        self.assertFalse(verdict["hidden_tests_pass"])
        self.assertEqual(verdict["hidden_tests"]["failed"], 1)
        self.assertFalse(verdict["validations_passed"])
        self.assertEqual(verdict["out_of_scope_paths"], ["docs/guide.md", "pkg/new.py", "pkg/other.py"])
        self.assertFalse(verdict["within_budget"])

    def test_the_agents_own_copy_of_a_hidden_test_is_replaced_by_the_human_one(self) -> None:
        cheat = "def test_add():\n    assert True\n"
        runner = FakeRunner(edits={"automation/tests/test_calc.py": cheat})
        task = make_task(self.base, self.head)
        verdict = self.verdict(self.session("U", runner, task), task, runner)
        self.assertFalse(verdict["hidden_tests_pass"])

    def test_pytest_counts_and_the_fail_at_base_rule(self) -> None:
        self.assertEqual(
            pytest_counts("x\n2 failed, 10 passed, 1 error in 0.52s\n"), {"passed": 10, "failed": 2, "errors": 1, "skipped": 0}
        )
        import_error = hidden_test_result(Completed(2, "ERROR automation/tests/test_x.py\n1 error in 0.05s\n"))
        self.assertTrue(import_error["failed_any"])
        self.assertFalse(import_error["passed_all"])
        nothing_ran = hidden_test_result(Completed(5, "no tests ran in 0.01s\n"))
        self.assertFalse(nothing_ran["failed_any"] or nothing_ran["passed_all"])


class SelectionTests(TrackRTestCase):
    def pull_request(self, number: int, merged_at: str, head: str, files: Sequence[str]) -> dict[str, Any]:
        return {
            "number": number,
            "title": f"PR {number}",
            "body": "",
            "mergedAt": merged_at,
            "mergeCommit": {"oid": head},
            "files": [{"path": p} for p in files],
        }

    def test_static_checks_and_the_oracle_keep_a_qualifying_pull_request(self) -> None:
        env = isolated_environment(os.environ, self.tmp / "sel")
        pr = self.pull_request(1, "2026-11-14T00:00:00Z", self.head, ["automation/tests/test_calc.py", "pkg/calc.py"])
        static = select_module.static_checks(pr, self.repo, env)
        self.assertEqual(static["reasons"], [])
        self.assertEqual(static["base_sha"], self.base)
        self.assertEqual(static["hidden_tests"], ["automation/tests/test_calc.py"])
        result = select_module.oracle_check(
            self.repo,
            self.base,
            self.head,
            static["hidden_tests"],
            runner=FakeRunner(),
            environ=self.environ,
            work_dir=self.work,
            overlay=self.overlay,
        )
        self.assertTrue(result["fails_at_base"])
        self.assertTrue(result["passes_at_head"])

    def test_the_window_order_and_the_rejection_reasons(self) -> None:
        prs = [
            self.pull_request(3, "2026-11-24T00:00:00Z", self.head, []),
            self.pull_request(2, "2026-11-15T00:00:00Z", self.head, ["pkg/calc.py"]),
            self.pull_request(1, "2026-11-14T00:00:00Z", self.head, ["automation/tests/test_calc.py", "pkg/calc.py"]),
        ]
        self.assertEqual([pr["number"] for pr in select_module.in_window(prs)], [1, 2])
        tasks, audit = select_module.select_tasks(
            prs, self.repo, runner=FakeRunner(), environ=self.environ, work_dir=self.work, count=1, overlay=self.overlay
        )
        self.assertEqual([task["number"] for task in tasks], [1])
        self.assertEqual([(entry["number"], entry["decision"]) for entry in audit], [(1, "kept"), (2, "not_examined")])
        self.assertEqual(len(scope_errors(tasks[0])), len(SCOPE_FIELDS))
        self.assertTrue(select_module.documentation_only(["docs/a.md", "README.md"]))
        self.assertFalse(select_module.documentation_only(["docs/a.md", "automation/tests/test_a.py"]))


class PilotTests(TrackRTestCase):
    def test_arm_order_rotates_per_task(self) -> None:
        tasks = [{"number": n} for n in range(4)]
        self.assertEqual([arm for _, _, arm in pilot.plan(tasks)], ["U", "S", "S", "U", "U", "S", "S", "U"])

    def fake_session(self, costs: list[Optional[float]]):
        calls = []

        def session(task, arm, mirror, session_dir, config):
            calls.append((task["number"], arm))
            return {"arm": arm, "status": "skipped", "cost_usd": costs[len(calls) - 1]}

        return session, calls

    def test_the_pilot_stops_before_a_session_that_could_exceed_the_total(self) -> None:
        session, calls = self.fake_session([1.0, None, 1.0])
        tasks = [make_task(self.base, self.head, number=n, id=f"pr{n}") for n in (1, 2)]
        summary = pilot.run_pilot(
            tasks, self.repo, self.tmp / "pilot", self.config(FakeRunner()), budget_total=9.5, session_fn=session
        )
        # 1.00 spent, then an unknown cost counts as the 4.50 cap: 5.50 + 4.50 > 9.50 stops the third.
        self.assertEqual(calls, [(1, "U"), (1, "S")])
        self.assertIn("budget", summary["stopped"])
        self.assertEqual(summary["spent_usd"], 5.5)

    def test_a_full_pilot_scores_both_arms_and_exports_blind_diffs(self) -> None:
        runner = FakeRunner(edits={"pkg/calc.py": CALC_FIXED, ".claude/settings.local.json": "{}"})
        tasks = [make_task(self.base, self.head)]
        out = self.tmp / "pilot"
        summary = pilot.run_pilot(tasks, self.repo, out, self.config(runner), rng=random.Random(7))
        self.assertIsNone(summary["stopped"])
        for arm in ("U", "S"):
            self.assertEqual(summary["arms"][arm]["sessions"], 1)
            self.assertEqual(summary["arms"][arm]["hidden_test_pass_rate"], 1.0)
            self.assertEqual(summary["arms"][arm]["mean_cost_usd"], 1.25)
            # The oracle counts the agent's .claude/ file as out of scope; the review export leaves it out.
            self.assertEqual(summary["arms"][arm]["out_of_scope"], 1)
        self.assertEqual(summary["arms"]["S"]["decisions"], {"stop_for_review": 1})
        mapping = read_json(out / "mapping.json")["sessions"]
        index = read_json(out / "review" / "index.json")
        self.assertEqual(sorted(entry["id"] for entry in index["queue"]), sorted(mapping))
        self.assertEqual(sorted(item["arm"] for item in mapping.values()), ["S", "U"])
        self.assertNotIn("arm", json.dumps(index["queue"]))
        for review_id in mapping:
            patch = (out / "review" / review_id / "change.patch").read_text()
            self.assertTrue(patch.startswith(pilot.NEUTRAL_HEADER))
            self.assertIn("pkg/calc.py", patch)
            self.assertNotIn(".claude", patch)
            self.assertNotIn("slices.json", patch)
            self.assertNotIn("agent change", patch)
        self.assertEqual(len(list((out / "sessions").iterdir())), 2)
        self.assertTrue((out / "summary.md").read_text().startswith("| Measure | U | S |"))
        conditions = {item["condition"]: item["fired"] for item in summary["stop_conditions"]}
        self.assertFalse(any(conditions[name] for name in conditions if "cap" in name))
        self.assertIsNone(conditions["the reviewer guesses the arm correctly in more than 75% of cases"])

    def test_the_human_solution_baseline_shows_a_scope_that_its_own_solution_breaks(self) -> None:
        tight = make_task(self.base, self.head, allowed_paths=["pkg/calc.py"], max_files_changed=1)
        (row,) = pilot.human_baseline([tight])
        self.assertEqual(row["out_of_scope_paths"], ["automation/tests/test_calc.py"])
        self.assertFalse(row["within_budget"])


class FrozenTaskSetTests(unittest.TestCase):
    """The committed pilot task set, and a dry run of the pilot over it with placeholder scopes."""

    def setUp(self) -> None:
        self.data = read_json(TASKS_FILE)

    def test_the_frozen_set_has_six_tasks_that_passed_every_check_and_a_blinded_scope(self) -> None:
        tasks = self.data["tasks"]
        self.assertEqual(len(tasks), 6)
        self.assertEqual(len({task["number"] for task in tasks}), 6)
        for task in tasks:
            self.assertTrue(all(task["checks"].values()), task["number"])
            self.assertTrue(task["hidden_tests"])
            self.assertLessEqual(task["human_solution"]["files_changed"], select_module.MAX_FILES_CHANGED)
            self.assertFalse(any(path.startswith(select_module.EXCLUDED_PREFIXES) for path in task["human_solution"]["files"]))
            self.assertEqual(scope_errors(task), [], task["number"])
            # Written blind, from the pull request's description only, and recorded as such.
            self.assertIn("title and body only", task["scope_provenance"]["inputs"])
        kept = [entry["number"] for entry in self.data["audit"] if entry["decision"] == "kept"]
        self.assertEqual(kept, [task["number"] for task in tasks])
        self.assertTrue(all(entry["reasons"] for entry in self.data["audit"]))
        self.assertEqual(self.data["provenance"]["model_sessions"], 0)
        self.assertEqual(self.data["overlay"]["commit"], OVERLAY.commit)
        self.assertTrue(all(task["oracle"]["overlay"] == OVERLAY.commit for task in tasks))

    def test_a_dry_run_over_the_frozen_set_with_placeholder_scopes(self) -> None:
        env = isolated_environment(os.environ, Path(tempfile.mkdtemp(prefix="trackr-env-")))
        if not all(has_object(REPO_ROOT, task["head_sha"], env) for task in self.data["tasks"]):
            self.skipTest("this checkout doesn't hold the task set's commits (a shallow clone or a consumer's copy)")
        placeholder = {
            "task_statement": "PLACEHOLDER: filled in by this test only.",
            "allowed_paths": ["./"],
            "required_validations": ["true"],
            "max_files_changed": 15,
        }
        tasks = [{**task, **placeholder} for task in self.data["tasks"]]
        with tempfile.TemporaryDirectory(prefix="trackr-frozen-") as scratch:
            root = Path(scratch).resolve()
            runner = FakeRunner(edits={"TRACKR_DRY_RUN.txt": "placeholder\n"})
            overlay = Overlay.from_record(self.data["overlay"])
            config = driver.DriverConfig(runner=runner, environ={**os.environ, "HOME": str(root)}, work_dir=root, overlay=overlay)
            summary = pilot.run_pilot(tasks, REPO_ROOT, root / "out", config, oracle_fn=lambda *a, **k: _placeholder_verdict())
            self.assertEqual(summary["sessions_run"], 12)
            rows = [json.loads(line) for line in (root / "out" / "results.jsonl").read_text().splitlines()]
            first, second = (f"pr{task['number']}" for task in tasks[:2])
            self.assertEqual(
                [(row["task"], row["arm"]) for row in rows[:4]], [(first, "U"), (first, "S"), (second, "S"), (second, "U")]
            )
            records = [row["record"] for row in rows]
            self.assertTrue(all(record["overlay"]["commit"] == OVERLAY.commit for record in records))
            self.assertTrue(all(record["solution_absent_from_clone"] and record["remotes"] == [] for record in records))
            self.assertEqual(len(read_json(root / "out" / "mapping.json")["sessions"]), 12)


def _placeholder_verdict() -> dict[str, Any]:
    return {"hidden_tests_pass": False, "in_scope": True, "within_budget": True, "validations_passed": True}


if __name__ == "__main__":
    unittest.main()
