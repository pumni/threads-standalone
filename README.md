# threads-standalone

Standalone local account management, Threads API reads and journaled mutations, and bounded local browser reads. The Python import namespace is `threads_platform`; the installed command is `threads-local`.

This public repository must contain only source and synthetic examples. Never commit tokens, cookies, browser profiles, local account files, operation journals, remote IDs/cursors, usernames, permalinks, post text, raw HTTP data, or private evidence.

## Current capabilities

The current checkpoint adds the existing Nurture v1 flow to local account metadata, read-only `threads-local api` commands, journaled mutations and bounded browser reads. Account files keep only `env://` credential references; an operator supplies the secret in the process environment when a command runs.

Example command shapes (all identifiers below are synthetic placeholders):

```powershell
threads-local account add demo
threads-local account list
threads-local account credential set-env demo THREADS_PLATFORM_THREADS_TOKEN_DEMO
threads-local api quota demo
threads-local api media demo SYNTHETIC_MEDIA_ID
threads-local api search demo "synthetic query" --mode keyword --type recent --limit 25
threads-local api mentions demo --after SYNTHETIC_CURSOR --limit 25
threads-local post demo "synthetic text"
threads-local reply demo SYNTHETIC_THREAD_ID "synthetic reply" --parent-reply-id SYNTHETIC_PARENT_REPLY_ID
threads-local post-image demo https://example.invalid/image.jpg "synthetic caption" --alt-text "synthetic alt text"
threads-local post-video demo https://example.invalid/video.mp4
threads-local post-carousel demo carousel-manifest.json
threads-local moderate-reply demo SYNTHETIC_REPLY_ID hide
threads-local operation show SYNTHETIC_OPERATION_UUID
threads-local nurture run demo --preset recruitment
threads-local nurture run demo --preset recruitment --apply --reply-file synthetic-reply.json
threads-local nurture run demo --preset recruitment --apply --post-file synthetic-content.json
threads-local nurture run demo --preset recruitment --apply --quote-file synthetic-quote.json
threads-local nurture insights refresh demo --preset recruitment
```

Post, reply, media, carousel, and moderation commands can cause remote side effects when an operator runs them with real credentials. Each mutation uses the local v1 operation journal and account process lock; an ambiguous result is recorded for review and is never retried automatically. Quote publishing is available through the runtime contract and is not exposed as a direct CLI command. See [MUTATION_PARITY.md](docs/MUTATION_PARITY.md) for the journal and failure behavior.

Nurture observe is read-only and API-only: it constructs neither the browser runtime nor a mutation runtime. It records bounded receipts and fingerprints under the local data root. A Nurture reply, text post or quote requires `--apply` and a valid operator-authored draft/candidate bound to the selected target or preset. The runner durably reserves state before at most one SPLIT-04 journaled mutation and does not retry an ambiguous or published target. With no actionable draft, the command may return a recommendation, but performs no mutation. Insights refresh is explicit, read-only, and limited to content verified through a `PUBLISHED` operation journal entry. See [NURTURE_PARITY.md](docs/NURTURE_PARITY.md) for schemas, fingerprints, test mapping and the historical evidence boundary.

Set `THREADS_LOCAL_DATA_ROOT` to the intended local data directory before using account commands. Set the named `THREADS_PLATFORM_THREADS_TOKEN_*` variable in the process environment before API commands. Do not place its value in command arguments, shell history, files tracked by Git, test fixtures, or logs.

API reads and mutation commands call the official Threads API when run by an operator. All Nurture, API, and mutation tests use fake API/HTTP responses and temporary synthetic state; no live Threads request or mutation is made by tests. Nurture behavior in the destination has not been live-verified.

SPLIT-03 also restores the local, explicitly invoked browser commands:

```powershell
threads-local account login demo
threads-local profile demo synthetic_user
threads-local thread demo /@synthetic_user/post/SYNTHETIC_THREAD_REF
threads-local feed demo --limit 10
```

`account login` opens headed Chromium for a human-operated login and waits for Enter before closing it. The persistent account-specific profile lives under `<data-root>/profiles/<account-uuid>`; its process lock is `<data-root>/locks/<account-uuid>.lock`. Browser cookies and profile state stay on the operator's machine. Feed, profile, and thread commands perform bounded reads only. Install the matching Chromium binary explicitly with `uv run playwright install chromium`; the CLI does not download or launch a browser during account metadata/credential commands or API-only commands.

Browser tests use fake Playwright objects and synthetic inputs. They do not launch Chromium, log in to Threads, or make live Threads requests. See [BROWSER_PARITY.md](docs/BROWSER_PARITY.md) for boundaries and test coverage. Browser behavior in the destination has not been live-verified.

Controller, Worker, and PostgreSQL remain outside this checkpoint.

## Development

Requires Python 3.14 and uv.

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
```

CI also scans full Git history for secrets and runs the locked gates, package build, installed-wheel CLI check, and fake API/read/mutation/Nurture smoke. It does not need a Threads credential, database, or old-repository checkout.

## Provenance and license status

The source transfer and file classifications are recorded in [PROVENANCE_DECISION.md](docs/PROVENANCE_DECISION.md). The file-level transfer audit for this checkpoint passed for the files listed there. The destination's public distribution license remains undecided; no first-party LICENSE or SPDX marker has been added. Third-party dependencies remain under their respective upstream licenses.
