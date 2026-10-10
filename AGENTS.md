# Repository instructions

## Current checkpoint

SPLIT-04 (`pumni/threads-standalone#8`) authorizes standalone journaled API mutations on top of the accepted account, API READ, and browser capabilities. The destination import namespace remains `threads_platform`; the package is `threads-standalone` and the CLI is `threads-local`.

The frozen source is `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; destination base is `pumni/threads-standalone@fbd7c8591460e477cf15ac2b86d864c3c5189751`. Keep the old source read-only and work only on the authorized branch from that destination base.

## Scope and data boundaries

- Keep account JSON v1, UUID, alias, `THREADS_LOCAL_DATA_ROOT`, and `env://` credential-reference behavior compatible with the frozen source.
- API commands are read-only: quota, media, post insights, public profile, profile posts, search, mentions, replies, and conversation. Preserve flags, paging, HTTP shapes, output bounds, and sanitized errors.
- Mutation commands are text post, reply (including optional parent reply), image, video, carousel, moderation, and local `operation show`. Quote publishing remains runtime-only. Preserve exact operation journal v1 schema, phase transitions, process locks, quota checks, bounded media polling, durable publish/mutation boundaries, error classification, and ambiguous/no-retry behavior.
- Browser commands are `account login ALIAS`, `feed ALIAS [--limit N]`, `profile ALIAS USERNAME`, and `thread ALIAS THREAD_REF`. They use headed Chromium and the local account's persistent `profiles/<uuid>` directory with `locks/<uuid>.lock`; recognition, origins, errors, and bounds remain fail-closed.
- Account metadata/credential commands and all API commands must not import or instantiate Playwright. Browser imports stay in the explicitly selected browser path. Chromium installation is an explicit operator step; application startup never downloads it.
- Tests and examples use synthetic values, fake Playwright objects, temporary journal state, and mock HTTP only. Never contact live Threads or read/copy local accounts, tokens, browser data, operation journals, private evidence, or raw live API data. Do not perform any actual remote post, reply, or moderation action.
- Do not add Controller, Worker/WorkerJob, PostgreSQL, SQLAlchemy, Alembic, asyncpg, FastAPI, Uvicorn, or a runtime/test/CI dependency on `pumni/thread`.
- No first-party LICENSE, SPDX declaration, or open-source license claim is authorized. Preserve any original notice and keep the open provenance/license decision documented in `docs/PROVENANCE_DECISION.md`.
- Implement one authorized checkpoint at a time. SPLIT-05 and later remain unauthorized until coordinator review.

## Validation

Use the locked uv environment. Run `uv sync --locked`, Ruff lint/format, Pyright, pytest, `uv build`, isolated installed-wheel CLI and mocked-read smoke tests, the local secret scan, and `git diff --check`. Hosted CI is required for checkpoint acceptance.
