# API-AUTO customer acceptance contract (design proposal)

**Status:** API-AUTO-00 planning only. This document does not authorize source changes, live Threads calls, token handling, customer-root access, an EXE build, or distribution.

**Reviewed baseline:** `pumni/threads-standalone@b178d14ba55e535f5256e23862ad2c8fd401a88a` (`origin/main`, re-fetched and matched to the remote HEAD on 2026-10-12).

## Product boundary

The first customer release is a local, foreground, official-API recruitment workflow:

```text
account and permission preflight
  -> bounded official-API observation
  -> deterministic eligibility and ranking
  -> operator-authored approved content packet
  -> immutable account + target + final-content approval
  -> at most one existing journaled API mutation per cycle
  -> author readback / reconciliation
  -> bounded report and next foreground cycle
```

`OBSERVE_ONLY` is the default and cannot make a remote change. `REVIEW_QUEUE` can retain a private local proposal for an operator. `APPROVED_DISPATCH` can send only an unexpired, single-use approval whose account, exact target binding, action, final text hash, campaign revision, permission evidence, and time window still match at dispatch. New discovery, ranking, or a keyword match never grants approval by itself.

Release 1 is text posts and replies only. No public reply to a newly discovered person is sent automatically because a keyword matched. A public third-party target requires a separate target-specific review and explicit final text approval. No follow, like, quote, media, moderation, DM, CRM, browser automation, blog crawling, or external AI provider is in this release. AI may be added later behind a draft-only provider contract; an AI result must enter `NEEDS_REVIEW` until an operator approves the exact final packet.

## Existing implementation and evidence

| Area | Present on reviewed `main` | Evidence boundary / gap |
| --- | --- | --- |
| API read/runtime | `api/client.py` and `standalone/api.py` implement identity, quota, media, profile, profile-post, search, mention, reply, and conversation calls. Credential references resolve through the existing account model. | Synthetic HTTP coverage exists. The runtime does not locally inspect a token's granted scopes or prove Meta App Review access. A successful tester-token call is not customer permission evidence. |
| One-cycle Nurture | `RecruitmentSession.observe()` and `NurtureRunner` perform bounded API observation, deterministic selection, local receipt/state updates, account locking, cooldowns, and dedupe. | Observe is remote-read-only but writes local Nurture receipts/state. It is not a zero-write operation. The built-in recruitment preset leaves inbound identity/cap unconfigured by default. |
| Watch | `--watch-observe` is a foreground loop with bounded intervals/cycles and an account watch lock. | It repeats observation only. It does not plan, queue approval, pause/resume a campaign, or dispatch. Its cycles can write local receipts. |
| Existing apply path | `--apply-reply-file` / `--apply-post-file` review the exact input, compare the current selected target/candidate, require an interactive private terminal `APPLY`, and permit one journaled effect. | This is a one-shot operator approval, not a durable queue of previously approved campaign actions. It must remain usable and unchanged. |
| Mutation authority | `LocalThreadsMutationRuntime` and `LocalOperationStore` own API writes, account process locks, quota checks, durable phase boundaries, and `AMBIGUOUS` handling. `NurtureStore` owns existing receipts, fingerprints, cooldowns, and action reservations. | Reuse these authorities. Do not add another effect journal or change either v1 schema. Existing publish acknowledgement/journal phases are not an independent author readback and do not prove third-party visibility. |
| Planner input seam | `NurturePreview` / shortlist results carry an account-scoped target fingerprint, bounded reason codes, and private in-memory text; their `repr` and receipts omit raw target/content. | They do not expose raw root/parent IDs. API-AUTO must not weaken that privacy contract. Before #73, agree whether the existing fingerprint is the persisted exact-target binding and which new private in-memory adapter can carry root/parent evidence when available. Do not edit `nurture_discovery.py`, `nurture_runner.py`, or other shared files to solve this in the first slice. |
| Readback | API media and conversation/reply readers exist. | Recruitment apply does not currently compose a post-send readback/reconciliation phase. #77 must define it without resending. |
| Windows packaging | `scripts/run_recruitment.py` is a Python entry point; `pyproject.toml` currently declares Python 3.14 and the existing dependencies. | No PyInstaller spec, Windows EXE, clean-machine smoke, or bundled-runtime result exists on this baseline. Packaging feasibility is not shipping evidence. |

Issue #71 reports limited tester activity and also says that an end-to-end approved recruitment-session apply was not live-verified; one tester-B-to-A reply attempt was rejected before container creation. Treat those as operator-reported observations, not independently verified capability or production permission. The current source and synthetic tests establish code paths, not live service behavior.

## Proposed action and permission matrix

The permissions below are the candidate minimum scopes for the documented operation. They must be rechecked against current Meta developer documentation, the specific endpoint, the app's granted access level, and the customer's authorization before implementation enables an action. The Meta collection lists the Threads scopes and documents the relevant read, create, quota, and reply endpoints; scope names alone do not establish App Review approval or eligibility for customer accounts.

| Action | Candidate permission | API-AUTO decision |
| --- | --- | --- |
| Bind token to the selected local account; read `/me` identity | `threads_basic` | Read-only preflight. Require the returned identity to match the account binding. Missing, expired, or mismatched identity is `STOP`. |
| Read own posts / public profile posts | `threads_basic`; `threads_profile_discovery` where required by the discovery endpoint | Bounded reads only. Do not infer ownership from a configured username alone. |
| Search public posts | `threads_keyword_search` | Disabled unless exact app/user access is evidenced and a bounded query policy is configured. Search result is not consent to reply. |
| Read replies and conversations | `threads_read_replies` | Bounded read. Inbound configuration must prove the owner identity and exact post scope; partial coverage cannot be reported as “unanswered.” |
| Read mentions | `threads_manage_mentions` | Optional bounded observation only; no mention is an automatic approval. |
| Publish an original text post or create a reply | `threads_content_publish` | Eligible only in `APPROVED_DISPATCH`, after the exact packet/target approval, permission gate, quota check, durable reservation, and single existing journaled mutation. |
| Read post/account insights | `threads_manage_insights` | Optional read-only reporting; no conversion or “winning content” claim from engagement metrics alone. |
| Hide/unhide or approve/ignore a reply | `threads_manage_replies` | Out of release 1. Existing moderation CLI stays separate and never enters campaign dispatch. |
| Quote, image/video, carousel, delete, location tagging | Additional endpoint-specific permission/capability | Out of release 1. Existing mutation support is not automatic campaign authorization. |
| Follow, like, DM, CRM message | No supported release-1 action | `NO-GO`; no documented API path in this workflow and no workaround/private endpoint. |

This mapping is a design proposal based on Meta's official Threads API collection and the repository's documented endpoints; it is not a claim that any token has these grants. At runtime, a 403 or other permission failure stops the action with a sanitized result. Do not use a successful tester-token call as a substitute for the app's actual access level or an approved customer-use case. The current API client does not inspect token scopes; a future capability snapshot must be based on an operator-verified grant source and must never contain the token.

## Customer and target eligibility

| Candidate | Default decision | Conditions for review / dispatch |
| --- | --- | --- |
| Operator-owned original job/CV post | `REVIEW_QUEUE` | Operator-authored, truthful job facts; exact final text; source/rights recorded locally. Candidate CV or personal data requires a legitimate purpose and the candidate's authorization. No generated job facts. |
| Reply to a question on the operator's own post | `REVIEW_QUEUE` | Read permission, exact root/parent binding, complete enough conversation context, eligible reply control, relevant answer grounded in approved job facts, and target-specific final text approval. |
| Reply to another person's public post | `OBSERVE_ONLY` by default | A human must select the exact public target and review the same complete context and exact reply text. Search/mention and publish grants must both be verified. No keyword-only auto-reply, recipient harvesting, unsolicited batches, or inferred job-seeker identity. |
| Search result, mention, or profile lookup | `OBSERVE_ONLY` | Bounded official read under the exact required grant. A match can create a proposal, never an approval. Partial, stale, private, or provenance-uncertain context yields `NEEDS_CONTEXT` / `SKIP`. |
| Quote or media post | `NO-GO` for release 1 | Requires a separate scope, content-rights, media-hosting, and acceptance decision. |

Eligibility is limited to explicit job-related intent and a supported opportunity. Do not infer employment, protected traits, sensitive circumstances, or consent from a public post. Do not fabricate an applicant persona or manufacture a conversation. Unclear context, unsupported job claims, a changed target, duplicate, cooldown, or missing source provenance means no proposal or no dispatch.

The current authenticated-profile DTO contains identity fields but not a privacy/visibility field. Campaign configuration must therefore represent visibility as `PUBLIC`, `PRIVATE`, or `UNKNOWN`, with operator evidence and a review timestamp. `UNKNOWN` blocks claims of public reach. A private profile may publish to its authorized audience, but reports must not call that publicly visible. Only a separately authorized independent observation can establish `THIRD_PARTY_VISIBLE`.

## Typed planning, approval, and dispatch contracts

### Planner (`#73`)

Keep `plan_cycle(...)` pure: no HTTP, filesystem, browser, token resolver, clock reads, or mutation runtime. Proposed DTOs:

- `CampaignScope`: immutable account UUID, preset/campaign identity and revision, mode, allowed sources/actions, verified capability snapshot reference, quota snapshot, cycle timestamp, and explicit caps.
- `ObservedCandidate`: account UUID, source/provenance, exact-target binding, optional ephemeral root/parent references when evidenced, completeness, deterministic reason/rank, target fingerprint, cooldown state, eligibility state, and observation time. Raw target/context fields are excluded from `repr` and logs.
- `ActionProposal`: account UUID, operation (`POST_TEXT` or `REPLY`), exact target binding, source/candidate fingerprint, required permissions, content status/reference, final-content SHA-256 when available, expiry, and bounded reason codes.
- `PlanDecision`: `OBSERVE_ONLY`, `NEEDS_CONTEXT`, `NOT_ELIGIBLE`, `NEEDS_REVIEW`, `APPROVED_WAITING`, or `STOP`; never a “sent” state.

Existing Nurture fingerprints are the candidate persisted binding because they support exact-target re-selection and avoid storing raw remote IDs in the approval store. Before #73 is implemented, confirm that the fingerprint inputs distinguish the account, root post, and parent reply needed for a reply. If the contract cannot prove this, stop and propose a new API-AUTO-only in-memory observation adapter; do not broaden a shared v1 record. Dispatch must use Nurture's exact-fingerprint revalidation, and a missing or changed target must stop rather than select a replacement.

### Content and approval (`#74`)

Define a `ContentSource` protocol that returns `APPROVED_PACKET`, `NEEDS_REVIEW`, or `UNAVAILABLE`. Release 1 implements only an operator-authored local packet source. A future AI/provider adapter is draft-only and returns `NEEDS_REVIEW` until the operator approves the exact final text. Packets include provenance/source reference, account scope, allowed operation, target scope, text hash, and a local private content reference. No provider network call is in release 1.

An approval is immutable and single-use. Bind at least: approval ID, account UUID, exact target fingerprint (and operation), final UTF-8 text hash, packet ID/revision, campaign-policy hash, required permission set and verified capability snapshot, approver identity, approval time, `not_before`, and `expires_at`. Any change to account, target, parent, text, policy, permission evidence, or expiry invalidates the approval. Store only minimized private local state; never place live IDs, content, tokens, or evidence in this repository or CI.

### Dispatch (`#76`)

1. Revalidate account identity, grants, visibility policy, candidate provenance, exact target/context, content hash, approval window, cooldown, dedupe state, and both platform and local action caps.
2. Atomically reserve the approval for one dispatch; then use existing `NurtureRunner` reservation and `LocalThreadsMutationRuntime` / `LocalOperationStore` as the sole remote-effect authority. Do not call the HTTP client directly from the campaign coordinator.
3. Permit at most one remote mutation per cycle. `max_actions_per_cycle` is fixed at 1 for release 1. `max_actions_per_hour` and `max_actions_per_day` require explicit operator/customer values; missing values mean dispatch disabled. Also stop on platform quota exhaustion.
4. Link the approval to the existing Nurture receipt and operation ID. The new campaign/approval store tracks approvals and cycle coordination only; it is not a competing effect journal.
5. On any possible effect after the durable publish boundary, retain `AMBIGUOUS` and do not resend. Restart/recovery reads the authoritative journal and revalidates evidence; uncertainty remains blocked for operator resolution.

## Cycles, pause/resume, and recovery (`#75`, `#77`)

- Run in one foreground process. No service, background task, Windows Task Scheduler job, catch-up loop, or hidden retry.
- Use a bounded interval (proposed 15–1440 minutes, matching the current watch bounds) and an explicit finite cycle count for dispatch runs. Default mode is `OBSERVE_ONLY`; default action cap is zero until the operator enables a reviewed policy.
- Persist a strict, versioned, minimized campaign config/state separately from Nurture v1 and the operation journal. Use atomic writes, account-scoped process locks, path validation, and a dedicated campaign run lock. Use Nurture fingerprints/cooldowns for target dedupe rather than inventing a parallel target-history system.
- Anchor due times to the campaign schedule, skip missed cycles after downtime, and never replay an interrupted occurrence. On resume or restart, repeat account/permission/quota preflight; expired approvals require new review.
- Pause blocks new dispatch and does not cancel or reinterpret a request already in flight. Stop on Ctrl+C or fatal errors; an in-flight mutation with unknown outcome is `AMBIGUOUS`.
- Keep `PUBLISHED` (provider acknowledgement), `AUTHOR_READBACK_VERIFIED` (bounded author-side API readback matches returned ID, account, content hash, and target relationship), `THIRD_PARTY_VISIBLE` (separately proven), and `UNVERIFIED` distinct. Readback failure never triggers a publish retry.

## Stop and error matrix

| Condition | Decision | Recovery |
| --- | --- | --- |
| Missing/expired credential, identity mismatch, missing/unknown grant, or unverified app access | `STOP`; no mutation | Operator fixes account/app access out of band, then starts a fresh preflight. Never print or copy the token. |
| Unknown/private visibility where public recruiting reach is required | `STOP` or private-audience warning; never claim public reach | Operator verifies visibility. Private targets are ineligible for public recruiting replies. |
| Missing/partial context, unknown provenance, low/unsupported job relevance, or changed target | `NEEDS_CONTEXT` / `SKIP`; no approval | Obtain sufficient permitted context and review a fresh proposal. Never fall back to another candidate. |
| Content provider unavailable, content hash mismatch, stale/revoked approval, or exhausted cap | `STOP` / `NEEDS_REVIEW`; no mutation | Create and approve a new exact packet, or wait for a new explicitly bounded cycle. |
| HTTP 401/403/404 or deterministic rejected create/container | Safe classified failure; no automatic new target | Surface bounded error code; correct identity/scope/target and re-review. |
| 429 / rate limit on a read before dispatch | Stop the cycle; bounded backoff only when safe and explicit | Honor documented retry information; do not exceed cycle/action caps. |
| Timeout, cancellation, transport failure, invalid response, or persistence error after a possible write | `AMBIGUOUS`; no retry | Read journal and perform bounded author-side reconciliation. If evidence is insufficient, remain blocked for operator resolution. |
| Author readback missing or inconsistent after provider acknowledgement | `PUBLISHED` + `UNVERIFIED`, not failed-and-resend | Reconcile with bounded reads. Never issue a second publish to “fix” verification. |

## Proposed ownership and file plan

API-AUTO changes remain independent of open Post-S5 work. Initial ownership is limited to new `api_auto_*.py`, `tests/test_api_auto_*.py`, and `docs/API_AUTO_*.md` files. No dependency on unmerged #59/#70 branches and no edits to `__main__.py`, existing Nurture or mutation journal modules, browser source, `opportunity_queue.py`, root guide, or `pyproject.toml`.

Proposed sequence:

1. `#73`: `standalone/api_auto_plan.py` plus `tests/test_api_auto_plan.py`; pure DTO planner and fake `observe -> plan` flow. Resolve the fingerprint/root/parent interface question before implementation.
2. `#74`: API-AUTO-only content-source and immutable approval DTO/store design; operator-authored content only.
3. `#75`: API-AUTO-specific foreground cycle/pause/resume coordinator and strict versioned campaign state; no generic scheduler.
4. `#76`: narrow adapter to existing Nurture and mutation authorities; one journaled action max per cycle.
5. `#77`: bounded author readback and ambiguous recovery; no duplicate effect journal or retry.
6. `#78`: Windows packaging in a separately owned integration slice after branch ownership is released; no executable in this planning task.
7. `#79`: fake end-to-end suite, separately gated live UAT, customer runbook and handoff.

`#59` owns S7 qualification/ranking work; `#70` owns the synthetic browser-to-opportunity bridge. Keep API candidates separate from browser `SYNTHETIC_FIXTURE` provenance and existing S6 `MANUAL_URL` queue identity. If the API planner needs shared changes or either contract is incompatible, stop and propose an interface instead of editing those files.

**Repository-scope conflict:** the checked-in `AGENTS.md` still authorizes only SPLIT-08 on `codex/split-08-synthetic-state-rehearsal` and says later checkpoints are unauthorized. This API-AUTO-00 review follows the coordinator's later, explicit plan-only handoff and uses its isolated `origin/main` worktree. Before #73 source work, the coordinator must explicitly supersede or reconcile that repository guard; this task does not edit `AGENTS.md`.

## Windows one-file feasibility

The shape is technically feasible with a Windows-built, console-enabled one-file freezer around the recruitment entry point. PyInstaller documents bundling the Python runtime/dependencies and `--onefile`, and specifies that Windows builds must be produced on Windows rather than cross-compiled. This has not been built or tested here. Exact Python 3.14 and dependency compatibility, hidden imports, executable size/startup, signing/Defender behavior, and clean-machine operation remain implementation gates.

One file means the launch executable only. Keep account metadata, private campaign state, operation journals, content packets, logs, and credentials outside the executable in a protected per-user data directory. Preserve `THREADS_LOCAL_DATA_ROOT` and `env://` credential references; do not embed secrets or write state to the PyInstaller extraction directory. Start with the CLI/console entry point so stop states and private approval prompts remain visible. Do not bundle or download Chromium for this API-only path.

Release also depends on the open source-rights/license decision in #2 and explicit fresh-install/live-use gates in #18/#46. A successful test-token run or a built EXE is not a customer authorization or a license grant.

## Acceptance test matrix and evidence labels

All implementation tests use synthetic IDs/text, temporary roots, fake API/HTTP/clock objects, and no live network. At minimum cover:

- `OBSERVE_ONLY` never constructs a mutation runtime; no/1/20/over-cap candidates; stable ordering; two-account isolation; no raw target/text in repr, logs, receipts, or public fixtures.
- Wrong account, invalid provenance, private/unknown target, incomplete conversation, duplicate, stale target, missing permission, expired token, unavailable provider, content hash change, expired approval, and local/platform quota exhaustion all fail closed.
- Approval binds exact target + parent + final content; edits, retargeting, policy changes, replay, concurrent dispatch, and post-expiry use are rejected.
- One action maximum per cycle and explicit hour/day caps; pause/resume, stop, restart, missed-cycle skip, locks, and no catch-up behavior.
- Failure injection before and after each durable mutation boundary; `PUBLISH_REQUESTED` ambiguity blocks retries; provider acknowledgement and author readback have separate states; no false third-party visibility claim.
- API-only source/wheel/EXE does not import or instantiate Playwright or install Chromium. EXE smoke runs on a clean supported Windows target without preinstalled Python/uv and writes state only to the configured private user root.

Label all reports `SYNTHETIC`, `OPERATOR-REPORTED`, `APP-PERMISSION-VERIFIED`, `AUTHOR-READBACK-VERIFIED`, or `THIRD-PARTY-VISIBLE` with separate evidence. Do not use “production ready,” “fully autonomous,” or a success percentage until the relevant gates actually pass.

## Open decisions before dispatch is enabled

1. Customer-approved action/hour/day limits and the review authority for each account.
2. Exact permitted target classes and whether any third-party public reply is in scope after human review.
3. Source-approved owner/profile visibility evidence and behavior for a private/unknown profile.
4. Actual Meta app access level, review grants, authorized use case, and required token scope set for each customer.
5. Whether the current target fingerprint is a sufficient persisted exact binding for root/parent reply dispatch, or an API-AUTO-only ephemeral DTO adapter is required.
6. State-retention/deletion window and Windows credential provisioning method while preserving `env://` behavior.
7. Owner's source/distribution rights decision and the precise Windows/Python 3.14 packaging toolchain.

Until these are resolved, `APPROVED_DISPATCH` remains disabled and the customer acceptance is **NO-GO for unattended remote effects**.

## Source notes

- Local source reviewed at the baseline above: `api/client.py`, `standalone/api.py`, `standalone/mutations.py`, `standalone/nurture_runner.py`, `standalone/nurture_store.py`, `standalone/recruitment_session.py`, `standalone/recruitment_watch.py`, `scripts/run_recruitment.py`, `pyproject.toml`, `AGENTS.md`, and `docs/RECRUITMENT_SESSION.md`, `docs/RECRUITMENT_TERMINAL.md`, `docs/NURTURE_PARITY.md`, `docs/MUTATION_PARITY.md`.
- GitHub planning sources: [#71 API-AUTO epic](https://github.com/pumni/threads-standalone/issues/71), [#72 API-AUTO-00](https://github.com/pumni/threads-standalone/issues/72), [#73 API-AUTO-01](https://github.com/pumni/threads-standalone/issues/73), [#59 S7](https://github.com/pumni/threads-standalone/issues/59), [#70 S11A-2](https://github.com/pumni/threads-standalone/issues/70), [#18 fresh-start gate](https://github.com/pumni/threads-standalone/issues/18), [#46 live-UAT gate](https://github.com/pumni/threads-standalone/issues/46), and [#2 source-rights gate](https://github.com/pumni/threads-standalone/issues/2).
- Meta sources: [Meta Threads API collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api), [reply publishing request](https://www.postman.com/meta/threads/request/y4uzu58/respond-to-replies), [reply read request](https://www.postman.com/meta/threads/request/2oj5hld/get-a-list-of-all-a-user-s-replies), and [access-token debugger request](https://www.postman.com/meta/threads/request/mm48yqc/debug-access-token). The Meta developer documentation endpoint returned HTTP 429 during this review, so scope mapping remains subject to revalidation against the current developer docs at implementation.
- Packaging sources: [PyInstaller operating modes](https://pyinstaller.org/en/stable/operating-mode.html) and [PyInstaller manual](https://pyinstaller.org/en/stable/).

## Review record

This API-AUTO-00 review created this design document only. No Python code, tests, config, product schemas, or packaging files were changed. No tests or builds were run. No credentials were resolved, no operator root was accessed, and no Threads request or mutation was made.
