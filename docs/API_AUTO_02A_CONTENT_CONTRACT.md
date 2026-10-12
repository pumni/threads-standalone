# API-AUTO-02A: operator-prepared content contract

## Scope

This slice binds operator-prepared text to one current API-AUTO-01 planner
proposal and returns a review-required in-memory packet. It is a pure Python
contract. It does not approve content, persist packets, invoke a draft provider,
make HTTP/API calls, or dispatch posts or replies.

The supported actions are an account's own text post and a reply bound to one
exact planner target. The module stores the account UUID and hashes for the
candidate fingerprint, target, context, content, policy revision, and packet
revision. It does not store a raw remote target or parent ID. The hashes bind a
proposal for comparison. This module never decodes a digest or retains a raw
remote target or parent ID.

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
hours apart, and within the planner proposal's expiry. Capability and quota
snapshots must be verified and fresh. The planner proposal must match the
account, policy, action, exact target and context hashes, candidate fingerprint,
permissions, and normalized content digest. A changed binding, stale window,
missing permission, exhausted quota, cooldown, ambiguous prior action, or known
duplicate packet cannot produce a review packet.

The candidate fingerprint is for re-selection and deduplication only. These
DTOs contain no raw root, parent, or API target IDs, and this module does not
decode digests into source values. Planner context digests must remain
independent of observation timestamps so a fresh observation of unchanged
context can be compared by the planner's context contract.

## Review boundary

Every valid result is `NEEDS_REVIEW` with `NEEDS_OPERATOR_REVIEW` state. The
result has no approval flag, dispatch flag, or approved packet type. The optional
`approval_input_for_review()` projection is inert metadata for a separately
reviewed consumer; it does not assert consent and cannot be treated as an
approval. A caller-provided set of known packet revision hashes supports pure
duplicate checks in memory. It is not durable replay protection.

The contract has no storage, queue, scheduler, browser, HTTP, API client, or
mutation dependency. All identifiers used as target evidence are digests. Raw
target IDs must not be added to this packet or its representation.

## Known bootstrap limitation — tracked by #135

The accepted planner and this content contract are not yet connected into an
end-to-end bootstrap. Calling `plan_cycle(scope, candidates, None, now=...)`
returns `OBSERVE_ONLY / DRAFT_UNAVAILABLE` without an `ActionProposal`, while
`prepare_packet(...)` requires an `ActionProposal` to create the first review
packet. The `_proposal(...)` helper in the unit tests constructs a synthetic
proposal to test content validation; it does not demonstrate a wired product
flow from observation to operator review.

Until [the design gate in issue #135](https://github.com/pumni/threads-standalone/issues/135)
is reviewed and accepted, #74B must not manufacture an `ActionProposal`, treat a
bare fingerprint or digest as an exact target, or treat `ApprovalInput` as
operator consent or durable approval. The #135 design must specify a pure,
fully evidenced, unapproved `ReviewIntent` for an eligible candidate, including
account UUID, API actor, source/provenance, target/root/parent/context digests,
verified permission and quota evidence, policy revision, and freshness. It must
define the path from that intent to an operator-authored review packet, explicit
TTY consent stored privately and bound to the exact packet revision, and fresh
official-API target reacquisition compared with that immutable approval before
any later guarded #76 dispatch proposal. The accepted planner must remain
fail-closed during that design.

Persistent #74B storage and approval implementation is blocked until the
coordinator reviews and accepts this interface through #135. This section
documents the current integration gap; it does not change planner behavior or
authorize bootstrap, persistence, or dispatch implementation.

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

`tests/test_api_auto_content.py` uses synthetic IDs and in-memory DTOs. It covers
operator-authored posts and exact-target replies; missing provider, rights, job
facts, and permissions; malformed or overlong Unicode; normalization and
content-hash changes; wrong account, policy, target, or context; synthetic/live
provenance mismatch; stale, expired, and not-yet-effective windows; quota,
cooldown, ambiguous prior action, duplicate packet, and unknown review state.
Every rejected or unavailable case returns without any HTTP call, store write,
approval, or remote mutation.
