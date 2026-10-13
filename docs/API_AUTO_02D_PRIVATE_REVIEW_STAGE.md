# API-AUTO-02D: private review-only stage

This slice adds a small local store for content that has already passed the
API-AUTO-02A packet checks and the API-AUTO-02B exact-context preview. A stored
record is always `REVIEW_ONLY`. It is not approval evidence, a consent receipt,
an `ApprovedPacketSummary`, a reservation, or permission to dispatch.

The store is synthetic-only until the coordinator accepts a separate physical
retention and deletion policy for real operator content.

## Interface

`PrivateReviewStageStore(data_root)` requires an existing absolute, private
per-user data root outside the source checkout. It never creates or repairs the
caller-supplied root.

- `stage_review(packet, preview, scope, candidates, display, *, now)` rebuilds
  the accepted private preview from the supplied planner and display inputs.
  It writes only when the builder returns `READY_FOR_PRIVATE_TTY`, has no
  reasons, equals the full supplied preview, and remains within the original
  expiry window. It returns `STAGED` with an opaque stage UUID, or a fixed
  `STOP` reason code.
- `get_stage(account_id, stage_id, *, now)` validates the private record,
  account/path binding, schema, file layout, hashes, and expiry on every open.
  It returns only redacted metadata in `REVIEW_ONLY` state.
- `load_private_text(account_id, stage_id, *, now)` is the only operation that
  returns the normalized final text and the declared source attribution. Its
  result is redacted in `repr` and remains `REVIEW_ONLY`.

All timestamps are caller-supplied strict UTC values. Results and errors expose
only fixed reason codes. The store does not accept a TTY receipt or any approval
input. It imports no API client, credential resolver, browser, provider, or
dispatcher and has no network, CLI, prompt, or mutation path.

## Stored record

The namespace is independent from Nurture v1, campaign state, opportunity
queues, and the operation journal:

```text
<private-data-root>/api_auto_review_stage_v1/<account-uuid>/<stage-uuid>.json
```

Each bounded JSON record contains both immutable metadata and its private
content in one file. The metadata binds the account, action, source,
provenance, actor digest, candidate fingerprint, packet revision, preview and
display-context digests, target/context/root/parent/reply/context-evidence
digests, policy digest, content hash, rights declarations and attribution
digest, original content and review windows, required permission names,
capability/quota snapshot digests, observation times, quota values, and fixed
`REVIEW_ONLY` state. Account UUIDs are private local scope keys.

Only the normalized operator text and the bounded source-attribution
declaration are stored as readable content. Raw actor IDs, target/thread/root/
parent/reply IDs, visible root/parent/reply text, credentials, and customer
paths are not stored. The digests are not reversible target references. A
restart validates local record integrity; it cannot reacquire or authenticate a
remote target from those digests.

The record carries a SHA-256 integrity digest to detect partial or ordinary
uncoordinated edits. This digest is not keyed, is not a signature, and cannot
prove who edited a file or that a person consented. Filesystem ACLs protect the
record from other local principals; later dispatch code must independently
revalidate official API evidence and use a separately reviewed approval
authority.

## Filesystem safeguards

- The supplied root must exist, resolve without aliases, remain outside a
  detected checkout, and contain no symlink, junction, reparse point, or unsafe
  path component. Existing unsafe state is rejected rather than repaired.
- On POSIX, the root and created directories must be mode `0700`, files must be
  `0600`, and extended access/default ACLs are rejected. Record, lock, and
  temporary files must be regular single-link files.
- On Windows, the root and every ancestor are checked through the Windows DACL.
  The root must be private to the current user and trusted system principals;
  broader read or replace rights stop the operation. Newly created namespace
  and account directories receive an explicit current-user-only DACL through
  `icacls`, then the ACL is independently enumerated and verified. Lock,
  temporary, and record files are accepted only when their single-user ACL is
  verified from the secured account directory. `chmod` alone is not used as an
  ACL check.
- All operations validate a cross-process account lock. Record creation uses
  exclusive same-directory temporary files, file flush/fsync, and an atomic
  no-clobber rename. Linux also fsyncs the directory after publication. Windows
  uses the same-volume no-replace rename and file flush; Python does not expose
  a portable directory-fsync operation there.
- Restart detects bounded, correctly named, private orphan temporary files
  while holding the account lock and returns
  `STOP / INCOMPLETE_TEMPORARY_RECORD` without reading their content or
  deleting them. There is no automatic cleanup path; an incomplete private
  record remains available for explicit operator handling. A completed rename
  is still `REVIEW_ONLY`; failure after publication returns
  `PUBLICATION_AMBIGUOUS`, never approval or an automatic retry.
- A strict v1 allowlist rejects duplicate JSON keys, non-finite values,
  unknown fields, version drift, malformed UUIDs/hashes/timestamps/state,
  swapped account or filename bindings, partial records, invalid normalized
  text, and content/hash mismatches. There is no migration or silent repair.
- At most 64 records are retained per account. A packet revision or preview
  digest already present in that account cannot be staged again. Accounts have
  separate lock and record namespaces.

## Expiry and retention

The stage preserves the packet's original `expires_at`; loading never extends
it. `get_stage` and `load_private_text` return `STOP / EXPIRED` at or after that
time. Expired records remain on disk and count toward the 64-record cap. This
slice intentionally has no discard or cleanup API and makes no secure-erasure
or backup-removal claim. The 64-record and 32 KiB bounds limit storage size,
but a separate owner decision must set a physical retention/deletion period
before real content may be staged. Tests use new `tmp_path` roots and leave no
operator data behind.

## Synthetic acceptance

The dedicated tests cover planner → review intent → packet → exact preview →
stage → fresh-store restart, private content/hash consistency, and absence of
raw actor/target/context values from disk and `repr`. Negative cases cover
account/actor/target/root/parent/text changes, lost permission/quota evidence,
preview mismatch, duplicate staging, wrong-account reads, expiry, the 0/1/64/65
queue boundaries, and account isolation. Durability and filesystem tests cover
crash points around temp write/fsync/rename, ambiguous post-rename results,
orphan-temp detection and preservation, corruption and duplicate-key JSON, schema/state drift,
hard links, symlinks, ACL/mode violations, repository-root rejection, and
cross-process lock contention.

No test treats a stage or TTY receipt as consent, approval, a target recovery
mechanism, or permission to send. There are no real account, API, browser,
credential, or remote-mutation tests in this slice.
