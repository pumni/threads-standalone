from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, replace
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
from threads_platform.standalone.recruitment_followup_projection import (
    FollowupProjectionError,
    project_manual_followups,
)

ACCOUNT_A = "123e4567-e89b-42d3-a456-426614174000"
ACCOUNT_B = "123e4567-e89b-42d3-a456-426614174001"
PRIVATE_CONTEXT = "SYNTHETIC_PRIVATE_CONTEXT: job seeker asks about remote IT work."
RECORDED_AT = "2026-10-12T08:00:00Z"
DRAFT_SHA256 = "a" * 64


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012x}"


def _target(index: int) -> str:
    return f"https://www.threads.com/@synthetic/post/Post{index:04d}"


def _entry(
    index: int,
    *,
    opportunity_id: str | None = None,
    target_url: str | None = None,
    state: str | None = None,
    context: str | None = PRIVATE_CONTEXT,
    context_provenance: str | None = "OPERATOR_SUPPLIED",
    context_completeness: str | None = "COMPLETE",
    skip_reason: str | None = None,
) -> OpportunityQueueEntry:
    resolved_state = state or ("READY" if index < 20 else "SKIPPED")
    resolved_skip_reason = skip_reason
    if resolved_state == "SKIPPED" and resolved_skip_reason is None:
        resolved_skip_reason = "Synthetic skip reason."
    return OpportunityQueueEntry(
        id=opportunity_id or _uuid(1_000 + index),
        target_url=target_url or _target(index),
        source="MANUAL_URL",
        context=context,
        context_provenance=cast(
            ContextProvenance,
            context_provenance
            if context_provenance is not None
            else ("NONE" if context is None else "OPERATOR_SUPPLIED"),
        ),
        context_completeness=cast(
            ContextCompleteness,
            context_completeness
            if context_completeness is not None
            else ("UNKNOWN" if context is None else "COMPLETE"),
        ),
        created_at=RECORDED_AT,
        updated_at=RECORDED_AT,
        state=cast(OpportunityState, resolved_state),
        skip_reason=resolved_skip_reason,
    )


def _queue(
    count: int = 1,
    *,
    account_uuid: str = ACCOUNT_A,
    entries: tuple[OpportunityQueueEntry, ...] | None = None,
) -> OpportunityQueue:
    values = entries if entries is not None else tuple(_entry(i) for i in range(count))
    return OpportunityQueue(account_uuid=account_uuid, opportunities=values)


def _event(
    seed: int,
    *,
    status: str,
    source: str,
    attempt_id: str | None = None,
    draft_sha256: str = DRAFT_SHA256,
    recorded_at: str = RECORDED_AT,
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
    entry: OpportunityQueueEntry,
    events: tuple[ManualOutcomeEvent, ...],
    *,
    account_uuid: str = ACCOUNT_A,
    target_url: str | None = None,
    target_fingerprint: str | None = None,
) -> ManualOutcomeRecord:
    target = entry.target_url if target_url is None else target_url
    fingerprint = (
        ManualOutcomeStore.target_fingerprint(UUID(account_uuid), target)
        if target_fingerprint is None
        else target_fingerprint
    )
    return ManualOutcomeRecord(
        account_uuid=account_uuid,
        target_fingerprint=fingerprint,
        events=events,
    )


def _record_with_latest_status(
    entry: OpportunityQueueEntry,
    status: str,
) -> ManualOutcomeRecord:
    handoff = _event(1, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    if status == "HANDOFF_UNRESOLVED":
        return _record(entry, (handoff,))
    outcome = _event(
        2,
        status=status,
        source="OPERATOR",
        attempt_id=handoff.attempt_id,
        draft_sha256=handoff.draft_sha256,
        recorded_at="2026-10-12T08:01:00Z",
    )
    return _record(entry, (handoff, outcome))


class _DuplicateKeyMapping(Mapping[str, ManualOutcomeRecord | None]):
    def __init__(self, key: str, *, repetitions: int = 2) -> None:
        self._key = key
        self._repetitions = repetitions

    def __getitem__(self, key: str) -> ManualOutcomeRecord | None:
        if key != self._key:
            raise KeyError(key)
        return None

    def __iter__(self) -> Iterator[str]:
        return iter((self._key,) * self._repetitions)

    def __len__(self) -> int:
        return self._repetitions


@pytest.mark.parametrize("count", [0, 1, 20, 40])
def test_queue_sizes_project_bounded_no_followup_rows(count: int) -> None:
    queue = _queue(count)

    result = project_manual_followups(queue, {})

    assert len(result.rows) == count
    assert all(row.state == "NO_FOLLOWUP" for row in result.rows)
    assert all(row.reason_code == "NO_S4_RECORD_IN_SNAPSHOT" for row in result.rows)
    assert all(row.latest_attempt_id is None for row in result.rows)


def test_no_record_and_explicit_none_are_snapshot_scoped() -> None:
    queue = _queue()
    target = queue.opportunities[0].target_url

    missing = project_manual_followups(queue, {})
    explicit_none = project_manual_followups(queue, {target: None})

    assert missing == explicit_none
    assert missing.rows[0].state == "NO_FOLLOWUP"
    assert missing.rows[0].manual_next_step == "NONE"


@pytest.mark.parametrize(
    ("status", "expected_state", "expected_reason", "expected_next_step"),
    [
        (
            "HANDOFF_UNRESOLVED",
            "NEEDS_VERIFICATION",
            "HANDOFF_UNRESOLVED",
            "CHECK_NATIVE_UI_PRIVATELY",
        ),
        (
            "AMBIGUOUS",
            "NEEDS_VERIFICATION",
            "AMBIGUOUS_OUTCOME",
            "CHECK_NATIVE_UI_PRIVATELY",
        ),
        (
            "OPERATOR_REPORTED_SENT_UNVERIFIED",
            "NEEDS_VERIFICATION",
            "OPERATOR_SEND_UNVERIFIED",
            "CHECK_NATIVE_UI_PRIVATELY",
        ),
        (
            "ABORTED_NOT_SENT",
            "CLOSED",
            "LOCAL_ATTEMPT_ABORTED_NOT_SENT",
            "RESELECT_IF_STILL_RELEVANT",
        ),
    ],
)
def test_latest_s4_status_maps_only_to_allowed_local_states(
    status: str,
    expected_state: str,
    expected_reason: str,
    expected_next_step: str,
) -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    record = _record_with_latest_status(entry, status)

    result = project_manual_followups(queue, {entry.target_url: record})
    row = result.rows[0]

    assert row.state == expected_state
    assert row.reason_code == expected_reason
    assert row.manual_next_step == expected_next_step
    assert row.latest_attempt_id == record.events[-1].attempt_id
    assert row.latest_event_at == record.events[-1].recorded_at
    assert row.state not in {"SENT", "REMOTE_VERIFIED", "WAITING_FOR_REPLY"}


def test_reopened_attempt_uses_latest_valid_s4_event() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    first_handoff = _event(10, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    first_abort = _event(
        11,
        status="ABORTED_NOT_SENT",
        source="OPERATOR",
        attempt_id=first_handoff.attempt_id,
        draft_sha256=first_handoff.draft_sha256,
        recorded_at="2026-10-12T08:01:00Z",
    )
    second_handoff = _event(12, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    second_abort = _event(
        13,
        status="ABORTED_NOT_SENT",
        source="OPERATOR",
        attempt_id=second_handoff.attempt_id,
        draft_sha256=second_handoff.draft_sha256,
        recorded_at="2026-10-12T08:03:00Z",
    )
    record = _record(entry, (first_handoff, first_abort, second_handoff, second_abort))

    row = project_manual_followups(queue, {entry.target_url: record}).rows[0]

    assert row.state == "CLOSED"
    assert row.latest_attempt_id == second_handoff.attempt_id
    assert row.latest_attempt_id != first_handoff.attempt_id


def test_legacy_unverified_operator_report_needs_verification() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    legacy_event = _event(
        20,
        status="OPERATOR_REPORTED_SENT_UNVERIFIED",
        source="LEGACY_OPERATOR",
    )
    record = _record(entry, (legacy_event,))

    row = project_manual_followups(queue, {entry.target_url: record}).rows[0]

    assert row.state == "NEEDS_VERIFICATION"
    assert row.reason_code == "OPERATOR_SEND_UNVERIFIED"


def test_aborted_legacy_declaration_is_closed_only_as_local_attempt() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    legacy_event = _event(21, status="ABORTED_NOT_SENT", source="LEGACY_OPERATOR")
    record = _record(entry, (legacy_event,))

    row = project_manual_followups(queue, {entry.target_url: record}).rows[0]

    assert row.state == "CLOSED"
    assert row.reason_code == "LOCAL_ATTEMPT_ABORTED_NOT_SENT"


def test_queue_input_order_is_stable_and_rows_are_frozen() -> None:
    queue = _queue(3)

    first = project_manual_followups(queue, {})
    second = project_manual_followups(queue, {})

    assert first == second
    assert tuple(row.opportunity_id for row in first.rows) == tuple(
        entry.id for entry in queue.opportunities
    )
    with pytest.raises(FrozenInstanceError):
        first.rows[0].__setattr__("state", "CLOSED")


@pytest.mark.parametrize(
    "bad_target",
    [
        "https://example.com/@synthetic/post/Post0000",
        "https://www.threads.com/@synthetic/post/Post0000?utm_source=synthetic",
        "https://www.threads.com/@synthetic/post/%50ost0000",
        "https://www.threads.com/@synthetic/post/\\ud800",
    ],
)
def test_invalid_foreign_or_noncanonical_queue_targets_fail_closed(
    bad_target: str,
) -> None:
    queue = _queue()
    bad_entry = replace(queue.opportunities[0], target_url=bad_target)
    bad_queue = replace(queue, opportunities=(bad_entry,))

    with pytest.raises(FollowupProjectionError) as error:
        project_manual_followups(bad_queue, {})

    assert error.value.code == "FOLLOWUP_QUEUE_INVALID"
    assert bad_target not in str(error.value)


def test_queue_rejects_duplicate_ids_targets_invalid_uuid_and_active_overflow() -> None:
    entries = (_entry(0), _entry(1))
    duplicate_id = replace(entries[1], id=entries[0].id)
    duplicate_target = replace(entries[1], target_url=entries[0].target_url)

    for bad_queue in (
        _queue(entries=(entries[0], duplicate_id)),
        _queue(entries=(entries[0], duplicate_target)),
        replace(_queue(), account_uuid="not-a-uuid"),
        _queue(41),
        _queue(entries=tuple(_entry(index, state="READY") for index in range(21))),
    ):
        with pytest.raises(FollowupProjectionError) as error:
            project_manual_followups(bad_queue, {})
        assert error.value.code == "FOLLOWUP_QUEUE_INVALID"


@pytest.mark.parametrize(
    "bad_entry",
    [
        replace(_entry(0), source=cast(object, "SYNTHETIC_FIXTURE")),
        replace(_entry(0), created_at="2026-10-12T08:00:00+00:00"),
        replace(_entry(0), updated_at="2026-10-11T08:00:00Z"),
        replace(_entry(0), context=" padded "),
        replace(_entry(0), state=cast(object, "UNKNOWN")),
    ],
)
def test_malformed_typed_queue_entries_fail_closed(bad_entry: OpportunityQueueEntry) -> None:
    with pytest.raises(FollowupProjectionError) as error:
        project_manual_followups(_queue(entries=(bad_entry,)), {})
    assert error.value.code == "FOLLOWUP_QUEUE_INVALID"


def test_s6_unknown_context_and_non_ascii_skip_space_are_accepted() -> None:
    needs_context = _entry(0, state="NEEDS_CONTEXT", context_completeness="UNKNOWN")
    skipped = _entry(1, state="SKIPPED", skip_reason="Synthetic\u00a0skip reason.")
    queue = _queue(entries=(needs_context, skipped))

    result = project_manual_followups(queue, {})

    assert [row.state for row in result.rows] == ["NO_FOLLOWUP", "NO_FOLLOWUP"]


def test_two_account_isolation_and_target_fingerprint_binding() -> None:
    queue_a = _queue(account_uuid=ACCOUNT_A)
    queue_b = _queue(account_uuid=ACCOUNT_B)
    entry_a = queue_a.opportunities[0]
    record_a = _record(
        entry_a, (_event(30, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),)
    )

    assert project_manual_followups(queue_a, {entry_a.target_url: record_a}).rows[0].state == (
        "NEEDS_VERIFICATION"
    )
    with pytest.raises(FollowupProjectionError) as wrong_account:
        project_manual_followups(queue_b, {entry_a.target_url: record_a})
    assert wrong_account.value.code == "FOLLOWUP_EVIDENCE_INVALID"

    bad_fingerprint = replace(record_a, target_fingerprint="0" * 64)
    with pytest.raises(FollowupProjectionError) as wrong_target:
        project_manual_followups(queue_a, {entry_a.target_url: bad_fingerprint})
    assert wrong_target.value.code == "FOLLOWUP_EVIDENCE_INVALID"


def test_stray_noncanonical_or_duplicate_snapshot_keys_fail_closed() -> None:
    queue = _queue()
    target = queue.opportunities[0].target_url
    stray = "https://www.threads.com/@elsewhere/post/NotInQueue"

    mappings: tuple[object, ...] = (
        {stray: None},
        {f"{target}?utm_source=synthetic": None},
        _DuplicateKeyMapping(target),
        _DuplicateKeyMapping(target, repetitions=41),
        {1: None},
    )
    for mapping in mappings:
        with pytest.raises(FollowupProjectionError) as error:
            project_manual_followups(
                queue,
                cast(Mapping[str, ManualOutcomeRecord | None], mapping),
            )
        assert error.value.code == "FOLLOWUP_EVIDENCE_INVALID"
        assert target not in str(error.value)
        assert stray not in repr(error.value)


@pytest.mark.parametrize(
    "bad_record",
    [
        "AMBIGUOUS",
        7,
    ],
)
def test_status_only_and_nonrecord_values_are_not_accepted(
    bad_record: object,
) -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    with pytest.raises(FollowupProjectionError) as error:
        project_manual_followups(
            queue,
            cast(Mapping[str, ManualOutcomeRecord | None], {entry.target_url: bad_record}),
        )
    assert error.value.code == "FOLLOWUP_EVIDENCE_INVALID"


def test_bad_s4_record_account_and_fingerprint_are_rejected() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    handoff = _event(40, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    wrong_account = _record(entry, (handoff,), account_uuid=ACCOUNT_B)
    wrong_account_uuid = replace(_record(entry, (handoff,)), account_uuid="not-a-uuid")
    wrong_target = _record(
        entry,
        (handoff,),
        target_url="https://www.threads.com/@synthetic/post/OtherPost",
    )

    for record in (wrong_account, wrong_account_uuid, wrong_target):
        with pytest.raises(FollowupProjectionError) as error:
            project_manual_followups(queue, {entry.target_url: record})
        assert error.value.code == "FOLLOWUP_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    "record",
    [
        lambda entry: _record(
            entry,
            (
                replace(
                    _event(50, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                    verification=cast(object, "REMOTE_VERIFIED"),
                ),
            ),
        ),
        lambda entry: _record(
            entry,
            (
                replace(
                    _event(51, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                    source=cast(object, "UNKNOWN"),
                ),
            ),
        ),
        lambda entry: _record(
            entry,
            (
                replace(
                    _event(52, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                    recorded_at="not-a-time",
                ),
            ),
        ),
        lambda entry: _record(
            entry,
            (
                _event(53, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                _event(
                    54,
                    status="ABORTED_NOT_SENT",
                    source="OPERATOR",
                    attempt_id=_uuid(99_999),
                ),
            ),
        ),
        lambda entry: _record(
            entry,
            (_event(55, status="ABORTED_NOT_SENT", source="OPERATOR"),),
        ),
        lambda entry: _record(
            entry,
            (
                _event(56, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                _event(57, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
            ),
        ),
        lambda entry: _record(
            entry,
            (
                replace(
                    _event(58, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                    event_id="not-a-uuid",
                ),
            ),
        ),
        lambda entry: _record(
            entry,
            (
                replace(
                    _event(59, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"),
                    attempt_id="not-a-uuid",
                ),
            ),
        ),
        lambda entry: _record(
            entry,
            (_event(60, status="REMOTE_VERIFIED", source="SOFTWARE_HANDOFF"),),
        ),
        lambda entry: _record(
            entry,
            (
                _event(
                    61,
                    status="HANDOFF_UNRESOLVED",
                    source="SOFTWARE_HANDOFF",
                    draft_sha256="not-a-digest",
                ),
            ),
        ),
    ],
)
def test_contradictory_or_malformed_event_histories_are_rejected(
    record: Callable[[OpportunityQueueEntry], ManualOutcomeRecord],
) -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    s4_record = record(entry)
    with pytest.raises(FollowupProjectionError) as error:
        project_manual_followups(queue, {entry.target_url: s4_record})
    assert error.value.code == "FOLLOWUP_EVIDENCE_INVALID"


def test_mismatched_operator_draft_and_duplicate_event_ids_are_rejected() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    handoff = _event(60, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF")
    operator = _event(
        61,
        status="AMBIGUOUS",
        source="OPERATOR",
        attempt_id=handoff.attempt_id,
        draft_sha256="b" * 64,
    )
    duplicate_id = replace(
        _event(
            62,
            status="ABORTED_NOT_SENT",
            source="OPERATOR",
            attempt_id=handoff.attempt_id,
            draft_sha256=handoff.draft_sha256,
        ),
        event_id=handoff.event_id,
    )

    for record in (
        _record(entry, (handoff, operator)),
        _record(entry, (handoff, replace(duplicate_id, draft_sha256=handoff.draft_sha256))),
    ):
        with pytest.raises(FollowupProjectionError):
            project_manual_followups(queue, {entry.target_url: record})


def test_s4_reader_compatible_utc_timestamp_is_accepted() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    handoff = _event(
        63,
        status="HANDOFF_UNRESOLVED",
        source="SOFTWARE_HANDOFF",
        recorded_at="2026-10-12 08:00:00Z",
    )

    result = project_manual_followups(queue, {entry.target_url: _record(entry, (handoff,))})

    assert result.rows[0].state == "NEEDS_VERIFICATION"
    assert result.rows[0].latest_event_at == "2026-10-12 08:00:00Z"


def test_oversized_s4_history_and_empty_record_are_rejected() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    events: list[ManualOutcomeEvent] = []
    for index in range(8):
        handoff = _event(
            70 + index * 2,
            status="HANDOFF_UNRESOLVED",
            source="SOFTWARE_HANDOFF",
        )
        aborted = _event(
            71 + index * 2,
            status="ABORTED_NOT_SENT",
            source="OPERATOR",
            attempt_id=handoff.attempt_id,
            draft_sha256=handoff.draft_sha256,
        )
        events.extend((handoff, aborted))
    events.append(_event(90, status="HANDOFF_UNRESOLVED", source="SOFTWARE_HANDOFF"))
    oversized = _record(entry, tuple(events))
    empty = _record(entry, ())

    for record in (oversized, empty):
        with pytest.raises(FollowupProjectionError) as error:
            project_manual_followups(queue, {entry.target_url: record})
        assert error.value.code == "FOLLOWUP_EVIDENCE_INVALID"


def test_projection_results_and_errors_do_not_expose_target_or_context() -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    record = _record_with_latest_status(entry, "OPERATOR_REPORTED_SENT_UNVERIFIED")
    result = project_manual_followups(queue, {entry.target_url: record})

    rendered = repr(result) + repr(result.rows[0])
    assert entry.target_url not in rendered
    assert PRIVATE_CONTEXT not in rendered
    assert "OPERATOR_REPORTED_SENT_UNVERIFIED" not in rendered
    assert "SENT" not in {row.state for row in result.rows}

    with pytest.raises(FollowupProjectionError) as error:
        project_manual_followups(queue, {"https://private.example/private": None})
    assert entry.target_url not in repr(error.value)
    assert PRIVATE_CONTEXT not in str(error.value)


def test_projection_performs_no_file_or_socket_io(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _queue()
    entry = queue.opportunities[0]
    record = _record_with_latest_status(entry, "AMBIGUOUS")

    def no_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected external side effect")

    for method in ("open", "read_bytes", "read_text", "write_bytes", "write_text", "mkdir"):
        monkeypatch.setattr(Path, method, no_io)
    monkeypatch.setattr(socket.socket, "connect", no_io)
    monkeypatch.setattr(socket, "create_connection", no_io)

    result = project_manual_followups(queue, {entry.target_url: record})

    assert result.rows[0].state == "NEEDS_VERIFICATION"


def test_module_import_does_not_import_or_start_playwright() -> None:
    code = (
        "import sys; "
        "import threads_platform.standalone.recruitment_followup_projection; "
        "assert not any(name == 'playwright' or name.startswith('playwright.') "
        "for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, "pure module import boundary failed"
