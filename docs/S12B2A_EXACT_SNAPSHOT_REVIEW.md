# S12B-2A exact-snapshot review contract

This slice adds a pure, in-memory equality check for a future private CRM
handoff review. It does not write files, call a CRM, authenticate an operator,
consume an approval, or authorize export. It performs no terminal I/O.

## Public API

`threads_platform.standalone.recruitment_crm_review` provides:

- `build_crm_export_review_packet(queue, full_s4_records,
  selected_opportunity_ids, exact_approvals, *, destination_identifier,
  now_utc) -> CrmExportReviewPacketV1`
- `required_crm_export_confirmation(packet) -> str`
- `preflight_crm_export_review_confirmation(packet, fresh_queue,
  fresh_full_s4_records, exact_approvals, selected_opportunity_ids,
  chosen_destination_identifier, explicit_confirmation, now_utc) ->
  "SNAPSHOT_MATCH_ONLY"`

`CrmExportReviewPacketV1` and its per-item rows are frozen, slotted dataclasses
with redacted representations. The packet contains the exact ordered selected
IDs, per-item field choices and URL opt-ins, approval purpose/assertion/timing
metadata, the action, an opaque destination identifier, creation/expiry times,
and fingerprints. It does not retain raw URLs, queue context, S4 event history,
local paths, or rendered handoff bytes. A future private frontend can render
its review from its own in-memory typed inputs; it must not use `repr()` as a
review screen.

The destination identifier must have the form `sha256:<64 lowercase hex
characters>`. This validates only its opaque shape. It does not normalize,
resolve, or prove the safety or identity of a filesystem path.

## Snapshot commitment

The version-1 commitment is SHA-256 over compact, sorted-key, strict UTF-8 JSON
prefixed by the domain separator
`threads-local/recruitment-crm-review-snapshot/v1\0`. The canonical payload
includes:

- every S6 queue entry in queue order, selected or not, with account UUID,
  target permalink, source, full context and context metadata, state, timestamps,
  and skip reason;
- every S4 record for every current queue target, including `None` records and
  the complete ordered event history with all event fields;
- the S6/S4/S9B-1/S12A schema version identifiers and the validated
  S9B-1 follow-up projection;
- ordered selected opportunity IDs and each corresponding complete S12A
  approval, including purpose, operator assertions, selected fields,
  individual `TARGET_URL` choice, approval issue/expiry values, target and queue
  commitments, and approval commitment;
- `EXPORT`, the bounded opaque destination identifier, the packet creation time,
  the earliest selected-approval expiry, and the SHA-256 of the public S12A
  handoff bytes.

S12A's public `build_recruitment_crm_handoff()` and S9B-1's public
`project_manual_followups()` validate the supplied typed state. No private
validators are imported. The S12A handoff remains bounded to 64 KiB; the
canonical snapshot is bounded to 1 MiB. S4 mapping input must be a plain `dict`
with exactly one key for each current queue target, matching S12A's contract.

The SHA-256 value is an equality commitment only. It is not a signature,
authentication proof, key-backed integrity check, legal-consent record,
independent verification of operator assertions, one-time approval, or
permission to write a file. A matching hash cannot prove that a caller supplied
fresh or trustworthy S6/S4 data.

## Confirmation and fail-closed behavior

The required exact phrase is:

`EXPORT <account UUID> <full 64-character snapshot fingerprint>`

The helper returns this value but never prints it. A future private-TTY
frontend must separately display the exact reviewed opportunity and field
choices, including each target URL, and collect a fresh caller-entered phrase.
The phrase is not a substitute for showing the review. It does not prove who
entered it, and an identical phrase can be replayed.

Preflight rebuilds the snapshot from the fresh typed S6/S4 inputs and exact
approvals. It rejects any change in the full queue, any S4 history event,
account, target, selection order, per-item field/URL choice, approval purpose
or expiry, destination identifier, action, or derived handoff. It also rejects
expired approvals, invalid timestamps, clock rollback, malformed packets, and
any confirmation that is not byte-for-byte equal. Errors expose only fixed
codes such as `CRM_REVIEW_STALE` and
`CRM_REVIEW_CONFIRMATION_MISMATCH`.

The only successful result is `SNAPSHOT_MATCH_ONLY`. It must never be
interpreted as `APPROVED_TO_EXPORT`, `FILE_WRITE_READY`, a remote CRM import,
or evidence of publication. Operator lawful-basis, rights, and consent values
remain `ASSERTED_NOT_INDEPENDENTLY_VERIFIED`.

**EXPECTED SECURITY GAP:** calling preflight again with the same packet,
inputs, confirmation, and time range can return `SNAPSHOT_MATCH_ONLY` again.
There is no durable redemption or revocation ledger in this slice. S12A
approvals remain reusable; this contract does not claim otherwise.

## Future integration requirements

A separately authorized S12B-2B implementation must provide the actual private
TTY review and confirmation flow, a trusted clock, and a one-lock reader that
returns the current S6 queue plus complete S4 history. Under that same account
lock it must recompute and compare the snapshot, durably redeem the exact
approval set, and coordinate redemption with a safe local no-clobber file
writer. The caller must not hold the lock while waiting for terminal input.
That future slice must separately test crash recovery, replay/revocation,
Windows/POSIX path and symlink safety, and downstream retention/withdrawal.

This API has no filesystem, terminal, browser, network, subprocess, Threads, or
CRM effects. Its `now_utc` value is supplied by the caller for synthetic
validation and is not a trusted production clock. No real customer data or
live Threads data is used by the tests.
