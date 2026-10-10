# Bootstrap acceptance

## SPLIT-01 scope

This bootstrap uses Python 3.14, a `threads-standalone` distribution, the retained `threads_platform` import namespace, and the `threads-local` entrypoint. The command only parses `--help`; no network, browser, credentials, local state, PostgreSQL, Worker, or Control Plane code is present.

The package is a new skeleton. It has no runtime dependency on the public `pumni/thread` repository and contains no source or test copied from that repository.

## Local gates

Run from a clean checkout:

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
uv run --isolated --no-project --with dist/*.whl threads-local --help
git diff --check
```

CI also runs a secret scan and the same locked quality/build gates. It requires only the repository-scoped ephemeral `GITHUB_TOKEN`; no Threads, database, or personal access token is configured.

## Repository controls

The destination must be verified PRIVATE before the first file push. Use an independent Git history and keep the default branch protected by requiring the cheap CI check and secret scan after the coordinator accepts this draft. Do not enable publishing, mutation, PostgreSQL, Worker, or source-monorepo dependencies.
