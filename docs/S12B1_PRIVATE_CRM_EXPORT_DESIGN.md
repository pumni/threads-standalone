# S12B-1 private local CRM export design

Status: design and synthetic regression evidence only. This document does not
implement or authorize a file writer, CLI, CRM connection, API access, or export
of customer data. S12B-2 requires its own approved implementation scope and
operator UAT.

## Current S12A contract

The accepted S12A API is pure and in-memory:

- `create_recruitment_export_approval()` creates an immutable V1 approval for
  one active S6 opportunity. It commits to that account and the full S6 entry,
  including its canonical target and context. It records the fixed purpose,
  requested fields, review time, and operator assertions for lawful basis,
  rights, and consent.
- `build_recruitment_crm_handoff()` accepts a typed S6 queue, a complete typed
  S4 snapshot, selected opportunity IDs, matching approvals, and caller-supplied
  `now_utc`. It returns deterministic UTF-8 JSON bytes, capped at 64 KiB. It
  performs no file write, network call, browser action, or CRM operation.
- The default field set omits `TARGET_URL`. Each approval must explicitly
  include `RecruitmentExportField.TARGET_URL` for that one opportunity before
  its canonical URL appears in the packet.

S12A SHA-256 commitments are equality checks, not authentication, identity,
consent, legal permission, or proof of platform rights. The rights, consent,
and lawful-basis booleans are operator assertions and the packet labels them
`ASSERTED_NOT_INDEPENDENTLY_VERIFIED`.

**EXPECTED SECURITY GAP — reusable approval:** S12A has no durable approval
ledger, redemption, revocation, or trusted clock. A still-valid approval can be
passed to separate builder calls; both calls succeed and produce the same
bytes. Duplicate approval objects are rejected inside one packet, but that
does not prevent replay across calls. Approval expiry is checked against the
caller-provided time. S12A approvals also do not commit to the complete S4
history, selected set as a whole, output destination, or a confirmation action.
The new contract tests deliberately demonstrate reuse. They do not describe it
as safe one-time approval.

## S12B-2 operator flow

### Review without holding a lock

1. Resolve one local account alias to its exact UUID. Reject an invalid,
   missing, or mismatched account before review.
2. Read a typed preview snapshot, then release the account lock before any
   terminal input. The preview includes the account alias and UUID; exact
   selected opportunity IDs; each canonical target; each exact proposed field
   list; purpose; operator rights, consent, and lawful-basis assertions; and
   the existing S4 manual-only follow-up status. Show `TARGET_URL` separately
   for each opportunity and require a separate affirmative opt-in for each
   URL. Do not infer URL approval from a general export confirmation.
3. Render the exact review only to a verified private interactive terminal.
   Refuse CI, pipes, redirected input/output, and non-TTY sessions. Do not put
   raw targets, packet bytes, or personal content in command arguments,
   standard output, logs, shell history, or CI output. The private terminal is
   itself a sensitive display; operators must prevent screen capture and
   shoulder surfing.
4. Require an explicit confirmation phrase for the `EXPORT` action, exact
   account UUID, ordered selected IDs, reviewed field selections, destination,
   and full versioned snapshot fingerprint. The confirmation commitment is
   computed over all of those values; it is not a substitute for showing them.
   Any changed item, URL choice, field, purpose, destination, or fingerprint
   requires the operator to review and confirm again.
5. On interrupt, declined confirmation, or terminal loss, abort without an
   output file or approval redemption. Never hold an account lock while
   displaying a review or waiting for a person.

### Fresh one-lock preflight

After confirmation, acquire the account-scoped S4/S6 process lock once for the
final preflight and export transaction. Under that one lock:

1. Read the latest S6 queue and complete typed S4 history for every current
   canonical queue target. Include every event and its order, not just the
   latest projected status. A change to an older S4 event must stale the
   review even when the latest status is unchanged.
2. Recompute the versioned snapshot fingerprint and compare it with the exact
   preview fingerprint and confirmation. Revalidate account UUID, selected
   entries, canonical targets, context, per-entry fields, purpose, destination,
   and limits. Reject stale or contradictory state with fixed errors and no
   output.
3. Obtain UTC time from the trusted local system clock only after entering the
   lock. Do not accept `now_utc` or a clock value from the CLI in production.
   Use this time for approval expiry and redemption records. Clock rollback,
   invalid clock state, or a time outside the supported bound must fail closed.
4. Construct fresh S12A approvals and the packet from this exact snapshot.
   Validate the complete S4 mapping through the existing S9B-1 projection.
   `NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT` means only that no record was present in
   the read snapshot. `HANDOFF_UNRESOLVED`, `AMBIGUOUS`, and
   `OPERATOR_REPORTED_SENT_UNVERIFIED` remain `NEEDS_VERIFICATION`;
   `ABORTED_NOT_SENT` remains a local abort. None means a verified send, reply,
   conversion, or hire.

The existing `OpportunityQueueStore.account_lock(account_id)` delegates to the
same account lock used by `ManualOutcomeStore.account_lock(account_id)`.
`OpportunityQueueStore.list()` also takes that lock itself, so calling it
inside the final lock would be a nested acquisition and must not be used.
`ManualOutcomeStore.read(account_id, target)` reads a complete typed record for
one target without acquiring the account lock. For S12B-2, expose a narrow,
reviewed read-under-lock seam that returns the latest S6 queue plus S4 records
for every queue target while owning exactly one shared account lock. It may
reuse the existing unlocked S6 decoder and per-target S4 reader internally;
callers must not compose `list()` or another lock-taking public operation
inside it. Tests should prove concurrent S4/S6 writes cannot produce a mixed
snapshot. This seam is future work; S12B-1 does not add it.

The preview read used before terminal review is separate and short-lived. The
post-confirmation preflight is the single final lock acquisition that rechecks
all data and holds the lock through durable approval redemption and local file
commit. No prompt or operator wait occurs under that lock.

### Snapshot and approval binding

Use a new, explicitly versioned canonical fingerprint with a domain separator.
Its input must include:

- account UUID and complete ordered S6 queue fields for selected and
  unselected entries (including canonical target, state, source, context,
  context provenance/completeness, timestamps, and skip reason);
- all ordered typed S4 event fields for every current queue target, including
  account and target fingerprints, event IDs, attempt IDs, draft digests,
  timestamps, statuses, sources, and verification labels;
- ordered selected opportunity IDs, exact per-entry field allowlists and URL
  opt-ins, bounded purpose, destination identity, and action;
- schema/version identifiers for the fingerprint and source stores.

Canonical JSON must reject duplicate keys/non-finite values and use strict UTF-8.
The full fingerprint is local sensitive metadata; do not publish it with raw
targets or customer data. Include the full fingerprint in the explicit
confirmation binding. A digest is still only a commitment: the approval flow
must separately authenticate the local operator via the private terminal and
cannot prove legal authority or consent.

### Durable redemption, revocation, and withdrawal

S12B-2 needs a separately reviewed, private, account-scoped, versioned approval
ledger. It must use unique unpredictable approval IDs and store bindings to the
full S6/S4 fingerprint, account, exact targets, selected IDs, fields, purpose,
destination, confirmation action, issue/expiry time, and a fixed lifecycle
state. A random ID prevents guessing; it does not by itself prevent local file
tampering. Strict validation, private filesystem permissions, atomic writes,
account locking, and a separately reviewed integrity/key-management design are
still required.

Under the same final account lock, each approval must make one atomic state
transition from `AVAILABLE` to `REDEEMED` (or a non-replayable `RESERVED` state
before file commit). Expired, stale, revoked, withdrawn, consumed, or unknown
IDs fail closed. Revocation of an unused approval is a durable transition to
`REVOKED`; replay after restart must remain impossible. A multi-item export
must redeem the exact selected approval set as one bounded transaction or
leave every item unavailable pending recovery. A partial ledger update must
never make a redeemed approval available again.

Filesystem and ledger changes cannot be treated as one portable transaction.
Use a recoverable state machine: durably reserve/consume before publishing the
destination, bind the reservation to the exact packet digest and destination,
then mark committed only after the no-clobber file commit is durable. After a
crash, a reserved item stays non-replayable. Recovery may verify that the
destination is the exact committed file and finish the ledger transition; if
the state is uncertain, require a new human review and never overwrite or
re-export automatically. A failed write may consume an approval without
producing a file; this safe availability loss requires a new review.

Revocation cannot erase a file already created, a CRM import, backups, or
downstream copies. Provide a manual withdrawal procedure with a record of which
local export transaction and downstream recipient the operator must contact.
Do not claim a local revoke retracts CRM data, independently verifies consent,
or satisfies a provider's legal-retention policy.

## Local-only output sink

The future writer accepts one explicit absolute local destination, chosen and
confirmed by the operator. It must reject repo paths, `THREADS_LOCAL_DATA_ROOT`
and protected account/browser/credential roots, UNC/network/cloud paths, and
any destination whose locality or ownership cannot be verified. Do not accept
URLs, shell expressions, environment-variable references, or secret-bearing
arguments. Do not upload, sync, open, or contact the CRM.

Before creating anything, validate every parent component without following
links, verify the final parent remains the reviewed directory, enforce
owner-only access, and fail if the destination exists in any form, including a
symlink/reparse point. `Path.resolve()` followed by a later write is not a
TOCTOU-safe check. Bound bytes by the S12A 64 KiB packet limit. Write all bytes
to a uniquely named exclusive temporary file in the same validated directory,
loop on short writes, flush file data, then commit with an atomic **no-clobber**
operation. Sync the containing directory where supported. On failure, remove
only the temporary file created by this transaction; never recursively clean
or delete an operator directory or pre-existing destination.

### POSIX filesystems

Walk from a trusted directory handle using `openat`-style descriptor-relative
operations with `O_DIRECTORY` and `O_NOFOLLOW` for each component. Create the
temporary file with `O_CREAT | O_EXCL | O_WRONLY` and mode `0600` (subject to a
restrictive umask), write with a complete-write loop, then `fsync` the file.
`rename()` may replace a destination on POSIX and is not an acceptable
no-clobber primitive. Prefer an available, reviewed `renameat2(RENAME_NOREPLACE)`;
where unavailable, a same-filesystem hard-link from the completed temp file to
the absent destination can provide atomic no-clobber creation, followed by
unlinking only the owned temporary name. `fsync` the directory after commit.
Reject filesystems that do not provide the required semantics. POSIX mode bits
do not cover every ACL, network filesystem, mount, backup, or administrator
access condition.

### Windows / NTFS

Do not assume `os.replace()` is safe: it replaces an existing target. Validate
each parent handle and reject all reparse points, including junctions, symbolic
links, and mount-point reparse tags. Reject UNC paths and remote drives. Use a
reviewed Windows-specific implementation that creates the temp with
`CreateFileW(CREATE_NEW)` and no-follow/reparse-point checks, applies a
current-user-only DACL, writes fully, and calls `FlushFileBuffers`. Commit on
the same volume with a no-replace move (for example, `MoveFileExW` without
`MOVEFILE_REPLACE_EXISTING`, with write-through requested); any existing target
must cause a safe failure. Verify handles, volume, file identity, and directory
state around the rename. Do not substitute `os.replace()` if the native
primitive is unavailable.

Windows path validation and rename behavior must be covered by hosted Windows
tests for existing files, dangling symlinks, junctions, parent replacement,
short writes, and crashes. ACL inheritance, antivirus/indexer interference,
open handles, redirected folders, cloud-sync clients, and filesystem drivers
can change observed behavior. If the platform API cannot guarantee the
required no-follow and no-clobber properties, fail closed and defer export;
document tested filesystem limits rather than claiming universal safety.

## Failure and recovery contract

| Condition | Required behavior |
| --- | --- |
| Wrong account, UUID, target, selection, context, field list, or purpose | Fixed error; no approval redemption or file. Re-review exact current data. |
| S6 changes, any S4 event changes (even if latest projection is unchanged), or target changes after review | Snapshot mismatch under the final lock; no file. Do not silently substitute another opportunity. |
| S4 history is malformed or contradictory | Reject the complete snapshot. Never guess a send, reply, or outcome. |
| Expired, withdrawn, revoked, replayed, or already reserved approval | Reject. Require fresh private review; never make a consumed item reusable. |
| `TARGET_URL` was not individually opted into | Omit it. If a URL appears in fields without exact per-item approval, reject the entire packet. |
| CI, pipe, redirected terminal, or non-private TTY | Refuse review/export before disclosing targets. No stdout fallback. |
| Declined prompt, interrupt, EOF, or session loss before commit | Abort; do not write. If redemption was already durably reserved, keep it non-replayable and require recovery/re-review. |
| Lock contention or unsafe/stale state | Return a fixed bounded error; do not retry automatically. |
| Existing target, dangling symlink, symlink/junction in a parent, parent swap, or unsafe path | Fail before writing. Never follow, truncate, replace, or delete the competing path. |
| Short write, flush/fsync failure, or atomic no-clobber commit failure | Do not publish a partial destination. Clean only this operation's known temp file; retain a consumed/reserved approval if state was committed. |
| Crash after file commit but before user response or final ledger update | Destination remains complete and approval remains non-replayable. On restart, verify transaction ID, digest, destination identity and ledger state; otherwise stop for operator reconciliation. |
| JSON copied, emailed, backed up, or imported downstream | Local revoke cannot recall it. Follow the manual downstream withdrawal/retention procedure. |

Error text and audit records should use fixed codes and opaque transaction IDs.
Do not log targets, private context, full packet bytes, account credentials, or
personal data. Diagnostic output must distinguish `NO_FILE_CREATED`,
`COMMIT_OUTCOME_UNKNOWN`, and `FILE_COMMITTED`; never imply a remote CRM import.

## Synthetic contract tests and remaining gates

`tests/test_s12b1_crm_export_security_contract.py` exercises only the existing
public S12A API with new typed S6/S4 fixtures. It checks account isolation,
default URL omission, per-item URL opt-in, stale S6 approvals and expiry,
conservative S4 projection labels, deterministic bytes, the documented replay
gap, and the builder's lack of disk/network/process effects. It does not fake a
TTY writer or assert that S12B-2 exists.

These source tests are not private-terminal UAT, a filesystem safety proof,
customer-data validation, CRM provider compliance, legal review, or API scope
approval. Before S12B-2, separately authorize the reviewed S4/S6 snapshot seam,
versioned redemption/revocation state, platform-specific safe-file writer,
error recovery, downstream retention/withdrawal procedures, and real operator
UAT. No current S12A API or S12B-1 test authorizes Threads access, publishing,
browser actions, CRM network requests, scheduling, or customer exports.
