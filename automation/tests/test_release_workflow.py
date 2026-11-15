"""The release workflow attests its archive, and only one job may sign (SPEC-009 RA-ENT-003)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github/workflows"
RELEASE = WORKFLOWS / "release.yml"
PINNED = re.compile(r"^\s*(?:-\s+)?uses:\s+([\w.-]+/[\w./-]+)@([0-9a-f]{40}) # v\d")


def jobs(workflow: Path) -> dict[str, str]:
    """Each top-level job's name mapped to its text, read without a YAML parser."""
    text = workflow.read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    found: dict[str, str] = {}
    name = None
    for line in text.splitlines():
        header = re.match(r"^  ([a-z][a-z0-9_-]*):\s*$", line)
        if header:
            name = header.group(1)
            found[name] = ""
        elif name is not None:
            found[name] += line + "\n"
    return found


class ActionPinningTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit_with_its_version(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"^\s*(?:-\s+)?uses:", line):
                    with self.subTest(workflow=workflow.name, line=number):
                        self.assertRegex(line, PINNED)


class ReleaseAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = jobs(RELEASE)

    def test_the_attest_job_signs_the_archive_with_actions_attest(self) -> None:
        attest = self.jobs["attest"]
        self.assertIn("needs: build", attest)
        self.assertRegex(attest, r"uses: actions/attest@[0-9a-f]{40} # v4")
        self.assertIn("subject-path: dist/boundroom-${{ github.ref_name }}.tar.gz", attest)
        self.assertIn('sha256sum --check --strict "boundroom-$TAG.tar.gz.sha256"', attest)

    def test_only_the_attest_job_can_mint_an_oidc_token(self) -> None:
        workflow = RELEASE.read_text(encoding="utf-8")
        top = workflow.split("\njobs:\n", 1)[0]
        self.assertNotIn("id-token", top)
        for name, text in self.jobs.items():
            with self.subTest(job=name):
                if name == "attest":
                    self.assertIn("id-token: write", text)
                    self.assertIn("attestations: write", text)
                    self.assertIn("contents: read", text)
                    self.assertNotIn("contents: write", text)
                else:
                    self.assertNotIn("id-token", text)
                    self.assertNotIn("attestations:", text)

    def test_the_attest_job_runs_no_repository_code(self) -> None:
        """It checks nothing out, so no gate, dependency or script can reach its token."""
        attest = self.jobs["attest"]
        self.assertNotIn("actions/checkout", attest)
        self.assertNotIn("setup-uv", attest)
        self.assertNotIn("uv ", attest)

    def test_nothing_is_published_before_it_is_attested(self) -> None:
        self.assertIn("needs: [build, attest]", self.jobs["release"])

    def test_a_tag_without_a_changelog_section_is_refused_first(self) -> None:
        self.assertIn('python3 Tools/changelog_section.py "$TAG" > /dev/null', self.jobs["semver"])

    def test_the_release_notes_are_the_changelog_section(self) -> None:
        build = self.jobs["build"]
        self.assertIn('python3 Tools/changelog_section.py "$TAG" > notes/notes.md', build)
        self.assertIn("name: release-notes", build)
        release = self.jobs["release"]
        self.assertIn("name: release-notes", release)
        self.assertIn("cat notes/notes.md", release)
        self.assertIn('--notes-file "$RUNNER_TEMP/release-notes.md"', release)
        self.assertNotIn("--notes ", release)

    def test_the_notes_are_not_an_attested_release_asset(self) -> None:
        """Every file in dist/ is published and the attest job reads only the release artifact."""
        self.assertNotIn("dist/notes", self.jobs["build"])
        self.assertNotIn("release-notes", self.jobs["attest"])

    def test_the_release_notes_say_how_to_verify(self) -> None:
        self.assertIn("gh attestation verify boundroom-$TAG.tar.gz --repo $GITHUB_REPOSITORY", self.jobs["release"])


if __name__ == "__main__":
    unittest.main()
