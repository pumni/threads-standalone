# S13A synthetic offline acceptance evidence

**Checkpoint:** S13A offline acceptance
**Authorized base:** `0d88e38c40b35c1ca6fbc97d9cefdaaf22c3013f`
**Evidence type:** Synthetic local tests only

This document records what the S13A acceptance tests exercise and keeps local
synthetic evidence separate from operator, browser, and API release gates. The
tests create two synthetic account records and all private state beneath a
pytest temporary root. They use existing public interfaces; they do not claim
that a complete operator workflow has been implemented across these slices.

## Synthetic cases

| Case | Result | Evidence |
| --- | --- | --- |
| S6 queue import and account isolation | PASS | Two synthetic accounts receive distinct canonical targets and independently scoped queue snapshots. |
| S7 qualification | PASS | The Vietnamese job-seeker is classified and eligible for review; the recruiter is correctly classified and excluded from reply review. |
| S8 draft and human review boundary | PASS | The deterministic fake generator creates an ephemeral draft; `KEPT_FOR_SESSION` is followed by a distinct S9A approval step. No provider or send path is called. |
| S9A exact approval | PASS | Exact approval preflight succeeds for the original account, target, context, draft, and queue snapshot; wrong account, wrong target, and changed fresh context are rejected. |
| S4 outcome journal and restart read | PASS | A synthetic unresolved handoff is reread through a new store instance, then local ambiguous and aborted outcomes are recorded under the temporary root. |
| S7 blocking and S9B-1 projection | PASS | Ambiguous evidence is blocked from ranking and projects to `NEEDS_VERIFICATION`; explicit `ABORTED_NOT_SENT` projects to `CLOSED`; a second account with no supplied record projects to `NO_FOLLOWUP`. Foreign-account evidence is rejected. No sent or reply-waiting state is produced. |
| S10A planner and S10B private content CLI | PASS | Synthetic operator-authored content is reviewed through the private CLI, account isolation is checked, stale compare-and-review is rejected, and publication status remains `NOT_PUBLISHED_BY_PLANNER`. Non-TTY review is rejected. |
| Network guard | PASS | Acceptance tests replace socket connection and URL-open entry points with failures; no network call is expected or made. |

“PASS” means only that the named behavior passed with generated synthetic
fixtures in the local test environment. In particular, S4 records carry
`UNVERIFIED` operator declarations and do not establish remote delivery,
Threads conversation coverage, or hiring status. A local `CLOSED` follow-up
state means only that the local handoff attempt was marked `ABORTED_NOT_SENT`.

## Local verification

Windows, CPython 3.14.7:

| Check | Result |
| --- | --- |
| `uv sync --locked` | PASS |
| `uv run --locked ruff check .` | PASS |
| `uv run --locked ruff format --check .` | PASS |
| `uv run --locked pyright` | PASS; 0 errors, 0 warnings, 0 informations |
| `uv run --locked pytest` | PASS; 1,517 passed, 12 skipped |
| `uv build` | PASS; source and wheel artifacts built |
| Isolated installed-wheel CLI smoke | PASS; `threads-local --help`, outside checkout, `PYTHONPATH` unset |
| Isolated installed-wheel S5 browser-MVP smoke | PASS; 1 passed, outside checkout, `PYTHONPATH` unset, installed import-path assertion enabled |

The 12 full-suite skips are existing Windows symlink or POSIX-mode permission
limitations. No S13A acceptance case was skipped. The wheel smoke used the
synthetic fake-browser integration already maintained by S5; it did not start
Playwright or open a browser.

## Release gates

| Gate | Status | Evidence boundary |
| --- | --- | --- |
| S13A synthetic offline acceptance | PASS | The cases above use synthetic accounts, text, and temporary local state only. |
| `MANUAL_UAT_READY` | NOT_TESTED | No operator UAT or real customer evidence was used. |
| `AUTHORIZED_BROWSER_READ_READY` | NOT_TESTED | No browser session, Playwright startup, or live Threads page was used. |
| `OFFICIAL_API_READY` | NOT_TESTED | No official API request or API credential was used. |
| Full supported cross-slice operator workflow | NOT_TESTED | The tests compose existing public contracts; they do not add or claim missing workflow integration. |
| S10C release state | PENDING | S10C PR #112 was unmerged at this checkpoint and is not reported as shipped or verified. |
| Hosted Windows QA, Ubuntu QA, and Gitleaks | PENDING | Must be checked on the exact pushed S13A PR head before coordinator acceptance; local results do not substitute. |

No live UAT is claimed. S9A preflight does not call `begin_handoff`; the tests
do not open a composer, fill a browser, send, publish, or verify a remote
outcome. S10A/S10B approval is local editorial review only and does not publish
content. No real account root, user content, token, cookie, or API media ID is
used.
