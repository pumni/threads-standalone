# threads-standalone

Standalone local account management, read-only Threads API commands, and bounded local browser reads. The Python import namespace is `threads_platform`; the installed command is `threads-local`.

This public repository must contain only source and synthetic examples. Never commit tokens, cookies, browser profiles, local account files, operation journals, remote IDs/cursors, usernames, permalinks, post text, raw HTTP data, or private evidence.

## Current capabilities

The current checkpoint provides local account metadata and the read-only `threads-local api` commands `quota`, `media`, `post-insights`, `public-profile`, `profile-posts`, `search`, `mentions`, `replies`, and `conversation`. It stores only `env://` credential references; the secret itself must be supplied in the process environment when a command runs.

Example command shapes (all identifiers below are synthetic placeholders):

```powershell
threads-local account add demo
threads-local account list
threads-local account credential set-env demo THREADS_PLATFORM_THREADS_TOKEN_DEMO
threads-local api quota demo
threads-local api media demo SYNTHETIC_MEDIA_ID
threads-local api search demo "synthetic query" --mode keyword --type recent --limit 25
threads-local api mentions demo --after SYNTHETIC_CURSOR --limit 25
```

Set `THREADS_LOCAL_DATA_ROOT` to the intended local data directory before using account commands. Set the named `THREADS_PLATFORM_THREADS_TOKEN_*` variable in the process environment before API commands. Do not place its value in command arguments, shell history, files tracked by Git, test fixtures, or logs.

These API commands call the official Threads API when run by an operator. Automated tests use only fake HTTP responses; the destination API behavior has not been live-verified.

SPLIT-03 also restores the local, explicitly invoked browser commands:

```powershell
threads-local account login demo
threads-local profile demo synthetic_user
threads-local thread demo /@synthetic_user/post/SYNTHETIC_THREAD_REF
threads-local feed demo --limit 10
```

`account login` opens headed Chromium for a human-operated login and waits for Enter before closing it. The persistent account-specific profile lives under `<data-root>/profiles/<account-uuid>`; its process lock is `<data-root>/locks/<account-uuid>.lock`. Browser cookies and profile state stay on the operator's machine. Feed, profile, and thread commands perform bounded reads only. Install the matching Chromium binary explicitly with `uv run playwright install chromium`; the CLI does not download or launch a browser during account metadata/credential commands or API-only commands.

Browser tests use fake Playwright objects and synthetic inputs. They do not launch Chromium, log in to Threads, or make live Threads requests. See [BROWSER_PARITY.md](docs/BROWSER_PARITY.md) for boundaries and test coverage. Browser behavior in the destination has not been live-verified.

API mutation, Controller/Worker, and PostgreSQL remain outside this checkpoint.

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
uv run --isolated --no-project --with dist/threads_standalone-0.1.0-py3-none-any.whl --with pytest pytest tests/test_api_contract.py::test_wheel_smoke_quota_read
```

CI also scans for secrets and runs the locked gates, package build, installed-wheel CLI check, and mock-only API read smoke. It does not need a Threads credential, database, or old-repository checkout.

## Provenance and license status

The source transfer and file classifications are recorded in [PROVENANCE_DECISION.md](docs/PROVENANCE_DECISION.md). The file-level transfer audit for this checkpoint passed for the files listed there. The destination's public distribution license remains undecided; no first-party LICENSE or SPDX marker has been added. Third-party dependencies remain under their respective upstream licenses.
