# S11A-3 private opportunity queue v2

This source-only slice implements issue [#84](https://github.com/pumni/threads-standalone/issues/84), under [#63](https://github.com/pumni/threads-standalone/issues/63) and roadmap [#57](https://github.com/pumni/threads-standalone/issues/57). It adds a separate v2 store and a typed intake from the accepted S11A-2 proposal batch. It does not alter S6 queue v1, add CLI wiring, or start a browser/network operation. Synthetic fixture data remains synthetic; live collection remains gated separately by #64.

## Store boundary

`OpportunityQueueV2Store(data_root)` stores one strict JSON document per account:

```text
<data_root>/opportunity_queue_v2/accounts/<account_uuid>.json
```

All operations use the existing S4/S6 account process lock through `ManualOutcomeStore.account_lock`. This serializes v1 and v2 account operations using the same lock identity. The v2 store has its own namespace and never invokes `OpportunityQueueStore.import_candidates` or writes an `opportunity_queue_v1` document. Writes use a bounded same-directory staging file, file flush/fsync and strict reread. Ordinary v2 updates use atomic replace under the account lock; migration uses atomic hard-link creation for no-clobber installation and fails closed where hard links are unavailable. Queue directories are created private where the platform supports mode bits; files are set to owner read/write mode where supported. Reparse points, symlinks, oversized files and path redirects fail closed.

The top-level v2 document has exactly these keys:

```json
{
  "version": 2,
  "account_uuid": "00000000-0000-4000-8000-000000000001",
  "opportunities": []
}
```

Each entry has exactly these keys:

```json
{
  "id": "00000000-0000-4000-8000-000000000002",
  "target_url": "https://www.threads.com/@author/post/post_1",
  "source": "SYNTHETIC_FIXTURE",
  "author_marker": "author",
  "author_identity_status": "UNVERIFIED",
  "context": null,
  "context_provenance": "SYNTHETIC_FIXTURE",
  "context_completeness": "UNKNOWN",
  "source_evidence": {
    "proposal_result_version": 1,
    "query": "design role",
    "excerpt": "bounded search excerpt or null",
    "position": 0,
    "search_context_status": "UNKNOWN",
    "context_status": "COMPLETE",
    "truncated": false,
    "search_observation_truncated": false,
    "search_result_truncated": false,
    "conversation_result_truncated": false,
    "conversation_observations": [
      {
        "target_url": "https://www.threads.com/@author/post/post_1",
        "author_marker": "author",
        "author_identity_status": "UNVERIFIED",
        "excerpt": "bounded reply excerpt or null",
        "position": 0,
        "parent_url": null,
        "is_root": true,
        "truncated": false
      }
    ],
    "reason_codes": ["AUTHOR_IDENTITY_UNVERIFIED"]
  },
  "created_at": "2026-10-12T00:00:00Z",
  "updated_at": "2026-10-12T00:00:00Z",
  "state": "NEEDS_CONTEXT",
  "skip_reason": null
}
```

The stored source union is exactly `MANUAL_URL | SYNTHETIC_FIXTURE`; `BROWSER_UI` is rejected. A manual entry has `author_marker=null`, `author_identity_status=UNKNOWN`, and `source_evidence=null`. Its context provenance is `NONE` without operator context and `OPERATOR_SUPPLIED` after context is entered. A synthetic entry preserves the bridge's unverified author, canonical target, search query/excerpt/position, source statuses, truncation flags, reasons, and each reply's original explicit parent. Synthetic source evidence is immutable through the public context/skip operations. The extra `query`, proposal result version, and reason codes retain bounded batch/proposal provenance that would otherwise be discarded.

For a synthetic-only entry, `context` stays null, `context_provenance` stays `SYNTHETIC_FIXTURE`, completeness is `UNKNOWN`, and state is `NEEDS_CONTEXT`, even if the fixture's conversation evidence is `COMPLETE`. Supplying separate operator-authored context changes only the operator context fields. `READY` can be requested only with `set_context(..., completeness="COMPLETE", operator_confirmed_complete=True)`; the boolean is the caller's explicit assertion of completeness. It does not verify that the author is the account actor or that Threads context is complete. `PARTIAL` and `UNKNOWN` operator context remain `NEEDS_CONTEXT`.

## Bounds and validation

- 20 active and 40 retained entries per account; skipped entries count toward retained capacity.
- One JSON document is at most 1 MiB. URL fields are at most 2,048 UTF-8 bytes; excerpts at most 500 characters; query at most 120 characters; operator context at most 4,000 characters; skip reason at most 1,000 characters.
- At most 20 proposal entries and 20 conversation observations per proposal. Positions are exact integers from 0 through 19; boolean values are not accepted as integers.
- UUIDs and entry IDs must be canonical UUIDv4 strings. Timestamps use exact UTC second precision (`YYYY-MM-DDTHH:MM:SSZ`). Canonical permalinks are checked and never converted to API media IDs.
- JSON duplicate keys, unknown/missing keys, wrong types, duplicate account targets/IDs, unsupported versions, invalid provenance, contradictory author/parent/root evidence and malformed parents fail closed with fixed codes. Exception text contains only the error code.
- Duplicate incoming manual URLs and identical synthetic evidence deduplicate by canonical permalink in first-seen input order. Repeated identical synthetic evidence is idempotent. A manual/synthetic source collision or different evidence for an existing synthetic target fails as a proposal conflict; it never relabels or overwrites the earlier source.

## Public operations

```python
store = OpportunityQueueV2Store(data_root)
manual_summary = store.import_manual_urls(account_uuid, canonical_or_tracking_urls)
synthetic_summary = store.intake_synthetic_proposals(account_uuid, proposal_batch)
queue = store.list(account_uuid)
entry = store.set_context(
    account_uuid,
    opportunity_id,
    operator_context,
    completeness="COMPLETE",
    operator_confirmed_complete=True,
)
skipped = store.skip(account_uuid, opportunity_id, reason="not relevant")
```

`intake_synthetic_proposals` accepts the S11A-2 `BrowserOpportunityProposalBatch` only. It checks the exact v1 result version, account UUID, source, canonical author/target, all bounds and evidence consistency before taking the lock or writing. It writes only `SYNTHETIC_FIXTURE` entries into the v2 namespace. The method has no browser, API, network, or CLI dependency. The UUID remains a local account partition supplied by the caller, not proof of a Threads login or actor identity.

## Explicit v1 migration

Migration is an opt-in two-step API; there is no read/startup migration and no CLI call site:

1. `preview_v1_migration(account_uuid)` takes the shared account lock, strictly reads the existing v1 document, and returns bounded counts, target-exists status and the SHA-256 of the exact v1 file bytes. It does not expose URLs or context text, and it creates/modifies neither queue document. The shared S4/S6 lock implementation may create its lock metadata when first used.
2. Review `preview.confirmation_token`. It is account- and source-byte-bound as `MIGRATE_V1_TO_V2:<account_uuid>:<sha256>`.
3. Call `apply_v1_migration(account_uuid, confirmation=preview.confirmation_token)` explicitly. Missing/wrong confirmation fails. A changed v1 file fails and requires a new preview. Existing v2 targets are rejected; no v2 file is overwritten.

The preview and apply methods each read v1 into one bounded byte snapshot, strictly decode that buffer with the S6 v1 decoder, and hash those exact same bytes while holding the shared account lock. Apply maps every entry to `source=MANUAL_URL`, sets identity to `UNKNOWN`, keeps `source_evidence=null`, and preserves IDs, canonical target URLs, operator context/provenance/completeness, timestamps, state and skip reason. It stages and strictly rereads v2, compares the decoded value to the intended queue, then installs with same-directory hard-link creation. Hard-link creation is an atomic create-only operation on Windows and POSIX filesystems that support it: if a v2 target appears at any point before installation, the migration fails without overwriting or deleting that target. Filesystems that do not support hard links fail closed; ordinary v2 updates continue to use atomic replacement under the account lock. Any staging/install failure leaves v1 byte-for-byte untouched and the staging file is cleaned up. The v1 file remains available as a rollback source; v2 is not automatically preferred by existing CLI code.

The preview token is a confirmation guard, not an authorization credential. Migration is local-only and does not authenticate a person. Existing v1 `READY` state is preserved only because v1 already records `OPERATOR_SUPPLIED` complete context; synthetic fixture completeness is never migrated from v1.

Migration and queue errors use fixed codes under `OpportunityQueueV2Error`, including `OPPORTUNITY_QUEUE_V2_INVALID`, `..._BUSY`, `..._LIMIT_REACHED`, `..._INVALID_PROVENANCE`, `..._PROPOSAL_CONFLICT`, `..._CONFIRMATION_REQUIRED`, `..._MIGRATION_SOURCE_CHANGED`, `..._MIGRATION_SOURCE_MISSING`, and `..._MIGRATION_TARGET_EXISTS`. No local content is placed in an exception message.

## Scope and follow-up

This PR does not update S6 queue v1, the shared CLI, S4 journal, S7 ranking, account metadata, browser runtime, or any production selectors. It performs no live Threads reads, browser launch, API mutation, reply, or engagement. Queue-v2 CLI integration, operator UX for reviewing migration tokens, retention/deletion policy, and any production browser source need separate scoped review. Live automated collection remains blocked on the separate #64 permission decision and UAT.
