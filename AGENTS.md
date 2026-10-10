# AGENTS.md

## Current authorization

This repository is the original SPLIT-01 bootstrap skeleton for issue #244. Keep changes within the active authorized checkpoint. The Python import namespace is `threads_platform`; the distribution is `threads-standalone`; the command is `threads-local`.

## Hard boundaries

- This scaffold is newly authored. Do not copy source or tests from `pumni/thread` until a later checkpoint explicitly authorizes it and provenance is resolved.
- The current CLI is help-only. Do not add API, browser, credential, account, local-state, operation-journal, database, Worker, or Control Plane behavior in this checkpoint.
- Do not add a runtime or CI dependency on `pumni/thread`, PostgreSQL, SQLAlchemy, Alembic, asyncpg, FastAPI, Uvicorn, Worker packages, or private Git dependencies.
- Keep tests synthetic and network-free. Never commit tokens, browser data, account state, operation journals, or private evidence.
- Do not add a first-party LICENSE until the owner confirms reuse rights and license/notice treatment.

## Validation

Use the locked uv environment. Run the ruff check and format check, Pyright, pytest, `uv build`, isolated wheel `threads-local --help` smoke, and `git diff --check` for changes.
