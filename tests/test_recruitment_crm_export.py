from __future__ import annotations

import builtins
import json
import socket
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest

from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeEvent,
    ManualOutcomeRecord,
    ManualOutcomeSource,
    ManualOutcomeStatus,
    ManualOutcomeStore,
)
from threads_platform.standalone.opportunity_queue import (
    ContextCompleteness,
    ContextProvenance,
    OpportunityQueue,
    OpportunityQueueEntry,
    OpportunityState,
)
from threads_platform.standalone.recruitment_crm_export import (
    DEFAULT_EXPORT_FIELDS,
    RecruitmentCrmExportError,
    RecruitmentExportApproval,
    RecruitmentExportField,
    RecruitmentExportPurpose,
    build_recruitment_crm_handoff,
    create_recruitment_export_approval,
)

ACCOUNT_A = "123e4567-e89b-42d3-a456-426614174000"
ACCOUNT_B = "123e4567-e89b-42d3-a456-426614174001"
REVIEWED_AT = "2026-10-12T08:00:00Z"
EXPIRES_AT = "2026-10-13T08:00:00Z"
NOW = "2026-10-12T08:30:00Z"
PRIVATE_CONTEXT = "PRIVATE_CV_NAME: Synthetic Candidate; contact private@example.invalid"


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012x}"


def _target(index: int) -> str:
    return f"https://www.threads.com/@synthetic/post/Post{index:04d}"


def _entry(
    index: int,
    *,
    opportunity_id: str | None = None,
    target_url: str | None = None,
    context: str | None = PRIVATE_CONTEXT,
    context_completeness: str = "COMPLETE",
    state: str = "READY",
    updated_at: str = REVIEWED_AT,
    skip_reason: str | None = None,
) -> OpportunityQueueEntry:
    if context is None:
        context_provenance = "NONE"
        context_completeness = "UNKNOWN"
    else:
        context_provenance = "OPERATOR_SUPPLIED"
    if state == "SKIPPED" and skip_reason is None:
        skip_reason = "Synthetic local reason."
    return OpportunityQueueEntry(
        id=opportunity_id or _uuid(1_000 + index),
        target_url=target_url or _target(index),
        source="MANUAL_URL",
        context=context,
        context_provenance=cast(ContextProvenance, context_provenance),
        context_completeness=cast(ContextCompleteness, context_completeness),
        created_at=REVIEWED_AT,
        updated_at=updated_at,
        state=cast(OpportunityState, state),
        skip_reason=skip_reason,
    )


def _queue(
    *entries: OpportunityQueueEntry,
    account_uuid: str = ACCOUNT_A,
) -> OpportunityQueue:
    return OpportunityQueue(account_uuid=account_uuid, opportunities=tuple(entries))


def _approval(
    queue: OpportunityQueue,
    entry: OpportunityQueueEntry,
    *,
    fields: tuple[RecruitmentExportField, ...] = DEFAULT_EXPORT_FIELDS,
    purpose: RecruitmentExportPurpose = RecruitmentExportPurpose.FOLLOW_UP,
    lawful_basis: bool = True,
    rights: bool = True,
    consent: bool = True,
    reviewed_at: str = REVIEWED_AT,
    expires_at: str = EXPIRES_AT,
) -> RecruitmentExportApproval:
    return create_recruitment_export_approval(
        queue,
        entry.id,
        purpose=purpose,
        lawful_basis_asserted=lawful_basis,
        rights_asserted=rights,
        consent_asserted=consent,
        reviewed_at=reviewed_at,
        expires_at=expires_at,
        selected_fields=fields,
    )


def _snapshot(queue: OpportunityQueue) -> dict[str, ManualOutcomeRecord | None]:
    return {entry.target_url: None for entry in queue.opportunities}


def _event(
    seed: int,
    *,
    status: str,
    source: str,
    attempt_id: str | None = None,
    draft_sha256: str = "a" * 64,
    recorded_at: str = REVIEWED_AT,
) -> ManualOutcomeEvent:
    event_id = _uuid(10_000 + seed)
    return ManualOutcomeEvent(
        event_id=event_id,
        attempt_id=event_id if attempt_id is None else attempt_id,
        draft_sha256=draft_sha256,
        recorded_at=recorded_at,
        status=cast(ManualOutcomeStatus, status),
        source=cast(ManualOutcomeSource, source),
        verification="UNVERIFIED",
    )


def _record(
    account_uuid: str,
    entry: OpportunityQueueEntry,
    events: tuple[ManualOutcomeEvent, ...],
) -> ManualOutcomeRecord:
    return ManualOutcomeRecord(
        account_uuid=account_uuid,
        target_fingerprint=ManualOutcomeStore.target_fingerprint(
            UUID(account_uuid), entry.target_url
        ),
        events=events,
    )


def _record_for_status(
    account_uuid: str,
    entry: OpportunityQueueEntry,
    status: str,
) -> ManualOutcomeRecord:
    pending = _event(1, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    if status == "HANDOFF_UNRESOLVED":
        events = (pending,)
    else:
        outcome = _event(
            2,
            status=status,
            source="OPERATOR",
            attempt_id=pending.attempt_id,
            draft_sha256=pending.draft_sha256,
            recorded_at="2026-10-12T08:01:00Z",
        )
        events = (pending, outcome)
    return _record(account_uuid, entry, events)


def _export(
    queue: OpportunityQueue,
    selected: tuple[str, ...],
    approvals: tuple[RecruitmentExportApproval, ...],
    records: dict[str, ManualOutcomeRecord | None] | None = None,
    *,
    now_utc: str = NOW,
) -> bytes:
    return build_recruitment_crm_handoff(
        queue,
        _snapshot(queue) if records is None else records,
        selected,
        approvals,
        now_utc=now_utc,
    )


def _payload(value: bytes) -> dict[str, object]:
    decoded = value.decode("utf-8", errors="strict")
    parsed = json.loads(decoded, parse_constant=lambda _: pytest.fail("non-finite JSON"))
    assert isinstance(parsed, dict)
    return parsed


def test_zero_selected_entries_produce_deterministic_empty_packet() -> None:
    queue = _queue(_entry(0))

    first = _export(queue, (), ())
    second = _export(queue, (), ())

    assert first == second
    payload = _payload(first)
    assert payload["schema"] == "threads-local/recruitment-crm-handoff"
    assert payload["version"] == 1
    assert payload["records"] == []


def test_empty_queue_is_supported() -> None:
    assert _payload(_export(_queue(), (), ()))["records"] == []


def test_one_default_record_omits_target_context_and_private_text() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    encoded = _export(queue, (entry.id,), (approval,))
    payload = _payload(encoded)
    record = payload["records"][0]  # type: ignore[index]

    assert record["fields"]["source"] == "MANUAL_URL"  # type: ignore[index]
    assert record["fields"]["followup_status"] == "NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT"  # type: ignore[index]
    assert record["fields"]["opportunity_id"] != entry.id  # type: ignore[index]
    assert "target_url" not in record["fields"]  # type: ignore[index]
    assert entry.target_url.encode() not in encoded
    assert PRIVATE_CONTEXT.encode() not in encoded
    assert b"private@example.invalid" not in encoded
    assert payload["status_semantics"] == ("LOCAL_EVIDENCE_ONLY_NO_VERIFIED_SEND_OR_CONVERSION")
    assert record["operator_approval"]["rights"] == "ASSERTED_NOT_INDEPENDENTLY_VERIFIED"  # type: ignore[index]


def test_target_url_requires_opportunity_specific_opt_in() -> None:
    entry = _entry(2)
    queue = _queue(entry)
    fields = (*DEFAULT_EXPORT_FIELDS, RecruitmentExportField.TARGET_URL)
    approval = _approval(queue, entry, fields=fields)

    record = _payload(_export(queue, (entry.id,), (approval,)))["records"][0]  # type: ignore[index]

    assert record["fields"]["target_url"] == entry.target_url  # type: ignore[index]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("HANDOFF_UNRESOLVED", "NEEDS_VERIFICATION"),
        ("AMBIGUOUS", "NEEDS_VERIFICATION"),
        ("OPERATOR_REPORTED_SENT_UNVERIFIED", "NEEDS_VERIFICATION"),
        ("ABORTED_NOT_SENT", "LOCAL_ATTEMPT_ABORTED_NOT_SENT"),
    ],
)
def test_s4_projection_never_promotes_operator_outcomes(
    outcome: str,
    expected: str,
) -> None:
    entry = _entry(3)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    records = {entry.target_url: _record_for_status(ACCOUNT_A, entry, outcome)}

    record = _payload(_export(queue, (entry.id,), (approval,), records))["records"][0]  # type: ignore[index]

    assert record["fields"]["followup_status"] == expected  # type: ignore[index]
    assert record["fields"]["followup_status"] not in {  # type: ignore[index]
        "SENT",
        "REPLIED",
        "WAITING_FOR_REPLY",
        "CONVERTED",
        "HIRED",
    }


def test_two_accounts_have_independent_opaque_ids() -> None:
    shared_id = _uuid(77)
    entry_a = _entry(4, opportunity_id=shared_id, target_url=_target(4))
    entry_b = _entry(4, opportunity_id=shared_id, target_url=_target(5))
    queue_a = _queue(entry_a, account_uuid=ACCOUNT_A)
    queue_b = _queue(entry_b, account_uuid=ACCOUNT_B)
    approval_a = _approval(queue_a, entry_a)
    approval_b = _approval(queue_b, entry_b)

    record_a = _payload(_export(queue_a, (shared_id,), (approval_a,)))["records"][0]  # type: ignore[index]
    record_b = _payload(_export(queue_b, (shared_id,), (approval_b,)))["records"][0]  # type: ignore[index]

    assert record_a["record_id"] != record_b["record_id"]
    assert record_a["fields"]["opportunity_id"] != record_b["fields"]["opportunity_id"]  # type: ignore[index]
    assert ACCOUNT_A.encode() not in json.dumps(record_a).encode()
    assert ACCOUNT_B.encode() not in json.dumps(record_b).encode()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: replace(entry, context="updated synthetic context"),
        lambda entry: replace(entry, target_url=_target(91)),
        lambda entry: replace(entry, updated_at="2026-10-12T08:10:00Z"),
        lambda entry: replace(
            entry,
            state=cast(OpportunityState, "NEEDS_CONTEXT"),
            context_completeness=cast(ContextCompleteness, "PARTIAL"),
        ),
    ],
    ids=["context", "target", "updated-at", "queue-state"],
)
def test_changed_queue_snapshot_rejects_stale_approval(mutate: object) -> None:
    entry = _entry(5)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    changed_entry = mutate(entry)  # type: ignore[operator]
    changed_queue = _queue(changed_entry)

    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(changed_queue, (entry.id,), (approval,))

    assert caught.value.code == "CRM_EXPORT_APPROVAL_STALE"
    assert entry.target_url not in str(caught.value)
    assert PRIVATE_CONTEXT not in repr(caught.value)


def test_wrong_account_and_wrong_target_approvals_fail_closed() -> None:
    entry_a = _entry(6)
    queue_a = _queue(entry_a, account_uuid=ACCOUNT_A)
    approval_a = _approval(queue_a, entry_a)
    entry_b = _entry(6)
    queue_b = _queue(entry_b, account_uuid=ACCOUNT_B)

    for queue in (queue_b, _queue(replace(entry_a, target_url=_target(60)))):
        with pytest.raises(RecruitmentCrmExportError) as caught:
            _export(queue, (entry_a.id,), (approval_a,))
        assert caught.value.code == "CRM_EXPORT_APPROVAL_STALE"


def test_missing_replayed_and_duplicate_approvals_are_rejected() -> None:
    entry = _entry(7)
    second_entry = _entry(70)
    queue = _queue(entry, second_entry)
    approval = _approval(queue, entry)

    with pytest.raises(RecruitmentCrmExportError, match="CRM_EXPORT_APPROVAL_REQUIRED"):
        _export(queue, (entry.id,), ())
    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(queue, (entry.id, second_entry.id), (approval, approval))
    assert caught.value.code == "CRM_EXPORT_DUPLICATE_SELECTION"


def test_duplicate_selected_opportunity_is_rejected() -> None:
    entry = _entry(8)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(queue, (entry.id, entry.id), (approval,))

    assert caught.value.code == "CRM_EXPORT_DUPLICATE_SELECTION"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lawful_basis_asserted", False),
        ("rights_asserted", False),
        ("consent_asserted", False),
    ],
)
def test_missing_operator_rights_or_consent_assertion_rejected(
    field: str,
    value: bool,
) -> None:
    entry = _entry(9)
    queue = _queue(entry)
    assertions: dict[str, object] = {
        "purpose": RecruitmentExportPurpose.FOLLOW_UP,
        "lawful_basis_asserted": True,
        "rights_asserted": True,
        "consent_asserted": True,
        "reviewed_at": REVIEWED_AT,
        "expires_at": EXPIRES_AT,
    }
    assertions[field] = value

    with pytest.raises(RecruitmentCrmExportError) as caught:
        create_recruitment_export_approval(queue, entry.id, **assertions)  # type: ignore[arg-type]

    assert caught.value.code == "CRM_EXPORT_APPROVAL_REQUIRED"


def test_unknown_or_noncanonical_fields_rejected() -> None:
    entry = _entry(10)
    queue = _queue(entry)

    with pytest.raises(RecruitmentCrmExportError) as unsupported:
        _approval(queue, entry, fields=cast(tuple[RecruitmentExportField, ...], ("PRIVATE_NOTE",)))
    assert unsupported.value.code == "CRM_EXPORT_UNSUPPORTED_FIELD"

    with pytest.raises(RecruitmentCrmExportError) as duplicate:
        _approval(
            queue,
            entry,
            fields=(RecruitmentExportField.SOURCE, RecruitmentExportField.SOURCE),
        )
    assert duplicate.value.code == "CRM_EXPORT_UNSUPPORTED_FIELD"


def test_approval_is_immutable_and_explicitly_versioned() -> None:
    entry = _entry(11)
    approval = _approval(_queue(entry), entry)

    assert approval.version == 1
    assert "<opaque>" in repr(approval)
    with pytest.raises(FrozenInstanceError):
        approval.expires_at = "2026-10-14T08:00:00Z"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("reviewed_at", "expires_at", "now", "code"),
    [
        ("2026-10-12T08:00:00+00:00", EXPIRES_AT, NOW, "CRM_EXPORT_INVALID_APPROVAL"),
        (REVIEWED_AT, "2026-10-14T08:00:00Z", NOW, "CRM_EXPORT_INVALID_APPROVAL"),
        (REVIEWED_AT, EXPIRES_AT, "2026-10-13T08:00:00Z", "CRM_EXPORT_APPROVAL_STALE"),
    ],
)
def test_approval_timestamp_bounds_and_expiry(
    reviewed_at: str,
    expires_at: str,
    now: str,
    code: str,
) -> None:
    entry = _entry(12)
    queue = _queue(entry)
    with pytest.raises(RecruitmentCrmExportError) as caught:
        approval = _approval(queue, entry, reviewed_at=reviewed_at, expires_at=expires_at)
        _export(queue, (entry.id,), (approval,), now_utc=now)
    assert caught.value.code == code


def test_default_export_fields_never_include_url_or_context() -> None:
    assert RecruitmentExportField.TARGET_URL not in DEFAULT_EXPORT_FIELDS
    assert RecruitmentExportField.OPPORTUNITY_STATE not in DEFAULT_EXPORT_FIELDS


def test_s4_snapshot_must_cover_exact_queue_and_validate_full_history() -> None:
    entry = _entry(13)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    with pytest.raises(RecruitmentCrmExportError) as missing:
        _export(queue, (entry.id,), (approval,), {})
    assert missing.value.code == "CRM_EXPORT_INVALID_S4_SNAPSHOT"

    bad_record = replace(
        _record_for_status(ACCOUNT_A, entry, "OPERATOR_REPORTED_SENT_UNVERIFIED"),
        target_fingerprint="b" * 64,
    )
    with pytest.raises(RecruitmentCrmExportError) as mismatched:
        _export(queue, (entry.id,), (approval,), {entry.target_url: bad_record})
    assert mismatched.value.code == "CRM_EXPORT_INVALID_S4_SNAPSHOT"

    pending = _event(31, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    conflicting = _event(
        32,
        status="AMBIGUOUS",
        source="OPERATOR",
        attempt_id=_uuid(99_999),
        draft_sha256=pending.draft_sha256,
        recorded_at="2026-10-12T08:01:00Z",
    )
    contradictory_record = _record(ACCOUNT_A, entry, (pending, conflicting))
    with pytest.raises(RecruitmentCrmExportError) as contradictory:
        _export(queue, (entry.id,), (approval,), {entry.target_url: contradictory_record})
    assert contradictory.value.code == "CRM_EXPORT_INVALID_S4_SNAPSHOT"


def test_unsupported_source_and_noncanonical_target_fail_closed() -> None:
    entry = _entry(14)
    wrong_source = replace(entry, source=cast(Literal["MANUAL_URL"], "SYNTHETIC_FIXTURE"))

    with pytest.raises(RecruitmentCrmExportError) as source_error:
        _validated = _approval(_queue(wrong_source), wrong_source)
    assert source_error.value.code == "CRM_EXPORT_INVALID_QUEUE"

    noncanonical = replace(
        entry,
        target_url=f"{entry.target_url}?utm_source=synthetic",
    )
    with pytest.raises(RecruitmentCrmExportError) as url_error:
        _approval(_queue(noncanonical), noncanonical)
    assert url_error.value.code == "CRM_EXPORT_INVALID_QUEUE"


def test_duplicate_opportunity_ids_and_targets_fail_closed() -> None:
    entry = _entry(15)
    duplicate_id = replace(_entry(16), id=entry.id)
    duplicate_target = replace(_entry(16), target_url=entry.target_url)

    for invalid_queue in (_queue(entry, duplicate_id), _queue(entry, duplicate_target)):
        with pytest.raises(RecruitmentCrmExportError) as caught:
            _export(invalid_queue, (), ())
        assert caught.value.code == "CRM_EXPORT_INVALID_QUEUE"


def test_40_retained_20_active_and_20_selected_boundary() -> None:
    entries = tuple(
        _entry(
            index,
            state="READY" if index < 20 else "SKIPPED",
        )
        for index in range(40)
    )
    queue = _queue(*entries)
    selected_entries = entries[:20]
    approvals = tuple(_approval(queue, entry) for entry in selected_entries)

    payload = _payload(_export(queue, tuple(entry.id for entry in selected_entries), approvals))

    assert len(payload["records"]) == 20  # type: ignore[arg-type]


def test_excess_retained_active_and_selected_bounds_rejected() -> None:
    entries_41 = tuple(
        _entry(index, state="READY" if index < 20 else "SKIPPED") for index in range(41)
    )
    with pytest.raises(RecruitmentCrmExportError) as retained:
        _export(_queue(*entries_41), (), ())
    assert retained.value.code == "CRM_EXPORT_INVALID_QUEUE"

    entries_21_active = tuple(_entry(index) for index in range(21))
    with pytest.raises(RecruitmentCrmExportError) as active:
        _export(_queue(*entries_21_active), (), ())
    assert active.value.code == "CRM_EXPORT_INVALID_QUEUE"

    entries = tuple(
        _entry(index, state="READY" if index < 20 else "SKIPPED") for index in range(21)
    )
    queue = _queue(*entries)
    with pytest.raises(RecruitmentCrmExportError) as selected:
        _export(queue, tuple(entry.id for entry in entries), ())
    assert selected.value.code == "CRM_EXPORT_LIMIT_EXCEEDED"


def test_s4_abort_is_local_attempt_only_and_missing_record_is_snapshot_only() -> None:
    entry_a = _entry(20)
    entry_b = _entry(21)
    queue = _queue(entry_a, entry_b)
    approvals = (_approval(queue, entry_a), _approval(queue, entry_b))
    records = {
        entry_a.target_url: _record_for_status(ACCOUNT_A, entry_a, "ABORTED_NOT_SENT"),
        entry_b.target_url: None,
    }

    payload = _payload(_export(queue, (entry_a.id, entry_b.id), approvals, records))
    values = [record["fields"]["followup_status"] for record in payload["records"]]  # type: ignore[index]

    assert values == [
        "LOCAL_ATTEMPT_ABORTED_NOT_SENT",
        "NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT",
    ]


def test_no_disk_network_subprocess_or_browser_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(22)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("unexpected external side effect")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    result = _export(queue, (entry.id,), (approval,))

    assert result.startswith(b"{")


def test_error_and_approval_repr_do_not_expose_private_context_or_target() -> None:
    entry = _entry(23)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    changed = _queue(replace(entry, context="different private context"))

    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(changed, (entry.id,), (approval,))

    assert PRIVATE_CONTEXT not in str(caught.value)
    assert entry.target_url not in repr(caught.value)
    assert PRIVATE_CONTEXT not in repr(approval)
    assert entry.target_url not in repr(approval)
