# Provenance and license decision

## Status

**File-level transfer audit: PASS for the listed SPLIT-02, SPLIT-03, and SPLIT-04 files. Public distribution/license policy: OPEN under `pumni/threads-standalone#2`.** The owner-directed engineering migration allows the bounded transfer recorded below; it is not a blanket relicensing or legal determination. No first-party LICENSE or SPDX marker is added.

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

## Source tests audited but not transferred

The following source test histories were inspected for origin/restrictions and behavior; destination coverage was written as targeted synthetic tests rather than copying these files: `tests/unit/test_standalone_api.py`, `tests/unit/test_standalone_cli.py`, `tests/unit/test_standalone_app.py`, `tests/unit/test_standalone_runtime.py`, `tests/unit/test_browser_adapter.py`, `tests/unit/test_browser_capabilities.py`, `tests/unit/test_threads_api_contract.py`, `tests/unit/test_threads_discovery_contract.py`, `tests/unit/test_threads_credential_secrets.py`, `tests/unit/test_threads_httpx2_transport.py`, and `tests/unit/test_standalone_architecture.py`. The SPLIT-04 mutation runtime test file listed above is the only transferred source test in this checkpoint; other CLI, API, and architecture coverage is destination-authored and uses synthetic values and fake transports. No Worker fixture or live-evidence test was transferred.

## Third-party dependency review

No third-party source implementation or test fixture is copied. Direct runtime dependencies remain separately licensed upstream packages: [`httpx2` (BSD-3-Clause)](https://github.com/pydantic/httpx2/blob/main/LICENSE.md), [`pydantic` (MIT)](https://github.com/pydantic/pydantic/blob/main/LICENSE), [`pydantic-settings` (MIT)](https://github.com/pydantic/pydantic-settings/blob/main/LICENSE), and [`Playwright 1.63.0` (Apache-2.0)](https://github.com/microsoft/playwright-python/blob/v1.63.0/LICENSE). Playwright is declared as a dependency, not vendored; no Playwright implementation or Chromium binary is copied into this repository. These dependency licenses do not select a license for this project. The lockfile must remain limited to the standalone package's API/browser dependencies and the existing development tools.

No live Threads payload, credential, local account data, browser profile/cookie store, operation journal, private evidence, or personal email was accessed, used in this audit, or included in tests. All API tests use synthetic values and mock HTTP; browser tests use fake Playwright objects and synthetic temporary profiles.
