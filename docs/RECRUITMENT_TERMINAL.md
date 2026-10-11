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

## AO-02 stability and synthetic demo

Run the source-only synthetic demo with one command from the repository root:

```powershell
uv run --locked python scripts/demo_recruitment_watch.py --cycles 3
```

`--cycles` defaults to 3 and accepts 1–1,000. The demo creates one fresh temporary root, adds an
account with no credential reference, and replaces the RecruitmentSession app factory for the
duration of the run with a fake identity/quota/search API. The fake factory accepts only API reads;
it rejects mutation or browser runtimes, and the fake runtime has no mutation methods. The temporary
root is resolved to its canonical path for NurtureStore validation and is removed on exit. The demo
does not inspect `THREADS_LOCAL_DATA_ROOT`, read a token from the environment, or support a live
backend switch. If the fake factory were bypassed, the credentialless account fails before a live
API request can be authenticated.

The fake clock advances by the production minimum interval of 15 minutes without waiting. This does
not lower or change the minimum interval accepted by `scripts/run_recruitment.py --watch-observe`.
The synthetic identity, the repeated candidate, and its text are never printed or stored in the v1
receipt/state; only bounded counts and decision codes appear. `credential=verified` in the preflight
line means the fake identity responded, while the demo start/safety lines declare that no credential
was configured.

Example from `--cycles 3` (receipt UUIDs and temporary root are intentionally omitted):

```text
demo start backend=FAKE identity=FAKE cycles=3 credential_source=NONE data_root=TEMPORARY interval_minutes=15 sleeper=FAKE
watch preflight status=OBSERVED credential=verified post_quota=available reply_quota=available
watch cycle=1 at=2026-10-11T00:00:00+00:00 status=SUCCESS outcome=SUCCESS discovered=1 deduped=1 selected=1 skipped=0 decisions=OBSERVE_ONLY inbound=INBOUND_NOT_CONFIGURED
watch cycle=2 at=2026-10-11T00:15:00+00:00 status=SUCCESS outcome=SUCCESS discovered=1 deduped=1 selected=0 skipped=1 decisions=NO_ACTION inbound=INBOUND_NOT_CONFIGURED
watch cycle=3 at=2026-10-11T00:30:00+00:00 status=SUCCESS outcome=SUCCESS discovered=1 deduped=1 selected=0 skipped=1 decisions=NO_ACTION inbound=INBOUND_NOT_CONFIGURED
watch shutdown status=COMPLETED cycles=3
demo storage cycles=3 receipts=3 receipt_bytes=1707 target_state_bytes=530 fake_wait_seconds=1800
demo safety mutation_runtime=DISABLED remote_mutations=0 credentials=NONE
```

`discovered` is the candidate count before deduplication; `deduped` is the current run's unique
candidate count. In the example, the previously seen candidate remains visible in the counts but is
not selected again during its cooldown. The demo runs the same `RecruitmentSession.watch_observe`,
`RecruitmentSession.observe`, `NurtureRunner`, and `NurtureStore` receipt/state path as the
foreground watcher.

The watcher holds one account-scoped process lock for the whole foreground run, and Nurture keeps
its per-cycle account lock. A timeout, failed read, unsafe state, invalid output, or preflight STOP
ends the watcher without a retry loop. Ctrl+C and task cancellation during a read or fake/real sleep
release the watcher lock; a cancelled Nurture run has its existing v1 `INTERRUPTED` receipt and can
be restarted. The demo is finite and does not install a daemon or scheduler.

Stability evidence samples `tracemalloc` retained bytes at 3, 10, 100, and 1,000 coordinator cycles.
The process loop keeps a scalar cycle count and the current observation; it does not retain prior
cycle payloads. Separate end-to-end synthetic runs at 3, 10, and 100 cycles report completed
receipt count/bytes and target-state bytes. Nurture persists one v1 receipt per cycle, so receipt
files and their total bytes grow linearly; AO-02 does not prune or change v1 retention semantics.
The repeated target state stays one record in these measurements. These measurements characterize
the synthetic run and do not claim an RSS bound or long-term disk-retention policy.

| Synthetic cycles | Completed receipts | Receipt bytes | Target-state bytes |
| ---: | ---: | ---: | ---: |
| 3 | 3 | 1,707 | 530 |
| 10 | 10 | 5,683 | 530 |
| 100 | 100 | 56,803 | 530 |

`scripts/demo_recruitment_watch.py` and `scripts/run_recruitment.py` are source repository scripts,
not wheel entry points. `threads_platform.standalone.recruitment_watch` and the session/Nurture code
are in the installed wheel; installed-wheel CI verifies their `site-packages` import provenance.
This demonstration does not verify real credentials, API behavior, operator filesystem permissions,
or real Threads uptime. Issues #2 and #18 remain open, and RS-06 (#25) remains HOLD. Production
`--validate`, `--preflight`, `--observe`, `--review`, and `--apply-*` modes, v1 state, Workflow's
terminal mutation rule, and READ-only Recurrence are unchanged.

| AO-02 stability scenario | Synthetic evidence |
| --- | --- |
| 1. Three, ten, one hundred, and many watch cycles use fake time | `tests/test_recruitment_watch_stability.py::test_demo_reports_receipt_and_target_state_growth_by_cycle_count`; `test_watch_coordinator_retained_tracemalloc_stays_bounded_for_1000_cycles` |
| 2. Repeated candidates obey dedupe/cooldown | The one-command demo test; `tests/test_recruitment_e2e.py::test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop` |
| 3. No cycle payload accumulation; receipt/state bytes are measured by cycle count | `tests/test_recruitment_watch_stability.py::test_watch_coordinator_retained_tracemalloc_stays_bounded_for_1000_cycles`; `test_demo_reports_receipt_and_target_state_growth_by_cycle_count` |
| 4. Rate limit, timeout, expired credential, and unverified quota fail closed without busy-loop | `tests/test_recruitment_watch_stability.py::test_read_failures_stop_once_without_busy_loop`; `tests/test_recruitment_watch.py::test_watch_phase_timeout_stops_without_fast_retry`; E2E expired-credential and quota-STOP cases |
| 5. Disconnect/read failure during a later cycle stops without retry | `tests/test_recruitment_watch_stability.py::test_read_failure_on_later_cycle_stops_without_retry_or_catch_up`; `tests/test_recruitment_e2e.py::test_real_script_watch_fails_closed_on_read_errors` |
| 6. Ctrl+C during sleep and cancellation during observe | `tests/test_recruitment_watch_stability.py::test_ctrl_c_or_cancellation_stops_and_releases_watch_lock`; `tests/test_recruitment_watch.py::test_cancel_between_cycles_reports_interruption_and_does_not_start_next_cycle`; `test_cancelled_read_releases_account_guard_and_other_account_progresses` |
| 7. Restart after interrupted receipt preserves old state and does not replay writes | `tests/test_recruitment_e2e.py::test_watch_cancellation_persists_interrupted_receipt_and_allows_restart`; `tests/test_recruitment_session.py::test_cancelled_observe_persists_interruption_and_can_restart` |
| 8. Concurrent same-account watchers allow only one owner | `tests/test_recruitment_watch.py::test_watch_lock_is_per_account_and_restart_keeps_lock_file`; `test_cancelled_read_releases_account_guard_and_other_account_progresses` |
| 9. Different accounts progress independently | `tests/test_recruitment_watch.py::test_cancelled_read_releases_account_guard_and_other_account_progresses`; `tests/test_recruitment_session.py::test_account_lock_contention_isolated_to_that_account` |
| 10. Corrupt, missing, symlink/reparse, permission, and full-disk cases stop safely using temporary-root/injected failures | `tests/test_recruitment_e2e.py::test_real_script_watch_missing_root_stops_without_creating_it`; `test_real_script_watch_malformed_v1_state_fails_closed`; `test_real_script_watch_synthetic_full_disk_failure_stops_safely`; `tests/test_cross_platform_state.py::test_account_state_symlink_to_outside_root_fails_closed`; `tests/test_recruitment_watch.py::test_watch_lock_path_error_fails_closed_without_cleanup` |
| 11. Preflight STOP prevents the first observation | `tests/test_recruitment_watch.py::test_preflight_stop_and_read_failure_never_retry`; `tests/test_recruitment_e2e.py::test_real_script_watch_quota_stop_prevents_first_cycle` |
| 12. No mutation runtime, POST, AI draft, or synthetic approval in the demo | `tests/test_recruitment_watch_stability.py::test_demo_is_a_one_command_synthetic_read_only_run`; `tests/test_recruitment_e2e.py::test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop` |
| 13. Token, identity, target, content, response, and path do not leak to terminal output | The one-command demo test; `tests/test_recruitment_e2e.py::test_real_script_watch_runs_three_read_only_cycles_in_one_event_loop`; `tests/test_recruitment_watch.py::test_watch_lock_path_error_fails_closed_without_cleanup` |
| 14. Existing production CLI modes remain mutually exclusive and unchanged | `tests/test_recruitment_e2e.py::test_real_script_watch_help_and_mode_exclusivity`; `tests/test_recruitment_session.py::test_cli_requires_exactly_one_session_or_apply_mode` |
| 15. v1 schema, Workflow terminal mutation rule, READ-only Recurrence, and source/wheel provenance remain intact | Full synthetic CI suite; `tests/test_recruitment_watch.py::test_watch_module_comes_from_installed_wheel_in_wheel_smoke`; `tests/test_recruitment_e2e.py::test_real_script_validate_is_zero_write_and_proves_wheel_import_provenance`; Windows/Ubuntu installed-wheel tests. |

## AO-03 private recruitment shortlist

Run one fresh bounded discovery pass and review a candidate in the foreground terminal:

```powershell
uv run --locked python scripts/run_recruitment.py --account ALIAS --shortlist
```

The command requires an interactive private terminal: stdin, stdout, and stderr must all be TTYs,
and CI mode is rejected before account state or the API is opened. The shortlist and selected
preview are written to the private terminal's stderr stream; stdout stays empty. Enter, EOF, an
invalid selection, or Ctrl+C exits without creating a draft or sending an action. The CLI offers no
POST, REPLY, APPLY, or approval choice. It displays at most five candidates; the internal typed
API caps its requested limit at 10.

Each row shows the discovery source, the existing Nurture score and reason codes, eligibility or
suppression status, and a suggested next step. `ELIGIBLE — REVIEW_CANDIDATE` only means the item
passed current observation policy; `COOLDOWN`, `PENDING`, `CONFIRMED`, `AMBIGUOUS`, and
`NOT_ELIGIBLE` are read-only and show `OBSERVE_ONLY`. A numeric selection prints one ephemeral
preview with source, score, reason codes, status, and at most 500 characters of content. Control
characters are escaped before terminal output. No remote ID, author username, permalink, or
credential is included in the preview.

The shortlist runs a new discovery pass through the existing inbound and Nurture discovery/ranking
policy. It does not reconstruct content from Auto Observe receipts: those receipts hold only the
existing bounded decision/count state. The candidate and its bounded preview remain in memory for
this process and are not written to a receipt or v1 target state. The per-account Nurture lock file
may be created and remains after release. Acquiring that lock runs the existing stale-run repair,
which may mark an interrupted receipt and move it to the completed area. Shortlisting reads existing
target state and creates no shortlist receipt, reply draft, approval token, or candidate-content
record. A later reply workflow would still have to revalidate the exact target.

If inbound is configured, both `--inbound-owner-username USERNAME` and
`--inbound-thread-cap N` must be supplied on this invocation. Owner binding and profile/conversation
reads fail closed before discovery fallback. With no inbound configuration, the existing discovery
path continues and the output says `INBOUND_NOT_CONFIGURED`; this does not claim that there are no
messages.

For a synthetic demonstration, run one command from the repository checkout:

```powershell
uv run --locked python scripts/demo_recruitment_shortlist.py
```

It creates a fresh temporary root and credentialless synthetic account, installs a fake-only API
factory for the demo call, and supplies one eligible candidate, one cooldown candidate, and one
excluded candidate. The optional `--select N` chooses which synthetic row to preview. The fake
runtime has no mutation methods and its factory rejects mutation or browser runtimes. The demo has
no path to a live backend, does not read `THREADS_LOCAL_DATA_ROOT` or credentials, and prints only
synthetic content. It is a source-repository script, not a wheel entry point. The session, shortlist,
Nurture discovery, store, and policy modules are shipped in the wheel; installed-wheel CI checks
their import provenance from `site-packages` outside the checkout.

`--validate`, `--preflight`, `--observe`, and `--watch-observe` remain safe-summary modes and do not
print candidate content. `--shortlist` is the private interactive content review mode. Synthetic
tests cover ranking reuse and duplicate handling, cooldown/action states, bounded output and privacy,
private-terminal gating and exit input, account isolation, configured inbound failures, mode
exclusivity, and the one-command fake demo. No real Threads API, credential, operator root, live
terminal ACL, rights/provenance, or real-account behavior is verified here. Issues #2 and #18 remain
open; RS-06 (#25) remains HOLD. AO-03 adds no AI, automatic posting, or automatic replies.

| AO-03 acceptance | Synthetic evidence |
| --- | --- |
| Existing score/reason ranking, source precedence, duplicate removal, cooldown and ineligible states | `tests/test_recruitment_shortlist.py::test_shortlist_uses_existing_ranking_and_dedupes_api_source_duplicates`; `test_shortlist_explains_policy_and_durable_target_states_without_actionability` |
| Configured inbound owner mismatch/profile/conversation failure stops without discovery fallback; no-config path remains available | `tests/test_recruitment_shortlist.py::test_configured_inbound_failures_stop_before_discovery_fallback`; `test_no_configured_inbound_keeps_legacy_discovery_path`; `test_inbound_configuration_needs_owner_and_cap_and_no_config_does_not_claim_ready` |
| Bounded private preview, no candidate/draft/approval persistence, account isolation, and mutation runtime disabled | `tests/test_recruitment_shortlist.py::test_shortlist_bounds_items_and_preview_and_hides_private_fields_from_repr`; `test_shortlist_uses_account_scoped_state_and_writes_no_candidate_or_receipt`; `test_cli_shortlist_requires_private_tty_and_selection_stays_preview_only` |
| Redirected/CI output refusal, selection validation, EOF, Ctrl+C, and existing mode exclusivity | `tests/test_recruitment_shortlist.py::test_redirected_output_or_ci_rejects_shortlist_before_api_or_lock`; `test_eof_invalid_input_and_ctrl_c_exit_without_action`; `test_shortlist_mode_is_mutually_exclusive_with_existing_modes` |
| One-command fake demo, source-only script boundary, and independent wheel import provenance | `tests/test_recruitment_shortlist.py::test_demo_source_script_runs_one_command_with_fake_data_only`; Windows/Ubuntu full and installed-wheel CI. |

## AO-04A synthetic reply proposal preview

Run the offline proposal demonstration from the source checkout with one command:

```powershell
uv run --locked python scripts/demo_recruitment_ai_draft.py
uv run --locked python scripts/demo_recruitment_ai_draft.py --select 2
```

The script creates a new temporary root and credentialless synthetic account, then runs the existing
Nurture ranking and eligibility policy through a fake read-only API. Its two distinct eligible
fixtures have context-specific static fake replies, so the selected context and reply visibly match.
Candidate numbers follow the existing ranked shortlist order; the demo does not re-rank candidates.
With no option, candidate 1 is selected. `--select 2` selects candidate 2. Other values and
non-numeric input are rejected before the temporary root or fake API is opened. The option exists
only on this source-only demo; the production CLI is unchanged.

The output places the selected `SYNTHETIC` context beside its fake reply. Context and reply are each
limited to 500 characters and total demo output is capped at 2,048 characters. The proposal remains
`PROPOSED_FOR_HUMAN_REVIEW`, `NOT_APPROVED`, and `NOT_SENT`; the demonstration ends `DISCARDED` with
`candidate_or_reply_persistence=0` and `remote_mutations=0`. Before printing, the demo checks its own
temporary root for candidate context, fingerprint, remote ID, or reply content. The fake API exposes
only read methods and has no mutation runtime. No provider, network, credential, apply, or approval
option exists. The script is in the source repository and is not a wheel entry point.

The proposal API accepts only the existing typed shortlist and a one-based selection. It binds the
proposal to the exact selected candidate object and fingerprint, and rejects every status except
`ELIGIBLE` with `REVIEW_CANDIDATE`. Candidate text is untrusted input. The proposal module's fake
generator is deterministic and ignores that text. The demo's separate fake generator maps its two
fixed synthetic contexts to two fixed replies; it cannot contact a model or network. Generated output
is rejected if empty, over 500 characters, whitespace-padded, or contains terminal control/format
characters. Timeout and generator failures produce bounded error codes, and cancellation propagates.
This synthetic behavior is not evidence that a real AI provider is safe or authorized.

The selected candidate and proposal exist only in process memory. The proposal is not converted to
`NurtureDraft(source="OPERATOR_FILE")`, does not create an approval token, and is not written to
Nurture v1 state/receipts, the operation journal, public logs, or any durable inbox. The demo's
temporary root may contain its synthetic account and the existing shortlist lock while it runs;
the temporary directory is removed on exit. The production `--shortlist` command remains a private
human review of fresh discovery; it does not call this generator or add an AI mode. A later send
workflow would require separate authorization and exact-target revalidation.

| AO-04A acceptance | Synthetic evidence |
| --- | --- |
| Only `ELIGIBLE` / `REVIEW_CANDIDATE` can generate; cooldown, pending, confirmed, ambiguous, and ineligible states stop before generation | `tests/test_recruitment_reply_proposal.py::test_non_eligible_candidate_states_cannot_generate_proposal` |
| Selection binds the exact candidate and target fingerprint; candidate replacement fails | `test_only_selected_eligible_candidate_is_exactly_bound_to_preview` |
| Reply is fake-only, deterministic, bounded, and untrusted candidate instructions are not executed | `test_fake_generator_is_deterministic_and_treats_candidate_text_as_untrusted`; `test_invalid_or_oversized_generator_output_is_rejected_without_truncation` |
| Timeout, generator failure, and cancellation fail safely without exposing exception data | `test_timeout_and_generator_exception_are_bounded_and_sanitized`; `test_cancellation_propagates_without_creating_a_proposal` |
| Repr and errors redact target/content; proposal is ephemeral and causes no persistence or mutation | `test_repr_and_failures_hide_context_reply_and_target_fingerprint`; `test_proposal_path_has_no_persistence_or_mutation_side_effects` |
| Existing production CLI modes remain intact with no AI/apply-proposal mode | `test_production_cli_keeps_existing_modes_and_has_no_ai_or_apply_proposal_mode` |
| Demo selection 1/2 displays only the matching synthetic context/reply; output bounds, invalid-selection rejection, no persistence/mutation | `test_demo_source_script_runs_one_command_with_fake_generator_only` |
| Installed-wheel proposal-module provenance; source-only demo is explicitly skipped in wheel suite | `test_new_proposal_module_imports_from_installed_wheel_when_requested`; Windows/Ubuntu installed-wheel CI. |

The package module is included in the installed wheel and its wheel suite checks `site-packages`
provenance. The demonstration script remains source-only; its subprocess test runs in source-checkout
CI and is skipped only in the independent installed-wheel suite. No real AI call, Threads/Meta API,
operator root, credential, approval, post, reply, quote, follow, or DM is exercised. Content rights
and provenance issue #2 and real installation/live UAT issue #18 remain open; RS-06 issue #25 remains
HOLD. AO-04A does not add AI-assisted production drafting or unattended publishing.

## AO-04B synthetic human reply editor

The source-only fake editor can be opened in a private foreground terminal for either synthetic
candidate:

```powershell
uv run --locked python scripts/demo_recruitment_ai_draft.py --interactive --select 1
uv run --locked python scripts/demo_recruitment_ai_draft.py --interactive --select 2
```

It reuses the AO-04A fake API, ranked shortlist, `HumanReplyPreview`, and deterministic fake
generator. The selected synthetic context and current fake or edited reply are shown together in a
labelled preview. Press `E` to enter one bounded line (1–500 characters), `K` to keep the current
text in this process session, or `D` to discard it. Edits can be repeated up to five times. Blank
input, EOF, Ctrl+C, invalid actions, overlong input, or invalid reply text fail closed to discard.
Invalid private text is not echoed by the application or included in an error.

`--interactive` requires stdin, stdout, and stderr to be TTYs and rejects CI/test environments
before the demo creates its synthetic account root or constructs its fake API. The editor does not
accept reply text through arguments or environment variables. For noninteractive CI, the source-only
script has a fixed-input fake path:

```powershell
uv run --locked python scripts/demo_recruitment_ai_draft.py --scripted --select 1
uv run --locked python scripts/demo_recruitment_ai_draft.py --scripted --select 2
```

The scripted path uses only hard-coded synthetic edits. It is a demo/test facility, does not bypass
any production CLI TTY guard, and adds no `--shortlist`, `--review`, or `--apply-*` mode.

The final display uses separate fields: `EDITORIAL_DISPOSITION=KEPT_FOR_SESSION|DISCARDED`,
`APPROVAL_STATUS=NOT_APPROVED_FOR_SENDING`, `DELIVERY_STATUS=NOT_SENT`,
`DRAFT_PERSISTED=NO`, and `remote_mutations=0`. Keeping means only an in-memory editorial choice;
it is not consent, an approval token, a saved draft, or a send action. The editor preserves the
original candidate object and fingerprint and does not convert the fake proposal into
`NurtureDraft(source="OPERATOR_FILE")`. Edited text, candidate content, selection, and disposition
are not written to receipts, state, journal, or durable files. The demo may briefly create its
synthetic account and existing shortlist lock in a new temporary root; that root is removed at exit.

| AO-04B acceptance | Synthetic evidence |
| --- | --- |
| Exact candidate identity/fingerprint survives repeated edits; target replacement fails | `tests/test_recruitment_reply_proposal.py::test_editor_edits_preserve_exact_candidate_and_fingerprint` |
| Only existing eligible proposals can enter the editor; cooldown/pending/confirmed/ambiguous/excluded still stop | `test_non_eligible_candidate_states_cannot_generate_proposal`; `HumanReplyEditorialSession.begin` requires a valid bound `HumanReplyPreview` |
| Empty, blank, padded, overlong, control, bidi, and invalid type edits fail without trim or echo | `test_invalid_edited_reply_is_rejected_without_trimming_or_echoing` |
| Keep/discard are ephemeral editorial outcomes, never approval or delivery | `test_editor_keep_and_discard_are_not_approval_or_delivery`; `test_editor_scripted_input_keep_discard_cancel_and_invalid_are_safe` |
| TTY/CI rejection happens before demo setup; bounded input, EOF, invalid action, and Ctrl+C discard | `test_interactive_demo_refuses_redirected_and_ci_before_setup`; `test_editor_scripted_input_keep_discard_cancel_and_invalid_are_safe` |
| A/B scripted demo shows only the selected context and matching edited reply, with no persisted draft or mutation | `test_demo_source_script_runs_one_command_with_fake_generator_only`; source-checkout Windows/Ubuntu CI |
| Existing production modes, proposal privacy, and independent wheel import remain intact | `test_production_cli_keeps_existing_modes_and_has_no_ai_or_apply_proposal_mode`; `test_repr_and_failures_hide_context_reply_and_target_fingerprint`; installed-wheel CI |

The editor contract is part of the installed package and is verified from `site-packages`. The demo
script remains source-only; its subprocess, input-loop, persistence, and TTY tests are skipped only
in the independent installed-wheel suite. No real AI/provider, Threads/Meta API, account,
credentials, or remote mutation is used. Rights/provenance issue #2 and live install/UAT issue #18
remain OPEN; RS-06 issue #25 remains HOLD.

## AO-04C synthetic reply quality advisory

The existing source-only scripted demo now shows a nonbinding heuristic warning, its update after a
more informative edit, and the session-only keep decision. Run it with synthetic candidate 1:

```powershell
uv run --locked python scripts/demo_recruitment_ai_draft.py --scripted --select 1
```

The scripted edit sequence starts with `i dont know`, which produces
`EDITORIAL_QUALITY=REVIEW_RECOMMENDED` and `QUALITY_FLAGS=LOW_INFORMATION`. It then enters a longer
synthetic reply, after which the preview reports `NO_OBVIOUS_ISSUE_DETECTED` and `QUALITY_FLAGS=NONE`.
The editor still allows `K`; keeping remains `KEPT_FOR_SESSION`, `NOT_APPROVED_FOR_SENDING`,
`NOT_SENT`, `DRAFT_PERSISTED=NO`, and `remote_mutations=0`.

The evaluator is a small deterministic English phrase check, not a spelling, relevance, safety,
correctness, or factuality checker. A short useful answer may be flagged, while a long vague,
incorrect, harmful, or off-topic answer may not be. `NO_OBVIOUS_ISSUE_DETECTED` is not evidence that
a reply is good, correct, approved, or publishable. The advisory never rewrites the reply or blocks
keeping it for the current process session. It creates no approval token, state, receipt, journal
entry, or saved draft. The interactive path retains the private-TTY guard; scripted output contains
only hard-coded synthetic data.

| AO-04C acceptance | Synthetic evidence |
| --- | --- |
| Known fake reply has neutral wording; `i dont know` flags low information; an informative edit updates the advisory | `tests/test_recruitment_reply_proposal.py::test_synthetic_quality_advisory_updates_without_claiming_quality_or_approval`; `test_demo_source_script_runs_one_command_with_fake_generator_only` |
| Advisory has bounded safe codes and hides candidate, reply, and fingerprint in repr | `test_synthetic_quality_advisory_updates_without_claiming_quality_or_approval`; installed-wheel suite |
| Warning appears before the keep action; keeping remains session-only and nonbinding | `test_editor_quality_warning_precedes_nonblocking_flagged_keep`; `test_demo_source_script_runs_one_command_with_fake_generator_only` |
| Existing TTY, editor, proposal, production CLI, persistence, and mutation boundaries remain intact | `tests/test_recruitment_reply_proposal.py`; source-checkout Windows/Ubuntu CI |

## AO-05A synthetic foreground journey

Run the complete synthetic journey in one process with:

```powershell
uv run --locked python scripts/demo_recruitment_journey.py --scripted
```

The interactive variant is available only in a private foreground PowerShell terminal:

```powershell
uv run --locked python scripts/demo_recruitment_journey.py --interactive
```

The interactive command rejects CI, pytest, and streams without a TTY before creating the
temporary account root or fake API. It performs one existing foreground watch cycle with a fake
clock and no real wait, then calls the existing fresh shortlist policy against the same fake API,
account, temporary root, and Nurture v1 state. Before it offers F, it requires the fresh shortlist
to contain exactly one synthetic W with `COOLDOWN / OBSERVE_ONLY`; a missing, duplicate, or
reclassified W stops the journey. New synthetic candidate F is selected only when the fresh policy
returns `ELIGIBLE / REVIEW_CANDIDATE`. Shortlist timestamps use the injected fixed synthetic clock;
fake discovery timestamps are aligned to that clock while the W fingerprint remains bound to its
stable synthetic remote ID. The demo carries the same candidate object and fingerprint through
`create_human_reply_preview` and the existing `HumanReplyEditorialSession` and quality advisory.

In interactive mode choose the displayed candidate number, then use `E` to edit, `K` to keep in
the current process session, or `D` to discard. No eligible F, a W or invalid selection, target
change, blank selection, EOF, or Ctrl+C during candidate selection reports `JOURNEY_STATUS=STOP`
and exits nonzero before proposal generation. After a valid proposal reaches the editor, `D` is a
completed discard; it still ends `NOT_APPROVED_FOR_SENDING` and `NOT_SENT`. Invalid editor input
discards safely. The scripted path uses only fixed synthetic input;
it edits a deliberately low-information answer, displays the updated advisory, and keeps the
answer in memory until the process exits.

Watch may write its existing synthetic run receipt, Nurture v1 target state, and lock files inside
the temporary demo root. The fresh shortlist may create its existing account lock there. The demo
reports receipt/state byte counts without printing paths. Candidate text, proposals, edited replies,
quality advisories, and editorial disposition are never persisted. All paths end with
`NOT_APPROVED_FOR_SENDING`, `NOT_SENT`, `DRAFT_PERSISTED=NO`, and `remote_mutations=0`. Keeping the
answer is not consent, an approval token, or a send action.

| AO-05A acceptance | Synthetic evidence |
| --- | --- |
| Fake watch completes without credential or wall-clock waiting and writes the existing receipt/state | `tests/test_recruitment_journey.py::test_watch_cooldown_blocks_w_and_fresh_candidate_stays_exactly_bound`; one-command source demo |
| Fresh shortlist requires exactly one W with `COOLDOWN / OBSERVE_ONLY`, offers F only when eligible, and returns STOP/nonzero when no F exists | `test_watch_cooldown_blocks_w_and_fresh_candidate_stays_exactly_bound`; `test_watched_candidate_cooldown_must_be_present_exactly_once`; `test_no_eligible_candidate_never_calls_fake_generator`; `test_no_eligible_candidate_is_nonzero_at_script_entrypoint` |
| Wrong/ineligible selection, target replacement, and fingerprint change return STOP before proposal | `test_displayed_fingerprint_detects_replaced_or_changed_target`; `test_journey_stops_if_selected_target_changes_before_proposal`; `test_ctrl_c_and_invalid_selection_never_create_proposal`; `test_cancelled_or_out_of_range_selection_stops_before_proposal`; `test_ctrl_c_during_candidate_selection_stops_before_proposal`; `test_ineligible_or_invalid_selection_is_nonzero_at_script_entrypoint` |
| Fresh shortlist receives the injected fixed synthetic time, not wall clock | `test_watch_cooldown_blocks_w_and_fresh_candidate_stays_exactly_bound` |
| Preflight, watch read, and fresh shortlist failures stop without later proposal work | `test_watch_preflight_or_fresh_shortlist_failure_stops_safely` |
| Edit, quality advisory, keep, discard, cancel, invalid selection, and no persistence/mutation | `test_interactive_keep_discard_and_quality_advisory_are_session_only`; `test_ctrl_c_and_invalid_selection_never_create_proposal`; source demo persistence guard |
| Interactive CI, GITHUB_ACTIONS, pytest, and redirected-stream rejection precedes setup | `test_interactive_guard_rejects_unsafe_context_before_root_account_or_api` |
| AO-01 through AO-04C production and package contracts remain covered | Existing recruitment session, watch stability, shortlist, and reply proposal suites; hosted full Windows/Ubuntu suite |

The journey demo is source-only and is not part of the installed wheel. Its interactive output is
for the private terminal only; scripted output contains hard-coded synthetic data and is intended
for CI. It adds no production CLI mode, ranking/cooldown policy, schema, journal, approval token,
AI call, remote mutation, scheduler, or daemon. Threads/Meta behavior and live-account UAT remain
unverified.
