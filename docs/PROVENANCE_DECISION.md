# Provenance and license decision

## Status

**File-level transfer audit: PASS for SPLIT-02 listed files. Public distribution/license policy: OPEN under `pumni/threads-standalone#2`.** The owner-directed engineering migration allows the bounded transfer recorded below; it is not a blanket relicensing or legal determination. No first-party LICENSE or SPDX marker is added.

## Audit method and limitations

The source was inspected at immutable commit `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`. For each source file below, the audit checked source history and GitHub commit author origin, file headers, repository-level license/notice files, and relevant third-party attribution/restrictions before copying or porting. Source commit history for the listed first-party files resolves to the repository owner's GitHub account; the files contain no copyright/SPDX header, and the source baseline has no first-party LICENSE, COPYING, or NOTICE. No personal emails or personal data are recorded here.

This history-based classification supports the coordinator's `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` category for this bounded engineering migration. It does not establish general public reuse rights or settle authorship facts outside the inspected repository history. The open license-policy issue remains a separate release blocker.

## First-party source paths transferred or adapted

| Frozen source path | Destination path | Classification | Treatment |
| --- | --- | --- | --- |
| `src/threads_platform/standalone/accounts.py` | `src/threads_platform/standalone/accounts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred account v1 store and path-safety behavior. |
| `src/threads_platform/standalone/__main__.py` | `src/threads_platform/standalone/__main__.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported account and read-only API CLI branches; no mutation/browser commands. |
| `src/threads_platform/standalone/api.py` | `src/threads_platform/standalone/api.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported local read runtime and validation/error handling. |
| `src/threads_platform/standalone/app.py` | `src/threads_platform/standalone/app.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Reduced composition to local accounts and API reads. |
| `src/threads_platform/application/ports/threads.py` | `src/threads_platform/api/contracts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Selected only API-read DTOs, errors, protocols, and credential resolver contracts. |
| `src/threads_platform/infrastructure/threads_api/client.py` | `src/threads_platform/api/client.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported HTTP read methods and parsing/error helpers; omitted container creation/publication and reply-management methods. |
| `src/threads_platform/infrastructure/threads_api/environment_credentials.py` | `src/threads_platform/credentials/environment.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Ported exact `env://` validation and process-environment resolver; no secret persistence/cache. |
| `src/threads_platform/config/settings.py` | `src/threads_platform/config.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Reduced settings to the API base URL; distributed/database settings were not transferred. |
| `src/threads_platform/domain/discovery.py` | `src/threads_platform/api/contracts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Selected only the two API search enums. |
| `tests/unit/test_standalone_accounts.py` | `tests/test_accounts.py` | `FIRST_PARTY_AUTHORIZED_FOR_OWNER_DIRECTED_TRANSFER` | Transferred synthetic temporary-directory account tests. |

The ported behavior keeps attribution traceable through this mapping and the frozen source SHA. The destination Git history remains independent; no source commit history was rewritten into it.

## Source tests audited but not transferred

The following source test histories were inspected for origin/restrictions and behavior; destination coverage was written as targeted synthetic tests rather than copying these files: `tests/unit/test_standalone_api.py`, `tests/unit/test_standalone_cli.py`, `tests/unit/test_standalone_app.py`, `tests/unit/test_standalone_runtime.py`, `tests/unit/test_threads_api_contract.py`, `tests/unit/test_threads_discovery_contract.py`, `tests/unit/test_threads_credential_secrets.py`, `tests/unit/test_threads_httpx2_transport.py`, and `tests/unit/test_standalone_architecture.py`. Live-evidence tests and mutation-only tests were not read into or copied to the destination.

## Third-party dependency review

No third-party source implementation or test fixture is copied. Direct runtime dependencies remain separately licensed upstream packages: [`httpx2` (BSD-3-Clause)](https://github.com/pydantic/httpx2/blob/main/LICENSE.md), [`pydantic` (MIT)](https://github.com/pydantic/pydantic/blob/main/LICENSE), and [`pydantic-settings` (MIT)](https://github.com/pydantic/pydantic-settings/blob/main/LICENSE). Their dependency licenses do not select a license for this project. The lockfile must remain limited to this standalone package's API/settings dependencies and the existing development tools.

No live Threads payload, credential, local account data, browser profile, operation journal, private evidence, or personal email was used in this audit or tests. All API tests use synthetic values and mock HTTP.
