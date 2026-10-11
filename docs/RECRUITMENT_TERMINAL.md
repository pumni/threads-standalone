# Recruitment foreground terminal guide (RS-05)

This guide documents the explicit, single-session coordinator in
[`scripts/run_recruitment.py`](../scripts/run_recruitment.py). The script is a thin source-tree
entry point that imports `threads_platform.standalone.recruitment_session`; it is not included in
the wheel. The wheel ships the `threads_platform` package and the `threads-local` executable.
Wheel tests run the source entry point while verifying that its imported package comes from the
installed wheel. This preserves the source file's provenance without implying that the wheel
contains it.

All RS-05 automated scenarios use fake API/identity/mutation implementations and synthetic roots.
No real account, token, browser profile, operator root, or Threads request is part of this evidence.
Issue #2 (rights/provenance) and issue #18 (operator installation/live UAT) remain unresolved.

## Phases and local/remote effects

| Phase | Reads | Writes | What its result establishes |
| --- | --- | --- | --- |
| `--validate` | Local account v1 and recruitment preset | None; no client, lock, receipt, or root creation | Local metadata and root are usable. `READY` says nothing about a credential, API access, ownership, or quota. |
| `--preflight` | Token-bound identity, then publishing and reply quota | No local or remote writes | A bounded READ-only capability snapshot was observed at that moment. It is not an ownership approval or a guarantee that a later mutation will succeed. |
| `--observe` | Discovery; optionally configured inbound identity, owned posts, and conversations | Local receipt and dedupe state | A bounded observation completed. With no inbound owner/cap flags, legacy discovery runs normally. When inbound is configured, an identity/read failure stops the run and persists a sanitized `FAILED` receipt before generic discovery. |
| `--review` | Same bounded reads as observe, plus an optional validated local reply/post packet | Local receipt and dedupe state; private terminal preview | The exact currently selected effect and packet were shown for review. No remote mutation runtime is created. |
| `--apply-reply-file` / `--apply-post-file` | Review reads, then exact revalidation and quota reads | Local receipt, reservation and existing operation journal; one remote effect after consent | Only the displayed, still-valid effect can cross the existing journaled mutation boundary. A later quota check remains authoritative. |

Example local-only check:

```powershell
$env:THREADS_LOCAL_DATA_ROOT = Join-Path $env:TEMP 'synthetic-threads-root'
$accountAlias = 'synthetic-account'
uv run --locked python scripts/run_recruitment.py --account $accountAlias --validate
```

`--validate` is intentionally zero-write. The remote preflight is a separate opt-in command:

```powershell
uv run --locked python scripts/run_recruitment.py --account $accountAlias --preflight
```

Preflight performs the official token-bound identity read and quota read through the existing
READ runtime. It prints only `credential=verified` and bounded quota states (`available`,
`exhausted`, or `unverified`); it does not print a username, user ID, quota counts, token, raw
response, or write a receipt. Missing credentials and API errors stop before quota is read. An
unknown quota response is `STOP`. A successful snapshot is not retained and can become stale
immediately. Apply checks the relevant quota again before reserving and publishing.

## Inbound identity, exact target, and consent

Inbound reading stays disabled unless both `--inbound-owner-username` and
`--inbound-thread-cap 1..10` are supplied for that invocation. The username is an ephemeral claim;
the account's resolved token must prove the same username through the authenticated identity read.
An identity mismatch or failed identity/profile/conversation read fails closed. It cannot fall
through to search/mentions and cannot authorize a reply or post. Omitting both flags preserves the
legacy discovery path.

Review/apply requires a foreground private terminal: CI must be off, stderr must be a TTY, and
apply also requires stdin to be a TTY. The private terminal displays the target fingerprint,
source, reason codes, action, target context and proposed reply, or the own-content source,
category, action and proposed post. Raw context and draft text never go to stdout, an error,
receipt, or mutation journal. Type `APPLY` to approve the displayed effect; any other input
cancels before the mutation runtime is constructed. The runner revalidates the same target or
candidate after consent and makes at most one mutation per invocation.

```powershell
uv run --locked python scripts/run_recruitment.py --account $accountAlias --review --review-reply-file REPLY_PACKET
uv run --locked python scripts/run_recruitment.py --account $accountAlias --review --review-post-file POST_PACKET
uv run --locked python scripts/run_recruitment.py --account $accountAlias --apply-reply-file REPLY_PACKET
uv run --locked python scripts/run_recruitment.py --account $accountAlias --apply-post-file POST_PACKET
```

Each review/apply invocation is a distinct foreground phase. Apply repeats the reads and exact
target check; a previous review is not a durable approval token. The operator must inspect the
private preview immediately before entering `APPLY`.

## Failures and restart behavior

Configured inbound failures persist bounded receipt outcomes. Local access and credential errors
are reported as safe codes. A deterministic pre-submit rejection remains distinguishable from an
ambiguous result. Once a publish request may have reached Threads, the operation journal and
Nurture state retain `AMBIGUOUS`; rerunning does not replay that target or own-content candidate.
Interruption is recorded and a new invocation starts a new bounded read. Same-account Nurture and
mutation locks prevent concurrent work for that account; other account IDs use separate locks.
Receipts and journal records retain their existing v1 key sets.

The source entry point can be checked without using a live account:

```powershell
uv run --locked python scripts/run_recruitment.py --help
```

The installed-wheel CI runs the same synthetic script E2E tests from outside the checkout, with
`PYTHONPATH` unset, and asserts that the imported runtime module resolves from `site-packages`.
The helper script itself remains a repository source file and is not represented as an installed
wheel console command.

## RS-05 synthetic acceptance matrix

| # | Scenario | Evidence |
| ---: | --- | --- |
| 1 | Real script `--validate` is zero-write and imports only local metadata | `tests/test_recruitment_e2e.py::test_real_script_validate_is_zero_write_and_proves_wheel_import_provenance` |
| 2 | Missing local root/config fails without creating a root | `tests/test_recruitment_session.py::test_script_validate_is_zero_write_with_only_synthetic_environment`; `test_validate_missing_or_unwritable_root_fails_without_creating_state` |
| 3 | Missing credential stops preflight before any API request | `tests/test_recruitment_e2e.py::test_real_script_preflight_rejects_missing_credential_without_remote_call` |
| 4 | Expired/invalid identity and denied permission fail closed | `tests/test_recruitment_e2e.py::test_real_script_preflight_fails_closed_on_authentication_and_permission_errors` |
| 5 | Available, exhausted, and unknown quota states are bounded; apply retains its own quota check | `tests/test_recruitment_e2e.py::test_real_script_preflight_reports_bounded_quota_snapshot`; `tests/test_nurture_apply.py` |
| 6 | No-config inbound uses legacy discovery and persists success | `tests/test_recruitment_e2e.py::test_real_script_no_config_preserves_legacy_discovery_and_persists_success_receipt` |
| 7 | Configured identity match uses only the bounded owner profile prefix | `tests/test_recruitment_session.py::test_script_inbound_configuration_binds_owner_and_honors_ephemeral_cap` |
| 8 | Foreign identity fails with a receipt before discovery fallback, including an approved reply/post packet | `tests/test_recruitment_e2e.py::test_real_script_configured_inbound_failure_persists_failed_receipt_and_never_falls_back` (identity case); `test_real_script_foreign_inbound_identity_blocks_approved_candidate` |
| 9 | Profile and conversation read failures fail closed with persisted receipts | The same parameterized E2E test for `profile` and `conversation`; `tests/test_recruitment_session.py::test_configured_inbound_read_failure_is_persisted_and_stops_discovery` |
| 10 | A wrong reply fingerprint cannot switch to another candidate | `tests/test_recruitment_e2e.py::test_real_script_reply_wrong_target_does_not_switch_candidate_or_mutate` |
| 11 | Private review precedes consent; declining causes zero mutation | `tests/test_recruitment_e2e.py::test_real_script_private_review_and_declined_consent_never_mutate` |
| 12 | Own-content can be reviewed/applied independently of engagement selection | `tests/test_recruitment_e2e.py::test_real_script_approved_own_content_is_independent_and_journaled_once`; `test_real_script_foreign_inbound_identity_blocks_approved_candidate`; `tests/test_nurture_own_content.py::test_discovery_candidate_and_own_content_are_decided_independently` |
| 13 | One approved reply/post uses the existing operation journal and stores no raw text | `tests/test_recruitment_e2e.py::test_real_script_approved_reply_uses_exact_review_and_real_operation_journal`; `test_real_script_approved_own_content_is_independent_and_journaled_once` |
| 14 | Ambiguous publish remains terminal through restart; interruption/restart and duplicate/cooldown are covered | `tests/test_recruitment_e2e.py::test_real_script_ambiguous_publish_is_persisted_and_never_replayed_after_restart`; `tests/test_recruitment_session.py::test_cancelled_observe_persists_interruption_and_can_restart`; `tests/test_nurture_durability.py::test_pending_and_ambiguous_targets_are_never_evicted`; `tests/test_nurture_own_content.py::test_same_account_content_apply_is_serialized_by_nurture_lock` |
| 15 | Same-account concurrency, synthetic filesystem failure, privacy, v1 compatibility, and installed-wheel script/package provenance | `tests/test_nurture_durability.py::test_same_account_lock_is_busy_and_different_accounts_are_independent`; `test_run_receipt_is_exact_and_updates_atomically`; `tests/test_cross_platform_state.py`; `tests/test_full_synthetic_e2e.py`; installed-wheel CI runs the complete suite and checks `site-packages` provenance. |

All evidence listed above is synthetic. No hosted result is claimed in this document; the Draft PR
records exact-head Windows/Linux locked QA, full tests, installed-wheel tests, and full-history
Gitleaks after the run completes.

## AO-01 foreground Auto Observe

`--watch-observe` is an additive, foreground-only mode. It is mutually exclusive with validate,
preflight, one-shot observe, review, and apply. The script calls one `asyncio.run`; that coroutine
reuses the same typed `RecruitmentSession.observe` path for each cycle. It does not spawn a CLI
process, construct a browser or mutation runtime, request review/consent, synthesize drafts, or
perform remote writes.

Before the first cycle, the watcher performs the existing read-only token identity and quota
preflight. A missing/expired credential, denied read, unknown/exhausted quota, invalid root/state,
timeout, or any failed cycle stops the watcher; there is no retry loop. Each preflight and observe
phase has a 5-minute timeout. The default interval is 30 minutes between cycle starts, and accepted
values are 15–1,440 minutes. `--max-cycles 1..1000` gives deterministic shutdown; omitting it keeps
the explicitly invoked foreground process running until Ctrl+C or a fail-closed stop. There is no
short-interval CLI override.

```powershell
# Syntax example only; this synthetic checkpoint does not authorize real-account polling.
uv run --locked python scripts/run_recruitment.py --account $accountAlias --watch-observe
uv run --locked python scripts/run_recruitment.py --account $accountAlias --watch-observe --interval-minutes 30 --max-cycles 3
```

The watcher prints only a bounded UTC timestamp, cycle number, receipt outcome/decision codes,
bounded counts, inbound capability status, and safe STOP code. `INBOUND_NOT_CONFIGURED` means the
inbound path was not requested; `INBOUND_READY` means the configured identity/profile/conversation
reads passed. An inbound error stops with `inbound=UNAVAILABLE`; it is never reported as zero
replies. No raw text, remote ID, username, URL, credential reference, or response body is printed.
The command creates no log file. Each completed Nurture cycle still writes its normal existing v1
receipt and dedupe state; these retain the existing store behavior and are not watch-specific logs.

For one watcher per account, the process acquires
`nurture/locks/<account-uuid>.watch.lock`, then each `observe` cycle uses the existing
`nurture/locks/<account-uuid>.lock`. Watch lock files remain after release and are never unlinked;
the OS releases the held process lock on exit. Separate account UUIDs have separate watcher locks.
The observe path does not take an operation-journal lock because it creates no mutation runtime.
If a process stops during a cycle, the existing Nurture receipt records `INTERRUPTED` (or is
repaired as interrupted on the next run). Restart is a fresh read and uses the existing v1 cooldown
state; it cannot replay a remote action because the watcher has no remote mutation path. Ctrl+C and
task cancellation report `INTERRUPTED` and return exit code 130.

Stop the watcher before using a separate private review command. Watch discovery is not durable
approval; review must fetch and verify the current target again. Existing `--validate`, `--preflight`,
one-shot `--observe`, private `--review`/`--apply-*`, Workflow, and read-only Recurrence semantics
remain unchanged. This synthetic implementation is not a real-account deployment or a 24/7
availability claim; issues #18 (operator installation/live UAT) and #2 (rights/provenance) remain
open.

Synthetic AO-01 evidence is in `tests/test_recruitment_watch.py` and the real-script cases in
`tests/test_recruitment_e2e.py`. The source entry script remains outside the wheel; wheel smoke
tests import the watch helper from the independently installed `threads_platform` package under
`site-packages`.

| AO-01 acceptance | Synthetic evidence |
| --- | --- |
| CLI help, mutually exclusive modes, interval limits/default, and finite cycles | `tests/test_recruitment_e2e.py::test_real_script_watch_help_and_mode_exclusivity`; `tests/test_recruitment_watch.py::test_watch_interval_and_cycle_bounds` |
| Three cycles run through one event loop with expected reads, v1 receipts, cooldown dedupe, and no mutation runtime | `tests/test_recruitment_e2e.py::test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop` |
| Inbound absent/configured/foreign identity and profile/conversation failures | `tests/test_recruitment_e2e.py::test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop`; `test_real_script_watch_reports_verified_inbound_capability_without_content`; `test_real_script_watch_checks_inbound_binding_and_stops_without_fallback`; `test_real_script_watch_fails_closed_on_read_errors` |
| Missing/expired/denied credentials, quota STOP, and discovery rate limit stop without retry | `tests/test_recruitment_e2e.py::test_real_script_watch_stops_on_preflight_authentication_failure`; `test_real_script_watch_stops_on_expired_credential_before_api_calls`; `test_real_script_watch_quota_stop_prevents_first_cycle`; `test_real_script_watch_fails_closed_on_read_errors`; `tests/test_recruitment_watch.py::test_preflight_stop_and_read_failure_never_retry` |
| Phase timeout and cancellation during read/between cycles; restart and per-account lock behavior | `tests/test_recruitment_watch.py::test_watch_phase_timeout_stops_without_fast_retry`; `test_cancel_between_cycles_reports_interruption_and_does_not_start_next_cycle`; `test_watch_lock_is_per_account_and_restart_keeps_lock_file`; `test_cancelled_read_releases_account_guard_and_other_account_progresses`; `tests/test_recruitment_e2e.py::test_watch_cancellation_persists_interrupted_receipt_and_allows_restart` |
| Missing root, malformed v1 state, synthetic path rejection/full disk, and privacy | `tests/test_recruitment_e2e.py::test_real_script_watch_missing_root_stops_without_creating_it`; `test_real_script_watch_malformed_v1_state_fails_closed`; `test_real_script_watch_synthetic_full_disk_failure_stops_safely`; `tests/test_recruitment_watch.py::test_watch_lock_path_error_fails_closed_without_cleanup`; `test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop` |
| The helper imports from an installed wheel while the entry script remains source-only | `tests/test_recruitment_watch.py::test_watch_module_comes_from_installed_wheel_in_wheel_smoke`; `tests/test_recruitment_e2e.py::test_real_script_validate_is_zero_write_and_proves_wheel_import_provenance`; Windows/Ubuntu installed-wheel CI. |
