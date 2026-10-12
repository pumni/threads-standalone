# S9B-2B: Offline atomic S9A to S4 reservation

## Scope

S9B-2B adds a foreground local transaction from an existing S9A
`ExactContextApprovalV2` to one durable S4 v1 reservation. It does not create an
approval, start Playwright, open Threads, fill a composer, send, publish, or
call a provider or network service. The S4 event remains
`HANDOFF_UNRESOLVED` / `SOFTWARE_HANDOFF` / `UNVERIFIED`; the receipt says
`NOT_SENT` and `UNKNOWN_IDENTITY`.

The entrypoint is:

```python
reserve_exact_approved_handoff(
    account_uuid,
    exact_context_approval,
    current_editorial_session,
    data_root=explicit_local_root,
)
```

The caller must already have shown the full exact S9A packet privately and
received its distinct human confirmation. S8 `KEPT_FOR_SESSION` alone is not
approval. The supplied editorial session must still contain that exact final
draft. The S9A preflight rebuilds the current S7/S8/S6 packet and checks the
approval fingerprint, account, opportunity, target, full context digest, final
draft digest, attempt UUID, and expiry.

## One-lock transaction

S4 issues an opaque `ManualOutcomeLockLease` while holding its existing
nonblocking per-account filesystem lock. The lease is bound to the resolved
data root and account UUID, becomes inactive when the lock context exits, and
is checked by every new under-lock S4/S6 operation. Existing public S4/S6
lock-taking methods keep their previous behavior.

The transaction acquires that shared S4-compatible lock exactly once and then:

1. Reads the complete fresh S6 queue through
   `OpportunityQueueStore.read_under_account_lock`.
2. Reads and validates all S4 v1 records and full event histories for the
   account through `ManualOutcomeStore.read_account_under_account_lock`.
   The scan is bounded at 1,024 records; a larger or malformed store fails
   closed. The selected opportunity's record is used in S9A's full-history
   snapshot comparison.
3. Samples aware UTC time while the lease is active and calls the pure S9A
   `preflight_exact_context_approval` using the fresh queue, caller's current
   editorial session, complete selected S4 record, approved attempt UUID, and
   current time.
4. Reads the queue and all S4 records again under the same lease. Any change
   since the preflight snapshots rejects the reservation.
5. Appends one S4 v1 event with `event_id == attempt_id ==` the approved UUID,
   the approved draft SHA-256, and `HANDOFF_UNRESOLVED` /
   `SOFTWARE_HANDOFF` / `UNVERIFIED`. The guarded S4 writer verifies the lease,
   rechecks all account records against the expected snapshot, enforces
   account-wide attempt-ID uniqueness and legacy S4 blocking/capacity rules,
   and reuses S4's existing atomic file writer without acquiring a lock.

No `queue.list`, `queue.get`, `begin_handoff`, or other lock-taking S4/S6
method is called inside the transaction. Busy locks fail immediately. There
is no retry loop.

## Rejection behavior

The operation rejects malformed account/approval bindings, changed queue or
S4 state, stale/expired approvals, mismatched target or draft, repeated attempt
UUIDs anywhere in that account's S4 journal, active unresolved handoffs,
ambiguous outcomes, operator-reported unverified sends, capacity exhaustion,
clock faults, invalid paths, symlinks/reparse points, and failed atomic writes.
Errors carry only fixed codes. A failed persistence operation does not launch a
browser or try again. If the process stops after the S4 replacement but before
returning its receipt, the persisted unresolved event blocks another attempt
and requires operator reconciliation.

## S9A snapshot input requirement

When the selected target already has S4 history, the S9A review packet supplied
to `approve_exact_context_review` must have been built with that target mapped
to its full typed `ManualOutcomeRecord`, not only its latest status string.
The bridge passes the fresh full record to S9A preflight so changes in earlier
events are detected even when the latest status remains
`ABORTED_NOT_SENT`. With no S4 record, an omitted outcome and an explicit
`None` both represent `NO_RECORD` under S9A v2.

S4 v1 stores only the target fingerprint, attempt/event UUIDs, draft digest,
timestamp, status, source, and verification. It does not persist context,
approval fingerprint, packet fingerprint, or the full reviewed draft. The
returned in-memory receipt is redacted in `repr`, but its `target_url` field is
available to the caller for the authorized next step. A durable approval audit
record or browser-open workflow is outside this change.

## Synthetic verification

`tests/test_recruitment_atomic_handoff.py` uses only synthetic accounts,
permalinks, contexts, drafts, S4 records, and temporary local roots. It checks
one lock acquisition, lease enforcement, durable v1 event identity, S9A stale
and expiry rejection, full context and draft changes, queue/S4 changes during
the transaction, S4 blocking states, account-wide replay prevention,
two-account isolation, failure cleanup, private receipt representation, and
the absence of socket or legacy handoff calls. No live Threads, browser, API,
provider, send, or publish operation is performed. Directory-symlink coverage
is skipped where the operating system does not permit creating synthetic
directory symlinks.
