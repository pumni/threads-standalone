# Repository instructions

## Current checkpoint

SPLIT-02 (`pumni/threads-standalone#4`) authorizes local account metadata, process-environment credential references, and official Threads API READ commands only. The destination import namespace remains `threads_platform`; the package is `threads-standalone` and the CLI is `threads-local`.

The frozen source is `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; destination base is `pumni/threads-standalone@66f01bda46b47601cbdab997fd887a5f05e29df6`. Keep the old source read-only and use a fresh feature branch from the stated destination base.

## Scope and data boundaries

- Keep account JSON v1, UUID, alias, `THREADS_LOCAL_DATA_ROOT`, and `env://` credential-reference behavior compatible with the frozen source.
- API commands are read-only: quota, media, post insights, public profile, profile posts, search, mentions, replies, and conversation. Preserve flags, paging, HTTP shapes, output bounds, and sanitized errors.
- Tests and examples use synthetic values and mock HTTP only. Never contact live Threads or read/copy local accounts, tokens, browser data, operation journals, private evidence, or raw live API data.
- Do not add browser, API mutation, Controller, Worker/WorkerJob, PostgreSQL, SQLAlchemy, Alembic, asyncpg, FastAPI, Uvicorn, or a runtime/test/CI dependency on `pumni/thread`.
- No first-party LICENSE, SPDX declaration, or open-source license claim is authorized. Preserve any original notice and keep the open provenance/license decision documented in `docs/PROVENANCE_DECISION.md`.
- Implement one authorized checkpoint at a time. SPLIT-03 and later remain unauthorized until coordinator review.

## Validation

Use the locked uv environment. Run `uv sync --locked`, Ruff lint/format, Pyright, pytest, `uv build`, isolated installed-wheel CLI and mocked-read smoke tests, the local secret scan, and `git diff --check`. Hosted CI is required for checkpoint acceptance.
