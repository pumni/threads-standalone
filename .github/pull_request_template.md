## Scope

Which authorized checkpoint does this PR implement?

## Verification

- [ ] `uv sync --locked`
- [ ] Ruff lint and format
- [ ] Pyright
- [ ] Synthetic pytest
- [ ] `uv build` and isolated wheel `threads-local --help` smoke
- [ ] Secret scan
- [ ] `git diff --check`

## Safety

- [ ] No unauthorized source/test copying from `pumni/thread`
- [ ] No token, account state, browser profile, operation journal, or private evidence
- [ ] No live Threads call, database, Worker, or Control Plane dependency
- [ ] Changed files stay within the authorized issue scope
