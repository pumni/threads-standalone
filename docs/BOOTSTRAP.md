# SPLIT-06 acceptance

## Checkpoint scope

This checkpoint ports Standalone WorkflowPlan and Recurrence from frozen source `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1` onto destination baseline `pumni/threads-standalone@eed547afbaa76fbf25d1f05a17b37c70eaa1ef73`. The Python namespace remains `threads_platform`; the command remains `threads-local`. The full mapping and limits are in [WORKFLOW_RECURRENCE_PARITY.md](WORKFLOW_RECURRENCE_PARITY.md).

`threads-local workflow run FILE` loads and validates the whole v1 plan before account lookup, then executes its steps sequentially in the foreground. It accepts 1–32 steps, at most 65,536 bytes, and one `post_text` mutation only as the last step. A post runs only after explicit operator invocation and goes through the existing SPLIT-04 mutation runtime and operation journal. API-only plans do not instantiate a browser runtime. Output is buffered until completion; a failure stops the workflow and no step is retried by the runner.

Recurrence commands are `create FILE --every-seconds N`, `list`, `show ID`, `run ID`, and `disable ID`. A create validates the workflow as READ-only and atomically snapshots its bytes. Each load/run validates the v1 snapshot again and rejects mutation or unclassified future steps. Record v1 has exactly the documented metadata keys; snapshot, `record.json`, and `runner.lock` are stored under `<data-root>/recurrences/<id>/`. `run` is an operator-started foreground loop using a fixed anchor: missed boundaries are skipped, interrupted occurrences are marked and not replayed, and no daemon or remote mutation is created. Existing account and SPLIT-04 operation-journal schemas are unchanged. No automatic migration or conversion of local recurrence data is performed.

All new tests are synthetic and use temporary local roots, fake API/HTTP calls, fixed UUIDs, and fake recurrence timing. No live Threads call, account state, token, browser profile, or operator journal is used. Workflow browser reads retain the existing local browser lock and only initialize that runtime for a workflow containing a browser step.

## Local gates

From a clean Python 3.14 checkout, run:

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl threads-local --help
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl --with pytest --with pytest-asyncio pytest tests/test_workflow_cli.py::test_cli_workflow_mixed_reads_buffer_exact_output_and_uses_one_http_context tests/test_workflow_cli.py::test_cli_workflow_post_success_is_exact_and_never_echoes_text tests/test_recurrence_cli.py::test_cli_recurrence_create_list_show_keep_workflow_metadata_private
git diff --check
```

GitHub Actions additionally runs full-history secret scanning, architecture/privacy checks, and the same locked build/wheel checks at the Draft PR head. Smoke behavior is synthetic; it does not require a Threads credential or old-repository checkout.

## Hard exclusions

No Controller, Worker/WorkerJob, scheduler, PostgreSQL/SQLAlchemy/Alembic/asyncpg, FastAPI/Uvicorn, old-repository runtime dependency, live API request, browser login, or production-read claim is included. No first-party LICENSE, SPDX declaration, or public reuse claim is added; issue #2 remains open. SPLIT-07 needs separate authorization.

## Prior checkpoint reference

## SPLIT-05 acceptance (previous checkpoint)

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
