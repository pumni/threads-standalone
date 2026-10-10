# Provenance and license decision

## Status

**File-level transfer audit: PASS for the listed SPLIT-02 through SPLIT-06 files. Public distribution/license policy: OPEN under `pumni/threads-standalone#2`.** The owner-directed engineering migration allows the bounded transfer recorded below; it is not a blanket relicensing or legal determination. No first-party LICENSE or SPDX marker is added.

## Audit method and limitations

The source was inspected at immutable commit `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`. For each source file below, the audit checked source history and GitHub commit author origin, file headers, repository-level license/notice files, and relevant third-party attribution/restrictions before copying or porting. Source commit history for the listed first-party files resolves to the repository owner's GitHub account; the files contain no copyright/SPDX header, and the source baseline has no first-party LICENSE, COPYING, or NOTICE. No personal emails or personal data are recorded here.

This history-based classification supports the coordinator's `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` category for this bounded engineering migration. It does not establish general public reuse rights or settle authorship facts outside the inspected repository history. The open license-policy issue remains a separate release blocker.

## First-party source paths transferred or adapted

| Frozen source path | Destination path | Classification | Treatment |
| --- | --- | --- | --- |
| `src/threads_platform/standalone/accounts.py` | `src/threads_platform/standalone/accounts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred account v1 store and path-safety behavior. |
| `src/threads_platform/standalone/__main__.py` | `src/threads_platform/standalone/__main__.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | SPLIT-02 ported account and API READ branches; later authorized checkpoints added browser and SPLIT-04 mutation commands in scoped changes. |
| `src/threads_platform/standalone/api.py` | `src/threads_platform/standalone/api.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported local read runtime and validation/error handling. |
| `src/threads_platform/standalone/app.py` | `src/threads_platform/standalone/app.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | SPLIT-02 composed local accounts and API reads; SPLIT-04 adds the mutation runtime using the local API client and credential resolver. |
| `src/threads_platform/application/ports/threads.py` | `src/threads_platform/api/contracts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | SPLIT-02 selected API-read DTOs, errors, protocols, and credential resolver contracts; SPLIT-04 adds media-container DTOs and mutation protocol methods. |
| `src/threads_platform/infrastructure/threads_api/client.py` | `src/threads_platform/api/client.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | SPLIT-02 ported HTTP reads and parsing/error helpers; SPLIT-04 adds container create/status/publish and reply-management methods. |
| `src/threads_platform/infrastructure/threads_api/environment_credentials.py` | `src/threads_platform/credentials/environment.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported exact `env://` validation and process-environment resolver; no secret persistence/cache. |
| `src/threads_platform/config/settings.py` | `src/threads_platform/config.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Reduced settings to the API base URL; distributed/database settings were not transferred. |
| `src/threads_platform/domain/discovery.py` | `src/threads_platform/api/contracts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Selected only the two API search enums. |
| `tests/unit/test_standalone_accounts.py` | `tests/test_accounts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic temporary-directory account tests. |
| `src/threads_platform/standalone/runtime.py` | `src/threads_platform/standalone/runtime.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported headed local login, account-isolated profile/lock paths, bounded feed/profile/thread reads and sanitized runtime errors. |
| `src/threads_platform/standalone/__main__.py` | `src/threads_platform/standalone/__main__.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Adapted only the browser command parser/dispatch/output; existing SPLIT-02 account and API branches remain. |
| `src/threads_platform/application/ports/browser.py` | `src/threads_platform/browser/contracts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported bounded browser errors, launch/network configuration and feed/profile/thread engine Protocols; omitted Worker surface and media-composer contracts. |
| `src/threads_platform/application/browser_capabilities.py` | `src/threads_platform/browser/capabilities.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Selected only versioned feed and target result DTOs; omitted capability policies, media DTOs, `domain.workers`, and distributed domain imports. |
| `src/threads_platform/application/browser_read_semantics.py` | `src/threads_platform/browser/semantics.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported pure profile/thread/feed normalization and bounded permalink parsing. |
| `src/threads_platform/infrastructure/browser/playwright_engine.py` | `src/threads_platform/browser/playwright_engine.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported persistent Chromium lifecycle, origin/redirect guard, and bounded feed/profile/thread recognition; omitted Worker UI inspection and all media-composer/upload methods. |
| `src/threads_platform/application/ports/process_lock.py` | `src/threads_platform/locks/process_lock.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported the local process-lock Protocol and collision error. |
| `src/threads_platform/infrastructure/local/process_lock.py` | `src/threads_platform/locks/filesystem.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported non-blocking Windows/POSIX file locking; removed Worker-specific wording from the error message. |

The ported behavior keeps attribution traceable through this mapping and the frozen source SHA. The destination Git history remains independent; no source commit history was rewritten into it.

## SPLIT-04 mutation transfer audit

The following paths were checked against frozen source history and file headers before transfer or adaptation. The inspected histories resolve to the `pumni` repository owner; the files have no copyright, SPDX, or third-party license header. The repository-level source check found no first-party LICENSE, COPYING, or NOTICE. The code is classified only as `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER`; this does not resolve the public license question tracked under destination issue #2.

| Frozen source path | Destination path | Classification | Treatment |
| --- | --- | --- | --- |
| `src/threads_platform/standalone/mutations.py` | `src/threads_platform/standalone/mutations.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred strict operation journal v1, local runtime, validation, lock, quota, publish/reply/media/carousel/quote-runtime/moderation, and ambiguous/no-retry behavior. Imports were mapped to destination account/API/lock modules. |
| `src/threads_platform/application/ports/threads.py` | `src/threads_platform/api/contracts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Added only `MediaContainerRequest`, `MediaContainer`, and the mutation methods to the existing destination API protocol; retained the read contracts already present. |
| `src/threads_platform/infrastructure/threads_api/client.py` | `src/threads_platform/api/client.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Adapted only container create/status/publish and reply moderation HTTP methods; the destination's existing API READ methods were retained. |
| `src/threads_platform/standalone/__main__.py` | `src/threads_platform/standalone/__main__.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Adapted only post, reply, image, video, carousel, moderation, and local operation-show CLI handlers/parser entries. No quote CLI, Nurture, Workflow, recurrence, or distributed commands were copied. |
| `tests/unit/test_standalone_mutations.py` | `tests/test_mutation_runtime.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred and import-adapted the owner's synthetic runtime/journal tests; credential and remote identifiers are explicit synthetic sentinels, and all state uses pytest temporary directories. |

The SPLIT-04 tests contain no real token, account record, live Threads ID/content, browser profile or cookie, journal from an operator machine, or private evidence. HTTP behavior is exercised through fake handlers only. This transfer does not grant a general license or change the separate owner decision on public distribution.

## SPLIT-05 Nurture transfer audit

The authorized source is `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; the destination branch starts at `pumni/threads-standalone@d28bb979d720ffc6b493e76e9709d80bc0709d5c`. Before transfer, each of the nine Nurture implementation files and ten Nurture test files below was checked with source `git log --follow`, header search, and repository notice/license inventory. Every inspected file history resolves to the repository owner `pumni`; no file has a copyright, SPDX, third-party license, or generated-code notice. The frozen source has no first-party `LICENSE`, `COPYING`, or `NOTICE`. This source-history classification follows only the coordinator's bounded owner-directed transfer authorization; it does not determine general reuse rights or settle the public distribution license tracked under issue #2.

| Frozen source path | Destination path | Classification | Treatment |
| --- | --- | --- | --- |
| `src/threads_platform/standalone/nurture.py` | `src/threads_platform/standalone/nurture.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved the strict v1 recruitment preset and deterministic policy. |
| `src/threads_platform/standalone/nurture_runner.py` | `src/threads_platform/standalone/nurture_runner.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved sequential observe/apply orchestration, receipts, and mutation boundary. |
| `src/threads_platform/standalone/nurture_discovery.py` | `src/threads_platform/standalone/nurture_discovery.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved bounded API discovery, normalization, ranking, dedupe, and cooldown rules. |
| `src/threads_platform/standalone/nurture_conversations.py` | `src/threads_platform/standalone/nurture_conversations.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved bounded owned-thread reply discovery and fail-closed ownership checks. |
| `src/threads_platform/standalone/nurture_store.py` | `src/threads_platform/standalone/nurture_store.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved local v1 receipts, target/content state, insights snapshots, lock and path safety. |
| `src/threads_platform/standalone/nurture_content.py` | `src/threads_platform/standalone/nurture_content.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved operator content validation and source/draft/content fingerprints. |
| `src/threads_platform/standalone/nurture_draft.py` | `src/threads_platform/standalone/nurture_draft.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved strict private reply-draft validation and redacted representations. |
| `src/threads_platform/standalone/nurture_quote_draft.py` | `src/threads_platform/standalone/nurture_quote_draft.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved strict private quote-draft validation and redacted representations. |
| `src/threads_platform/standalone/nurture_insights.py` | `src/threads_platform/standalone/nurture_insights.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Preserved explicit, bounded, journal-linked read-only insights refresh. |
| `tests/unit/test_standalone_nurture.py` | `tests/test_nurture.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic preset/policy tests. |
| `tests/unit/test_standalone_nurture_apply.py` | `tests/test_nurture_apply.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic reply/quote apply, reservation, ambiguity, and CLI tests. |
| `tests/unit/test_standalone_nurture_content.py` | `tests/test_nurture_content.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic content packet and fingerprint tests. |
| `tests/unit/test_standalone_nurture_conversations.py` | `tests/test_nurture_conversations.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic inbound conversation normalization tests. |
| `tests/unit/test_standalone_nurture_discovery.py` | `tests/test_nurture_discovery.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred bounded source, normalization, ranking and dedupe tests. |
| `tests/unit/test_standalone_nurture_durability.py` | `tests/test_nurture_durability.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic receipt/state durability, locking and recovery tests. |
| `tests/unit/test_standalone_nurture_insights.py` | `tests/test_nurture_insights.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic snapshot, journal-link, bound and score tests. |
| `tests/unit/test_standalone_nurture_own_content.py` | `tests/test_nurture_own_content.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic content provenance, apply, and cooldown tests. |
| `tests/unit/test_standalone_nurture_runner.py` | `tests/test_nurture_runner.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic observe, receipt, cancellation, output and no-mutation tests. |
| `tests/unit/test_standalone_nurture_runner_inbound.py` | `tests/test_nurture_runner_inbound.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic inbound priority and ownership tests. |

The imports in the transferred files were adapted only from monorepo API/process-lock owners to the already present standalone `api.contracts`, `api.client`, and `locks` modules. `tests/test_nurture_cli.py` is destination-authored and uses synthetic UUIDs, temporary roots, a fake API, and the existing local mutation journal. No third-party implementation, binary, or fixture was copied. The transferred first-party test fixtures contain only synthetic values; no live payload, account value, token, browser profile, local journal, or private evidence was copied. No dependency or license declaration was added. Issue #2 remains OPEN; no first-party `LICENSE` or SPDX marker is added.

The transferred tests contain only deterministic fake API values, synthetic account/remote identifiers, test-only text and temporary filesystem state. Source history and header checks do not prove a general public reuse license; only the bounded provenance classification above is recorded.

## SPLIT-06 WorkflowPlan and Recurrence transfer audit

Before copying, the source history, file headers, repository license/notice inventory, and third-party attribution/restriction markers were checked for each implementation and test source below at the frozen source SHA. The listed first-party histories resolve to the repository owner `pumni`; no file contains a copyright, SPDX, generated-code, or third-party implementation notice, and the source repository has no first-party LICENSE, COPYING, or NOTICE. The audit classification is limited to `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` under the coordinator authorization for #12. Public reuse rights remain undecided under issue #2.

| Frozen source path | Destination path | Classification | Treatment |
| --- | --- | --- | --- |
| `src/threads_platform/standalone/workflows.py` | `src/threads_platform/standalone/workflows.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported strict WorkflowPlan v1 parsing, bounded sequential execution, step errors, and the optional final `post_text` route. Imports were mapped to the existing destination API, browser, mutation, and runtime contracts; browser runtime construction remains conditional on browser steps. |
| `src/threads_platform/standalone/recurrences.py` | `src/threads_platform/standalone/recurrences.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported recurrence record/snapshot v1, local atomic persistence, path checks, process lock, fixed-anchor foreground runner, skip/no-replay behavior, and read-only snapshot validation. Imports were mapped to the destination locks and WorkflowPlan module. |
| `src/threads_platform/standalone/__main__.py` | `src/threads_platform/standalone/__main__.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Adapted only WorkflowPlan/Recurrence imports, command handlers, formatting, parser entries, and sanitized errors. Existing account, API, browser, mutation, and Nurture branches remain. |
| `tests/unit/test_standalone_workflows.py` | `tests/test_workflows.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred 19 synthetic Workflow test functions; API/browser/mutation dependencies map to destination contracts and fake runtimes. |
| `tests/unit/test_standalone_recurrences.py` | `tests/test_recurrences.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred 28 synthetic Recurrence test functions; state and clock use temporary directories and fake timing. |
| Selected Workflow and Recurrence CLI tests from `tests/unit/test_standalone_cli.py` | `tests/test_workflow_cli.py`, `tests/test_recurrence_cli.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred seven Workflow and three Recurrence CLI regression functions only. Destination imports were mapped; the API-only workflow regression now asserts the browser runtime is not constructed. Other source CLI tests and helpers were not copied. |

The destination-only tests and documentation use synthetic identifiers, test text, fake API/HTTP/browser adapters, temporary roots, and fake time. The new CI wheel smoke invokes fake Workflow reads, a fake final post, and recurrence create/list/show. It performs no live API request or mutation. No token, real account state, browser profile/cookie, local journal, private evidence, or private source data was accessed or copied. No third-party implementation, binary, dependency, or license declaration was added; `uv.lock` is unchanged. Issue #2 remains OPEN.

## Other source tests audited but not transferred

Other source test histories were inspected for origin/restrictions and behavior, but those files were not transferred: `tests/unit/test_standalone_api.py`, `tests/unit/test_standalone_app.py`, `tests/unit/test_standalone_runtime.py`, `tests/unit/test_browser_adapter.py`, `tests/unit/test_browser_capabilities.py`, `tests/unit/test_threads_api_contract.py`, `tests/unit/test_threads_discovery_contract.py`, `tests/unit/test_threads_credential_secrets.py`, `tests/unit/test_threads_httpx2_transport.py`, and `tests/unit/test_standalone_architecture.py`. Only the specific Workflow and Recurrence CLI functions listed in the SPLIT-06 audit table were selected from `test_standalone_cli.py`; the mixed source CLI file was not copied. No Worker fixture or live-evidence test was transferred.

## Third-party dependency review

No third-party source implementation or test fixture is copied. Direct runtime dependencies remain separately licensed upstream packages: [`httpx2` (BSD-3-Clause)](https://github.com/pydantic/httpx2/blob/main/LICENSE.md), [`pydantic` (MIT)](https://github.com/pydantic/pydantic/blob/main/LICENSE), [`pydantic-settings` (MIT)](https://github.com/pydantic/pydantic-settings/blob/main/LICENSE), and [`Playwright 1.63.0` (Apache-2.0)](https://github.com/microsoft/playwright-python/blob/v1.63.0/LICENSE). Playwright is declared as a dependency, not vendored; no Playwright implementation or Chromium binary is copied into this repository. These dependency licenses do not select a license for this project. The lockfile must remain limited to the standalone package's API/browser dependencies and the existing development tools.

No live Threads payload, credential, local account data, browser profile/cookie store, operation journal, private evidence, or personal email was accessed, used in this audit, or included in tests. All API tests use synthetic values and mock HTTP; browser tests use fake Playwright objects and synthetic temporary profiles.
