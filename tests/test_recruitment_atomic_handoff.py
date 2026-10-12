from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from playwright import sync_api as playwright_sync

import threads_platform.standalone.recruitment_atomic_handoff as atomic_handoff
from threads_platform.locks.filesystem import FilesystemProcessLock, ProcessAlreadyRunning
from threads_platform.standalone.manual_opportunities import load_manual_opportunities
from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeError,
    ManualOutcomeEvent,
    ManualOutcomeRecord,
    ManualOutcomeStore,
)
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueueError,
    OpportunityQueueStore,
)
from threads_platform.standalone.recruitment_context_approval import (
    approve_exact_context_review,
    build_exact_context_review_packet,
)
from threads_platform.standalone.recruitment_qualification import rank_opportunities
from threads_platform.standalone.recruitment_reply_assistant import (
    OpportunityReplyEditorialSession,
    build_opportunity_reply_preview,
)
from threads_platform.standalone.recruitment_reply_proposal import DeterministicFakeReplyGenerator

ACCOUNT_A = UUID("123e4567-e89b-42d3-a456-426614174000")
ACCOUNT_B = UUID("123e4567-e89b-42d3-a456-426614174001")
ATTEMPT_A = "323e4567-e89b-42d3-a456-426614174000"
ATTEMPT_B = "323e4567-e89b-42d3-a456-426614174001"
TARGET = "https://www.threads.com/@synthetic/post/S9B2B123"
CONTEXT = "Mình đang tìm việc IT remote. Nội dung do operator cung cấp."
DRAFT = "Bạn có thể chia sẻ thêm về công việc IT từ xa mà bạn đang tìm không?"
OTHER_DRAFT = "Bạn đang tìm công việc ở lĩnh vực nào?"
NOW = 1_800_000_000
RECORDED_AT = "2027-01-15T08:00:00Z"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic-local-data"
    root.mkdir(parents=True)
    return root


def _seed_queue(root: Path, account_id: UUID, *, context: str = CONTEXT) -> object:
    target_file = root.parent / f"targets-{account_id}.txt"
    target_file.write_text(f"{TARGET}\n", encoding="utf-8", newline="")
    intake = load_manual_opportunities(target_file)
    queue_store = OpportunityQueueStore(root)
    queue_store.import_candidates(
        account_id,
        intake,
        context=context,
        context_completeness="COMPLETE",
    )
    return queue_store.list(account_id)


async def _session(queue: object, account_id: UUID, *, draft: str = DRAFT):
    ranking = rank_opportunities(queue, limit=20)
    qualification = ranking.items[0]
    entry = queue.opportunities[0]
    preview = await build_opportunity_reply_preview(
        entry,
        qualification,
        DeterministicFakeReplyGenerator(),
        account_uuid=str(account_id),
    )
    return OpportunityReplyEditorialSession.begin(preview).edit(draft)


def _seed_legacy_abort(
    store: ManualOutcomeStore,
    account_id: UUID,
    target: str = TARGET,
    *,
    event_id: str = "423e4567-e89b-42d3-a456-426614174000",
    recorded_at: str = RECORDED_AT,
) -> ManualOutcomeRecord:
    fingerprint = store.target_fingerprint(account_id, target)
    event = ManualOutcomeEvent(
        event_id=event_id,
        attempt_id=event_id,
        draft_sha256=hashlib.sha256(b"legacy synthetic draft").hexdigest(),
        recorded_at=recorded_at,
        status="ABORTED_NOT_SENT",
        source="LEGACY_OPERATOR",
        verification="UNVERIFIED",
    )
    record = ManualOutcomeRecord(str(account_id), fingerprint, (event,))
    store._write(record)
    return record


async def _approved(
    root: Path,
    account_id: UUID = ACCOUNT_A,
    *,
    attempt_id: str = ATTEMPT_A,
    context: str = CONTEXT,
    draft: str = DRAFT,
    current_record: ManualOutcomeRecord | None = None,
    keep: bool = False,
):
    queue = _seed_queue(root, account_id, context=context)
    session = await _session(queue, account_id, draft=draft)
    if keep:
        session = session.keep_for_session()
    selected = queue.opportunities[0]
    packet = build_exact_context_review_packet(
        queue,
        session,
        account_uuid=str(account_id),
        s4_outcomes={selected.target_url: current_record},
    )
    approval = approve_exact_context_review(
        packet,
        confirmation=packet.confirmation_phrase,
        attempt_id=attempt_id,
        now_epoch_seconds=NOW,
    )
    return queue, selected, session, approval


def _patch_clock(monkeypatch: pytest.MonkeyPatch, seconds: int = NOW + 1) -> None:
    monkeypatch.setattr(
        atomic_handoff,
        "_trusted_utc_now",
        lambda: datetime.fromtimestamp(seconds, UTC),
    )


@pytest.mark.asyncio
async def test_success_uses_one_s4_lock_and_writes_exact_approved_v1_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, selected, session, approval = await _approved(root, keep=True)
    _patch_clock(monkeypatch)

    acquired: list[FilesystemProcessLock] = []
    original_acquire = FilesystemProcessLock.acquire

    def count_acquire(lock: FilesystemProcessLock) -> None:
        original_acquire(lock)
        acquired.append(lock)

    monkeypatch.setattr(FilesystemProcessLock, "acquire", count_acquire)

    def no_legacy_handoff(*args: object, **kwargs: object) -> None:
        raise AssertionError("begin_handoff must not run inside the transaction")

    monkeypatch.setattr(ManualOutcomeStore, "begin_handoff", no_legacy_handoff)
    monkeypatch.setattr(socket, "create_connection", no_legacy_handoff)
    monkeypatch.setattr(socket.socket, "connect", no_legacy_handoff)
    monkeypatch.setattr(playwright_sync, "sync_playwright", no_legacy_handoff)

    def trusted_clock() -> datetime:
        assert len(acquired) == 1
        assert acquired[0].held
        return datetime.fromtimestamp(NOW + 1, UTC)

    monkeypatch.setattr(atomic_handoff, "_trusted_utc_now", trusted_clock)
    receipt = atomic_handoff.reserve_exact_approved_handoff(
        ACCOUNT_A,
        approval,
        session,
        data_root=root,
    )

    assert len(acquired) == 1
    assert receipt.account_uuid == str(ACCOUNT_A)
    assert receipt.opportunity_id == selected.id
    assert receipt.target_url == TARGET
    assert receipt.attempt_id == ATTEMPT_A
    assert receipt.event_id == ATTEMPT_A
    assert receipt.draft_sha256 == approval.draft_sha256
    assert receipt.status == "HANDOFF_UNRESOLVED"
    assert receipt.verification == "UNVERIFIED"
    assert receipt.send_status == "NOT_SENT"
    assert receipt.actor_identity == "UNKNOWN_IDENTITY"
    assert TARGET not in repr(receipt)
    assert CONTEXT not in repr(receipt)
    assert DRAFT not in repr(receipt)

    store = ManualOutcomeStore(root)
    record = store.read(ACCOUNT_A, TARGET)
    assert record is not None
    assert len(record.events) == 1
    event = record.latest
    assert event.event_id == event.attempt_id == ATTEMPT_A
    assert event.draft_sha256 == approval.draft_sha256
    assert event.recorded_at == datetime.fromtimestamp(NOW + 1, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (event.status, event.source, event.verification) == (
        "HANDOFF_UNRESOLVED",
        "SOFTWARE_HANDOFF",
        "UNVERIFIED",
    )
    path = (
        root
        / "manual_recruitment_v1"
        / "records"
        / str(ACCOUNT_A)
        / f"{record.target_fingerprint}.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {"version", "account_uuid", "target_fingerprint", "events"}
    assert document["version"] == 1
    assert set(document["events"][0]) == {
        "event_id",
        "attempt_id",
        "draft_sha256",
        "recorded_at",
        "status",
        "source",
        "verification",
    }


@pytest.mark.asyncio
async def test_approved_kept_for_session_draft_requires_separate_s9a_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root, keep=True)
    assert session.disposition == "KEPT_FOR_SESSION"
    _patch_clock(monkeypatch)

    receipt = atomic_handoff.reserve_exact_approved_handoff(
        ACCOUNT_A, approval, session, data_root=root
    )
    assert receipt.status == "HANDOFF_UNRESOLVED"


@pytest.mark.asyncio
async def test_fresh_full_aborted_s4_history_allows_one_new_exact_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    queue = _seed_queue(root, ACCOUNT_A)
    selected = queue.opportunities[0]
    store = ManualOutcomeStore(root)
    prior_pending = store.begin_handoff(ACCOUNT_A, selected.target_url, "a" * 64)
    store.resolve_handoff(
        ACCOUNT_A,
        selected.target_url,
        "a" * 64,
        prior_pending.attempt_id,
        "ABORTED_NOT_SENT",
    )
    previous = store.read(ACCOUNT_A, selected.target_url)
    assert previous is not None
    approved_queue, _, session, approval = await _approved(
        root,
        current_record=previous,
    )
    assert approved_queue == queue
    _patch_clock(monkeypatch)

    receipt = atomic_handoff.reserve_exact_approved_handoff(
        ACCOUNT_A, approval, session, data_root=root
    )
    current = store.read(ACCOUNT_A, selected.target_url)
    assert current is not None
    assert current.events[: len(previous.events)] == previous.events
    assert current.latest.attempt_id == receipt.attempt_id == ATTEMPT_A
    assert current.latest.event_id == receipt.event_id == ATTEMPT_A
    assert current.latest.status == "HANDOFF_UNRESOLVED"


@pytest.mark.asyncio
async def test_wrong_account_is_rejected_before_lock_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    acquisitions = 0

    def count_acquire(_: FilesystemProcessLock) -> None:
        nonlocal acquisitions
        acquisitions += 1

    monkeypatch.setattr(FilesystemProcessLock, "acquire", count_acquire)
    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_B, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_INVALID"
    assert acquisitions == 0
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) is None
    assert ManualOutcomeStore(root).read(ACCOUNT_B, TARGET) is None


@pytest.mark.asyncio
async def test_wrong_target_and_changed_final_draft_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    queue, selected, session, approval = await _approved(root)
    _patch_clock(monkeypatch)
    wrong_target = replace(approval, target_url="https://www.threads.com/@synthetic/post/Other")
    with pytest.raises(atomic_handoff.AtomicHandoffError) as target_error:
        atomic_handoff.reserve_exact_approved_handoff(
            ACCOUNT_A, wrong_target, session, data_root=root
        )
    assert target_error.value.code == "ATOMIC_HANDOFF_STALE_APPROVAL"

    changed_session = await _session(queue, ACCOUNT_A, draft=OTHER_DRAFT)
    with pytest.raises(atomic_handoff.AtomicHandoffError) as draft_error:
        atomic_handoff.reserve_exact_approved_handoff(
            ACCOUNT_A, approval, changed_session, data_root=root
        )
    assert draft_error.value.code in {
        "ATOMIC_HANDOFF_STALE_APPROVAL",
        "ATOMIC_HANDOFF_INVALID",
    }
    assert ManualOutcomeStore(root).read(ACCOUNT_A, selected.target_url) is None


@pytest.mark.asyncio
async def test_full_context_change_after_character_500_invalidates_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    long_context = "Mình đang tìm việc IT remote. " + ("synthetic evidence " * 55)
    queue, selected, session, approval = await _approved(root, context=long_context)
    changed_context = long_context[:-1] + "!"
    OpportunityQueueStore(root).select(
        ACCOUNT_A,
        selected.id,
        context=changed_context,
        update_context=True,
        context_completeness="COMPLETE",
    )
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code in {
        "ATOMIC_HANDOFF_STALE_APPROVAL",
        "ATOMIC_HANDOFF_INVALID",
    }
    assert ManualOutcomeStore(root).read(ACCOUNT_A, selected.target_url) is None
    assert queue.opportunities[0].context != changed_context


@pytest.mark.asyncio
async def test_changed_queue_during_locked_preflight_is_detected_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, selected, session, approval = await _approved(root)
    original_clock = datetime.fromtimestamp(NOW + 1, UTC)

    def mutate_queue_while_transaction_holds_lock() -> datetime:
        queue_store = OpportunityQueueStore(root)
        queue_store._write_unlocked(
            root,
            replace(
                queue_store._read_unlocked(root, ACCOUNT_A),
                opportunities=(
                    replace(
                        queue_store._read_unlocked(root, ACCOUNT_A).opportunities[0],
                        updated_at="2027-01-15T08:00:00Z",
                    ),
                ),
            ),
        )
        return original_clock

    monkeypatch.setattr(
        atomic_handoff, "_trusted_utc_now", mutate_queue_while_transaction_holds_lock
    )
    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_STALE_APPROVAL"
    assert ManualOutcomeStore(root).read(ACCOUNT_A, selected.target_url) is None


@pytest.mark.asyncio
async def test_s4_change_between_snapshot_and_write_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, selected, session, approval = await _approved(root)
    _patch_clock(monkeypatch)
    original_preflight = atomic_handoff.preflight_exact_context_approval
    store = ManualOutcomeStore(root)

    def write_intervening_history(*args: object, **kwargs: object):
        _seed_legacy_abort(store, ACCOUNT_A, selected.target_url)
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(
        atomic_handoff,
        "preflight_exact_context_approval",
        write_intervening_history,
    )
    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_STALE_APPROVAL"
    record = store.read(ACCOUNT_A, selected.target_url)
    assert record is not None
    assert record.latest.status == "ABORTED_NOT_SENT"
    assert len(record.events) == 1


@pytest.mark.asyncio
async def test_expired_approval_is_rejected_without_s4_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    _patch_clock(monkeypatch, NOW + 300)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_STALE_APPROVAL"
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ("AMBIGUOUS", "OPERATOR_REPORTED_SENT_UNVERIFIED"),
)
async def test_blocking_s4_outcomes_remain_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    root = _root(tmp_path)
    _, selected, session, approval = await _approved(root)
    store = ManualOutcomeStore(root)
    pending = store.begin_handoff(ACCOUNT_A, selected.target_url, "a" * 64)
    store.resolve_handoff(
        ACCOUNT_A,
        selected.target_url,
        "a" * 64,
        pending.attempt_id,
        outcome,  # type: ignore[arg-type]
    )
    blocked_snapshot = store.read(ACCOUNT_A, selected.target_url)
    assert blocked_snapshot is not None
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_BLOCKED"
    assert store.read(ACCOUNT_A, selected.target_url) == blocked_snapshot


@pytest.mark.asyncio
async def test_unresolved_s4_handoff_remains_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, selected, session, approval = await _approved(root)
    store = ManualOutcomeStore(root)
    pending = store.begin_handoff(ACCOUNT_A, selected.target_url, "a" * 64)
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_BLOCKED"
    after = store.read(ACCOUNT_A, selected.target_url)
    assert after is not None
    assert after.latest == pending


@pytest.mark.asyncio
async def test_prior_aborted_attempt_id_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    store = ManualOutcomeStore(root)
    prior = _seed_legacy_abort(store, ACCOUNT_A, event_id=ATTEMPT_A)
    _, selected, session, approval = await _approved(
        root,
        attempt_id=ATTEMPT_A,
        current_record=prior,
    )
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_REPLAY"
    assert store.read(ACCOUNT_A, selected.target_url) == prior


@pytest.mark.asyncio
async def test_attempt_id_cannot_be_replayed_on_a_different_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, selected, session, approval = await _approved(root)
    store = ManualOutcomeStore(root)
    other_target = "https://www.threads.com/@synthetic/post/OtherTarget"
    other_record = _seed_legacy_abort(
        store,
        ACCOUNT_A,
        other_target,
        event_id=ATTEMPT_A,
    )
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_REPLAY"
    assert store.read(ACCOUNT_A, selected.target_url) is None
    assert store.read(ACCOUNT_A, other_target) == other_record


@pytest.mark.asyncio
async def test_corrupt_s4_history_for_same_account_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    store = ManualOutcomeStore(root)
    _seed_legacy_abort(
        store,
        ACCOUNT_A,
        "https://www.threads.com/@synthetic/post/CorruptNeighbor",
    )
    account_dir = root / "manual_recruitment_v1" / "records" / str(ACCOUNT_A)
    (account_dir / f"{'f' * 64}.json").write_text("{\n", encoding="utf-8")
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_INVALID"
    assert store.read(ACCOUNT_A, TARGET) is None


@pytest.mark.asyncio
async def test_s4_full_history_change_with_same_aborted_status_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    store = ManualOutcomeStore(root)
    original = _seed_legacy_abort(store, ACCOUNT_A)
    _, selected, session, approval = await _approved(
        root,
        current_record=original,
    )
    replacement = ManualOutcomeEvent(
        event_id="523e4567-e89b-42d3-a456-426614174000",
        attempt_id="523e4567-e89b-42d3-a456-426614174000",
        draft_sha256=hashlib.sha256(b"another synthetic declaration").hexdigest(),
        recorded_at="2027-01-15T08:01:00Z",
        status="ABORTED_NOT_SENT",
        source="LEGACY_OPERATOR",
        verification="UNVERIFIED",
    )
    changed = replace(original, events=(*original.events, replacement))
    store._write(changed)
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code in {
        "ATOMIC_HANDOFF_STALE_APPROVAL",
        "ATOMIC_HANDOFF_INVALID",
    }
    assert store.read(ACCOUNT_A, selected.target_url) == changed


@pytest.mark.asyncio
async def test_busy_account_lock_fails_without_retry_or_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    acquisitions = 0

    def busy(_: FilesystemProcessLock) -> None:
        nonlocal acquisitions
        acquisitions += 1
        raise ProcessAlreadyRunning("synthetic lock contention")

    monkeypatch.setattr(FilesystemProcessLock, "acquire", busy)
    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_BUSY"
    assert acquisitions == 1
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) is None


@pytest.mark.asyncio
async def test_atomic_replace_failure_preserves_prior_journal_and_cleans_own_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    store = ManualOutcomeStore(root)
    prior = _seed_legacy_abort(store, ACCOUNT_A)
    _, selected, session, approval = await _approved(root, current_record=prior)
    _patch_clock(monkeypatch)

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("threads_platform.standalone.manual_outcomes.os.replace", fail_replace)
    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_INVALID"
    assert store.read(ACCOUNT_A, selected.target_url) == prior
    account_dir = root / "manual_recruitment_v1" / "records" / str(ACCOUNT_A)
    assert not tuple(account_dir.glob("*.tmp"))


@pytest.mark.asyncio
async def test_two_accounts_with_same_target_have_isolated_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session_a, approval_a = await _approved(root, ACCOUNT_A, attempt_id=ATTEMPT_A)
    _, _, session_b, approval_b = await _approved(root, ACCOUNT_B, attempt_id=ATTEMPT_B)
    _patch_clock(monkeypatch)

    atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval_a, session_a, data_root=root)
    record_a = ManualOutcomeStore(root).read(ACCOUNT_A, TARGET)
    record_b_before = ManualOutcomeStore(root).read(ACCOUNT_B, TARGET)
    assert record_a is not None and record_a.latest.attempt_id == ATTEMPT_A
    assert record_b_before is None

    atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_B, approval_b, session_b, data_root=root)
    record_b = ManualOutcomeStore(root).read(ACCOUNT_B, TARGET)
    assert record_b is not None and record_b.latest.attempt_id == ATTEMPT_B
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) == record_a


@pytest.mark.asyncio
async def test_guarded_reads_reject_expired_wrong_account_and_wrong_root_leases(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _seed_queue(root, ACCOUNT_A)
    queue_store = OpportunityQueueStore(root)
    outcome_store = ManualOutcomeStore(root)
    other_root = _root(tmp_path / "other")

    with outcome_store.account_lock_lease(ACCOUNT_A) as lease:
        assert queue_store.read_under_account_lock(ACCOUNT_A, lock_lease=lease).account_uuid == str(
            ACCOUNT_A
        )
        assert (
            outcome_store.read_under_account_lock(
                ACCOUNT_A,
                TARGET,
                lock_lease=lease,
            )
            is None
        )
        with pytest.raises(OpportunityQueueError) as wrong_account:
            queue_store.read_under_account_lock(ACCOUNT_B, lock_lease=lease)
        assert wrong_account.value.code == "OPPORTUNITY_QUEUE_INVALID"
        with pytest.raises(ManualOutcomeError) as wrong_root:
            ManualOutcomeStore(other_root).read_under_account_lock(
                ACCOUNT_A,
                TARGET,
                lock_lease=lease,
            )
        assert wrong_root.value.code == "MANUAL_OUTCOME_INVALID"

    with pytest.raises(OpportunityQueueError) as expired_queue_lease:
        queue_store.read_under_account_lock(ACCOUNT_A, lock_lease=lease)
    assert expired_queue_lease.value.code == "OPPORTUNITY_QUEUE_INVALID"
    with pytest.raises(ManualOutcomeError) as expired_s4_lease:
        outcome_store.read_under_account_lock(ACCOUNT_A, TARGET, lock_lease=lease)
    assert expired_s4_lease.value.code == "MANUAL_OUTCOME_INVALID"


@pytest.mark.asyncio
async def test_missing_and_invalid_clock_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    monkeypatch.setattr(atomic_handoff, "_trusted_utc_now", lambda: datetime.now())

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_CLOCK_INVALID"
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) is None


@pytest.mark.asyncio
async def test_approval_fingerprint_tampering_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    tampered = replace(approval, confirmation_sha256="0" * 64)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, tampered, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_STALE_APPROVAL"
    assert ManualOutcomeStore(root).read(ACCOUNT_A, TARGET) is None


@pytest.mark.asyncio
async def test_symlinked_s4_namespace_fails_closed_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    _, _, session, approval = await _approved(root)
    outside = tmp_path / "outside-synthetic"
    outside.mkdir()
    namespace = root / "manual_recruitment_v1"
    try:
        namespace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")
    _patch_clock(monkeypatch)

    with pytest.raises(atomic_handoff.AtomicHandoffError) as error:
        atomic_handoff.reserve_exact_approved_handoff(ACCOUNT_A, approval, session, data_root=root)
    assert error.value.code == "ATOMIC_HANDOFF_INVALID"
    assert not tuple(outside.iterdir())
