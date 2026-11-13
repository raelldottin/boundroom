"""Claude runner profiles, the ceiling, the slice allowlist and no nested runs (SPEC-008 PS-11 to PS-21)."""

from __future__ import annotations

import fcntl
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Optional

from automation.supervisor import runner_profile as rp

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_AGENT = REPO_ROOT / "automation/supervisor/run_agent.sh"
SLICE = {
    "slice_id": "S-1",
    "allowed_paths": ["src/**", "docs/guide.md"],
    "required_validations": ["make test"],
}
POLICY = {"supervisor_owned_paths": ["automation/queue/**", "AGENTS.md"]}


def choose(requested: str, parent: Optional[str] = None, **environ: str) -> rp.RunnerProfile:
    return rp.choose({"runner_profile": requested}, environ, parent)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="runner-profile-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tree(self, *processes: tuple[str, str]) -> str:
        """A fake process tree for the ``ps`` double, nearest first; returns the first pid."""
        lines = []
        for depth, (comm, args) in enumerate(processes):
            parent = depth + 201 if depth + 1 < len(processes) else 0
            lines.append(f"{depth + 200}\t{parent}\t{comm}\t{args}")
        path = self.tmp / "tree.tsv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["FAKE_PS_TREE"] = str(path)
        return "200"


class ParentResolutionTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        previous = os.environ.get("FAKE_PS_TREE")
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("FAKE_PS_TREE", previous) if previous is not None else os.environ.pop("FAKE_PS_TREE", None)
            )
        )

    def test_ps16_only_the_two_permission_flags_are_read(self) -> None:
        cases = {
            "claude --permission-mode bypassPermissions --settings x.json": "bypassPermissions",
            "claude --permission-mode=acceptEdits": "acceptEdits",
            "claude --dangerously-skip-permissions --permission-mode default": "bypassPermissions",
            "claude --print --model opus": "default",
            "claude --permission-prompt-tool=stdio --permission-mode plan": "plan",
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                self.assertEqual(expected, rp.launch_mode(args))

    def test_ps16_the_nearest_claude_ancestor_decides(self) -> None:
        start = self.tree(
            ("python3", "python3 run_next.py"), ("/opt/claude/versions/2.1.3", "claude --permission-mode acceptEdits")
        )
        self.assertEqual("acceptEdits", rp.parent_launch_mode(int(start)))

    def test_ps17_no_claude_ancestor_or_a_nearer_harness_means_unknown(self) -> None:
        start = self.tree(("-zsh", "-zsh"), ("/usr/bin/login", "/usr/bin/login -fpl someone"))
        self.assertIsNone(rp.parent_launch_mode(int(start)))
        start = self.tree(
            ("/opt/hermes/venv/bin/python3", "/opt/hermes/venv/bin/python3 /opt/hermes/venv/bin/hermes chat"),
            ("/opt/claude/versions/2.1.3", "claude --dangerously-skip-permissions"),
        )
        self.assertIsNone(rp.parent_launch_mode(int(start)))


class ChoiceTests(unittest.TestCase):
    def test_ps16_each_parent_mode_maps_to_its_row(self) -> None:
        rows = {
            "bypassPermissions": ("unbounded", "edits"),
            "acceptEdits": ("edits", "edits"),
            "default": ("bounded", "bounded"),
            "dontAsk": ("bounded", "bounded"),
            "auto": ("edits", "edits"),
        }
        for parent, (resolved, effective) in rows.items():
            with self.subTest(parent=parent):
                profile = choose("parent", parent)
                self.assertEqual((resolved, effective), (profile.resolved, profile.effective))
                self.assertEqual(rp.MODES[effective], profile.mode)

    def test_ps19_a_plan_mode_parent_starts_nothing(self) -> None:
        with self.assertRaises(rp.ProfileRefused):
            choose("parent", "plan")

    def test_ps17_an_unknown_parent_falls_back_to_bounded_with_the_sandbox(self) -> None:
        profile = choose("parent", None)
        self.assertEqual(("bounded", True), (profile.effective, profile.sandbox))
        self.assertIn("fallback", " ".join(profile.reasons))

    def test_a_mode_this_code_does_not_know_also_gets_the_sandbox(self) -> None:
        profile = choose("parent", "someFutureMode")
        self.assertEqual(("bounded", True), (profile.effective, profile.sandbox))

    def test_claude_code_21_manual_mode_resolves_like_default(self) -> None:
        # The RA-SET-009 canary: a Manual parent reported "manual", which fell through to the fallback.
        for mode in ("manual", "default"):
            profile = choose("parent", mode)
            self.assertEqual(("bounded", "dontAsk", False), (profile.effective, profile.mode, profile.sandbox))
            self.assertIn(f"the parent runs {mode}", " ".join(profile.reasons))

    def test_ps18_the_effective_profile_is_the_lower_of_request_and_ceiling(self) -> None:
        for requested, ceiling in itertools.product(("bounded", "edits", "unbounded"), rp.CEILINGS):
            with self.subTest(requested=requested, ceiling=ceiling):
                profile = choose(requested, BOUNDROOM_CLAUDE_PROFILE_CEILING=ceiling)
                expected = min(requested, ceiling, key=lambda name: rp._RANK[name])
                self.assertEqual(expected, profile.effective)

    def test_ps13_unbounded_needs_the_isolation_declaration_or_an_explicit_ceiling(self) -> None:
        with self.assertRaises(rp.ProfileRefused) as caught:
            choose("unbounded")
        self.assertIn("BOUNDROOM_CLAUDE_ISOLATED", str(caught.exception))
        self.assertEqual("unbounded", choose("unbounded", BOUNDROOM_CLAUDE_ISOLATED="1").effective)
        self.assertEqual("unbounded", choose("unbounded", BOUNDROOM_CLAUDE_PROFILE_CEILING="unbounded").effective)

    def test_a_bypass_parent_is_lowered_to_edits_unless_isolated(self) -> None:
        self.assertEqual("parent → unbounded → edits (ceiling)", choose("parent", "bypassPermissions").chain())
        self.assertEqual("unbounded", choose("parent", "bypassPermissions", BOUNDROOM_CLAUDE_ISOLATED="1").effective)

    def test_custom_needs_its_file_and_is_not_capped(self) -> None:
        with self.assertRaises(rp.ProfileRefused):
            choose("custom")
        profile = choose("custom", BOUNDROOM_CLAUDE_SETTINGS="/etc/mine.json", BOUNDROOM_CLAUDE_PROFILE_CEILING="bounded")
        self.assertEqual(("custom", None), (profile.effective, profile.mode))

    def test_unknown_values_are_refused(self) -> None:
        with self.assertRaises(rp.ProfileRefused):
            rp.choose({}, {"BOUNDROOM_CLAUDE_PROFILE": "yolo"}, None)
        with self.assertRaises(rp.ProfileRefused):
            choose("edits", BOUNDROOM_CLAUDE_PROFILE_CEILING="everything")

    def test_bounded_is_the_default_and_only_the_mode_override_alone_turns_profiles_off(self) -> None:
        """SPEC-008 decision 1 (2026-11-12): bounded from v0.4.0 (RA-SET-010)."""
        self.assertTrue(rp.configured({}, {}))
        default = rp.choose({}, {}, None)
        self.assertEqual(
            ("bounded", "default", "dontAsk", True), (default.requested, default.requested_source, default.mode, default.sandbox)
        )
        self.assertFalse(rp.configured({}, {"BOUNDROOM_CLAUDE_PERMISSION_MODE": "acceptEdits"}))
        both = {"BOUNDROOM_CLAUDE_PERMISSION_MODE": "acceptEdits", "BOUNDROOM_CLAUDE_PROFILE": "edits"}
        self.assertTrue(rp.configured({}, both))
        self.assertTrue(rp.configured({"runner_profile": "edits"}, {"BOUNDROOM_CLAUDE_PERMISSION_MODE": "acceptEdits"}))

    def test_the_ceiling_flag_wins_over_its_variable(self) -> None:
        profile = rp.choose(
            {"runner_profile": "edits", "runner_profile_ceiling": "bounded"}, {"BOUNDROOM_CLAUDE_PROFILE_CEILING": "edits"}, None
        )
        self.assertEqual(("bounded", "flag --runner-profile-ceiling"), (profile.effective, profile.ceiling_source))

    def test_the_process_tree_is_read_only_for_the_parent_profile(self) -> None:
        with unittest.mock.patch.object(rp, "parent_launch_mode", side_effect=AssertionError("walked the tree")):
            rp.choose({}, {})
        with unittest.mock.patch.object(rp, "parent_launch_mode", return_value="acceptEdits") as walk:
            self.assertEqual("edits", rp.choose({"runner_profile": "parent"}, {}).effective)
        walk.assert_called_once()

    def test_the_way_back_to_bypass_is_unbounded_and_isolated(self) -> None:
        profile = rp.choose({}, {"BOUNDROOM_CLAUDE_PROFILE": "unbounded", "BOUNDROOM_CLAUDE_ISOLATED": "1"}, None)
        self.assertEqual("bypassPermissions", profile.mode)

    def test_ps21_the_record_names_every_step(self) -> None:
        record = choose("parent", "bypassPermissions").record()
        self.assertEqual(
            ("parent", "bypassPermissions", "unbounded", "edits", "edits", "acceptEdits"),
            (
                record["requested"],
                record["parent_mode"],
                record["resolved"],
                record["ceiling"],
                record["effective"],
                record["permission_mode"],
            ),
        )
        self.assertTrue(record["reasons"])


class SliceSettingsTests(TempCase):
    def test_ps11_the_allowlist_is_a_pure_function_of_the_slice_and_policy(self) -> None:
        profile = choose("edits")
        first = rp.slice_settings(SLICE, POLICY, profile)
        self.assertEqual(first, rp.slice_settings(json.loads(json.dumps(SLICE)), dict(POLICY), profile))
        allow = first["permissions"]["allow"]
        self.assertIn("Edit(src/**)", allow)
        self.assertIn("Edit(docs/guide.md)", allow)
        self.assertIn("Bash(make test)", allow)
        self.assertIn("Bash(git push)", allow)
        self.assertEqual("acceptEdits", first["permissions"]["defaultMode"])

    def test_ps12_owned_paths_git_claude_and_secrets_are_denied(self) -> None:
        deny = rp.slice_settings({**SLICE, "allowed_paths": ["**"]}, POLICY, choose("edits"))["permissions"]["deny"]
        for rule in (
            "Edit(AGENTS.md)",
            "Edit(automation/queue/**)",
            "Edit(.git/**)",
            "Edit(.claude/**)",
            "Read(~/.ssh/**)",
            "WebFetch",
        ):
            self.assertIn(rule, deny)

    def test_only_edit_rules_name_files_because_claude_code_ignores_write_rules(self) -> None:
        # The RA-SET-009 canary: Claude Code warned that Write(path) rules are not matched by file
        # permission checks, and that Edit rules cover every file-editing tool.
        permissions = rp.slice_settings(SLICE, POLICY, choose("bounded"))["permissions"]
        self.assertFalse([rule for rule in permissions["allow"] + permissions["deny"] if rule.startswith("Write(")])

    def test_the_runner_may_write_its_handoff_and_nothing_else_outside_its_paths(self) -> None:
        # The RA-SET-009 canary: a bounded runner couldn't write its handoff, so no slice could pass.
        handoff = "automation/handoffs/20261112T000000Z-S-1.json"
        allow = rp.slice_settings(SLICE, POLICY, choose("bounded"), handoff)["permissions"]["allow"]
        self.assertIn(f"Edit({handoff})", allow)
        self.assertNotIn("Edit(automation/handoffs/**)", allow)
        self.assertNotIn(f"Edit({handoff})", rp.slice_settings(SLICE, POLICY, choose("bounded"))["permissions"]["allow"])
        with rp.runner_environment(SLICE, POLICY, choose("bounded"), {}, handoff=handoff) as env:
            written = json.loads(Path(env[rp.RUNNER_SETTINGS_ENV]).read_text(encoding="utf-8"))
        self.assertIn(f"Edit({handoff})", written["permissions"]["allow"])

    def test_a_bounded_runner_can_commit_and_check_its_upstream(self) -> None:
        # The RA-SET-009 canary: the sandbox turned the .git deny rule into a write block, so
        # `git commit` failed, and `git rev-list` (the prompt's clean-stop check) was denied.
        settings = rp.slice_settings(SLICE, POLICY, choose("bounded"))
        # Without " *" an entry matches only the bare command, which is why the second canary still failed.
        self.assertEqual(["git add *", "git commit *", "git push"], settings["sandbox"]["excludedCommands"])
        self.assertIn("Bash(git rev-list:*)", settings["permissions"]["allow"])
        self.assertIn("Edit(.git/**)", settings["permissions"]["deny"])  # the file tools still can't touch .git

    def test_the_prompt_lists_what_the_runner_may_run_and_how_to_commit(self) -> None:
        # The third canary: a chained `git add && git commit && git push` stayed in the sandbox and
        # didn't commit, while the same commands run one at a time did.
        settings = rp.slice_settings(SLICE, POLICY, choose("bounded"), "automation/handoffs/h.json")
        notes = rp.runner_notes(settings)
        self.assertTrue(notes.startswith("## What you may run\n"))
        for rule in settings["permissions"]["allow"]:
            self.assertIn(f"- `{rule}`", notes)
        self.assertIn("as separate commands", notes)
        self.assertIn("outside this run's sandbox", notes)
        self.assertNotIn("sandbox", rp.runner_notes(rp.slice_settings(SLICE, POLICY, choose("edits"))))

    def test_ps14_bounded_fails_closed_without_its_sandbox(self) -> None:
        sandbox = rp.slice_settings(SLICE, POLICY, choose("bounded"))["sandbox"]
        self.assertEqual(
            (True, True, False), (sandbox["enabled"], sandbox["failIfUnavailable"], sandbox["allowUnsandboxedCommands"])
        )
        self.assertEqual([], sandbox["network"]["allowedDomains"])
        self.assertNotIn("sandbox", rp.slice_settings(SLICE, POLICY, choose("edits")))

    def test_ps15_the_settings_file_lives_outside_the_repository_and_is_removed(self) -> None:
        with rp.runner_environment(SLICE, POLICY, choose("edits"), {}) as env:
            path = Path(env[rp.RUNNER_SETTINGS_ENV])
            self.assertTrue(path.exists())
            self.assertNotIn(REPO_ROOT, path.resolve().parents)
            self.assertEqual("acceptEdits", env[rp.RUNNER_MODE_ENV])
        self.assertFalse(path.exists())

    def test_custom_passes_the_operators_file_and_no_mode(self) -> None:
        environ = {"BOUNDROOM_CLAUDE_SETTINGS": "/etc/mine.json"}
        with rp.runner_environment(SLICE, POLICY, rp.choose({"runner_profile": "custom"}, environ, None), environ) as env:
            self.assertEqual("/etc/mine.json", env[rp.RUNNER_SETTINGS_ENV])
            self.assertNotIn(rp.RUNNER_MODE_ENV, env)


class RunAgentTests(TempCase):
    """run_agent.sh passes the profile to Claude Code, and keeps today's flags without one."""

    def run_claude(self, **extra: str) -> list[str]:
        capture = self.tmp / "args.txt"
        fake = self.tmp / "claude"
        fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{capture}"\n', encoding="utf-8")
        fake.chmod(0o755)
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (self.tmp / "prompt.md").write_text("prompt", encoding="utf-8")
        (self.tmp / "context.json").write_text("{}", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BOUNDROOM_", "REPO_AUTOMATION_"))}
        env.update({"FAKE_PS_TREE": "", "BOUNDROOM_AGENT_RUNNER": "claude", "BOUNDROOM_CLAUDE_BIN": str(fake), **extra})
        result = subprocess.run(
            [str(RUN_AGENT), "--repo-root", str(repo), "--prompt-file", str(self.tmp / "prompt.md"),
             "--context-file", str(self.tmp / "context.json"), "--handoff-file", str(self.tmp / "handoff.json"), "--slice-id", "S-1"],
            env=env, capture_output=True, text=True, cwd=REPO_ROOT,
        )  # fmt: skip
        self.assertEqual(0, result.returncode, result.stderr)
        return capture.read_text(encoding="utf-8").split("\n")

    def test_a_profile_sets_mode_settings_and_budget(self) -> None:
        args = self.run_claude(
            BOUNDROOM_CLAUDE_RUNNER_SETTINGS="/tmp/s.json",
            BOUNDROOM_CLAUDE_RUNNER_MODE="dontAsk",
            BOUNDROOM_CLAUDE_RUNNER_BUDGET_USD="5",
        )
        for flag, value in (("--settings", "/tmp/s.json"), ("--permission-mode", "dontAsk"), ("--max-budget-usd", "5")):
            self.assertEqual(value, args[args.index(flag) + 1])

    def test_without_a_profile_the_runner_is_unchanged(self) -> None:
        args = self.run_claude()
        self.assertEqual("bypassPermissions", args[args.index("--permission-mode") + 1])
        self.assertNotIn("--settings", args)


class NestedRunTests(TempCase):
    """PS-20: with the slice variable set, or the run lock held, nothing starts another run."""

    def queue(self) -> Path:
        path = self.tmp / "slices.json"
        shutil.copy(REPO_ROOT / "automation/examples/example-slices.json", path)
        return path

    def invoke(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "XDG_STATE_HOME": str(self.tmp / "state")}
        # The board CLI runs as a module; the entry points run as scripts.
        target = ["-m", script] if not script.endswith(".py") else [str(REPO_ROOT / script)]
        return subprocess.run(
            [sys.executable, *target, *args], env={**base, **env}, capture_output=True, text=True, cwd=REPO_ROOT
        )

    def test_run_next_refuses_inside_a_slice_by_every_name(self) -> None:
        queue = self.queue()
        before = queue.read_bytes()
        for name in rp.NESTED_RUN_ENV:
            with self.subTest(name=name):
                result = self.invoke("automation/supervisor/run_next.py", "--queue", str(queue), "--dry-run", **{name: "S-9"})
                self.assertEqual(78, result.returncode, result.stderr)
                self.assertIn(name, result.stderr)
        self.assertEqual(before, queue.read_bytes())

    def test_run_next_refuses_while_another_run_holds_the_lock(self) -> None:
        queue = self.queue()
        before = queue.read_bytes()
        lock = rp.lock_path({"XDG_STATE_HOME": str(self.tmp / "state")})
        lock.parent.mkdir(parents=True)
        with lock.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = self.invoke("automation/supervisor/run_next.py", "--queue", str(queue))
        self.assertEqual(78, result.returncode, result.stderr)
        self.assertIn("holds", result.stderr)
        self.assertEqual(before, queue.read_bytes())

    def test_evaluate_handoff_refuses_inside_a_slice_unless_dry_run(self) -> None:
        args = ("--slice-id", "S-1", "--handoff", "h.json", "--base-sha", "a", "--head-sha", "b", "--queue", str(self.queue()))
        refused = self.invoke("automation/supervisor/evaluate_handoff.py", *args, BOUNDROOM_SUPERVISOR_SLICE_ID="S-9")
        self.assertEqual(78, refused.returncode, refused.stderr)
        dry = self.invoke("automation/supervisor/evaluate_handoff.py", *args, "--dry-run", BOUNDROOM_SUPERVISOR_SLICE_ID="S-9")
        self.assertNotIn("supervised slice", dry.stderr)

    def test_board_writes_refuse_inside_a_slice_but_reads_work(self) -> None:
        board = self.tmp / "board"
        self.assertEqual(0, self.invoke("automation.supervisor.local_board", "--board", str(board), "init").returncode)
        before = (board / "board.db").read_bytes() if (board / "board.db").exists() else None
        refused = self.invoke(
            "automation.supervisor.local_board", "--board", str(board), "add", "task", BOUNDROOM_SUPERVISOR_SLICE_ID="S-9"
        )
        self.assertEqual(78, refused.returncode, refused.stderr)
        listed = self.invoke(
            "automation.supervisor.local_board", "--board", str(board), "list", BOUNDROOM_SUPERVISOR_SLICE_ID="S-9"
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        if before is not None:
            self.assertEqual(before, (board / "board.db").read_bytes())

    def test_ps13_a_refused_profile_stops_before_the_claim(self) -> None:
        queue = self.queue()
        before = queue.read_bytes()
        result = self.invoke("automation/supervisor/run_next.py", "--queue", str(queue), "--runner-profile", "unbounded")
        self.assertEqual(78, result.returncode, result.stderr)
        self.assertIn("BOUNDROOM_CLAUDE_ISOLATED", result.stderr)
        self.assertEqual(before, queue.read_bytes())


if __name__ == "__main__":
    unittest.main()
