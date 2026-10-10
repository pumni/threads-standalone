# SPLIT-03 browser parity

## Contract and state

This checkpoint restores the frozen source behavior at `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1` for explicit local browser use, based on destination `main@2ad8294f5193cb2e1bdc1f1c15d8658ccc346b48`. The destination keeps the `threads_platform` namespace and the existing account JSON v1 schema.

| Command | Behavior |
| --- | --- |
| `threads-local account login ALIAS` | Opens headed persistent Chromium for a human-operated login, waits for Enter, then closes the browser. Normal login challenges remain with the operator. |
| `threads-local profile ALIAS USERNAME` | Opens the local account profile and verifies the bounded profile target. |
| `threads-local thread ALIAS THREAD_REF` | Opens the local account profile and verifies the bounded thread target. |
| `threads-local feed ALIAS [--limit N]` | Reads at most 20 normalized feed permalinks with bounded candidate/scroll work. |

The managed browser profile is `<data-root>/profiles/<account-uuid>` and its non-blocking process lock is `<data-root>/locks/<account-uuid>.lock`. Each account resolves to its own profile path. These are machine-local operational files, not PostgreSQL state and not migration input. The account schema is unchanged. Profiles, cookies, local account records, and lock artifacts must never be committed.

Browser navigation is restricted to the frozen source origin `https://www.threads.com`. Off-origin navigation/redirects, malformed targets, unrecognized UI states, challenge/session errors, browser crashes, and timeouts produce bounded errors and fail closed. No generic click/script interface, media composer, browser mutation, challenge bypass, anti-detect, or stealth behavior is included.

## Lazy boundary

`threads-local account add/list`, credential-reference setup, and all nine `threads-local api` reads remain usable without importing the browser package, Playwright, or Chromium. The CLI imports the local browser runtime only after an explicit `account login`, `profile`, `thread`, or `feed` command. Chromium installation is an explicit operator step (`uv run playwright install chromium`); application startup does not download browser binaries.

## Synthetic verification

Browser tests use fake Playwright/context/page/route objects and synthetic temporary directories. They cover result bounds and schemas, target/permalink normalization, headed persistent launch options, account profile separation, process-lock collisions, origin and redirect rejection, proxy validation, timeout/crash/error mapping, and challenge/session-expiry fail-closed handling. A CLI import sentinel covers account/API-only paths. Tests do not launch Chromium, make a live Threads request, read a real browser profile, or use real cookies or credentials. Destination browser behavior has not been live-verified.

The destination PR and issue #6 are the review evidence for this checkpoint. `ADR-0008` is accepted; source `ADR-0009` remains **Proposed** and is not changed or represented as accepted here. Broader rights and public-license policy remains open under destination issue #2.
