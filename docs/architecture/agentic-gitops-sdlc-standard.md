# Agentic SDLC and GitOps: portable governance standard (draft v0)

Status: proposed, documentation-only. Owner approval and evidence required before adopting any deployment adapter. See tracking issue [#170](https://github.com/raelldottin/repo-automation/issues/170). This document complements, and does not override, each consumer repository's AGENTS.md, branch rules, license, infrastructure ownership or release contract.

## Scope and terms

The software development lifecycle (SDLC) is the path from a user need to specifications, code, independent validation, delivery and feedback. CI (continuous integration) checks a proposed change; CD (continuous delivery/deployment) promotes tested artifacts. **GitOps** is a narrower operating model: declarative desired state is versioned, automatically pulled and continuously reconciled against reality. A GitHub Actions push-to-deploy workflow alone is **Git-governed CI/CD, not full OpenGitOps**. Do not claim otherwise.

Research references:
- Google, [Beyond vibe coding for the web](https://codelabs.developers.google.com/codelabs/beyond-vibe-coding-for-the-web) and [The New SDLC With Vibe Coding](https://www.kaggle.com/whitepaper-the-new-SDLC-with-vibe-coding).
- DORA, [2025 State of AI-assisted Software Development](https://dora.dev/research/2025/dora-report/), [AI Capabilities Model](https://dora.dev/ai/capabilities-model/report/) and [2026 AI tensions](https://dora.dev/insights/balancing-ai-tensions/).
- [OpenGitOps principles](https://opengitops.dev/), [SLSA v1.2](https://slsa.dev/spec/v1.2/).

## Gap matrix: Google guidance versus controls to operationalize

The following are **implementation gaps to verify per repository**, not unsupported claims that Google's guidance has no answer. Google urges planning/specification, independent agents and iteration, and warns about agreeableness, verification gaps and debt. DORA observes that AI can amplify instability when software delivery fundamentals are weak.

| Risk / observed or potential gap | Required enforceable control | Proof artifact |
| --- | --- | --- |
| Prompt-first implementation without requirements | Short user problem, product requirements, design/ADR, acceptance cases, risks and explicit non-goals before writing | Linked spec, design and acceptance matrix |
| Agent agrees with mistaken premises | Preflight asks whether constraints are possible; challenges contradicting requirements; independent reviewer verifies actual repo/API facts | Assumptions and contradicted-claim ledger |
| Self-authored tests are treated as independent proof | Separate deterministic checks, independent review, negative tests and end-to-end target-environment evidence | Exact CI commands and artifacts, reviewer identity |
| Large AI-authored changes increase scrutiny cost | Small vertical slices with WIP limits; diff budgets as review guidance, not arbitrary pass/fail; no automatic large PR mergers | PR scope and review record |
| Unsafe agent permissions and prompt injection | Least-privilege agents and secrets; treat PR contents, issues and fetched data as untrusted; disallow untrusted workflow write/production tokens | Forbidden-action tests and workflow permission inspection |
| Workflow edits can bypass checks | Protected check names, separate policy tests, change-controlled workflow files, independent review on any CI/security modification | Policy-as-code negative tests |
| Supply-chain provenance is unclear | Pinned dependencies/actions; SBOM and attestations where supported; signed/checksummed artifact bound to Git SHA and toolchain | Artifact provenance manifest |
| Build success is mistaken for release correctness | Layered lint/type/unit/integration/UI/accessibility/performance/security validations appropriate to target | Proof ladder with explicit SKIP/BLOCKED |
| GitHub Actions is mislabeled true GitOps | Explicit adapter classification as Git-governed CI/CD or pull/reconcile GitOps; declare actual observed state and drift handling | Desired/observed comparison and event log |
| Agents publish without human authority | Protected promotion environments, owner approval for exact artifact/head SHA, no automatic tag/sign/publish/deploy | Approval record, permission tests |
| Rollbacks are assumed universally reversible | Platform-specific rollback, fix-forward, data/schema compatibility, launch-pausing and incident drills | Recovery rehearsal and times |
| AI gains are not measured | DORA change lead time, deploy frequency, change failure and recovery time plus flakiness, audit quality and user outcome; first establish baseline | Versioned scorecard and feedback record |

## Mandatory agent operating contract for consumers

1. Read local AGENTS.md and repository-specific architecture, test, release and security documents before changes. Local constraints prevail where stricter.
2. Work from a concrete, approved issue/spec: problem, user outcomes, system boundary, threat model if relevant, negative cases, rollback and acceptance evidence. Do not use vague single-shot prompts as release specifications.
3. Run a non-mutating preflight: repo identity, branch, clean/dirty status, owners, in-flight worktrees, pinned tools, deployment target and current evidence. If unknown, stop or mark BLOCKED.
4. Separate code author, invariant/security challenger and final verifier when critical; no agent may certify its own risky changes. Preserve logs and exact reviewed SHA.
5. Gate PRs with actual, pinned test commands; never modify a required check, file path filter or workflow to hide failures. Test both happy paths and failure paths (revoked approval, duplicate event, stale credentials, malicious input, unavailable services).
6. Never expose secret values, accept secret-bearing pull requests, execute untrusted code with privileged credentials or publish from unreviewed forks. Restrict GitHub token permissions, use protected environments and ephemeral short-lived credentials when supported.
7. Define source SHA, build toolchain, dependency lock digest, artifact digest, signing/provenance and environment for every release candidate. A build is not a deployment, and a deployment is not proof of health.
8. Production release, signing, promotion, merge of agent-authored changes, rule changes and Git tag changes require the repository owner's explicit approval on the specific head/artifact. No prior consent automatically transfers to a new SHA.
9. Prefer declarative, pull/reconcile deployment where platform permits; observe desired versus actual state, alert on drift, preserve manual recovery authority. Never install Kubernetes/ArgoCD purely for a label.
10. Document the executed checks, exact test outcomes, security findings, validation gaps, evidence level and clean-stop status. Missing environment/credentials means BLOCKED, not PASS.
11. Preserve existing consumer lock/pin, Tools/repo-automation-sync.sh and reusable-manifest rules. Never independently update consumer pins or auto-merge synchronized changes without the owner's separate authorization.

## Platform adapters (choose one; do not enforce an incompatible universal deploy workflow)

| Kind | Build/test | Release / deploy control | Classification |
| --- | --- | --- | --- |
| Apple Swift iOS/watchOS | Pinned Xcode and simulator, xcodebuild tests, Swift lint, UI/accessibility, signing separation | Versioned archive -> manual signing/protected TestFlight -> explicit App Review; query App Store Connect status and detect version drift; fix-forward for shipped binary | Git-governed CI/CD and release reconciliation; not full pull-based GitOps |
| Django / web | Pinned Python/uv, Pyright, migrations, unit and HTTP/security tests, image scan | Immutable OCI digest + migrations/health + protected environment. If an authorized reconciler exists, declare desired image and let it pull/reconcile | May become true GitOps when 4 OpenGitOps principles actually met |
| AWS/home lab | Local Docker Desktop + Compose v2 first; Chef Test Kitchen; Terraform validate/plan and policy/secret scan | Terraform controls infra, Chef host configuration, Compose service lifecycle; AWS only after local gates and human plan approval; add agent reconciliation only if designed | Mixed IaC and Git-governed delivery; true GitOps only with automated pull/reconciliation |
| Python model / CLI | uv or project-pinned tooling, unit/property/reproducibility, model evaluation and security scans | Versioned source/wheels/report artifacts with SHA; no persistent deployment if none exists | CI and artifact release, not runtime GitOps |
| Next.js / fintech | Pinned Bun/Node islands, type/lint/test/build, provider-independent and money-safety invariants | Protected deployment preview -> explicit promotion with staged health, account-scope controls, no unauthorized financial mutation | Git-governed CD; only claim GitOps if actual reconciler installed |

## Rollout stages and ownership

R0 INVENTORY: List active owned repos, real branch and CI checks, AGENTS.md present/absent, environment, release owner, existing test proof and sensitive boundaries.

R1 STANDARD: Version and review this policy through repo-automation PR; do not rewrite the existing supervisor/control plane, consumers' locks or branch policies.

R2 PILOT: Use raelldottin/climate-closet-ios, issue #2. Create a docs/AGENTS PR before changing automation; separately implement a minimal CI policy/negative test gate. Do not add Apple signing/upload secrets until a separately approved promotion milestone.

R3 VERIFY: Independent evidence for clean PR and negative bypasses; verify actual artifact digest, execution context, simulator tests and environment restriction.

R4 PER-CONSUMER: Prepare scoped GitOps policy/AGENTS.md PR by adapter, preserve local instructions, report exact head, require owner's per-PR approval and green checks. No blind multi-repository merge.

R5 MEASURE: Record pre/post DORA metrics and user outcomes; stage incident/recovery drills and review adoption monthly.

### Initial inventory snapshot — November 7, 2026

Scanned selected active repositories via GitHub. Presence of a workflow does not prove it passes or deploys safely.

| Repository | AGENTS.md | Workflow directory observed | Immediate action |
| --- | --- | --- | --- |
| owlory/owlory | yes | no | Apple CI/release adapter audit |
| gymphant/gymphant | yes | 2 | Swift gate/provenance audit |
| raelldottin/climate-closet-ios | yes | 2 | **PILOT** issue #2: check enforcement and App Store gate |
| raelldottin/tunory | yes | 1 | MusicKit release permissions and Apple adapter |
| alepes-com/alepes | yes; GitOps section | 5 | Financial safety and GitOps evidence alignment |
| alepes-com/alepes-macro-005 | no | no | Research-only AGENTS + Python CI, no orders |
| questsirl/questsirl | yes; GitOps section | 1 | Django image and reconciliation design |
| opnory/opnory | yes | 1 | CI and deploy authority/permissions |
| alepes-com/home_lab | no on default dev | no | Verify branch and local Compose/Terraform/Chef phases first |
| alepes-com/website | yes | 1 | Web preview vs production release checks |
| raelldottin/hermes-swarm-protocol | no | no | Verify canonical location of swarm wiki/policy |
| raelldottin/repo-automation | yes; GitOps section | 9 | Shared policy source; preserve pins |
| raelldottin/tachikoma | yes | 8 | Eval/run workflow and release provenance |
| raelldottin/hermes-agent | yes | many | Inventory existing upstream workflows before any patch |

This is a selected product/infra inventory, not an assertion that every owned GitHub repo has been onboarded. Exclude archived/training/forked repositories until approved. Repository names and visibility may change.

## Non-goals and release boundary

No code execution/publishing by adopting this policy alone; no auto-merge, no branch-protection bypass, no secret provisioning, no forced production deployment, no real money or brokerage orders, no automatic App Store signing, no shared hosting assumption across product organizations, and no unapproved modifications to existing owner workflows.

Adoption verdict per repository: POLICY_ONLY, CI_VERIFIED, RELEASE_VERIFIED, GITOPS_RECONCILED or BLOCKED. State only the level demonstrated by first-hand evidence.
