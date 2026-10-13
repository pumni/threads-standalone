# API-AUTO-02B: Private Review Context Preview

## Purpose

`api_auto_review_context` binds an already prepared `ReviewContentPacket` to
the full context an operator is expected to inspect. It returns inert,
in-memory data for a later private TTY step. It does not print the preview,
collect consent, create approval, persist data, call an API, or dispatch an
action.

This slice supplies the context validation gate for #74B-1A. It does not
implement the later TTY confirmation or approval store.

## Public API

- `ReviewDisplaySnapshot`: frozen, slotted input containing the current
  account, actor, action, source, provenance, candidate fingerprint, raw
  target IDs, full context text, and observation time.
- `review_display_context_sha256(snapshot)`: returns the versioned digest of
  valid normalized display context. It returns an empty string for invalid
  input. Observation time is validated but excluded from the digest.
- `build_private_review_preview(packet, scope, candidates, display, *, now)`:
  revalidates the content packet and current plan, matches one selected
  `ReviewIntent` to exactly one candidate, validates all display bindings,
  and returns a `PrivateReviewPreviewResult`.
- `PrivateReviewPreview`: frozen, slotted, repr-redacted snapshot of the
  exact target context, final packet text, rights evidence, packet revision,
  context digest, and effective expiry. It has no approval or dispatch
  authority.

The result decision is `READY_FOR_PRIVATE_TTY` only when every check passes.
`STOP` is returned for binding, context, packet, planner, input, or expiry
failures. `NOT_ELIGIBLE` reports a packet whose candidate is no longer
eligible. Failures return safe reason codes without including private values.

## Bindings and context hash

The display snapshot must match the packet and selected candidate on account,
actor, action, source, provenance, candidate fingerprint, and raw thread,
reply, root, and parent IDs. Its observation timestamp must exactly match the
candidate binding timestamp. The timestamp is deliberately absent from the
context digest, so a later observation of unchanged content has the same
digest; packet expiry and current revalidation still apply.

The context digest is SHA-256 over canonical JSON preceded by the
`threads-standalone:api-auto:review-display-context:v1` domain separator. It
commits to account and actor identity, action/source/provenance, candidate
fingerprint, raw target IDs, and the full normalized reply/root/parent text.
Text normalization uses Unicode NFC and converts CRLF or CR line endings to
LF. It does not trim, truncate, or replace context with an ID or digest.

For an own-account text post, source must be `OWN_ACCOUNT`; all remote target
IDs and reply/root/parent context fields must be absent. For a reply, source
must be `INBOUND_REPLY`; the thread ID must equal the root ID; all reply/root/
parent IDs and all three nonblank full texts must be present. The displayed
reply text must match the current `RemoteReply` text after the same
normalization. If parent and root IDs are equal, their displayed text must
also be equal.

Each context string is limited to 4,096 Unicode code points and 16 KiB in
UTF-8; the combined reply/root/parent text is limited to 24 KiB. Invalid
UTF-8, control/format/surrogate characters (except normalized newlines),
terminal escape characters, incomplete text, and oversized values stop the
preview. No text is shortened to fit.

The preview digest uses the separate
`threads-standalone:api-auto:private-review-preview:v1` domain separator. It
commits to the packet revision, display-context digest, packet binding, and
effective expiry (the earlier of packet and prepared-content expiry). Neither
digest establishes that an observation came from the official API; the
producer of candidate and display DTOs remains responsible for that boundary.

## Fail-closed behavior

The function first calls the accepted `revalidate_review_packet` path and
requires the same packet object to remain in `NEEDS_REVIEW` with no reason
codes. It then replans the supplied candidates and requires exactly one
non-dispatchable `ReviewIntent` matching exactly one candidate and the packet
binding. Planner `STOP`, `NEEDS_CONTEXT`, duplicate/collision/ambiguous
conditions, unexpected extra intents, or invalid batch outcomes stop preview
creation. Other candidates that are safely ineligible do not invalidate one
otherwise valid selected intent.

For replies, the current binding must contain a verified complete public
context, verified actor/source/relationship/root and reply controls, and a
non-operator-owned `RemoteReply` with exact matching reply/root/parent IDs.
Missing or mismatched full context always stops. The code never falls back to
fingerprints, IDs, or hashes as displayable content.

## Data and side-effect boundary

All inputs and outputs are ordinary immutable Python DTOs. The module performs
no filesystem, network, environment, wall-clock, terminal, or process I/O.
The caller supplies `now`; the function neither reads a clock nor sleeps.
There is no approval field, consent input, file/store writer, API client, or
publisher in this slice. Tests use synthetic fixtures and never claim
synthetic data has `OFFICIAL_API` provenance.

The preview is ephemeral and private because it contains the full context and
final content text. Callers must not log or persist its `repr`-hidden fields.
The later #74B-1B gate must display these exact bound fields and capture an
explicit operator confirmation before any approval design can proceed.

## Verification coverage

`tests/test_api_auto_review_context.py` covers valid synthetic post and reply
paths, full-context matching, later observation timestamps, account/actor/
target/text mismatches, context bounds and unsafe characters, packet and
permission/quota/freshness revalidation, expiry, candidate batches, safe
representations, and the module's no-I/O boundary. These unit tests do not
exercise live API calls, terminal consent, approval persistence, or mutation.
