# API-AUTO-01 observation planner

`threads_platform.standalone.api_auto_plan` is a pure planner over observations
already obtained by an upstream API/Nurture reader. It imports only the Python
standard library. It does not read the clock, resolve credentials, access files,
make HTTP or browser calls, or invoke a mutation runtime. The injected `now`
value and all read evidence must be UTC-aware.

The planner accepts immutable account, capability, quota, candidate, and exact
target DTOs. It checks their account/source/action scope, freshness, required
permission names, quota, cooldown state, and exact binding before it can return
an `ActionProposal`. Its hard bounds are 20 candidates and one proposal per
cycle. Candidate order is stable: account-owned post candidates first, then
score descending, source priority, source order, and original input order.

## Exact target and fingerprint

The fingerprint is treated only as an opaque dedupe/re-selection key. The
planner never parses it, uses it to construct a target, or accepts it as proof
of account, actor, thread, root, reply, or parent identity. A duplicate
fingerprint with conflicting binding evidence stops the cycle. A reply ID seen
with another account, actor, root, or parent also stops the cycle.

For an inbound reply, `VerifiedTargetBinding` carries the local account UUID,
verified API actor ID, root thread ID, and the existing API contract's
`RemoteReply` DTO. The planner checks that its exact reply ID, `root_post_id`,
`replied_to_id`, and `is_reply_owned_by_me` fields agree with the private
binding. The reply target is the selected inbound reply ID; the root and its
observed parent relationship are also included in both digests. The upstream
reader must prove that the root belongs to the selected account and that the
relationship is complete. Replies to keyword, tag, mention, or watched profile
search results are rejected: discovery is not consent to reply.

For an original post, the target binds to the verified local account and actor
with no pre-existing remote thread ID. Missing account identity, required read
permission, exact target IDs, parent/root relationship, or fresh context yields
`STOP` and no proposal. After a restart, the caller must reacquire a binding
through permitted official API reads; a saved fingerprint or digest cannot
reconstruct a target. A changed target or context fails the packet digest
comparison and stops the cycle.

`target_sha256` and `context_sha256` are equality checks over the private,
ephemeral binding and observed context evidence. Both must match the approved
packet before a proposal is returned. Observation timestamps are checked for
freshness separately and are excluded from the context digest, so rereading the
same context later does not invalidate approval. If a packet's candidate is
absent after restart, the planner returns `STOP / TARGET_NOT_REACQUIRED`.
These digests do not store or recover remote IDs and are not a private
target-reference store. If future API reads cannot reacquire the exact target
with the required permissions, implementation of a minimized private
target-reference store requires a separate design and review.

## Content, approval, and dispatch boundary

An `ApprovedPacketSummary` contains only account/action/fingerprint bindings,
target and context hashes, content hashes, and an expiry. It contains no final
text. Even when its exact evidence matches, this planner emits `NEEDS_REVIEW`
with `dispatchable=False`. Missing packet content produces `OBSERVE_ONLY` with
`DRAFT_UNAVAILABLE`; the planner never invents generic reply text.
`ContentSource` and `ActionDispatcher` are Protocol seams only and are not
called or implemented in this slice.

When `plan_cycle(scope, candidates, None)` accepts an eligible, fully bound
candidate, it keeps the `OBSERVE_ONLY / DRAFT_UNAVAILABLE` result and may attach
one frozen `ReviewIntent` to the first eligible item in the existing stable
order. The intent is an ephemeral review request: it carries no content,
approval, raw target IDs, or dispatch authority. It binds the planner's target
and context digests, verified action permissions and quota, policy, and the
freshness evidence; its expiry is the earliest evidence timestamp plus the
scope context-age limit. For inbound replies, root, parent, and reply reference
digests are separately domain-separated and derived only after the planner
checks the exact API reply relationship. Own-account posts have no prior
remote target reference. If the evidence lifetime is already over, the planner
keeps its existing result and emits no intent.

Supplying an `ApprovedPacketSummary` keeps the accepted proposal path and never
emits a `ReviewIntent`. The intent does not create a packet or serve as approval.
The separate #135-B slice must connect an intent to operator-prepared content
through its own inert review-packet type before any separately authorized
approval-store work can begin.

Every remote ID, account UUID, fingerprint, context hash, and content hash is
excluded from DTO representations. The planner has no journal or persistence
path. It cannot send, retry, reserve, or claim a result such as “sent.”
`AMBIGUOUS` prior action state stops planning so a later slice cannot treat it
as permission to retry.

## Synthetic acceptance coverage

`tests/test_api_auto_plan.py` uses only fixed synthetic UUIDs, IDs, timestamps,
hashes, and fake observation/dispatcher objects. It covers empty, single,
20-item, and overflow cycles; deterministic order and dedupe; account-owned
post versus exact inbound reply binding; account/actor/root/reply/parent/source
mismatches; target changes and restart reacquisition; stale/partial context;
missing API permissions; quota, cooldown, and ambiguous states; packet expiry
and mismatch; privacy-safe representations; and a fake observe-to-plan path
with zero dispatch calls. Synthetic provenance stays explicitly marked as
`SYNTHETIC_FIXTURE`; no fixture represents a live account or API response.
