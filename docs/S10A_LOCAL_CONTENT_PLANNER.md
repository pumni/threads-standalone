# S10A local recruitment content planner

This is an offline-only library slice for planning original recruitment and CV-advice posts. It adds a separate private versioned store; it does not add CLI integration, a scheduler, a media transfer, a browser action, an API call, or a publish operation. It implements part of [#62](https://github.com/pumni/threads-standalone/issues/62) under [#88](https://github.com/pumni/threads-standalone/issues/88) and roadmap [#57](https://github.com/pumni/threads-standalone/issues/57). Optional AI/media rights work remains gated separately by #25.

## Store and calendar boundary

`RecruitmentContentPlannerStore(data_root, utc_now=...)` stores one strict UTF-8 JSON document per account:

```text
<data_root>/recruitment_content_planner_v1/accounts/<account_uuid>.json
```

Operations take the existing S4 account process lock through `ManualOutcomeStore.account_lock`. Planner data has a new namespace and does not change the S4 journal, queues, Recurrences, Nurture or existing content codecs. The local data root must already exist and must be outside the checkout, as required by the shared lock boundary.

The top-level document has exactly `version`, `account_uuid`, and `packets`. Both the calendar and each packet carry the canonical account UUID. Every packet has a stable UUIDv4 `packet_id`, `version: 1`, original operator text, provenance and rights assertions, optional inert attachment metadata, bounded private notes, UTC schedule and audit times, editorial status, and a fixed remote-publication status. Duplicate JSON keys, unknown/missing fields, unsupported versions, wrong account IDs, invalid types, oversized files, and duplicate packet IDs fail closed.

Writes use a same-directory staging file, flush/fsync, strict reread, and atomic replace. Planner namespace and account directories are created with owner-only mode where supported; documents are owner read/write only where supported. Symlinks/reparse points, redirected paths, non-regular files, and non-private POSIX files fail closed. The store retains at most 40 packets per account, of which at most 20 may be non-rejected. There is no delete or automatic-expiry operation in this slice.

## Editorial and rights semantics

New packets start in `DRAFT`. `approve()` changes a draft to `READY_FOR_MANUAL_PUBLICATION`; `reject()` records a required local reason and changes the packet to `REJECTED`. A rejected packet cannot be approved. Approval is an editorial assertion only. Every stored item has `remote_publication_status: NOT_PUBLISHED_BY_PLANNER`; the schema has no published state and this module has no publish method. A ready packet still requires a separate human action through an authorized path.

Creation accepts only `provenance="OPERATOR_AUTHORED"` and requires both `rights_asserted=True` and `consent_asserted=True`. Attachments, when present, separately require rights and consent assertions. The stored `UNVERIFIED_OPERATOR_ASSERTION` label explicitly means the software did not verify ownership, subject consent, licensing, or accuracy. `platform_permission_status` is always `NOT_ESTABLISHED`; these assertions do not grant or prove platform permission.

Optional `ContentAttachmentMetadataV1` stores only a bounded HTTPS URL, kind (`IMAGE`, `VIDEO`, or `REFERENCE_LINK`), optional label/attribution, and the operator's unverified rights/consent assertions. URLs are inert metadata: the planner never opens a local path, downloads, previews, uploads, or follows the link. URL queries, fragments, credentials, local hosts, and non-public IP literals are rejected.

The planner screens original text, notes, review notes, labels, attribution, and decoded URL paths for common CV/private-applicant markers, contact fields, email/phone patterns, addresses, and sensitive-data terms. It rejects a URL embedded in post text; callers can provide a separately rights-asserted link reference instead. This is a conservative pattern check, not a full personal-data classifier or proof that arbitrary names and narratives are non-identifying. Operators must not enter private applicant CVs or personal data; sensitive customer material is not suitable for this public repository or CI.

## Dates, bounds, and duplicates

Schedule inputs must be timezone-aware UTC datetimes (zero UTC offset). They must be within 180 days before or after the injected store clock. Persisted timestamps use exact UTC microsecond precision (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). `due(account_uuid)` and `upcoming(account_uuid)` are local deterministic listings sorted by schedule time; rejected packets are excluded. They do not create notifications, timers, or background work.

Original post text is limited to 500 characters; private notes to 1,000; editorial notes to 500; attachment URLs to 2,048 UTF-8 bytes; labels to 120; attribution to 200; each account document to 512 KiB. Exact normalized duplicate text returns the existing packet ID with an `EXACT_DUPLICATE` warning and does not add a second packet. Similar text may be added, with up to five deterministic `NEAR_DUPLICATE` warnings containing only matching packet IDs and similarity percentages. These warnings are advisory and compare only records within the same account calendar.

## Example

```python
from datetime import UTC, datetime, timedelta

from threads_platform.standalone.recruitment_content_planner import (
    RecruitmentContentPlannerStore,
)

planner = RecruitmentContentPlannerStore(data_root)
result = planner.create(
    account_uuid,
    "Share a clear role brief and interview timeline with every applicant.",
    datetime.now(UTC) + timedelta(days=1),
    provenance="OPERATOR_AUTHORED",
    rights_asserted=True,
    consent_asserted=True,
)
ready_for_review = planner.approve(account_uuid, result.packet.packet_id)
```

The approval result remains `NOT_PUBLISHED_BY_PLANNER`. No CLI, native-composer handoff, browser runtime, API client, AI provider, or scheduler is part of this interface.

## Failure boundary and follow-up

`RecruitmentContentPlannerError` exposes fixed codes only (`CONTENT_PLANNER_INVALID`, `..._STATE_INVALID`, `..._RIGHTS_REQUIRED`, `..._PRIVATE_CONTENT_REJECTED`, `..._SCHEDULE_INVALID`, `..._LIMIT_REACHED`, `..._BUSY`, and related source/review codes). It does not include text, notes, URLs, UUIDs, or paths.

This slice does not import external sources, quote/repost content, inspect applicant files, read media, verify copyright/consent, establish account ownership or platform permission, send reminders, publish, or report remote success. Any integration with S9A review, CLI workflows, official publishing APIs, native UI, external AI/media, or live account operations needs its own reviewed scope and authorization.
