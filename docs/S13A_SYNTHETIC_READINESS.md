# S13A synthetic offline acceptance evidence

**Checkpoint:** S13A offline acceptance
**Original authoring baseline:** `0d88e38c40b35c1ca6fbc97d9cefdaaf22c3013f`
**Integrated release-code baseline:** `adc0487ca2ba5034514f35ddcb1deee41cb3070c`
**Evidence type:** Synthetic local tests only

This document records what the S13A acceptance tests exercise and keeps local
synthetic evidence separate from operator, browser, and API release gates. The
tests create two synthetic account records and all private state beneath a
pytest temporary root. They use existing public interfaces; they do not claim
that a complete operator workflow has been implemented across these slices.
The branch integrates the accepted S10C change at the integrated baseline.
S10C issue #111 / PR #112 is merged, but the three tests in
`test_s13a_offline_acceptance.py` do **not** exercise S10C; S13A-specific S10C
coverage is `NOT_TESTED`.

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

## Local verification on the original reviewed head

The following results were recorded on the previous reviewed S13A head
`f39d9cfae69b5bf4ae786a7a5ae34e32dc4753a5` before S10C integration. New local
gates on the integrated PR head are recorded below after they complete.

| Check | Result |
| --- | --- |
| `uv sync --locked` | PASS |
| Ruff lint and format | PASS |
| Pyright | PASS; 0 errors, 0 warnings, 0 informations |
| Full pytest | PASS; 1,517 passed, 12 skipped |
| `uv build` | PASS |
| Isolated installed-wheel CLI and S5 smoke | PASS; CLI help and 1 synthetic S5 browser-MVP test outside checkout with `PYTHONPATH` unset |

The 12 full-suite skips are existing Windows symlink or POSIX-mode permission
limitations. No S13A acceptance case was skipped. The wheel smoke used the
synthetic fake-browser integration already maintained by S5; it did not start
Playwright or open a browser.

## Integrated-head local verification

Windows, CPython 3.14.7. These gates ran after merging the integrated baseline
at code snapshot `bcea264b7ccbeaadbd5d3ab417db0628c29be4e5` and before this
documentation-only update:

| Check | Result |
| --- | --- |
| `uv sync --locked` | PASS |
| `uv run --locked ruff check .` | PASS |
| `uv run --locked ruff format --check .` | PASS |
| `uv run --locked pyright` | PASS; 0 errors, 0 warnings, 0 informations |
| `uv run --locked pytest` | PASS; 1,533 passed, 12 skipped |
| `uv build` | PASS; source distribution and wheel built |
| Isolated installed-wheel `threads-local --help` | PASS; outside checkout with `PYTHONPATH` unset |
| Isolated installed-wheel S5 browser-MVP smoke | PASS; 1 passed outside checkout, `PYTHONPATH` unset, installed import-path assertion enabled |

The same 12 Windows symlink and POSIX-mode limitations account for the skips.
All three S13A tests passed on this integrated code snapshot. The final
documentation commit requires a fresh hosted check run on its exact PR head.

## Previous reviewed-head hosted checks

These completed checks cover only the previous reviewed head
`f39d9cfae69b5bf4ae786a7a5ae34e32dc4753a5`:

| Check | Result | Job |
| --- | --- | --- |
| Hosted Windows QA | PASS | [job 114740980467](https://github.com/pumni/threads-standalone/actions/runs/38228244617/job/114740980467) |
| Hosted Ubuntu QA | PASS | [job 114740980778](https://github.com/pumni/threads-standalone/actions/runs/38228244617/job/114740980778) |
| Full-history Gitleaks | PASS | [job 114740981713](https://github.com/pumni/threads-standalone/actions/runs/38228244617/job/114740981713) |

## Integrated-head verification

Status at this evidence revision: `PENDING`. The integrated S13A PR head still
requires fresh Windows QA, Ubuntu QA, and Gitleaks checks. After they pass, the
exact-head results will be linked in a final PR comment; they are not inferred
from the predecessor checks above.

## Release gates

`SYNTHETIC_READY` applies only to the named synthetic cases below; it is not
an overall release-readiness decision. The separate live/operator gates are
not granted by this work.

| Gate | Status | Evidence boundary |
| --- | --- | --- |
| `SYNTHETIC_READY` (named S13A cases only) | PASS | The cases above use synthetic accounts, text, and temporary local state only. |
| `MANUAL_UAT_READY` | NOT_TESTED / NOT_GRANTED | No operator UAT or real customer evidence was used. |
| `AUTHORIZED_BROWSER_READ_READY` | NOT_TESTED / NOT_GRANTED | No browser session, Playwright startup, Meta permission, or live Threads page was used. |
| `OFFICIAL_API_READY` | NOT_TESTED / NOT_GRANTED | No official API request, API credential, endpoint scope, or app review evidence was used. |
| Full supported cross-slice operator workflow | NOT_TESTED | The tests compose existing public contracts; they do not add or claim missing workflow integration. |
| S9B-2 handoff integration | GAP / NOT_TESTED | The tests exercise queue, S4 journal, and follow-up projection interfaces; they do not verify integrated handoff coordination. |
| S10C release change | MERGED | S10C #111 / PR #112 is present in the integrated baseline; S13A test coverage for S10C remains `NOT_TESTED`. |
| Integrated-head Windows QA, Ubuntu QA, and Gitleaks | PENDING | Fresh checks on the integrated PR head are required; predecessor-head PASS results above do not substitute. |

No live UAT is claimed. S9A preflight does not call `begin_handoff`; the tests
do not open a composer, fill a browser, send, publish, or verify a remote
outcome. S10A/S10B approval is local editorial review only and does not publish
content. No real account root, user content, token, cookie, or API media ID is
used.
