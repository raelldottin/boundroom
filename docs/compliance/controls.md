# CIS and SOC 2 control map

**Status:** observed 2026-11-10. This page maps this repository's controls to the CIS Software Supply Chain Security Guide and the SOC 2 Trust Services Criteria (TSC). It names the evidence each control leaves and lists the gaps.

**What this page can and can't claim.** SOC 2 is an auditor's report on an organization's controls over a period. A repository can't "be SOC 2 compliant" on its own. What it can be is audit-ready: its controls are defined, enforced where possible, and leave evidence an auditor can sample. Nothing here is an attestation.

**How each control is held.** **Enforced** means GitHub or a workflow blocks the action. **Process** means [AGENTS.md](https://github.com/raelldottin/repo-automation/blob/main/AGENTS.md) requires it but nothing blocks it. **Gap** means it is not in place.

## Change management: SOC 2 CC8.1; CIS 1.1, code changes

| Control | Status | Evidence |
| --- | --- | --- |
| Every change to `main` goes through a pull request | Enforced: `main` ruleset | Pull request history |
| Five required checks must pass: lint and types on 3.11 and 3.12, tests with an 88% coverage floor, no agent attribution trailers, dependency review | Enforced: `main` ruleset | Check runs on each pull request |
| Linear history; no force-push or deletion of `main` | Enforced: `main` ruleset | Ruleset configuration |
| **An approving review from a person other than the author** | **Process only.** The ruleset requires 0 approvals. | Review records, once enforced |
| Code-owner review (`.github/CODEOWNERS`) | **Gap.** The file exists, but the ruleset doesn't require it. | Review records |
| Stale approvals dismissed on push; the last push approved | **Gap.** Both are off. | Ruleset configuration |
| Releases only from a tag on `main`, built by `release.yml`, which reruns the gate and publishes the archive and `.sha256` | Enforced: release-tag ruleset and workflow | Release assets and workflow runs |
| A person creates or approves each release tag | Process | Tag creator, release record |
| Tags can't be updated or deleted | Enforced: release-tag ruleset, but the admin role can bypass it | Ruleset configuration |

## Identity, access and segregation of duties: SOC 2 CC6.1, CC6.3; CIS 1.3, contribution access

| Control | Status | Evidence |
| --- | --- | --- |
| The author is never the approver | Process | Pull request author versus reviewer |
| **The agent acts under its own identity** | **Gap.** The agent uses the owner's GitHub token, so its pull requests, merges and tags appear as the owner's own actions. #180–#189 were merged this way on 2026-11-10, before these controls. | Actor on each event |
| Multi-factor authentication on accounts with write access | Unverified: an account setting this repository can't see | GitHub account security log |
| Least-privilege workflow tokens | Enforced: every workflow declares `permissions:`. `release.yml` asks for `contents: write` only in its publish job. | Workflow files |

## Vulnerability and secret management: SOC 2 CC7.1, CC6.8; CIS 1.5, code risks, and 3, dependencies

| Control | Status | Evidence |
| --- | --- | --- |
| Secret scanning and push protection | Enforced: repository security settings | Security tab alerts |
| Locked dependencies (`uv.lock`) audited by `pip-audit` on push, pull request and weekly | Enforced: workflow | Workflow runs |
| Pull requests that add a high-severity vulnerable dependency are refused | Enforced: required `Review dependency graph diff` | Check runs |
| Dependabot version updates for pip and GitHub Actions, weekly | Enforced: `.github/dependabot.yml` | Dependabot pull requests |
| **Dependabot security updates** | **Gap: disabled** | Dependabot pull requests |
| **Static analysis (code scanning, for example CodeQL)** | **Gap: not configured** | Code-scanning alerts |
| Private vulnerability reporting (`SECURITY.md`) | **Gap.** `SECURITY.md` points to it, but the setting is disabled. | Security advisories |

## Build and artifact integrity: SOC 2 CC8.1, CC7.1; CIS 2, build pipelines, and 4, artifacts

| Control | Status | Evidence |
| --- | --- | --- |
| Third-party actions pinned to full commit SHAs, with the version as a comment, and updated by Dependabot | Enforced: workflow files | Workflow files |
| The release archive is published with a SHA-256 checksum | Enforced: `release.yml` | Release assets |
| **Signed commits and tags, artifact attestations, SBOM** | **Gap** | Not available |

## Owner actions, in this order

These are repository and account security settings. The agent doesn't change them.

1. **Give the agent its own identity.** Create a GitHub App or a machine user with write access to this repository but no admin role, and have the agent use its token. Without it, required approvals block every pull request: the agent's pull requests appear authored by you, and GitHub doesn't let an author approve their own pull request.
2. **Require review on `main`.** Set the `main` ruleset's pull request rule to one approving review, code-owner review, dismissal of stale approvals on push, and approval of the last push:

   ```bash
   gh api repos/raelldottin/repo-automation/rulesets/25254426 | jq '{name, target, enforcement, conditions, bypass_actors, rules: [.rules[] | if .type == "pull_request" then .parameters |= (.required_approving_review_count = 1 | .require_code_owner_review = true | .dismiss_stale_reviews_on_push = true | .require_last_push_approval = true) else . end]}' | gh api -X PUT repos/raelldottin/repo-automation/rulesets/25254426 --input -
   ```

   The CIS guide recommends two approvers. With one maintainer, one approving review plus required checks is the achievable control. Record that as a known deviation.
3. **Turn on Dependabot security updates:**

   ```bash
   gh api -X PUT repos/raelldottin/repo-automation/automated-security-fixes
   ```

4. **Turn on private vulnerability reporting:**

   ```bash
   gh api -X PUT repos/raelldottin/repo-automation/private-vulnerability-reporting
   ```

5. **Turn on code scanning** (CodeQL default setup, free for public repositories): **Settings → Code security → Code scanning → Default**.
6. **Confirm multi-factor authentication** on every account with write access, and turn on commit signing (vigilant mode) for your account.
7. **Release tags:** keep the admin bypass only for people, and never give the agent's identity the admin role. A person creating the tag is the release approval.

## Agent-side follow-ups (pull requests, each approved by a person)

- **Artifact attestation:** build provenance for the release archive (`actions/attest-build-provenance`), plus an SBOM.
- **Workflow change control:** a CODEOWNERS entry and required review for `.github/` once the owner adds a second reviewer.
- **Periodic evidence:** a scheduled export of ruleset configuration and recent pull request reviews, so an auditor can sample a period.
