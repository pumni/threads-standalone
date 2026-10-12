"""Offline, single-lock S9A approval to S4 reservation transaction."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeError,
    ManualOutcomeStore,
)
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueueError,
    OpportunityQueueStore,
)
from threads_platform.standalone.recruitment_context_approval import (
    ExactContextApprovalError,
    ExactContextApprovalV2,
    preflight_exact_context_approval,
)
from threads_platform.standalone.recruitment_reply_assistant import (
    OpportunityReplyEditorialSession,
)


class AtomicHandoffError(Exception):
    """Fixed-code transaction failure that does not include private content."""

    _CODES = frozenset(
        {
            "ATOMIC_HANDOFF_INVALID",
            "ATOMIC_HANDOFF_BUSY",
            "ATOMIC_HANDOFF_BLOCKED",
            "ATOMIC_HANDOFF_STALE_APPROVAL",
            "ATOMIC_HANDOFF_REPLAY",
            "ATOMIC_HANDOFF_LIMIT_REACHED",
            "ATOMIC_HANDOFF_CLOCK_INVALID",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported atomic handoff error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ReservedApprovedHandoff:
    """Redacted local reservation receipt; it is never a send capability."""

    account_uuid: str
    opportunity_id: str
    target_url: str = field(repr=False)
    target_fingerprint: str
    attempt_id: str
    event_id: str
    draft_sha256: str
    packet_fingerprint: str
    approval_fingerprint: str
    checked_at_epoch_seconds: int
    status: str = "HANDOFF_UNRESOLVED"
    verification: str = "UNVERIFIED"
    send_status: str = "NOT_SENT"
    actor_identity: str = "UNKNOWN_IDENTITY"

    def __repr__(self) -> str:
        return (
            "ReservedApprovedHandoff("
            f"account_uuid={self.account_uuid!r}, opportunity_id={self.opportunity_id!r}, "
            "target_url=<redacted>, "
            f"target_fingerprint={self.target_fingerprint}, attempt_id={self.attempt_id!r}, "
            f"event_id={self.event_id!r}, draft_sha256={self.draft_sha256}, "
            f"approval_fingerprint={self.approval_fingerprint}, "
            "status=HANDOFF_UNRESOLVED, verification=UNVERIFIED, send_status=NOT_SENT, "
            "actor_identity=UNKNOWN_IDENTITY)"
        )


def _trusted_utc_now() -> datetime:
    return datetime.now(UTC)


def _fail(code: str) -> NoReturn:
    raise AtomicHandoffError(code)


def _raise_manual_error(error: ManualOutcomeError) -> NoReturn:
    if error.code == "MANUAL_OUTCOME_BUSY":
        _fail("ATOMIC_HANDOFF_BUSY")
    if error.code == "MANUAL_OUTCOME_BLOCKED":
        _fail("ATOMIC_HANDOFF_BLOCKED")
    if error.code == "MANUAL_OUTCOME_ATTEMPT_INVALID":
        _fail("ATOMIC_HANDOFF_REPLAY")
    if error.code in {"MANUAL_OUTCOME_APPROVAL_MISMATCH", "MANUAL_OUTCOME_ATTEMPT_MISMATCH"}:
        _fail("ATOMIC_HANDOFF_STALE_APPROVAL")
    if error.code == "MANUAL_OUTCOME_LIMIT_REACHED":
        _fail("ATOMIC_HANDOFF_LIMIT_REACHED")
    _fail("ATOMIC_HANDOFF_INVALID")


def _raise_queue_error(error: OpportunityQueueError) -> NoReturn:
    if error.code == "OPPORTUNITY_QUEUE_BUSY":
        _fail("ATOMIC_HANDOFF_BUSY")
    _fail("ATOMIC_HANDOFF_INVALID")


def _raise_approval_error(error: ExactContextApprovalError) -> NoReturn:
    if error.code == "S9A_S4_BLOCKED":
        _fail("ATOMIC_HANDOFF_BLOCKED")
    if error.code == "S9A_APPROVAL_EXPIRED":
        _fail("ATOMIC_HANDOFF_STALE_APPROVAL")
    if error.code == "S9A_APPROVAL_INVALID":
        _fail("ATOMIC_HANDOFF_STALE_APPROVAL")
    _fail("ATOMIC_HANDOFF_INVALID")


def _read_clock() -> tuple[datetime, int]:
    try:
        current = _trusted_utc_now()
        if (
            type(current) is not datetime
            or current.tzinfo is None
            or current.utcoffset() != UTC.utcoffset(current)
        ):
            _fail("ATOMIC_HANDOFF_CLOCK_INVALID")
        current = current.astimezone(UTC)
        epoch = int(current.timestamp())
        if not 0 <= epoch <= 253_402_300_799:
            _fail("ATOMIC_HANDOFF_CLOCK_INVALID")
        return current, epoch
    except AtomicHandoffError:
        raise
    except Exception:  # noqa: BLE001 — clock faults must fail closed.
        _fail("ATOMIC_HANDOFF_CLOCK_INVALID")


def _validate_initial_binding(
    account_id: UUID,
    approval: ExactContextApprovalV2,
    session: OpportunityReplyEditorialSession,
) -> None:
    if (
        type(account_id) is not UUID
        or account_id.version != 4
        or type(approval) is not ExactContextApprovalV2
        or type(session) is not OpportunityReplyEditorialSession
        or type(approval.account_uuid) is not str
        or approval.account_uuid != str(account_id)
        or type(approval.opportunity_id) is not str
        or type(approval.target_url) is not str
        or type(approval.attempt_id) is not str
    ):
        _fail("ATOMIC_HANDOFF_INVALID")
    try:
        opportunity_id = UUID(approval.opportunity_id)
        attempt_id = UUID(approval.attempt_id)
        ManualOutcomeStore.target_fingerprint(account_id, approval.target_url)
    except ValueError, TypeError, AttributeError, ManualOutcomeError:
        _fail("ATOMIC_HANDOFF_INVALID")
    if (
        opportunity_id.version != 4
        or str(opportunity_id) != approval.opportunity_id
        or attempt_id.version != 4
        or str(attempt_id) != approval.attempt_id
    ):
        _fail("ATOMIC_HANDOFF_INVALID")


def reserve_exact_approved_handoff(
    account_id: UUID,
    approval: ExactContextApprovalV2,
    current_editorial_session: OpportunityReplyEditorialSession,
    *,
    data_root: Path,
) -> ReservedApprovedHandoff:
    """Revalidate exact S9A evidence and durably reserve one S4 attempt offline.

    The caller supplies the exact S8 session reviewed in S9A. A fresh S6 queue
    and complete current S4 record are loaded under one shared account lock;
    S9A preflight then verifies the account, target, context, draft, attempt,
    qualification, full S4 snapshot and trusted expiry time before persistence.
    """

    _validate_initial_binding(account_id, approval, current_editorial_session)
    approval_snapshot = replace(approval)
    try:
        root = Path(data_root)
        outcomes = ManualOutcomeStore(root)
        queue_store = OpportunityQueueStore(root)
    except OSError, TypeError, ValueError:
        _fail("ATOMIC_HANDOFF_INVALID")

    try:
        with outcomes.account_lock_lease(account_id) as lease:
            queue = queue_store.read_under_account_lock(account_id, lock_lease=lease)
            selected = tuple(
                entry
                for entry in queue.opportunities
                if entry.id == approval_snapshot.opportunity_id
            )
            if len(selected) != 1:
                _fail("ATOMIC_HANDOFF_STALE_APPROVAL")
            entry = selected[0]
            if entry.target_url != approval_snapshot.target_url:
                _fail("ATOMIC_HANDOFF_STALE_APPROVAL")

            current_account_records = outcomes.read_account_under_account_lock(
                account_id,
                lock_lease=lease,
            )
            target_fingerprint = outcomes.target_fingerprint(account_id, entry.target_url)
            current_record = next(
                (
                    record
                    for record in current_account_records
                    if record.target_fingerprint == target_fingerprint
                ),
                None,
            )
            now, epoch = _read_clock()
            try:
                permit = preflight_exact_context_approval(
                    queue,
                    current_editorial_session,
                    approval_snapshot,
                    account_uuid=str(account_id),
                    s4_outcomes={entry.target_url: current_record},
                    attempt_id=approval_snapshot.attempt_id,
                    now_epoch_seconds=epoch,
                )
            except ExactContextApprovalError as error:
                _raise_approval_error(error)

            latest_queue = queue_store.read_under_account_lock(account_id, lock_lease=lease)
            latest_account_records = outcomes.read_account_under_account_lock(
                account_id,
                lock_lease=lease,
            )
            if latest_queue != queue or latest_account_records != current_account_records:
                _fail("ATOMIC_HANDOFF_STALE_APPROVAL")
            if (
                permit.account_uuid != str(account_id)
                or permit.opportunity_id != approval_snapshot.opportunity_id
                or permit.attempt_id != approval_snapshot.attempt_id
                or permit.packet_fingerprint != approval_snapshot.packet_fingerprint
                or permit.approval_fingerprint != approval_snapshot.fingerprint
            ):
                _fail("ATOMIC_HANDOFF_STALE_APPROVAL")

            event = outcomes.reserve_approved_handoff_under_account_lock(
                account_id,
                entry.target_url,
                approval_snapshot.draft_sha256,
                approval_snapshot.attempt_id,
                now,
                current_record,
                current_account_records,
                lock_lease=lease,
            )
    except AtomicHandoffError:
        raise
    except ManualOutcomeError as error:
        _raise_manual_error(error)
    except OpportunityQueueError as error:
        _raise_queue_error(error)
    except OSError:
        _fail("ATOMIC_HANDOFF_INVALID")

    return ReservedApprovedHandoff(
        account_uuid=str(account_id),
        opportunity_id=approval_snapshot.opportunity_id,
        target_url=entry.target_url,
        target_fingerprint=ManualOutcomeStore.target_fingerprint(account_id, entry.target_url),
        attempt_id=event.attempt_id,
        event_id=event.event_id,
        draft_sha256=event.draft_sha256,
        packet_fingerprint=permit.packet_fingerprint,
        approval_fingerprint=permit.approval_fingerprint,
        checked_at_epoch_seconds=permit.checked_at_epoch_seconds,
    )
