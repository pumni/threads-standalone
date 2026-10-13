# Repository instructions — `pumni/threads-standalone`

Python 3.14 project using `uv`. Import namespace: `threads_platform`; CLI: `threads-local`.

These are **always-on safeguards**, not task authorization or a substitute for current product contracts. Read the affected code/tests first. Use [docs/CONTEXT_MAP.md](docs/CONTEXT_MAP.md) to find **only** the relevant design, compatibility, and release documents; verify their status against current code and issues instead of treating historical checkpoint evidence as current permission.

## Task authority and isolation

- Every code/docs task needs a **current owner/coordinator handoff** naming the repository, issue, authorized changes and effects, file ownership, branch strategy, and STOP conditions. An open issue, PR, or these instructions do **not** authorize implementation, live operations, publication, or merge. Implementation authority and Meta/platform/operator permissions are separate.
- Fetch and verify latest `origin/main` before starting; record its actual SHA, Git/worktree status, and relevant open PRs. Use an isolated feature branch/worktree unless the handoff explicitly chooses another approach. Never reset, rebase, stash, clean, force-push, cherry-pick into, or modify someone else's worktree.
- Change only owned files. Shared integrations and overlapping ownership require an **explicit new ownership decision**; STOP and propose an interface/contract rather than editing around a conflict. Recheck conflicts before integration or merge.
- Use one narrow Draft PR per micro-slice, with exact head SHA, changed-file manifest, honest verification evidence, and human/coordinator review. Merge only under an explicit coordinator/owner decision and applicable GitHub protections.

## Privacy, credentials, and external effects

- Never place real tokens, cookies, secrets, account identifiers/names, post/reply IDs, private URLs, customer content, candidate CVs, private roots, or operator evidence into public GitHub issues, PRs, fixtures, logs, screenshots, CI, or commits. Use synthetic data, redacted counts, and safe reason codes. Preserve third-party notices.
- Development, tests, and demos use fake API/HTTP/clock/Playwright adapters and **fresh temporary synthetic roots**. Do not inspect, copy, delete, migrate, or test against existing customer/operator data roots, browser profiles, credential stores, or operation journals. Live requests, real token resolution, writes to operator roots, and customer effects require a **separate explicit operator GO** identifying account, purpose, operation, limits, and rollback.
- Protect compatibility of `THREADS_LOCAL_DATA_ROOT`, account UUID/alias metadata, and `env://` credential references. Never expose raw secrets through commands, executables, or persistent logs.
- Only documented official Meta Threads API endpoints with verified endpoint-specific scope, app access, and authorization may be used. Tester-token success does not prove customer App Review approval or public visibility. Automated browser collection/UI send needs separately verified applicable platform permission and per-operation GO. Private GraphQL/XHR, stealth, CAPTCHA bypass, follow/like bots, engagement farming, fabricated applicants, and bulk unsolicited replies are not authorized.
- Preserve the separation of `OBSERVE_ONLY` (remote read, possibly local receipts), `REVIEW_QUEUE`, and explicitly authorized `APPROVED_DISPATCH`. New external recipients require eligible context and review of the **exact account, target, and final text**; keyword matches alone never authorize a reply. Content/AI providers must not silently dispatch. External AI, blog/video, CRM, and messaging integrations require their own scope.
- `--watch-observe` remains a bounded foreground remote-read-only loop; current recruitment apply requires interactive private-TTY `APPLY`; Recurrence v1 is READ-only. Do not reinterpret any of them as an automatic sender. A future dispatcher requires separately reviewed immutable, unexpired, single-use approvals, limits, and platform permission.

## State and architecture invariants

- Preserve account-scoped locks, strict validation, dedupe/cooldown, provenance, stable fingerprints, `NurtureStore` v1 and `LocalOperationStore` v1 as effect/journal authorities. Do not silently mutate/migrate persisted v1 schemas; any new approval/campaign state needs a reviewed versioned contract. An `AMBIGUOUS` effect blocks automatic retry/resend across restarts.
- Nurture fingerprints are selection/dedupe indexes, **not reversible exact IDs**. Approved reply effects must reacquire and verify full source/account/actor/root/parent/target binding through permitted official API reads, or STOP. Never reconstruct IDs from hashes/permalinks or promote a `SYNTHETIC_FIXTURE` to official API or `MANUAL_URL` provenance.
- Treat `PUBLISHED`, author-side API readback, and independently verified third-party visibility as different evidence levels.
- Keep Playwright imports/Chromium startup in explicit browser paths. API-only CLI, Nurture, and EXE startup must not initiate a browser/download. No Controller/Worker/WorkerJob, PostgreSQL/SQLAlchemy/Alembic, FastAPI/Uvicorn, or runtime `pumni/thread` dependency without separately approved architecture change.
- Public source does not imply a redistribution license or customer-release clearance. Source provenance/licensing remain unresolved; synthetic QA, live UAT, platform permission, and shipping approval are separate gates.

## Verification, reporting, and STOP

- During code changes, use focused synthetic tests first. For an authorized **code PR**, the existing final gates remain: locked `uv sync --locked`, Ruff lint/format, strict Pyright, full synthetic pytest, `uv build`, installed-wheel smoke **outside the checkout** with `PYTHONPATH` unset, and applicable hosted Windows/Ubuntu CI plus full-history Gitleaks at the **exact head**.
- For a **docs-only PR** (Markdown/instructions only; no code, CI/config, dependencies, generated artifacts, or scripts), check content, repo-relative links, and `git diff --check`. Full local test/lint/type/build/wheel steps are **NOT RUN (docs-only)**. Do not investigate/fix unrelated hosted CI failures within this task; report observed failures honestly to the coordinator. Respect GitHub branch protection and correct failures actually caused by the docs change.
- Report PASS/FAIL/NOT RUN and evidence accurately. Synthetic tests, API acknowledgements, and operator claims do not alone establish live approval, third-party visibility, production readiness, a delivered EXE, or end-to-end customer acceptance.
- **STOP** on unknown actor/permissions, changed target/root/parent, stale or missing approval, corrupted state, unresolved effects, Git divergence, overlapping file ownership, credential/access failures, or platform-policy uncertainty. Escalate the specific blocker with the smallest safe proposed remedy.
