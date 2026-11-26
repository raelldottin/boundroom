"""How a lane spends its time budget, and what it records about each phase.

The clock is the only thing faked here besides the agent CLI: ``ExecutionContext.clock`` is
the seam, so a phase can "spend" an exact number of seconds without the test waiting for
them, and every grant and charge is asserted to the second instead of to a tolerance.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Mapping, Optional

from automation.benchmark.adapter import _default_command_template
from automation.benchmark.strategies import (
    AGENT_TIMEOUT_RETURNCODE,
    IMPLEMENT_PLAN_HANDOFF,
    LANE_CEILING_SECONDS,
    PHASE_CEILING_SECONDS,
    PHASE_CENSORED,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_TREATMENT_INVALID,
    PLAN_ARTIFACT,
    PLAN_COMPACT_ARTIFACT,
    PLAN_RESEARCH_HANDOFF,
    PLAN_SPEC,
    REQUIRED_ARTIFACT_MISSING,
    RESEARCH_ARTIFACT,
    RESEARCH_COMPACT_ARTIFACT,
    RESEARCH_SPEC,
    RPI_DIR,
    TREATMENT_INVALID,
    TREATMENT_VALID,
    ArtifactSpec,
    ExecutionContext,
    OneSessionStrategy,
    PhaseResult,
    RpiStrategy,
    SliceContextStrategy,
    StrategyResult,
    build_strategy,
    compose_prompt,
    phase_state,
    read_artifact,
    run_phase,
)
from automation.tests.test_benchmark_lanes import FIXTURE_ARTIFACT, REPO_ROOT, TASK, FakeAgent, TempDirCase, strategy_for


class FakeClock:
    """A monotonic clock that moves only when the test moves it."""

    def __init__(self, now: float = 5000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SpendingAgent(FakeAgent):
    """The fake agent CLI, spending simulated seconds per phase and ending phases with chosen codes."""

    def __init__(
        self,
        clock: FakeClock,
        spend: Optional[Mapping[str, float]] = None,
        returncodes: Optional[Mapping[str, int]] = None,
        withhold: tuple[str, ...] = (),
    ) -> None:
        super().__init__(withhold=withhold)
        self.clock = clock
        self.spend = dict(spend or {})
        self.returncodes = dict(returncodes or {})

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        returncode = super().__call__(command, workspace, env, timeout)
        phase = self.sessions[-1]["phase"]
        self.clock.advance(self.spend.get(phase, 0.0))
        return self.returncodes.get(phase, returncode)

    def granted(self) -> dict[str, int]:
        return {session["phase"]: session["timeout"] for session in self.sessions}


# What the fake agent's implement session writes before it builds (``FakeAgent``).
BUILT_SOURCE = "int main(void){return 0;}\n"


class BuildThenBreakAgent(SpendingAgent):
    """Implement records a build through the harness's checkpointed build, then breaks the tree."""

    workspace: Optional[Path] = None

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        returncode = super().__call__(command, workspace, env, timeout)
        if self.sessions[-1]["phase"] == "implement":
            self.workspace = Path(workspace)
            (self.workspace / "compile.sh").chmod(0o755)
            build = subprocess.run(["sh", str(self.workspace / RPI_DIR / "build.sh")], capture_output=True, text=True)
            if build.returncode != 0:
                raise RuntimeError(f"fixture build failed: {build.stderr}")
            (self.workspace / "main.c").write_text("int main(void){return undeclared;}\n", encoding="utf-8")
            (self.workspace / "half.c").write_text("/* interrupted */\n", encoding="utf-8")
        return returncode


class ClockedCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock()

    def context(self, agent: FakeAgent, timeout_seconds: int = LANE_CEILING_SECONDS) -> ExecutionContext:
        return ExecutionContext(
            repo_root=REPO_ROOT,
            task=TASK,
            workspace=self.directory(),
            control_dir=self.directory(),
            command_template=_default_command_template(REPO_ROOT),
            env={},
            timeout_seconds=timeout_seconds,
            runner=agent,
            clock=self.clock,
        )

    def agent(self, **kwargs) -> SpendingAgent:
        return SpendingAgent(self.clock, **kwargs)

    def run_lane(
        self, lane: str, agent: Optional[SpendingAgent] = None, timeout_seconds: int = LANE_CEILING_SECONDS
    ) -> tuple[SpendingAgent, StrategyResult]:
        agent = agent or self.agent()
        return agent, strategy_for(lane).execute(self.context(agent, timeout_seconds))


class RunPhaseAccountingTests(ClockedCase):
    """A phase is charged what it was granted; a killed session's shutdown is grace, not work."""

    def test_a_killed_session_is_charged_its_budget_and_the_overrun_is_grace(self) -> None:
        agent = self.agent(spend={"research": 7.5}, returncodes={"research": AGENT_TIMEOUT_RETURNCODE})
        result = run_phase(self.context(agent), "research", "prompt\n", {}, 5)

        self.assertEqual(5, agent.sessions[0]["timeout"])
        self.assertEqual((5.0, 2.5, 5), (result.seconds, result.grace_seconds, result.budget_seconds))
        self.assertEqual(PHASE_CENSORED, result.state)

    def test_a_session_that_ends_by_itself_is_charged_everything_it_spent(self) -> None:
        for returncode, spent in ((1, 7.5), (0, 3.25)):
            with self.subTest(returncode=returncode):
                agent = self.agent(spend={"research": spent}, returncodes={"research": returncode})
                result = run_phase(self.context(agent), "research", "prompt\n", {}, 5)
                self.assertEqual((spent, 0.0, 5), (result.seconds, result.grace_seconds, result.budget_seconds))
                self.assertEqual(returncode, result.returncode)

    def test_no_time_left_still_grants_the_session_one_second(self) -> None:
        for remaining in (0, -30):
            with self.subTest(remaining=remaining):
                agent = self.agent()
                result = run_phase(self.context(agent), "plan", "prompt\n", {}, remaining)
                self.assertEqual(1, agent.sessions[0]["timeout"])
                self.assertEqual(1, result.budget_seconds)

    def test_the_session_files_hold_exactly_what_the_agent_is_handed(self) -> None:
        agent = self.agent()
        ctx = self.context(agent)
        prompt = "# Réécrire ✓\n\nBuild it.\n"
        context = {"objective": "réécrire le programme ✓", "steps": [1, 2]}

        result = run_phase(ctx, "research", prompt, context, 60)

        control = Path(ctx.control_dir) / "research"
        expected = json.dumps(context, indent=2, ensure_ascii=False) + "\n"
        self.assertEqual(expected.encode("utf-8"), (control / "context.json").read_bytes())
        self.assertEqual(prompt, (control / "prompt.md").read_text(encoding="utf-8"))
        self.assertEqual(prompt, agent.sessions[0]["prompt"])
        self.assertEqual(len(prompt), result.prompt_chars)


class PhaseResultReportTests(unittest.TestCase):
    def test_a_phase_reports_every_field_in_order(self) -> None:
        expected = {
            "phase": "implement",
            "state": PHASE_COMPLETED,
            "returncode": 0,
            "seconds": 2.0,
            "grace_seconds": 0.0,
            "budget_seconds": 0,
            "prompt_chars": 7,
            "artifact_chars": 0,
            "artifact_valid": None,
            "artifact_errors": [],
            "discarded": [],
        }
        report = PhaseResult("implement", 0, 2.0, 7).to_dict()
        self.assertEqual(expected, report)
        self.assertEqual(list(expected), list(report))

    def test_a_lane_reports_every_field_in_order(self) -> None:
        expected = {
            "lane": "A",
            "returncode": 0,
            "agent_invocations": 0,
            "seconds": 0,
            "phase_failures": 0,
            "phases": [],
            "compaction": {},
            "jspace": None,
            "phase_budgets": {},
            "plan_research_handoff": None,
            "implement_plan_handoff": None,
            "implement_restore": None,
            "treatment_validity": TREATMENT_VALID,
            "treatment_invalid_reason": None,
            "treatment_invalid_phase": None,
        }
        report = StrategyResult(lane="A", returncode=0).to_dict()
        self.assertEqual(expected, report)
        self.assertEqual(list(expected), list(report))

    def test_seconds_are_reported_to_the_millisecond(self) -> None:
        phase = PhaseResult("plan", 0, 1.23456, 10, grace_seconds=0.98765)
        self.assertEqual((1.235, 0.988), (phase.to_dict()["seconds"], phase.to_dict()["grace_seconds"]))
        self.assertEqual(1.235, StrategyResult(lane="C", returncode=0, phases=(phase,)).to_dict()["seconds"])


class PhaseStateTests(unittest.TestCase):
    def test_how_a_phase_ended_and_what_it_left_decide_its_state(self) -> None:
        table = [
            ((0, None), PHASE_COMPLETED),
            ((0, True), PHASE_COMPLETED),
            ((1, None), PHASE_FAILED),
            ((AGENT_TIMEOUT_RETURNCODE, None), PHASE_CENSORED),
            ((AGENT_TIMEOUT_RETURNCODE, True), PHASE_CENSORED),
            ((0, False), PHASE_TREATMENT_INVALID),
            ((AGENT_TIMEOUT_RETURNCODE, False), PHASE_TREATMENT_INVALID),
        ]
        for (returncode, artifact_valid), state in table:
            with self.subTest(returncode=returncode, artifact_valid=artifact_valid):
                self.assertEqual(state, phase_state(returncode, artifact_valid))


class StrategyResultTests(unittest.TestCase):
    def test_a_failure_is_a_nonzero_exit_or_a_missing_artifact(self) -> None:
        phases = (
            PhaseResult("research", 0, 1.0, 1),
            PhaseResult("research_compact", 0, 1.0, 1, artifact_valid=True),
            PhaseResult("plan", 0, 1.0, 1, artifact_valid=True),
            PhaseResult("plan_compact", 1, 1.0, 1),
            PhaseResult("implement", AGENT_TIMEOUT_RETURNCODE, 1.0, 1, artifact_valid=True),
            PhaseResult("extra", 0, 1.0, 1, artifact_valid=False),
        )
        result = StrategyResult(lane="D", returncode=0, phases=phases)
        self.assertEqual(3, result.phase_failures)
        self.assertEqual(6, result.agent_invocations)

    def test_an_explicit_stop_reason_outranks_the_phases(self) -> None:
        result = StrategyResult(
            lane="C",
            returncode=0,
            phases=(PhaseResult("research", 0, 1.0, 1, artifact_valid=False),),
            treatment_invalid_reason="custom-reason",
            treatment_invalid_phase="plan",
        )
        report = result.to_dict()
        self.assertEqual(
            (TREATMENT_INVALID, "custom-reason", "plan"),
            (report["treatment_validity"], report["treatment_invalid_reason"], report["treatment_invalid_phase"]),
        )

    def test_without_one_the_first_phase_with_no_artifact_is_named(self) -> None:
        result = StrategyResult(
            lane="D",
            returncode=0,
            phases=(
                PhaseResult("research", 0, 1.0, 1, artifact_valid=True),
                PhaseResult("research_compact", 0, 1.0, 1, artifact_valid=False),
                PhaseResult("plan", 0, 1.0, 1, artifact_valid=False),
            ),
        )
        report = result.to_dict()
        self.assertEqual(
            (TREATMENT_INVALID, REQUIRED_ARTIFACT_MISSING, "research_compact"),
            (report["treatment_validity"], report["treatment_invalid_reason"], report["treatment_invalid_phase"]),
        )

    def test_an_explicit_reason_without_phases_still_invalidates_the_lane(self) -> None:
        result = StrategyResult(lane="C", returncode=0, treatment_invalid_reason=REQUIRED_ARTIFACT_MISSING)
        self.assertEqual(TREATMENT_INVALID, result.treatment_validity)


class ArtifactSpecTests(unittest.TestCase):
    SPEC = ArtifactSpec(filename="x.json", list_keys=("items",), text_keys=("goal",))

    def test_the_template_lists_text_keys_then_list_keys(self) -> None:
        template = RESEARCH_SPEC.template()
        self.assertEqual(
            {
                "task": "...",
                "relevant_files": [],
                "findings": [],
                "constraints": [],
                "unknowns": [],
                "risks": [],
                "evidence": [],
            },
            template,
        )
        self.assertEqual("task", next(iter(template)))

    def test_validate_names_every_problem(self) -> None:
        table = [
            ([], ["artifact is not a JSON object"]),
            ({"goal": "", "items": []}, ["missing or empty text field: goal"]),
            ({"goal": 3, "items": []}, ["missing or empty text field: goal"]),
            ({"goal": "g"}, ["missing or non-list field: items"]),
            ({"goal": "g", "items": "not a list"}, ["missing or non-list field: items"]),
            ({}, ["missing or empty text field: goal", "missing or non-list field: items"]),
            ({"goal": "g", "items": []}, []),
        ]
        for data, errors in table:
            with self.subTest(data=data):
                self.assertEqual(errors, self.SPEC.validate(data))

    def test_the_plan_spec_requires_a_goal(self) -> None:
        self.assertEqual(["missing or empty text field: goal"], PLAN_SPEC.validate({**PLAN_SPEC.template(), "goal": ""}))


class ComposePromptTests(unittest.TestCase):
    def test_the_jspace_skill_sits_between_the_title_and_the_rest(self) -> None:
        prompt = compose_prompt("# Research: owner/proj", "the envelope", "the instruction", jspace=FIXTURE_ARTIFACT)
        self.assertEqual(
            f"# Research: owner/proj\n\n{FIXTURE_ARTIFACT.prompt_block()}\n\nthe envelope\n\nthe instruction\n", prompt
        )

    def test_blank_sections_are_dropped_and_the_rest_trimmed(self) -> None:
        self.assertEqual("# Title\n\nbody\n", compose_prompt("  # Title\n", "", " \n\t", "body  "))


class ReadArtifactTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.directory()

    def _write(self, text: str) -> None:
        (self.workspace / RPI_DIR).mkdir(exist_ok=True)
        (self.workspace / RPI_DIR / RESEARCH_ARTIFACT).write_text(text, encoding="utf-8")

    def test_a_missing_artifact_reads_as_nothing(self) -> None:
        self.assertEqual((None, ""), read_artifact(self.workspace, RESEARCH_ARTIFACT))

    def test_an_unparseable_artifact_keeps_its_raw_text(self) -> None:
        self._write("{not json")
        self.assertEqual((None, "{not json"), read_artifact(self.workspace, RESEARCH_ARTIFACT))

    def test_a_parseable_artifact_comes_back_parsed_and_raw(self) -> None:
        self._write('{"task": "t"}')
        self.assertEqual(({"task": "t"}, '{"task": "t"}'), read_artifact(self.workspace, RESEARCH_ARTIFACT))


class StrategyNamingTests(unittest.TestCase):
    def test_the_treatment_names_the_lane_unless_a_name_is_given(self) -> None:
        self.assertEqual("C", RpiStrategy().name)
        self.assertEqual("D", RpiStrategy(compaction=True).name)
        self.assertEqual("E", RpiStrategy(jspace=FIXTURE_ARTIFACT).name)
        self.assertEqual("E", RpiStrategy(compaction=True, jspace=FIXTURE_ARTIFACT).name)
        self.assertEqual("X", RpiStrategy(compaction=True, name="X").name)

    def test_each_lane_builds_its_own_treatment(self) -> None:
        self.assertIsInstance(build_strategy("A"), OneSessionStrategy)
        self.assertIsInstance(build_strategy("B"), SliceContextStrategy)
        for lane, compaction in (("C", False), ("D", True)):
            with self.subTest(lane=lane):
                strategy = build_strategy(lane)
                assert isinstance(strategy, RpiStrategy)
                self.assertEqual((lane, compaction, None), (strategy.name, strategy.compaction, strategy.jspace))

    def test_an_unknown_lane_names_every_lane_there_is(self) -> None:
        with self.assertRaises(ValueError) as raised:
            build_strategy("Z")
        self.assertEqual("Unknown lane 'Z'; expected one of A, B, C, D, E", str(raised.exception))


class BudgetProfileTests(ClockedCase):
    """A greedy first phase must not be able to spend the phase that writes the code.

    In run 35650966066 every multi-phase lane submitted nothing: research was handed the
    whole remaining budget and lane D spent 1799 of 1800 seconds on it, leaving implement
    one second. Ceilings also hold the decomposition together - C must not inherit the
    compaction time D and E spend, or ``D - C`` measures more than compaction.
    """

    def test_single_session_lanes_get_the_whole_budget(self) -> None:
        for lane in ("A", "B"):
            with self.subTest(lane=lane):
                agent, result = self.run_lane(lane)
                self.assertEqual({"implement": LANE_CEILING_SECONDS}, agent.granted())
                self.assertEqual({"implement": LANE_CEILING_SECONDS}, result.phase_budgets)

    def test_every_multi_phase_lane_runs_each_phase_at_its_ceiling(self) -> None:
        for lane, phases in (
            ("C", ["research", "plan", "implement"]),
            ("D", ["research", "research_compact", "plan", "plan_compact", "implement"]),
            ("E", ["research", "research_compact", "plan", "plan_compact", "implement"]),
        ):
            with self.subTest(lane=lane):
                agent, result = self.run_lane(lane)
                self.assertEqual(phases, agent.phases())
                self.assertEqual({phase: PHASE_CEILING_SECONDS[phase] for phase in phases}, agent.granted())
                # The profile is part of the treatment, so it travels with the result.
                self.assertEqual(PHASE_CEILING_SECONDS, result.phase_budgets)

    def test_c_does_not_reclaim_the_compaction_slots(self) -> None:
        agent, _ = self.run_lane("C")
        forfeited = PHASE_CEILING_SECONDS["research_compact"] + PHASE_CEILING_SECONDS["plan_compact"]
        self.assertEqual(LANE_CEILING_SECONDS - forfeited, sum(agent.granted().values()))

    def test_implement_keeps_its_allowance_when_research_spends_its_own(self) -> None:
        agent, _ = self.run_lane("C", self.agent(spend={"research": PHASE_CEILING_SECONDS["research"]}))
        self.assertEqual(PHASE_CEILING_SECONDS["implement"], agent.granted()["implement"])

    def test_a_smaller_budget_scales_every_ceiling_down_and_never_over(self) -> None:
        expected = {"research": 181, "research_compact": 90, "plan": 181, "plan_compact": 90, "implement": 454}
        agent, result = self.run_lane("D", timeout_seconds=1000)
        self.assertEqual(expected, result.phase_budgets)
        self.assertEqual(expected, agent.granted())
        self.assertLessEqual(sum(agent.granted().values()), 1000)

    def test_half_the_lane_ceiling_halves_the_profile(self) -> None:
        _, result = self.run_lane("D", timeout_seconds=LANE_CEILING_SECONDS // 2)
        self.assertEqual({phase: seconds // 2 for phase, seconds in PHASE_CEILING_SECONDS.items()}, result.phase_budgets)

    def test_a_one_second_budget_today_grants_each_phase_its_one_second_floor(self) -> None:
        """Records current behaviour, not a property to defend.

        ``_scaled_ceilings`` never scales a ceiling below one second, so under a one-second
        instance budget the five phases are granted five seconds between them: more than the
        budget. The test pins the floor so a change to it is a decision, not a side effect.
        """
        agent, result = self.run_lane("D", timeout_seconds=1)
        self.assertEqual({phase: 1 for phase in PHASE_CEILING_SECONDS}, result.phase_budgets)
        self.assertEqual({phase: 1 for phase in PHASE_CEILING_SECONDS}, agent.granted())


class ExhaustedBudgetTests(ClockedCase):
    """The instance budget is the backstop: a phase that overruns its kill takes what is left."""

    def test_one_second_left_after_research_is_all_plan_and_implement_get(self) -> None:
        agent, result = self.run_lane("C", self.agent(spend={"research": LANE_CEILING_SECONDS - 1}))
        self.assertEqual({"research": PHASE_CEILING_SECONDS["research"], "plan": 1, "implement": 1}, agent.granted())
        self.assertEqual(TREATMENT_VALID, result.treatment_validity)

    def test_no_time_left_after_research_stops_the_lane_before_plan(self) -> None:
        agent = self.agent(spend={"research": LANE_CEILING_SECONDS}, returncodes={"research": AGENT_TIMEOUT_RETURNCODE})
        agent, result = self.run_lane("C", agent)

        self.assertEqual(["research"], agent.phases())
        report = result.to_dict()
        self.assertEqual(
            (TREATMENT_INVALID, REQUIRED_ARTIFACT_MISSING, "plan"),
            (report["treatment_validity"], report["treatment_invalid_reason"], report["treatment_invalid_phase"]),
        )
        self.assertEqual(AGENT_TIMEOUT_RETURNCODE, result.returncode)
        self.assertIsNone(result.plan_research_handoff)
        self.assertIsNone(result.implement_plan_handoff)

    def test_d_out_of_time_after_research_skips_compaction(self) -> None:
        agent, result = self.run_lane("D", self.agent(spend={"research": LANE_CEILING_SECONDS}))
        self.assertEqual(["research"], agent.phases())
        self.assertEqual({}, result.compaction)
        self.assertEqual("plan", result.to_dict()["treatment_invalid_phase"])

    def test_d_out_of_time_after_compacting_research_stops_before_plan(self) -> None:
        agent, result = self.run_lane("D", self.agent(spend={"research_compact": LANE_CEILING_SECONDS}))
        self.assertEqual(["research", "research_compact"], agent.phases())
        self.assertEqual("plan", result.to_dict()["treatment_invalid_phase"])
        self.assertEqual(REQUIRED_ARTIFACT_MISSING, result.to_dict()["treatment_invalid_reason"])

    def test_d_out_of_time_after_plan_skips_its_compaction_and_implement_gets_a_second(self) -> None:
        agent, result = self.run_lane("D", self.agent(spend={"plan": LANE_CEILING_SECONDS}))
        self.assertEqual(["research", "research_compact", "plan", "implement"], agent.phases())
        self.assertEqual(1, agent.granted()["implement"])
        self.assertEqual({"mode": IMPLEMENT_PLAN_HANDOFF, "artifact": PLAN_ARTIFACT}, result.implement_plan_handoff)


class RpiPathTests(ClockedCase):
    def artifact_chars(self, phase: str, filename: str) -> int:
        """How long the artifact the fake agent writes for ``phase`` is (two-space JSON, ASCII)."""
        workspace = self.directory()
        FakeAgent()._write_artifact(workspace, phase)
        return len((workspace / RPI_DIR / filename).read_text(encoding="utf-8"))

    def test_compaction_records_its_sizes(self) -> None:
        research = self.artifact_chars("research", RESEARCH_ARTIFACT)
        research_compact = self.artifact_chars("research_compact", RESEARCH_COMPACT_ARTIFACT)
        plan = self.artifact_chars("plan", PLAN_ARTIFACT)
        plan_compact = self.artifact_chars("plan_compact", PLAN_COMPACT_ARTIFACT)
        # The ratio is kept to four decimals. These sizes round differently at three, four and
        # five, so the expectation below would not hold at any other precision.
        for compacted, raw in ((research_compact, research), (plan_compact, plan)):
            self.assertEqual(3, len({round(compacted / raw, digits) for digits in (3, 4, 5)}))

        _, result = self.run_lane("D")

        self.assertEqual(
            {
                "research_raw_chars": research,
                "research_compacted_chars": research_compact,
                "research_compression_ratio": round(research_compact / research, 4),
                "plan_raw_chars": plan,
                "plan_compacted_chars": plan_compact,
                "plan_compression_ratio": round(plan_compact / plan, 4),
            },
            result.compaction,
        )

    def test_a_compaction_that_never_landed_hands_plan_the_raw_research(self) -> None:
        agent, result = self.run_lane("D", self.agent(withhold=("research_compact",)))

        self.assertIn(f"`{RPI_DIR}/{RESEARCH_ARTIFACT}`", agent.prompt_for("plan"))
        self.assertNotIn(RESEARCH_COMPACT_ARTIFACT, agent.prompt_for("plan"))
        self.assertEqual({"mode": PLAN_RESEARCH_HANDOFF, "artifact": RESEARCH_ARTIFACT}, result.plan_research_handoff)
        self.assertEqual(
            (self.artifact_chars("research", RESEARCH_ARTIFACT), 0, 0.0),
            (
                result.compaction["research_raw_chars"],
                result.compaction["research_compacted_chars"],
                result.compaction["research_compression_ratio"],
            ),
        )
        # The lane ran on, but the compaction phase still reports that it was not administered.
        self.assertEqual("implement", agent.phases()[-1])
        self.assertEqual("research_compact", result.to_dict()["treatment_invalid_phase"])

    def test_a_missing_plan_stops_the_lane_and_keeps_plans_exit_code(self) -> None:
        agent, result = self.run_lane("C", self.agent(withhold=("plan",), returncodes={"plan": 3}))

        self.assertEqual(["research", "plan"], agent.phases())
        self.assertEqual(3, result.returncode)
        report = result.to_dict()
        self.assertEqual(
            (TREATMENT_INVALID, REQUIRED_ARTIFACT_MISSING, "plan"),
            (report["treatment_validity"], report["treatment_invalid_reason"], report["treatment_invalid_phase"]),
        )
        self.assertEqual({"mode": PLAN_RESEARCH_HANDOFF, "artifact": RESEARCH_ARTIFACT}, result.plan_research_handoff)
        self.assertIsNone(result.implement_plan_handoff)

    def test_a_killed_implement_with_no_recorded_build_says_so_and_a_finished_one_restores_nothing(self) -> None:
        _, killed = self.run_lane("C", self.agent(returncodes={"implement": AGENT_TIMEOUT_RETURNCODE}))
        self.assertEqual({"restored": False, "reason": "no successful build was recorded"}, killed.implement_restore)

        _, finished = self.run_lane("C")
        self.assertIsNone(finished.implement_restore)

    def test_a_killed_implement_is_put_back_to_its_last_recorded_build(self) -> None:
        agent = BuildThenBreakAgent(self.clock, returncodes={"implement": AGENT_TIMEOUT_RETURNCODE})

        _, result = self.run_lane("C", agent)

        self.assertEqual({"restored": True, "entries": ["compile.sh", "main.c"]}, result.implement_restore)
        assert agent.workspace is not None
        self.assertEqual(BUILT_SOURCE, (agent.workspace / "main.c").read_text(encoding="utf-8"))
        self.assertFalse((agent.workspace / "half.c").exists())


if __name__ == "__main__":
    unittest.main()
