"""Pure, offline exact-context approval and preflight for recruitment replies.

The approval contract is deliberately separate from the S4 manual outcome
journal. It creates an in-memory, one-attempt review token; it does not persist,
open a browser, or perform a handoff.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TextIO, cast
from uuid import UUID

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import normalize_manual_permalink
from threads_platform.standalone.manual_opportunities import private_review_terminal_available
from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeEvent,
    ManualOutcomeRecord,
    ManualOutcomeStatus,
    ManualOutcomeStore,
)
from threads_platform.standalone.opportunity_queue import OpportunityQueue, OpportunityQueueEntry
from threads_platform.standalone.recruitment_qualification import (
    OpportunityQualification,
    RecruitmentQualificationError,
    rank_opportunities,
)
from threads_platform.standalone.recruitment_reply_assistant import (
    OpportunityReplyEditorialSession,
    OpportunityReplyProposal,
)
from threads_platform.standalone.recruitment_reply_proposal import (
    ReplyProposalError,
    validate_reply_text,
)

ApprovalS4Status = ManualOutcomeStatus | Literal["NO_RECORD"]
_MAX_CONTEXT_CHARS = 4_000
_MAX_CONTEXT_BYTES = 16_000
_MAX_DRAFT_CHARS = 500
_MAX_QUEUE_ENTRIES = 40
_MAX_CANONICAL_BYTES = 1_048_576
_MAX_RENDERED_BYTES = 65_536
_MAX_TTL_SECONDS = 900
_MAX_EPOCH_SECONDS = 253_402_300_799
_VALID_S4_STATUSES = frozenset(
    {
        "HANDOFF_UNRESOLVED",
        "ABORTED_NOT_SENT",
        "OPERATOR_REPORTED_SENT_UNVERIFIED",
        "AMBIGUOUS",
    }
)
_BLOCKING_S4_STATUSES = frozenset(
    {"HANDOFF_UNRESOLVED", "AMBIGUOUS", "OPERATOR_REPORTED_SENT_UNVERIFIED"}
)
_QUEUE_DOMAIN = b"threads-standalone:s9a:queue-state:v2\0"
_QUALIFICATION_DOMAIN = b"threads-standalone:s9a:qualification:v2\0"
_PACKET_DOMAIN = b"threads-standalone:s9a:review-packet:v2\0"
_CONFIRMATION_DOMAIN = b"threads-standalone:s9a:human-confirmation:v2\0"
_APPROVAL_DOMAIN = b"threads-standalone:s9a:approval:v2\0"
_S4_SNAPSHOT_DOMAIN = b"threads-standalone:s9a:s4-snapshot:v2\0"


class ExactContextApprovalError(ValueError):
    """Fixed-code failure safe for private terminal output."""

    _CODES = frozenset(
        {
            "S9A_PACKET_INVALID",
            "S9A_ACCOUNT_MISMATCH",
            "S9A_OPPORTUNITY_INVALID",
            "S9A_TARGET_INVALID",
            "S9A_CONTEXT_INVALID",
            "S9A_QUALIFICATION_INVALID",
            "S9A_SESSION_INVALID",
            "S9A_S4_STATUS_INVALID",
            "S9A_S4_BLOCKED",
            "S9A_CONFIRMATION_REQUIRED",
            "S9A_APPROVAL_INVALID",
            "S9A_APPROVAL_EXPIRED",
            "S9A_ATTEMPT_INVALID",
            "S9A_ATTEMPT_MISMATCH",
            "S9A_PRIVATE_TERMINAL_REQUIRED",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported S9A approval error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ExactContextReviewPacketV2:
    """Immutable private rendering input for one exact S6/S7/S8 candidate."""

    version: Literal[2]
    account_uuid: str
    opportunity_id: str
    target_url: str = field(repr=False)
    source: Literal["MANUAL_URL"]
    context: str = field(repr=False)
    context_sha256: str
    context_provenance: Literal["OPERATOR_SUPPLIED"]
    context_completeness: Literal["COMPLETE"]
    queue_state_sha256: str
    queue_state: Literal["READY"]
    created_at: str
    updated_at: str
    qualification: OpportunityQualification
    qualification_sha256: str
    s4_status: ApprovalS4Status
    s4_snapshot_sha256: str
    final_draft: str = field(repr=False)
    draft_sha256: str
    editorial_disposition: Literal["EDITING", "KEPT_FOR_SESSION"]
    fingerprint: str
    confirmation_phrase: str

    def __repr__(self) -> str:
        return (
            "ExactContextReviewPacketV2("
            f"version={self.version}, account_uuid={self.account_uuid!r}, "
            f"opportunity_id={self.opportunity_id!r}, target_url=<redacted>, "
            f"source={self.source}, context=<redacted>, "
            f"context_sha256={self.context_sha256}, queue_state={self.queue_state}, "
            f"qualification_state={self.qualification.state}, s4_status={self.s4_status}, "
            "final_draft=<redacted>, fingerprint=<bound>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ExactContextApprovalV2:
    """In-memory approval evidence bound to one packet and one attempt ID."""

    version: Literal[2]
    account_uuid: str
    opportunity_id: str
    target_url: str = field(repr=False)
    source: Literal["MANUAL_URL"]
    context_provenance: Literal["OPERATOR_SUPPLIED"]
    context_completeness: Literal["COMPLETE"]
    context_sha256: str
    queue_state_sha256: str
    qualification_sha256: str
    s4_status: ApprovalS4Status
    s4_snapshot_sha256: str
    draft_sha256: str
    editorial_disposition: Literal["EDITING", "KEPT_FOR_SESSION"]
    packet_fingerprint: str
    confirmation_sha256: str
    attempt_id: str
    issued_at_epoch_seconds: int
    expires_at_epoch_seconds: int
    fingerprint: str

    def __repr__(self) -> str:
        return (
            "ExactContextApprovalV2("
            f"version={self.version}, account_uuid={self.account_uuid!r}, "
            f"opportunity_id={self.opportunity_id!r}, target_url=<redacted>, "
            f"packet_fingerprint={self.packet_fingerprint}, attempt_id={self.attempt_id!r}, "
            "fingerprint=<bound>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ExactContextPreflightPermitV2:
    """A successful local comparison result; it is not a send capability."""

    version: Literal[2]
    account_uuid: str
    opportunity_id: str
    attempt_id: str
    packet_fingerprint: str
    approval_fingerprint: str
    checked_at_epoch_seconds: int

    def __repr__(self) -> str:
        return (
            "ExactContextPreflightPermitV2("
            f"version={self.version}, account_uuid={self.account_uuid!r}, "
            f"opportunity_id={self.opportunity_id!r}, attempt_id={self.attempt_id!r}, "
            "handoff=<not performed>)"
        )


def build_exact_context_review_packet(
    queue: OpportunityQueue,
    editorial_session: OpportunityReplyEditorialSession,
    *,
    account_uuid: str,
    s4_outcomes: Mapping[str, ManualOutcomeStatus | ManualOutcomeRecord | None],
) -> ExactContextReviewPacketV2:
    """Recompute S7 and bind the exact current S6 queue and S8 draft in memory."""

    account_id = _validate_uuid(account_uuid, "S9A_ACCOUNT_MISMATCH")
    if type(queue) is not OpportunityQueue or queue.account_uuid != str(account_id):
        raise ExactContextApprovalError("S9A_ACCOUNT_MISMATCH")
    _validate_queue_snapshot(queue)
    statuses, s4_snapshot_digests = _validated_s4_outcomes(queue, s4_outcomes)
    try:
        ranking = rank_opportunities(queue, limit=20, s4_outcomes=statuses)
    except RecruitmentQualificationError:
        raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID") from None
    if type(editorial_session) is not OpportunityReplyEditorialSession:
        raise ExactContextApprovalError("S9A_SESSION_INVALID")
    candidate = editorial_session.candidate
    try:
        opportunity_id = _validate_uuid(candidate.opportunity_id, "S9A_OPPORTUNITY_INVALID")
    except AttributeError:
        raise ExactContextApprovalError("S9A_SESSION_INVALID") from None
    if candidate.account_uuid != str(account_id):
        raise ExactContextApprovalError("S9A_ACCOUNT_MISMATCH")
    if editorial_session.disposition == "DISCARDED" or editorial_session.current_reply is None:
        raise ExactContextApprovalError("S9A_SESSION_INVALID")

    matches = tuple(entry for entry in queue.opportunities if entry.id == str(opportunity_id))
    if len(matches) != 1:
        raise ExactContextApprovalError("S9A_OPPORTUNITY_INVALID")
    entry = matches[0]
    if entry.source != "MANUAL_URL" or entry.state != "READY":
        raise ExactContextApprovalError("S9A_OPPORTUNITY_INVALID")
    target_url = _canonical_target(entry.target_url)
    if candidate.target_url != target_url:
        raise ExactContextApprovalError("S9A_TARGET_INVALID")
    if (
        entry.context is None
        or entry.context_provenance != "OPERATOR_SUPPLIED"
        or entry.context_completeness != "COMPLETE"
    ):
        raise ExactContextApprovalError("S9A_CONTEXT_INVALID")
    context = _validated_context(entry.context)
    if (
        candidate.context != context
        or candidate.context_provenance != entry.context_provenance
        or candidate.context_completeness != entry.context_completeness
    ):
        raise ExactContextApprovalError("S9A_CONTEXT_INVALID")
    if not _valid_proposal(editorial_session.preview.proposal, candidate):
        raise ExactContextApprovalError("S9A_SESSION_INVALID")
    draft = _validated_draft(editorial_session.current_reply)

    raw_status = statuses.get(target_url)
    if raw_status in _BLOCKING_S4_STATUSES:
        raise ExactContextApprovalError("S9A_S4_BLOCKED")
    qualified = tuple(item for item in ranking.items if item.opportunity_id == entry.id)
    if len(qualified) != 1 or qualified[0].state != "ELIGIBLE_FOR_REVIEW":
        raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID")
    qualification = qualified[0]
    if candidate.qualification != qualification:
        raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID")

    status: ApprovalS4Status = "NO_RECORD" if raw_status is None else raw_status
    s4_snapshot_digest = s4_snapshot_digests.get(
        target_url,
        _s4_snapshot_digest({"status": "NO_RECORD", "evidence": "NO_RECORD"}),
    )

    context_digest = _plain_sha256_text(context, _MAX_CONTEXT_BYTES)
    draft_digest = _plain_sha256_text(draft, 2_000)
    queue_digest = _queue_state_digest(queue)
    qualification_digest = _qualification_digest(qualification)
    payload = _packet_payload(
        account_uuid=str(account_id),
        opportunity_id=entry.id,
        target_url=target_url,
        source=entry.source,
        context_sha256=context_digest,
        context_provenance="OPERATOR_SUPPLIED",
        context_completeness="COMPLETE",
        queue_state_sha256=queue_digest,
        queue_state="READY",
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        qualification=qualification,
        qualification_sha256=qualification_digest,
        s4_status=status,
        s4_snapshot_sha256=s4_snapshot_digest,
        draft_sha256=draft_digest,
        editorial_disposition=editorial_session.disposition,
    )
    fingerprint = _digest_payload(_PACKET_DOMAIN, payload)
    packet = ExactContextReviewPacketV2(
        version=2,
        account_uuid=str(account_id),
        opportunity_id=entry.id,
        target_url=target_url,
        source="MANUAL_URL",
        context=context,
        context_sha256=context_digest,
        context_provenance="OPERATOR_SUPPLIED",
        context_completeness="COMPLETE",
        queue_state_sha256=queue_digest,
        queue_state="READY",
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        qualification=qualification,
        qualification_sha256=qualification_digest,
        s4_status=status,
        s4_snapshot_sha256=s4_snapshot_digest,
        final_draft=draft,
        draft_sha256=draft_digest,
        editorial_disposition=editorial_session.disposition,
        fingerprint=fingerprint,
        confirmation_phrase=f"APPROVE EXACT PACKET S9A V2 {fingerprint}",
    )
    _validate_packet(packet)
    return packet


def render_exact_context_review_packet(packet: ExactContextReviewPacketV2) -> str:
    """Render the complete packet; callers must show it only in a private TTY."""

    _validate_packet(packet)
    qualification = packet.qualification
    reasons = ", ".join(qualification.reason_codes)
    components = ", ".join(
        f"{component.reason_code}:{component.points:+d}"
        for component in qualification.score_components
    )
    s4_note = (
        "ABORTED_NOT_SENT is an operator declaration, not evidence of remote success."
        if packet.s4_status == "ABORTED_NOT_SENT"
        else ""
    )
    lines = [
        "EXACT CONTEXT REVIEW — S9A V2 — PRIVATE LOCAL TERMINAL",
        f"Account UUID: {packet.account_uuid}",
        f"Opportunity ID: {packet.opportunity_id}",
        f"Canonical Threads target: {packet.target_url}",
        f"Source: {packet.source}",
        "Context provenance: OPERATOR_SUPPLIED",
        "Context completeness: COMPLETE (operator assertion only)",
        "Author identity and live conversation coverage remain unverified.",
        f"Queue state: {packet.queue_state}",
        f"Queue snapshot SHA-256: {packet.queue_state_sha256}",
        f"S7: {qualification.classification}/{qualification.state} score={qualification.score}",
        f"S7 job family/location: {qualification.job_family}/{qualification.location_signal}",
        f"S7 reasons: {reasons}",
        f"S7 score components: {components}",
        f"S4 outcome: {packet.s4_status}",
        f"S4 snapshot SHA-256: {packet.s4_snapshot_sha256}",
        s4_note,
        f"S8 editorial disposition: {packet.editorial_disposition} (not send approval)",
        "",
        "FULL REVIEWED CONTEXT (exact stored text):",
        _render_context_lines(packet.context),
        f"Context SHA-256: {packet.context_sha256}",
        "",
        "EXACT FINAL EDITED DRAFT:",
        packet.final_draft,
        f"Draft SHA-256: {packet.draft_sha256}",
        "",
        f"Review packet fingerprint: {packet.fingerprint}",
        "A separate exact reviewer confirmation is required; S8 keep status is not approval.",
        f"Type this exact confirmation after reviewing the packet: {packet.confirmation_phrase}",
    ]
    rendered = "\n".join(line for line in lines if line != "") + "\n"
    try:
        encoded_size = len(rendered.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise ExactContextApprovalError("S9A_PACKET_INVALID") from None
    if encoded_size > _MAX_RENDERED_BYTES:
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    return rendered


def write_exact_context_review_packet(
    packet: ExactContextReviewPacketV2,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environment: Mapping[str, str],
) -> None:
    """Write sensitive review text only when the existing private-TTY gate passes."""

    if not private_review_terminal_available(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environment=environment,
    ):
        raise ExactContextApprovalError("S9A_PRIVATE_TERMINAL_REQUIRED")
    try:
        stdout.write(render_exact_context_review_packet(packet))
    except Exception:  # noqa: BLE001 — stream errors must not disclose review text.
        raise ExactContextApprovalError("S9A_PACKET_INVALID") from None


def approve_exact_context_review(
    packet: ExactContextReviewPacketV2,
    *,
    confirmation: str,
    attempt_id: str,
    now_epoch_seconds: int,
    ttl_seconds: int = 300,
) -> ExactContextApprovalV2:
    """Record a distinct exact human confirmation for one bounded attempt."""

    _validate_packet(packet)
    if type(confirmation) is not str or confirmation != packet.confirmation_phrase:
        raise ExactContextApprovalError("S9A_CONFIRMATION_REQUIRED")
    attempt = _validate_uuid(attempt_id, "S9A_ATTEMPT_INVALID")
    now = _validate_epoch(now_epoch_seconds)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
        raise ExactContextApprovalError("S9A_ATTEMPT_INVALID")
    expires = now + ttl_seconds
    if expires > _MAX_EPOCH_SECONDS:
        raise ExactContextApprovalError("S9A_ATTEMPT_INVALID")
    confirmation_digest = _digest_text(
        _CONFIRMATION_DOMAIN,
        confirmation,
        256,
    )
    payload = _approval_payload(
        version=2,
        account_uuid=packet.account_uuid,
        opportunity_id=packet.opportunity_id,
        target_url=packet.target_url,
        source=packet.source,
        context_provenance=packet.context_provenance,
        context_completeness=packet.context_completeness,
        context_sha256=packet.context_sha256,
        queue_state_sha256=packet.queue_state_sha256,
        qualification_sha256=packet.qualification_sha256,
        s4_status=packet.s4_status,
        s4_snapshot_sha256=packet.s4_snapshot_sha256,
        draft_sha256=packet.draft_sha256,
        editorial_disposition=packet.editorial_disposition,
        packet_fingerprint=packet.fingerprint,
        confirmation_sha256=confirmation_digest,
        attempt_id=str(attempt),
        issued_at_epoch_seconds=now,
        expires_at_epoch_seconds=expires,
    )
    fingerprint = _digest_payload(_APPROVAL_DOMAIN, payload)
    return ExactContextApprovalV2(
        version=2,
        account_uuid=packet.account_uuid,
        opportunity_id=packet.opportunity_id,
        target_url=packet.target_url,
        source=packet.source,
        context_provenance=packet.context_provenance,
        context_completeness=packet.context_completeness,
        context_sha256=packet.context_sha256,
        queue_state_sha256=packet.queue_state_sha256,
        qualification_sha256=packet.qualification_sha256,
        s4_status=packet.s4_status,
        s4_snapshot_sha256=packet.s4_snapshot_sha256,
        draft_sha256=packet.draft_sha256,
        editorial_disposition=packet.editorial_disposition,
        packet_fingerprint=packet.fingerprint,
        confirmation_sha256=confirmation_digest,
        attempt_id=str(attempt),
        issued_at_epoch_seconds=now,
        expires_at_epoch_seconds=expires,
        fingerprint=fingerprint,
    )


def preflight_exact_context_approval(
    fresh_queue: OpportunityQueue,
    fresh_editorial_session: OpportunityReplyEditorialSession,
    approval: ExactContextApprovalV2,
    *,
    account_uuid: str,
    s4_outcomes: Mapping[str, ManualOutcomeStatus | ManualOutcomeRecord | None],
    attempt_id: str,
    now_epoch_seconds: int,
) -> ExactContextPreflightPermitV2:
    """Compare a token with fresh S6/S7/S8/S4 snapshots without side effects."""

    _validate_approval(approval)
    attempt = _validate_uuid(attempt_id, "S9A_ATTEMPT_INVALID")
    now = _validate_epoch(now_epoch_seconds)
    if str(attempt) != approval.attempt_id:
        raise ExactContextApprovalError("S9A_ATTEMPT_MISMATCH")
    if now >= approval.expires_at_epoch_seconds or now < approval.issued_at_epoch_seconds:
        raise ExactContextApprovalError("S9A_APPROVAL_EXPIRED")
    packet = build_exact_context_review_packet(
        fresh_queue,
        fresh_editorial_session,
        account_uuid=account_uuid,
        s4_outcomes=s4_outcomes,
    )
    if (
        packet.fingerprint != approval.packet_fingerprint
        or packet.account_uuid != approval.account_uuid
        or packet.opportunity_id != approval.opportunity_id
        or packet.target_url != approval.target_url
        or packet.source != approval.source
        or packet.context_provenance != approval.context_provenance
        or packet.context_completeness != approval.context_completeness
        or packet.context_sha256 != approval.context_sha256
        or packet.queue_state_sha256 != approval.queue_state_sha256
        or packet.qualification_sha256 != approval.qualification_sha256
        or packet.s4_status != approval.s4_status
        or packet.s4_snapshot_sha256 != approval.s4_snapshot_sha256
        or packet.draft_sha256 != approval.draft_sha256
        or packet.editorial_disposition != approval.editorial_disposition
    ):
        raise ExactContextApprovalError("S9A_APPROVAL_INVALID")
    return ExactContextPreflightPermitV2(
        version=2,
        account_uuid=packet.account_uuid,
        opportunity_id=packet.opportunity_id,
        attempt_id=approval.attempt_id,
        packet_fingerprint=packet.fingerprint,
        approval_fingerprint=approval.fingerprint,
        checked_at_epoch_seconds=now,
    )


def _validate_packet(packet: ExactContextReviewPacketV2) -> None:
    if type(packet) is not ExactContextReviewPacketV2 or packet.version != 2:
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    account_id = _validate_uuid(packet.account_uuid, "S9A_PACKET_INVALID")
    opportunity_id = _validate_uuid(packet.opportunity_id, "S9A_PACKET_INVALID")
    if str(account_id) != packet.account_uuid or str(opportunity_id) != packet.opportunity_id:
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    target_url = _canonical_target(packet.target_url)
    context = _validated_context(packet.context)
    draft = _validated_draft(packet.final_draft)
    if (
        type(packet.account_uuid) is not str
        or type(packet.opportunity_id) is not str
        or type(packet.target_url) is not str
        or type(packet.source) is not str
        or type(packet.context_provenance) is not str
        or type(packet.context_completeness) is not str
        or type(packet.queue_state_sha256) is not str
        or type(packet.queue_state) is not str
        or type(packet.created_at) is not str
        or type(packet.updated_at) is not str
        or type(packet.s4_status) is not str
        or type(packet.s4_snapshot_sha256) is not str
        or type(packet.editorial_disposition) is not str
        or type(packet.fingerprint) is not str
        or type(packet.confirmation_phrase) is not str
        or packet.source != "MANUAL_URL"
        or packet.context_provenance != "OPERATOR_SUPPLIED"
        or packet.context_completeness != "COMPLETE"
        or packet.queue_state != "READY"
        or packet.s4_status not in {"NO_RECORD", *_VALID_S4_STATUSES}
        or packet.s4_status in _BLOCKING_S4_STATUSES
        or packet.editorial_disposition not in {"EDITING", "KEPT_FOR_SESSION"}
        or type(packet.qualification) is not OpportunityQualification
        or packet.qualification.account_uuid != packet.account_uuid
        or packet.qualification.opportunity_id != packet.opportunity_id
        or packet.qualification.target_url != target_url
        or packet.qualification.source != packet.source
        or packet.qualification.state != "ELIGIBLE_FOR_REVIEW"
        or packet.qualification.context_completeness != "COMPLETE"
        or not _is_sha256(packet.context_sha256)
        or not _is_sha256(packet.queue_state_sha256)
        or not _is_sha256(packet.qualification_sha256)
        or not _is_sha256(packet.s4_snapshot_sha256)
        or not _is_sha256(packet.draft_sha256)
        or packet.context_sha256 != _plain_sha256_text(context, _MAX_CONTEXT_BYTES)
        or packet.draft_sha256 != _plain_sha256_text(draft, 2_000)
        or packet.qualification_sha256 != _qualification_digest(packet.qualification)
        or not _valid_s6_timestamp(packet.created_at)
        or not _valid_s6_timestamp(packet.updated_at)
    ):
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    payload = _packet_payload(
        account_uuid=packet.account_uuid,
        opportunity_id=packet.opportunity_id,
        target_url=target_url,
        source=packet.source,
        context_sha256=packet.context_sha256,
        context_provenance=packet.context_provenance,
        context_completeness=packet.context_completeness,
        queue_state_sha256=packet.queue_state_sha256,
        queue_state=packet.queue_state,
        created_at=packet.created_at,
        updated_at=packet.updated_at,
        qualification=packet.qualification,
        qualification_sha256=packet.qualification_sha256,
        s4_status=packet.s4_status,
        s4_snapshot_sha256=packet.s4_snapshot_sha256,
        draft_sha256=packet.draft_sha256,
        editorial_disposition=packet.editorial_disposition,
    )
    if (
        packet.fingerprint != _digest_payload(_PACKET_DOMAIN, payload)
        or packet.confirmation_phrase != f"APPROVE EXACT PACKET S9A V2 {packet.fingerprint}"
    ):
        raise ExactContextApprovalError("S9A_PACKET_INVALID")


def _validate_approval(approval: ExactContextApprovalV2) -> None:
    if type(approval) is not ExactContextApprovalV2 or approval.version != 2:
        raise ExactContextApprovalError("S9A_APPROVAL_INVALID")
    if any(
        type(value) is not str
        for value in (
            approval.account_uuid,
            approval.opportunity_id,
            approval.target_url,
            approval.source,
            approval.context_provenance,
            approval.context_completeness,
            approval.s4_status,
            approval.s4_snapshot_sha256,
            approval.editorial_disposition,
            approval.packet_fingerprint,
            approval.confirmation_sha256,
            approval.attempt_id,
            approval.fingerprint,
        )
    ):
        raise ExactContextApprovalError("S9A_APPROVAL_INVALID")
    account_id = _validate_uuid(approval.account_uuid, "S9A_APPROVAL_INVALID")
    opportunity_id = _validate_uuid(approval.opportunity_id, "S9A_APPROVAL_INVALID")
    attempt_id = _validate_uuid(approval.attempt_id, "S9A_APPROVAL_INVALID")
    if (
        str(account_id) != approval.account_uuid
        or str(opportunity_id) != approval.opportunity_id
        or str(attempt_id) != approval.attempt_id
        or _canonical_target(approval.target_url) != approval.target_url
        or approval.source != "MANUAL_URL"
        or approval.context_provenance != "OPERATOR_SUPPLIED"
        or approval.context_completeness != "COMPLETE"
        or approval.s4_status not in {"NO_RECORD", *_VALID_S4_STATUSES}
        or approval.s4_status in _BLOCKING_S4_STATUSES
        or approval.editorial_disposition not in {"EDITING", "KEPT_FOR_SESSION"}
        or not _is_sha256(approval.context_sha256)
        or not _is_sha256(approval.queue_state_sha256)
        or not _is_sha256(approval.qualification_sha256)
        or not _is_sha256(approval.s4_snapshot_sha256)
        or not _is_sha256(approval.draft_sha256)
        or not _is_sha256(approval.packet_fingerprint)
        or not _is_sha256(approval.confirmation_sha256)
        or type(approval.issued_at_epoch_seconds) is not int
        or type(approval.expires_at_epoch_seconds) is not int
        or approval.issued_at_epoch_seconds > _MAX_EPOCH_SECONDS
        or approval.expires_at_epoch_seconds > _MAX_EPOCH_SECONDS
        or not 0 <= approval.issued_at_epoch_seconds < approval.expires_at_epoch_seconds
        or approval.expires_at_epoch_seconds - approval.issued_at_epoch_seconds > _MAX_TTL_SECONDS
        or approval.confirmation_sha256
        != _digest_text(
            _CONFIRMATION_DOMAIN,
            f"APPROVE EXACT PACKET S9A V2 {approval.packet_fingerprint}",
            256,
        )
    ):
        raise ExactContextApprovalError("S9A_APPROVAL_INVALID")
    payload = _approval_payload(
        version=approval.version,
        account_uuid=approval.account_uuid,
        opportunity_id=approval.opportunity_id,
        target_url=approval.target_url,
        source=approval.source,
        context_provenance=approval.context_provenance,
        context_completeness=approval.context_completeness,
        context_sha256=approval.context_sha256,
        queue_state_sha256=approval.queue_state_sha256,
        qualification_sha256=approval.qualification_sha256,
        s4_status=approval.s4_status,
        s4_snapshot_sha256=approval.s4_snapshot_sha256,
        draft_sha256=approval.draft_sha256,
        editorial_disposition=approval.editorial_disposition,
        packet_fingerprint=approval.packet_fingerprint,
        confirmation_sha256=approval.confirmation_sha256,
        attempt_id=approval.attempt_id,
        issued_at_epoch_seconds=approval.issued_at_epoch_seconds,
        expires_at_epoch_seconds=approval.expires_at_epoch_seconds,
    )
    if approval.fingerprint != _digest_payload(_APPROVAL_DOMAIN, payload):
        raise ExactContextApprovalError("S9A_APPROVAL_INVALID")


def _valid_proposal(proposal: OpportunityReplyProposal, candidate: object) -> bool:
    return (
        type(proposal) is OpportunityReplyProposal
        and proposal.candidate is candidate
        and proposal.source == "FAKE_SYNTHETIC"
        and proposal.status == "PROPOSED_FOR_HUMAN_REVIEW"
        and proposal.approval_status == "NOT_APPROVED_FOR_SENDING"
        and proposal.delivery_status == "NOT_SENT"
    )


def _validated_s4_outcomes(
    queue: OpportunityQueue,
    outcomes: Mapping[str, ManualOutcomeStatus | ManualOutcomeRecord | None],
) -> tuple[dict[str, ManualOutcomeStatus], dict[str, str]]:
    valid_targets = {entry.target_url for entry in queue.opportunities}
    result: dict[str, ManualOutcomeStatus] = {}
    digests: dict[str, str] = {}
    try:
        values = tuple(outcomes.items())
    except Exception:  # noqa: BLE001 — untrusted adapters must fail closed.
        raise ExactContextApprovalError("S9A_S4_STATUS_INVALID") from None
    for target, snapshot in values:
        if type(target) is not str or target not in valid_targets:
            raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
        if _canonical_target(target) != target:
            raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
        if snapshot is None:
            digests[target] = _s4_snapshot_digest({"status": "NO_RECORD", "evidence": "NO_RECORD"})
        elif type(snapshot) is str and snapshot in _VALID_S4_STATUSES:
            status = cast(ManualOutcomeStatus, snapshot)
            result[target] = status
            digests[target] = _s4_snapshot_digest({"status": status, "evidence": "STATUS_ONLY"})
        elif type(snapshot) is ManualOutcomeRecord:
            status, digest = _manual_outcome_snapshot(queue, target, snapshot)
            result[target] = status
            digests[target] = digest
        else:
            raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
    return result, digests


def _manual_outcome_snapshot(
    queue: OpportunityQueue,
    target_url: str,
    record: ManualOutcomeRecord,
) -> tuple[ManualOutcomeStatus, str]:
    try:
        account_id = UUID(queue.account_uuid)
        expected_fingerprint = ManualOutcomeStore.target_fingerprint(account_id, target_url)
    except Exception:  # noqa: BLE001 — a supplied journal snapshot must fail closed.
        raise ExactContextApprovalError("S9A_S4_STATUS_INVALID") from None
    if (
        type(record.account_uuid) is not str
        or record.account_uuid != queue.account_uuid
        or type(record.target_fingerprint) is not str
        or record.target_fingerprint != expected_fingerprint
        or type(record.events) is not tuple
        or not 1 <= len(record.events) <= 16
    ):
        raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
    events: list[dict[str, str]] = []
    for event in record.events:
        if type(event) is not ManualOutcomeEvent:
            raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
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
            raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
        if (
            not _valid_uuid_v4(event.event_id)
            or not _valid_uuid_v4(event.attempt_id)
            or not _is_sha256(event.draft_sha256)
            or not _valid_s6_timestamp(event.recorded_at)
            or event.status not in _VALID_S4_STATUSES
            or event.source not in {"SOFTWARE_HANDOFF", "OPERATOR", "LEGACY_OPERATOR"}
            or event.verification != "UNVERIFIED"
        ):
            raise ExactContextApprovalError("S9A_S4_STATUS_INVALID")
        events.append(
            {
                "event_id": event.event_id,
                "attempt_id": event.attempt_id,
                "draft_sha256": event.draft_sha256,
                "recorded_at": event.recorded_at,
                "status": event.status,
                "source": event.source,
                "verification": event.verification,
            }
        )
    return record.events[-1].status, _s4_snapshot_digest(
        {
            "account_uuid": record.account_uuid,
            "target_fingerprint": record.target_fingerprint,
            "events": events,
        }
    )


def _valid_uuid_v4(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except ValueError, TypeError, AttributeError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _s4_snapshot_digest(value: object) -> str:
    return _digest_payload(_S4_SNAPSHOT_DOMAIN, value)


def _validate_queue_snapshot(queue: OpportunityQueue) -> None:
    if (
        type(queue) is not OpportunityQueue
        or type(queue.opportunities) is not tuple
        or len(queue.opportunities) > _MAX_QUEUE_ENTRIES
    ):
        raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID")
    for entry in queue.opportunities:
        if type(entry) is not OpportunityQueueEntry:
            raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID")
        if type(entry.created_at) is not str or type(entry.updated_at) is not str:
            raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID")
        try:
            _parse_s6_timestamp(entry.created_at)
            _parse_s6_timestamp(entry.updated_at)
        except ValueError:
            raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID") from None
        if entry.context is not None:
            try:
                _validated_context(entry.context)
            except ExactContextApprovalError:
                raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID") from None
        if entry.skip_reason is not None and (
            type(entry.skip_reason) is not str
            or not entry.skip_reason
            or entry.skip_reason != entry.skip_reason.strip()
            or len(entry.skip_reason) > 1_000
            or _has_forbidden_text(entry.skip_reason, allow_newline=False)
        ):
            raise ExactContextApprovalError("S9A_QUALIFICATION_INVALID")


def _validated_context(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_CONTEXT_CHARS
        or _has_forbidden_text(value, allow_newline=True)
    ):
        raise ExactContextApprovalError("S9A_CONTEXT_INVALID")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise ExactContextApprovalError("S9A_CONTEXT_INVALID") from None
    if size > _MAX_CONTEXT_BYTES:
        raise ExactContextApprovalError("S9A_CONTEXT_INVALID")
    return value


def _validated_draft(value: object) -> str:
    try:
        draft = validate_reply_text(value)
        if len(draft.encode("utf-8", errors="strict")) > 2_000:
            raise ReplyProposalError("INVALID_EDITED_REPLY")
    except ReplyProposalError, UnicodeError:
        raise ExactContextApprovalError("S9A_SESSION_INVALID") from None
    return draft


def _has_forbidden_text(value: str, *, allow_newline: bool) -> bool:
    for character in value:
        category = unicodedata.category(character)
        if allow_newline and character == "\n":
            continue
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            return True
        if category == "Zs" and character != " ":
            return True
    return False


def _render_context_lines(value: str) -> str:
    """Prefix every source line so multiline context cannot spoof packet labels."""

    return "\n".join(f"| {line}" for line in value.split("\n"))


def _canonical_target(value: object) -> str:
    if type(value) is not str or len(value) > 2_048:
        raise ExactContextApprovalError("S9A_TARGET_INVALID")
    try:
        if len(value.encode("utf-8", errors="strict")) > 2_048:
            raise ExactContextApprovalError("S9A_TARGET_INVALID")
        normalized = normalize_manual_permalink(value).thread_ref
    except BrowserContractError, UnicodeError, ValueError:
        raise ExactContextApprovalError("S9A_TARGET_INVALID") from None
    if normalized != value:
        raise ExactContextApprovalError("S9A_TARGET_INVALID")
    return normalized


def _validate_uuid(value: object, code: str) -> UUID:
    try:
        parsed = UUID(cast(str, value))
    except ValueError, TypeError, AttributeError:
        raise ExactContextApprovalError(code) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ExactContextApprovalError(code)
    return parsed


def _validate_epoch(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_EPOCH_SECONDS:
        raise ExactContextApprovalError("S9A_ATTEMPT_INVALID")
    return value


def _parse_s6_timestamp(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError
    return parsed


def _valid_s6_timestamp(value: str) -> bool:
    try:
        _parse_s6_timestamp(value)
    except ValueError:
        return False
    return True


def _queue_state_digest(queue: OpportunityQueue) -> str:
    entries: list[dict[str, object]] = []
    for entry in queue.opportunities:
        context_digest = (
            None if entry.context is None else _plain_sha256_text(entry.context, _MAX_CONTEXT_BYTES)
        )
        skip_reason_digest = (
            None if entry.skip_reason is None else _plain_sha256_text(entry.skip_reason, 4_000)
        )
        entries.append(
            {
                "id": entry.id,
                "target_url": entry.target_url,
                "source": entry.source,
                "context_sha256": context_digest,
                "context_provenance": entry.context_provenance,
                "context_completeness": entry.context_completeness,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "state": entry.state,
                "skip_reason_sha256": skip_reason_digest,
            }
        )
    return _digest_payload(
        _QUEUE_DOMAIN,
        {"account_uuid": queue.account_uuid, "opportunities": entries},
    )


def _qualification_digest(qualification: OpportunityQualification) -> str:
    return _digest_payload(_QUALIFICATION_DOMAIN, _qualification_payload(qualification))


def _qualification_payload(qualification: OpportunityQualification) -> dict[str, object]:
    return {
        "rank": qualification.rank,
        "input_position": qualification.input_position,
        "account_uuid": qualification.account_uuid,
        "opportunity_id": qualification.opportunity_id,
        "target_url": qualification.target_url,
        "source": qualification.source,
        "classification": qualification.classification,
        "state": qualification.state,
        "score": qualification.score,
        "job_family": qualification.job_family,
        "location_signal": qualification.location_signal,
        "location_relevance": qualification.location_relevance,
        "context_completeness": qualification.context_completeness,
        "recency_signal": qualification.recency_signal,
        "unanswered_status": qualification.unanswered_status,
        "reason_codes": qualification.reason_codes,
        "score_components": tuple(
            {"reason_code": component.reason_code, "points": component.points}
            for component in qualification.score_components
        ),
    }


def _packet_payload(
    *,
    account_uuid: str,
    opportunity_id: str,
    target_url: str,
    source: str,
    context_sha256: str,
    context_provenance: str,
    context_completeness: str,
    queue_state_sha256: str,
    queue_state: str,
    created_at: str,
    updated_at: str,
    qualification: OpportunityQualification,
    qualification_sha256: str,
    s4_status: ApprovalS4Status,
    s4_snapshot_sha256: str,
    draft_sha256: str,
    editorial_disposition: str,
) -> dict[str, object]:
    return {
        "version": 2,
        "account_uuid": account_uuid,
        "opportunity_id": opportunity_id,
        "target_url": target_url,
        "source": source,
        "context_sha256": context_sha256,
        "context_provenance": context_provenance,
        "context_completeness": context_completeness,
        "queue_state_sha256": queue_state_sha256,
        "queue_state": queue_state,
        "created_at": created_at,
        "updated_at": updated_at,
        "qualification": _qualification_payload(qualification),
        "qualification_sha256": qualification_sha256,
        "s4_status": s4_status,
        "s4_snapshot_sha256": s4_snapshot_sha256,
        "draft_sha256": draft_sha256,
        "editorial_disposition": editorial_disposition,
    }


def _approval_payload(
    *,
    version: int,
    account_uuid: str,
    opportunity_id: str,
    target_url: str,
    source: str,
    context_provenance: str,
    context_completeness: str,
    context_sha256: str,
    queue_state_sha256: str,
    qualification_sha256: str,
    s4_status: ApprovalS4Status,
    s4_snapshot_sha256: str,
    draft_sha256: str,
    editorial_disposition: str,
    packet_fingerprint: str,
    confirmation_sha256: str,
    attempt_id: str,
    issued_at_epoch_seconds: int,
    expires_at_epoch_seconds: int,
) -> dict[str, object]:
    return {
        "version": version,
        "account_uuid": account_uuid,
        "opportunity_id": opportunity_id,
        "target_url": target_url,
        "source": source,
        "context_provenance": context_provenance,
        "context_completeness": context_completeness,
        "context_sha256": context_sha256,
        "queue_state_sha256": queue_state_sha256,
        "qualification_sha256": qualification_sha256,
        "s4_status": s4_status,
        "s4_snapshot_sha256": s4_snapshot_sha256,
        "draft_sha256": draft_sha256,
        "editorial_disposition": editorial_disposition,
        "packet_fingerprint": packet_fingerprint,
        "confirmation_sha256": confirmation_sha256,
        "attempt_id": attempt_id,
        "issued_at_epoch_seconds": issued_at_epoch_seconds,
        "expires_at_epoch_seconds": expires_at_epoch_seconds,
    }


def _digest_text(domain: bytes, value: str, max_bytes: int) -> str:
    try:
        payload = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ExactContextApprovalError("S9A_PACKET_INVALID") from None
    if len(payload) > max_bytes:
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    return _sha256(domain, payload)


def _plain_sha256_text(value: str, max_bytes: int) -> str:
    try:
        payload = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ExactContextApprovalError("S9A_PACKET_INVALID") from None
    if len(payload) > max_bytes:
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    return hashlib.sha256(payload).hexdigest()


def _digest_payload(domain: bytes, value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except TypeError, ValueError, UnicodeError:
        raise ExactContextApprovalError("S9A_PACKET_INVALID") from None
    if len(payload) > _MAX_CANONICAL_BYTES:
        raise ExactContextApprovalError("S9A_PACKET_INVALID")
    return _sha256(domain, payload)


def _sha256(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + len(payload).to_bytes(8, "big") + payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
