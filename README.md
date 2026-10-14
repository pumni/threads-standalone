# threads-standalone

Local account management, official Threads API commands, bounded headed-browser tools, and an operator-controlled manual recruitment workflow. The Python import namespace is `threads_platform`; the installed command is `threads-local`.

The repository contains source and synthetic examples only. Never commit tokens, cookies, browser profiles, local account files, operation or manual-outcome journals, real account identifiers, customer data, raw API data, or private review evidence.

**Product and roadmap:** [Two independent operating lanes — Browser and official Threads API](docs/PRODUCT_DIRECTION.md). The Browser lane is an independent automation tool (search/scroll/read/watch and review workflows), **not** an API fallback. Building and testing Browser functionality does not depend on Meta App Review, API token scopes, or recurring Threads Terms adjudication. Current live-browser restrictions are runtime facts, not prohibitions on writing Browser code; they can change only through a separate reviewed implementation and operator-selected UAT. No external-platform authorization is asserted. Historical milestone sections below remain historical.

## Browser-first manual workflow

Micro-slices S1–S5 provide a local workflow that keeps target selection and reply decisions with an operator:

1. **Use a synthetic local root and alias for practice.** Set `THREADS_LOCAL_DATA_ROOT` to a new directory outside the checkout and use synthetic account aliases. Do not use a customer account, real customer profile, or customer data for development or UAT.
2. **Intake and shortlist offline.** The operator supplies 1–20 UTF-8 permalink lines. `manual-list` canonicalizes and deduplicates them, then prints the ordered shortlist to a private interactive terminal. It does not search Threads.
3. **Write and review a private draft offline.** The operator writes the draft and may supply operator-written context. `manual-review` shows the exact canonical target, full draft, `UNKNOWN_IDENTITY`, `UNVERIFIED` author, `NOT_SENT`, and a SHA-256 approval. That SHA binds the account UUID, canonical URL, and edited draft; it does not bind context text or the browser actor's identity.
4. **Request one exact browser handoff only with separate authorization.** `manual-handoff` rechecks account, selection, canonical target, draft, and SHA; requires `--confirm-target`, `--interactive`, and literal `OPEN`; and durably records `HANDOFF_UNRESOLVED` before starting headed Chromium. With the default engine this is a real browser navigation to the selected URL. The integration test replaces the engine with a fake and never starts Chromium or reaches the website. S1–S5 provide no browser reply composer, auto-fill, or Send click.
5. **Verify account and target yourself.** The operator must check the logged-in account and exact target in the browser. If a reply is separately permitted, the operator uses the platform's native UI manually. Closing Chromium says nothing about whether a reply was sent; identity remains `UNKNOWN_IDENTITY` to the application.
6. **Resolve the local handoff before any later attempt.** `manual-status` reads the local journal. `manual-record` requires the current attempt ID, the same approved SHA, and literal `RECORD`; operator declarations are always `UNVERIFIED`. `ABORTED_NOT_SENT` is the only outcome that permits another handoff. `AMBIGUOUS` and `OPERATOR_REPORTED_SENT_UNVERIFIED` block it. Browser close is never treated as a send result.

The synthetic-only search and conversation adapters from S2 have no live Threads selectors or CLI search command. S1/S4 browser behavior has not had live UAT. Synthetic test success does not establish that Meta permits automated browser reading or any particular browser activity.

## Safe offline CLI example

Run only with synthetic fixtures and a fresh local root. The commands below do not open a browser or call an API:

```powershell
$sampleId = [Guid]::NewGuid().ToString('N')
$sampleRoot = Join-Path $env:TEMP "threads-standalone-synthetic-$sampleId"
$env:THREADS_LOCAL_DATA_ROOT = Join-Path $sampleRoot 'data'
$inputRoot = Join-Path $sampleRoot 'inputs'
New-Item -ItemType Directory -Path $inputRoot | Out-Null
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$targetsFile = Join-Path $inputRoot 'targets.txt'
$draftFile = Join-Path $inputRoot 'draft.txt'
[System.IO.File]::WriteAllText($targetsFile, "https://www.threads.com/@synthetic/post/fake_01`nhttps://www.threads.com/@synthetic/post/fake_02?utm_source=synthetic`n", $utf8NoBom)
[System.IO.File]::WriteAllText($draftFile, 'Synthetic operator-authored draft.', $utf8NoBom)
uv run --locked threads-local account add synthetic
uv run --locked threads-local account list
uv run --locked threads-local recruitment manual-list synthetic --targets-file $targetsFile
uv run --locked threads-local recruitment manual-review synthetic `
  --targets-file $targetsFile `
  --select 2 `
  --confirm-target "https://www.threads.com/@synthetic/post/fake_02" `
  --draft-file $draftFile
```

This creates a dedicated synthetic data root and UTF-8 files with no BOM outside the checkout. These commands require a private interactive terminal; private output is written to `stderr`. Manual review is ephemeral and does not write a queue, receipt, or draft journal. The sample stops before browser handoff.

The `manual-handoff`, `account login`, `profile`, `thread`, `feed`, and `browser open-thread` commands can open headed Chromium and navigate to the Threads website. Do not run them as a dry run or in CI. Use them only after separately approved live authorization and with the exact target and draft reviewed. Do not use them with real accounts as part of this synthetic checkpoint.

## Command boundaries

| Command group | Effect when an operator runs it |
| --- | --- |
| `recruitment manual-list`, `manual-review`, `manual-status` | Local input and journal reads; private terminal output; no network request |
| `recruitment manual-record` | Local journal write after exact operator confirmation; no network request |
| `recruitment manual-handoff`, `browser`, `account login`, `profile`, `thread`, `feed` | Headed browser and network navigation; account-specific persistent profile and process lock |
| `api` reads | Official API network request when supplied valid credentials and permissions |
| `post`, `reply`, media publishing, moderation, Nurture `--apply` | Can cause remote effects; use only with separate authorization for account, target, and content |
| `workflow`, `recurrence`, other Nurture commands | Follow their documented local/API boundaries; they do not make the browser MVP autonomous |

Browser Nurture has its own bounded, read-only browser workflow. The explicitly selected `browser` source opens the registered account's existing headed Chromium profile after an interactive one-shot confirmation; continuous mode requires a separate account and policy scoped opt-in after a successful supervised cycle. Automated Chromium tests use synthetic pages and an offline transport only. Private Windows live UAT has not yet been verified, and topic-search relevance counts are not a home/For You recommendation baseline. See [the operator notes](docs/BROWSER_NURTURE.md) for the UAT runbook, offline fixture use, state, and Windows Task Scheduler setup.

The API, mutation, Nurture, Workflow, and Recurrence commands remain separate existing capabilities. They retain their existing v1 account and operation schemas. S5 adds no production dependency and changes no existing schema.

## Setup and development

Requires Python 3.14 and uv. Chromium installation is an explicit operator step; application startup does not download it.

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest tests/test_api_runtime.py tests/test_cli_smoke.py
```

## Development and CI verification

For local development, run tests directly related to the change and the relevant lint/type checks. API changes do not need the full Browser E2E suite; Browser changes do not need API-specific tests by default. The explicit API, Browser, shared, and full-only test-file lists and source-file routing rules are maintained in the [CI routing helper](.github/scripts/ci.py). Any unmapped path, dependency/packaging change, or CI/test-infrastructure change selects full regression.

The `CI` workflow always starts for pull requests and pushes to `main`; it does not use workflow-level path filters. Each PR gets full-history Gitleaks, and changed Markdown gets local-link and diff-whitespace checks. Code PRs also build a wheel and install it with locked dependencies in an isolated environment outside the checkout. `PYTHONPATH` is unset; the wheel smoke checks package metadata and contents, the `threads-local` entry point, API/Browser parser contracts, and one synthetic fake-engine CLI flow. The source lane checks the lazy Browser import boundary. Wheel smoke runs only the package/CLI files, requires zero skips, and does not rerun full pytest.

| Change group | Required regression | Browser install | Windows coverage |
| --- | --- | --- | --- |
| `DOCS_ONLY` | Markdown links, `git diff --check`, and Gitleaks | No | No Python QA |
| `API_ONLY` | API + shared test lists on Ubuntu; targeted wheel smoke | No | API runtime/mutation plus filesystem, lock, process, CLI, state, and Browser lifecycle smoke |
| `BROWSER_ONLY` | Browser + shared test lists on Ubuntu; targeted wheel smoke | Ubuntu | Browser + Windows compatibility acceptance |
| `SHARED` | Full API and Browser synthetic suite on Ubuntu; targeted wheel smoke | Ubuntu | Windows compatibility smoke |
| `FULL` | Full synthetic suite, package build, and wheel smoke on Ubuntu and Windows | Both | Full Windows regression |

Pytest reports the 30 slowest tests that take at least 0.5 seconds, with skipped/failure counts in the job output. Pushes to `main`, manual `workflow_dispatch`, and unclassified/critical changes use `FULL`. Newer runs cancel older runs for the same PR; main runs are not cancelled. The stable status check is named `required CI gate`; the repository owner must mark it required in branch protection after review.

CI uses synthetic inputs and temporary roots only. Browser acceptance installs Chromium only in jobs that run Browser tests; fixtures use local/fake browser sessions and never send Threads requests. Both the Gitleaks Action event scan and the CLI `--all` history scan remain enabled because they provide event-commit reporting and repository-history coverage, respectively.

Browser Nurture acceptance launches local Playwright Chromium against generated synthetic pages. Both the fixture source and the live-source routing test block all real network access; neither uses a Threads account or sends a Threads request. See [the Browser Nurture notes](docs/BROWSER_NURTURE.md).

## Release readiness

This checkpoint is **synthetic-ready, not production-ready**. No live Threads UAT was run; the installed Browser Jobs live source is still disabled. Release readiness requires hosted Windows/Ubuntu QA, full-history Gitleaks, installed-wheel smoke, approved local data-root/security checks and separate operator-selected account/actor/target UAT. Browser development does **not** require a recurring Threads Terms clearance or API App Review. Official API capabilities separately depend on the scopes and App Review applicable to those endpoints. This repository does not claim external-platform endorsement.

See [the release readiness runbook](docs/BROWSER_MVP_RELEASE_READINESS.md) for installation and stop/recovery steps, required evidence, and the outstanding release gates. S1–S5 do not provide autonomous search, scheduling, crawling, automatic replies, or unsupervised engagement.

## Provenance and license status

Source transfer and file classifications are recorded in [PROVENANCE_DECISION.md](docs/PROVENANCE_DECISION.md); that record is not a general reuse license. The public distribution license remains undecided. No first-party LICENSE, SPDX declaration, or open-source license claim has been added. Third-party dependencies remain under their respective upstream licenses.
