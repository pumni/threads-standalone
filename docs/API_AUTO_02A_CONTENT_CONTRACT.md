# API-AUTO-02A: operator-prepared content contract

## Scope

This module provides a pure Python contract for binding operator-prepared text
to an API-AUTO-01 planner result and returning a review-required in-memory
packet. The legacy proposal-based path remains available. The ReviewIntent path
revalidates its input with the planner and does not create an `ActionProposal`.
Neither path approves content, persists packets, invokes a draft provider, makes
HTTP/API calls, or dispatches posts or replies.

The supported actions are an account's own text post and a reply bound to one
exact planner target. Legacy proposal packets store the account UUID and hashes
for candidate fingerprint, target, context, content, policy revision, and
packet revision. ReviewIntent packets also store an actor digest and separate
root, parent, reply, and context-evidence digests. They do not store raw remote
target or actor IDs. The planner rechecks the exact target from ephemeral
observation DTOs; no digest is decoded or treated as a remote identifier.

## Text and evidence

The operator supplies the final text and its origin, attribution, usage-rights
declaration, job-facts digest, and effective time window. The contract accepts
operator-authored content and explicitly marked synthetic fixtures. AI and
external-source origins remain unavailable in this slice. `DraftProvider` is a
future protocol seam only; no provider is instantiated or called.

Text must be valid UTF-8, nonblank, and at most 500 Unicode code points after
normalization. The `nfc-lf-v1` normalization converts CRLF and CR to LF, applies
Unicode NFC, and preserves other text and spaces. The content digest is over
that exact normalized UTF-8 text. The 500-code-point limit is this contract's
local bound; it is not a claim that every platform surface has the same limit.

The operator must declare a supported rights basis, a right to use the text,
source attribution, and a valid job-facts digest and declaration. Operator
content requires operator-verified facts. Synthetic content requires synthetic
provenance for both content and planner observation. Missing declarations make
content ineligible; conflicting provenance stops evaluation.

Content preparation and expiry must be UTC aware, ordered, no more than 24
hours apart, and within the current planner proposal or ReviewIntent expiry.
Capability and quota snapshots must be verified and fresh. The legacy
proposal-based path checks the planner proposal's account, policy, action,
exact target and context hashes, candidate fingerprint, permissions, and
normalized content digest. The ReviewIntent path independently replans the
candidate before computing content and packet-revision hashes from normalized
operator text. A changed binding, stale window, missing permission, exhausted
quota, cooldown, ambiguous prior action, or known duplicate packet cannot
produce a review packet.

The candidate fingerprint is for re-selection and deduplication only. These
DTOs contain no raw root, parent, or API target IDs, and this module does not
decode digests into source values. Planner context digests must remain
independent of observation timestamps so a fresh observation of unchanged
context can be compared by the planner's context contract.

## Review boundary

Every valid result is `NEEDS_REVIEW` with `NEEDS_OPERATOR_REVIEW` state. The
result has no approval flag or approved packet type; `ReviewContentPacket` also
has `dispatchable=False`. The optional `approval_input_for_review()` and
`approval_input_for_review_packet()` projections are inert metadata for a
separately reviewed consumer; neither asserts consent or is an approval. A
caller-provided set of known packet revision hashes supports pure duplicate
checks in memory. It is not durable replay protection.

The contract has no storage, queue, scheduler, browser, HTTP, API client, or
mutation dependency. All identifiers used as target evidence are digests. Raw
target IDs must not be added to this packet or its representation.

## ReviewIntent bootstrap — #135

The pure content path now connects a planner-selected, unapproved
`ReviewIntent` to operator-supplied text without constructing an `ActionProposal`.
`prepare_review_packet(intent, prepared_content, scope, candidates, now=...)`
re-runs `plan_cycle(scope, candidates, None, now=...)`, requires one current
selectable intent with `OBSERVE_ONLY / DRAFT_UNAVAILABLE` and `proposal=None`,
and requires the supplied intent to equal that planner result. Planner STOP,
context, collision, ambiguity, permission, quota, and policy decisions remain
fail-closed.

`ReviewIntentBinding` is a distinct digest-only type. It carries account,
actor, action, source/provenance, fingerprint, target/context/root/parent/reply
and context-evidence digests, policy, required permissions, quota evidence,
observation times, and the intent expiry. Actor and remote IDs are not stored as
raw values. The planner's current exact-target comparison remains the authority
for binding those digests to the ephemeral observation DTOs.

`ReviewContentPacket` holds the normalized `PreparedContent` in memory with its
content SHA-256, a versioned and domain-separated packet-revision SHA-256, and
`NEEDS_OPERATOR_REVIEW` state. Content is hashed only after the existing
`nfc-lf-v1` normalization and text bound. The packet has no approval, consent,
store, journal, or dispatch authority. Existing scope, candidate-state,
prepared-content, and rights/job-facts validators are reused.

`revalidate_review_packet(packet, scope, candidates, now=...)` independently
re-runs the planner and checks the current verified account, actor, exact
target, context, root/parent/reply, policy, permissions, quota and candidate
state. The same target and context may be observed at a later time; freshness
timestamps alone do not change the packet binding. The packet's original
expiry remains fixed and is not extended by fresh restart evidence. Missing or
changed binding evidence, insufficient permissions, exhausted or changed
quota, stale evidence, or an expired packet yields no review packet.

`approval_input_for_review_packet(evaluation)` returns only the existing inert
`ApprovalInput` field projection from an internally valid review evaluation.
It does not assert TTY consent, create an `ApprovedPacketSummary`, persist an
approval, or authorize dispatch. Explicit operator review and private
single-use approval remain separate #74B work. The synthetic planner/content
DTO seam does not itself claim an authenticated official API observation.

## Requirements for the separately reviewed #74B store

Before persistent approval or queueing is implemented, #74B must define and
review the storage and operator-consent boundary. At minimum, the design needs:

- an explicit operator review surface that shows the bound account, exact
  target, final normalized text, provenance/rights declarations, and expiry;
- affirmative, explicit operator consent tied to the displayed packet revision;
- a single-use approval reference that is bound to account, target, context,
  content, policy, and packet revision, with atomic consume/reservation;
- account-isolated, versioned atomic storage and durable duplicate prevention;
- restart and crash recovery that stops on stale or unavailable target
  revalidation and never retries an ambiguous remote result;
- private handling for any raw IDs that business requirements prove necessary,
  with least-privilege access and no exposure through logs or representations;
- path and permission checks, including Windows ACLs, symlinks, and reparse
  points, if file storage is selected.

If the official API cannot reacquire the exact root/parent target after a new
cycle or restart, the future action must stop. A private target-reference store
needs a separate design and review if API reacquisition does not meet the
business requirement. This slice creates no such store and grants no approval
authority.

## Synthetic acceptance coverage

`tests/test_api_auto_content.py` preserves the proposal-based compatibility
coverage. `tests/test_api_auto_review_packet.py` uses fresh synthetic IDs and
in-memory DTOs to cover the complete `ReviewIntent` to review-packet path for an
own-account post and exact inbound reply, normalization, rights and revision
integrity, wrong or changed account/actor/target/root/parent/reply/context/
policy, refreshed evidence, restart, missing permissions, quota and candidate
states. Every rejected or unavailable case returns without any HTTP call,
store write, approval, or remote mutation.
