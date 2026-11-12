# SPEC-007, part 2: Slack limits, retries and data rules

Status: **accepted** with [SPEC-007](knowledge-loop-platforms.md), in the same pull request (raelldottin/boundroom#197, merged 2026-11-12). It specifies how the Slack adapter stays within Slack's rate limits, handles Slack's delivery retries, and meets Slack's terms for data it handles. RA-KNOW-012, the Web API client (`automation/knowledge/slack_client.py`), is implemented and tested on a fake clock with a fake transport; it makes no real Slack calls. RA-KNOW-013, inbound delivery over Socket Mode (`automation/knowledge/slack_inbound.py`), is implemented and tested on a fake clock with fake connections and synthetic payloads; it opens no real connection. Its fixtures are synthetic, not recorded, so the three assumptions under [Sources](#sources) are still unverified. RA-KNOW-014 remains.

Owner request recorded here (2026-11-12): read Slack's API terms, and design idempotence, backoff and retry timers for Slack's rate limits and restrictions, where those are enough.

## Are retries enough?

**For rate limits and delivery, yes.** Slack's limits are published per method, its throttling answer says how long to wait, and its event deliveries carry unique ids. A client that throttles itself, honors `Retry-After`, retries only what's safe, and deduplicates what it receives meets them.

**For Slack's terms, no.** Most of Slack's restrictions are about what happens to the data, and no timer or retry changes that: deletion deadlines, a ban on training language models, limits on persistent indexes, encryption, consent. Those need the data controls in [Data rules](#data-rules). One terms rule *is* about rate: the API Terms forbid use that "exceeds rate limits". So the client must stay under the limits by design. Retrying after a `429` is a fallback, not the plan.

## Sources

Read on 2026-11-12. Quotes are short; the pages are authoritative.

| Document | What it sets |
| --- | --- |
| [Slack API Terms of Service](https://slack.com/terms-of-service/api), effective 2025-10-10 | No use that "exceeds rate limits", or that circumvents Slack's technical processes. No bulk export of message and file data without an additional agreement. API data may not train a large language model. Under the Data Access and Real-Time Search APIs, no "persistent copies, archives, indexes, or long-term data stores of other organizations' API Data". Caching only as "essential for the immediate operation", and deleted promptly. Stricter duties apply to apps offered to others outside your organization. |
| [Slack App Developer Policy](https://docs.slack.dev/developer-policy), effective 2024-12-10 | Strong encryption for stored data. Delete all associated data within **14 business days** of an uninstall or discontinuation. Honor users' deletion requests. Only necessary scopes. No LLM training "under any circumstances". Collect only with proper consent. No surveillance, and no profiles beyond what the app needs. |
| [Rate limits](https://docs.slack.dev/apis/web-api/rate-limits) | Limits are "per API method per workspace/team per app". Tier 1 is 1+ per minute, Tier 2 is 20+, Tier 3 is 50+ and Tier 4 is 100+. Design for about 1 request per second per method. A `429` carries `Retry-After` in seconds and blocks only that method, token and workspace. `chat.postMessage` allows about 1 message per second per channel. Event deliveries max out at 30,000 per workspace per app per hour, after which an `app_rate_limited` event arrives each minute. |
| [Rate-limit change for non-Marketplace apps](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/) | Commercially distributed, unlisted apps get 1 request per minute, and 15 objects per page, on `conversations.history` and `conversations.replies`. Internal customer-built (custom) apps keep 50+ per minute and up to 1,000 objects. |
| [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/) | Tier 3. Cursor pagination through `response_metadata.next_cursor` and `has_more`. Recommends no more than 200 per page. Returns `ratelimited` with `Retry-After`. |
| [`chat.postEphemeral`](https://docs.slack.dev/reference/methods/chat.postEphemeral/) | Tier 4. No idempotency key, so a retried call can post twice. |
| [Events API](https://docs.slack.dev/apis/events-api/) | Acknowledge within 3 seconds. Up to 3 retries: almost immediately, after 1 minute, and after 5 minutes, marked with `x-slack-retry-num` and `x-slack-retry-reason`. `event_id` is globally unique. Events more than 2 hours late aren't delivered unless Delayed Events is on, which adds hourly retries for 24 hours. Subscriptions are disabled if more than 95% of deliveries fail in an hour, for apps receiving at least 1,000 events an hour. |
| [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode) | Every payload is acknowledged by its `envelope_id`. Up to 10 connections per app. Disconnects come with reasons `warning` (about 10 seconds ahead), `refresh_requested` or `link_disabled`. Connections are refreshed regularly. Socket Mode apps can't be listed in the Marketplace. |

**What the sources don't say,** and the spec assumes until RA-KNOW-013 checks it with recorded fixtures:
- **The Socket Mode acknowledgement deadline.** The spec assumes the Events API's 3 seconds.
- **Whether Socket Mode redelivers a payload with the same `envelope_id`.** The spec assumes redelivery may happen with either the same or a new envelope.
- **The tier of `apps.connections.open`.** The spec assumes Tier 1.

## Decisions

1. **Boundroom is an internal (custom) app in each workspace, never a distributed one.** One app installation per workspace, created by that workspace's admins. This keeps Tier 3 history limits and keeps the app outside the duties for apps "offered for use by others outside your organization". It also keeps capture within the workspace's own data, which matters for the persistent-index rule. A configuration that declares a distributed app is refused for the `source` role.
2. **Throttle first, retry second.** Every Web API call passes a per-method, per-workspace limiter that stays under the published tier before Slack ever answers `429`.
3. **At-least-once in, exactly-once effect.** Slack may deliver anything more than once. Every handler is idempotent, keyed by Slack's own ids.
4. **At-most-once out, for posts with no idempotency key.** An answer whose post may or may not have landed isn't retried blindly. A share is reconciled before any retry.
5. **One app, one token per workspace.** No extra apps or tokens to multiply limits. That would circumvent the limits, which the terms forbid.

## Timers and budgets

Every timer reads an injectable clock, so tests run on a fake clock with no waiting and no network.

| Timer or budget | Value | Why |
| --- | --- | --- |
| Acknowledge an inbound payload | Within **1 second** of receipt, before any processing | Slack allows 3 seconds. Acknowledging first and processing from a local queue leaves 2 seconds of margin. |
| Limiter rate per (workspace, method) | **80% of the tier floor, and never above 1 per second.** Tier 1: 0.8 per minute. Tier 2: 16 per minute. Tier 3: 40 per minute. Tier 4: 60 per minute, held to the 1-per-second ceiling. | Slack publishes floors ("50+"), not exact limits, and asks for about 1 per second. The 20% margin covers clock skew and other app activity in the workspace. |
| Limiter burst | **3** calls | Small enough that a burst never looks like abuse, large enough for one question's lookups |
| Posts per channel | **1 per second**, burst 1, on top of the method limiter | `chat.postMessage`'s per-channel limit |
| After a `429` | Wait exactly `Retry-After` seconds, plus **0 to 1 second** of random jitter, for that method and workspace only. Then **halve** that limiter's rate for **10 minutes**, and restore it in 10% steps per minute. | `Retry-After` is authoritative. Jitter stops workers retrying together. Halving adapts when the floor is an overestimate. |
| `429` with no `Retry-After` | Treat it as **60 seconds** | Conservative. It shouldn't happen. |
| Transient failure (see [Errors](#errors)) | Exponential backoff with full jitter: a random wait between 0 and `min(cap, base × 2^attempt)`, base **1 second** | Standard backoff that avoids synchronized retries |
| — for a question being answered | Cap **8 seconds**, at most **3 attempts**, and a **20-second** deadline for the whole answer | A person is waiting. After the deadline, Boundroom posts a short "couldn't answer, try again" if it safely can, and stops. |
| — for capture and backfill | Cap **5 minutes**, at most **8 attempts** per page, then the channel's capture is marked failed for this run | Capture can wait. A failed channel doesn't block the others. |
| Circuit breaker per workspace | Opens after **5 consecutive** transient failures, or on any authentication error. Probes once after **5 minutes**, doubling to **1 hour**. | Stops a dead workspace from consuming retries, or looking like abuse |
| Deduplication memory | **25 hours** of processed `event_id`s and envelope keys, stored as hashes | Covers Slack's retries at about 0 seconds, 1 minute and 5 minutes, and Delayed Events' hourly retries for 24 hours |
| Stale question | Questions older than **10 minutes** on arrival are logged and not answered | An answer that late, after retries or a reconnect, would surprise the asker and may reflect old access |
| Socket Mode connections | Keep **2** open (Slack allows 10). On `warning` or `refresh_requested`, open the replacement before the old one closes. On an unexpected close, reconnect with backoff: base 1 second, cap 30 seconds, full jitter. | Two connections ride out a refresh. `apps.connections.open` is treated as Tier 1 until verified, which is why one connection is never enough. |
| `app_rate_limited` received | Record a gap for that workspace from `minute_rate_limited` until the events stop, and slow capture. In the `source` role, backfill the gap with `conversations.history` later. | Events dropped while over 30,000 per hour are never redelivered. |
| History page size | **200** messages per page | Slack recommends no more than 200 |

At these budgets, one workspace's capture reads at most 40 pages of 200 messages a minute, or 8,000 messages, which is plenty for a knowledge loop. A distributed app would be held to 15 messages a minute, which is why decision 1 rules it out for capture.

## Idempotence

| Operation | Key | Effect of a repeat |
| --- | --- | --- |
| Inbound event | `event_id` | Already seen in the last 25 hours: acknowledge it and drop it |
| Other Socket Mode payloads (slash commands) | `envelope_id`, and a hash of team, channel, user, command text and the minute received | Already seen: acknowledge and drop. The hash catches a redelivery under a new envelope, at the cost of ignoring an identical command repeated within the same minute. |
| Captured message | (`team`, `channel`, `ts`) | An upsert: the same message twice is one record |
| Edit | (`team`, `channel`, `ts`, `edited.ts`) | The newest `edited.ts` wins, whatever order the edits arrive in |
| Deletion | (`team`, `channel`, `ts`) | A tombstone that outranks any edit or re-capture of that message. Derived Hindsight and Wiki entries are withdrawn, as SPEC-003 requires. |
| Capture cursor | Per channel: the latest `ts` committed | Saved only after the page's records are committed. A crash re-reads the page, and the upserts make that harmless. |
| Answer to a question | The question's idempotency key | At most one answer per question. A post whose result is unknown (timeout, dropped connection) is **not** retried, because `chat.postEphemeral` has no idempotency key and a duplicate answer is worse than a missing one. |
| Approved share | A `share_id` stored in the posted message's metadata | Before any retry, Boundroom reads the destination for a message carrying that `share_id`. If it's there, the share is done. A share is never posted without that check. |

## Errors

Every Web API response is classified. A call is retried only when Slack definitely didn't act on it, or when the operation is idempotent.

| Class | Slack errors and conditions | Response |
| --- | --- | --- |
| Throttled | HTTP `429`, `ratelimited` | Wait for `Retry-After` and adapt, as in the timers above |
| Transient | HTTP `5xx`, `internal_error`, `fatal_error`, `service_unavailable`, `request_timeout`, connection refused, TLS failures | Back off and retry within the budget. A timeout on a post has an unknown result: treat it as in [Idempotence](#idempotence). |
| Access gone | `channel_not_found`, `not_in_channel`, `access_denied`, `no_permission`, `channel_is_limited_access`, `team_access_not_granted`, `ekm_access_denied`, `user_not_in_channel` | Don't retry. Treat the source as no longer readable: withhold derived evidence from it, as SPEC-003 requires for revoked access, until the next successful read. |
| Credentials gone | `invalid_auth`, `not_authed`, `token_revoked`, `token_expired`, `account_inactive` | Don't retry. Open the workspace's circuit breaker, stop the adapter for that workspace, and alert the operator. If the cause is an uninstall, the [purge](#data-rules) starts. |
| Configuration | `missing_scope`, `not_allowed_token_type`, `invalid_arguments` and the other `invalid_*` errors, `method_deprecated` | Don't retry. Report it as a configuration error naming the method. |
| Anything else | Unlisted errors | Don't retry. Record it, and add it to this table in a later change once understood. |

## Data rules

What retries can't cover. Each rule has an acceptance test in RA-KNOW-014.

| Id | Rule | Source | Timer |
| --- | --- | --- | --- |
| SD-1 | **Purge on uninstall.** On `app_uninstalled` or `tokens_revoked`, or if the operator removes the workspace, delete all data from that workspace: captured messages, derived Hindsight and Wiki entries, dedupe hashes, and cursors. | Developer Policy: 14 business days | Start immediately, target 24 hours, alert at 5 business days, and treat 14 as a hard deadline |
| SD-2 | **Deletions propagate.** A `message_deleted` event, or a message missing on reconciliation, withdraws it and everything derived from it. | Developer Policy: honor deletion requests; SPEC-003 | Withdrawn from answers at once. Derived entries are rebuilt within 24 hours. |
| SD-3 | **A user's deletion request** removes their captured messages and derived entries. | Developer Policy | As SD-1 |
| SD-4 | **No LLM training on Slack data,** ever. Retrieval for answers is the only use. | API Terms; Developer Policy | n/a |
| SD-5 | **Interface-only keeps nothing.** In the `interface` role, a question's text is held only while it's being answered, then deleted. Only its dedupe hash is kept, for 25 hours. | API Terms: caching only as essential for immediate operation | Deleted at answer time |
| SD-6 | **Capture is internal-only and decision-gated.** `source: capture` requires an internal app and the owner's recorded reading of the persistent-index rule. | API Terms; SPEC-007 decision 5 | n/a |
| SD-7 | **Encrypted at rest and in transit,** including the dedupe store, cursors and evidence | Developer Policy | n/a |
| SD-8 | **Minimal scopes per role,** as in SPEC-007, with each scope's reason in the app's description | Developer Policy | n/a |
| SD-9 | **No bulk export.** Capture only from allowlisted channels, incrementally. No workspace-wide archive or export path. | API Terms | n/a |
| SD-10 | **Breach notice.** A suspected compromise of Slack data alerts the operator at once, so they can notify users and Slack as the policy requires. | Developer Policy; API Terms | Immediate |
| SD-11 | **Retention matches the workspace.** Captured evidence is kept no longer than the workspace's own message retention, or the operator's shorter setting. | SPEC-003 retention rules | Checked daily |

## Delivery slices

These refine SPEC-007's Slack slices. RA-KNOW-012 and 013 come before RA-KNOW-009, and RA-KNOW-014 comes before RA-KNOW-011. Each is one pull request by the agent, approved by a person. All are hermetic, with a fake clock, recorded or synthetic Slack payloads, and no real Slack calls.

| Slice | Delivers | Acceptance |
| --- | --- | --- |
| RA-KNOW-012 | The Slack Web API client: the per-method limiter, per-channel posting, `Retry-After` handling with adaptive slowdown, error classification, backoff budgets and the circuit breaker | On a fake clock: the limiter never exceeds its rate. A `429` waits exactly `Retry-After` plus at most 1 second. Each error class gets its response. An unknown-result post isn't retried. Credentials errors open the breaker. |
| RA-KNOW-013 | Inbound delivery over Socket Mode: acknowledge first, the local work queue, deduplication, reconnection with two connections, stale-question dropping, and the `app_rate_limited` gap ledger. It also records fixtures that settle the three assumptions under [Sources](#sources). | Duplicates at about 0 seconds, 1 minute, 5 minutes and 24 hours have one effect. Acknowledgement precedes processing. A `warning` disconnect loses nothing. Gaps are recorded. |
| RA-KNOW-014 | Data rules SD-1 to SD-11: the purge job and its deadline alerts, deletion propagation, interface-only non-retention, encryption checks, and the retention sweep | An uninstall purges every store, checked by scanning them. Deleted messages stop answering at once. In interface-only mode, no question text remains after answering. |

## Open questions for the owner

These add to SPEC-007's list:
1. **Delayed Events.** Turn it on for the app, so events up to 24 hours late still arrive, at the cost of a longer deduplication window?
2. **Margins.** Are the 80% limiter margin and the 20-second answer deadline acceptable, or do you want them configurable per workspace?
