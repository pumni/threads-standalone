"""Deterministic, privacy-safe iCalendar export for the local content planner."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from threads_platform.standalone.recruitment_content_planner import (
    ContentAttachmentMetadataV1,
    RecruitmentContentCalendarV1,
    RecruitmentContentPacketV1,
    RecruitmentContentPlannerError,
)

_MAX_PACKETS = 40
_MAX_TEXT_CHARS = 500
_MAX_PRIVATE_NOTES_CHARS = 1_000
_MAX_REVIEW_NOTE_CHARS = 500
_MAX_URL_CHARS = 2_048
_MAX_LABEL_CHARS = 120
_MAX_ATTRIBUTION_CHARS = 200
_EVENT_DURATION = timedelta(minutes=15)
_UID_DOMAIN = b"threads-standalone/s10c/calendar-event-uid/v1\x00"
_STATUSES = frozenset({"DRAFT", "READY_FOR_MANUAL_PUBLICATION", "REJECTED"})
_ATTACHMENT_KINDS = frozenset({"IMAGE", "VIDEO", "REFERENCE_LINK"})
_SUMMARIES = {
    "DRAFT": "Local recruitment content reminder (DRAFT)",
    "READY_FOR_MANUAL_PUBLICATION": (
        "Local recruitment content reminder (READY_FOR_MANUAL_PUBLICATION)"
    ),
}
_DESCRIPTIONS = {
    "DRAFT": (
        "NOT_PUBLISHED_BY_PLANNER. Local editorial reminder only. Status: DRAFT. "
        "This reminder does not authorize Threads publication."
    ),
    "READY_FOR_MANUAL_PUBLICATION": (
        "NOT_PUBLISHED_BY_PLANNER. Local editorial reminder only. "
        "Status: READY_FOR_MANUAL_PUBLICATION. This reminder does not authorize Threads publication."
    ),
}


def render_recruitment_content_calendar(calendar: RecruitmentContentCalendarV1) -> str:
    """Render a bounded planner snapshot as deterministic UTF-8 RFC5545 text."""

    packets = _validate_calendar(calendar)
    events: list[tuple[datetime, str, RecruitmentContentPacketV1]] = []
    for packet in packets:
        if packet.editorial_status == "REJECTED":
            continue
        uid = _event_uid(calendar.account_uuid, packet.packet_id)
        events.append((packet.scheduled_at, uid, packet))
    events.sort(key=lambda item: (item[0], item[1]))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Threads Standalone//S10C Offline Calendar//EN",
        "CALSCALE:GREGORIAN",
    ]
    for _, uid, packet in events:
        try:
            event_end = packet.scheduled_at + _EVENT_DURATION
        except OverflowError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        status = packet.editorial_status
        lines.extend(
            (
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{_format_utc(packet.updated_at)}",
                f"DTSTART:{_format_utc(packet.scheduled_at)}",
                f"DTEND:{_format_utc(event_end)}",
                f"SUMMARY:{_SUMMARIES[status]}",
                f"DESCRIPTION:{_DESCRIPTIONS[status]}",
                "CLASS:PRIVATE",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            )
        )
    lines.append("END:VCALENDAR")
    return "".join(f"{_fold_content_line(line)}\r\n" for line in lines)


def _validate_calendar(
    calendar: RecruitmentContentCalendarV1,
) -> tuple[RecruitmentContentPacketV1, ...]:
    if type(calendar) is not RecruitmentContentCalendarV1:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    account_id = calendar.account_uuid
    if type(account_id) is not UUID or account_id.version != 4:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if type(calendar.packets) is not tuple:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if len(calendar.packets) > _MAX_PACKETS:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_LIMIT_REACHED")

    packet_ids: set[UUID] = set()
    packets: list[RecruitmentContentPacketV1] = []
    for packet in calendar.packets:
        _validate_packet(packet, account_id, packet_ids)
        packet_ids.add(packet.packet_id)
        packets.append(packet)
    return tuple(packets)


def _validate_packet(
    packet: RecruitmentContentPacketV1,
    account_id: UUID,
    packet_ids: set[UUID],
) -> None:
    if type(packet) is not RecruitmentContentPacketV1:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if type(packet.version) is not int or packet.version != 1:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if type(packet.account_uuid) is not UUID or packet.account_uuid != account_id:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_ACCOUNT_MISMATCH")
    if type(packet.packet_id) is not UUID or packet.packet_id.version != 4:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if packet.packet_id in packet_ids:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if type(packet.editorial_status) is not str or packet.editorial_status not in _STATUSES:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if (
        type(packet.content_provenance) is not str
        or packet.content_provenance != "OPERATOR_AUTHORED"
        or packet.rights_asserted is not True
        or packet.consent_asserted is not True
        or type(packet.rights_assertion_status) is not str
        or packet.rights_assertion_status != "UNVERIFIED_OPERATOR_ASSERTION"
        or type(packet.platform_permission_status) is not str
        or packet.platform_permission_status != "NOT_ESTABLISHED"
        or type(packet.remote_publication_status) is not str
        or packet.remote_publication_status != "NOT_PUBLISHED_BY_PLANNER"
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if type(packet.original_text) is not str or len(packet.original_text) > _MAX_TEXT_CHARS:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if packet.private_notes is not None and (
        type(packet.private_notes) is not str
        or len(packet.private_notes) > _MAX_PRIVATE_NOTES_CHARS
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if packet.review_note is not None and (
        type(packet.review_note) is not str or len(packet.review_note) > _MAX_REVIEW_NOTE_CHARS
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    _validate_attachment(packet.attachment)

    created_at = _require_utc(packet.created_at)
    updated_at = _require_utc(packet.updated_at)
    scheduled_at = _require_utc(packet.scheduled_at)
    if updated_at < created_at:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    try:
        scheduled_end = scheduled_at + _EVENT_DURATION
    except OverflowError:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
    if scheduled_end > datetime.max.replace(tzinfo=UTC):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")

    if packet.editorial_status == "DRAFT":
        if packet.reviewed_at is not None or packet.review_note is not None:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    else:
        if packet.reviewed_at is None:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        reviewed_at = _require_utc(packet.reviewed_at)
        if reviewed_at < created_at or reviewed_at > updated_at:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if packet.editorial_status == "REJECTED" and packet.review_note is None:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")


def _validate_attachment(attachment: ContentAttachmentMetadataV1 | None) -> None:
    if attachment is None:
        return
    if type(attachment) is not ContentAttachmentMetadataV1:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if (
        type(attachment.kind) is not str
        or attachment.kind not in _ATTACHMENT_KINDS
        or type(attachment.url) is not str
        or len(attachment.url) > _MAX_URL_CHARS
        or attachment.rights_asserted is not True
        or attachment.consent_asserted is not True
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if attachment.label is not None and (
        type(attachment.label) is not str or len(attachment.label) > _MAX_LABEL_CHARS
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    if attachment.attribution is not None and (
        type(attachment.attribution) is not str
        or len(attachment.attribution) > _MAX_ATTRIBUTION_CHARS
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")


def _require_utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is not UTC:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
    return value


def _event_uid(account_id: UUID, packet_id: UUID) -> str:
    digest = hashlib.sha256(_UID_DOMAIN + account_id.bytes + packet_id.bytes).hexdigest()
    return f"{digest}@threads-standalone.invalid"


def _format_utc(value: datetime) -> str:
    return (
        f"{value.year:04d}{value.month:02d}{value.day:02d}T"
        f"{value.hour:02d}{value.minute:02d}{value.second:02d}Z"
    )


def _fold_content_line(line: str) -> str:
    """Fold at 75 UTF-8 octets, counting the continuation space."""

    physical_lines: list[str] = []
    current: list[str] = []
    octets = 0
    for character in line:
        character_octets = len(character.encode("utf-8"))
        if octets + character_octets > 75:
            physical_lines.append("".join(current))
            current = [" "]
            octets = 1
        current.append(character)
        octets += character_octets
    physical_lines.append("".join(current))
    return "\r\n".join(physical_lines)
