# SPLIT-03 acceptance

## Checkpoint scope

This destination implementation ports local account metadata, environment-backed credential references, the frozen-source Threads API READ CLI surface, and bounded local browser login/feed/profile/thread commands. The package is `threads-standalone`, the import namespace remains `threads_platform`, and the executable is `threads-local`.

Account documents retain schema v1, UUIDs, aliases, and optional `env://` credential references. The secret value is read from the explicitly named process environment variable for each API invocation and is not persisted or cached. `THREADS_LOCAL_DATA_ROOT` and its path protections remain in force.

API reads are `quota`, `media`, `post-insights`, `public-profile`, `profile-posts`, `search`, `mentions`, `replies`, and `conversation`. Paging cursors, CLI flags, request fields, normalized error codes, and bounded/sanitized CLI output are covered by synthetic tests in `docs/API_READ_PARITY.md`.

Browser commands are `account login ALIAS`, `profile ALIAS USERNAME`, `thread ALIAS THREAD_REF`, and `feed ALIAS [--limit N]`. Login is headed and operator-assisted. Profiles are stored locally at `profiles/<account-uuid>` and protected by `locks/<account-uuid>.lock`; the account v1 JSON schema is unchanged. Browser modules load only for these explicit commands, and browser reads use fake Playwright tests without launching a browser or contacting Threads. See [BROWSER_PARITY.md](BROWSER_PARITY.md).

## Hard exclusions

No API or browser mutation, Controller, Worker/WorkerJob, PostgreSQL/SQLAlchemy/Alembic/asyncpg, FastAPI/Uvicorn, old-repository runtime dependency, real token, local account root, live browser session, live API request, or production-read claim is included. Later standalone capability checkpoints require separate authorization.

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
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl --with pytest pytest tests/test_api_contract.py::test_wheel_smoke_quota_read
git diff --check
```

GitHub Actions additionally runs a full-history secret scan and the locked quality/build/wheel smoke gates. API smoke responses are fake HTTP responses and browser tests use a fake Playwright API; no Threads credential or live Threads network access is used by tests.

## Provenance and data controls

The file-level audit and remaining license decision are in [PROVENANCE_DECISION.md](PROVENANCE_DECISION.md). The destination is public. Never commit token values, cookies, browser profiles, real account state, remote identifiers/cursors, usernames, URLs or content from live Threads responses, operation journals, private evidence, or personal email addresses.
