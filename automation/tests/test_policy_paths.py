"""The path rules the supervisor's scope check is built from.

Each helper is a pure function of its arguments, so every case states the inputs and the whole
return value. Several rows pin behaviour that is surprising but current (a blank prefix allows
everything, ``a/**`` is not a glob outside ``stray_owned_paths``, raw spellings survive
``out_of_scope_paths``); changing one of those is a separate, reviewed decision.
"""

from __future__ import annotations

import unittest

from automation.supervisor import policy


class NormalizeRepoPathTest(unittest.TestCase):
    def test_a_path_is_stripped_and_put_in_posix_normal_form(self) -> None:
        cases = (
            ("./x", "x"),
            ("docs/", "docs"),
            ("", "."),
            ("   ", "."),
            ("a//b", "a/b"),
            ("a/./b", "a/b"),
            ("a/../b", "a/../b"),
            ("/abs", "/abs"),
            ("a/**", "a/**"),
            (" a/b ", "a/b"),
            ("docs\\y", "docs\\y"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(expected, policy.normalize_repo_path(raw))


class PathMatchesPrefixTest(unittest.TestCase):
    def test_a_prefix_matches_itself_and_what_is_under_it_but_not_a_sibling(self) -> None:
        cases = (
            ("docs/a", "docs", True),
            ("docs", "docs", True),
            ("docs-private/x", "docs", False),
            ("docsx", "docs", False),
            ("docs/a", "docs/", True),
            ("docs", "docs/", True),
            ("./docs/a", "./docs/", True),
            ("docs/a", " docs ", True),
            ("a", "a/b", False),
            ("", "x", False),
            ("a/b", "a/**", False),
        )
        for path, prefix, expected in cases:
            with self.subTest(path=path, prefix=prefix):
                self.assertEqual(expected, policy.path_matches_prefix(path, prefix))

    def test_an_empty_blank_or_dot_prefix_allows_every_path(self) -> None:
        for prefix in ("", "  ", ".", "./"):
            for path in ("x", "a/b", ""):
                with self.subTest(prefix=prefix, path=path):
                    self.assertIs(True, policy.path_matches_prefix(path, prefix))


class OutOfScopePathsTest(unittest.TestCase):
    OWNED = ["automation/handoffs/", "automation/queue/slices.json"]

    def test_paths_outside_allowed_and_owned_entries_are_reported(self) -> None:
        cases = (
            ("empty input", [], ["docs"], [], None, []),
            ("allowed and owned paths pass", ["docs/a", "automation/queue/slices.json"], ["docs"], self.OWNED, None, []),
            ("a sibling of an allowed prefix", ["docs-private/x", "docs/a"], ["docs"], [], None, ["docs-private/x"]),
            (
                "an owned exact file does not cover a neighbour",
                ["automation/queue/slices.json.bak"],
                ["docs"],
                self.OWNED,
                None,
                ["automation/queue/slices.json.bak"],
            ),
            ("sorted and deduplicated", ["b", "a", "b"], ["docs"], [], None, ["a", "b"]),
            ("raw spellings are kept", ["./x", "x", ""], ["y"], [], None, ["", "./x", "x"]),
        )
        for name, dirty, allowed, owned, handoff, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.out_of_scope_paths(dirty, allowed, owned, handoff))

    def test_the_runs_own_handoff_is_never_out_of_scope(self) -> None:
        dirty = ["./outside/h.json", "outside/h.json", "outside/other.json"]
        cases = (
            ("own handoff, both spellings", "outside/h.json", ["outside/other.json"]),
            ("handoff given with a leading ./", "./outside/h.json", ["outside/other.json"]),
            ("no handoff", None, ["./outside/h.json", "outside/h.json", "outside/other.json"]),
            ("empty handoff", "", ["./outside/h.json", "outside/h.json", "outside/other.json"]),
        )
        for name, handoff, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.out_of_scope_paths(dirty, ["docs"], [], handoff))


class OwnedEntryPrefixTest(unittest.TestCase):
    def test_trailing_globs_and_slashes_are_dropped(self) -> None:
        cases = (
            ("a/**", "a"),
            ("a/**/**", "a"),
            ("**", "**"),
            ("/**", "."),
            ("", "."),
            ("a/", "a"),
            ("./a/**", "a"),
            ("a/*", "a/*"),
            (" a/** ", "a"),
        )
        for entry, expected in cases:
            with self.subTest(entry=entry):
                self.assertEqual(expected, policy.owned_entry_prefix(entry))


class StrayOwnedPathsTest(unittest.TestCase):
    def test_paths_beside_the_handoff_in_its_owned_entry_are_stray(self) -> None:
        owned = ["automation/handoffs/", "automation/queue/slices.json"]
        changed = [
            "automation/handoffs/h.json",
            "automation/handoffs/other.json",
            "automation/queue/slices.json",
            "docs/a",
            "README.md",
        ]
        cases = (
            ("example queue layout", changed, ["docs"], owned, "automation/handoffs/h.json", ["automation/handoffs/other.json"]),
            ("no handoff reports nothing", changed, ["docs"], owned, None, []),
            ("empty handoff reports nothing", changed, ["docs"], owned, "", []),
            ("a glob entry holds the handoff", ["a/x", "a/h.json"], ["docs"], ["a/**"], "a/h.json", ["a/x"]),
            (
                "output is normalized",
                ["./automation/handoffs/o.json"],
                ["docs"],
                owned,
                "automation/handoffs/h.json",
                ["automation/handoffs/o.json"],
            ),
            (
                "allowed beats holding",
                ["automation/handoffs/notes.md"],
                ["automation/handoffs/notes.md"],
                owned,
                "automation/handoffs/h.json",
                [],
            ),
            (
                "a narrower owned entry without the handoff wins",
                ["a/q/slices.json", "a/x"],
                [],
                ["a", "a/q"],
                "a/h.json",
                ["a/x"],
            ),
            ("an owned dot entry makes every other path stray", ["x", "docs/a", "h.json"], ["docs"], ["."], "h.json", ["x"]),
            ("sorted and deduplicated", ["a/y", "a/x", "./a/x"], [], ["a"], "a/h.json", ["a/x", "a/y"]),
        )
        for name, paths, allowed, owned_entries, handoff, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.stray_owned_paths(paths, allowed, owned_entries, handoff))


class IntroducedPathsTest(unittest.TestCase):
    def test_only_paths_dirty_after_but_not_before_the_run_are_the_runs(self) -> None:
        cases = (
            ("pre-existing dirt is not the run's", ["x"], ["x", "y"], ["y"]),
            ("a path only dirty before is ignored", ["x"], [], []),
            ("spellings are compared normalized", ["./a"], ["a"], []),
            ("blank entries become dot", ["a"], ["", "a", "./b"], [".", "b"]),
            ("sorted", [], ["b", "a"], ["a", "b"]),
        )
        for name, before, after, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.introduced_paths(before, after))


class CountPathsWithinScopeTest(unittest.TestCase):
    def test_counts_allowed_entries_that_are_not_owned(self) -> None:
        cases = (
            ("allowed paths count", ["docs/a", "docs/b"], ["docs"], [], 2),
            ("paths outside allowed do not count", ["src/a"], ["docs"], [], 0),
            ("owned paths do not count even when allowed", ["docs/q.json", "docs/a"], ["docs"], ["docs/q.json"], 1),
            ("entries are counted, not distinct paths", ["x", "x", "./x"], ["x"], [], 3),
        )
        for name, dirty, allowed, owned, expected in cases:
            with self.subTest(name):
                self.assertEqual(expected, policy.count_paths_within_scope(dirty, allowed, owned))


class NormalizePathsTest(unittest.TestCase):
    def test_blank_entries_are_dropped_and_the_rest_normalized_sorted_and_unique(self) -> None:
        self.assertEqual(["a", "b"], policy.normalize_paths([" ", "", "./b", "b", "a"]))
        self.assertEqual([], policy.normalize_paths([]))


if __name__ == "__main__":
    unittest.main()
