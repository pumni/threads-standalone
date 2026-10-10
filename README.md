# threads-standalone

Private destination bootstrap for the Standalone product split. This repository starts with an independently authored package skeleton and a help-only `threads-local` command.

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
