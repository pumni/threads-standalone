# threads-standalone

Local account management, official Threads API commands, bounded headed-browser tools, and an operator-controlled manual recruitment workflow. The Python import namespace is `threads_platform`; the installed command is `threads-local`.

The repository contains source and synthetic examples only. Never commit tokens, cookies, browser profiles, local account files, operation or manual-outcome journals, real account identifiers, customer data, raw API data, or private review evidence.

**Product and roadmap:** [Two independent operating lanes — Browser and official Threads API](docs/PRODUCT_DIRECTION.md). Browser is a durable standalone path, **not** a temporary bridge or something to retire when API permissions arrive. This is a product decision, not authorization for automated live browsing. For current implementation, read the applicable code and exact-head PRs; historical milestone sections below remain historical.

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

The API, mutation, Nurture, Workflow, and Recurrence commands remain separate existing capabilities. They retain their existing v1 account and operation schemas. S5 adds no production dependency and changes no existing schema.

## Setup and development

Requires Python 3.14 and uv. Chromium installation is an explicit operator step; application startup does not download it.

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
```

CI installs the built wheel with locked runtime dependencies in an isolated environment outside the checkout, clears `PYTHONPATH`, verifies the `threads-local` entry point and package contents, and runs the synthetic suite against the installed wheel. The integrated S1–S5 test uses a fake browser engine and temporary synthetic roots.

## Release readiness

This checkpoint is **synthetic-ready, not production-ready**. No live Threads UAT was run. No platform permission for automated browser reads or actions is claimed. A release still requires hosted Windows and Ubuntu QA, full-history Gitleaks, isolated installed-wheel smoke, approved local data-root/security checks, separately approved account/actor/target UAT, and confirmation of platform terms and compliance requirements. Official API features remain subject to their applicable Meta app review and permissions.

See [the release readiness runbook](docs/BROWSER_MVP_RELEASE_READINESS.md) for installation and stop/recovery steps, required evidence, and the outstanding release gates. S1–S5 do not provide autonomous search, scheduling, crawling, automatic replies, or unsupervised engagement.

## Provenance and license status

Source transfer and file classifications are recorded in [PROVENANCE_DECISION.md](docs/PROVENANCE_DECISION.md); that record is not a general reuse license. The public distribution license remains undecided. No first-party LICENSE, SPDX declaration, or open-source license claim has been added. Third-party dependencies remain under their respective upstream licenses.
