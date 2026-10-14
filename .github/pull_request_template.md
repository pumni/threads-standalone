## Scope

- Issue / authorized handoff:
- Functional outcome:
- Base SHA:
- Head SHA:
- Changed-file manifest:

## Verification

- Selected CI lane (`DOCS_ONLY` / `API_ONLY` / `BROWSER_ONLY` / `SHARED` / `FULL`):
- [ ] Stable `required CI gate` passes at the exact head
- [ ] Full-history Gitleaks passes
- [ ] Changed Markdown links and `git diff --check` pass (when Markdown changed)
- [ ] Targeted installed-wheel smoke passes outside the checkout with `PYTHONPATH` unset and zero skips (code PRs)
- [ ] Full Ubuntu/Windows synthetic regression and packaging smoke reviewed (full regression/release only)
- [ ] Hosted run URLs and test counts/durations recorded

## Safety

- [ ] No unauthorized source/test copying from `pumni/thread`
- [ ] No token, account state, browser profile, operation journal, or private evidence
- [ ] No live Threads call, database, Worker, or Control Plane dependency
- [ ] Changed files stay within the authorized issue scope
- [ ] No merge; owner review remains required
