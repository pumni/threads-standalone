# S9B-1 local follow-up projection

S9B-1 provides an offline projection function over one caller-supplied S6 opportunity queue and a mapping of exact target permalinks to full typed S4 outcome records. It performs no file access, queue or journal reads, writes, browser work, network requests, polling, or sending. It adds no CLI command or durable state.

The projection preserves the supplied queue order and returns frozen metadata rows containing an opportunity ID, a local follow-up state and fixed reason code, an optional validated latest attempt ID and UTC timestamp, and a fixed manual next step. It never returns the target permalink, operator context, draft text, skip reason, or target fingerprint.

## States

- **NO_FOLLOWUP** means the supplied S4 mapping has no record for that exact queue target, either because the key is missing or its value is None. This reports only what the caller supplied; it does not prove there was no send or that a fresh store read found no record.
- **NEEDS_VERIFICATION** covers HANDOFF_UNRESOLVED, AMBIGUOUS, and OPERATOR_REPORTED_SENT_UNVERIFIED. The suggested next step is a private check of the native Threads UI. An operator report remains unverified and never becomes SENT, REMOTE_VERIFIED, or WAITING_FOR_REPLY.
- **CLOSED** is returned only when the latest valid S4 event is ABORTED_NOT_SENT. It describes the local handoff attempt according to the S4 record. It does not prove remote failure or success and does not mean the hiring opportunity is closed. An operator may reselect the opportunity if it remains relevant.

These are the only states. There is no scheduler, deadline tracking, reply detector, or automatic retry.

## Validation and account isolation

The input must be the existing typed S6 OpportunityQueue and a Mapping from canonical MANUAL_URL targets to full ManualOutcomeRecord values or None. Status strings alone are rejected.

Before projecting, the function validates:

- The queue account and every opportunity ID are canonical UUIDv4 values.
- The queue contains at most 40 retained opportunities and 20 active opportunities, with unique IDs and canonical, unique target permalinks.
- Queue source, state, context provenance/completeness, context bounds, timestamps, skip reason, and state/context combinations satisfy the S6 v1 shape.
- Each mapping key exactly matches a queue target; foreign, noncanonical, duplicate, and excess mapping entries fail closed.
- Every S4 record belongs to the queue account and has the target fingerprint computed by ManualOutcomeStore.target_fingerprint for that account and canonical target.
- Every full S4 event has a canonical UUIDv4 event and attempt ID, lowercase SHA-256 draft digest, a 20-character UTC ISO timestamp accepted by the S4 v1 reader, an allowed source/status, and an UNVERIFIED verification marker.
- Event histories follow the S4 v1 protocol: software handoff events are unresolved and start unique attempts; operator outcomes must match the current pending attempt and draft digest; a new handoff may follow only an aborted attempt; legacy operator events cannot follow the software protocol or a blocking legacy outcome.

Validation errors contain only fixed error codes. Result representations omit source content and target URLs. S4 records are revalidated in memory because their frozen dataclasses can also be constructed directly; the projection does not call the private S4 decoder or access a store.

## Snapshot use and limits

A caller preparing a real decision must load the complete, current S4 records from the protected local store immediately before use. The projection does not establish snapshot freshness or prevent another process from changing the journal after that read. S9B-2 must separately handle account locking and cross-process races before any future handoff.

This slice does not prove that a reported send occurred, determine whether a reply arrived, verify actor identity, or resolve a hiring opportunity. It uses only synthetic tests and makes no live Threads claim.
