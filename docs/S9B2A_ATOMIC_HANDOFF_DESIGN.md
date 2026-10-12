# S9B-2A: Atomic exact-approval to S4 reservation design

## Status and scope

This document and its synthetic tests specify and characterize the current
contracts. They do **not** implement a production reservation bridge, authorize
a browser launch, or perform a send. The existing public interfaces do not
provide an atomic S9A-to-S4 transition.

The only durable handoff record remains S4 manual outcome journal v1. Its
`HANDOFF_UNRESOLVED` event is a local reservation with `UNVERIFIED` status; it
does not establish that Threads received anything. S9B-2B needs separate
authorization before changing production code.

## Existing public contracts inspected

- `OpportunityQueueStore.account_lock(account_id)` delegates to
  `ManualOutcomeStore.account_lock(account_id)`. S6 `list`, `get`, `import`,
  `select`, `skip`, and `remove` acquire that account lock themselves. S6
  `remove` reads S4 while holding it and refuses to remove a target with a
  `HANDOFF_UNRESOLVED` event.
- `ManualOutcomeStore.account_lock(account_id)` uses a fail-fast filesystem
  process lock. `read` is unlocked. `begin_handoff` acquires the same lock,
  reads the full S4 v1 record, checks blocking/capacity rules, creates a new
  UUID for both `event_id` and `attempt_id`, and atomically persists a v1
  `HANDOFF_UNRESOLVED` event. It cannot accept the S9A-approved attempt ID.
- S4 keeps `HANDOFF_UNRESOLVED`, `AMBIGUOUS`, and
  `OPERATOR_REPORTED_SENT_UNVERIFIED` blocking. Only an operator's explicit
  `ABORTED_NOT_SENT` outcome permits a later local attempt. All statuses remain
  unverified declarations; none proves delivery or a closed hiring opportunity.
- `preflight_exact_context_approval` is pure. It rebuilds S9A's packet from
  caller-supplied current S6/S8 values and the supplied full typed S4 record,
  checks the approval fingerprint, exact attempt ID, and expiry, then returns
  `ExactContextPreflightPermitV2`. The permit's representation explicitly says
  `handoff=<not performed>`. S9A does not acquire a lock, persist/consume the
  approval, or reserve an S4 attempt. An S8 `KEPT_FOR_SESSION` disposition is
  only an editing-session state; the separate exact S9A human confirmation is
  still required.

The source-contract tests exercise these public boundaries with synthetic UUIDs,
permalinks, context, drafts, and temporary roots. The S4 calls in those tests
are independent S4 behavior checks or an intervening-history fixture. They do
not represent a successful atomic S9A-to-S4 transaction.

## Why separate preflight and reservation race

`OpportunityQueueStore.list` holds the shared account lock only while loading a
snapshot, then releases it. S9A preflight later checks that snapshot and the S4
record passed by its caller; S4 `read` does not lock. After preflight returns,
another S6 selection can change context or queue timestamps, or another S4
operation can append a record event. A later `begin_handoff` obtains the lock
and checks current S4 state, but it does not reload/revalidate S6 or the S9A
approval. The reviewed context, draft, queue, or complete S4 history can
therefore be stale at reservation time.

There is a second identity gap: S9A approval binds an explicit UUID `attempt_id`,
but `begin_handoff` generates a different UUID under its own lock. Calling
preflight followed by `begin_handoff` cannot prove that the durable pending S4
event is the attempt the human approved.

## Why the current locks cannot be nested

S6 and S4 use the same per-account lock path. Their public lock-taking methods
acquire it internally, and the filesystem lock is not reentrant. While an
outer S4 lock is held, calling `queue.list` attempts to acquire the same lock
again and fails with `OPPORTUNITY_QUEUE_BUSY`. While an outer S6 lock is held,
calling `begin_handoff` reacquires it and fails with `MANUAL_OUTCOME_BUSY`.
Ignoring these errors would be unsafe; allowing a blocking/reentrant variant
would risk deadlock or hide a second lock acquisition. `ManualOutcomeStore.read`
does not reacquire a lock, but an unlocked read alone cannot protect the
read-check-write sequence.

The missing public seams are deliberate implementation gaps for S9B-2B:

1. A lock-owning transaction entrypoint that acquires the account lock exactly
   once and makes the boundary explicit.
2. A reviewed S6 read-under-already-held-lock operation. Existing
   `_read_unlocked` is private and must not be called by an external bridge;
   public `list` reacquires the lock.
3. A reviewed S4 full-record read and reservation-under-already-held-lock
   operation. `read` is unlocked; `begin_handoff` reacquires the lock and
   chooses its own attempt UUID.
4. An append path that accepts and validates the approved `attempt_id`, uses
   that same UUID as the S4 v1 event identity required by the current codec,
   preserves v1 validation/capacity/atomic-write rules, and does not reacquire
   the lock.

## Proposed S9B-2B transaction contract

The bridge should expose one foreground, local-only operation, conceptually:

```text
reserve_exact_approved_handoff(
    account_uuid,
    exact_context_approval,
    current_editorial_session,
) -> ReservedManualHandoff
```

`ReservedManualHandoff` is an immutable in-memory result containing the exact
account, opportunity ID, canonical target, approved attempt ID, draft digest,
and the validated permit. It is not a send capability and should not expose
private context/draft through `repr` or error messages.

The transaction must do the following in order:

1. Validate the UUID and approval object's structural/fingerprint integrity
   before touching state. Acquire the existing shared account lock once. If
   lock acquisition reports busy or unsafe state, return a fixed safe error
   without writing.
2. While holding that lock, load the current S6 queue through a reviewed
   read-under-lock seam. Verify account UUID, selected opportunity ID,
   canonical Threads target, source `MANUAL_URL`, `READY` state, full context,
   `OPERATOR_SUPPLIED` provenance, and `COMPLETE` operator assertion.
3. While still holding the lock, load the full S4 v1 record for that exact
   account and target through a reviewed read-under-lock seam. Validate the
   complete event history and account/target fingerprint. Do not reduce the
   record to its latest status: S9A binds the full S4 snapshot.
4. Rebuild the S9A packet from that fresh queue, current reviewed draft, and
   full S4 record, then call the pure S9A preflight with the same account UUID
   and approved attempt ID. Sample trusted time inside the transaction after
   lock acquisition; do not let the operator or an old preflight supply the
   transaction's clock value. This rechecks exact source, context, draft,
   qualification, queue state, S4 history and approval expiry under the lock.
5. Check S4's replay/capacity rules against the just-read record. Reject
   pending, ambiguous, or operator-reported-unverified-sent records and any
   repeated/previous attempt ID. A prior `ABORTED_NOT_SENT` record may proceed
   only if the full record still matches the approved snapshot.
6. Append one v1 `HANDOFF_UNRESOLVED` / `SOFTWARE_HANDOFF` event whose
   `event_id` and `attempt_id` are exactly the S9A-approved UUID, whose draft
   SHA-256 is the approved final draft digest, and whose verification is
   `UNVERIFIED`. Use the existing bounded capacity and atomic replacement
   behavior. Do this through the reviewed S4 under-lock writer without
   acquiring the account lock again.
7. Release the account lock, then return the immutable reservation result.
   Any later native UI OPEN must be a separately authorized foreground step;
   it must not send or publish. The operator's explicit S4 resolution remains
   required. `AMBIGUOUS` and `OPERATOR_REPORTED_SENT_UNVERIFIED` remain
   blocking and cannot be auto-retried or promoted to verified delivery.

No `begin_handoff` call may occur inside this transaction because it
reacquires the shared lock and generates another attempt ID. No queue method
that acquires the account lock may be called from inside the transaction.

## Failure and recovery behavior

| Condition found under the single lock | Required result |
| --- | --- |
| Wrong account, opportunity, target, source, context, provenance, completeness, draft, or attempt | Reject with a fixed safe error; no S4 write |
| Queue changed after approval, even if still `READY` | S9A preflight rejects the changed queue fingerprint; no S4 write |
| S4 full history changed, including a new history with the same latest `ABORTED_NOT_SENT` status | S9A rejects the stale S4 snapshot; no S4 write |
| Approval expired before the under-lock check | Reject; no S4 write |
| Current S4 state is pending, ambiguous, unverified-sent, malformed, or contradictory | Fail closed; preserve current record; no new reservation |
| Account lock is busy | Return a safe busy result; no queue/S4 mutation |
| Journal is at its safe capacity, path validation fails, or atomic write fails before replacement | Return a safe local error; do not open UI |
| Process stops after the atomic replacement but before reporting success | S4 remains `HANDOFF_UNRESOLVED`; block replay and require explicit human reconciliation |

Recovery must not infer whether a browser opened or a remote send occurred. The
S4 v1 record stores target fingerprint, event/attempt identity, draft digest,
status, source, time, and `UNVERIFIED`; it does not durably store S9A's full
context or approval fingerprint. The transaction result is therefore
ephemeral. A process restart with an unresolved reservation must remain
blocked. Persisting a separate durable approval/audit record would require a
separate schema and authorization, not a silent S4 v1 change.

S6 currently permits context edits while an S4 attempt is pending. The atomic
reservation guarantees the approved S6 snapshot at the instant of reservation;
it does not freeze later queue edits. A future UI-open contract must use the
immutable result from that transaction and must not reconstruct a draft from a
later mutable queue snapshot. Any UI account identity uncertainty remains a
stop condition, and browser launch is outside this design-only task.

## Verification performed by this slice

`tests/test_s9b2a_handoff_lock_contract.py` uses only synthetic data and
temporary roots. It covers S4 durability/replay blocking and local abort
semantics, S6 protection of an unresolved target, S4/S6 shared-lock contention
and account isolation, and S9A rejection of changed S6/S4 snapshots. It does
not prove a production atomic bridge exists, nor does it perform network,
browser, API, or remote side effects.
