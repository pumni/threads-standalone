"""Synthetic regressions for existing S4/S6/S9A contracts.

The S4 reservation calls below test S4 in isolation. They do not represent an
atomic transition from an S9A approval to an S4 reservation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest

from threads_platform.standalone.manual_opportunities import (
    ManualOpportunity,
    ManualOpportunityIntake,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeError, ManualOutcomeStore
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueueError,
    OpportunityQueueStore,
)
from threads_platform.standalone.recruitment_context_approval import (
    ExactContextApprovalError,
    approve_exact_context_review,
    build_exact_context_review_packet,
    preflight_exact_context_approval,
)
from threads_platform.standalone.recruitment_qualification import rank_opportunities
from threads_platform.standalone.recruitment_reply_assistant import (
    OpportunityReplyEditorialSession,
    build_opportunity_reply_preview,
)
from threads_platform.standalone.recruitment_reply_proposal import DeterministicFakeReplyGenerator

ACCOUNT_A = UUID("123e4567-e89b-42d3-a456-426614174000")
ACCOUNT_B = UUID("123e4567-e89b-42d3-a456-426614174001")
TARGET = "https://www.threads.com/@synthetic/post/AbC123"
CONTEXT = "Mình đang tìm việc IT remote. Nội dung synthetic do operator cung cấp."
UPDATED_CONTEXT = "Mình đang tìm việc IT bán thời gian. Nội dung synthetic do operator cung cấp."
DRAFT = "Bạn có thể chia sẻ thêm về công việc IT bạn đang tìm không?"
DRAFT_SHA256 = hashlib.sha256(DRAFT.encode("utf-8")).hexdigest()
APPROVED_ATTEMPT_ID = "323e4567-e89b-42d3-a456-426614174000"
NOW = 1_800_000_000


def _intake(target_url: str = TARGET) -> ManualOpportunityIntake:
    candidate = ManualOpportunity(
        input_position=1,
        source_line=1,
        target_url=target_url,
        author_username="synthetic",
    )
    return ManualOpportunityIntake(candidates=(candidate,), duplicates=())


def _local_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic-local-data"
    root.mkdir()
    return root


def _ready_queue(root: Path, account_id: UUID = ACCOUNT_A):
    store = OpportunityQueueStore(root)
    store.import_candidates(
        account_id,
        _intake(),
        context=CONTEXT,
        context_completeness="COMPLETE",
    )
    return store, store.list(account_id)


async def _approved_review(queue, *, account_id: UUID, s4_outcomes):
    entry = queue.opportunities[0]
    qualification = rank_opportunities(queue, limit=20).items[0]
    preview = await build_opportunity_reply_preview(
        entry,
        qualification,
        DeterministicFakeReplyGenerator(),
        account_uuid=str(account_id),
    )
    session = OpportunityReplyEditorialSession.begin(preview).edit(DRAFT)
    packet = build_exact_context_review_packet(
        queue,
        session,
        account_uuid=str(account_id),
        s4_outcomes=s4_outcomes,
    )
    approval = approve_exact_context_review(
        packet,
        confirmation=packet.confirmation_phrase,
        attempt_id=APPROVED_ATTEMPT_ID,
        now_epoch_seconds=NOW,
    )
    return session, packet, approval


@pytest.mark.parametrize(
    "blocking_outcome",
    ["AMBIGUOUS", "OPERATOR_REPORTED_SENT_UNVERIFIED"],
)
def test_s4_pending_and_blocking_outcomes_block_replay_after_restart(
    tmp_path: Path,
    blocking_outcome: Literal["AMBIGUOUS", "OPERATOR_REPORTED_SENT_UNVERIFIED"],
) -> None:
    root = _local_data_root(tmp_path)
    store = ManualOutcomeStore(root)
    attempt = store.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)

    assert attempt.status == "HANDOFF_UNRESOLVED"
    assert attempt.source == "SOFTWARE_HANDOFF"
    assert attempt.verification == "UNVERIFIED"
    assert attempt.attempt_id == attempt.event_id
    restarted = ManualOutcomeStore(root)
    assert restarted.read(ACCOUNT_A, TARGET).latest == attempt

    with pytest.raises(ManualOutcomeError) as pending_replay:
        restarted.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)
    assert pending_replay.value.code == "MANUAL_OUTCOME_BLOCKED"

    blocking_event, repeated = restarted.resolve_handoff(
        ACCOUNT_A,
        TARGET,
        DRAFT_SHA256,
        attempt.attempt_id,
        blocking_outcome,
    )
    assert not repeated
    assert blocking_event.status == blocking_outcome
    assert blocking_event.verification == "UNVERIFIED"
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET).latest == blocking_event
    with pytest.raises(ManualOutcomeError) as blocked_replay:
        restarted.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)
    assert blocked_replay.value.code == "MANUAL_OUTCOME_BLOCKED"


def test_s4_abort_is_local_only_and_allows_a_new_separate_attempt(tmp_path: Path) -> None:
    root = _local_data_root(tmp_path)
    store = ManualOutcomeStore(root)
    first = store.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)
    aborted, repeated = store.resolve_handoff(
        ACCOUNT_A,
        TARGET,
        DRAFT_SHA256,
        first.attempt_id,
        "ABORTED_NOT_SENT",
    )

    assert not repeated
    assert aborted.status == "ABORTED_NOT_SENT"
    assert aborted.source == "OPERATOR"
    assert aborted.verification == "UNVERIFIED"

    second = store.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)
    assert second.status == "HANDOFF_UNRESOLVED"
    assert second.attempt_id != first.attempt_id
    record = ManualOutcomeStore(root).read(ACCOUNT_A, TARGET)
    assert [event.status for event in record.events] == [
        "HANDOFF_UNRESOLVED",
        "ABORTED_NOT_SENT",
        "HANDOFF_UNRESOLVED",
    ]
    assert all(event.verification == "UNVERIFIED" for event in record.events)


def test_s6_and_s4_share_a_fail_fast_account_lock_but_isolate_other_accounts(
    tmp_path: Path,
) -> None:
    root = _local_data_root(tmp_path)
    queue_store, _ = _ready_queue(root, ACCOUNT_A)
    queue_store.import_candidates(
        ACCOUNT_B,
        _intake(),
        context=CONTEXT,
        context_completeness="COMPLETE",
    )
    outcome_store = ManualOutcomeStore(root)

    with outcome_store.account_lock(ACCOUNT_A):
        with pytest.raises(OpportunityQueueError) as queue_busy:
            queue_store.list(ACCOUNT_A)
        assert queue_busy.value.code == "OPPORTUNITY_QUEUE_BUSY"
        assert queue_store.list(ACCOUNT_B).opportunities[0].target_url == TARGET
        other_account_attempt = outcome_store.begin_handoff(
            ACCOUNT_B,
            TARGET,
            DRAFT_SHA256,
        )
        assert other_account_attempt.status == "HANDOFF_UNRESOLVED"

        with pytest.raises(ManualOutcomeError) as outcome_busy:
            outcome_store.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)
        assert outcome_busy.value.code == "MANUAL_OUTCOME_BUSY"

    assert outcome_store.read(ACCOUNT_A, TARGET) is None
    assert outcome_store.read(ACCOUNT_B, TARGET).latest == other_account_attempt

    with queue_store.account_lock(ACCOUNT_A):
        with pytest.raises(ManualOutcomeError) as reverse_busy:
            outcome_store.begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)
        assert reverse_busy.value.code == "MANUAL_OUTCOME_BUSY"


def test_s6_keeps_an_entry_when_its_s4_handoff_is_unresolved_after_restart(
    tmp_path: Path,
) -> None:
    root = _local_data_root(tmp_path)
    _, queue = _ready_queue(root)
    entry = queue.opportunities[0]
    ManualOutcomeStore(root).begin_handoff(ACCOUNT_A, TARGET, DRAFT_SHA256)

    restarted_queue = OpportunityQueueStore(root)
    with pytest.raises(OpportunityQueueError) as unresolved:
        restarted_queue.remove(ACCOUNT_A, entry.id)
    assert unresolved.value.code == "OPPORTUNITY_QUEUE_HANDOFF_UNRESOLVED"
    assert restarted_queue.get(ACCOUNT_A, entry.id).target_url == TARGET


@pytest.mark.asyncio
async def test_s9a_preflight_rejects_a_changed_s6_snapshot_without_s4_writes(
    tmp_path: Path,
) -> None:
    root = _local_data_root(tmp_path)
    queue_store, original_queue = _ready_queue(root)
    session, _, approval = await _approved_review(
        original_queue,
        account_id=ACCOUNT_A,
        s4_outcomes={},
    )

    permit = preflight_exact_context_approval(
        original_queue,
        session,
        approval,
        account_uuid=str(ACCOUNT_A),
        s4_outcomes={},
        attempt_id=APPROVED_ATTEMPT_ID,
        now_epoch_seconds=NOW + 1,
    )
    assert permit.attempt_id == APPROVED_ATTEMPT_ID
    assert "handoff=<not performed>" in repr(permit)

    entry = original_queue.opportunities[0]
    queue_store.select(
        ACCOUNT_A,
        entry.id,
        context=UPDATED_CONTEXT,
        update_context=True,
        context_completeness="COMPLETE",
    )
    fresh_queue = queue_store.list(ACCOUNT_A)
    with pytest.raises(ExactContextApprovalError) as stale:
        preflight_exact_context_approval(
            fresh_queue,
            session,
            approval,
            account_uuid=str(ACCOUNT_A),
            s4_outcomes={},
            attempt_id=APPROVED_ATTEMPT_ID,
            now_epoch_seconds=NOW + 1,
        )
    assert stale.value.code in {"S9A_CONTEXT_INVALID", "S9A_APPROVAL_INVALID"}
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) is None


@pytest.mark.asyncio
async def test_s9a_rejects_changed_full_s4_history_with_the_same_latest_abort(
    tmp_path: Path,
) -> None:
    root = _local_data_root(tmp_path)
    _, queue = _ready_queue(root)
    outcomes = ManualOutcomeStore(root)

    prior = outcomes.begin_handoff(ACCOUNT_A, TARGET, "a" * 64)
    outcomes.resolve_handoff(
        ACCOUNT_A,
        TARGET,
        "a" * 64,
        prior.attempt_id,
        "ABORTED_NOT_SENT",
    )
    record_before = outcomes.read(ACCOUNT_A, TARGET)
    assert record_before.latest.status == "ABORTED_NOT_SENT"
    session, _, approval = await _approved_review(
        queue,
        account_id=ACCOUNT_A,
        s4_outcomes={TARGET: record_before},
    )

    # This independent S4 attempt models an intervening local history change.
    # It is not the attempt approved by S9A and is not an atomic handoff.
    intervening = outcomes.begin_handoff(ACCOUNT_A, TARGET, "b" * 64)
    assert intervening.attempt_id != approval.attempt_id
    outcomes.resolve_handoff(
        ACCOUNT_A,
        TARGET,
        "b" * 64,
        intervening.attempt_id,
        "ABORTED_NOT_SENT",
    )
    record_after = outcomes.read(ACCOUNT_A, TARGET)
    assert record_after.latest.status == record_before.latest.status
    assert len(record_after.events) > len(record_before.events)

    with pytest.raises(ExactContextApprovalError) as stale_history:
        preflight_exact_context_approval(
            queue,
            session,
            approval,
            account_uuid=str(ACCOUNT_A),
            s4_outcomes={TARGET: record_after},
            attempt_id=APPROVED_ATTEMPT_ID,
            now_epoch_seconds=NOW + 1,
        )
    assert stale_history.value.code == "S9A_APPROVAL_INVALID"
