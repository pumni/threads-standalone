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

The destination was verified PRIVATE before its first file push; on 2026-10-10 the operator explicitly changed GitHub visibility to PUBLIC to enable hosted CI. The current PUBLIC status is intentional, but this does not authorize uploading any local account state, profile, token, journal, operator evidence, remote IDs, raw cursor, username, URLs or post/draft content. Maintain an independent Git history and protect the default branch with required CI and secret scan after coordinator acceptance. Do not enable publishing, mutation, PostgreSQL, Worker, or source-monorepo dependencies.
