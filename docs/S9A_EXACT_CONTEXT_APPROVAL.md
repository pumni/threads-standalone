# S9A exact-context approval

S9A adds a pure, in-memory approval contract for one S6 opportunity and its S8
final edited draft. It recomputes S7, binds the complete queue snapshot and the
current S4 outcome snapshot, and requires a separate exact human confirmation.
It does not call the S4 handoff, mutate the queue or journal, or authorize a
Threads send.

## Source chain and checks

The caller passes the currently read `OpportunityQueue`, the current
`OpportunityReplyEditorialSession`, the selected account UUID, and an S4
snapshot mapping keyed by canonical queue target. A snapshot may be the typed
`ManualOutcomeRecord` returned by `ManualOutcomeStore.read`, one of its typed
statuses, or `None`. An absent mapping entry means only `NO_RECORD`; it does not
establish that a post was never sent. Pass full records when available: S9A
commits their complete event history so a later event with the same final
status still invalidates an old approval. A status-only value binds only that
status. The caller must read the S4 journal immediately before packet creation
and again before preflight. S9A itself does no file reads or writes.

`build_exact_context_review_packet` validates that:

- The account and opportunity are canonical UUID v4 values and match S6 and S8.
- The S6 source is `MANUAL_URL`, the target is already the canonical Threads
  permalink, the queue entry is `READY`, and the full context is
  `OPERATOR_SUPPLIED` with `COMPLETE` operator-declared completeness.
- The complete stored context exactly matches the S8 candidate context. The
  context digest is ordinary SHA-256 over its exact UTF-8 bytes. S9A performs no
  trimming, case folding, whitespace collapsing, or Unicode normalization.
- S7 is freshly recomputed from the whole current queue and supplied S4
  statuses. The selected item must still be an eligible job seeker and its
  qualification must match the one used by S8.
- The final edited S8 reply is valid and no longer than 500 characters. Its
  exact UTF-8 bytes are committed with SHA-256. `KEPT_FOR_SESSION` is recorded
  only as S8 editorial state; it never counts as this approval.
- A changed queue entry, source, provenance, state, context, timestamp, S7
  result, S4 status, S8 disposition, account, target, or final draft changes the
  packet fingerprint. The queue-state digest covers all retained queue entries,
  including ordering and skip metadata, because those entries can affect S7
  duplicate and ranking signals.

The rendered packet includes the full reviewed context, exact final draft,
their digests, the canonical target, S7 reasons, queue-state digest, and S4
status plus a digest of the current S4 snapshot. It states that `COMPLETE` is an
operator assertion and that author identity and conversation coverage remain
unverified. `ABORTED_NOT_SENT` is shown as an operator declaration only, never
as evidence of remote success. Each source-context line is prefixed with `|` in
the display so multiline operator text cannot imitate packet labels; the
original stored context and its digest remain exact.

`write_exact_context_review_packet` uses the existing private-terminal gate:
stdin, stdout, and stderr must be TTYs, and CI environment markers must be
absent. The renderer returns sensitive content, so callers must not put that
string in logs, files, telemetry, or non-private output.

## Confirmation, token, and preflight

After the reviewer sees the complete packet, the caller must collect the exact
displayed phrase:

```text
APPROVE EXACT PACKET S9A V2 <packet SHA-256 fingerprint>
```

`approve_exact_context_review` rejects any other input and creates an immutable
version 2 token bound to the packet, exact confirmation digest, unique caller
supplied attempt UUID, and a bounded lifetime of at most 15 minutes (default
five minutes). Packet and token fingerprints use domain-separated SHA-256 over
canonical sorted compact JSON with an eight-byte length prefix.

`preflight_exact_context_approval` requires fresh queue, S8, S4 status, account,
attempt UUID, and current time inputs. It rebuilds the packet and compares all
bound fields. It rejects expired tokens, attempt mismatches, stale evidence,
and these S4 states:

- `HANDOFF_UNRESOLVED`
- `AMBIGUOUS`
- `OPERATOR_REPORTED_SENT_UNVERIFIED`

`ABORTED_NOT_SENT` is not one of those blocking states, but a changed S4 status
still invalidates an existing token because the status is part of its packet.
The returned `ExactContextPreflightPermitV2` is a comparison result only; it is
not a send capability and performs no handoff.

## Boundary for S9B

S9A has no persistence or secret key, so its fingerprints are commitments, not
signatures or proof of reviewer identity. The pure API cannot durably consume a
token or distinguish repeated calls using the same token, attempt UUID, and
unchanged snapshots. S9B must keep the token in memory, read the full S6 and S4
snapshots again, and coordinate its final preflight with the S4 account lock and
handoff reservation. A separate review is needed if S9B cannot make those
checks and reservation one serialized operation; a caller must not assume this
pure preflight closes a race with queue or journal changes. It must not treat
the permit as proof of `SENT`, infer actor identity, or infer conversation
coverage. A follow-up design would be needed before claiming globally enforced
single-use approval independent of the S4 journal.

The S9A module has no CLI entry point, provider, HTTP/API client, browser
startup, Playwright invocation, S4 handoff call, composer fill, queue write,
journal write, or send/publish path. No live Threads UAT is performed by this
contract or its synthetic tests.
