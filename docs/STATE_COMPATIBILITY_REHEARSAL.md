# SPLIT-08 synthetic v1 state rehearsal

## Status and boundary

- `SYNTHETIC_STATE_REHEARSAL=PASS` on local Windows; hosted Windows/Ubuntu acceptance is pending the exact Draft PR head.
- `REAL_STATE_REHEARSAL=NOT_PERFORMED / NOT_AUTHORIZED`.
- Destination base: `pumni/threads-standalone@db7a3236cad881f9ed097d99cebdc76e6ac7e6c4`.
- Frozen source, read-only: `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`.

The rehearsal generates all state in test temporary directories and uses synthetic aliases, UUIDs, provider IDs, fingerprints, timestamps and invented content. It never resolves a credential reference, starts Playwright, constructs an API or mutation runtime, contacts Threads, or opens an operator data root. It does not copy a browser profile or any real account, journal, cookie, token or evidence.

## Synthetic v1 fixture and links

The setup phase writes a case-preserving account alias under its case-folded filename with a credential reference only; eleven operation journals covering all seven v1 kinds; a completed and an active Nurture receipt; target state in `CONFIRMED`, `PENDING`, `AMBIGUOUS` and `NONE`; content candidates in `PUBLISHED`, `RESERVED` and `AMBIGUOUS`; one insights snapshot with both null and zero metrics; and a quota-only WorkflowPlan in a disabled Recurrence snapshot. The synthetic links retain generated account, run and operation UUIDs and the exact Nurture fingerprints. The carousel journal carries two synthetic child container IDs.

The fixtures include `PUBLISH_REQUESTED`, `MUTATION_REQUESTED`, `PUBLISHED`, `CONFIRMED`, `FAILED_FINAL` and `AMBIGUOUS` journal phases. The rehearsal reads `AMBIGUOUS` and in-flight records twice and verifies their phase, operation link and file bytes remain unchanged. It constructs no mutation runtime and sends no fake publish request, so this is a no-retry/read inspection check, not a claim that remote effects can be reconciled automatically.

Exact account, journal, receipt, target, content, insights, recurrence and workflow keysets are asserted against the v1 store contracts. Account UUID, case-folded alias path and credential-reference string are retained. Nurture target/content fingerprints, receipt operation links, snapshot fingerprint, nullable metrics, recurrence account binding, disabled status, fixed anchor and byte-identical Workflow snapshot are checked in a new interpreter.

The frozen SPLIT-00 prose lists the insights path without a preset directory. The frozen source code and destination code both store snapshots under `nurture/insights/<account-uuid>/<preset-id>/<content-fingerprint>/<snapshot-id>.json`; the synthetic test follows both implementations. This note records the prose discrepancy without changing a persisted schema or claiming a migration contract.

## Read-only inspection versus fixture writes

Fixture creation is explicitly write-expected and synthetic: account and journal stores write sample v1 documents; Nurture fixture setup acquires its synthetic account lock and writes receipts/state; recurrence setup creates and disables a synthetic recurrence. Those setup actions are not part of the zero-write audit.

The audit runs a fresh isolated Python process from a cold copy with `THREADS_LOCAL_DATA_ROOT` explicitly set, `PYTHONPATH` absent and no token values in its environment. It calls account/journal getters, Nurture v1 decoding/read helpers, recurrence `get`/`load_workflow`, and pure selection/cooldown helpers. The Nurture public owner lock is deliberately not acquired: acquiring it can repair stale `RUNNING` receipts. The synthetic active receipt is read directly and remains `RUNNING`. `FilesystemProcessLock.acquire`, API runtime construction, mutation runtime construction, browser runtime construction and socket connection are guarded to fail if attempted.

The zero-write audit does not run the CLI commands `nurture run` or `recurrence run`. Nurture observation can write receipts; recurrence run writes occurrence state. Neither command is used as evidence of a read-only scan. Stale Nurture `RUNNING` recovery is not exercised in this checkpoint because it is a separate expected-write scenario.

## Cold copy and rollback

The temporary fixture contains 22 files: one account, eleven operation journals, six Nurture documents/lock files, three recurrence files and one account lock. A file inventory records each relative path, bytes and SHA-256 in memory only. Cold-copy comparison verifies 22/22 files byte-for-byte and hash-for-hash before and after the new-process read; the original synthetic root also remains unchanged. No raw UUID or hash is printed.

Rollback is a separate synthetic write-expected simulation. After the cold-copy read passes, the test changes only the copied synthetic ambiguous journal to model a failed destination acceptance gate. It then selects a separate copy of the untouched synthetic backup as the rollback root. The original, backup and restored roots match across all 22 files; the restored ambiguous journal remains `AMBIGUOUS` with its original bytes. This exercises discard/restore without merging the destination copy or replaying a remote action.

## Corruption and safety coverage

The new failure matrix checks duplicate and oversized account state; duplicate and unknown journal fields; duplicate, unknown, bad fingerprint, bad UUID and bad timestamp Nurture target state; invalid content fingerprint; invalid insights metric; duplicate or invalid-time recurrence records; and a syntactically valid mutation step in a recurrence snapshot. Every rejected document remains byte-identical after parsing. Existing tests continue to cover symlink/reparse, path escape, account locks, journal transitions, duplicate JSON and sanitized errors. Windows symlink cases remain individually permission-gated; Linux keeps their coverage.

The cold reader validates `CONFIRMED` suppression only inside the saved Nurture cooldown window. It also confirms `PENDING` and `AMBIGUOUS` target states remain suppressed and recently `PUBLISHED` content is not due. This is bounded synthetic restart evidence; it does not claim full restart-safe idempotency beyond the existing state and cooldown contracts.

## Results

Local Windows, Python 3.14.7: all 17 new rehearsal tests passed; the complete suite reported 932 passed and 5 skipped (937 collected). The five skips are existing per-test symlink privilege skips, including the SPLIT-07 account symlink regression; there is no global skip. The same result was observed in a fresh installed-wheel environment outside the checkout. `uv sync --locked`, the locked runtime dependency tree, Ruff lint/format, Pyright, `uv build`, `threads-local --help` and `git diff --check` passed. The dependency tree contains no PostgreSQL, Controller, Worker or source-repository dependency.

For both local zero-write and rollback checks, 22/22 synthetic files matched byte-for-byte and by SHA-256 in memory; no raw hashes or identifiers were printed. Rollback restored the untouched synthetic backup with the ambiguous journal intact. The hosted run must retain the prior 920 tests and run all 937 tests plus installed-wheel smoke on Ubuntu and Windows with zero hosted skips, locked quality gates, build, dependency/privacy/architecture checks and full-history Gitleaks. Record exact hosted results in the PR description after that run completes.

No automatic migration, schema upgrade, live cutover or product-code change is included. Source provenance and public distribution rights remain as recorded in [PROVENANCE_DECISION.md](PROVENANCE_DECISION.md); issue #2 remains open.
