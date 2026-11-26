"""Lane E must administer the canonical J-Space skill, or admit that it did not.

Every case here is one way the pinned artifact can fail to be what the lock claims. The
expected outcome is always the same: refuse, name the reason, and never hand lane E some
other text. A silent substitution would make ``E - D`` measure our paraphrase.

Resolution runs against a plain directory and a fake revision reader, because the Git
subprocess is the only out-of-process dependency. Real Git is used once per class: to show
``checkout_revision`` reads a checkout's HEAD, and to resolve end to end with the defaults.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from automation.benchmark import jspace
from automation.benchmark.jspace import JSpaceArtifact, JSpaceUnavailable

SKILL_TEXT = "# J-Space\n\nInstall at `<skill-root>`; invoke with `<python-command>`.\n"
SKILL_SHA = hashlib.sha256(SKILL_TEXT.encode("utf-8")).hexdigest()
ARTIFACT_PATH = "j-space/SKILL.md"
SOURCE = "fixture/j-space"
REVISION = "a" * 40


# Set by a hook or a CI runner, these would point Git at another repository than the fixture's.
_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")


def _git_env(ceiling: Path) -> dict[str, str]:
    """The environment with no repository named and no search above ``ceiling``."""
    env = {key: value for key, value in os.environ.items() if key not in _REPOSITORY_VARIABLES}
    env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return env


def _git(root: Path, *args: str) -> str:
    """Run the fixture's Git with none of the user's or the system's config, and no hooks."""
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**_git_env(root.parent), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


class _TempDirCase(unittest.TestCase):
    tmp: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="jspace-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)

    def write_lock(self, name: str, **changes: Optional[str]) -> Path:
        lock: dict[str, Any] = {"source": SOURCE, "revision": REVISION, "artifact": ARTIFACT_PATH, "sha256": SKILL_SHA}
        lock.update(changes)
        path = self.tmp / f"{name}.lock.json"
        path.write_text(json.dumps(lock), encoding="utf-8")
        return path


class ResolutionTests(_TempDirCase):
    """``resolve`` against a plain directory, with the revision read from a table."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.root = cls.tmp / "checkout"
        (cls.root / ARTIFACT_PATH).parent.mkdir(parents=True)
        (cls.root / ARTIFACT_PATH).write_text(SKILL_TEXT, encoding="utf-8")

    def setUp(self) -> None:
        self.lock = self.write_lock(self.id().rsplit(".", 1)[-1])

    @staticmethod
    def revisions(table: dict[Path, Optional[str]]) -> Any:
        return lambda root: table.get(root)

    def resolve(
        self,
        root: Any = None,
        *,
        at: Optional[str] = REVISION,
        environ: Optional[dict[str, str]] = None,
        lock: Optional[Path] = None,
    ) -> JSpaceArtifact:
        reader = self.revisions({self.root: at})
        return jspace.resolve(root, lock or self.lock, revision_of=reader, environ={} if environ is None else environ)

    def refusal(self, **kwargs: Any) -> str:
        with self.assertRaises(JSpaceUnavailable) as caught:
            self.resolve(**kwargs)
        return str(caught.exception)

    def expected_artifact(self) -> JSpaceArtifact:
        return JSpaceArtifact(
            source=SOURCE, revision=REVISION, artifact=ARTIFACT_PATH, sha256=SKILL_SHA, root=self.root.resolve(), text=SKILL_TEXT
        )

    def test_an_unset_root_refuses_and_says_what_to_point_at(self) -> None:
        unset = f"jspace_unavailable: set JSPACE_ROOT to a checkout of {SOURCE} at {REVISION}"
        rows = {
            "no root, no variable": {},
            "empty root argument": {"root": ""},
            "empty variable": {"environ": {"JSPACE_ROOT": ""}},
        }
        for label, kwargs in rows.items():
            with self.subTest(label):
                self.assertEqual(unset, self.refusal(**kwargs))

    def test_the_environment_variable_is_used_when_no_root_is_passed(self) -> None:
        artifact = self.resolve(environ={"JSPACE_ROOT": str(self.root)})
        self.assertEqual(self.expected_artifact(), artifact)

    def test_an_explicit_root_beats_the_environment_variable(self) -> None:
        artifact = self.resolve(self.root, environ={"JSPACE_ROOT": str(self.tmp / "elsewhere")})
        self.assertEqual(self.expected_artifact(), artifact)

    def test_a_root_under_the_home_directory_is_expanded(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            artifact = self.resolve(environ={"JSPACE_ROOT": "~/checkout"})
        self.assertEqual(self.expected_artifact(), artifact)

    def test_a_root_that_is_not_a_directory_refuses(self) -> None:
        for target in (self.root / ARTIFACT_PATH, self.tmp / "absent"):
            with self.subTest(str(target)):
                self.assertEqual(f"jspace_unavailable: JSPACE_ROOT is not a directory: {target}", self.refusal(root=target))

    def test_a_directory_without_a_readable_revision_refuses_even_with_the_right_bytes(self) -> None:
        self.assertEqual(
            f"jspace_unavailable: {self.root} is not a Git checkout, so its revision cannot be verified",
            self.refusal(root=self.root, at=None),
        )

    def test_a_different_revision_refuses(self) -> None:
        drifted = "b" * 40
        self.assertEqual(
            f"jspace_unavailable: {self.root} is at {drifted}, not the pinned {REVISION}",
            self.refusal(root=self.root, at=drifted),
        )

    def test_a_missing_artifact_refuses(self) -> None:
        lock = self.write_lock("moved", artifact="j-space/MOVED.md")
        moved = self.root / "j-space/MOVED.md"
        self.assertEqual(
            f"jspace_unavailable: cannot read {moved}: [Errno 2] No such file or directory: '{moved}'",
            self.refusal(root=self.root, lock=lock),
        )

    def test_content_that_does_not_match_the_pinned_hash_refuses(self) -> None:
        lock = self.write_lock("rehashed", sha256="f" * 64)
        self.assertEqual(
            f"jspace_unavailable: {ARTIFACT_PATH} hashes to {SKILL_SHA}, not the pinned {'f' * 64}",
            self.refusal(root=self.root, lock=lock),
        )

    def test_a_matching_checkout_resolves_to_the_pinned_bytes(self) -> None:
        artifact = self.resolve(self.root)
        self.assertEqual(self.expected_artifact(), artifact)
        self.assertEqual(
            {
                "source": SOURCE,
                "revision": REVISION,
                "artifact": ARTIFACT_PATH,
                "sha256": SKILL_SHA,
                "root": str(self.root.resolve()),
            },
            artifact.provenance(),
        )

    def test_the_prompt_block_is_the_canonical_text_plus_only_its_open_names(self) -> None:
        block = self.resolve(self.root).prompt_block()
        self.assertEqual(
            f"<skill-root> is `{self.root.resolve()}`. <python-command> is `python3`.\n\n"
            "# J-Space\n\nInstall at `<skill-root>`; invoke with `<python-command>`.",
            block,
        )

    def test_a_resolved_artifact_cannot_be_altered_after_verification(self) -> None:
        artifact = self.resolve(self.root)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.text = "a paraphrase"  # ty: ignore[invalid-assignment]


class LockTests(_TempDirCase):
    def load_refusal(self, path: Path) -> str:
        with self.assertRaises(JSpaceUnavailable) as caught:
            jspace.load_lock(path)
        return str(caught.exception)

    def test_a_complete_lock_is_returned_as_written(self) -> None:
        lock = {"source": SOURCE, "revision": REVISION, "artifact": ARTIFACT_PATH, "sha256": SKILL_SHA}
        self.assertEqual(lock, jspace.load_lock(self.write_lock("complete")))

    def test_a_missing_lock_file_refuses_and_names_the_path(self) -> None:
        absent = self.tmp / "absent.json"
        self.assertEqual(f"jspace_unavailable: lock file missing at {absent}", self.load_refusal(absent))

    def test_a_lock_file_that_is_not_json_refuses(self) -> None:
        broken = self.tmp / "broken.json"
        broken.write_text("not json", encoding="utf-8")
        self.assertEqual(
            "jspace_unavailable: lock file is not valid JSON: Expecting value: line 1 column 1 (char 0)",
            self.load_refusal(broken),
        )

    def test_an_incomplete_lock_file_names_every_missing_key_in_order(self) -> None:
        rows = {
            "one empty": ({"revision": ""}, "revision"),
            "two absent": ({"revision": None, "sha256": None}, "revision, sha256"),
            "all empty": ({"source": "", "revision": "", "artifact": "", "sha256": ""}, "source, revision, artifact, sha256"),
        }
        for label, (changes, missing) in rows.items():
            with self.subTest(label):
                lock = self.write_lock(label.replace(" ", "-"), **changes)
                self.assertEqual(f"jspace_unavailable: lock file is missing {missing}", self.load_refusal(lock))


class RealCheckoutTests(_TempDirCase):
    """The one place a real Git subprocess runs: reading HEAD, and resolving with the defaults."""

    checkout: Path
    head: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checkout = cls.tmp / "checkout"
        (cls.checkout / ARTIFACT_PATH).parent.mkdir(parents=True)
        (cls.checkout / ARTIFACT_PATH).write_text(SKILL_TEXT, encoding="utf-8")
        _git(cls.tmp, "init", "-q", "-b", "main", str(cls.checkout))
        _git(cls.checkout, "add", "-A")
        _git(cls.checkout, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "pin")
        cls.head = _git(cls.checkout, "rev-parse", "HEAD")

    def setUp(self) -> None:
        # checkout_revision runs Git with the process environment, so isolate that too.
        self.enterContext(mock.patch.dict(os.environ, _git_env(self.tmp), clear=True))

    def test_checkout_revision_reads_the_head_of_a_checkout(self) -> None:
        self.assertRegex(self.head, re.compile(r"^[0-9a-f]{40}$"))
        self.assertEqual(self.head, jspace.checkout_revision(self.checkout))

    def test_checkout_revision_is_none_for_a_directory_outside_git(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(jspace.checkout_revision(plain))

    def test_resolve_with_its_defaults_reads_the_variable_and_the_real_head(self) -> None:
        lock = self.write_lock("real", revision=self.head)
        with mock.patch.dict(os.environ, {"JSPACE_ROOT": str(self.checkout)}):
            artifact = jspace.resolve(lock_path=lock)
        expected = JSpaceArtifact(
            source=SOURCE,
            revision=self.head,
            artifact=ARTIFACT_PATH,
            sha256=SKILL_SHA,
            root=self.checkout.resolve(),
            text=SKILL_TEXT,
        )
        self.assertEqual(expected, artifact)


class DefaultLookupTests(_TempDirCase):
    """Left out, the revision reader and the environment are looked up when resolve runs."""

    def test_the_module_s_revision_reader_is_the_one_in_place_at_call_time(self) -> None:
        root = self.tmp / "checkout"
        (root / ARTIFACT_PATH).parent.mkdir(parents=True, exist_ok=True)
        (root / ARTIFACT_PATH).write_text(SKILL_TEXT, encoding="utf-8")
        lock = self.write_lock("late")
        with (
            mock.patch.object(jspace, "checkout_revision", lambda path: REVISION),
            mock.patch.dict(os.environ, {"JSPACE_ROOT": str(root)}),
        ):
            self.assertEqual(REVISION, jspace.resolve(lock_path=lock).revision)


class ShippedLockTests(unittest.TestCase):
    """The lock in the repository is the claim lane E's provenance rests on."""

    def test_the_shipped_lock_pins_a_full_revision_and_hash(self) -> None:
        lock = jspace.load_lock()
        self.assertRegex(lock["revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(lock["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(lock["artifact"].endswith(".md"), lock["artifact"])

    def test_the_skill_text_is_not_vendored_into_this_repository(self) -> None:
        """No copy of the skill means no way to silently drift from the pinned bytes."""
        benchmark = Path(jspace.__file__).resolve().parent
        vendored = [path for path in benchmark.rglob("*") if path.name == "SKILL.md"]
        self.assertEqual([], vendored)


if __name__ == "__main__":
    unittest.main()
