"""Which ProgramBench instances run, and what the harness knows about each.

``programbench`` is an optional third-party package, so these tests stand a stub in for it in
``sys.modules`` whose ``TASKS_DIR`` is a real temporary catalogue. A ``None`` entry makes the
import fail, which is what a checkout without the package sees, whether or not it is installed
where the tests run.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sys
import tempfile
import types
import unittest
import unittest.mock
from collections.abc import Generator
from pathlib import Path
from typing import Optional

from automation.benchmark.instances import DEFAULT_SMOKE_INSTANCES, all_instances, resolve_instances, task_spec

CMATRIX = "abishekvashok__cmatrix.5c082c6"


@contextlib.contextmanager
def programbench_absent() -> Generator[None, None, None]:
    with unittest.mock.patch.dict(sys.modules, {"programbench": None, "programbench.constants": None}):
        yield


@contextlib.contextmanager
def programbench_catalogue(tasks: dict[str, Optional[str]], *, tasks_dir: Optional[Path] = None) -> Generator[Path, None, None]:
    """A ``programbench`` whose catalogue holds ``tasks``: name -> task.yaml text, or None for no yaml."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "tasks"
        root.mkdir()
        for name, yaml in tasks.items():
            (root / name).mkdir()
            if yaml is not None:
                (root / name / "task.yaml").write_text(yaml, encoding="utf-8")
        package = types.ModuleType("programbench")
        constants = types.ModuleType("programbench.constants")
        vars(constants).update(TASKS_DIR=str(tasks_dir if tasks_dir is not None else root))
        vars(package).update(constants=constants)
        with unittest.mock.patch.dict(sys.modules, {"programbench": package, "programbench.constants": constants}):
            yield root


class TaskIdentityTests(unittest.TestCase):
    def test_the_public_id_is_a_stable_digest_that_does_not_name_the_program(self) -> None:
        with programbench_absent():
            spec = task_spec(CMATRIX)
        self.assertEqual("task-1f485258fcca", spec.public_id)
        self.assertNotIn("cmatrix", spec.public_id)

    def test_the_objective_names_the_build_contract_and_not_the_program(self) -> None:
        with programbench_absent():
            spec = task_spec(CMATRIX)
        self.assertIn("`compile.sh`", spec.objective)
        self.assertIn("`./executable`", spec.objective)
        for leak in ("cmatrix", "abishekvashok", "unknown"):
            self.assertNotIn(leak, spec.objective)

    def test_a_task_spec_cannot_be_changed_after_it_is_made(self) -> None:
        with programbench_absent():
            spec = task_spec(CMATRIX)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.repository = "someone/else"  # ty: ignore[invalid-assignment]


class TaskSpecWithoutCatalogueTests(unittest.TestCase):
    def test_the_repository_is_derived_from_the_instance_id(self) -> None:
        with programbench_absent():
            spec = task_spec(CMATRIX)
        self.assertEqual(
            (CMATRIX, "abishekvashok/cmatrix", "", "unknown", "unknown"),
            (spec.instance_id, spec.repository, spec.commit, spec.language, spec.difficulty),
        )

    def test_only_the_commit_suffix_is_dropped_from_a_dotted_repository_name(self) -> None:
        with programbench_absent():
            self.assertEqual("owner/my.repo", task_spec("owner__my.repo.abc1234").repository)

    def test_a_catalogue_path_that_is_not_a_directory_counts_as_no_catalogue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            not_a_dir = Path(raw) / "tasks.txt"
            not_a_dir.write_text("", encoding="utf-8")
            with programbench_catalogue({}, tasks_dir=not_a_dir):
                self.assertEqual("abishekvashok/cmatrix", task_spec(CMATRIX).repository)
                self.assertEqual(list(DEFAULT_SMOKE_INSTANCES), all_instances())


class TaskSpecFromCatalogueTests(unittest.TestCase):
    YAML = "\n".join(
        [
            "# ProgramBench task",
            "repository: upstream/renamed",
            "",
            'commit: "5c082c6"',
            "language: c",
            "difficulty: 'easy'",
            "eval_clean_hashes:",
            "  - abc",
            "- language: rust",
            "metadata:",
            " language: python",
            "a line with no separator",
            "homepage: https://example.org/x",
        ]
    )

    def test_the_task_yaml_supplies_the_scalar_fields(self) -> None:
        with programbench_catalogue({CMATRIX: self.YAML}):
            spec = task_spec(CMATRIX)
        self.assertEqual(
            ("upstream/renamed", "5c082c6", "c", "easy"),
            (spec.repository, spec.commit, spec.language, spec.difficulty),
        )

    def test_a_task_without_a_yaml_falls_back_to_the_instance_id(self) -> None:
        with programbench_catalogue({CMATRIX: None}):
            spec = task_spec(CMATRIX)
        self.assertEqual(("abishekvashok/cmatrix", "", "unknown"), (spec.repository, spec.commit, spec.language))

    def test_an_empty_value_counts_as_absent(self) -> None:
        with programbench_catalogue({CMATRIX: "repository: \"\"\nlanguage: ''\ndifficulty: hard\n"}):
            spec = task_spec(CMATRIX)
        self.assertEqual(
            ("abishekvashok/cmatrix", "unknown", "hard"),
            (spec.repository, spec.language, spec.difficulty),
        )


class InstanceSelectionTests(unittest.TestCase):
    CATALOGUE = {
        "zeta__z.1": "repository: zeta/z\n",
        "alpha__a.1": "repository: alpha/a\n",
        "testorg__fixture.1": "repository: testorg/fixture\n",
        "noyaml__n.1": None,
    }

    def test_all_instances_lists_the_real_tasks_sorted(self) -> None:
        with programbench_catalogue(self.CATALOGUE) as root:
            (root / "stray-file.yaml").write_text("repository: x/y\n", encoding="utf-8")
            self.assertEqual(["alpha__a.1", "zeta__z.1"], all_instances())

    def test_without_programbench_all_instances_is_the_smoke_set(self) -> None:
        with programbench_absent():
            self.assertEqual(list(DEFAULT_SMOKE_INSTANCES), all_instances())

    def test_an_explicit_list_wins_over_all(self) -> None:
        with programbench_catalogue(self.CATALOGUE):
            self.assertEqual(["mine__m.1"], resolve_instances(("mine__m.1",), use_all=True))

    def test_all_reads_the_catalogue(self) -> None:
        with programbench_catalogue(self.CATALOGUE):
            self.assertEqual(["alpha__a.1", "zeta__z.1"], resolve_instances(None, use_all=True))
            self.assertEqual(["alpha__a.1", "zeta__z.1"], resolve_instances([], use_all=True))

    def test_neither_runs_the_smoke_set(self) -> None:
        with programbench_catalogue(self.CATALOGUE):
            self.assertEqual(list(DEFAULT_SMOKE_INSTANCES), resolve_instances(None, use_all=False))
            self.assertEqual(list(DEFAULT_SMOKE_INSTANCES), resolve_instances([], use_all=False))


if __name__ == "__main__":
    unittest.main()
