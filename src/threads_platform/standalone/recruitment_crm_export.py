"""Pure, privacy-minimized JSON handoff for explicitly approved S6 opportunities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, cast

from threads_platform.standalone.manual_outcomes import ManualOutcomeRecord
from threads_platform.standalone.opportunity_queue import OpportunityQueue, OpportunityQueueEntry
from threads_platform.standalone.recruitment_followup_projection import (
    FollowupProjectionError,
    FollowupRow,
    project_manual_followups,
)

RecruitmentFollowupStatus = Literal[
    "NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT",
    "NEEDS_VERIFICATION",
    "LOCAL_ATTEMPT_ABORTED_NOT_SENT",
]

_SCHEMA = "threads-local/recruitment-crm-handoff"
_VERSION = 1
_S6_VERSION = 1
_S4_VERSION = 1
_S9B1_VERSION = 1
_MAX_RETAINED = 40
_MAX_ACTIVE = 20
_MAX_SELECTED = 20
_MAX_PACKET_BYTES = 64 * 1024
_MAX_APPROVAL_AGE = timedelta(hours=24)
_MIN_REVIEW_YEAR = 2000
_MAX_REVIEW_YEAR = 2100


class RecruitmentExportPurpose(StrEnum):
    """Fixed, bounded reasons for creating a private recruitment handoff."""

    FOLLOW_UP = "RECRUITMENT_FOLLOW_UP"
    PIPELINE_REVIEW = "RECRUITMENT_PIPELINE_REVIEW"
    SOURCE_REVIEW = "RECRUITMENT_SOURCE_REVIEW"


class RecruitmentExportField(StrEnum):
    """Fields the operator may explicitly approve for one opportunity."""

    OPPORTUNITY_ID = "OPPORTUNITY_ID"
    SOURCE = "SOURCE"
    OPPORTUNITY_STATE = "OPPORTUNITY_STATE"
    FOLLOWUP_STATUS = "FOLLOWUP_STATUS"
    FOLLOWUP_REASON = "FOLLOWUP_REASON"
    NEXT_STEP = "NEXT_STEP"
    TARGET_URL = "TARGET_URL"


DEFAULT_EXPORT_FIELDS = (
    RecruitmentExportField.OPPORTUNITY_ID,
    RecruitmentExportField.SOURCE,
    RecruitmentExportField.FOLLOWUP_STATUS,
    RecruitmentExportField.FOLLOWUP_REASON,
)

_FIELD_ORDER = tuple(RecruitmentExportField)
_FIELD_NAMES = {field: field.value.lower() for field in RecruitmentExportField}
_VALID_PURPOSES = frozenset(RecruitmentExportPurpose)
_HEX_DIGEST_LENGTH = 64


class RecruitmentCrmExportError(ValueError):
    """Fixed-code failure that never includes input or account data."""

    _CODES = frozenset(
        {
            "CRM_EXPORT_INVALID_INPUT",
            "CRM_EXPORT_INVALID_QUEUE",
            "CRM_EXPORT_INVALID_S4_SNAPSHOT",
            "CRM_EXPORT_INVALID_APPROVAL",
            "CRM_EXPORT_APPROVAL_REQUIRED",
            "CRM_EXPORT_APPROVAL_STALE",
            "CRM_EXPORT_DUPLICATE_SELECTION",
            "CRM_EXPORT_LIMIT_EXCEEDED",
            "CRM_EXPORT_UNSUPPORTED_FIELD",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported recruitment CRM export error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentExportApproval:
    """Immutable per-opportunity operator assertions bound to one queue snapshot."""

    version: int
    account_uuid: str
    opportunity_id: str
    source: Literal["MANUAL_URL"]
    target_commitment: str
    queue_revision_commitment: str
    purpose: RecruitmentExportPurpose
    lawful_basis_asserted: bool
    rights_asserted: bool
    consent_asserted: bool
    reviewed_at: str
    expires_at: str
    selected_fields: tuple[RecruitmentExportField, ...]
    approval_commitment: str

    def __repr__(self) -> str:
        return (
            "RecruitmentExportApproval(version=1, opportunity_id=<opaque>, "
            f"purpose={self.purpose!r}, selected_field_count={len(self.selected_fields)})"
        )


def create_recruitment_export_approval(
    queue: OpportunityQueue,
    opportunity_id: str,
    *,
    purpose: RecruitmentExportPurpose,
    lawful_basis_asserted: bool,
    rights_asserted: bool,
    consent_asserted: bool,
    reviewed_at: str,
    expires_at: str,
    selected_fields: tuple[RecruitmentExportField, ...] = DEFAULT_EXPORT_FIELDS,
) -> RecruitmentExportApproval:
    """Create a fixed-field approval bound to one exact active queue entry.

    The assertions record an operator claim only. They do not prove legal
    authority, platform permission, data-subject consent, or identity.
    """

    entries = _validated_queue_entries(queue)
    if type(opportunity_id) is not str:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    entry = next((item for item in entries if item.id == opportunity_id), None)
    if entry is None or entry.state == "SKIPPED":
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    if type(purpose) is not RecruitmentExportPurpose or purpose not in _VALID_PURPOSES:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    if any(
        type(assertion) is not bool or assertion is not True
        for assertion in (lawful_basis_asserted, rights_asserted, consent_asserted)
    ):
        raise RecruitmentCrmExportError("CRM_EXPORT_APPROVAL_REQUIRED")

    fields = _normalize_fields(selected_fields)
    reviewed = _parse_bounded_utc(reviewed_at)
    expires = _parse_bounded_utc(expires_at)
    if not reviewed < expires or expires - reviewed > _MAX_APPROVAL_AGE:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")

    target_commitment = _commit(
        b"threads-local/recruitment-crm-target/v1",
        [queue.account_uuid, entry.id, entry.target_url],
    )
    queue_revision = _queue_revision_commitment(queue.account_uuid, entry)
    values: dict[str, object] = {
        "version": _VERSION,
        "account_uuid": queue.account_uuid,
        "opportunity_id": entry.id,
        "source": entry.source,
        "target_commitment": target_commitment,
        "queue_revision_commitment": queue_revision,
        "purpose": purpose.value,
        "lawful_basis_asserted": lawful_basis_asserted,
        "rights_asserted": rights_asserted,
        "consent_asserted": consent_asserted,
        "reviewed_at": reviewed_at,
        "expires_at": expires_at,
        "selected_fields": [field.value for field in fields],
    }
    return RecruitmentExportApproval(
        version=_VERSION,
        account_uuid=queue.account_uuid,
        opportunity_id=entry.id,
        source="MANUAL_URL",
        target_commitment=target_commitment,
        queue_revision_commitment=queue_revision,
        purpose=purpose,
        lawful_basis_asserted=lawful_basis_asserted,
        rights_asserted=rights_asserted,
        consent_asserted=consent_asserted,
        reviewed_at=reviewed_at,
        expires_at=expires_at,
        selected_fields=fields,
        approval_commitment=_commit(b"threads-local/recruitment-crm-approval/v1", values),
    )


def build_recruitment_crm_handoff(
    queue: OpportunityQueue,
    s4_records: Mapping[str, ManualOutcomeRecord | None],
    selected_opportunity_ids: tuple[str, ...],
    approvals: tuple[RecruitmentExportApproval, ...],
    *,
    now_utc: str,
) -> bytes:
    """Render a deterministic, bounded versioned JSON packet in memory only."""

    entries = _validated_queue_entries(queue)
    selected = _validate_selection(selected_opportunity_ids, entries)
    s4_snapshot = _validate_s4_snapshot(queue, entries, s4_records)
    try:
        projection = project_manual_followups(queue, s4_snapshot)
    except FollowupProjectionError as error:
        code = (
            "CRM_EXPORT_INVALID_QUEUE"
            if error.code == "FOLLOWUP_QUEUE_INVALID"
            else "CRM_EXPORT_INVALID_S4_SNAPSHOT"
        )
        raise RecruitmentCrmExportError(code) from None

    now = _parse_bounded_utc(now_utc)
    approval_by_id = _validate_approvals(queue, selected, approvals, now)
    followup_by_id = {row.opportunity_id: row for row in projection.rows}
    records: list[dict[str, object]] = []
    for entry in entries:
        if entry.id not in selected:
            continue
        approval = approval_by_id[entry.id]
        followup = followup_by_id[entry.id]
        field_values = {
            _FIELD_NAMES[field]: _field_value(
                field,
                queue.account_uuid,
                entry,
                followup,
            )
            for field in approval.selected_fields
        }
        records.append(
            {
                "record_id": _opaque_id(
                    b"threads-local/recruitment-crm-record/v1",
                    queue.account_uuid,
                    entry.id,
                ),
                "fields": field_values,
                "operator_approval": {
                    "business_purpose": approval.purpose.value,
                    "reviewed_at": approval.reviewed_at,
                    "expires_at": approval.expires_at,
                    "lawful_basis": "ASSERTED_NOT_INDEPENDENTLY_VERIFIED",
                    "rights": "ASSERTED_NOT_INDEPENDENTLY_VERIFIED",
                    "consent": "ASSERTED_NOT_INDEPENDENTLY_VERIFIED",
                },
            }
        )

    payload = {
        "schema": _SCHEMA,
        "version": _VERSION,
        "source_versions": {
            "s6_opportunity_queue": _S6_VERSION,
            "s4_manual_outcomes": _S4_VERSION,
            "s9b1_followup_projection": _S9B1_VERSION,
        },
        "status_semantics": "LOCAL_EVIDENCE_ONLY_NO_VERIFIED_SEND_OR_CONVERSION",
        "records": records,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except TypeError, ValueError, UnicodeError:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT") from None
    if len(encoded) > _MAX_PACKET_BYTES:
        raise RecruitmentCrmExportError("CRM_EXPORT_LIMIT_EXCEEDED")
    return encoded


def _validated_queue_entries(queue: object) -> tuple[OpportunityQueueEntry, ...]:
    try:
        projection = project_manual_followups(queue, {})  # type: ignore[arg-type]
    except FollowupProjectionError:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_QUEUE") from None
    if type(queue) is not OpportunityQueue:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_QUEUE")
    if len(queue.opportunities) > _MAX_RETAINED or queue.active_count > _MAX_ACTIVE:
        raise RecruitmentCrmExportError("CRM_EXPORT_LIMIT_EXCEEDED")
    if len(projection.rows) != len(queue.opportunities):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_QUEUE")
    return queue.opportunities


def _validate_selection(
    selected_ids: object,
    entries: tuple[OpportunityQueueEntry, ...],
) -> frozenset[str]:
    if type(selected_ids) is not tuple:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    typed_ids = cast(tuple[object, ...], selected_ids)
    if len(typed_ids) > _MAX_SELECTED:
        raise RecruitmentCrmExportError("CRM_EXPORT_LIMIT_EXCEEDED")
    if any(type(item) is not str for item in typed_ids):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    ids = cast(tuple[str, ...], typed_ids)
    if len(set(ids)) != len(ids):
        raise RecruitmentCrmExportError("CRM_EXPORT_DUPLICATE_SELECTION")
    by_id = {entry.id: entry for entry in entries}
    for opportunity_id in ids:
        entry = by_id.get(opportunity_id)
        if entry is None or entry.state == "SKIPPED":
            raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    return frozenset(ids)


def _validate_s4_snapshot(
    queue: OpportunityQueue,
    entries: tuple[OpportunityQueueEntry, ...],
    records: object,
) -> dict[str, ManualOutcomeRecord | None]:
    if type(records) is not dict:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_S4_SNAPSHOT")
    typed_records = cast(dict[object, object], records).copy()
    if len(typed_records) > _MAX_RETAINED:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_S4_SNAPSHOT")
    expected_targets = {entry.target_url for entry in entries}
    if set(typed_records) != expected_targets or any(type(key) is not str for key in typed_records):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_S4_SNAPSHOT")
    if any(
        record is not None and type(record) is not ManualOutcomeRecord
        for record in typed_records.values()
    ):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_S4_SNAPSHOT")
    # The account value is used by the public projector to validate all S4 records.
    if type(queue.account_uuid) is not str:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_QUEUE")
    return cast(dict[str, ManualOutcomeRecord | None], typed_records)


def _validate_approvals(
    queue: OpportunityQueue,
    selected: frozenset[str],
    approvals: object,
    now: datetime,
) -> dict[str, RecruitmentExportApproval]:
    if type(approvals) is not tuple:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT")
    typed_approvals = cast(tuple[object, ...], approvals)
    if len(typed_approvals) > _MAX_SELECTED:
        raise RecruitmentCrmExportError("CRM_EXPORT_LIMIT_EXCEEDED")
    if len(typed_approvals) != len(selected):
        raise RecruitmentCrmExportError("CRM_EXPORT_APPROVAL_REQUIRED")

    entries = {entry.id: entry for entry in queue.opportunities}
    approval_by_id: dict[str, RecruitmentExportApproval] = {}
    for approval_value in typed_approvals:
        approval = approval_value
        if type(approval) is not RecruitmentExportApproval:
            raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
        _validate_approval_shape(approval)
        if approval.opportunity_id in approval_by_id:
            raise RecruitmentCrmExportError("CRM_EXPORT_DUPLICATE_SELECTION")
        if approval.opportunity_id not in selected:
            raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
        approval_by_id[approval.opportunity_id] = approval

    for opportunity_id in selected:
        approval = approval_by_id.get(opportunity_id)
        entry = entries[opportunity_id]
        if approval is None:
            raise RecruitmentCrmExportError("CRM_EXPORT_APPROVAL_REQUIRED")
        expected_target = _commit(
            b"threads-local/recruitment-crm-target/v1",
            [queue.account_uuid, entry.id, entry.target_url],
        )
        expected_revision = _queue_revision_commitment(queue.account_uuid, entry)
        if (
            approval.account_uuid != queue.account_uuid
            or approval.opportunity_id != entry.id
            or approval.source != entry.source
            or approval.target_commitment != expected_target
            or approval.queue_revision_commitment != expected_revision
        ):
            raise RecruitmentCrmExportError("CRM_EXPORT_APPROVAL_STALE")
        if now < _parse_bounded_utc(approval.reviewed_at) or now >= _parse_bounded_utc(
            approval.expires_at
        ):
            raise RecruitmentCrmExportError("CRM_EXPORT_APPROVAL_STALE")
    return approval_by_id


def _validate_approval_shape(approval: RecruitmentExportApproval) -> None:
    if (
        type(approval.version) is not int
        or approval.version != _VERSION
        or type(approval.account_uuid) is not str
        or type(approval.opportunity_id) is not str
        or type(approval.source) is not str
        or approval.source != "MANUAL_URL"
        or type(approval.target_commitment) is not str
        or type(approval.queue_revision_commitment) is not str
        or type(approval.purpose) is not RecruitmentExportPurpose
        or approval.purpose not in _VALID_PURPOSES
        or type(approval.lawful_basis_asserted) is not bool
        or approval.lawful_basis_asserted is not True
        or type(approval.rights_asserted) is not bool
        or approval.rights_asserted is not True
        or type(approval.consent_asserted) is not bool
        or approval.consent_asserted is not True
        or type(approval.reviewed_at) is not str
        or type(approval.expires_at) is not str
    ):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
    _require_digest(approval.target_commitment)
    _require_digest(approval.queue_revision_commitment)
    _parse_bounded_utc(approval.reviewed_at)
    reviewed = _parse_bounded_utc(approval.reviewed_at)
    expires = _parse_bounded_utc(approval.expires_at)
    if not reviewed < expires or expires - reviewed > _MAX_APPROVAL_AGE:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
    fields = _normalize_fields(approval.selected_fields)
    if fields != approval.selected_fields:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
    _require_digest(approval.approval_commitment)
    values: dict[str, object] = {
        "version": approval.version,
        "account_uuid": approval.account_uuid,
        "opportunity_id": approval.opportunity_id,
        "source": approval.source,
        "target_commitment": approval.target_commitment,
        "queue_revision_commitment": approval.queue_revision_commitment,
        "purpose": approval.purpose.value,
        "lawful_basis_asserted": approval.lawful_basis_asserted,
        "rights_asserted": approval.rights_asserted,
        "consent_asserted": approval.consent_asserted,
        "reviewed_at": approval.reviewed_at,
        "expires_at": approval.expires_at,
        "selected_fields": [field.value for field in fields],
    }
    if _commit(b"threads-local/recruitment-crm-approval/v1", values) != (
        approval.approval_commitment
    ):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")


def _normalize_fields(value: object) -> tuple[RecruitmentExportField, ...]:
    if type(value) is not tuple or not value:
        raise RecruitmentCrmExportError("CRM_EXPORT_UNSUPPORTED_FIELD")
    typed_values = cast(tuple[object, ...], value)
    if any(type(field) is not RecruitmentExportField for field in typed_values):
        raise RecruitmentCrmExportError("CRM_EXPORT_UNSUPPORTED_FIELD")
    fields = cast(tuple[RecruitmentExportField, ...], typed_values)
    if len(set(fields)) != len(fields):
        raise RecruitmentCrmExportError("CRM_EXPORT_UNSUPPORTED_FIELD")
    if any(field not in _FIELD_ORDER for field in fields):
        raise RecruitmentCrmExportError("CRM_EXPORT_UNSUPPORTED_FIELD")
    ordered = tuple(field for field in _FIELD_ORDER if field in fields)
    if ordered != fields:
        raise RecruitmentCrmExportError("CRM_EXPORT_UNSUPPORTED_FIELD")
    return ordered


def _field_value(
    field: RecruitmentExportField,
    account_uuid: str,
    entry: OpportunityQueueEntry,
    followup: FollowupRow,
) -> str:
    if field is RecruitmentExportField.OPPORTUNITY_ID:
        return _opaque_id(
            b"threads-local/recruitment-crm-opportunity/v1",
            account_uuid,
            entry.id,
        )
    if field is RecruitmentExportField.SOURCE:
        return entry.source
    if field is RecruitmentExportField.OPPORTUNITY_STATE:
        return entry.state
    if field is RecruitmentExportField.FOLLOWUP_STATUS:
        if followup.state == "NO_FOLLOWUP":
            return "NO_S4_RECORD_IN_SUPPLIED_SNAPSHOT"
        if followup.state == "NEEDS_VERIFICATION":
            return "NEEDS_VERIFICATION"
        if followup.state == "CLOSED" and followup.reason_code == "LOCAL_ATTEMPT_ABORTED_NOT_SENT":
            return "LOCAL_ATTEMPT_ABORTED_NOT_SENT"
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_S4_SNAPSHOT")
    if field is RecruitmentExportField.FOLLOWUP_REASON:
        return followup.reason_code
    if field is RecruitmentExportField.NEXT_STEP:
        return followup.manual_next_step
    if field is RecruitmentExportField.TARGET_URL:
        return entry.target_url
    raise RecruitmentCrmExportError("CRM_EXPORT_UNSUPPORTED_FIELD")


def _queue_revision_commitment(account_uuid: str, entry: OpportunityQueueEntry) -> str:
    return _commit(
        b"threads-local/recruitment-crm-queue-entry/v1",
        [
            account_uuid,
            entry.id,
            entry.target_url,
            entry.source,
            entry.context,
            entry.context_provenance,
            entry.context_completeness,
            entry.created_at,
            entry.updated_at,
            entry.state,
            entry.skip_reason,
        ],
    )


def _opaque_id(namespace: bytes, account_uuid: str, opportunity_id: str) -> str:
    return _commit(namespace, [account_uuid, opportunity_id])


def _commit(namespace: bytes, value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except TypeError, ValueError, UnicodeError:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_INPUT") from None
    return hashlib.sha256(namespace + b"\0" + encoded).hexdigest()


def _parse_bounded_utc(value: object) -> datetime:
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL") from None
    if (
        parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value
        or not _MIN_REVIEW_YEAR <= parsed.year <= _MAX_REVIEW_YEAR
    ):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
    return parsed


def _require_digest(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != _HEX_DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RecruitmentCrmExportError("CRM_EXPORT_INVALID_APPROVAL")
