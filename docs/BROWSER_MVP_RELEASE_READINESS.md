# Browser MVP release readiness

This runbook covers the S1–S5 operator workflow. It does not authorize a live Threads session, automated reading, API request, reply, or customer-data test. Synthetic readiness and production readiness are separate gates.

## Feature and effect matrix

| Step / command | What it does | Network or remote effect |
| --- | --- | --- |
| `recruitment manual-list` | Canonicalizes up to 20 operator-supplied permalinks, deduplicates them, and prints the ordered shortlist to a private TTY | Offline |
| `recruitment manual-review` | Shows the selected exact target, operator context, full operator-authored draft, actor `UNKNOWN_IDENTITY`, and SHA-256 | Offline; no queue or draft persistence |
| `recruitment manual-handoff` | Revalidates exact account, target, draft SHA and `--confirm-target`; asks for `OPEN`; persists `HANDOFF_UNRESOLVED`; starts headed S1 browser handoff | Navigates to the selected Threads URL. This is a real browser operation and requires separate authorization |
| `recruitment manual-status` | Reads the local outcome journal for the exact account and canonical target | Offline; status is not proof of an online result |
| `recruitment manual-record` | Requires the current attempt ID, same approved SHA and literal `RECORD`; records an operator declaration as `UNVERIFIED` | Offline local journal write |
| S1 `browser open-thread`, login/profile/thread/feed | Uses a headed browser and account-specific persistent profile | Browser network access; separate approval required |
| S2 search/conversation adapters | Exercise bounded extraction against fake engines and synthetic fixtures | Synthetic only; no live selector or browser-search CLI |

The approval SHA binds the account UUID, canonical target URL, and final edited draft. It does not bind operator context or browser actor identity. The application reports `UNKNOWN_IDENTITY` for the actor. S1–S5 do not find targets online, fill a reply, or click Send. The operator may use the official native UI only where separately permitted.

## Synthetic startup and verification

Use a clean checkout and a new synthetic root outside the repository. Never point the root at a customer/operator directory, existing browser profile, or prior product data.

```powershell
uv sync --locked
$env:THREADS_LOCAL_DATA_ROOT = Join-Path $env:TEMP 'threads-standalone-synthetic'
uv run --locked threads-local --help
uv run --locked threads-local account add synthetic
```

For synthetic rehearsal, use only `manual-list` and `manual-review` with synthetic permalink strings and operator-authored synthetic files in a private interactive PowerShell terminal. The integrated test exercises the complete dispatcher path, including handoff and outcome recording, with a fake browser engine and temporary data. It never starts Chromium, performs DNS/network access, opens an OS login, calls the API, or invokes an API mutation runtime.

The live browser command is intentionally excluded from test and dry-run instructions. Installing Chromium is a separate operator action that can download a browser binary:

```powershell
uv run --locked playwright install chromium
```

Only install it in an environment approved for the intended browser use. The application does not install Chromium during startup.

## Private TTY and operator sequence

The four manual commands require stdin, stdout, and stderr to be a private interactive TTY and refuse CI. Run them in a local PowerShell window without piping or redirecting output. The full six-step workflow and safe synthetic examples are in the root [Vietnamese operator guide](../HUONG_DAN_SU_DUNG_THREADS_STANDALONE.md).

For any separately approved live handoff:

1. Inspect the private `manual-review` output. Confirm account alias/UUID, one-based selection, exact canonical URL, full draft and SHA. Changing the account, canonical target, or draft invalidates the SHA; context is displayed but is not included in it.
2. `manual-handoff` asks for exact `OPEN`. It writes a durable `HANDOFF_UNRESOLVED` attempt before browser startup. Persistence errors or lock contention must stop before Chromium opens.
3. In Chromium, the operator checks the logged-in identity and exact target. The software cannot verify the actor. It does not compose, autofill, or send.
4. Closing the browser, a crash, Ctrl+C, timeout, or missing attestation does not establish sent or not sent. Check `manual-status`; do not repeat the handoff while it reports `HANDOFF_UNRESOLVED`.
5. To resolve the current attempt, supply the exact attempt ID and unchanged SHA to `manual-record`, then type `RECORD`. Select `ABORTED_NOT_SENT` only when the operator knows no send occurred. Use `AMBIGUOUS` when the result may have occurred but is unknown, or `OPERATOR_REPORTED_SENT_UNVERIFIED` for a truthful operator report of a send. Both latter outcomes block another handoff.
6. Read status again. Every recorded outcome is `UNVERIFIED`, not an official receipt or API result. Only `ABORTED_NOT_SENT` allows a later handoff.

## STOP and recovery matrix

| Observation | Required response |
| --- | --- |
| `NO_RECORD` | The local journal has no record for this account/target. It does not establish anything about online activity. Recheck the target and review before any separately approved handoff. |
| `HANDOFF_UNRESOLVED` | STOP. Do not start another handoff. Resolve only the displayed current attempt ID and same SHA with an exact `RECORD` and truthful outcome. |
| `AMBIGUOUS` or `OPERATOR_REPORTED_SENT_UNVERIFIED` | STOP. No retry or new handoff for that account/target. Escalate through the responsible operator's process; do not alter the journal to clear the block. |
| `MANUAL_OUTCOME_BLOCKED` | STOP. A pending or blocking outcome exists. Read private status and follow its exact state; never bypass the block. |
| `MANUAL_OUTCOME_INVALID` / corrupt or unreadable journal | STOP and preserve the whole local data root. Do not delete, hand-edit, restore an older journal over it, or start a replacement attempt. Escalate for a reviewed recovery. |
| `MANUAL_OUTCOME_BUSY` / account lock busy | STOP and allow the existing foreground process to finish. Do not remove lock files manually or start a parallel command. |
| `MANUAL_TARGET_MISMATCH`, `MANUAL_APPROVAL_MISMATCH`, or changed draft | STOP. Re-run offline list and review, inspect the full new preview, and obtain a new SHA. Do not attempt to reuse an old approval. |
| stale attempt ID / `MANUAL_OUTCOME_ATTEMPT_MISMATCH` | STOP. Read current status in the private terminal and use only its current pending attempt. An older `ABORTED_NOT_SENT` event cannot resolve a newer attempt. |
| Ctrl+C, EOF, browser crash, timeout, challenge, session loss, or uncertain target | STOP. Preserve the pending journal. Browser close is not an outcome; no automatic retry occurs. Resolve only after the operator has reliable grounds for one of the allowed declarations. |
| account identity or target cannot be verified in the browser | STOP without acting in the native UI. Record only a truthful outcome; never infer identity from the URL or profile. |

`NO_RECORD` is a local absence only. `HANDOFF_UNRESOLVED` remains durable across process restarts. Do not use cleanup scripts or account removal to erase unresolved journal state or account profiles. The S5 workflow has no reset command.

## Data handling, evidence, and rollback

- Keep the data root private and access-controlled. Account metadata, persistent Chromium profiles and cookies are sensitive. Protect backups with the same controls; never upload them to CI, issues, chat, or shared evidence folders.
- The manual outcome journal stores bounded account/target fingerprints, draft SHA, attempt/event IDs, timestamps, status, and verification marker. It does not store raw target URLs, draft text, or context. The targets, draft, and context input files remain operator-managed and may contain private information.
- For review evidence, report branch/commit, gate result/counts, safe error codes, and whether a gate is pending. Do not capture or attach cookies, tokens, profile directories, real target URLs, draft/context text, private screenshots, or raw live API responses.
- S5 adds no state migration and does not change existing v1 schemas. If rolling code back, stop foreground commands and retain the local root intact. Use only a previously reviewed version known to handle the journal namespace. If compatibility is uncertain, stop and preserve the data for review; never clear or downgrade journal state to make an older CLI proceed.
- Removing or replacing a profile is not a recovery for a journal block. Preserve both account profiles and outcome records until an authorized owner reviews the case.

## Release gates

The hosted workflow must pass on Windows and Ubuntu: locked uv sync, runtime dependency audit, Ruff lint/format, Pyright, full synthetic pytest, package build, isolated installed-wheel smoke outside the checkout, and full-history Gitleaks. The installed-wheel run must clear `PYTHONPATH`, verify the installed import path and CLI entry point, and run the integrated synthetic browser-MVP test using the wheel.

| Gate | Current checkpoint record |
| --- | --- |
| Local Windows locked quality gates (Python 3.14.7) | **PASS**: `uv sync --locked`, runtime tree audit, Ruff lint/format, Pyright, build, synthetic shortlist demo; pytest `1266 passed, 6 skipped` |
| Isolated installed-wheel smoke, artifact version `0.1.0` | **PASS** outside checkout with empty `PYTHONPATH`, installed CLI `--help`, site-packages assertion and full pytest `1260 passed, 12 skipped` |
| Hosted Windows QA | **PENDING** hosted check |
| Hosted Ubuntu QA | **PENDING** hosted check |
| Full-history Gitleaks | **PENDING** hosted check |
| Local test skip review | Six symlink tests skipped for Windows privilege; wheel run also skips six source-only demo tests not packaged in the wheel |
| Live Threads UAT | **NOT RUN**; no live account, browser, API, or customer data used for S5 |
| Local data-root and account-owner/security review | **PENDING** separate operator approval |
| Actor identity and exact-target proof in approved manual UAT | **PENDING**; software identity remains `UNKNOWN_IDENTITY` |
| Platform terms/compliance permission for automated browser reads or actions | **PENDING**; no permission is claimed |
| Meta App Review and permissions for any gated official API capability | **PENDING** and separate from the browser handoff |
| Provenance/license decision | **PENDING**; public distribution license remains undecided |
| Coordinator UAT issues #18 and #46 | **PENDING** separate review |

All gates above are required before calling this production-ready or planning customer rollout. A passing synthetic suite or installed wheel is not platform authorization or live UAT.
