# Future offline operator read-only rehearsal checklist

## Authorization status

This is a generic checklist for a separate future action. `REAL_STATE_REHEARSAL=NOT_PERFORMED / NOT_AUTHORIZED`. SPLIT-08 authorizes synthetic state only. A future real-state rehearsal requires fresh explicit user consent and a separately scoped coordinator authorization before anyone opens operator state.

This document is in a public repository and contains no machine-specific paths, account values, identifiers, hashes, journal contents or private evidence. Any future real-state notes and evidence must remain on the operator's machine and must not be pasted into GitHub, chat, CI or this repository.

## Before a separately authorized rehearsal

- Obtain fresh written authorization for the exact machine, data root, read-only actions and time window. If any requested inspection is outside that scope, stop.
- Work offline. Do not sign in, resolve credentials, call Threads, start a browser, publish, reply, moderate, or run a workflow or recurrence.
- Stop old and new Standalone processes first. Do not proceed while an account, Nurture or recurrence writer may still be active. Do not delete a lock file to infer that a process is idle.
- Keep the original executable and data root untouched. Make any required backup outside both public repositories, verify it locally, restrict access to the operator, and do not copy browser profiles/cookies or token values into the backup procedure unless a separate authorization explicitly covers them.
- Use only a reviewed read-only inspection procedure that does not invoke `nurture run`, `recurrence run`, apply, browser or mutation runtimes. There is no authorized generic migration/scanner command in this checkpoint.
- Keep raw file paths, aliases, UUIDs, provider IDs, fingerprints, cursors, journal bodies, profile material, tokens, logs and per-file hashes local. Share only coordinator-approved aggregate results after scrubbing.

## Read-only checks and stop criteria

- Confirm account v1 keysets, alias filename binding and credential-reference shape without resolving the referenced environment variable.
- Confirm journal v1 keysets and operation links, including every in-flight or `AMBIGUOUS` phase. Treat an uncertain remote outcome as undecided; never retry it or infer success from an old snapshot.
- Confirm Nurture receipt, target, content and insights v1 links and fingerprints without starting observation or applying a draft. A running receipt or any lock/state inconsistency requires a stop; recovery writes are outside a read-only audit.
- Confirm Workflow and Recurrence snapshots are READ-only, account-bound and anchored. Do not invoke recurrence run to inspect them.
- Compare local byte/hash inventories before and after the selected read-only checks. Do not publish raw hashes or filenames.
- Stop on missing, extra, malformed, oversized, linked, ambiguous or otherwise unexpected state; on any inventory mismatch; on active writers; or if a check would write. Do not repair, rekey, normalize, merge or upgrade state during inspection.

## Backup, rollback and reconciliation

- Keep the original root and verified backup untouched until a separately authorized decision is made. A rehearsal failure means stop and retain both copies for an operator decision; do not copy selected files back over the original or merge journals.
- A rollback decision must select one known-good, untouched code/root pair while all writers are stopped. Never restore an older snapshot after an external mutation may have occurred.
- If any publish, reply, moderation, or other remote effect may have occurred, preserve the latest journal locally and stop for human reconciliation. Do not rerun from a restored snapshot or automatically confirm an ambiguous operation.
- Record the result locally. Report only a scrubbed decision and aggregate test status if the separate authorization permits reporting; never attach account state or private evidence to a public issue or PR.

Until a separate authorization and human decision exist, no real data root, browser profile, token, journal, migration, cutover or rollback is inspected or performed.
