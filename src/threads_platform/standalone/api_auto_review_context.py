"""Pure exact-context binding for a future private operator review surface.

This module validates synthetic or already-observed DTOs in memory. It does not
authenticate a Threads response and has no I/O, clock, TTY, storage, or dispatch
capability.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from threads_platform.api.contracts import RemoteReply
from threads_platform.standalone.api_auto_content import (
    ContentDecision,
    ContentOrigin,
    ReviewContentPacket,
    ReviewPacketEvaluation,
    RightsEvidence,
    revalidate_review_packet,
)
from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    CampaignScope,
    CandidateSource,
    ContextCompleteness,
    CyclePlan,
    ObservationProvenance,
    ObservedCandidate,
    PlanDecision,
    PlanItem,
    PlanReason,
    ReviewIntent,
    TargetKind,
    VerifiedTargetBinding,
    plan_cycle,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REMOTE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_MAX_CONTEXT_CODEPOINTS = 4096
_MAX_CONTEXT_TEXT_BYTES = 16 * 1024
_MAX_TOTAL_CONTEXT_BYTES = 24 * 1024
_DISPLAY_CONTEXT_DOMAIN = b"threads-standalone:api-auto:review-display-context:v1\0"
_PREVIEW_DOMAIN = b"threads-standalone:api-auto:private-review-preview:v1\0"


class PrivateReviewDecision(StrEnum):
    READY_FOR_PRIVATE_TTY = "READY_FOR_PRIVATE_TTY"
    STOP = "STOP"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class PrivateReviewReason(StrEnum):
    CANDIDATE_NOT_ELIGIBLE = "CANDIDATE_NOT_ELIGIBLE"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    CONTEXT_INVALID = "CONTEXT_INVALID"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    CONTEXT_TOO_LONG = "CONTEXT_TOO_LONG"
    DISPLAY_BINDING_MISMATCH = "DISPLAY_BINDING_MISMATCH"
    DISPLAY_TIMESTAMP_MISMATCH = "DISPLAY_TIMESTAMP_MISMATCH"
    INVALID_INPUT = "INVALID_INPUT"
    PACKET_REVALIDATION_FAILED = "PACKET_REVALIDATION_FAILED"
    PLANNER_SELECTION_INVALID = "PLANNER_SELECTION_INVALID"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"


@dataclass(frozen=True, slots=True, repr=False)
class ReviewDisplaySnapshot:
    """Current displayable context supplied by an independent reader."""

    account_id: UUID = field(repr=False)
    actor_id: str = field(repr=False)
    action: ActionKind
    source: CandidateSource
    provenance: ObservationProvenance
    candidate_fingerprint: str = field(repr=False)
    thread_id: str | None = field(repr=False)
    reply_id: str | None = field(repr=False)
    root_id: str | None = field(repr=False)
    parent_id: str | None = field(repr=False)
    reply_text: str | None = field(repr=False)
    root_text: str | None = field(repr=False)
    parent_text: str | None = field(repr=False)
    observed_at: datetime

    def __repr__(self) -> str:
        action = self.action.value if type(self.action) is ActionKind else "INVALID"
        source = self.source.value if type(self.source) is CandidateSource else "INVALID"
        return (
            f"ReviewDisplaySnapshot(action={action}, source={source}, private_context=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PrivateReviewPreview:
    """Ephemeral in-memory preview; it contains no approval or dispatch authority."""

    account_id: UUID = field(repr=False)
    actor_id: str = field(repr=False)
    action: ActionKind
    source: CandidateSource
    provenance: ObservationProvenance
    candidate_fingerprint: str = field(repr=False)
    target_sha256: str = field(repr=False)
    context_sha256: str = field(repr=False)
    context_evidence_sha256: str = field(repr=False)
    policy_sha256: str = field(repr=False)
    root_sha256: str | None = field(repr=False)
    parent_sha256: str | None = field(repr=False)
    reply_sha256: str | None = field(repr=False)
    thread_id: str | None = field(repr=False)
    reply_id: str | None = field(repr=False)
    root_id: str | None = field(repr=False)
    parent_id: str | None = field(repr=False)
    reply_text: str | None = field(repr=False)
    root_text: str | None = field(repr=False)
    parent_text: str | None = field(repr=False)
    final_content_text: str = field(repr=False)
    content_origin: ContentOrigin
    rights_evidence: RightsEvidence = field(repr=False)
    normalization_version: str = field(repr=False)
    packet_revision_sha256: str = field(repr=False)
    display_context_sha256: str = field(repr=False)
    packet_expires_at: datetime = field(repr=False)
    content_expires_at: datetime = field(repr=False)
    expires_at: datetime = field(repr=False)
    preview_sha256: str = field(repr=False)

    def __repr__(self) -> str:
        action = self.action.value if type(self.action) is ActionKind else "INVALID"
        source = self.source.value if type(self.source) is CandidateSource else "INVALID"
        return f"PrivateReviewPreview(action={action}, source={source}, private_preview=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class PrivateReviewPreviewResult:
    decision: PrivateReviewDecision
    reason_codes: tuple[PrivateReviewReason, ...]
    preview: PrivateReviewPreview | None = field(repr=False)

    def __repr__(self) -> str:
        decision = (
            self.decision.value if type(self.decision) is PrivateReviewDecision else "INVALID"
        )
        reasons = (
            tuple(
                reason.value for reason in self.reason_codes if type(reason) is PrivateReviewReason
            )
            if type(self.reason_codes) is tuple
            else ("INVALID",)
        )
        return f"PrivateReviewPreviewResult(decision={decision}, reason_codes={reasons!r}, preview_present={self.preview is not None})"


def review_display_context_sha256(snapshot: ReviewDisplaySnapshot) -> str:
    """Return the versioned digest of normalized visible context, or ``""`` if invalid.

    Observation time is checked as input but deliberately excluded from this
    digest, so a fresh read of unchanged display context hashes identically.
    """

    payload, _texts, reason = _display_payload(snapshot)
    if reason is not None or payload is None:
        return ""
    return _domain_digest(_DISPLAY_CONTEXT_DOMAIN, payload)


def build_private_review_preview(
    packet: ReviewContentPacket,
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    display: ReviewDisplaySnapshot,
    *,
    now: datetime,
) -> PrivateReviewPreviewResult:
    """Bind a validated content packet to exact displayable target context.

    The result is inert data for a separately gated private TTY step. This
    function never prints, approves, persists, or dispatches the preview.
    """

    if (
        type(packet) is not ReviewContentPacket
        or type(scope) is not CampaignScope
        or type(candidates) is not tuple
        or any(type(candidate) is not ObservedCandidate for candidate in candidates)
        or type(display) is not ReviewDisplaySnapshot
        or not _is_utc(now)
    ):
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.INVALID_INPUT)

    try:
        validated = revalidate_review_packet(packet, scope, candidates, now=now)
    except Exception:  # noqa: BLE001 - malformed packet data must fail closed
        return _result(
            PrivateReviewDecision.STOP,
            PrivateReviewReason.PACKET_REVALIDATION_FAILED,
        )
    if type(validated) is not ReviewPacketEvaluation:
        return _result(
            PrivateReviewDecision.STOP,
            PrivateReviewReason.PACKET_REVALIDATION_FAILED,
        )
    if validated.decision is ContentDecision.NOT_ELIGIBLE:
        return _result(
            PrivateReviewDecision.NOT_ELIGIBLE,
            PrivateReviewReason.CANDIDATE_NOT_ELIGIBLE,
        )
    if (
        validated.decision is not ContentDecision.NEEDS_REVIEW
        or validated.reason_codes != ()
        or validated.packet is not packet
    ):
        return _result(
            PrivateReviewDecision.STOP,
            PrivateReviewReason.PACKET_REVALIDATION_FAILED,
        )

    candidate, intent, selection_reason = _selected_candidate(
        packet,
        scope,
        candidates,
        now=now,
    )
    if selection_reason is not None or candidate is None or intent is None:
        return _result(
            PrivateReviewDecision.STOP,
            selection_reason or PrivateReviewReason.PLANNER_SELECTION_INVALID,
        )

    payload, normalized_texts, display_reason = _display_payload(display)
    if display_reason is not None or payload is None or normalized_texts is None:
        return _result(
            PrivateReviewDecision.STOP, display_reason or PrivateReviewReason.CONTEXT_INVALID
        )
    display_digest = _domain_digest(_DISPLAY_CONTEXT_DOMAIN, payload)

    binding = candidate.binding
    if type(binding) is not VerifiedTargetBinding:
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.TARGET_MISMATCH)
    if (
        display.account_id != scope.account_id
        or display.account_id != candidate.account_id
        or display.actor_id != scope.actor_id
        or display.actor_id != binding.actor_id
        or display.action is not candidate.action
        or display.action is not packet.binding.action
        or display.source is not candidate.source
        or display.source is not packet.binding.source
        or display.provenance is not candidate.provenance
        or display.provenance is not binding.provenance
        or display.provenance is not packet.binding.provenance
        or display.candidate_fingerprint != candidate.fingerprint
        or display.candidate_fingerprint != packet.binding.candidate_fingerprint
    ):
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.DISPLAY_BINDING_MISMATCH)
    if display.provenance is ObservationProvenance.UNKNOWN:
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.PROVENANCE_MISMATCH)
    if display.observed_at != binding.observed_at:
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.DISPLAY_TIMESTAMP_MISMATCH)
    if (
        display.thread_id != binding.thread_id
        or display.reply_id != binding.reply_id
        or display.root_id != binding.root_id
        or display.parent_id != binding.parent_id
    ):
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.TARGET_MISMATCH)
    if (
        display_digest != binding.context_evidence_sha256
        or display_digest != packet.binding.context_evidence_sha256
        or not _intent_matches_packet_binding(intent, packet)
    ):
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.CONTEXT_MISMATCH)

    reply_text, root_text, parent_text = normalized_texts
    if candidate.action is ActionKind.POST_TEXT:
        if (
            candidate.source is not CandidateSource.OWN_ACCOUNT
            or binding.target_kind is not TargetKind.OWN_ACCOUNT
            or binding.actor_verified is not True
            or binding.source_verified is not True
            or binding.root_owned_by_account is not True
            or any(
                value is not None
                for value in (
                    display.thread_id,
                    display.reply_id,
                    display.root_id,
                    display.parent_id,
                    reply_text,
                    root_text,
                    parent_text,
                    binding.reply_observation,
                )
            )
        ):
            return _result(PrivateReviewDecision.STOP, PrivateReviewReason.TARGET_MISMATCH)
    elif candidate.action is ActionKind.REPLY:
        observation = binding.reply_observation
        if (
            candidate.source is not CandidateSource.INBOUND_REPLY
            or binding.target_kind is not TargetKind.INBOUND_REPLY
            or binding.completeness is not ContextCompleteness.COMPLETE
            or binding.actor_verified is not True
            or binding.source_verified is not True
            or binding.relationship_verified is not True
            or binding.root_owned_by_account is not True
            or binding.reply_owned_by_operator is not False
            or binding.public_context is not True
            or binding.reply_control_allows_reply is not True
            or display.thread_id is None
            or display.thread_id != display.root_id
            or display.reply_id is None
            or display.root_id is None
            or display.parent_id is None
            or display.reply_id == display.root_id
            or type(observation) is not RemoteReply
            or observation.reply_id != binding.reply_id
            or observation.root_post_id != binding.root_id
            or observation.replied_to_id != binding.parent_id
            or observation.is_reply_owned_by_me is not False
            or reply_text is None
            or root_text is None
            or parent_text is None
        ):
            return _result(PrivateReviewDecision.STOP, PrivateReviewReason.CONTEXT_INCOMPLETE)
        observed_reply_text, text_reason = _normalize_context_text(observation.text, required=True)
        if text_reason is not None or observed_reply_text != reply_text:
            return _result(PrivateReviewDecision.STOP, PrivateReviewReason.CONTEXT_MISMATCH)
        if display.parent_id == display.root_id and parent_text != root_text:
            return _result(PrivateReviewDecision.STOP, PrivateReviewReason.CONTEXT_MISMATCH)
    else:
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.UNSUPPORTED_ACTION)

    prepared = packet.prepared_content
    rights = prepared.rights
    if type(rights) is not RightsEvidence:
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.PACKET_REVALIDATION_FAILED)
    packet_expiry = packet.binding.expires_at
    content_expiry = prepared.expires_at
    expires_at = min(packet_expiry, content_expiry)
    if not _is_utc(packet_expiry) or not _is_utc(content_expiry) or expires_at <= now:
        return _result(PrivateReviewDecision.STOP, PrivateReviewReason.PACKET_REVALIDATION_FAILED)

    preview_digest = _preview_digest(packet, display, display_digest, expires_at)
    preview = PrivateReviewPreview(
        account_id=scope.account_id,
        actor_id=scope.actor_id,
        action=candidate.action,
        source=candidate.source,
        provenance=candidate.provenance,
        candidate_fingerprint=candidate.fingerprint,
        target_sha256=packet.binding.target_sha256,
        context_sha256=packet.binding.context_sha256,
        context_evidence_sha256=packet.binding.context_evidence_sha256,
        policy_sha256=packet.binding.policy_sha256,
        root_sha256=packet.binding.root_sha256,
        parent_sha256=packet.binding.parent_sha256,
        reply_sha256=packet.binding.reply_sha256,
        thread_id=display.thread_id,
        reply_id=display.reply_id,
        root_id=display.root_id,
        parent_id=display.parent_id,
        reply_text=reply_text,
        root_text=root_text,
        parent_text=parent_text,
        final_content_text=prepared.text,
        content_origin=prepared.origin,
        rights_evidence=rights,
        normalization_version=packet.normalization_version,
        packet_revision_sha256=packet.packet_revision_sha256,
        display_context_sha256=display_digest,
        packet_expires_at=packet_expiry,
        content_expires_at=content_expiry,
        expires_at=expires_at,
        preview_sha256=preview_digest,
    )
    return PrivateReviewPreviewResult(
        PrivateReviewDecision.READY_FOR_PRIVATE_TTY,
        (),
        preview,
    )


def _selected_candidate(
    packet: ReviewContentPacket,
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    *,
    now: datetime,
) -> tuple[ObservedCandidate | None, ReviewIntent | None, PrivateReviewReason | None]:
    try:
        plan = plan_cycle(scope, candidates, None, now=now)
    except Exception:  # noqa: BLE001 - planner errors must fail closed
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID
    if (
        type(plan) is not CyclePlan
        or plan.decision in {PlanDecision.STOP, PlanDecision.NEEDS_CONTEXT}
        or plan.decision not in {PlanDecision.OBSERVE_ONLY, PlanDecision.NOT_ELIGIBLE}
        or plan.reason_codes != ()
        or type(plan.items) is not tuple
        or any(type(item) is not PlanItem for item in plan.items)
    ):
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID

    intent_items = tuple(item for item in plan.items if item.review_intent is not None)
    if len(intent_items) != 1:
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID
    selected_item = intent_items[0]
    intent = selected_item.review_intent
    if (
        selected_item.decision is not PlanDecision.OBSERVE_ONLY
        or selected_item.reason_codes != (PlanReason.DRAFT_UNAVAILABLE,)
        or selected_item.proposal is not None
        or type(intent) is not ReviewIntent
        or intent.dispatchable is not False
    ):
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID

    allowed_ineligible = {
        (PlanReason.COOLDOWN,),
        (PlanReason.NOT_ELIGIBLE,),
        (PlanReason.QUOTA_EXHAUSTED,),
    }
    for item in plan.items:
        if item is selected_item:
            continue
        if item.proposal is not None or item.review_intent is not None:
            return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID
        if item.decision is PlanDecision.OBSERVE_ONLY and item.reason_codes == (
            PlanReason.DRAFT_UNAVAILABLE,
        ):
            continue
        if item.decision is PlanDecision.NOT_ELIGIBLE and item.reason_codes in allowed_ineligible:
            continue
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID

    matches = tuple(
        candidate
        for candidate in candidates
        if candidate.account_id == packet.binding.account_id
        and candidate.action is packet.binding.action
        and candidate.source is packet.binding.source
        and candidate.provenance is packet.binding.provenance
        and candidate.fingerprint == packet.binding.candidate_fingerprint
    )
    if len(matches) != 1:
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID
    candidate = matches[0]
    if (
        candidate.fingerprint != intent.candidate_fingerprint
        or candidate.account_id != intent.account_id
        or candidate.action is not intent.action
        or candidate.source is not intent.source
        or candidate.provenance is not intent.provenance
        or intent.target_sha256 != packet.binding.target_sha256
        or intent.context_sha256 != packet.binding.context_sha256
        or intent.context_evidence_sha256 != packet.binding.context_evidence_sha256
        or intent.root_sha256 != packet.binding.root_sha256
        or intent.parent_sha256 != packet.binding.parent_sha256
        or intent.reply_sha256 != packet.binding.reply_sha256
        or intent.policy_sha256 != packet.binding.policy_sha256
        or intent.required_permissions != packet.binding.required_permissions
        or intent.quota_used != packet.binding.quota_used
        or intent.quota_total != packet.binding.quota_total
    ):
        return None, None, PrivateReviewReason.PLANNER_SELECTION_INVALID
    return candidate, intent, None


def _intent_matches_packet_binding(
    intent: ReviewIntent,
    packet: ReviewContentPacket,
) -> bool:
    binding = packet.binding
    return (
        intent.account_id == binding.account_id
        and intent.action is binding.action
        and intent.source is binding.source
        and intent.provenance is binding.provenance
        and intent.candidate_fingerprint == binding.candidate_fingerprint
        and intent.target_sha256 == binding.target_sha256
        and intent.context_sha256 == binding.context_sha256
        and intent.context_evidence_sha256 == binding.context_evidence_sha256
        and intent.root_sha256 == binding.root_sha256
        and intent.parent_sha256 == binding.parent_sha256
        and intent.reply_sha256 == binding.reply_sha256
        and intent.policy_sha256 == binding.policy_sha256
        and intent.required_permissions == binding.required_permissions
        and intent.quota_used == binding.quota_used
        and intent.quota_total == binding.quota_total
    )


def _display_payload(
    snapshot: ReviewDisplaySnapshot,
) -> tuple[
    dict[str, object] | None,
    tuple[str | None, str | None, str | None] | None,
    PrivateReviewReason | None,
]:
    if (
        type(snapshot) is not ReviewDisplaySnapshot
        or type(snapshot.account_id) is not UUID
        or not _valid_remote_id(snapshot.actor_id)
        or type(snapshot.action) is not ActionKind
        or type(snapshot.source) is not CandidateSource
        or type(snapshot.provenance) is not ObservationProvenance
        or not _is_sha256(snapshot.candidate_fingerprint)
        or not _is_utc(snapshot.observed_at)
    ):
        return None, None, PrivateReviewReason.INVALID_INPUT

    if snapshot.action is ActionKind.POST_TEXT:
        if (
            snapshot.source is not CandidateSource.OWN_ACCOUNT
            or snapshot.provenance
            not in {
                ObservationProvenance.LOCAL_OPERATOR_PACKET,
                ObservationProvenance.SYNTHETIC_FIXTURE,
            }
            or any(
                value is not None
                for value in (
                    snapshot.thread_id,
                    snapshot.reply_id,
                    snapshot.root_id,
                    snapshot.parent_id,
                    snapshot.reply_text,
                    snapshot.root_text,
                    snapshot.parent_text,
                )
            )
        ):
            return None, None, PrivateReviewReason.TARGET_MISMATCH
        texts: tuple[str | None, str | None, str | None] = (None, None, None)
    elif snapshot.action is ActionKind.REPLY:
        if (
            snapshot.source is not CandidateSource.INBOUND_REPLY
            or snapshot.provenance
            not in {ObservationProvenance.OFFICIAL_API, ObservationProvenance.SYNTHETIC_FIXTURE}
            or not _valid_remote_id(snapshot.thread_id)
            or not _valid_remote_id(snapshot.reply_id)
            or not _valid_remote_id(snapshot.root_id)
            or not _valid_remote_id(snapshot.parent_id)
            or snapshot.thread_id != snapshot.root_id
            or snapshot.reply_id == snapshot.root_id
        ):
            return None, None, PrivateReviewReason.TARGET_MISMATCH
        visible: list[str] = []
        normalized_values: list[str | None] = []
        for value in (snapshot.reply_text, snapshot.root_text, snapshot.parent_text):
            normalized, reason = _normalize_context_text(value, required=True)
            if reason is not None or normalized is None:
                return None, None, reason or PrivateReviewReason.CONTEXT_INCOMPLETE
            normalized_values.append(normalized)
            visible.append(normalized)
        reply_text, root_text, parent_text = normalized_values
        assert reply_text is not None and root_text is not None and parent_text is not None
        if snapshot.parent_id == snapshot.root_id and parent_text != root_text:
            return None, None, PrivateReviewReason.CONTEXT_MISMATCH
        if sum(len(text.encode("utf-8")) for text in visible) > _MAX_TOTAL_CONTEXT_BYTES:
            return None, None, PrivateReviewReason.CONTEXT_TOO_LONG
        texts = (reply_text, root_text, parent_text)
    else:
        return None, None, PrivateReviewReason.UNSUPPORTED_ACTION

    reply_text, root_text, parent_text = texts
    payload: dict[str, object] = {
        "version": "api-auto-review-display-context-v1",
        "account_id": str(snapshot.account_id),
        "actor_id": snapshot.actor_id,
        "action": snapshot.action.value,
        "source": snapshot.source.value,
        "provenance": snapshot.provenance.value,
        "candidate_fingerprint": snapshot.candidate_fingerprint,
        "thread_id": snapshot.thread_id,
        "reply_id": snapshot.reply_id,
        "root_id": snapshot.root_id,
        "parent_id": snapshot.parent_id,
        "reply_text": reply_text,
        "root_text": root_text,
        "parent_text": parent_text,
    }
    return payload, texts, None


def _normalize_context_text(
    value: object,
    *,
    required: bool,
) -> tuple[str | None, PrivateReviewReason | None]:
    if value is None:
        if required:
            return None, PrivateReviewReason.CONTEXT_INCOMPLETE
        return None, None
    if type(value) is not str:
        return None, PrivateReviewReason.CONTEXT_INVALID
    try:
        value.encode("utf-8", errors="strict")
        normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeError, ValueError:
        return None, PrivateReviewReason.CONTEXT_INVALID
    if any(
        (unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} and character != "\n")
        for character in normalized
    ):
        return None, PrivateReviewReason.CONTEXT_INVALID
    if len(normalized) > _MAX_CONTEXT_CODEPOINTS or len(encoded) > _MAX_CONTEXT_TEXT_BYTES:
        return None, PrivateReviewReason.CONTEXT_TOO_LONG
    if required and not normalized.strip():
        return None, PrivateReviewReason.CONTEXT_INCOMPLETE
    return normalized, None


def _preview_digest(
    packet: ReviewContentPacket,
    display: ReviewDisplaySnapshot,
    display_digest: str,
    expires_at: datetime,
) -> str:
    binding = packet.binding
    payload: dict[str, object] = {
        "version": "api-auto-private-review-preview-v1",
        "packet_revision_sha256": packet.packet_revision_sha256,
        "display_context_sha256": display_digest,
        "account_id": str(binding.account_id),
        "actor_id": display.actor_id,
        "action": binding.action.value,
        "source": binding.source.value,
        "provenance": binding.provenance.value,
        "candidate_fingerprint": binding.candidate_fingerprint,
        "target_sha256": binding.target_sha256,
        "context_sha256": binding.context_sha256,
        "context_evidence_sha256": binding.context_evidence_sha256,
        "root_sha256": binding.root_sha256,
        "parent_sha256": binding.parent_sha256,
        "reply_sha256": binding.reply_sha256,
        "packet_expires_at": binding.expires_at.isoformat(),
        "content_expires_at": packet.prepared_content.expires_at.isoformat(),
        "effective_expires_at": expires_at.isoformat(),
    }
    return _domain_digest(_PREVIEW_DOMAIN, payload)


def _domain_digest(domain: bytes, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(domain + encoded).hexdigest()


def _valid_remote_id(value: object) -> bool:
    return type(value) is str and _REMOTE_ID.fullmatch(value) is not None


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)
    )


def _result(
    decision: PrivateReviewDecision,
    reason: PrivateReviewReason,
) -> PrivateReviewPreviewResult:
    return PrivateReviewPreviewResult(decision, (reason,), None)
