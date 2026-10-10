# SPLIT-05 acceptance

## Checkpoint scope

This destination implementation ports the accepted Standalone Nurture v1 flow on top of account, API READ, browser, and SPLIT-04 journaled mutation capabilities. The source is `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; destination base is `pumni/threads-standalone@d28bb979d720ffc6b493e76e9709d80bc0709d5c`. The package is `threads-standalone`, the import namespace remains `threads_platform`, and the executable is `threads-local`.

Account documents retain schema v1, UUIDs, aliases, and optional `env://` credential references. The secret value is read from the explicitly named process environment variable for each API invocation and is not persisted or cached. `THREADS_LOCAL_DATA_ROOT` and its path protections remain in force.

API reads are `quota`, `media`, `post-insights`, `public-profile`, `profile-posts`, `search`, `mentions`, `replies`, and `conversation`. Paging cursors, CLI flags, request fields, normalized error codes, and bounded/sanitized CLI output are covered by synthetic tests in `docs/API_READ_PARITY.md`.

Browser commands are `account login ALIAS`, `profile ALIAS USERNAME`, `thread ALIAS THREAD_REF`, and `feed ALIAS [--limit N]`. Login is headed and operator-assisted. Profiles are stored locally at `profiles/<account-uuid>` and protected by `locks/<account-uuid>.lock`; the account v1 JSON schema is unchanged. Browser modules load only for these explicit commands, and browser reads use fake Playwright tests without launching a browser or contacting Threads. See [BROWSER_PARITY.md](BROWSER_PARITY.md).

Mutation commands are `post`, `reply`, `post-image`, `post-video`, `post-carousel`, `moderate-reply`, and local `operation show`. Quote publishing remains runtime-only. Journal phase ordering, quota checks, bounded media polling, deterministic rejection reconciliation, and ambiguous/no-retry rules are covered in [MUTATION_PARITY.md](MUTATION_PARITY.md). Operation journal v1 records stay under `<data-root>/operations`; account process locks remain under `<data-root>/locks`.

Nurture commands are `nurture run ALIAS --preset recruitment` and `nurture insights refresh ALIAS --preset recruitment`. Optional reply, own-content, or quote drafts can enter the single mutation boundary only through explicit `--apply`. Default observe uses API reads and constructs neither a browser runtime nor a mutation runtime. Preserve local Nurture JSON v1, exact fingerprints, receipts, target/content state, path checks and its independent account process lock. Apply durably reserves its target/content before the existing SPLIT-04 journaled operation; `AMBIGUOUS` and published targets are never automatically retried. Insights refresh reads only journal-verified `PUBLISHED` content. See [NURTURE_PARITY.md](NURTURE_PARITY.md).

## Hard exclusions

No Controller, Worker/WorkerJob, scheduler, PostgreSQL/SQLAlchemy/Alembic/asyncpg, FastAPI/Uvicorn, old-repository runtime dependency, real token, local account root, live browser session, live API request, or production-read claim is included. Mutation and Nurture behavior is tested synthetically and has not been live-verified. SPLIT-06 and later checkpoints require separate authorization.

## Local gates

Run from a clean checkout with Python 3.14 and uv:

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl threads-local --help
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl --with pytest --with pytest-asyncio pytest tests/test_api_contract.py::test_wheel_smoke_quota_read tests/test_mutation_runtime.py::test_success_has_durable_phase_order_exact_text_request_and_one_publish
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl --with pytest pytest tests/test_nurture_cli.py::test_wheel_smoke_nurture_observe_apply_and_insights
git diff --check
```

GitHub Actions additionally runs a full-history secret scan and the locked quality/build/wheel smoke gates. API, mutation and Nurture smoke responses are synthetic; browser tests use fake Playwright objects. Tests use no Threads credential or live Threads network access.

## Provenance and data controls

The file-level audit and remaining license decision are in [PROVENANCE_DECISION.md](PROVENANCE_DECISION.md). The destination is public. Never commit token values, cookies, browser profiles, real account state, remote identifiers/cursors, usernames, URLs or content from live Threads responses, operation journals, private evidence, or personal email addresses.
