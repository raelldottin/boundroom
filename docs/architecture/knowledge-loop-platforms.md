# SPEC-007: Knowledge loop on Discord, Slack or both

Status: **accepted (raelldottin/boundroom#197, merged 2026-11-12 after the owner's approval).** It amends [SPEC-003](discord-knowledge-loop.md). Each delivery slice below is still its own pull request with its own approval. RA-KNOW-006 is implemented: the `ChatPlatform` interface, its normalized models and a fake platform in `automation/knowledge/platform.py`, with the contract every adapter must pass in `automation/tests/test_knowledge_platform.py`. RA-KNOW-007 is implemented: the configuration model and loader for `~/.config/boundroom/knowledge.json` in `automation/knowledge/config.py`, with the repository's narrow-only scope and identity links, tested in `automation/tests/test_knowledge_config.py`. RA-KNOW-008 is implemented: the Discord adapter in `automation/knowledge/discord_adapter.py`, over an injected gateway and REST transport, tested on synthetic payloads in `automation/tests/test_knowledge_discord.py`. Nothing else is: there is no Slack adapter, no live Discord transport and no knowledge service yet.

Owner decision recorded here (2026-11-12): **the knowledge loop must work with Discord, with Slack, or with both.** This reverses SPEC-003's "Discord replaces Slack" and its "No Slack support" non-goal. Everything else in SPEC-003 stands: the five stages (Capture, Organize, Ask, Share, Evaluate), the strict trust boundary, the use of Hindsight and the Swarm Wiki, and the rollout gates.

## Problem

SPEC-003 names Discord in its capture rules, its retrieval and sharing rules, its slices and its non-goals. Supporting Slack by copying those rules would give two services with two trust boundaries and two sets of permission bugs. The platform-specific part is small: how messages arrive, how a user's access is checked, and how a reply is posted. That part should sit behind one interface, so every rule SPEC-003 sets is written, enforced and tested once.

## Decision (proposed)

1. **One knowledge service, platform adapters.** Discord and Slack are adapters behind a `ChatPlatform` interface, as `BoardBackend` is for boards ([SPEC-004](agent-agnostic-board.md)). Capture, organize, retrieval, sharing, evaluation and the trust boundary are platform-neutral code. An adapter only translates.
2. **None, either or both, chosen by the operator.** No platform is enabled by default. Each one is turned on separately in the operator's configuration, with its own credentials, workspace and channel allowlists. Enabling one never enables the other.
3. **Two separate roles per platform.**
   - `interface`: people ask questions and get answers there (Ask and Share).
   - `source`: messages there are captured as evidence (Capture).

   A platform can be an interface without being a source. That's the default when a platform is enabled, because asking needs far fewer permissions than capturing.
4. **Platforms are isolated by default.** Every piece of evidence records its platform, workspace and audience. An answer given on one platform may cite only:
   - evidence the requester can see on that same platform, checked at answer time;
   - non-chat sources, such as repository documents and reviewed pull requests, that the requester's project grant allows.

   An answer on Slack never cites Discord messages, and the reverse, unless the operator links the two identities explicitly and a reviewed share approves that destination.
5. **Slack capture waits on a terms decision.** Slack's API terms, as revised in 2025, restrict bulk export, training language models on API data, and keeping persistent copies or indexes of message data. Capturing Slack messages into Hindsight and the Wiki builds exactly such an index. So Slack's `source` role offers `off`, which is the default, and `capture`. `capture` can be enabled only for an internal app in the operator's own workspace, after the owner records how the terms apply. Using Slack as an `interface` stores no Slack history and isn't affected.
6. **The trust boundary covers every adapter.** An adapter's credentials and code can't reach `run_next`, a shell, the supervisor's policy or queue, Kanban writes, GitHub writes or consumer sync, as SPEC-003 and AGENTS.md require. Each adapter gets the same negative tests: no Discord or Slack message, command or reaction can start, approve or change anything outside the knowledge service.

## The interface

A `typing.Protocol` with pydantic models, so the knowledge service can be tested against a fake platform with no network.

| Member | Meaning |
| --- | --- |
| `kind` | `discord` or `slack` |
| `workspace` | The Discord guild or Slack workspace id this adapter serves. One adapter instance serves one workspace. |
| `roles` | The roles the operator enabled: `interface`, `source`, or both |
| `interactions()` | Yields normalized `Interaction`s: questions asked through a slash command or a mention, with the asking `Principal` and the `Place` (channel and thread) |
| `reply(interaction, answer, visibility)` | Posts an answer in the same place. `visibility` is `ephemeral`, meaning only the asker sees it, or `thread`. Nothing else is accepted. |
| `can_see(principal, source_ref)` | Asks the platform, at the time of the call, whether this person can currently read this source. Used at retrieval time and again at reply time. |
| `audience(place)` | Who can currently read a channel or thread, for checking a reply's audience |
| `fetch_since(place, cursor)` | `source` role only. Incremental capture of messages, edits and deletions after a cursor, tolerating rate limits and partial pages. |
| `publish(destination, candidate, receipt)` | Share only. Posts a reviewed, sanitized candidate to an allowlisted destination, and refuses one without an approval receipt. |

**Normalized models:**
- `SourceRef`: platform, workspace, channel, thread, message id and revision.
- `Principal`: platform, workspace and user id.
- `Place`: platform, workspace, channel and thread.
- `Interaction`, `Answer`, `ShareCandidate` and `ApprovalReceipt`.

Evidence records add `platform` and `workspace` to the provenance SPEC-003 already lists.

## How each platform maps

| Concept | Discord | Slack |
| --- | --- | --- |
| Workspace | Guild | Workspace. Enterprise Grid orgs are out of scope at first. |
| Connection | Gateway websocket, outbound only | Socket Mode, outbound only. Neither needs a public inbound endpoint. |
| Asking | Application (slash) commands and mentions | Slash commands and `app_mention` events |
| Private answer | Ephemeral interaction response | `chat.postEphemeral` |
| Reading history (`source`) | Needs the privileged Message Content intent | Needs history scopes, plus the terms decision in decision 5 |
| Edits and deletions | Message update and delete events | `message_changed` and `message_deleted` events |
| Access check | Channel permission overwrites and roles | Channel membership and channel type |
| Excluded by default | DMs, private threads, channels not allowlisted | DMs, group DMs, private channels, **Slack Connect channels shared with other organizations**, channels not allowlisted |
| Credentials | A bot token | A bot token (`xoxb-`) and an app-level token (`xapp-`) for Socket Mode |
| Notable platform rule | Data stored from Discord must be encrypted at rest, and the intent review asks whether data trains AI models. Boundroom trains no models. | Rate limits depend on distribution: an internal app keeps the standard history tier, while a commercially distributed app outside the Marketplace gets 1 history request per minute. The workspace's own retention policy must be honored. |

**Least privilege by role.** The `interface` role on Discord needs only `applications.commands` and `bot`, with View Channel and Send Messages in allowlisted channels. It needs no Message Content intent, because Discord delivers commands and mentions without it. On Slack it needs `commands`, `app_mentions:read` and `chat:write`. History scopes and the Message Content intent are requested only when `source` is enabled.

## Configuration

- **Where it lives:** the operator's file for the knowledge service, `~/.config/boundroom/knowledge.json`, validated by a pydantic model. It has a `platforms` list, empty by default.
- **What each entry holds:**
  - `kind`, `workspace` and `roles`;
  - the channel allowlist for each role;
  - the audiences answers may go to;
  - the share destinations;
  - the retention rules;
  - the names of the environment variables or secret references holding its tokens, never the tokens themselves.
- **No defaults that connect.** As with telemetry ([SPEC-006](agent-telemetry.md)), there is no built-in workspace, channel or service address. A platform with no workspace or credentials isn't enabled.
- **The repository can't enable a platform.** A repository may only narrow the knowledge service's scope, for example by removing channels. It can't add a platform, a workspace, a channel or a credential.
- **Identity links are explicit.** A link between a Discord user and a Slack user is an operator-maintained record, never inferred from matching names or email addresses.

## Requirements

Each requirement has an acceptance test that runs without network access, against the fake platform and synthetic payloads for each adapter.

| Id | Requirement | Acceptance test |
| --- | --- | --- |
| CP-1 | No platform is enabled by default | With an empty configuration, the service starts no adapter, opens no connection and reads no token |
| CP-2 | Each platform is enabled on its own | Enabling Discord alone starts no Slack code, and the reverse |
| CP-3 | `interface` without `source` captures nothing | An interface-only adapter never calls `fetch_since` and never requests history scopes or the Message Content intent |
| CP-4 | Platforms are isolated | A Slack question never cites Discord evidence, and the reverse, with no identity link. With a link but no approved share, still none. |
| CP-5 | Access is checked twice | Evidence the requester could see at retrieval but not at reply is withheld, with an abstention |
| CP-6 | Exclusions hold | DMs, private channels and Slack Connect channels aren't captured or answered in unless separately allowlisted, and Slack Connect never by default |
| CP-7 | Slack capture waits on the terms decision | `source: capture` for Slack is refused without the recorded owner decision and an internal-app declaration |
| CP-8 | Edits and deletions propagate | A deleted or edited source stops answering, as SPEC-003 requires, on both adapters |
| CP-9 | Messages are data | Injection payloads in messages, commands and retrieved text can't change scopes, destinations or tools, on both adapters |
| CP-10 | The trust boundary holds | Neither adapter's process can import or call `run_next`, the policy, the queue, Kanban writes, GitHub writes or the sync tool. Tested as in SPEC-003. |
| CP-11 | Credentials stay out of the repository and the logs | Tokens come only from the operator's secret references. A scan finds no token pattern in records, traces or evidence. |
| CP-12 | Shares need a receipt | `publish` refuses any candidate without an approval receipt for that exact destination |

## Effects on other documents

- **[SPEC-003](discord-knowledge-loop.md):**
  - Its decision sentence and the "No Slack support" non-goal are amended to point here.
  - Its rules that name Discord now apply to every enabled platform.
  - Its slices RA-KNOW-001 to 005 run over the `ChatPlatform` interface.
- **AGENTS.md:** the knowledge loop section names both platforms, and its rules apply to a Slack request exactly as to a Discord one.
- **[Product roadmap](../product-roadmap.md):** the "Already decided" line no longer says Discord replaces Slack.
- **[SPEC-006](agent-telemetry.md):** if the knowledge service later emits telemetry, it follows SPEC-006's user-owned destination rules. Nothing here requires it.

## Delivery slices

These come before SPEC-003's RA-KNOW-001 to 005, which then build on the interface. Each slice is one pull request by the agent, approved by a person. All of them are hermetic: synthetic payloads, a fake platform, and no real Discord or Slack calls in tests or CI.

| Slice | Delivers | Acceptance |
| --- | --- | --- |
| RA-KNOW-006 | `automation/knowledge/platform.py`: the `ChatPlatform` protocol, the normalized models, a fake platform, and a contract test suite that every adapter must pass | CP-1 and CP-2 against the fake platform. The contract suite is parameterized by adapter. |
| RA-KNOW-007 | The configuration model and loader: platforms, roles, allowlists, secret references, the repository's narrow-only rule, and identity links | CP-1, CP-2, CP-7 and CP-11. Refused fields are rejected with named reasons. |
| RA-KNOW-008 | The Discord adapter, using synthetic gateway and interaction payloads | The contract suite, CP-3, CP-6, CP-8 and CP-9 for Discord |
| RA-KNOW-009 | The Slack adapter in the `interface` role, using synthetic Socket Mode payloads | The contract suite, CP-3, CP-6 and CP-9 for Slack. Builds on RA-KNOW-012 and 013 in [part 2](knowledge-loop-slack.md). |
| RA-KNOW-010 | Cross-platform isolation and identity links | CP-4, CP-5 and CP-12 with both adapters enabled together |
| RA-KNOW-011 | Slack's `source` role, built only after decision 2 below is recorded | CP-7 and CP-8 for Slack, plus the retention-policy tests. Builds on RA-KNOW-014 in [part 2](knowledge-loop-slack.md). |

[Part 2](knowledge-loop-slack.md) specifies the Slack adapter's rate limiting, retry timers, idempotence and data rules, with slices RA-KNOW-012 to 014.

Live canaries need an approved host, the platform owner's approval for the workspace, scoped credentials from the owner, and SPEC-003's release gate. The owner's company computer can't host them: only Claude Code is approved there, and a Discord bot or a Hindsight server is not.

## Alternatives considered

- **A separate Slack service.** That means two trust boundaries and duplicated permission logic. Rejected for the reasons in [Problem](#problem).
- **Slack as the only interface, with Discord dropped.** The owner asked for either or both.
- **A bridge that mirrors one platform into the other.** It would copy messages across audiences, which SPEC-003's sharing rules forbid without review.

## Open decisions for the owner

**Decided by the owner (2026-11-12):** decision 2. **An internal Slack app may keep a derived index of its own workspace's messages.** A distributed app may not. This lets RA-KNOW-011, Slack's `source` role, be built. It stays off unless the operator enables it per workspace with `app: "internal"` and the recorded `slack_terms_decision` the configuration already requires (CP-7), and capture still follows part 2's data rules. Decisions 1 and 3 to 6 remain open, so no workspace is connected yet.


1. **Which platforms first,** and for which workspaces: a personal Discord guild, a personal Slack workspace, or a company workspace.
2. **Slack capture under Slack's API terms.** Whether an internal app may keep a derived index of its own workspace's messages. This is a reading of a legal agreement, not a technical call. Until it's recorded, Slack is an `interface` only.
3. **A company Slack or Discord workspace.** Installing an app there, and storing derived knowledge from it, is the company's decision: its workspace admins, security and legal.
4. **Hosting** for the live service, which can't be the company computer.
5. **Identity links,** if any: who maintains them, and whether cross-platform answers are ever wanted.
6. **Retention** of captured evidence for each platform, within each workspace's own retention policy.
