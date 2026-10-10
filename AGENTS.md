# Repository instructions

## Current checkpoint

SPLIT-05 (`pumni/threads-standalone#10`) authorizes the synthetic-only port of existing Standalone Nurture v1: bounded API-read observe, local v1 state and receipts, operator-authored reply/content/quote drafts, explicit apply through the SPLIT-04 journal, and read-only insights. The import namespace remains `threads_platform`; the package is `threads-standalone`; the CLI is `threads-local`.

The frozen source is `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; the authorized destination base is `pumni/threads-standalone@d28bb979d720ffc6b493e76e9709d80bc0709d5c`. Keep the old source read-only and stop on any unexpected destination baseline drift. SPLIT-06 is not authorized by this checkpoint.

## Scope and data boundaries

- Keep account JSON v1, UUID, alias, `THREADS_LOCAL_DATA_ROOT`, and `env://` credential-reference behavior compatible with the frozen source.
- API commands are read-only: quota, media, post insights, public profile, profile posts, search, mentions, replies, and conversation. Preserve flags, paging, HTTP shapes, output bounds, and sanitized errors.
- Mutation commands are text post, reply (including optional parent reply), image, video, carousel, moderation, and local `operation show`. Quote publishing remains runtime-only. Preserve exact operation journal v1 schema, phase transitions, process locks, quota checks, bounded media polling, durable publish/mutation boundaries, error classification, and ambiguous/no-retry behavior.
- Browser commands are `account login ALIAS`, `feed ALIAS [--limit N]`, `profile ALIAS USERNAME`, and `thread ALIAS THREAD_REF`. They use headed Chromium and the local account's persistent `profiles/<uuid>` directory with `locks/<uuid>.lock`; recognition, origins, errors, and bounds remain fail-closed.
- Nurture CLI remains `nurture run ALIAS --preset recruitment` with optional `--apply`, `--reply-file`, `--post-file`, or `--quote-file`, and `nurture insights refresh ALIAS --preset recruitment`. Preserve preset, fingerprints, exact state/receipt keysets, ordering, caps, outcomes, locks, and safe error handling. Default observe and insights are API-only and construct no browser or mutation runtime. A remote action requires explicit apply plus a valid bound draft/candidate, durably reserves before the single SPLIT-04 journaled mutation, and never retries `AMBIGUOUS`/published targets. No scheduler, background work, autonomous agent, or second mutation per run.
- Account metadata/credential commands and all API/Nurture commands must not import or instantiate Playwright. Browser imports stay in the explicitly selected browser path. Chromium installation is an explicit operator step; application startup never downloads it.
- Tests and examples use synthetic UUIDs/IDs/text, fake API/HTTP and Playwright objects, and temporary local roots only. Never contact live Threads, publish/reply/moderate, or read/copy operator accounts, token values, browser data, local operation journals, private evidence, or raw live API data.
- Do not add Controller, Worker/WorkerJob, PostgreSQL, SQLAlchemy, Alembic, asyncpg, FastAPI, Uvicorn, or a runtime/test/CI dependency on `pumni/thread`.
- No first-party LICENSE, SPDX declaration, or open-source license claim is authorized. Preserve any original notice and keep the open provenance/license decision documented in `docs/PROVENANCE_DECISION.md`.
- Implement one authorized checkpoint at a time. SPLIT-06 and later remain unauthorized until separate coordinator review.

## Validation

Use the locked uv environment. Run `uv sync --locked`, Ruff lint/format, Pyright, pytest, `uv build`, isolated installed-wheel CLI and fake Nurture observe/apply/insights smoke, architecture/privacy checks, the full-history secret scan, and `git diff --check`. Hosted CI at the exact Draft PR head is required for checkpoint acceptance.
