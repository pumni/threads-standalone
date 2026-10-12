"""Pure, in-memory content packet contracts for API-AUTO-02A.

This slice turns operator-prepared text into an immutable review packet. It
does not grant approval, persist content, call a provider, or perform effects.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    ActionProposal,
    ApiPermission,
    CampaignScope,
    CandidateSource,
    CandidateState,
    CapabilitySnapshot,
    ObservationProvenance,
    PlanDecision,
    QuotaSnapshot,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NORMALIZATION_VERSION = "nfc-lf-v1"
_MAX_CONTENT_CODEPOINTS = 500
_MAX_PACKET_WINDOW = timedelta(hours=24)


class ContentAction(StrEnum):
    OWN_POST = "OWN_POST"
    EXACT_TARGET_REPLY = "EXACT_TARGET_REPLY"


class ContentOrigin(StrEnum):
    OPERATOR_AUTHORED = "OPERATOR_AUTHORED"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    AI_PROVIDER = "AI_PROVIDER"
    EXTERNAL_SOURCE = "EXTERNAL_SOURCE"
    UNKNOWN = "UNKNOWN"


class RightsBasis(StrEnum):
    OPERATOR_CREATED = "OPERATOR_CREATED"
    LICENSED = "LICENSED"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    OTHER = "OTHER"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    UNKNOWN = "UNKNOWN"


class JobFactsDeclaration(StrEnum):
    OPERATOR_VERIFIED = "OPERATOR_VERIFIED"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    UNKNOWN = "UNKNOWN"


class PacketReviewState(StrEnum):
    NEEDS_OPERATOR_REVIEW = "NEEDS_OPERATOR_REVIEW"
    REJECTED = "REJECTED"


class ContentDecision(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    STOP = "STOP"
    UNAVAILABLE = "UNAVAILABLE"


class ContentReason(StrEnum):
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    AMBIGUOUS_PRIOR_ACTION = "AMBIGUOUS_PRIOR_ACTION"
    API_PERMISSION_MISSING = "API_PERMISSION_MISSING"
    CONTENT_BLANK = "CONTENT_BLANK"
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    CONTENT_TOO_LONG = "CONTENT_TOO_LONG"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    COOLDOWN = "COOLDOWN"
    DUPLICATE_PACKET = "DUPLICATE_PACKET"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    INELIGIBLE_CANDIDATE = "INELIGIBLE_CANDIDATE"
    INVALID_CONTENT = "INVALID_CONTENT"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    INVALID_SCOPE = "INVALID_SCOPE"
    JOB_FACTS_MISSING = "JOB_FACTS_MISSING"
    PACKET_REJECTED = "PACKET_REJECTED"
    PACKET_REVISION_MISMATCH = "PACKET_REVISION_MISMATCH"
    POLICY_MISMATCH = "POLICY_MISMATCH"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
    REVIEW_STATE_INVALID = "REVIEW_STATE_INVALID"
    RIGHTS_NOT_DECLARED = "RIGHTS_NOT_DECLARED"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    TARGET_ACCOUNT_MISMATCH = "TARGET_ACCOUNT_MISMATCH"
    TEXT_INVALID_UNICODE = "TEXT_INVALID_UNICODE"
    TEXT_NOT_NORMALIZED = "TEXT_NOT_NORMALIZED"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    WINDOW_INVALID = "WINDOW_INVALID"
    WINDOW_NOT_ACTIVE = "WINDOW_NOT_ACTIVE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"


@dataclass(frozen=True, slots=True, repr=False)
class RightsEvidence:
    """Operator declaration for attribution, usage rights, and job facts."""

    source_attribution: str | None
    rights_basis: RightsBasis
    right_to_use_declared: bool
    job_facts_sha256: str | None
    job_facts_declaration: JobFactsDeclaration

    def __repr__(self) -> str:
        return (
            "RightsEvidence("
            f"rights_basis={self.rights_basis.value if type(self.rights_basis) is RightsBasis else 'INVALID'}, "
            f"right_to_use_declared={self.right_to_use_declared}, "
            f"job_facts_declared={self.job_facts_declaration is not JobFactsDeclaration.UNKNOWN})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PreparedContent:
    """In-memory text and declarations supplied by an operator or fixture."""

    text: str
    origin: ContentOrigin
    rights: RightsEvidence | None
    prepared_at: datetime
    effective_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "PreparedContent("
            f"origin={self.origin.value if type(self.origin) is ContentOrigin else 'INVALID'}, "
            f"text_present={bool(self.text) if type(self.text) is str else False}, "
            "private_content=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProposalBinding:
    """Private equality binding to one review-only API-AUTO-01 proposal."""

    account_id: UUID
    action: ActionKind
    source: CandidateSource
    provenance: ObservationProvenance
    candidate_fingerprint: str
    target_sha256: str
    context_sha256: str
    content_sha256: str
    packet_revision_sha256: str
    required_permissions: tuple[ApiPermission, ...]
    decision: PlanDecision
    dispatchable: bool

    def __repr__(self) -> str:
        return (
            "ProposalBinding("
            f"action={self.action.value if type(self.action) is ActionKind else 'INVALID'}, "
            f"source={self.source.value if type(self.source) is CandidateSource else 'INVALID'}, "
            f"permission_count={len(self.required_permissions)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ContentPacketDraft:
    """Immutable, review-required packet linked to the planner proposal."""

    proposal_binding: ProposalBinding = field(repr=False)
    account_id: UUID = field(repr=False)
    action: ContentAction
    candidate_fingerprint: str = field(repr=False)
    target_sha256: str = field(repr=False)
    context_sha256: str = field(repr=False)
    policy_revision_sha256: str = field(repr=False)
    content_sha256: str = field(repr=False)
    packet_revision_sha256: str = field(repr=False)
    prepared_content: PreparedContent = field(repr=False)
    normalization_version: str
    review_state: PacketReviewState = PacketReviewState.NEEDS_OPERATOR_REVIEW

    def __repr__(self) -> str:
        return (
            "ContentPacketDraft("
            f"action={self.action.value if type(self.action) is ContentAction else 'INVALID'}, "
            f"review_state={self.review_state.value if type(self.review_state) is PacketReviewState else 'INVALID'}, "
            "private_content=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalInput:
    """Inert field projection for a future separately approved #74B store."""

    account_id: UUID = field(repr=False)
    candidate_fingerprint: str = field(repr=False)
    action: ActionKind
    target_sha256: str = field(repr=False)
    context_sha256: str = field(repr=False)
    content_sha256: str = field(repr=False)
    packet_revision_sha256: str = field(repr=False)
    policy_sha256: str = field(repr=False)
    expires_at: datetime
    review_state: PacketReviewState

    def __repr__(self) -> str:
        return (
            "ApprovalInput("
            f"action={self.action.value if type(self.action) is ActionKind else 'INVALID'}, "
            f"review_state={self.review_state.value if type(self.review_state) is PacketReviewState else 'INVALID'}, "
            "private_binding=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ContentEvaluation:
    decision: ContentDecision
    reason_codes: tuple[ContentReason, ...]
    packet: ContentPacketDraft | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "ContentEvaluation("
            f"decision={self.decision.value}, "
            f"reason_codes={tuple(reason.value for reason in self.reason_codes)!r}, "
            f"packet_present={self.packet is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DraftRequest:
    """Future provider request containing digests, not raw target IDs."""

    action: ContentAction
    account_id: UUID = field(repr=False)
    candidate_fingerprint: str = field(repr=False)
    target_sha256: str = field(repr=False)
    context_sha256: str = field(repr=False)
    policy_revision_sha256: str = field(repr=False)
    job_facts_sha256: str = field(repr=False)
    max_codepoints: int = _MAX_CONTENT_CODEPOINTS

    def __repr__(self) -> str:
        return (
            "DraftRequest("
            f"action={self.action.value if type(self.action) is ContentAction else 'INVALID'}, "
            f"max_codepoints={self.max_codepoints}, private_binding=<redacted>)"
        )


class DraftProvider(Protocol):
    """Future draft-only source seam; this slice never invokes it."""

    def draft(self, request: DraftRequest) -> PreparedContent | None: ...


def content_sha256_for(text: str) -> str:
    """Return the SHA-256 for canonical text, without storing or approving it."""

    normalized, reason = _normalize_text(text)
    if reason is not None:
        raise ValueError("invalid content text")
    return _sha256_text(normalized)


def packet_revision_sha256_for(
    proposal: ActionProposal,
    prepared_content: PreparedContent,
    *,
    policy_revision_sha256: str,
) -> str:
    """Compute the packet revision digest; this is equality evidence, not approval."""

    if (
        type(proposal) is not ActionProposal
        or type(prepared_content) is not PreparedContent
        or _proposal_failure(proposal) is not None
    ):
        raise ValueError("invalid content packet inputs")
    if not _is_sha256(policy_revision_sha256):
        raise ValueError("invalid content packet inputs")
    normalized, reason = _normalize_text(prepared_content.text)
    if reason is not None or _rights_gate(prepared_content, proposal) is not None:
        raise ValueError("invalid content packet inputs")
    if _sha256_text(normalized) != proposal.content_sha256 or not all(
        _is_utc(value)
        for value in (
            prepared_content.prepared_at,
            prepared_content.effective_at,
            prepared_content.expires_at,
        )
    ):
        raise ValueError("invalid content packet inputs")
    binding = _proposal_binding(proposal)
    return _packet_revision_digest(
        binding,
        prepared_content,
        content_sha256=_sha256_text(normalized),
        policy_revision_sha256=policy_revision_sha256,
    )


def prepare_packet(
    proposal: ActionProposal,
    prepared_content: PreparedContent | None,
    scope: CampaignScope,
    candidate_state: CandidateState,
    *,
    now: datetime,
    known_packet_revisions: frozenset[str] = frozenset(),
) -> ContentEvaluation:
    """Bind operator-prepared content to one current review proposal."""

    if prepared_content is None:
        return _result(ContentDecision.UNAVAILABLE, ContentReason.PROVIDER_UNAVAILABLE)
    invalid_seen = _known_revisions_failure(known_packet_revisions)
    if invalid_seen is not None:
        return _result(ContentDecision.STOP, invalid_seen)
    proposal_failure = _proposal_failure(proposal)
    if proposal_failure is not None:
        return _result(ContentDecision.STOP, proposal_failure)
    scope_result = _scope_gate(scope, proposal, now)
    if scope_result is not None:
        return scope_result
    state_result = _candidate_state_gate(candidate_state)
    if state_result is not None:
        return state_result
    content_result = _prepared_content_gate(prepared_content, proposal, now)
    if content_result is not None:
        return content_result

    normalized, text_reason = _normalize_text(prepared_content.text)
    if text_reason is not None:
        return _result(ContentDecision.STOP, text_reason)
    content_sha256 = _sha256_text(normalized)
    if content_sha256 != proposal.content_sha256:
        return _result(ContentDecision.STOP, ContentReason.CONTENT_HASH_MISMATCH)
    normalized_content = replace(prepared_content, text=normalized)
    revision_sha256 = _packet_revision_digest(
        _proposal_binding(proposal),
        normalized_content,
        content_sha256=content_sha256,
        policy_revision_sha256=scope.policy_sha256,
    )
    if proposal.packet_revision_sha256 != revision_sha256:
        return _result(ContentDecision.STOP, ContentReason.PACKET_REVISION_MISMATCH)
    if revision_sha256 in known_packet_revisions:
        return _result(ContentDecision.STOP, ContentReason.DUPLICATE_PACKET)

    content_action = _content_action(proposal.action)
    if content_action is None:
        return _result(ContentDecision.STOP, ContentReason.ACTION_NOT_ALLOWED)
    packet = ContentPacketDraft(
        proposal_binding=_proposal_binding(proposal),
        account_id=proposal.account_id,
        action=content_action,
        candidate_fingerprint=proposal.candidate_fingerprint,
        target_sha256=proposal.target_sha256,
        context_sha256=proposal.context_sha256,
        policy_revision_sha256=scope.policy_sha256,
        content_sha256=content_sha256,
        packet_revision_sha256=revision_sha256,
        prepared_content=normalized_content,
        normalization_version=_NORMALIZATION_VERSION,
    )
    return evaluate_review_eligibility(
        packet,
        proposal,
        scope,
        candidate_state,
        now=now,
        known_packet_revisions=known_packet_revisions,
    )


def evaluate_review_eligibility(
    packet: ContentPacketDraft,
    current_proposal: ActionProposal,
    scope: CampaignScope,
    candidate_state: CandidateState,
    *,
    now: datetime,
    known_packet_revisions: frozenset[str] = frozenset(),
) -> ContentEvaluation:
    """Revalidate a draft for operator review; this function cannot approve it."""

    invalid_seen = _known_revisions_failure(known_packet_revisions)
    if invalid_seen is not None:
        return _result(ContentDecision.STOP, invalid_seen)
    if type(packet) is not ContentPacketDraft:
        return _result(ContentDecision.STOP, ContentReason.INVALID_INPUT)
    if type(packet.review_state) is not PacketReviewState:
        return _result(ContentDecision.STOP, ContentReason.REVIEW_STATE_INVALID)
    if packet.review_state is PacketReviewState.REJECTED:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.PACKET_REJECTED)
    if packet.review_state is not PacketReviewState.NEEDS_OPERATOR_REVIEW:
        return _result(ContentDecision.STOP, ContentReason.REVIEW_STATE_INVALID)

    proposal_failure = _proposal_failure(current_proposal)
    if proposal_failure is not None:
        return _result(ContentDecision.STOP, proposal_failure)
    scope_result = _scope_gate(scope, current_proposal, now)
    if scope_result is not None:
        return scope_result
    state_result = _candidate_state_gate(candidate_state)
    if state_result is not None:
        return state_result

    if type(packet.proposal_binding) is not ProposalBinding:
        return _result(ContentDecision.STOP, ContentReason.INVALID_INPUT)
    if packet.account_id != current_proposal.account_id or packet.account_id != scope.account_id:
        return _result(ContentDecision.STOP, ContentReason.TARGET_ACCOUNT_MISMATCH)
    if (
        type(packet.action) is not ContentAction
        or packet.action is not _content_action(current_proposal.action)
        or packet.candidate_fingerprint != current_proposal.candidate_fingerprint
    ):
        return _result(ContentDecision.STOP, ContentReason.INVALID_PROPOSAL)
    if packet.target_sha256 != current_proposal.target_sha256:
        return _result(ContentDecision.STOP, ContentReason.TARGET_MISMATCH)
    if packet.context_sha256 != current_proposal.context_sha256:
        return _result(ContentDecision.STOP, ContentReason.CONTEXT_MISMATCH)
    if packet.policy_revision_sha256 != scope.policy_sha256:
        return _result(ContentDecision.STOP, ContentReason.POLICY_MISMATCH)
    if packet.content_sha256 != current_proposal.content_sha256:
        return _result(ContentDecision.STOP, ContentReason.CONTENT_HASH_MISMATCH)
    if packet.proposal_binding != _proposal_binding(current_proposal):
        return _result(ContentDecision.STOP, ContentReason.INVALID_PROPOSAL)
    if packet.normalization_version != _NORMALIZATION_VERSION:
        return _result(ContentDecision.STOP, ContentReason.TEXT_NOT_NORMALIZED)

    content_result = _prepared_content_gate(packet.prepared_content, current_proposal, now)
    if content_result is not None:
        return content_result
    rights_result = _rights_gate(packet.prepared_content, current_proposal)
    if rights_result is not None:
        return rights_result
    normalized, text_reason = _normalize_text(packet.prepared_content.text)
    if text_reason is not None:
        return _result(ContentDecision.STOP, text_reason)
    if normalized != packet.prepared_content.text:
        return _result(ContentDecision.STOP, ContentReason.TEXT_NOT_NORMALIZED)
    content_sha256 = _sha256_text(normalized)
    if content_sha256 != packet.content_sha256 or content_sha256 != current_proposal.content_sha256:
        return _result(ContentDecision.STOP, ContentReason.CONTENT_HASH_MISMATCH)

    expected_revision = _packet_revision_digest(
        packet.proposal_binding,
        packet.prepared_content,
        content_sha256=content_sha256,
        policy_revision_sha256=packet.policy_revision_sha256,
    )
    if (
        packet.packet_revision_sha256 != expected_revision
        or current_proposal.packet_revision_sha256 != expected_revision
    ):
        return _result(ContentDecision.STOP, ContentReason.PACKET_REVISION_MISMATCH)
    if packet.packet_revision_sha256 in known_packet_revisions:
        return _result(ContentDecision.STOP, ContentReason.DUPLICATE_PACKET)
    if packet.prepared_content.expires_at > current_proposal.expires_at:
        return _result(ContentDecision.STOP, ContentReason.WINDOW_INVALID)
    if not _is_utc(packet.prepared_content.expires_at) or packet.prepared_content.expires_at <= now:
        return _result(ContentDecision.STOP, ContentReason.WINDOW_EXPIRED)

    return ContentEvaluation(ContentDecision.NEEDS_REVIEW, (), packet)


def approval_input_for_review(evaluation: ContentEvaluation) -> ApprovalInput | None:
    """Project inert approval fields for #74B without creating an approval DTO."""

    if (
        type(evaluation) is not ContentEvaluation
        or evaluation.decision is not ContentDecision.NEEDS_REVIEW
        or type(evaluation.packet) is not ContentPacketDraft
        or evaluation.packet.review_state is not PacketReviewState.NEEDS_OPERATOR_REVIEW
    ):
        return None
    packet = evaluation.packet
    binding = packet.proposal_binding
    if (
        type(packet.action) is not ContentAction
        or type(packet.account_id) is not UUID
        or type(binding) is not ProposalBinding
        or type(packet.prepared_content) is not PreparedContent
        or packet.normalization_version != _NORMALIZATION_VERSION
        or type(packet.review_state) is not PacketReviewState
        or not all(
            _is_sha256(value)
            for value in (
                packet.candidate_fingerprint,
                packet.target_sha256,
                packet.context_sha256,
                packet.policy_revision_sha256,
                packet.content_sha256,
                packet.packet_revision_sha256,
            )
        )
        or binding.account_id != packet.account_id
        or binding.action is not _planner_action(packet.action)
        or binding.candidate_fingerprint != packet.candidate_fingerprint
        or binding.target_sha256 != packet.target_sha256
        or binding.context_sha256 != packet.context_sha256
        or binding.content_sha256 != packet.content_sha256
        or binding.packet_revision_sha256 != packet.packet_revision_sha256
        or binding.decision is not PlanDecision.NEEDS_REVIEW
        or binding.dispatchable is not False
    ):
        return None
    normalized, reason = _normalize_text(packet.prepared_content.text)
    if (
        reason is not None
        or normalized != packet.prepared_content.text
        or _sha256_text(normalized) != packet.content_sha256
        or not _is_utc(packet.prepared_content.expires_at)
    ):
        return None
    return ApprovalInput(
        account_id=packet.account_id,
        candidate_fingerprint=packet.candidate_fingerprint,
        action=_planner_action(packet.action),
        target_sha256=packet.target_sha256,
        context_sha256=packet.context_sha256,
        content_sha256=packet.content_sha256,
        packet_revision_sha256=packet.packet_revision_sha256,
        policy_sha256=packet.policy_revision_sha256,
        expires_at=packet.prepared_content.expires_at,
        review_state=PacketReviewState.NEEDS_OPERATOR_REVIEW,
    )


def _proposal_failure(proposal: object) -> ContentReason | None:
    if type(proposal) is not ActionProposal:
        return ContentReason.INVALID_PROPOSAL
    if (
        type(proposal.account_id) is not UUID
        or type(proposal.action) is not ActionKind
        or type(proposal.source) is not CandidateSource
        or type(proposal.provenance) is not ObservationProvenance
        or type(proposal.decision) is not PlanDecision
        or proposal.decision is not PlanDecision.NEEDS_REVIEW
        or proposal.dispatchable is not False
        or not _is_sha256(proposal.candidate_fingerprint)
        or not _is_sha256(proposal.target_sha256)
        or not _is_sha256(proposal.context_sha256)
        or not _is_sha256(proposal.content_sha256)
        or not _is_sha256(proposal.packet_revision_sha256)
        or not _is_utc(proposal.expires_at)
    ):
        return ContentReason.INVALID_PROPOSAL
    if type(proposal.required_permissions) is not tuple or any(
        type(permission) is not ApiPermission for permission in proposal.required_permissions
    ):
        return ContentReason.INVALID_PROPOSAL
    if _content_action(proposal.action) is None:
        return ContentReason.ACTION_NOT_ALLOWED
    if proposal.action is ActionKind.POST_TEXT and (
        proposal.source is not CandidateSource.OWN_ACCOUNT
        or proposal.provenance
        not in {
            ObservationProvenance.LOCAL_OPERATOR_PACKET,
            ObservationProvenance.SYNTHETIC_FIXTURE,
        }
    ):
        return ContentReason.ACTION_NOT_ALLOWED
    if proposal.action is ActionKind.REPLY and (
        proposal.source is not CandidateSource.INBOUND_REPLY
        or proposal.provenance
        not in {ObservationProvenance.OFFICIAL_API, ObservationProvenance.SYNTHETIC_FIXTURE}
    ):
        return ContentReason.ACTION_NOT_ALLOWED
    return None


def _scope_gate(scope: object, proposal: ActionProposal, now: datetime) -> ContentEvaluation | None:
    if (
        type(now) is not datetime
        or not _is_utc(now)
        or type(scope) is not CampaignScope
        or type(scope.account_id) is not UUID
        or not _is_sha256(scope.policy_sha256)
        or type(scope.capabilities) is not CapabilitySnapshot
        or type(scope.quota) is not QuotaSnapshot
        or type(scope.allowed_sources) is not frozenset
        or type(scope.allowed_actions) is not frozenset
        or any(type(source) is not CandidateSource for source in scope.allowed_sources)
        or any(type(action) is not ActionKind for action in scope.allowed_actions)
        or type(scope.max_context_age) is not timedelta
        or scope.max_context_age <= timedelta(0)
        or type(scope.actor_id) is not str
        or not scope.actor_id.strip()
        or type(scope.enabled) is not bool
        or type(scope.policy_approved) is not bool
        or scope.capabilities.verified is not True
        or scope.capabilities.account_id != scope.account_id
        or type(scope.capabilities.actor_id) is not str
        or scope.capabilities.actor_id != scope.actor_id
        or type(scope.capabilities.permissions) is not frozenset
        or any(
            type(permission) is not ApiPermission for permission in scope.capabilities.permissions
        )
        or not _is_utc(scope.capabilities.observed_at)
        or not _is_utc(scope.quota.observed_at)
        or scope.quota.verified is not True
    ):
        return _result(ContentDecision.STOP, ContentReason.INVALID_SCOPE)
    if proposal.account_id != scope.account_id:
        return _result(ContentDecision.STOP, ContentReason.TARGET_ACCOUNT_MISMATCH)
    if not scope.enabled:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.ACTION_NOT_ALLOWED)
    if scope.policy_approved is not True:
        return _result(ContentDecision.STOP, ContentReason.POLICY_MISMATCH)
    if proposal.action not in scope.allowed_actions or proposal.source not in scope.allowed_sources:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.ACTION_NOT_ALLOWED)
    if not _fresh(scope.capabilities.observed_at, now, scope.max_context_age):
        return _result(ContentDecision.STOP, ContentReason.EVIDENCE_STALE)
    if not _fresh(scope.quota.observed_at, now, scope.max_context_age):
        return _result(ContentDecision.STOP, ContentReason.QUOTA_UNAVAILABLE)

    required = _required_permissions(proposal.action)
    if not set(required).issubset(proposal.required_permissions):
        return _result(ContentDecision.STOP, ContentReason.API_PERMISSION_MISSING)
    if not {*required, *proposal.required_permissions}.issubset(scope.capabilities.permissions):
        return _result(ContentDecision.STOP, ContentReason.API_PERMISSION_MISSING)

    usage, total = _quota_pair(proposal.action, scope.quota)
    if type(usage) is not int or type(total) is not int or usage < 0 or total <= 0 or usage > total:
        return _result(ContentDecision.STOP, ContentReason.QUOTA_UNAVAILABLE)
    if usage >= total:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.QUOTA_EXHAUSTED)
    if proposal.expires_at <= now:
        return _result(ContentDecision.STOP, ContentReason.WINDOW_EXPIRED)
    return None


def _candidate_state_gate(state: object) -> ContentEvaluation | None:
    if type(state) is not CandidateState:
        return _result(ContentDecision.STOP, ContentReason.INVALID_INPUT)
    if state is CandidateState.AMBIGUOUS:
        return _result(ContentDecision.STOP, ContentReason.AMBIGUOUS_PRIOR_ACTION)
    if state is CandidateState.COOLDOWN:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.COOLDOWN)
    if state is not CandidateState.ELIGIBLE:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.INELIGIBLE_CANDIDATE)
    return None


def _prepared_content_gate(
    prepared: object, proposal: ActionProposal, now: datetime
) -> ContentEvaluation | None:
    if type(prepared) is not PreparedContent:
        return _result(ContentDecision.STOP, ContentReason.INVALID_CONTENT)
    if type(prepared.origin) is not ContentOrigin:
        return _result(ContentDecision.STOP, ContentReason.INVALID_CONTENT)
    if prepared.origin in {ContentOrigin.AI_PROVIDER, ContentOrigin.EXTERNAL_SOURCE}:
        return _result(ContentDecision.UNAVAILABLE, ContentReason.PROVIDER_UNAVAILABLE)
    if prepared.origin is ContentOrigin.UNKNOWN:
        return _result(ContentDecision.STOP, ContentReason.INVALID_CONTENT)
    if type(prepared.rights) is not RightsEvidence:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.RIGHTS_NOT_DECLARED)
    rights_result = _rights_gate(prepared, proposal)
    if rights_result is not None:
        return rights_result
    if not all(
        _is_utc(value)
        for value in (prepared.prepared_at, prepared.effective_at, prepared.expires_at)
    ):
        return _result(ContentDecision.STOP, ContentReason.WINDOW_INVALID)
    if prepared.prepared_at > now:
        return _result(ContentDecision.STOP, ContentReason.WINDOW_INVALID)
    if now - prepared.prepared_at > _MAX_PACKET_WINDOW:
        return _result(ContentDecision.STOP, ContentReason.EVIDENCE_STALE)
    if (
        prepared.effective_at < prepared.prepared_at
        or prepared.expires_at <= prepared.effective_at
        or prepared.expires_at - prepared.prepared_at > _MAX_PACKET_WINDOW
        or prepared.expires_at > proposal.expires_at
    ):
        return _result(ContentDecision.STOP, ContentReason.WINDOW_INVALID)
    if prepared.effective_at > now:
        return _result(ContentDecision.STOP, ContentReason.WINDOW_NOT_ACTIVE)
    if prepared.expires_at <= now:
        return _result(ContentDecision.STOP, ContentReason.WINDOW_EXPIRED)
    _, text_reason = _normalize_text(prepared.text)
    if text_reason is not None:
        return _result(ContentDecision.STOP, text_reason)
    return None


def _rights_gate(prepared: PreparedContent, proposal: ActionProposal) -> ContentEvaluation | None:
    rights = prepared.rights
    if type(rights) is not RightsEvidence:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.RIGHTS_NOT_DECLARED)
    if (
        type(rights.rights_basis) is not RightsBasis
        or type(rights.job_facts_declaration) is not JobFactsDeclaration
    ):
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.RIGHTS_NOT_DECLARED)
    if (
        rights.right_to_use_declared is not True
        or rights.rights_basis is RightsBasis.UNKNOWN
        or type(rights.source_attribution) is not str
        or not rights.source_attribution.strip()
        or len(rights.source_attribution) > 256
    ):
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.RIGHTS_NOT_DECLARED)
    if not _is_sha256(rights.job_facts_sha256):
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.JOB_FACTS_MISSING)
    try:
        rights.source_attribution.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.RIGHTS_NOT_DECLARED)
    if prepared.origin is ContentOrigin.OPERATOR_AUTHORED:
        if (
            rights.rights_basis is RightsBasis.SYNTHETIC_FIXTURE
            or rights.job_facts_declaration is not JobFactsDeclaration.OPERATOR_VERIFIED
            or proposal.provenance is ObservationProvenance.SYNTHETIC_FIXTURE
        ):
            return _result(ContentDecision.STOP, ContentReason.PROVENANCE_MISMATCH)
    elif prepared.origin is ContentOrigin.SYNTHETIC_FIXTURE:
        if (
            rights.rights_basis is not RightsBasis.SYNTHETIC_FIXTURE
            or rights.job_facts_declaration is not JobFactsDeclaration.SYNTHETIC_FIXTURE
            or proposal.provenance is not ObservationProvenance.SYNTHETIC_FIXTURE
        ):
            return _result(ContentDecision.STOP, ContentReason.PROVENANCE_MISMATCH)
    else:
        return _result(ContentDecision.UNAVAILABLE, ContentReason.PROVIDER_UNAVAILABLE)
    if _has_forbidden_control(rights.source_attribution):
        return _result(ContentDecision.NOT_ELIGIBLE, ContentReason.RIGHTS_NOT_DECLARED)
    return None


def _known_revisions_failure(revisions: object) -> ContentReason | None:
    if type(revisions) is not frozenset:
        return ContentReason.INVALID_INPUT
    known_revisions = cast(frozenset[object], revisions)
    if any(not _is_sha256(item) for item in known_revisions):
        return ContentReason.INVALID_INPUT
    return None


def _proposal_binding(proposal: ActionProposal) -> ProposalBinding:
    return ProposalBinding(
        account_id=proposal.account_id,
        action=proposal.action,
        source=proposal.source,
        provenance=proposal.provenance,
        candidate_fingerprint=proposal.candidate_fingerprint,
        target_sha256=proposal.target_sha256,
        context_sha256=proposal.context_sha256,
        content_sha256=proposal.content_sha256,
        packet_revision_sha256=proposal.packet_revision_sha256,
        required_permissions=proposal.required_permissions,
        decision=proposal.decision,
        dispatchable=proposal.dispatchable,
    )


def _packet_revision_digest(
    binding: ProposalBinding,
    prepared: PreparedContent,
    *,
    content_sha256: str,
    policy_revision_sha256: str,
) -> str:
    rights = prepared.rights
    assert type(rights) is RightsEvidence
    payload = {
        "account": str(binding.account_id),
        "action": binding.action.value,
        "source": binding.source.value,
        "provenance": binding.provenance.value,
        "candidate_fingerprint": binding.candidate_fingerprint,
        "target_sha256": binding.target_sha256,
        "context_sha256": binding.context_sha256,
        "content_sha256": content_sha256,
        "policy_revision_sha256": policy_revision_sha256,
        "origin": prepared.origin.value,
        "source_attribution_sha256": _sha256_text(rights.source_attribution or ""),
        "rights_basis": rights.rights_basis.value,
        "right_to_use_declared": rights.right_to_use_declared,
        "job_facts_sha256": rights.job_facts_sha256,
        "job_facts_declaration": rights.job_facts_declaration.value,
        "normalization_version": _NORMALIZATION_VERSION,
        "prepared_at": prepared.prepared_at.isoformat(),
        "effective_at": prepared.effective_at.isoformat(),
        "expires_at": prepared.expires_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalize_text(text: object) -> tuple[str, ContentReason | None]:
    if type(text) is not str:
        return "", ContentReason.INVALID_CONTENT
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "", ContentReason.TEXT_INVALID_UNICODE
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "", ContentReason.TEXT_INVALID_UNICODE
    if not normalized.strip():
        return "", ContentReason.CONTENT_BLANK
    if len(normalized) > _MAX_CONTENT_CODEPOINTS:
        return "", ContentReason.CONTENT_TOO_LONG
    if _has_forbidden_control(normalized, allow_newline=True):
        return "", ContentReason.INVALID_CONTENT
    return normalized, None


def _has_forbidden_control(value: str, *, allow_newline: bool = False) -> bool:
    return any(
        unicodedata.category(character) == "Cc" and not (allow_newline and character == "\n")
        for character in value
    )


def _content_action(action: ActionKind) -> ContentAction | None:
    if action is ActionKind.POST_TEXT:
        return ContentAction.OWN_POST
    if action is ActionKind.REPLY:
        return ContentAction.EXACT_TARGET_REPLY
    return None


def _planner_action(action: ContentAction) -> ActionKind:
    if action is ContentAction.OWN_POST:
        return ActionKind.POST_TEXT
    return ActionKind.REPLY


def _required_permissions(action: ActionKind) -> tuple[ApiPermission, ...]:
    if action is ActionKind.POST_TEXT:
        return (ApiPermission.THREADS_BASIC, ApiPermission.THREADS_CONTENT_PUBLISH)
    return (
        ApiPermission.THREADS_BASIC,
        ApiPermission.THREADS_READ_REPLIES,
        ApiPermission.THREADS_CONTENT_PUBLISH,
    )


def _quota_pair(action: ActionKind, quota: QuotaSnapshot) -> tuple[int | None, int | None]:
    if action is ActionKind.POST_TEXT:
        return quota.post_usage, quota.post_total
    return quota.reply_usage, quota.reply_total


def _fresh(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
    return timedelta(0) <= now - observed_at <= max_age


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)
    )


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result(
    decision: ContentDecision,
    reason: ContentReason,
) -> ContentEvaluation:
    return ContentEvaluation(decision, (reason,), None)
