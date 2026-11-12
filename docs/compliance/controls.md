# CIS and SOC 2 control map

**Status:** checked against the repository's live GitHub settings and workflow files on 2026-11-10. This page maps this repository's controls to the CIS Software Supply Chain Security Guide and the SOC 2 Trust Services Criteria (TSC). It names the evidence each control leaves and lists the gaps.

**What this page can and can't claim.** SOC 2 is an auditor's report on an organization's controls over a period. A repository can't "be SOC 2 compliant" on its own. What it can be is audit-ready: its controls are defined, enforced where possible, and leave evidence an auditor can sample. Nothing here is an attestation.

**How each control is held.** **Enforced** means GitHub or a workflow blocks the action. **Process** means [AGENTS.md](https://github.com/raelldottin/boundroom/blob/main/AGENTS.md) requires it but nothing blocks it. **Gap** means it is not in place.

## Change management: SOC 2 CC8.1; CIS 1.1, code changes

| Control | Status | Evidence |
| --- | --- | --- |
| Every change to `main` goes through a pull request | Enforced: `main` ruleset | Pull request history |
| Five required checks must pass: lint and types on 3.11 and 3.12, tests with an 88% coverage floor, no agent attribution trailers, dependency review | Enforced: `main` ruleset | Check runs on each pull request |
| Linear history; no force-push or deletion of `main` | Enforced: `main` ruleset | Ruleset configuration |
| No one can bypass the `main` rules | Enforced: the `main` ruleset has no bypass actors, not even the admin role | Ruleset configuration |
| An approving review from a person other than the author | Enforced since 2026-11-10: `main` ruleset, one approving review. GitHub doesn't count an author's own review. | Review records |
| Code-owner review (`.github/CODEOWNERS`) | Enforced since 2026-11-10: `main` ruleset | Review records |
| Stale approvals dismissed on push; the last push approved | Enforced since 2026-11-10: `main` ruleset | Ruleset configuration |
| Releases only from a tag on `main`, built by `release.yml`, which reruns the gate and publishes the archive and `.sha256` | Enforced: release-tag ruleset and workflow | Release assets and workflow runs |
| A person creates or approves each release tag | Process | Tag creator, release record, ruleset bypass log |
| Release tags (`v*`) can't be created, updated or deleted except by the admin role | Enforced: release-tag ruleset, whose only bypass actor is the admin role. Each use of the bypass is logged. | Ruleset configuration and rule insights |

## Identity, access and segregation of duties: SOC 2 CC6.1, CC6.3; CIS 1.3, contribution access

| Control | Status | Evidence |
| --- | --- | --- |
| The author is never the approver | Enforced: GitHub doesn't count an author's review, and the last push must be approved by someone other than its pusher | Pull request author versus reviewer |
| The agent acts under its own identity | **In place, by process.** Since 2026-11-10 the agent writes as a GitHub App installed only on this repository, now `boundroom-agent[bot]`. It has write access to contents, pull requests, workflows and issues, read access to checks, statuses and actions, and no administration permission, so it can't change rulesets or settings, and it isn't the admin role the release-tag bypass names. Each command uses an installation token that expires after one hour, and the private key stays in the owner's keychain. Nothing technically stops the agent from using the owner's own `gh` login on the same machine, so using the bot is a process rule. #180–#189 were merged under the owner's token on 2026-11-10, before these controls. | Actor on each event: bot user id `354874784`, App ID `5696200`. Its login was `repo-automation-agent[bot]`, then `bound-room-agent[bot]`, and is `boundroom-agent[bot]` since 2026-11-12. |
| The agent's identity can't bypass `main` | Enforced: the `main` ruleset has no bypass actors | `current_user_can_bypass: never` for the app's token |
| Multi-factor authentication on accounts with write access | Unverified: an account setting the owner's `gh` token can't read | GitHub account security log |
| Least-privilege workflow tokens | Enforced: every workflow declares `permissions:`. `release.yml` asks for `contents: write` only in its publish job. The repository's default workflow token is read-only. | Workflow files, Actions settings |
| Workflows can't approve pull requests | Enforced: repository Actions setting | Actions settings |

## Vulnerability and secret management: SOC 2 CC7.1, CC6.8; CIS 1.5, code risks, and 3, dependencies

| Control | Status | Evidence |
| --- | --- | --- |
| Secret scanning and push protection | Enforced: repository security settings | Security tab alerts |
| Locked dependencies (`uv.lock`) audited by `pip-audit` on push, pull request and weekly | Enforced: workflow | Workflow runs |
| Pull requests that add a high-severity vulnerable dependency are refused | Enforced: required `Review dependency graph diff` | Check runs |
| Dependabot version updates for `uv` and GitHub Actions, weekly | Enforced: `.github/dependabot.yml` | Dependabot pull requests |
| **Dependabot alerts** | **Gap: disabled.** Dependabot security updates need them. | Security tab alerts |
| **Dependabot security updates** | **Gap: disabled** | Dependabot pull requests |
| **Static analysis (code scanning, for example CodeQL)** | **Gap: not configured.** No analyses exist. | Code-scanning alerts |
| Private vulnerability reporting (`SECURITY.md`) | Enforced since 2026-11-10: repository security setting | Security advisories |

## Build and artifact integrity: SOC 2 CC8.1, CC7.1; CIS 2, build pipelines, and 4, artifacts

| Control | Status | Evidence |
| --- | --- | --- |
| Third-party actions pinned to full commit SHAs, with the version as a comment, and updated by Dependabot | **Process.** All 28 references are pinned today, but the repository doesn't require it, so a new workflow could use a tag. | Workflow files |
| **Only approved actions may run** | **Gap.** The repository allows all actions. | Actions settings |
| The release archive is published with a SHA-256 checksum | Enforced: `release.yml` | Release assets |
| **Signed commits on `main`** | **Gap.** Commits on `main` are verified today only because GitHub signs squash merges. The ruleset doesn't require signatures. | Commit verification |
| **Artifact attestations, SBOM** | **Gap** | Not available |

## Owner actions, in this order

These are repository and account security settings. The agent doesn't change them.

1. **Give the agent its own identity.** Done on 2026-11-10: the GitHub App now named `boundroom-agent` (created as `repo-automation-agent`), installed only on this repository, without the administration permission. Keep one active private key, delete any key without a known copy, and add a repository to the installation only when the agent should open pull requests there. Without this identity, required approvals would block every pull request, because GitHub doesn't let an author approve their own pull request.
2. **Require review on `main`.** Done on 2026-11-10. The `main` ruleset's pull request rule requires one approving review, code-owner review, dismissal of stale approvals on push, and approval of the last push. This is the command that set it:

   ```bash
   gh api repos/raelldottin/boundroom/rulesets/25254426 | jq '{name, target, enforcement, conditions, bypass_actors, rules: [.rules[] | if .type == "pull_request" then .parameters |= (.required_approving_review_count = 1 | .require_code_owner_review = true | .dismiss_stale_reviews_on_push = true | .require_last_push_approval = true) else . end]}' | gh api -X PUT repos/raelldottin/boundroom/rulesets/25254426 --input -
   ```

   The CIS guide recommends two approvers. With one maintainer, one approving review plus required checks is the achievable control. Record that as a known deviation.
3. **Turn on private vulnerability reporting.** Done on 2026-11-10.
4. **Turn on Dependabot alerts, then Dependabot security updates.** Security updates fail while alerts are off, so run these in order:

   ```bash
   gh api -X PUT repos/raelldottin/boundroom/vulnerability-alerts
   ```

   ```bash
   gh api -X PUT repos/raelldottin/boundroom/automated-security-fixes
   ```

5. **Turn on code scanning** (CodeQL default setup, free for public repositories): **Settings → Code security → Code scanning → Default**.
6. **Require SHA-pinned actions:** **Settings → Actions → General → Require actions to be pinned to a full-length commit SHA**. To also close the "only approved actions" gap, choose **Allow select actions** there and allow GitHub-created actions plus the third-party actions the workflows use.
7. **Confirm multi-factor authentication** on every account with write access, and turn on commit signing (vigilant mode) for your account.
8. **Require signed commits on `main`:** add **Require signed commits** to the `main` ruleset. GitHub signs squash merges itself, so squash merges keep working. Rebase merges of unsigned commits would be blocked, so keep using squash.
9. **Release tags:** keep the admin bypass only for people, and never give the agent's identity the admin role. A person creating the tag is the release approval.

## Agent-side follow-ups (pull requests, each approved by a person)

- **Artifact attestation:** build provenance for the release archive (`actions/attest-build-provenance`), plus an SBOM.
- **Workflow change control:** a CODEOWNERS entry and required review for `.github/` once the owner adds a second reviewer.
- **Periodic evidence:** a scheduled export of ruleset configuration and recent pull request reviews, so an auditor can sample a period.
