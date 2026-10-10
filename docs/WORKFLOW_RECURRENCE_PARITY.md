# SPLIT-06 WorkflowPlan and Recurrence parity

## Baselines and boundary

This checkpoint ports the existing Standalone WorkflowPlan and Recurrence behavior from read-only source `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1` to destination base `pumni/threads-standalone@eed547afbaa76fbf25d1f05a17b37c70eaa1ef73`. The package and executable remain `threads-standalone` and `threads-local`; the Python namespace remains `threads_platform`.

The transfer reuses the destination account, API READ, browser READ, process-lock, and SPLIT-04 operation-journal contracts. It adds no Controller, Worker/WorkerJob, PostgreSQL, distributed scheduler, old-repository runtime dependency, or dependency. Account and operation-journal schemas are unchanged.

## WorkflowPlan v1

CLI: `threads-local workflow run FILE`.

The loader reads at most 65,536 bytes, rejects invalid UTF-8/JSON, duplicate JSON keys, unknown keys, aliases outside the bounded account-name contract, and plans outside 1–32 steps. The top-level key set is exactly `version`, `account`, and `steps`; version is integer `1`. Each action has an exact key set and bounded values:

| Action | Exact required fields | Executor |
| --- | --- | --- |
| `quota` | `action` | Threads API READ |
| `media` | `action`, `media_id` | Threads API READ |
| `replies` | `action`, `thread_id`; optional `after` | Threads API READ |
| `conversation` | `action`, `thread_id`; optional `after` | Threads API READ |
| `feed` | `action`, `limit` | Local browser READ |
| `profile` | `action`, `username` | Local browser READ |
| `thread` | `action`, `thread_ref` | Local browser READ |
| `public_profile` | `action`, `username` | Threads API READ |
| `profile_posts` | `action`, `username`, `limit`; optional `after` | Threads API READ |
| `search` | `action`, `query`, `mode`, `type`, `limit`; optional `after` | Threads API READ |
| `mentions` | `action`, `limit`; optional `after` | Threads API READ |
| `post_text` | `action`, `text` | SPLIT-04 journaled mutation |

All steps execute sequentially once in the caller's foreground process and stop on the first error. At most one `post_text` is accepted and only in the final position; the CLI must be explicitly invoked to execute it. It uses `LocalThreadsMutationRuntime` and the existing operation journal, preserving SPLIT-04's durable ordering, process lock, and ambiguous/no-retry rules. No workflow retry or recovery replay is added. A READ-only API plan does not construct a browser runtime. Browser steps use the existing local browser implementation and account profile lock.

Step output is buffered and written only after the full plan succeeds. Errors report the bounded code and, for step failures, the one-based step index and journal operation UUID when present. Input text, query, file path, and credential value are not echoed by workflow errors.

## Recurrence v1

CLI: `threads-local recurrence create FILE --every-seconds N`, `list`, `show ID`, `run ID`, and `disable ID`.

The persisted record has version `1` and the exact key set:

`version`, `id`, `workflow_file`, `account`, `interval_seconds`, `status`, `created_at`, `updated_at`, `anchor_at`, `next_due_at`, `last_started_at`, `last_finished_at`, `last_outcome`, `last_error_code`, `last_step_index`.

`workflow_file` is the fixed name `workflow.json`; a source path is not persisted. Intervals are bounded from 300 through 2,592,000 seconds. Timestamps are canonical UTC with microsecond precision. Status is `ENABLED` or `DISABLED`; outcomes are `RUNNING`, `SUCCESS`, `FAILED`, `SKIPPED`, or `INTERRUPTED`, with strict consistency checks for timestamps, step indices, and error codes. Unknown, missing, duplicate, or incompatible keys fail closed; version mismatch is reported as unsupported.

Local files are `<data-root>/recurrences/<uuid>/record.json`, `workflow.json`, and `runner.lock`. Creation copies the bounded source bytes into a staging directory, parses the snapshot, requires every step to be an accepted READ-only workflow step, verifies the referenced account, writes the record, then atomically renames the directory into place. Record updates use atomic replacement. Path validation rejects symlinks and escapes. Runner and disable operations share a non-blocking filesystem process lock.

Create and every load/run reject `post_text` and any future or unclassified step. The runner is started only by the operator's `recurrence run` command and stays in the foreground. It waits for due times on the original fixed anchor, persists `RUNNING` before invoking the workflow, advances to the next anchored boundary after each occurrence, and skips missed boundaries. It has no daemon, background service, catch-up, or replay. An interrupted or stale `RUNNING` occurrence is recorded as `INTERRUPTED` and is not retried; a later anchored occurrence may run while the foreground command remains active. Recurrence has no remote mutation path.

There is no local-state migration in this checkpoint. Existing account schema v1 and SPLIT-04 operation records are untouched. Malformed or unsupported recurrence records/snapshots fail closed; operators must not assume automatic conversion from any other state format.

## Synthetic test transfer and verification boundary

The pre-checkpoint destination had 767 passing synthetic tests; no existing test file was replaced or removed. This checkpoint adds the 19 Workflow test functions from source `tests/unit/test_standalone_workflows.py`, the 28 Recurrence test functions from `tests/unit/test_standalone_recurrences.py`, seven Workflow CLI regression functions, and three Recurrence CLI regression functions selected from source `tests/unit/test_standalone_cli.py`. Their destination paths are listed in [PROVENANCE_DECISION.md](PROVENANCE_DECISION.md). Tests use temporary roots, synthetic IDs/text, fake API/HTTP/browser adapters, and fake time only.

No live Threads API request, post, browser login, local operator state, token, profile, journal, or private evidence was used. Workflow and Recurrence have not been live-verified. Hosted CI at the exact Draft PR head remains the acceptance authority.
