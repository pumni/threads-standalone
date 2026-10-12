from __future__ import annotations

import builtins
import json
import os
import socket
import subprocess
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import cast
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
    RecruitmentExportApproval,
    RecruitmentExportField,
    RecruitmentExportPurpose,
    build_recruitment_crm_handoff,
    create_recruitment_export_approval,
)
from threads_platform.standalone.recruitment_crm_review import (
    CrmExportReviewError,
    CrmExportReviewPacketV1,
    build_crm_export_review_packet,
    preflight_crm_export_review_confirmation,
    required_crm_export_confirmation,
)

ACCOUNT_A = "123e4567-e89b-42d3-a456-426614174000"
ACCOUNT_B = "123e4567-e89b-42d3-a456-426614174001"
REVIEWED_AT = "2026-10-12T08:00:00Z"
EXPIRES_AT = "2026-10-13T08:00:00Z"
NOW = "2026-10-12T08:30:00Z"
DESTINATION_A = f"sha256:{'a' * 64}"
DESTINATION_B = f"sha256:{'b' * 64}"
PRIVATE_CONTEXT = "SYNTHETIC_PRIVATE_CONTEXT: private@example.invalid"


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012x}"


def _target(index: int) -> str:
    return f"https://www.threads.com/@synthetic/post/S12B2A{index:04d}"


def _entry(
    index: int,
    *,
    account_seed: int = 0,
    opportunity_id: str | None = None,
    target_url: str | None = None,
    context: str | None = PRIVATE_CONTEXT,
    state: OpportunityState = "READY",
) -> OpportunityQueueEntry:
    if context is None:
        provenance: ContextProvenance = "NONE"
        completeness: ContextCompleteness = "UNKNOWN"
    else:
        provenance = "OPERATOR_SUPPLIED"
        completeness = "COMPLETE"
    return OpportunityQueueEntry(
        id=opportunity_id or _uuid(12_300 + account_seed * 100 + index),
        target_url=target_url or _target(index),
        source="MANUAL_URL",
        context=context,
        context_provenance=provenance,
        context_completeness=completeness,
        created_at=REVIEWED_AT,
        updated_at=REVIEWED_AT,
        state=state,
        skip_reason="operator skipped" if state == "SKIPPED" else None,
    )


def _queue(*entries: OpportunityQueueEntry, account_uuid: str = ACCOUNT_A) -> OpportunityQueue:
    return OpportunityQueue(account_uuid=account_uuid, opportunities=tuple(entries))


def _approval(
    queue: OpportunityQueue,
    entry: OpportunityQueueEntry,
    *,
    fields: tuple[RecruitmentExportField, ...] = DEFAULT_EXPORT_FIELDS,
    reviewed_at: str = REVIEWED_AT,
    expires_at: str = EXPIRES_AT,
) -> RecruitmentExportApproval:
    return create_recruitment_export_approval(
        queue,
        entry.id,
        purpose=RecruitmentExportPurpose.FOLLOW_UP,
        lawful_basis_asserted=True,
        rights_asserted=True,
        consent_asserted=True,
        reviewed_at=reviewed_at,
        expires_at=expires_at,
        selected_fields=fields,
    )


def _event(
    seed: int,
    *,
    status: str,
    source: str,
    attempt_id: str | None = None,
    recorded_at: str = REVIEWED_AT,
) -> ManualOutcomeEvent:
    event_id = _uuid(30_000 + seed)
    return ManualOutcomeEvent(
        event_id=event_id,
        attempt_id=event_id if attempt_id is None else attempt_id,
        draft_sha256="c" * 64,
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


def _records(
    queue: OpportunityQueue,
    overrides: dict[str, ManualOutcomeRecord | None] | None = None,
) -> dict[str, ManualOutcomeRecord | None]:
    result = {entry.target_url: None for entry in queue.opportunities}
    if overrides is not None:
        result.update(overrides)
    return result


def _packet(
    queue: OpportunityQueue,
    selected_ids: tuple[str, ...],
    approvals: tuple[RecruitmentExportApproval, ...],
    records: dict[str, ManualOutcomeRecord | None] | None = None,
    *,
    destination_identifier: str = DESTINATION_A,
    now_utc: str = NOW,
) -> CrmExportReviewPacketV1:
    return build_crm_export_review_packet(
        queue,
        _records(queue) if records is None else records,
        selected_ids,
        approvals,
        destination_identifier=destination_identifier,
        now_utc=now_utc,
    )


def _preflight(
    packet: CrmExportReviewPacketV1,
    queue: OpportunityQueue,
    selected_ids: tuple[str, ...],
    approvals: tuple[RecruitmentExportApproval, ...],
    records: dict[str, ManualOutcomeRecord | None] | None = None,
    *,
    destination_identifier: str = DESTINATION_A,
    confirmation: str | None = None,
    now_utc: str = NOW,
) -> str:
    return preflight_crm_export_review_confirmation(
        packet,
        queue,
        _records(queue) if records is None else records,
        approvals,
        selected_ids,
        destination_identifier,
        required_crm_export_confirmation(packet) if confirmation is None else confirmation,
        now_utc,
    )


def test_empty_queue_has_deterministic_snapshot_and_match_only_result() -> None:
    queue = _queue()
    packet = _packet(queue, (), ())
    repeated = _packet(queue, (), ())

    assert packet == repeated
    assert packet.selected_opportunity_ids == ()
    assert packet.items == ()
    assert packet.expires_at_utc is None
    assert _preflight(packet, queue, (), ()) == "SNAPSHOT_MATCH_ONLY"


def test_one_item_packet_binds_fields_and_assertions_without_raw_target_or_context() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    assert packet.items[0].selected_fields == DEFAULT_EXPORT_FIELDS
    assert packet.items[0].target_url_opt_in is False
    assert packet.items[0].assertion_status == "ASSERTED_NOT_INDEPENDENTLY_VERIFIED"
    assert packet.items[0].lawful_basis_asserted is True
    assert packet.items[0].rights_asserted is True
    assert packet.items[0].consent_asserted is True
    assert packet.expires_at_utc == EXPIRES_AT
    for rendered in (repr(packet), repr(packet.items[0])):
        assert ACCOUNT_A not in rendered
        assert entry.id not in rendered
        assert entry.target_url not in rendered
        assert PRIVATE_CONTEXT not in rendered
        assert "private@example.invalid" not in rendered


def test_twenty_selected_and_forty_retained_queue_bounds_are_supported() -> None:
    active = tuple(_entry(index) for index in range(20))
    queue_twenty = _queue(*active)
    approvals_twenty = tuple(_approval(queue_twenty, entry) for entry in active)
    packet_twenty = _packet(
        queue_twenty,
        tuple(entry.id for entry in active),
        approvals_twenty,
    )
    assert len(packet_twenty.items) == 20

    skipped = tuple(_entry(index + 20, context=None, state="SKIPPED") for index in range(20))
    queue_forty = _queue(*active, *skipped)
    approvals_forty = tuple(_approval(queue_forty, entry) for entry in active)
    packet_forty = _packet(
        queue_forty,
        tuple(entry.id for entry in active),
        approvals_forty,
    )
    assert len(queue_forty.opportunities) == 40
    assert len(packet_forty.items) == 20


def test_selection_over_twenty_fails_with_fixed_limit_error() -> None:
    active = tuple(_entry(index) for index in range(20))
    skipped = tuple(_entry(index + 20, context=None, state="SKIPPED") for index in range(20))
    queue = _queue(*active, *skipped)
    with pytest.raises(CrmExportReviewError) as caught:
        _packet(queue, tuple(entry.id for entry in (*active, skipped[0])), ())

    assert caught.value.code == "CRM_REVIEW_LIMIT_EXCEEDED"
    assert str(caught.value) == "CRM_REVIEW_LIMIT_EXCEEDED"
    assert ACCOUNT_A not in str(caught.value)
    assert skipped[0].target_url not in repr(caught.value)


def test_unselected_queue_entry_change_stales_full_queue_commitment() -> None:
    selected = _entry(1)
    unselected = _entry(2)
    queue = _queue(selected, unselected)
    approval = _approval(queue, selected)
    packet = _packet(queue, (selected.id,), (approval,))
    changed_unselected = replace(unselected, context=f"{unselected.context} suffix changed")
    fresh_queue = _queue(selected, changed_unselected)

    with pytest.raises(CrmExportReviewError) as caught:
        _preflight(packet, fresh_queue, (selected.id,), (approval,))

    assert caught.value.code == "CRM_REVIEW_STALE"


def test_selected_context_suffix_change_stales_even_with_same_target() -> None:
    entry = _entry(3)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    changed = replace(entry, context=f"{entry.context} additionally reviewed suffix")
    fresh_queue = _queue(changed)

    with pytest.raises(CrmExportReviewError) as caught:
        _preflight(packet, fresh_queue, (entry.id,), (approval,))

    assert caught.value.code == "CRM_REVIEW_STALE"


def test_changed_target_identity_fails_closed() -> None:
    entry = _entry(4)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    changed = replace(entry, target_url=_target(404))
    fresh_queue = _queue(changed)

    with pytest.raises(CrmExportReviewError) as caught:
        _preflight(packet, fresh_queue, (entry.id,), (approval,))

    assert caught.value.code == "CRM_REVIEW_STALE"


def test_older_s4_event_change_stales_even_when_latest_status_is_unchanged() -> None:
    entry = _entry(5)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    pending = _event(501, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    outcome = _event(
        502,
        status="OPERATOR_REPORTED_SENT_UNVERIFIED",
        source="OPERATOR",
        attempt_id=pending.attempt_id,
        recorded_at="2026-10-12T08:01:00Z",
    )
    record = _record(ACCOUNT_A, entry, (pending, outcome))
    records = _records(queue, {entry.target_url: record})
    packet = _packet(queue, (entry.id,), (approval,), records)

    changed_pending = replace(pending, recorded_at="2026-10-12T08:00:01Z")
    changed_record = _record(ACCOUNT_A, entry, (changed_pending, outcome))
    fresh_records = _records(queue, {entry.target_url: changed_record})
    with pytest.raises(CrmExportReviewError) as caught:
        _preflight(packet, queue, (entry.id,), (approval,), fresh_records)

    assert caught.value.code == "CRM_REVIEW_STALE"


def test_account_change_and_selection_order_change_stale_packet() -> None:
    entry_a = _entry(6)
    queue_a = _queue(entry_a)
    approval_a = _approval(queue_a, entry_a)
    packet = _packet(queue_a, (entry_a.id,), (approval_a,))

    entry_b = replace(entry_a, target_url=_target(606))
    queue_b = _queue(entry_b, account_uuid=ACCOUNT_B)
    approval_b = _approval(queue_b, entry_b)
    with pytest.raises(CrmExportReviewError) as wrong_account:
        _preflight(packet, queue_b, (entry_b.id,), (approval_b,))
    assert wrong_account.value.code == "CRM_REVIEW_STALE"

    first = _entry(7)
    second = _entry(8)
    queue = _queue(first, second)
    first_approval = _approval(queue, first)
    second_approval = _approval(queue, second)
    ordered_packet = _packet(
        queue,
        (first.id, second.id),
        (first_approval, second_approval),
    )
    with pytest.raises(CrmExportReviewError) as changed_order:
        _preflight(
            ordered_packet,
            queue,
            (second.id, first.id),
            (second_approval, first_approval),
        )
    assert changed_order.value.code == "CRM_REVIEW_STALE"


def test_two_account_packets_are_bound_to_independent_snapshots() -> None:
    shared_id = _uuid(12_399)
    entry_a = _entry(699, opportunity_id=shared_id, target_url=_target(699))
    entry_b = _entry(699, account_seed=1, opportunity_id=shared_id, target_url=_target(799))
    queue_a = _queue(entry_a, account_uuid=ACCOUNT_A)
    queue_b = _queue(entry_b, account_uuid=ACCOUNT_B)
    packet_a = _packet(queue_a, (shared_id,), (_approval(queue_a, entry_a),))
    packet_b = _packet(queue_b, (shared_id,), (_approval(queue_b, entry_b),))

    assert packet_a.account_uuid == ACCOUNT_A
    assert packet_b.account_uuid == ACCOUNT_B
    assert packet_a.snapshot_fingerprint != packet_b.snapshot_fingerprint
    assert _preflight(packet_a, queue_a, (shared_id,), (_approval(queue_a, entry_a),))
    assert _preflight(packet_b, queue_b, (shared_id,), (_approval(queue_b, entry_b),))


def test_changed_destination_or_per_item_url_opt_in_stales_packet() -> None:
    entry = _entry(9)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    with pytest.raises(CrmExportReviewError) as changed_destination:
        _preflight(
            packet,
            queue,
            (entry.id,),
            (approval,),
            destination_identifier=DESTINATION_B,
        )
    assert changed_destination.value.code == "CRM_REVIEW_STALE"

    url_approval = _approval(
        queue,
        entry,
        fields=(*DEFAULT_EXPORT_FIELDS, RecruitmentExportField.TARGET_URL),
    )
    opted_in_packet = _packet(queue, (entry.id,), (url_approval,))
    assert opted_in_packet.items[0].target_url_opt_in is True
    assert RecruitmentExportField.TARGET_URL in opted_in_packet.items[0].selected_fields
    assert entry.target_url not in repr(opted_in_packet)
    with pytest.raises(CrmExportReviewError) as changed_fields:
        _preflight(packet, queue, (entry.id,), (url_approval,))
    assert changed_fields.value.code == "CRM_REVIEW_STALE"

    different_expiry = _approval(
        queue,
        entry,
        expires_at="2026-10-13T07:59:59Z",
    )
    with pytest.raises(CrmExportReviewError) as changed_expiry:
        _preflight(packet, queue, (entry.id,), (different_expiry,))
    assert changed_expiry.value.code == "CRM_REVIEW_STALE"


def test_mutated_packet_version_or_action_is_rejected() -> None:
    entry = _entry(16)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    for changed in (replace(packet, version=2), replace(packet, action="IMPORT")):
        with pytest.raises(CrmExportReviewError) as caught:
            required_crm_export_confirmation(changed)
        assert caught.value.code == "CRM_REVIEW_INVALID_PACKET"


def test_public_handoff_keeps_url_opt_in_per_item_and_s4_status_conservative() -> None:
    entry_a = _entry(901)
    entry_b = _entry(902)
    queue = _queue(entry_a, entry_b)
    approval_a = _approval(queue, entry_a)
    approval_b = _approval(
        queue,
        entry_b,
        fields=(*DEFAULT_EXPORT_FIELDS, RecruitmentExportField.TARGET_URL),
    )
    pending = _event(901, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    outcome = _event(
        902,
        status="OPERATOR_REPORTED_SENT_UNVERIFIED",
        source="OPERATOR",
        attempt_id=pending.attempt_id,
        recorded_at="2026-10-12T08:01:00Z",
    )
    records = _records(
        queue,
        {entry_a.target_url: _record(ACCOUNT_A, entry_a, (pending, outcome))},
    )

    payload = json.loads(
        build_recruitment_crm_handoff(
            queue,
            records,
            (entry_a.id, entry_b.id),
            (approval_a, approval_b),
            now_utc=NOW,
        ).decode("utf-8", errors="strict")
    )
    assert isinstance(payload, dict)
    records_out = payload["records"]
    assert isinstance(records_out, list)
    fields_a = records_out[0]["fields"]
    fields_b = records_out[1]["fields"]
    assert "target_url" not in fields_a
    assert fields_b["target_url"] == entry_b.target_url
    assert fields_a["followup_status"] == "NEEDS_VERIFICATION"
    assert "SENT" not in fields_a["followup_status"]
    assert "CONVERTED" not in fields_a["followup_status"]


def test_exact_confirmation_must_match_and_is_bound_to_action_account_and_digest() -> None:
    entry = _entry(10)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))
    confirmation = required_crm_export_confirmation(packet)

    assert confirmation == f"EXPORT {ACCOUNT_A} {packet.snapshot_fingerprint}"
    assert packet.snapshot_fingerprint in confirmation
    assert confirmation not in repr(packet)
    assert _preflight(packet, queue, (entry.id,), (approval,), confirmation=confirmation)
    for wrong in (confirmation.lower(), f"{confirmation} ", "EXPORT", ""):
        with pytest.raises(CrmExportReviewError) as caught:
            _preflight(packet, queue, (entry.id,), (approval,), confirmation=wrong)
        assert caught.value.code == "CRM_REVIEW_CONFIRMATION_MISMATCH"
        if wrong:
            assert wrong not in str(caught.value)


def test_expired_approval_and_clock_rollback_are_stale() -> None:
    entry = _entry(11)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    with pytest.raises(CrmExportReviewError) as rollback:
        _preflight(
            packet,
            queue,
            (entry.id,),
            (approval,),
            now_utc="2026-10-12T08:29:59Z",
        )
    assert rollback.value.code == "CRM_REVIEW_STALE"

    with pytest.raises(CrmExportReviewError) as expired:
        _preflight(
            packet,
            queue,
            (entry.id,),
            (approval,),
            now_utc=EXPIRES_AT,
        )
    assert expired.value.code == "CRM_REVIEW_STALE"


def test_duplicate_selection_approval_and_invalid_s4_fail_closed() -> None:
    entry = _entry(12)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    with pytest.raises(CrmExportReviewError) as duplicate_selection:
        _packet(queue, (entry.id, entry.id), (approval,))
    assert duplicate_selection.value.code == "CRM_REVIEW_INVALID_INPUT"

    with pytest.raises(CrmExportReviewError) as duplicate_approval:
        _packet(queue, (entry.id,), (approval, approval))
    assert duplicate_approval.value.code == "CRM_REVIEW_INVALID_APPROVALS"

    with pytest.raises(CrmExportReviewError) as missing_s4_key:
        _packet(queue, (entry.id,), (approval,), {})
    assert missing_s4_key.value.code == "CRM_REVIEW_INVALID_SNAPSHOT"


def test_destination_identifier_and_timestamps_are_bounded() -> None:
    entry = _entry(13)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    for invalid_destination in (
        "D:\\private\\crm.json",
        "sha256:" + "A" * 64,
        "sha256:" + "f" * 63,
    ):
        with pytest.raises(CrmExportReviewError) as caught:
            _packet(
                queue,
                (entry.id,),
                (approval,),
                destination_identifier=invalid_destination,
            )
        assert caught.value.code == "CRM_REVIEW_INVALID_DESTINATION"
        assert invalid_destination not in str(caught.value)

    for invalid_time in ("2026-10-12T08:30:00+00:00", "1900-01-01T00:00:00Z"):
        with pytest.raises(CrmExportReviewError) as caught:
            _packet(queue, (entry.id,), (approval,), now_utc=invalid_time)
        assert caught.value.code == "CRM_REVIEW_INVALID_INPUT"
        assert invalid_time not in str(caught.value)


def test_identical_preflight_replay_is_possible_and_only_reports_snapshot_match() -> None:
    entry = _entry(14)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    packet = _packet(queue, (entry.id,), (approval,))

    first = _preflight(packet, queue, (entry.id,), (approval,))
    second = _preflight(packet, queue, (entry.id,), (approval,))

    assert first == second == "SNAPSHOT_MATCH_ONLY"


def test_review_contract_has_no_disk_terminal_network_browser_or_process_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(15)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("side effect attempted by pure review contract")

    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "open", denied)
        patcher.setattr(builtins, "input", denied)
        patcher.setattr(builtins, "print", denied)
        patcher.setattr(os, "open", denied)
        patcher.setattr(Path, "open", denied)
        patcher.setattr(socket, "socket", denied)
        patcher.setattr(socket, "create_connection", denied)
        patcher.setattr(subprocess, "run", denied)
        patcher.setattr(subprocess, "Popen", denied)
        patcher.setattr(urllib.request, "urlopen", denied)
        packet = _packet(queue, (entry.id,), (approval,))
        result = _preflight(packet, queue, (entry.id,), (approval,))

    assert result == "SNAPSHOT_MATCH_ONLY"
