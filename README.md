# threads-standalone

Public destination bootstrap for the Standalone product split. This repository was initially created and verified PRIVATE before its first push; the operator changed its visibility to PUBLIC on 2026-10-10 to make GitHub-hosted CI available. The code remains an independently authored package skeleton with a help-only `threads-local` command.

**PUBLIC REPOSITORY WARNING:** Never commit or publish access tokens, browser cookies/profiles, account IDs, Nurture receipts, operation journals, raw Threads IDs/cursors, usernames, draft/post text, permalinks, live HTTP payloads or private evidence. Keep all operator data outside Git and use synthetic examples in tests.

No Threads API, browser, account, token, local state, database, Worker, or Control Plane capability is implemented in this scaffold.

## Development

Requires Python 3.14 and uv.

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
uv run --locked threads-local --help
```

The published wheel smoke is run in a fresh isolated environment by CI.

## Provenance

No source or test files have been copied from the public `pumni/thread` repository. First-party provenance and license/notice treatment remain unresolved; see [PROVENANCE_DECISION.md](docs/PROVENANCE_DECISION.md).
