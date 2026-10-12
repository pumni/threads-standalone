# S12A offline recruitment CRM handoff contract

S12A adds a pure in-memory JSON packet builder for synthetic/source-reviewed
inputs. It accepts a typed S6 `OpportunityQueue`, a complete typed S4 outcome
snapshot, an explicit tuple of selected opportunity IDs, and one immutable
operator approval for each selection. It uses S9B-1's public
`project_manual_followups()` projection. It does not write a file, read an
account root, start a browser, contact Threads, call a CRM, or publish anything.

## Public interface

`create_recruitment_export_approval()` binds one approval to an exact active S6
entry. The caller supplies a fixed `RecruitmentExportPurpose`, explicit positive
assertions for lawful basis, rights and consent, bounded UTC review and expiry
timestamps, and an ordered `RecruitmentExportField` tuple. The default allowlist
is `OPPORTUNITY_ID`, `SOURCE`, `FOLLOWUP_STATUS`, and `FOLLOWUP_REASON`.
`TARGET_URL` is absent by default; selecting it in this per-opportunity approval
is the only way the output can contain that entry's canonical URL.

`build_recruitment_crm_handoff()` accepts the queue, a plain `dict` containing
one S4 `ManualOutcomeRecord | None` value for every queue target, selected
opportunity IDs, the matching approvals, and a caller-supplied current UTC
timestamp. It returns deterministic UTF-8 JSON bytes. The timestamp makes the
24-hour approval expiry check deterministic; callers must supply their current
UTC time. No timestamp is taken from the system clock.

```python
approval = create_recruitment_export_approval(
    queue,
    opportunity_id,
    purpose=RecruitmentExportPurpose.FOLLOW_UP,
    lawful_basis_asserted=True,
    rights_asserted=True,
    consent_asserted=True,
    reviewed_at="2026-10-12T08:00:00Z",
    expires_at="2026-10-13T08:00:00Z",
)
packet = build_recruitment_crm_handoff(
    queue,
    s4_snapshot,
    (opportunity_id,),
    (approval,),
    now_utc="2026-10-12T08:30:00Z",
)
```

The example is illustrative only. This source contract cannot establish that a
human reviewed the exact fields or URL; a future private UI must display the
full exact opportunity and field list before it constructs the approval.

## Approval and identity limits

Each version 1 approval contains a domain-separated SHA-256 target commitment,
a separate commitment to the full S6 entry snapshot (account, opportunity,
canonical target, source, context, context provenance/completeness, timestamps,
state and skip reason), the fixed business purpose, the asserted rights fields,
approved field list, review/expiry times, and an integrity commitment over
those values. Any changed account, target, context, state or `updated_at` makes
the approval stale. Duplicate selections, missing approvals, extra approvals,
unsupported fields, stale approvals, noncanonical targets and expired
approvals fail with fixed error codes.

These SHA-256 values are equality commitments only. They are not signatures,
authentication, a secret, independent proof of platform rights, or proof that
the operator is entitled to export data. A hash can also be linkable or guessed
when its input has low entropy. The approval object and private export packet
must be handled as sensitive local data.

The lawful-basis, rights and consent booleans are operator assertions. The JSON
labels them `ASSERTED_NOT_INDEPENDENTLY_VERIFIED`; they do not prove lawful
basis, ownership, platform permission, or a candidate's consent. A future
operator workflow must separately provide the applicable legal and platform
review, exact private confirmation, withdrawal path, and secure file handling.

The approval expires no later than 24 hours after its review timestamp. This
pure contract checks expiry only against the caller-supplied `now_utc`; it has
no trusted clock or persistent revocation/replay store. It rejects duplicate
approval entries inside a packet but cannot detect reuse of a still-valid
approval across separate calls. A future UI/store must require fresh review or
track withdrawal and consumption. An export already copied into a CRM cannot be
withdrawn by this in-memory contract.

## Packet shape and privacy

The top-level object declares the packet schema/version and source versions for
S6 queue v1, S4 outcome v1 and S9B-1. Records follow supplied queue order and
contain an account-scoped opaque `record_id`, only the approved `fields`, and
fixed approval assertion metadata. Opportunity UUIDs and account UUIDs are not
included. `OPPORTUNITY_ID` is emitted only as a deterministic opaque digest.

The deterministic shape is:

```json
{
  "schema": "threads-local/recruitment-crm-handoff",
  "version": 1,
  "source_versions": {
    "s4_manual_outcomes": 1,
    "s6_opportunity_queue": 1,
    "s9b1_followup_projection": 1
  },
  "status_semantics": "LOCAL_EVIDENCE_ONLY_NO_VERIFIED_SEND_OR_CONVERSION",
  "records": [
    {
      "record_id": "<account-scoped opaque digest>",
      "fields": {
        "followup_reason": "NO_S4_RECORD_IN_SNAPSHOT",
        "followup_status": "NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT",
        "opportunity_id": "<opaque digest>",
        "source": "MANUAL_URL"
      },
      "operator_approval": {
        "business_purpose": "RECRUITMENT_FOLLOW_UP",
        "reviewed_at": "<bounded UTC timestamp>",
        "expires_at": "<bounded UTC timestamp>",
        "lawful_basis": "ASSERTED_NOT_INDEPENDENTLY_VERIFIED",
        "rights": "ASSERTED_NOT_INDEPENDENTLY_VERIFIED",
        "consent": "ASSERTED_NOT_INDEPENDENTLY_VERIFIED"
      }
    }
  ]
}
```

The example uses placeholders and a fixed reason code. The `fields` object
contains only fields named by that record's approval. When `TARGET_URL` is
approved, `target_url` is added to `fields` with that opportunity's exact
canonical permalink.

By default the packet omits raw target URLs, original operator context, draft or
post text, applicant CVs, names, contacts, private notes, rights text, media
references, credentials and personal information. Fixed status/reason values
and `MANUAL_URL` provenance are structured metadata, not confirmation of a
remote event. If the selected fields contain `TARGET_URL`, the packet is
sensitive and must be saved/imported privately by a separately authorized
future workflow. This slice has no file-writing path.

S4 follow-up values remain conservative:

| Projection evidence | Exported status |
| --- | --- |
| No record in supplied snapshot | `NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT` |
| Unresolved handoff, ambiguous event or operator-reported send | `NEEDS_VERIFICATION` |
| Locally recorded abort | `LOCAL_ATTEMPT_ABORTED_NOT_SENT` |

None of these labels means a verified send, reply, conversion, hire, or waiting
reply. `None` in the required S4 mapping means only that the caller's supplied
snapshot has no record for that target; it does not prove no remote action
occurred. The existing S9B-1 projection rejects malformed and contradictory S4
histories before rendering.

## Bounds, errors and future integration

The S6 queue is limited to 40 retained entries and 20 active entries; a packet
may select up to 20 active entries. Duplicate opportunity IDs/targets are
rejected by the typed projection. The complete S4 mapping must cover exactly
the queue's canonical targets. Packet output is capped at 64 KiB. Unsupported
free-text fields are not representable. Errors expose only fixed codes such as
`CRM_EXPORT_INVALID_QUEUE`, `CRM_EXPORT_INVALID_S4_SNAPSHOT`,
`CRM_EXPORT_APPROVAL_REQUIRED`, `CRM_EXPORT_APPROVAL_STALE`,
`CRM_EXPORT_DUPLICATE_SELECTION`, and `CRM_EXPORT_LIMIT_EXCEEDED`.

This contract does not enforce downstream retention, deletion, or withdrawal.
Before an authorized local-file/CRM integration, the operator must define an
expiry and deletion process for every copy, account isolation, private storage,
reviewed field selection, lawful-basis/consent review, and a way to honor
withdrawal downstream. A CRM connector, CLI, file writer, account lock,
operator confirmation UI, API, browser, and Threads activity are separate work
and are not included here.
