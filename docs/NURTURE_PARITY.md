# SPLIT-05 Nurture v1 parity

## Baselines and scope

- Source is read-only at `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`.
- Destination branch starts at `pumni/threads-standalone@d28bb979d720ffc6b493e76e9709d80bc0709d5c`.
- The package and CLI remain `threads_platform` and `threads-local`.
- This checkpoint transfers Nurture v1 only. It does not add Workflow/recurrence, a background scheduler, Controller, Worker/WorkerJob, PostgreSQL, or a dependency on the source repository.
- Every run and test in this change uses fake API methods, generated UUIDs, synthetic text/IDs and temporary files. No live Threads request, publish, reply, login, account state, token, browser profile, journal, or private evidence was used.

## Source implementation and test mapping

| Frozen source module | Destination module | Contract retained |
| --- | --- | --- |
| `standalone/nurture.py` | `standalone/nurture.py` | Strict recruitment preset v1, bounded settings and deterministic policy decisions. |
| `standalone/nurture_runner.py` | `standalone/nurture_runner.py` | Sequential receipt lifecycle, source ordering, explicit apply and journal linkage. |
| `standalone/nurture_discovery.py` | `standalone/nurture_discovery.py` | API-first bounded mentions/search/profile discovery, normalization, ranking, dedupe and cooldown. |
| `standalone/nurture_conversations.py` | `standalone/nurture_conversations.py` | Bounded inbound reply discovery, proven ownership and reply fingerprint dedupe. |
| `standalone/nurture_store.py` | `standalone/nurture_store.py` | JSON v1 receipts, target/content state, insights snapshots, atomic writes, path validation and account lock. |
| `standalone/nurture_content.py` | `standalone/nurture_content.py` | Operator content packet validation and byte-compatible source/draft/content fingerprints. |
| `standalone/nurture_draft.py` | `standalone/nurture_draft.py` | Strict private reply-draft schema and safe representation. |
| `standalone/nurture_quote_draft.py` | `standalone/nurture_quote_draft.py` | Strict private quote-draft schema and safe representation. |
| `standalone/nurture_insights.py` | `standalone/nurture_insights.py` | Bounded read-only refresh of journal-linked published content and immutable snapshots. |

| Source test family | Destination test file | Coverage retained |
| --- | --- | --- |
| `test_standalone_nurture.py` | `test_nurture.py` | Preset schema and deterministic policy. |
| `test_standalone_nurture_apply.py` | `test_nurture_apply.py` | Reply/quote validation, reservation, one-action limit, journal links, ambiguous/no-retry and CLI behavior. |
| `test_standalone_nurture_content.py` | `test_nurture_content.py` | Content packet, redaction and fingerprint contracts. |
| `test_standalone_nurture_conversations.py` | `test_nurture_conversations.py` | Reply normalization, ownership evidence and dedupe. |
| `test_standalone_nurture_discovery.py` | `test_nurture_discovery.py` | Source caps, fixtures, ranking, deterministic order and duplicate identities. |
| `test_standalone_nurture_durability.py` | `test_nurture_durability.py` | Atomic state, strict schemas, symlink/path rejection, locks and recovery. |
| `test_standalone_nurture_insights.py` | `test_nurture_insights.py` | Journal validation, fetch bounds, snapshots, null metrics and scoring. |
| `test_standalone_nurture_own_content.py` | `test_nurture_own_content.py` | Source/draft provenance, due policy, reservation, journal linkage and ambiguity. |
| `test_standalone_nurture_runner.py` | `test_nurture_runner.py` | Observe receipts, bounded calls, safe output, error handling and cancellation. |
| `test_standalone_nurture_runner_inbound.py` | `test_nurture_runner_inbound.py` | Proven inbound priority, unknown/self ownership and no-action outcomes. |

The tests' imports were adapted to the destination's existing `api.contracts`, `api.client`, `config`, and `locks` modules. All 490 previously passing destination tests remain in the suite. `tests/test_nurture_cli.py` adds a synthetic CLI smoke across observe, explicit own-content apply and insights, using a fake API with the real local SPLIT-04 operation journal.

## CLI and runtime boundary

The accepted CLI shapes remain:

```text
threads-local nurture run ALIAS --preset recruitment
threads-local nurture run ALIAS --preset recruitment --apply --reply-file FILE
threads-local nurture run ALIAS --preset recruitment --apply --post-file FILE
threads-local nurture run ALIAS --preset recruitment --apply --quote-file FILE
threads-local nurture insights refresh ALIAS --preset recruitment
```

Default `nurture run` composes only the API read runtime. It passes `include_mutations=False` and `include_browser=False`; the composition constructs neither a mutation runtime nor a browser runtime. The runner performs bounded API reads only. It does not launch Playwright or publish automatically. API read failures, invalid ownership and cancellation retain their bounded error/receipt outcomes; normal output and state omit raw reply text, usernames, cursors and remote IDs.

`--apply` can cause one explicit reply, text post or quote mutation only after a validated operator draft/candidate is selected and policy permits it. When an actionable target exists without a draft, the existing v1 contract returns a recommendation and does not mutate. Before the single SPLIT-04 journaled mutation, the runner durably reserves the target/content state. It links the resulting operation ID; a `PENDING`, `CONFIRMED`, or `AMBIGUOUS` target is not automatically reselected or retried. A timeout or exception after `PUBLISH_REQUESTED` is not treated as proof of failure. Insights refresh reads only content linked to an account-matching `POST_TEXT` operation in `PUBLISHED`; it does not mutate or create a Nurture run receipt.

## RS-01 account-scoped inbound identity

The built-in `recruitment` preset remains unchanged and leaves inbound unconfigured. An explicit Python caller may create an ephemeral `AccountNurtureInboundConfig` for one local alias and pass it to `NurtureRunner.run_with_selection`; construction revalidates the preset, alias, public username shape and 0–10 own-Thread cap. The existing CLI gets no new configuration flag and stays unconfigured. The override is not stored in account JSON or Nurture v1 state. The inbound reader first calls Meta's official Threads API `GET /me?fields=id,username` using the alias's resolved user token, then compares the returned authenticated username with the configured claim before reading `profile_posts`. Meta's [official Threads API collection](https://www.postman.com/meta/threads/request/34203612-3996f0a2-f2ee-4af7-9b66-3dbbe80b8d7d) documents that request as “Get App-Scoped User's Profile Information”; the public-profile lookup alone is not treated as ownership evidence. Missing identity evidence, denied permission, mismatch, invalid configuration and subsequent read errors have distinct safe inbound statuses. For an enabled inbound config (cap 1–10), every status other than `INBOUND_READY` fails the run at `selection`: the existing Nurture v1 receipt is persisted as `FAILED` with that bounded status as `error_code`, before generic discovery or any mutation. No inbound config and an explicit zero cap both report `INBOUND_NOT_CONFIGURED` and preserve the legacy discovery path; neither is described as a completed empty inbound scan. No receipt schema keys are added.

RS-01 synthetic acceptance coverage maps to `tests/test_api_discovery.py` (the exact `/me` read and strict response contract), `tests/test_api_runtime.py` (alias-bound credential resolution), `tests/test_nurture.py` (validated account-scoped configuration), `tests/test_nurture_conversations.py` (unconfigured, identity, permission, mismatch, cap, ownership and dedupe cases), `tests/test_nurture_runner_inbound.py` (failed identity/profile/conversation reads, persisted failed receipts, zero discovery fallback/mutation, invalid config, no-config/zero-cap legacy discovery and safe CLI output), `tests/test_nurture_own_content.py` (foreign inbound alias with an approved post candidate), and `tests/test_nurture_apply.py` (wrong authenticated username with a reply draft). All reads and mutation doubles use synthetic fixtures and temporary local roots; no v1 persisted key set changes.

## State and fingerprint compatibility

The port retains Nurture JSON schemas at version 1 and the exact path scheme, key sets, counters, UUID/link behavior, UTC timestamps, atomic persistence, account lock and root/path checks defined in [STATE_COMPATIBILITY.md in SPLIT-00](https://github.com/pumni/thread/blob/d96b41c1feae7bad50132e57ec51b7744c0b3396/docs/standalone-split/STATE_COMPATIBILITY.md). The port does not rewrite or re-key existing local data.

The port retains the source fingerprint inputs and encodings: normalized remote Thread ID; `reply:` plus normalized reply ID; canonical source-kind/source-ID JSON; the `threads-native-post:v1` draft namespace; the `threads-nurture-content:v1` content namespace; and the `threads-nurture-insights-snapshot:v1` snapshot namespace. Digest remains SHA-256 with the original separators, canonical JSON, Unicode normalization and lowercase hex. Tests compare deterministic fingerprints and persisted keys using only synthetic values.

Run receipts keep `RUNNING`, `SUCCESS`, `FAILED`, and `INTERRUPTED`; Nurture stale-run repair remains under its process lock. Target actions retain `NONE`, `PENDING`, `CONFIRMED`, and `AMBIGUOUS`; content retains `NEW`, `RESERVED`, `PUBLISHED`, `AMBIGUOUS`, and `REJECTED`. Operation journal v1 remains owned by SPLIT-04. Target state and the operation journal remain separate linked records.

## Checkpoint 15 evidence boundary

The source SPLIT-00 acceptance matrix records the coordinator's scoped Checkpoint 15 result: after the earlier confirmed Reply-to-Mention, one subsequent default observe invocation selected a different candidate, did not select the already-published target again, retained that target's `CONFIRMED` state and linked operation, left mutation journal hashes unchanged, and wrote one completed observe receipt. This is coordinator-reported historical evidence from source issue #229, not newly live-verified destination behavior. It supports suppression and state/journal preservation across that following CLI invocation only; it does **not** establish full restart-safe idempotency. This checkpoint uses synthetic regression tests and does not replay the historic operation.

## Isolation and provenance

The Nurture import closure uses the destination's existing API, account, mutation journal and local lock modules. It adds no runtime dependency. No Nurture module imports browser, Worker, Controller, scheduler, PostgreSQL, persistence, Alembic, SQLAlchemy, asyncpg, FastAPI, Uvicorn or the old repository. The app composition keeps the Nurture observe and insights paths API-only.

The source file-level history/header audit and path mapping are in [PROVENANCE_DECISION.md](PROVENANCE_DECISION.md). The transfer classification is limited to the owner-authorized SPLIT-05 engineering move. Public license/provenance issue #2 remains OPEN; this change adds no `LICENSE` or SPDX marker.

## Validation record

Validation results and exact commit/hosted CI status are recorded in the Draft PR after the authorized gates finish. No live API or browser acceptance is claimed here.
