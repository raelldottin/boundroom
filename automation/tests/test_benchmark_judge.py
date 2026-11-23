from __future__ import annotations

import io
import json
import os
import shlex
import tarfile
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path
from typing import Mapping, Optional

from automation.benchmark import judge as judge_module
from automation.benchmark import lanes as lanes_module
from automation.benchmark import run as run_module
from automation.benchmark.adapter import SupervisorAgentAdapter
from automation.benchmark.evalrunner import RecordedEvalRunner
from automation.benchmark.instances import TaskSpec
from automation.benchmark.judge import Judge, StopGate, parse_verdict
from automation.benchmark.scoring import InstanceScore
from automation.benchmark.strategies import build_strategy
from automation.tests.test_benchmark_lanes import FakeAgent

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")
PASSING_EVAL = {"test_results": [{"name": f"t{n}", "status": "passed"} for n in range(20)]}
FAILING_EVAL = {
    "test_results": [{"name": "t0", "status": "passed"}] + [{"name": f"t{n}", "status": "failed"} for n in range(1, 20)]
}


def _options(command: str) -> dict[str, str]:
    argv = shlex.split(command)
    return dict(zip(argv, argv[1:]))


class FakeJudge:
    """Stand in for the judge's agent session: reads its prompt, writes a verdict."""

    def __init__(self, verdict: Optional[dict] = None, raw: Optional[str] = None, returncode: int = 0) -> None:
        self.verdict = verdict if verdict is not None else {"score": 8, "good_enough": True, "reasons": ["clean"]}
        self.raw = raw
        self.returncode = returncode
        self.calls: list[dict] = []

    def __call__(self, command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
        options = _options(command)
        self.calls.append(
            {
                "command": command,
                "options": options,
                "prompt": Path(options["--prompt-file"]).read_text(encoding="utf-8"),
                "workspace": Path(workspace),
                "files": sorted(p.relative_to(workspace).as_posix() for p in Path(workspace).rglob("*") if ".git" not in p.parts),
                "env": dict(env),
                "timeout": timeout,
            }
        )
        if self.raw is not None or self.verdict:
            text = self.raw if self.raw is not None else json.dumps(self.verdict)
            (Path(workspace) / judge_module.VERDICT_FILENAME).write_text(text, encoding="utf-8")
        return self.returncode


def _submission(path: Path, files: Mapping[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _cell(run_dir: Path, lane: str, repeat: int, instance: str, eval_data: Optional[dict], **provenance) -> Path:
    cell_dir = run_dir / lane / f"r{repeat}" / instance
    _submission(cell_dir / "submission.tar.gz", {"main.c": "int main(void){return 0;}\n", "compile.sh": "cc main.c\n"})
    record = {"lane": lane, "repeat": repeat, "instance_id": instance, "provider_validity": "valid"}
    record.update(provenance)
    (cell_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    if eval_data is not None:
        (cell_dir / f"{instance}.eval.json").write_text(json.dumps(eval_data), encoding="utf-8")
    return cell_dir


class ParseVerdictTests(unittest.TestCase):
    def _parse(self, text: str) -> judge_module.Verdict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdict.json"
            path.write_text(text, encoding="utf-8")
            return parse_verdict(path)

    def test_a_valid_verdict_passes_at_or_above_the_minimum_score(self) -> None:
        verdict = self._parse(json.dumps({"score": 7, "good_enough": True, "reasons": ["ok"]}))
        self.assertIsNone(verdict.error)
        self.assertTrue(verdict.passes(7))
        self.assertFalse(verdict.passes(8))

    def test_good_enough_false_never_passes_whatever_the_score(self) -> None:
        self.assertFalse(self._parse(json.dumps({"score": 10, "good_enough": False})).passes(0))

    def test_a_missing_verdict_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = parse_verdict(Path(tmp) / "verdict.json")
        self.assertEqual("verdict_missing", verdict.error)
        self.assertFalse(verdict.passes(0))

    def test_malformed_verdicts_are_errors_not_passes(self) -> None:
        cases = {
            "not json": "verdict_not_json",
            "[1]": "verdict_not_an_object",
            json.dumps({"score": True, "good_enough": True}): "verdict_invalid_score",
            json.dumps({"score": 11, "good_enough": True}): "verdict_invalid_score",
            json.dumps({"score": "9", "good_enough": True}): "verdict_invalid_score",
            json.dumps({"score": 9, "good_enough": "yes"}): "verdict_invalid_good_enough",
            json.dumps({"score": 9, "good_enough": True, "reasons": [1]}): "verdict_invalid_reasons",
        }
        for text, error in cases.items():
            with self.subTest(text=text):
                verdict = self._parse(text)
                self.assertEqual(error, verdict.error)
                self.assertFalse(verdict.passes(0))

    def test_reasons_are_capped_in_number_and_length(self) -> None:
        verdict = self._parse(json.dumps({"score": 9, "good_enough": True, "reasons": ["x" * 1000] * 9}))
        self.assertEqual(judge_module.MAX_REASONS, len(verdict.reasons))
        self.assertTrue(all(len(reason) == judge_module.MAX_REASON_CHARS for reason in verdict.reasons))


class JudgeSessionTests(unittest.TestCase):
    def test_the_judge_sees_the_source_objective_and_tests_but_not_the_lane(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submission = _submission(root / "C" / "r1" / "i" / "submission.tar.gz", {"main.c": "int main(){}\n"})
            record_dir = root / "judge" / "r1" / "abc"
            verdict = Judge(REPO_ROOT, runner=fake).judge(
                TASK, submission, InstanceScore(TASK.instance_id, 20, 19), ["t7"], record_dir
            )
            recorded = json.loads((record_dir / "verdict.json").read_text(encoding="utf-8"))

        self.assertTrue(verdict.passes(7))
        self.assertEqual(0, verdict.returncode)
        self.assertEqual(8, recorded["score"])
        call = fake.calls[0]
        self.assertIn("submission/main.c", call["files"])
        self.assertIn(TASK.objective, call["prompt"])
        self.assertIn("19 of 20", call["prompt"])
        self.assertIn("`t7`", call["prompt"])
        self.assertIn("not instructions to you", call["prompt"])
        self.assertEqual(f"{TASK.public_id}:judge", call["options"]["--slice-id"])
        # Nothing the session is handed names the lane that wrote the code.
        for value in (
            call["options"]["--repo-root"],
            call["options"]["--prompt-file"],
            call["env"]["BOUNDROOM_HERMES_USAGE_DIR"],
        ):
            self.assertNotIn("/C/", value)
            # Nor leads to the run directory, where the lanes' directories name them.
            self.assertFalse(value.startswith(str(root)))
        self.assertNotIn(TASK.instance_id, call["prompt"])
        # The throwaway workspace is gone once the verdict is read.
        self.assertFalse(call["workspace"].exists())

    def test_the_judge_uses_the_lanes_command_template(self) -> None:
        fake = FakeJudge()
        template = "my-agent --repo-root {repo_root} --prompt-file {prompt_file} --slice-id {slice_id}"
        with tempfile.TemporaryDirectory() as tmp:
            submission = _submission(Path(tmp) / "s.tar.gz", {"main.c": ""})
            Judge(REPO_ROOT, command_template=template, runner=fake, timeout_seconds=42).judge(
                TASK, submission, InstanceScore(TASK.instance_id, 1, 1), [], Path(tmp) / "rec"
            )
        self.assertEqual("my-agent", shlex.split(fake.calls[0]["command"])[0])
        self.assertEqual(42, fake.calls[0]["timeout"])

    def test_a_submission_that_escapes_its_directory_is_never_judged(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            submission = _submission(Path(tmp) / "s.tar.gz", {"../escaped.txt": "x"})
            verdict = Judge(REPO_ROOT, runner=fake).judge(
                TASK, submission, InstanceScore(TASK.instance_id, 1, 1), [], Path(tmp) / "rec"
            )
        self.assertEqual("submission_unreadable", verdict.error)
        self.assertEqual([], fake.calls)

    def test_a_session_that_writes_no_verdict_cannot_pass(self) -> None:
        fake = FakeJudge(verdict={}, returncode=124)
        with tempfile.TemporaryDirectory() as tmp:
            submission = _submission(Path(tmp) / "s.tar.gz", {"main.c": ""})
            verdict = Judge(REPO_ROOT, runner=fake).judge(
                TASK, submission, InstanceScore(TASK.instance_id, 1, 1), [], Path(tmp) / "rec"
            )
        self.assertEqual("verdict_missing", verdict.error)
        self.assertEqual(124, verdict.returncode)
        self.assertFalse(verdict.passes(0))

    def test_a_session_that_fails_cannot_pass_on_the_verdict_it_left(self) -> None:
        fake = FakeJudge(returncode=124)
        with tempfile.TemporaryDirectory() as tmp:
            submission = _submission(Path(tmp) / "s.tar.gz", {"main.c": ""})
            verdict = Judge(REPO_ROOT, runner=fake).judge(
                TASK, submission, InstanceScore(TASK.instance_id, 1, 1), [], Path(tmp) / "rec"
            )
        self.assertEqual(8, verdict.score)
        self.assertEqual("judge_session_failed", verdict.error)
        self.assertFalse(verdict.passes(0))

    def test_a_corrupt_archive_is_never_judged(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            whole = _submission(Path(tmp) / "whole.tar.gz", {"main.c": "x" * 4096})
            truncated = Path(tmp) / "s.tar.gz"
            truncated.write_bytes(whole.read_bytes()[:-20])
            verdict = Judge(REPO_ROOT, runner=fake).judge(
                TASK, truncated, InstanceScore(TASK.instance_id, 1, 1), [], Path(tmp) / "rec"
            )
        self.assertEqual("submission_unreadable", verdict.error)
        self.assertEqual([], fake.calls)

    def test_a_stale_verdict_is_not_left_in_the_record(self) -> None:
        fake = FakeJudge(verdict={})
        with tempfile.TemporaryDirectory() as tmp:
            record_dir = Path(tmp) / "rec"
            record_dir.mkdir()
            (record_dir / "verdict.json").write_text('{"score": 10, "good_enough": true}', encoding="utf-8")
            submission = _submission(Path(tmp) / "s.tar.gz", {"main.c": ""})
            Judge(REPO_ROOT, runner=fake).judge(TASK, submission, InstanceScore(TASK.instance_id, 1, 1), [], record_dir)
            self.assertFalse((record_dir / "verdict.json").exists())


class JudgeOutputTrustTests(unittest.TestCase):
    """The judge session writes its outputs; the harness reads them outside its sandbox."""

    def _judge(self, runner, tmp: Path, files: Optional[Mapping[str, str]] = None) -> tuple[judge_module.Verdict, Path]:
        submission = _submission(tmp / "s.tar.gz", files or {"main.c": ""})
        record_dir = tmp / "rec"
        verdict = Judge(REPO_ROOT, runner=runner).judge(TASK, submission, InstanceScore(TASK.instance_id, 1, 1), [], record_dir)
        return verdict, record_dir

    def test_a_verdict_that_is_a_link_is_neither_read_nor_copied(self) -> None:
        for kind, make_link in (("symlink", lambda target, link: link.symlink_to(target)), ("hard link", os.link)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                secret = root / "secret.json"
                secret.write_text('{"score": 10, "good_enough": true}', encoding="utf-8")

                def linking_judge(command, workspace, env, timeout, make_link=make_link, secret=secret):
                    make_link(secret, Path(workspace) / judge_module.VERDICT_FILENAME)
                    return 0

                verdict, record_dir = self._judge(linking_judge, root)
                self.assertEqual("verdict_not_a_regular_file", verdict.error)
                self.assertFalse(verdict.passes(0))
                self.assertFalse((record_dir / judge_module.VERDICT_FILENAME).exists())

    def test_session_reports_that_are_symlinks_are_left_out_of_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "id_ed25519"
            secret.write_text("PRIVATE", encoding="utf-8")

            def linking_judge(command, workspace, env, timeout):
                sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
                sessions.mkdir(parents=True, exist_ok=True)
                (sessions / "judge.usage.json").write_text("{}", encoding="utf-8")
                (sessions / "stolen.json").symlink_to(secret)
                (sessions / "stolen-dir").symlink_to(root, target_is_directory=True)
                return FakeJudge()(command, workspace, env, timeout)

            verdict, record_dir = self._judge(linking_judge, root)
            copied = sorted(p.name for p in (record_dir / "agent-sessions").iterdir())
        self.assertTrue(verdict.passes(7))
        self.assertEqual(["judge.usage.json"], copied)

    def test_an_oversized_verdict_is_refused(self) -> None:
        fake = FakeJudge(raw=" " * (judge_module.MAX_VERDICT_BYTES + 1))
        with tempfile.TemporaryDirectory() as tmp:
            verdict, _ = self._judge(fake, Path(tmp))
        self.assertEqual("verdict_not_a_regular_file", verdict.error)

    def test_an_archive_that_unpacks_past_the_cap_is_never_extracted(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(judge_module, "MAX_SUBMISSION_BYTES", 10):
            verdict, _ = self._judge(fake, Path(tmp), {"main.c": "x" * 11})
        self.assertEqual("submission_too_large", verdict.error)
        self.assertEqual([], fake.calls)


class StopGateDecisionTests(unittest.TestCase):
    def _decide(self, run_dir: Path, fake: FakeJudge, repeat: int = 2, lanes=("A", "B"), **gate) -> dict:
        stop_gate = StopGate(judge=Judge(REPO_ROOT, runner=fake), **gate)
        return stop_gate.decide(run_dir, repeat, [lanes_module.Cell(lane, repeat, "inst") for lane in lanes], ceiling=5)

    def test_every_cell_passing_tests_and_judge_stops_the_run(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            for lane in ("A", "B"):
                _cell(run_dir, lane, 2, "inst", PASSING_EVAL)
            record = self._decide(run_dir, fake)
            written = json.loads((run_dir / "judge" / "r2" / "stop-gate.json").read_text(encoding="utf-8"))

        self.assertEqual(("stop", "quality_reached"), (record["decision"], record["reason"]))
        self.assertEqual(2, len(fake.calls))
        self.assertEqual(record, written)
        self.assertEqual({"judge_passed"}, {cell["status"] for cell in written["cells"]})

    def test_no_judge_is_paid_for_in_the_last_round(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "A", 3, "inst", PASSING_EVAL)
            record = StopGate(judge=Judge(REPO_ROOT, runner=fake)).decide(
                run_dir, 3, [lanes_module.Cell("A", 3, "inst")], ceiling=3
            )
        self.assertEqual(("stop", "ceiling"), (record["decision"], record["reason"]))
        self.assertEqual([], fake.calls)

    def test_no_judge_is_paid_for_before_min_repeats(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            for lane in ("A", "B"):
                _cell(run_dir, lane, 1, "inst", PASSING_EVAL)
            record = self._decide(run_dir, fake, repeat=1, min_repeats=2)
        self.assertEqual(("continue", "below_min_repeats"), (record["decision"], record["reason"]))
        self.assertEqual([], fake.calls)

    def test_one_cell_below_the_test_threshold_continues_without_any_judge(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "A", 2, "inst", FAILING_EVAL)
            _cell(run_dir, "B", 2, "inst", PASSING_EVAL)
            record = self._decide(run_dir, fake)
        self.assertEqual(("continue", "tests_not_passed"), (record["decision"], record["reason"]))
        self.assertEqual([], fake.calls)

    def test_the_judge_can_never_pass_failing_tests(self) -> None:
        fake = FakeJudge({"score": 10, "good_enough": True, "reasons": []})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "A", 2, "inst", FAILING_EVAL)
            record = self._decide(run_dir, fake, lanes=("A",))
        self.assertEqual("continue", record["decision"])

    def test_one_judge_saying_no_continues(self) -> None:
        verdicts = iter([{"score": 9, "good_enough": True}, {"score": 4, "good_enough": False, "reasons": ["hard-coded"]}])

        class Alternating(FakeJudge):
            def __call__(self, command, workspace, env, timeout):
                self.verdict = next(verdicts)
                return super().__call__(command, workspace, env, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            for lane in ("A", "B"):
                _cell(run_dir, lane, 2, "inst", PASSING_EVAL)
            record = self._decide(run_dir, Alternating())
        self.assertEqual(("continue", "judge_not_passed"), (record["decision"], record["reason"]))
        self.assertEqual(["judge_passed", "judge_not_passed"], [cell["status"] for cell in record["cells"]])

    def test_no_judge_is_paid_for_after_one_says_no(self) -> None:
        fake = FakeJudge(verdict={"score": 3, "good_enough": False})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            for lane in ("A", "B", "C"):
                _cell(run_dir, lane, 2, "inst", PASSING_EVAL)
            record = self._decide(run_dir, fake, lanes=("A", "B", "C"))
        self.assertEqual(1, len(fake.calls))
        self.assertEqual(("continue", "judge_not_passed"), (record["decision"], record["reason"]))
        self.assertEqual(["judge_not_passed", "not_judged", "not_judged"], [cell["status"] for cell in record["cells"]])

    def test_the_weakest_cell_by_the_tests_is_judged_first(self) -> None:
        fake = FakeJudge(verdict={"score": 3, "good_enough": False})
        nearly = {
            "test_results": [{"name": f"t{n}", "status": "passed"} for n in range(19)] + [{"name": "t19", "status": "failed"}]
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "A", 2, "inst", PASSING_EVAL)
            _cell(run_dir, "B", 2, "inst", nearly)
            record = self._decide(run_dir, fake)
        self.assertEqual(1, len(fake.calls))
        self.assertIn("`t19`", fake.calls[0]["prompt"])
        self.assertEqual(["not_judged", "judge_not_passed"], [cell["status"] for cell in record["cells"]])

    def test_records_that_are_not_objects_block_the_stop(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "A", 2, "inst", PASSING_EVAL)
            (_cell(run_dir, "B", 2, "inst", None) / "inst.eval.json").write_text("[]", encoding="utf-8")
            (_cell(run_dir, "C", 2, "inst", PASSING_EVAL) / "run.json").write_text("[]", encoding="utf-8")
            record = self._decide(run_dir, fake, lanes=("A", "B", "C"))
        self.assertEqual(["tests_passed", "no_eval", "no_provenance"], [cell["status"] for cell in record["cells"]])
        self.assertEqual([], fake.calls)

    def test_an_unparseable_verdict_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "A", 2, "inst", PASSING_EVAL)
            record = self._decide(run_dir, FakeJudge(raw="score: 10"), lanes=("A",))
        self.assertEqual("continue", record["decision"])
        self.assertEqual("verdict_not_json", record["cells"][0]["verdict"]["error"])

    def test_a_skipped_lane_does_not_hold_the_others_back(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "D", 2, "inst", PASSING_EVAL)
            _cell(run_dir, "E", 2, "inst", None, skipped=True)
            record = self._decide(run_dir, fake, lanes=("D", "E"))
        self.assertEqual("stop", record["decision"])
        self.assertEqual(1, len(fake.calls))

    def test_a_provider_invalid_or_unevaluated_cell_blocks_the_stop(self) -> None:
        for provenance, eval_data, status in (
            ({"provider_validity": "provider_degraded"}, PASSING_EVAL, "provider_validity:provider_degraded"),
            ({"treatment_validity": "invalid"}, PASSING_EVAL, "treatment_validity:invalid"),
            ({}, None, "no_eval"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                _cell(run_dir, "A", 2, "inst", eval_data, **provenance)
                record = self._decide(run_dir, FakeJudge(), lanes=("A",))
                self.assertEqual("continue", record["decision"])
                self.assertEqual(status, record["cells"][0]["status"])

    def test_the_record_names_cells_by_a_random_id(self) -> None:
        class ReportingJudge(FakeJudge):
            def __call__(self, command, workspace, env, timeout):
                sessions = Path(env["BOUNDROOM_HERMES_USAGE_DIR"])
                sessions.mkdir(parents=True, exist_ok=True)
                (sessions / "judge.usage.json").write_text("{}", encoding="utf-8")
                return super().__call__(command, workspace, env, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _cell(run_dir, "C", 2, "inst", PASSING_EVAL)
            first = self._decide(run_dir, ReportingJudge(), lanes=("C",))["cells"][0]["judge_id"]
            record_dir = run_dir / "judge" / "r2" / first
            self.assertTrue((record_dir / "verdict.json").is_file())
            # The session's reports reach the record, though the session wrote them elsewhere.
            self.assertTrue((record_dir / "agent-sessions" / "judge.usage.json").is_file())
            second = self._decide(run_dir, FakeJudge(), lanes=("C",))["cells"][0]["judge_id"]
        self.assertEqual(12, len(first))
        self.assertNotEqual(first, second)

    def test_a_judge_timeout_below_one_second_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Judge(REPO_ROOT, runner=FakeJudge(), timeout_seconds=0)

    def test_a_python_without_the_tar_data_filter_is_refused(self) -> None:
        with unittest.mock.patch.object(judge_module, "tarfile", types.SimpleNamespace()):
            with self.assertRaises(ValueError):
                StopGate(judge=Judge(REPO_ROOT, runner=FakeJudge()))

    def test_settings_out_of_range_are_refused(self) -> None:
        judge = Judge(REPO_ROOT, runner=FakeJudge())
        with self.assertRaises(ValueError):
            StopGate(judge=judge, min_repeats=0)
        with self.assertRaises(ValueError):
            StopGate(judge=judge, min_score=11)
        with self.assertRaises(ValueError):
            StopGate(judge=judge, pass_fraction=1.5)


def _factory(lane: str) -> SupervisorAgentAdapter:
    return SupervisorAgentAdapter(repo_root=REPO_ROOT, runner=FakeAgent(), strategy=build_strategy(lane))


class StopGateExperimentTests(unittest.TestCase):
    def _run(self, run_dir: Path, fake: FakeJudge, eval_data: dict, repeats: int, min_repeats: int = 2) -> dict:
        comparison = lanes_module.run_experiment(
            run_dir=run_dir,
            instances=["inst-a"],
            adapter_factory=_factory,
            repo_root=REPO_ROOT,
            lanes=["A", "B"],
            repeats=repeats,
            eval_runner=RecordedEvalRunner(lambda _instance_id: eval_data),
            stop_gate=StopGate(judge=Judge(REPO_ROOT, runner=fake), min_repeats=min_repeats),
        )
        assert comparison is not None
        return comparison

    def test_the_run_stops_at_the_first_round_the_gate_passes(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            comparison = self._run(run_dir, fake, PASSING_EVAL, repeats=4)
            self.assertTrue((run_dir / "B" / "r2" / "inst-a" / "submission.tar.gz").is_file())
            self.assertFalse((run_dir / "A" / "r3").exists())
            self.assertFalse((run_dir / "judge" / "r3").exists())
            markdown = lanes_module.render_comparison_markdown(comparison)

        gate = comparison["stop_gate"]
        self.assertEqual((2, 4, "quality_reached"), (gate["repeats_run"], gate["ceiling"], gate["stopped_by"]))
        self.assertEqual(["continue", "stop"], [entry["decision"] for entry in gate["rounds"]])
        self.assertEqual(2, len(fake.calls))  # round 2 only: round 1 is below min_repeats
        self.assertEqual(2, comparison["lanes"][0]["attempts"])
        self.assertIn("## Stop gate", markdown)
        self.assertIn("Ran 2 of at most 4 repeats; stopped by quality_reached", markdown)

    def test_code_that_never_passes_runs_to_the_ceiling_without_paying_a_judge(self) -> None:
        fake = FakeJudge()
        with tempfile.TemporaryDirectory() as tmp:
            comparison = self._run(Path(tmp), fake, FAILING_EVAL, repeats=3)
        gate = comparison["stop_gate"]
        self.assertEqual((3, "ceiling"), (gate["repeats_run"], gate["stopped_by"]))
        self.assertEqual(["continue", "continue", "stop"], [entry["decision"] for entry in gate["rounds"]])
        self.assertEqual([], fake.calls)

    def test_the_gate_needs_eval_and_a_reachable_minimum(self) -> None:
        gate = StopGate(judge=Judge(REPO_ROOT, runner=FakeJudge()), min_repeats=3)
        with tempfile.TemporaryDirectory() as tmp:
            cases: list[tuple[Optional[RecordedEvalRunner], int]] = [
                (None, 3),
                (RecordedEvalRunner(lambda _instance_id: PASSING_EVAL), 2),
            ]
            for eval_runner, repeats in cases:
                with self.subTest(repeats=repeats), self.assertRaises(ValueError):
                    lanes_module.run_experiment(
                        run_dir=Path(tmp),
                        instances=["i"],
                        adapter_factory=_factory,
                        repo_root=REPO_ROOT,
                        lanes=["A"],
                        repeats=repeats,
                        eval_runner=eval_runner,
                        stop_gate=gate,
                    )
            self.assertFalse((Path(tmp) / "A").exists())  # refused before any cell ran

    def test_without_the_gate_every_repeat_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            comparison = lanes_module.run_experiment(
                run_dir=run_dir,
                instances=["inst-a"],
                adapter_factory=_factory,
                repo_root=REPO_ROOT,
                lanes=["A"],
                repeats=2,
                eval_runner=RecordedEvalRunner(lambda _instance_id: PASSING_EVAL),
            )
            self.assertTrue((run_dir / "A" / "r2" / "inst-a" / "inst-a.eval.json").is_file())
            self.assertFalse((run_dir / "judge").exists())
        assert comparison is not None
        self.assertNotIn("stop_gate", comparison)


class StopGateCommandLineTests(unittest.TestCase):
    def test_judge_without_eval_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as raised:
            run_module.main(["lanes", "--run-dir", tmp, "--judge", "--repeats", "3"])
        self.assertEqual(2, raised.exception.code)

    def test_min_repeats_above_the_ceiling_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as raised:
            run_module.main(["lanes", "--run-dir", tmp, "--judge", "--eval", "--repeats", "2", "--min-repeats", "3"])
        self.assertEqual(2, raised.exception.code)

    def test_a_judge_timeout_below_one_second_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as raised:
            run_module.main(["lanes", "--run-dir", tmp, "--judge", "--eval", "--repeats", "2", "--judge-timeout", "0"])
        self.assertEqual(2, raised.exception.code)

    def test_the_judge_defaults_to_the_lanes_agent_command(self) -> None:
        args = run_module.build_parser().parse_args(
            ["lanes", "--run-dir", "out", "--judge", "--eval", "--agent-cmd", "lane-agent {repo_root}"]
        )
        gate = run_module.build_stop_gate(args, {})
        assert gate is not None
        self.assertEqual("lane-agent {repo_root}", gate.judge._template)
        args.judge_agent_cmd = "other-agent {repo_root}"
        gate = run_module.build_stop_gate(args, {})
        assert gate is not None
        self.assertEqual("other-agent {repo_root}", gate.judge._template)

    def test_judge_with_cleanroom_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as raised:
            run_module.main(["lanes", "--run-dir", tmp, "--judge", "--eval", "--cleanroom", "--repeats", "3"])
        self.assertEqual(2, raised.exception.code)

    def test_compare_keeps_the_stop_gate_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            StopGateExperimentTests()._run(run_dir, FakeJudge(), PASSING_EVAL, repeats=3)
            with unittest.mock.patch("builtins.print"):
                self.assertEqual(0, run_module.main(["compare", "--run-dir", tmp, "--lanes", "A", "B", "--repeats", "2"]))
            rebuilt = json.loads((run_dir / lanes_module.COMPARISON_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual("quality_reached", rebuilt["stop_gate"]["stopped_by"])
        self.assertEqual(3, rebuilt["stop_gate"]["ceiling"])

    def test_no_gate_without_the_flag(self) -> None:
        args = run_module.build_parser().parse_args(["lanes", "--run-dir", "out"])
        self.assertIsNone(run_module.build_stop_gate(args, {}))


if __name__ == "__main__":
    unittest.main()
