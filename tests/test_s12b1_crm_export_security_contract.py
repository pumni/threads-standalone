from __future__ import annotations

import builtins
import json
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
PRIVATE_CONTEXT = "SYNTHETIC_PRIVATE_CONTEXT: private@example.invalid"


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012x}"


def _target(index: int) -> str:
    return f"https://www.threads.com/@synthetic/post/S12B{index:04d}"


def _entry(
    index: int,
    *,
    opportunity_id: str | None = None,
    target_url: str | None = None,
    context: str | None = PRIVATE_CONTEXT,
) -> OpportunityQueueEntry:
    return OpportunityQueueEntry(
        id=opportunity_id or _uuid(1_200 + index),
        target_url=target_url or _target(index),
        source="MANUAL_URL",
        context=context,
        context_provenance=cast(
            ContextProvenance,
            "NONE" if context is None else "OPERATOR_SUPPLIED",
        ),
        context_completeness=cast(
            ContextCompleteness,
            "UNKNOWN" if context is None else "COMPLETE",
        ),
        created_at=REVIEWED_AT,
        updated_at=REVIEWED_AT,
        state=cast(OpportunityState, "READY"),
        skip_reason=None,
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
    event_id = _uuid(20_000 + seed)
    return ManualOutcomeEvent(
        event_id=event_id,
        attempt_id=event_id if attempt_id is None else attempt_id,
        draft_sha256="a" * 64,
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
    seed: int,
) -> ManualOutcomeRecord:
    pending = _event(seed, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    if status == "HANDOFF_UNRESOLVED":
        events = (pending,)
    else:
        outcome = _event(
            seed + 1,
            status=status,
            source="OPERATOR",
            attempt_id=pending.attempt_id,
            recorded_at="2026-10-12T08:01:00Z",
        )
        events = (pending, outcome)
    return _record(account_uuid, entry, events)


def _snapshot(
    queue: OpportunityQueue,
) -> dict[str, ManualOutcomeRecord | None]:
    return {entry.target_url: None for entry in queue.opportunities}


def _export(
    queue: OpportunityQueue,
    selected_ids: tuple[str, ...],
    approvals: tuple[RecruitmentExportApproval, ...],
    records: dict[str, ManualOutcomeRecord | None] | None = None,
    *,
    now_utc: str = NOW,
) -> bytes:
    return build_recruitment_crm_handoff(
        queue,
        _snapshot(queue) if records is None else records,
        selected_ids,
        approvals,
        now_utc=now_utc,
    )


def _payload(encoded: bytes) -> dict[str, object]:
    value = json.loads(encoded.decode("utf-8", errors="strict"))
    assert isinstance(value, dict)
    return value


def test_default_packet_is_deterministic_and_omits_url_and_private_context() -> None:
    entry = _entry(1)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    first = _export(queue, (entry.id,), (approval,))
    second = _export(queue, (entry.id,), (approval,))
    record = _payload(first)["records"][0]  # type: ignore[index]

    assert first == second
    assert RecruitmentExportField.TARGET_URL not in DEFAULT_EXPORT_FIELDS
    assert "target_url" not in record["fields"]  # type: ignore[index]
    assert entry.target_url.encode() not in first
    assert PRIVATE_CONTEXT.encode() not in first
    assert b"private@example.invalid" not in first
    assert record["operator_approval"]["rights"] == (  # type: ignore[index]
        "ASSERTED_NOT_INDEPENDENTLY_VERIFIED"
    )


def test_target_url_is_included_only_for_the_item_with_explicit_opt_in() -> None:
    entry_a = _entry(2)
    entry_b = _entry(3)
    queue = _queue(entry_a, entry_b)
    approval_a = _approval(
        queue,
        entry_a,
        fields=(*DEFAULT_EXPORT_FIELDS, RecruitmentExportField.TARGET_URL),
    )
    approval_b = _approval(queue, entry_b)

    encoded = _export(queue, (entry_a.id, entry_b.id), (approval_a, approval_b))
    records = _payload(encoded)["records"]
    assert isinstance(records, list)

    fields_a = records[0]["fields"]
    fields_b = records[1]["fields"]
    assert fields_a["target_url"] == entry_a.target_url  # type: ignore[index]
    assert "target_url" not in fields_b  # type: ignore[operator]
    assert entry_b.target_url.encode() not in encoded


def test_two_account_outputs_are_isolated_and_approvals_do_not_cross_accounts() -> None:
    shared_id = _uuid(1_250)
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
    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(queue_b, (shared_id,), (approval_a,))
    assert caught.value.code == "CRM_EXPORT_APPROVAL_STALE"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: replace(entry, context="changed synthetic context"),
        lambda entry: replace(entry, target_url=_target(91)),
    ],
    ids=["context-changed", "target-changed"],
)
def test_changed_s6_snapshot_rejects_approval(mutate: object) -> None:
    entry = _entry(5)
    original_queue = _queue(entry)
    approval = _approval(original_queue, entry)
    changed_entry = mutate(entry)  # type: ignore[operator]
    changed_queue = _queue(changed_entry)

    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(changed_queue, (entry.id,), (approval,))

    assert caught.value.code == "CRM_EXPORT_APPROVAL_STALE"
    assert entry.target_url not in str(caught.value)
    assert PRIVATE_CONTEXT not in repr(caught.value)


def test_expired_approval_fails_closed() -> None:
    entry = _entry(6)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    with pytest.raises(RecruitmentCrmExportError) as caught:
        _export(queue, (entry.id,), (approval,), now_utc=EXPIRES_AT)

    assert caught.value.code == "CRM_EXPORT_APPROVAL_STALE"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("HANDOFF_UNRESOLVED", "NEEDS_VERIFICATION"),
        ("AMBIGUOUS", "NEEDS_VERIFICATION"),
        ("OPERATOR_REPORTED_SENT_UNVERIFIED", "NEEDS_VERIFICATION"),
        ("ABORTED_NOT_SENT", "LOCAL_ATTEMPT_ABORTED_NOT_SENT"),
    ],
)
def test_s4_outcomes_remain_conservative(
    outcome: str,
    expected: str,
) -> None:
    entry = _entry(7)
    queue = _queue(entry)
    approval = _approval(queue, entry)
    records = {
        entry.target_url: _record_for_status(ACCOUNT_A, entry, outcome, 30),
    }

    record = _payload(_export(queue, (entry.id,), (approval,), records))["records"][0]  # type: ignore[index]
    status = record["fields"]["followup_status"]  # type: ignore[index]

    assert status == expected
    assert status not in {"SENT", "REPLIED", "CONVERTED", "HIRED"}


def test_expected_security_gap_valid_s12a_approval_can_be_reused() -> None:
    entry = _entry(8)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    first = _export(queue, (entry.id,), (approval,))
    second = _export(queue, (entry.id,), (approval,))

    # EXPECTED SECURITY GAP: the pure API does not durably redeem or revoke it.
    assert first == second


def test_pure_builder_has_no_file_network_or_process_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(9)
    queue = _queue(entry)
    approval = _approval(queue, entry)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("pure handoff builder attempted an external side effect")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    encoded = _export(queue, (entry.id,), (approval,))

    assert encoded.startswith(b"{")
