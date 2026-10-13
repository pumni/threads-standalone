# Browser Jobs release readiness

**Historical runtime checkpoint, not a Browser coding policy.** Under the current [owner product decision](PRODUCT_DIRECTION.md) and [AGENTS.md](../AGENTS.md), API and Browser are independent. Requiring written Meta authority or renewed Terms adjudication is not a development prerequisite. Keep the installed live Browser source disabled until a separate implemented/operator-scoped UAT change is accepted; do not claim real Threads execution from synthetic evidence.

## Decision snapshot

| Capability | State | Evidence required to change state |
| --- | --- | --- |
| `SYNTHETIC_READY` | **CANDIDATE — pending coordinator review** | The BJ-07 Draft PR's exact head must pass the synthetic tests, lint, type check, build, installed-wheel smoke, hosted Windows and Ubuntu checks, and full-history Gitleaks. Coordinator acceptance is still required. |
| `MANUAL_INPUT_READY` | **NOT READY** | The installed CLI's `manual` source deliberately returns `UNKNOWN / NO_TEXT`. It has no authorized manual text or provenance import and has not passed private customer-root deployment acceptance. |
| `AUTHORIZED_BROWSER_READ_READY` | **NO_GO — DISABLED** | Installed live source is still disabled. A future scoped code change can enable the production reader and CLI with operator-selected account/session controls and bounded private UAT. #64/#103 are historical policy discussions, not prerequisites for Browser development; synthetic evidence does not establish live functionality. |
| `BROWSER_ENGAGEMENT_READY` | **NO_GO** | Requires separate rights, effect review and one-shot approvals under #67. No like, reply, follow, or other account effect is covered here. |

This document records a synthetic release candidate. It does not approve customer delivery, live collection, real account use, or production operation. Source provenance and redistribution/license decision #2, deployment gates #18 and #46, and the separate API/AI/CRM/media gate #25 remain unresolved. No license or EXE/self-contained distribution claim is made.

## Scope and evidence model

BJ-07 starts from `origin/main` at `30508e41c4beaa4d68cfd4afbccf3d2fcae14537`. Its complete ownership is the new acceptance test `tests/test_browser_jobs_e2e.py` and this document. Existing source code, entry points, schemas, QA scripts, and earlier Browser Jobs tests remain unchanged.

Tests use newly created temporary directories, fake aliases, reserved `https://threads.example.invalid` URLs, Vietnamese synthetic text, and unmistakable contact placeholders. They do not use real customer posts, accounts, credentials, contact details, browser profiles, or private data roots. Test output and CI artifacts must not contain fixture bodies, aliases, paths, URLs, or contact placeholders.

The installed CLI's `fixture` source accepts an ordered list of selected fake targets and runs an in-memory fake batch. It does not scroll a feed or inspect a DOM. The BJ-02 observation tests may exercise virtualized-feed behavior through fake in-process interfaces; those results are **FAKE adapter evidence**, not installed-CLI behavior or live Threads evidence. Unsupported scenarios stay marked unsupported instead of being described as end-to-end passes.

## Evidence matrix

| Area | Evidence level | Acceptance evidence and limit |
| --- | --- | --- |
| Entrypoint and one-cycle flow | Installed wheel | From outside the checkout with `PYTHONPATH` unset, run `threads-local --help`, nested `recruitment browser-watch --help`, then one `--source fixture` cycle against a newly created private root and fixture. Verify `job_vacancies_v1`, `browser_job_watch_v1`, reserved URL handling, counts, provenance, and output redaction. Record the exact wheel, PR head, and command result in the Draft PR. |
| Parser and archive | Source-level synthetic E2E | A fixture passes through the CLI entry point in tests, BJ-01 extraction, BJ-03 archive, and BJ-04A checkpoint. Assertions cover recruiter vs. job-seeker/unrelated text, two distinct roles, role-scoped salary/location evidence, partial and unknown bodies, source provenance, and private account roots. |
| Cycles, restart, and STOP | In-process fake clock/signal | Three- and five-cycle runs use simulated 15-minute waits, repeat and revise a post, replay a completed plan, reject a changed plan with `CHECKPOINT_CONFLICT`, and stop/resume without a real wait. The existing BJ-04B tests cover POSIX non-`STOP` input, EOF, and Ctrl+C behavior. The CLI's multi-cycle mode itself remains a foreground, terminal-driven command. |
| Limits and negative capabilities | Source-level synthetic tests | Reject more than 20 posts per cycle, more than five cycles, and `--source browser` before creating archive/checkpoint state. `--source manual` returns `UNKNOWN / NO_TEXT` without reading a body or target. No HTTP, Playwright, login, hidden API, or account effect is authorized. |
| Contact and export privacy | Synthetic archive/export test | Only fixed placeholders such as `[SYNTHETIC_PHONE_001]` are allowed. Verify ambiguous contact evidence has no value or source span after raw expiry, selected JSON/CSV exports omit contact and source URLs, and CSV formula-like values are escaped. This is not permission to store real contact information. |
| Feed scrolling, virtualization, selectors, challenges, redirects | Fake adapter tests only / unsupported in installed CLI | `tests/test_browser_job_observation.py` may cover fake 3-to-8-card scrolling, eviction, and exact selected-post opening through in-process fake objects. The installed fixture command does not claim these UI features. There is no live browser or Threads test. |
| Hosted release gates | Live [Draft PR #139 check rollup](https://github.com/pumni/threads-standalone/pull/139) | Verify the check rollup belongs to the PR's current head SHA and that Windows, Ubuntu, and full-history Gitleaks all pass before coordinator review. |

## Local validation commands

Run these from the BJ-07 worktree with a fresh `--basetemp` directory outside the checkout. Do not reuse or delete a prior worker's temporary QA directory.

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
$qaBase = Join-Path $env:TEMP ("browser-jobs-bj07-" + [guid]::NewGuid().ToString("N"))
if (Test-Path -LiteralPath $qaBase) { throw 'basetemp already exists' }
uv run pytest --basetemp $qaBase
uv build
```

For the independent wheel check, create a new environment and working directory under a private temporary parent outside the checkout. Install the locked runtime dependencies and the built wheel into that environment, clear `PYTHONPATH`, and invoke its `threads-local` entry point from the outside working directory. Use a fresh private fixture directory and an empty data root created by the caller. Confirm the archive and checkpoint exist and the command output omits the synthetic body, alias, local paths, target URLs, and placeholder contacts. Preserve the temporary evidence directory; do not point the command at an existing account or operator root.

The PR must include the local command results, pass/skip counts, wheel smoke result, exact head SHA, changed-file manifest, and hosted Windows/Ubuntu/Gitleaks checks. `SYNTHETIC_READY` remains a candidate until a coordinator reviews that exact evidence.

### Current local candidate evidence

| Check | Windows result |
| --- | --- |
| `uv sync --locked` | PASS |
| Ruff check and format check | PASS |
| Strict Pyright | PASS — 0 errors, 0 warnings, 0 informations |
| Full synthetic pytest | PASS — 1,976 passed, 17 skipped; skips are host symlink/permission limits |
| `uv build` | PASS — source distribution and wheel built |
| Isolated installed-wheel smoke | PASS — top-level and nested help, one fixture cycle from outside the checkout with `PYTHONPATH` unset, archive/checkpoint creation, and output redaction |
| Exact-head hosted Windows, Ubuntu, Gitleaks | Live check rollup on [Draft PR #139](https://github.com/pumni/threads-standalone/pull/139); verify its head SHA before citing status |

The test suite collected 1,993 tests, including the ten BJ-07 acceptance tests. The exact committed head SHA, two-file manifest, and hosted run links belong to the live Draft PR record. This document does not claim coordinator acceptance.

## Operator limits and recovery

- The command is foreground-only and finite. It defaults to one cycle; `--max-cycles` accepts one through five, with at most 20 selected fixture posts per cycle.
- Multi-cycle execution requires a foreground terminal and a real interval of at least 15 minutes. QA simulates that interval through injected fake clocks; it must never sleep for 15 minutes in a test.
- To stop, type `STOP`, send EOF, or press Ctrl+C. After an interruption, resume only with the same synthetic account alias, caller-created root, run ID, ordered target plan, and fixture. A completed unchanged run replays without scanning again. A changed target plan under the same run ID fails with `CHECKPOINT_CONFLICT`.
- Unknown or unsupported observations do not become complete evidence and are not retried automatically. Checkpoint and archive data remain local to the caller-created private root; do not copy them into public logs, issues, screenshots, or artifacts.
- For unsupported behavior or a source defect, keep the result `UNSUPPORTED`/failed and request a separate narrow coordinator lease. Do not widen this two-file change or weaken assertions to make the matrix appear green.

## Release claims

Passing this synthetic matrix proves bounded behavior against fixtures and fake seams only. It does not prove complete Threads coverage, live source rights, customer readiness, account safety under live operation, packaging as a standalone executable, or production support. Browser reading stays disabled until its independent permission and integration gates pass; automated engagement stays separately blocked.
