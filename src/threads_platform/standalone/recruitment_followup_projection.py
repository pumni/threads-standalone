"""Pure, local projection of S6 opportunities and supplied S4 outcome records."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeError,
    ManualOutcomeEvent,
    ManualOutcomeRecord,
    ManualOutcomeStore,
)
from threads_platform.standalone.opportunity_queue import OpportunityQueue, OpportunityQueueEntry

FollowupState = Literal["NO_FOLLOWUP", "NEEDS_VERIFICATION", "CLOSED"]
FollowupReasonCode = Literal[
    "NO_S4_RECORD_IN_SNAPSHOT",
    "HANDOFF_UNRESOLVED",
    "AMBIGUOUS_OUTCOME",
    "OPERATOR_SEND_UNVERIFIED",
    "LOCAL_ATTEMPT_ABORTED_NOT_SENT",
]
FollowupNextStep = Literal[
    "NONE",
    "CHECK_NATIVE_UI_PRIVATELY",
    "RESELECT_IF_STILL_RELEVANT",
]

_MAX_QUEUE_ENTRIES = 40
_MAX_ACTIVE_OPPORTUNITIES = 20
_MAX_CONTEXT_CHARS = 4_000
_MAX_SKIP_REASON_CHARS = 1_000
_MAX_EVENTS = 16
_MAX_TARGET_BYTES = 2_048
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OUTCOME_STATUSES = frozenset(
    {"ABORTED_NOT_SENT", "OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"}
)
_ALL_STATUSES = frozenset({"HANDOFF_UNRESOLVED", *_OUTCOME_STATUSES})
_EVENT_SOURCES = frozenset({"SOFTWARE_HANDOFF", "OPERATOR", "LEGACY_OPERATOR"})
_BLOCKING_LEGACY_STATUSES = frozenset({"OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"})
_QUEUE_STATES = frozenset({"NEEDS_CONTEXT", "READY", "SKIPPED"})
_CONTEXT_COMPLETENESS = frozenset({"UNKNOWN", "PARTIAL", "COMPLETE"})
_CONTEXT_PROVENANCE = frozenset({"NONE", "OPERATOR_SUPPLIED"})


class FollowupProjectionError(ValueError):
    """Projection failure with a fixed code and no source data."""

    _CODES = frozenset({"FOLLOWUP_QUEUE_INVALID", "FOLLOWUP_EVIDENCE_INVALID"})

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported follow-up projection error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class FollowupRow:
    """Minimal per-opportunity metadata; never contains a permalink or context."""

    opportunity_id: str
    state: FollowupState
    reason_code: FollowupReasonCode
    latest_attempt_id: str | None
    latest_event_at: str | None
    manual_next_step: FollowupNextStep

    def __repr__(self) -> str:
        return (
            f"FollowupRow(opportunity_id={self.opportunity_id!r}, "
            f"state={self.state!r}, reason_code={self.reason_code!r}, "
            f"latest_attempt_id={self.latest_attempt_id!r}, "
            f"latest_event_at={self.latest_event_at!r}, "
            f"manual_next_step={self.manual_next_step!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FollowupProjection:
    """Immutable result for one supplied queue snapshot."""

    rows: tuple[FollowupRow, ...]

    def __repr__(self) -> str:
        return f"FollowupProjection(row_count={len(self.rows)})"


def project_manual_followups(
    queue: OpportunityQueue,
    s4_records: Mapping[str, ManualOutcomeRecord | None],
) -> FollowupProjection:
    """Project typed S6/S4 snapshots without reading or writing local state.

    Queue order is preserved. A missing mapping key and an explicit None both
    mean only that the caller's supplied snapshot has no S4 record for the target.
    """

    account_id, fingerprints = _validate_queue(queue)
    mapping_items = _bounded_mapping_items(s4_records)
    seen_targets: set[str] = set()
    latest_by_target: dict[str, ManualOutcomeEvent] = {}

    for target, record in mapping_items:
        if type(target) is not str or target not in fingerprints or target in seen_targets:
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
        seen_targets.add(target)
        if record is None:
            continue
        latest_by_target[target] = _validate_record(
            record,
            expected_account_id=account_id,
            expected_fingerprint=fingerprints[target],
        )

    rows: list[FollowupRow] = []
    for entry in queue.opportunities:
        latest = latest_by_target.get(entry.target_url)
        rows.append(_row_for_entry(entry, latest))
    return FollowupProjection(tuple(rows))


def _validate_queue(queue: object) -> tuple[UUID, dict[str, str]]:
    if (
        type(queue) is not OpportunityQueue
        or type(queue.opportunities) is not tuple
        or len(queue.opportunities) > _MAX_QUEUE_ENTRIES
    ):
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    account_id = _uuid_v4(queue.account_uuid, "FOLLOWUP_QUEUE_INVALID")
    if str(account_id) != queue.account_uuid:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    fingerprints: dict[str, str] = {}
    seen_ids: set[str] = set()
    active_count = 0
    for entry in queue.opportunities:
        target, fingerprint = _validate_queue_entry(entry, account_id)
        if entry.id in seen_ids or target in fingerprints:
            raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
        seen_ids.add(entry.id)
        fingerprints[target] = fingerprint
        if entry.state != "SKIPPED":
            active_count += 1
    if active_count > _MAX_ACTIVE_OPPORTUNITIES:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    return account_id, fingerprints


def _validate_queue_entry(entry: object, account_id: UUID) -> tuple[str, str]:
    if type(entry) is not OpportunityQueueEntry:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    _uuid_v4(entry.id, "FOLLOWUP_QUEUE_INVALID")
    if type(entry.source) is not str or entry.source != "MANUAL_URL":
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    if type(entry.state) is not str or entry.state not in _QUEUE_STATES:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    if (
        type(entry.context_completeness) is not str
        or entry.context_completeness not in _CONTEXT_COMPLETENESS
        or type(entry.context_provenance) is not str
        or entry.context_provenance not in _CONTEXT_PROVENANCE
    ):
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    context = entry.context
    if context is None:
        if entry.context_provenance != "NONE" or entry.context_completeness != "UNKNOWN":
            raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    else:
        if (
            type(context) is not str
            or not context
            or context != context.strip()
            or len(context) > _MAX_CONTEXT_CHARS
            or _has_forbidden_text(context, allow_newline=True)
            or entry.context_provenance != "OPERATOR_SUPPLIED"
        ):
            raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    if entry.state == "READY" and (context is None or entry.context_completeness != "COMPLETE"):
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    if entry.state == "NEEDS_CONTEXT" and entry.context_completeness == "COMPLETE":
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    skip_reason = entry.skip_reason
    if skip_reason is not None and (
        type(skip_reason) is not str
        or not skip_reason
        or skip_reason != skip_reason.strip()
        or len(skip_reason) > _MAX_SKIP_REASON_CHARS
        or _has_forbidden_text(skip_reason, allow_newline=False, reject_non_ascii_spaces=False)
    ):
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    if entry.state != "SKIPPED" and skip_reason is not None:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    created_at = _queue_timestamp(entry.created_at)
    updated_at = _queue_timestamp(entry.updated_at)
    if updated_at < created_at:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")

    target = entry.target_url
    if type(target) is not str or len(target) > _MAX_TARGET_BYTES:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    try:
        if len(target.encode("utf-8", errors="strict")) > _MAX_TARGET_BYTES:
            raise ValueError
        fingerprint = ManualOutcomeStore.target_fingerprint(account_id, target)
    except ManualOutcomeError, UnicodeError, TypeError, ValueError:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID") from None
    if type(fingerprint) is not str or _SHA256.fullmatch(fingerprint) is None:
        raise FollowupProjectionError("FOLLOWUP_QUEUE_INVALID")
    return target, fingerprint


def _bounded_mapping_items(
    records: object,
) -> tuple[tuple[object, object], ...]:
    if not isinstance(records, Mapping):
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
    typed_records = cast(Mapping[object, object], records)
    try:
        iterator: Iterator[tuple[object, object]] = iter(typed_records.items())
    except Exception:  # noqa: BLE001 - arbitrary Mapping implementations fail closed.
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID") from None

    items: list[tuple[object, object]] = []
    while True:
        try:
            item = next(iterator)
        except StopIteration:
            break
        except Exception:  # noqa: BLE001 - arbitrary Mapping iterators fail closed.
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID") from None
        if len(items) >= _MAX_QUEUE_ENTRIES:
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
        if type(item) is not tuple or len(item) != 2:
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
        items.append((item[0], item[1]))
    return tuple(items)


def _validate_record(
    record: object,
    *,
    expected_account_id: UUID,
    expected_fingerprint: str,
) -> ManualOutcomeEvent:
    if type(record) is not ManualOutcomeRecord:
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
    record_account_id = _uuid_v4(record.account_uuid, "FOLLOWUP_EVIDENCE_INVALID")
    if (
        str(record_account_id) != record.account_uuid
        or record_account_id != expected_account_id
        or type(record.target_fingerprint) is not str
        or record.target_fingerprint != expected_fingerprint
        or type(record.events) is not tuple
        or not 1 <= len(record.events) <= _MAX_EVENTS
    ):
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")

    seen_event_ids: set[str] = set()
    seen_attempt_ids: set[str] = set()
    pending: ManualOutcomeEvent | None = None
    protocol_started = False
    previous_status: str | None = None

    for event in record.events:
        if type(event) is not ManualOutcomeEvent:
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
        if any(
            type(value) is not str
            for value in (
                event.event_id,
                event.attempt_id,
                event.draft_sha256,
                event.recorded_at,
                event.status,
                event.source,
                event.verification,
            )
        ):
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")

        _uuid_v4(event.event_id, "FOLLOWUP_EVIDENCE_INVALID")
        _uuid_v4(event.attempt_id, "FOLLOWUP_EVIDENCE_INVALID")
        if (
            event.event_id in seen_event_ids
            or _SHA256.fullmatch(event.draft_sha256) is None
            or event.status not in _ALL_STATUSES
            or event.source not in _EVENT_SOURCES
            or event.verification != "UNVERIFIED"
        ):
            raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
        _event_timestamp(event.recorded_at)
        seen_event_ids.add(event.event_id)

        if event.source == "LEGACY_OPERATOR":
            if (
                protocol_started
                or pending is not None
                or event.status not in _OUTCOME_STATUSES
                or event.attempt_id != event.event_id
                or previous_status in _BLOCKING_LEGACY_STATUSES
            ):
                raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
        elif event.source == "SOFTWARE_HANDOFF":
            if (
                event.status != "HANDOFF_UNRESOLVED"
                or event.attempt_id != event.event_id
                or event.attempt_id in seen_attempt_ids
                or pending is not None
                or (previous_status is not None and previous_status != "ABORTED_NOT_SENT")
            ):
                raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
            protocol_started = True
            seen_attempt_ids.add(event.attempt_id)
            pending = event
        else:
            if (
                event.status not in _OUTCOME_STATUSES
                or pending is None
                or pending.attempt_id != event.attempt_id
                or pending.draft_sha256 != event.draft_sha256
            ):
                raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
            protocol_started = True
            pending = None
        previous_status = event.status

    return record.events[-1]


def _row_for_entry(
    entry: OpportunityQueueEntry,
    latest: ManualOutcomeEvent | None,
) -> FollowupRow:
    if latest is None:
        return FollowupRow(
            opportunity_id=entry.id,
            state="NO_FOLLOWUP",
            reason_code="NO_S4_RECORD_IN_SNAPSHOT",
            latest_attempt_id=None,
            latest_event_at=None,
            manual_next_step="NONE",
        )

    if latest.status == "HANDOFF_UNRESOLVED":
        state: FollowupState = "NEEDS_VERIFICATION"
        reason: FollowupReasonCode = "HANDOFF_UNRESOLVED"
        next_step: FollowupNextStep = "CHECK_NATIVE_UI_PRIVATELY"
    elif latest.status == "AMBIGUOUS":
        state = "NEEDS_VERIFICATION"
        reason = "AMBIGUOUS_OUTCOME"
        next_step = "CHECK_NATIVE_UI_PRIVATELY"
    elif latest.status == "OPERATOR_REPORTED_SENT_UNVERIFIED":
        state = "NEEDS_VERIFICATION"
        reason = "OPERATOR_SEND_UNVERIFIED"
        next_step = "CHECK_NATIVE_UI_PRIVATELY"
    elif latest.status == "ABORTED_NOT_SENT":
        state = "CLOSED"
        reason = "LOCAL_ATTEMPT_ABORTED_NOT_SENT"
        next_step = "RESELECT_IF_STILL_RELEVANT"
    else:
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")

    return FollowupRow(
        opportunity_id=entry.id,
        state=state,
        reason_code=reason,
        latest_attempt_id=latest.attempt_id,
        latest_event_at=latest.recorded_at,
        manual_next_step=next_step,
    )


def _uuid_v4(value: object, code: str) -> UUID:
    if type(value) is not str:
        raise FollowupProjectionError(code)
    try:
        parsed = UUID(value)
    except ValueError, TypeError, AttributeError:
        raise FollowupProjectionError(code) from None
    if parsed.version != 4 or str(parsed) != value:
        raise FollowupProjectionError(code)
    return parsed


def _queue_timestamp(value: object) -> datetime:
    return _timestamp(value, "FOLLOWUP_QUEUE_INVALID")


def _event_timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID") from None
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise FollowupProjectionError("FOLLOWUP_EVIDENCE_INVALID")
    return parsed


def _timestamp(value: object, code: str) -> datetime:
    if type(value) is not str or len(value) != 20:
        raise FollowupProjectionError(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise FollowupProjectionError(code) from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise FollowupProjectionError(code)
    return parsed


def _has_forbidden_text(
    value: str,
    *,
    allow_newline: bool,
    reject_non_ascii_spaces: bool = True,
) -> bool:
    for character in value:
        category = unicodedata.category(character)
        if allow_newline and character == "\n":
            continue
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            return True
        if reject_non_ascii_spaces and category == "Zs" and character != " ":
            return True
    return False
