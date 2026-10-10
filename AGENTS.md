# Repository instructions

## Current checkpoint

SPLIT-07 (`pumni/threads-standalone#14`) authorizes cross-platform packaging, dependency/security checks, and synthetic end-to-end QA only. Preserve all account, API READ, browser READ, journaled mutation, Nurture, WorkflowPlan, and READ-only Recurrence behavior. No live Threads call, production release, real state migration, or licensing decision is part of this checkpoint. The import namespace remains `threads_platform`; the package is `threads-standalone`; the CLI is `threads-local`.

The frozen source is `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; the authorized destination base is `pumni/threads-standalone@693d4eaaa41ef7988bd9e78a085df245d681eb20`. Keep the old source read-only and stop on any unexpected destination baseline drift. SPLIT-08 and later are not authorized by this checkpoint.

## Scope and data boundaries

- Keep account JSON v1, UUID, alias, `THREADS_LOCAL_DATA_ROOT`, and `env://` credential-reference behavior compatible with the frozen source.
- API commands are read-only: quota, media, post insights, public profile, profile posts, search, mentions, replies, and conversation. Preserve flags, paging, HTTP shapes, output bounds, and sanitized errors.
- Mutation commands are text post, reply (including optional parent reply), image, video, carousel, moderation, and local `operation show`. Quote publishing remains runtime-only. Preserve exact operation journal v1 schema, phase transitions, process locks, quota checks, bounded media polling, durable publish/mutation boundaries, error classification, and ambiguous/no-retry behavior.
- Browser commands are `account login ALIAS`, `feed ALIAS [--limit N]`, `profile ALIAS USERNAME`, and `thread ALIAS THREAD_REF`. They use headed Chromium and the local account's persistent `profiles/<uuid>` directory with `locks/<uuid>.lock`; recognition, origins, errors, and bounds remain fail-closed.
- Nurture CLI remains `nurture run ALIAS --preset recruitment` with optional `--apply`, `--reply-file`, `--post-file`, or `--quote-file`, and `nurture insights refresh ALIAS --preset recruitment`. Preserve preset, fingerprints, exact state/receipt keysets, ordering, caps, outcomes, locks, and safe error handling. Default observe and insights are API-only and construct no browser or mutation runtime. A remote action requires explicit apply plus a valid bound draft/candidate, durably reserves before the single SPLIT-04 journaled mutation, and never retries `AMBIGUOUS`/published targets. No scheduler, background work, autonomous agent, or second mutation per run.
- Workflow CLI is `workflow run FILE`. Preserve strict workflow JSON v1, 1–32 ordered steps, the 65,536-byte bound, one optional final `post_text`, bounded output, stop-on-first-error behavior, and local account locking through the existing runtimes. API-only workflows do not instantiate a browser runtime. A final post is available only through the existing SPLIT-04 mutation runtime/journal.
- Recurrence CLI is `recurrence create FILE --every-seconds N`, `list`, `show ID`, `run ID`, and `disable ID`. Preserve strict recurrence record v1 and the snapshot, atomic persistence, path checks, `runner.lock`, outcomes, and fixed anchor. Create and every run revalidate a READ-only workflow; future or unclassified steps fail closed. `run` waits and executes in the foreground; it skips missed boundaries and never catches up or replays an interrupted occurrence. It cannot perform remote mutation.
- Account metadata/credential commands and all API/Nurture commands must not import or instantiate Playwright. Browser imports stay in the explicitly selected browser path. Chromium installation is an explicit operator step; application startup never downloads it.
- Tests and examples use synthetic UUIDs/IDs/text, fake API/HTTP and Playwright objects, and temporary local roots only. Never contact live Threads, publish/reply/moderate, or read/copy operator accounts, token values, browser data, local operation journals, private evidence, or raw live API data.
- Do not add Controller, Worker/WorkerJob, PostgreSQL, SQLAlchemy, Alembic, asyncpg, FastAPI, Uvicorn, or a runtime/test/CI dependency on `pumni/thread`.
- No first-party LICENSE, SPDX declaration, or open-source license claim is authorized. Preserve any original notice and keep the open provenance/license decision documented in `docs/PROVENANCE_DECISION.md`.
- Implement one authorized checkpoint at a time. SPLIT-08 and later remain unauthorized until separate coordinator review.

## Validation

Use the locked uv environment on Windows and Linux. Run `uv sync --locked`, Ruff lint/format, Pyright, the complete synthetic pytest suite, `uv build`, and a fresh wheel environment outside the checkout using locked dependencies with no `PYTHONPATH`. Audit runtime dependencies, wheel contents, architecture/privacy boundaries, sanitized errors, path/state handling, account locks, and ambiguous/no-retry behavior. Hosted Windows and Ubuntu CI plus the full-history secret scan at the exact Draft PR head are required for checkpoint acceptance.
