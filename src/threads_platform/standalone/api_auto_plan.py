"""Pure, bounded planning for API-AUTO-01.

This module consumes already observed DTOs. It has no API, filesystem, clock,
credential, browser, or mutation dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from threads_platform.api.contracts import RemoteReply

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REMOTE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_MAX_CANDIDATES = 20
_MAX_ACTIONS_PER_CYCLE = 1


class CandidateSource(StrEnum):
    OWN_ACCOUNT = "OWN_ACCOUNT"
    INBOUND_REPLY = "INBOUND_REPLY"
    MENTIONS = "MENTIONS"
    KEYWORD = "KEYWORD"
    TAG = "TAG"
    WATCHED_PROFILE = "WATCHED_PROFILE"
    UNKNOWN = "UNKNOWN"


class ObservationProvenance(StrEnum):
    OFFICIAL_API = "OFFICIAL_API"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    LOCAL_OPERATOR_PACKET = "LOCAL_OPERATOR_PACKET"
    UNKNOWN = "UNKNOWN"


class ActionKind(StrEnum):
    POST_TEXT = "POST_TEXT"
    REPLY = "REPLY"


class TargetKind(StrEnum):
    OWN_ACCOUNT = "OWN_ACCOUNT"
    INBOUND_REPLY = "INBOUND_REPLY"


class ContextCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class CandidateState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    COOLDOWN = "COOLDOWN"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class PlanDecision(StrEnum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    STOP = "STOP"


class PlanReason(StrEnum):
    ACTION_CAP_REACHED = "ACTION_CAP_REACHED"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    AMBIGUOUS_PRIOR_ACTION = "AMBIGUOUS_PRIOR_ACTION"
    API_PERMISSION_MISSING = "API_PERMISSION_MISSING"
    APPROVAL_TARGET_MISMATCH = "APPROVAL_TARGET_MISMATCH"
    CANDIDATE_LIMIT_EXCEEDED = "CANDIDATE_LIMIT_EXCEEDED"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    CONTEXT_STALE = "CONTEXT_STALE"
    COOLDOWN = "COOLDOWN"
    DUPLICATE_FINGERPRINT = "DUPLICATE_FINGERPRINT"
    DRAFT_UNAVAILABLE = "DRAFT_UNAVAILABLE"
    INVALID_SCOPE = "INVALID_SCOPE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    POLICY_NOT_APPROVED = "POLICY_NOT_APPROVED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
    REPLY_TARGET_COLLISION = "REPLY_TARGET_COLLISION"
    TARGET_ACCOUNT_MISMATCH = "TARGET_ACCOUNT_MISMATCH"
    TARGET_ACTOR_MISMATCH = "TARGET_ACTOR_MISMATCH"
    TARGET_NOT_REACQUIRED = "TARGET_NOT_REACQUIRED"
    TARGET_SOURCE_MISMATCH = "TARGET_SOURCE_MISMATCH"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    TARGET_UNVERIFIED = "TARGET_UNVERIFIED"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    FINGERPRINT_BINDING_CONFLICT = "FINGERPRINT_BINDING_CONFLICT"


class ApiPermission(StrEnum):
    THREADS_BASIC = "threads_basic"
    THREADS_CONTENT_PUBLISH = "threads_content_publish"
    THREADS_KEYWORD_SEARCH = "threads_keyword_search"
    THREADS_MANAGE_MENTIONS = "threads_manage_mentions"
    THREADS_PROFILE_DISCOVERY = "threads_profile_discovery"
    THREADS_READ_REPLIES = "threads_read_replies"


@dataclass(frozen=True, slots=True, repr=False)
class CapabilitySnapshot:
    """Operator-verified account identity and permission evidence."""

    account_id: UUID = field(repr=False)
    actor_id: str = field(repr=False)
    permissions: frozenset[ApiPermission]
    verified: bool
    observed_at: datetime

    def __repr__(self) -> str:
        return (
            "CapabilitySnapshot("
            f"permission_count={len(self.permissions)}, verified={self.verified}, "
            f"observed_at={self.observed_at.isoformat()})"
        )


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """Bounded publishing quota values from an already completed API read."""

    post_usage: int | None
    post_total: int | None
    reply_usage: int | None
    reply_total: int | None
    verified: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class CampaignScope:
    """Immutable local policy and account scope for one planner call."""

    account_id: UUID = field(repr=False)
    actor_id: str = field(repr=False)
    campaign_id: str = field(repr=False)
    policy_sha256: str = field(repr=False)
    capabilities: CapabilitySnapshot
    quota: QuotaSnapshot
    allowed_sources: frozenset[CandidateSource]
    allowed_actions: frozenset[ActionKind]
    policy_approved: bool
    enabled: bool = True
    max_candidates: int = _MAX_CANDIDATES
    max_actions_per_cycle: int = _MAX_ACTIONS_PER_CYCLE
    max_context_age: timedelta = timedelta(minutes=15)

    def __repr__(self) -> str:
        return (
            "CampaignScope("
            f"source_count={len(self.allowed_sources)}, action_count={len(self.allowed_actions)}, "
            f"policy_approved={self.policy_approved}, enabled={self.enabled}, "
            f"max_candidates={self.max_candidates}, "
            f"max_actions_per_cycle={self.max_actions_per_cycle})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedTargetBinding:
    """Ephemeral exact target evidence; all account and remote IDs stay private."""

    account_id: UUID = field(repr=False)
    actor_id: str = field(repr=False)
    source: CandidateSource
    provenance: ObservationProvenance
    target_kind: TargetKind
    thread_id: str | None = field(repr=False)
    reply_id: str | None = field(repr=False)
    root_id: str | None = field(repr=False)
    parent_id: str | None = field(repr=False)
    reply_observation: RemoteReply | None = field(repr=False)
    observed_at: datetime
    completeness: ContextCompleteness
    context_evidence_sha256: str | None = field(repr=False)
    actor_verified: bool
    source_verified: bool
    relationship_verified: bool
    root_owned_by_account: bool
    reply_owned_by_operator: bool | None
    public_context: bool
    reply_control_allows_reply: bool

    def __repr__(self) -> str:
        return (
            "VerifiedTargetBinding("
            f"source={self.source.value}, provenance={self.provenance.value}, "
            f"target_kind={self.target_kind.value}, completeness={self.completeness.value}, "
            f"actor_verified={self.actor_verified}, source_verified={self.source_verified}, "
            f"relationship_verified={self.relationship_verified})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ObservedCandidate:
    """Nurture-compatible observation plus a separately reacquired exact binding."""

    account_id: UUID = field(repr=False)
    source: CandidateSource
    provenance: ObservationProvenance
    action: ActionKind
    fingerprint: str = field(repr=False)
    score: int
    source_order: int
    state: CandidateState
    observed_at: datetime
    binding: VerifiedTargetBinding | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "ObservedCandidate("
            f"source={self.source.value}, provenance={self.provenance.value}, "
            f"action={self.action.value}, score={self.score}, state={self.state.value}, "
            f"binding_present={self.binding is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedPacketSummary:
    """Minimized approved-content evidence supplied by a later content slice."""

    account_id: UUID = field(repr=False)
    candidate_fingerprint: str = field(repr=False)
    action: ActionKind
    target_sha256: str = field(repr=False)
    content_sha256: str = field(repr=False)
    packet_revision_sha256: str = field(repr=False)
    policy_sha256: str = field(repr=False)
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "ApprovedPacketSummary("
            f"action={self.action.value}, expires_at={self.expires_at.isoformat()})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ActionProposal:
    """A review-only proposal. It contains no remote IDs or content text."""

    account_id: UUID = field(repr=False)
    action: ActionKind
    source: CandidateSource
    provenance: ObservationProvenance
    candidate_fingerprint: str = field(repr=False)
    target_sha256: str = field(repr=False)
    context_sha256: str = field(repr=False)
    content_sha256: str = field(repr=False)
    packet_revision_sha256: str = field(repr=False)
    required_permissions: tuple[ApiPermission, ...]
    expires_at: datetime
    decision: PlanDecision = PlanDecision.NEEDS_REVIEW
    dispatchable: bool = False

    def __repr__(self) -> str:
        return (
            "ActionProposal("
            f"action={self.action.value}, source={self.source.value}, "
            f"provenance={self.provenance.value}, decision={self.decision.value}, "
            f"required_permissions={tuple(item.value for item in self.required_permissions)!r}, "
            f"dispatchable={self.dispatchable}, private_evidence=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PlanItem:
    source: CandidateSource
    decision: PlanDecision
    reason_codes: tuple[PlanReason, ...]
    score: int
    candidate_fingerprint: str = field(repr=False)
    proposal: ActionProposal | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "PlanItem("
            f"source={self.source.value}, decision={self.decision.value}, "
            f"reason_codes={tuple(item.value for item in self.reason_codes)!r}, "
            f"score={self.score}, proposal_present={self.proposal is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CyclePlan:
    decision: PlanDecision
    items: tuple[PlanItem, ...]
    reason_codes: tuple[PlanReason, ...] = ()

    def __repr__(self) -> str:
        return (
            "CyclePlan("
            f"decision={self.decision.value}, candidate_count={len(self.items)}, "
            f"proposal_count={sum(item.proposal is not None for item in self.items)}, "
            f"reason_codes={tuple(item.value for item in self.reason_codes)!r})"
        )


class ContentSource(Protocol):
    """Future interface only; API-AUTO-01 does not call or implement it."""

    def approved_summary(
        self, account_id: UUID, candidate_fingerprint: str
    ) -> ApprovedPacketSummary | None: ...


class ActionDispatcher(Protocol):
    """Future interface only; API-AUTO-01 has no dispatch implementation."""

    def dispatch(self, proposal: ActionProposal) -> None: ...


_SOURCE_PERMISSIONS: dict[CandidateSource, tuple[ApiPermission, ...]] = {
    CandidateSource.OWN_ACCOUNT: (ApiPermission.THREADS_BASIC,),
    CandidateSource.INBOUND_REPLY: (
        ApiPermission.THREADS_BASIC,
        ApiPermission.THREADS_READ_REPLIES,
    ),
    CandidateSource.MENTIONS: (
        ApiPermission.THREADS_BASIC,
        ApiPermission.THREADS_MANAGE_MENTIONS,
    ),
    CandidateSource.KEYWORD: (
        ApiPermission.THREADS_BASIC,
        ApiPermission.THREADS_KEYWORD_SEARCH,
    ),
    CandidateSource.TAG: (
        ApiPermission.THREADS_BASIC,
        ApiPermission.THREADS_KEYWORD_SEARCH,
    ),
    CandidateSource.WATCHED_PROFILE: (
        ApiPermission.THREADS_BASIC,
        ApiPermission.THREADS_PROFILE_DISCOVERY,
    ),
}
_SOURCE_PRIORITY = {
    CandidateSource.OWN_ACCOUNT: 0,
    CandidateSource.INBOUND_REPLY: 1,
    CandidateSource.MENTIONS: 2,
    CandidateSource.KEYWORD: 3,
    CandidateSource.TAG: 4,
    CandidateSource.WATCHED_PROFILE: 5,
    CandidateSource.UNKNOWN: 99,
}


def plan_cycle(
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    approved_packet_summary: ApprovedPacketSummary | None = None,
    *,
    now: datetime,
) -> CyclePlan:
    """Return deterministic review proposals from already observed evidence."""

    failure = _scope_failure(scope, candidates, now)
    if failure is not None:
        return CyclePlan(PlanDecision.STOP, (), (failure,))
    if (
        approved_packet_summary is not None
        and type(approved_packet_summary) is not ApprovedPacketSummary
    ):
        return CyclePlan(PlanDecision.STOP, (), (PlanReason.INVALID_SCOPE,))
    if not scope.enabled:
        return CyclePlan(PlanDecision.OBSERVE_ONLY, (), (PlanReason.NOT_ELIGIBLE,))
    if not candidates:
        return CyclePlan(PlanDecision.OBSERVE_ONLY, ())

    conflict = _binding_conflict(candidates)
    if conflict is not None:
        return CyclePlan(PlanDecision.STOP, (), (conflict,))
    if approved_packet_summary is not None and not any(
        candidate.fingerprint == approved_packet_summary.candidate_fingerprint
        for candidate in candidates
    ):
        return CyclePlan(PlanDecision.STOP, (), (PlanReason.TARGET_NOT_REACQUIRED,))

    ordered = tuple(
        candidate
        for _, candidate in sorted(
            enumerate(candidates),
            key=lambda pair: (
                0 if pair[1].source is CandidateSource.OWN_ACCOUNT else 1,
                -pair[1].score,
                _SOURCE_PRIORITY[pair[1].source],
                pair[1].source_order,
                pair[0],
            ),
        )
    )
    fingerprint_seen: set[str] = set()
    prepared: list[tuple[ObservedCandidate, PlanDecision, tuple[PlanReason, ...], str, str]] = []

    for candidate in ordered:
        if candidate.fingerprint in fingerprint_seen:
            prepared.append(
                (
                    candidate,
                    PlanDecision.NOT_ELIGIBLE,
                    (PlanReason.DUPLICATE_FINGERPRINT,),
                    "",
                    "",
                )
            )
            continue
        fingerprint_seen.add(candidate.fingerprint)
        check = _candidate_check(scope, candidate, now)
        if check is PlanDecision.NEEDS_CONTEXT:
            if (
                approved_packet_summary is not None
                and candidate.fingerprint == approved_packet_summary.candidate_fingerprint
            ):
                packet_failure = _packet_failure(
                    scope,
                    candidate,
                    approved_packet_summary,
                    _target_digest(scope, candidate),
                    now,
                )
                if packet_failure is not None:
                    return CyclePlan(PlanDecision.STOP, (), (packet_failure,))
            prepared.append(
                (
                    candidate,
                    PlanDecision.NEEDS_CONTEXT,
                    (PlanReason.CONTEXT_INCOMPLETE,),
                    "",
                    "",
                )
            )
            continue
        if isinstance(check, PlanReason):
            return CyclePlan(
                PlanDecision.STOP,
                tuple(
                    PlanItem(
                        source=item.source,
                        decision=PlanDecision.STOP,
                        reason_codes=(
                            check if item is candidate else PlanReason.TARGET_NOT_REACQUIRED,
                        ),
                        score=item.score,
                        candidate_fingerprint=item.fingerprint,
                        proposal=None,
                    )
                    for item in ordered
                ),
                (check,),
            )
        target_digest, context_digest = check
        state_result = _state_decision(scope, candidate)
        if state_result is not None:
            prepared.append(
                (candidate, state_result[0], state_result[1], target_digest, context_digest)
            )
            continue
        prepared.append((candidate, PlanDecision.NEEDS_REVIEW, (), target_digest, context_digest))

    stop_reasons = tuple(
        dict.fromkeys(
            reason
            for _, decision, reasons, _, _ in prepared
            if decision is PlanDecision.STOP
            for reason in reasons
        )
    )
    if stop_reasons:
        return CyclePlan(
            PlanDecision.STOP,
            tuple(
                PlanItem(
                    source=candidate.source,
                    decision=decision,
                    reason_codes=reasons,
                    score=candidate.score,
                    candidate_fingerprint=candidate.fingerprint,
                    proposal=None,
                )
                for candidate, decision, reasons, _, _ in prepared
            ),
            stop_reasons,
        )

    packet_index: ObservedCandidate | None = None
    if approved_packet_summary is not None:
        packet_index = next(
            candidate
            for candidate in ordered
            if candidate.fingerprint == approved_packet_summary.candidate_fingerprint
        )

    items: list[PlanItem] = []
    proposal_count = 0
    for candidate, provisional, reasons, target_digest, context_digest in prepared:
        if provisional is not PlanDecision.NEEDS_REVIEW:
            items.append(
                PlanItem(
                    source=candidate.source,
                    decision=provisional,
                    reason_codes=reasons,
                    score=candidate.score,
                    candidate_fingerprint=candidate.fingerprint,
                    proposal=None,
                )
            )
            continue

        packet = approved_packet_summary
        if packet is None or candidate is not packet_index:
            items.append(
                PlanItem(
                    source=candidate.source,
                    decision=PlanDecision.OBSERVE_ONLY,
                    reason_codes=(PlanReason.DRAFT_UNAVAILABLE,),
                    score=candidate.score,
                    candidate_fingerprint=candidate.fingerprint,
                    proposal=None,
                )
            )
            continue
        packet_failure = _packet_failure(scope, candidate, packet, target_digest, now)
        if packet_failure is not None:
            return CyclePlan(
                PlanDecision.STOP,
                tuple(
                    PlanItem(
                        source=item.source,
                        decision=PlanDecision.STOP,
                        reason_codes=(packet_failure,),
                        score=item.score,
                        candidate_fingerprint=item.fingerprint,
                        proposal=None,
                    )
                    for item in ordered
                ),
                (packet_failure,),
            )
        if proposal_count >= scope.max_actions_per_cycle:
            items.append(
                PlanItem(
                    source=candidate.source,
                    decision=PlanDecision.NOT_ELIGIBLE,
                    reason_codes=(PlanReason.ACTION_CAP_REACHED,),
                    score=candidate.score,
                    candidate_fingerprint=candidate.fingerprint,
                    proposal=None,
                )
            )
            continue

        proposal_count += 1
        required = _required_permissions(candidate)
        proposal = ActionProposal(
            account_id=scope.account_id,
            action=candidate.action,
            source=candidate.source,
            provenance=candidate.provenance,
            candidate_fingerprint=candidate.fingerprint,
            target_sha256=target_digest,
            context_sha256=context_digest,
            content_sha256=packet.content_sha256,
            packet_revision_sha256=packet.packet_revision_sha256,
            required_permissions=required,
            expires_at=packet.expires_at,
        )
        items.append(
            PlanItem(
                source=candidate.source,
                decision=PlanDecision.NEEDS_REVIEW,
                reason_codes=(),
                score=candidate.score,
                candidate_fingerprint=candidate.fingerprint,
                proposal=proposal,
            )
        )

    plan_items = tuple(items)
    return CyclePlan(_aggregate_decision(plan_items), plan_items)


def _scope_failure(
    scope: CampaignScope, candidates: tuple[ObservedCandidate, ...], now: datetime
) -> PlanReason | None:
    if (
        type(now) is not datetime
        or not _is_utc(now)
        or type(scope) is not CampaignScope
        or type(candidates) is not tuple
        or any(type(candidate) is not ObservedCandidate for candidate in candidates)
        or any(not _candidate_shape_valid(candidate) for candidate in candidates)
        or type(scope.account_id) is not UUID
        or not _valid_remote_id(scope.actor_id)
        or type(scope.campaign_id) is not str
        or not scope.campaign_id
        or not _is_sha256(scope.policy_sha256)
        or type(scope.capabilities) is not CapabilitySnapshot
        or type(scope.quota) is not QuotaSnapshot
        or type(scope.capabilities.permissions) is not frozenset
        or type(scope.capabilities.verified) is not bool
        or type(scope.capabilities.observed_at) is not datetime
        or type(scope.quota.verified) is not bool
        or type(scope.quota.observed_at) is not datetime
        or type(scope.allowed_sources) is not frozenset
        or any(type(item) is not CandidateSource for item in scope.allowed_sources)
        or type(scope.allowed_actions) is not frozenset
        or any(type(item) is not ActionKind for item in scope.allowed_actions)
        or type(scope.policy_approved) is not bool
        or type(scope.enabled) is not bool
        or type(scope.max_candidates) is not int
        or not 1 <= scope.max_candidates <= _MAX_CANDIDATES
        or type(scope.max_actions_per_cycle) is not int
        or not 0 <= scope.max_actions_per_cycle <= _MAX_ACTIONS_PER_CYCLE
        or type(scope.max_context_age) is not timedelta
        or scope.max_context_age <= timedelta(0)
        or scope.max_context_age > timedelta(days=1)
    ):
        return PlanReason.INVALID_SCOPE
    if len(candidates) > scope.max_candidates:
        return PlanReason.CANDIDATE_LIMIT_EXCEEDED
    if not scope.policy_approved:
        return PlanReason.POLICY_NOT_APPROVED
    capability = scope.capabilities
    if type(capability.account_id) is not UUID or capability.account_id != scope.account_id:
        return PlanReason.TARGET_ACCOUNT_MISMATCH
    if not _valid_remote_id(capability.actor_id) or capability.actor_id != scope.actor_id:
        return PlanReason.TARGET_ACTOR_MISMATCH
    if (
        capability.verified is not True
        or not _is_fresh(capability.observed_at, now, scope.max_context_age)
        or any(type(permission) is not ApiPermission for permission in capability.permissions)
    ):
        return PlanReason.TARGET_UNVERIFIED
    if ApiPermission.THREADS_BASIC not in capability.permissions:
        return PlanReason.API_PERMISSION_MISSING
    quota = scope.quota
    if (
        quota.verified is not True
        or not _valid_count(quota.post_usage)
        or not _valid_count(quota.post_total)
        or not _valid_count(quota.reply_usage)
        or not _valid_count(quota.reply_total)
        or not _is_fresh(quota.observed_at, now, scope.max_context_age)
        or (
            quota.post_usage is not None
            and quota.post_total is not None
            and quota.post_usage > quota.post_total
        )
        or (
            quota.reply_usage is not None
            and quota.reply_total is not None
            and quota.reply_usage > quota.reply_total
        )
    ):
        return PlanReason.QUOTA_UNAVAILABLE
    if (
        quota.post_usage is None
        or quota.post_total is None
        or quota.reply_usage is None
        or quota.reply_total is None
    ):
        return PlanReason.QUOTA_UNAVAILABLE
    return None


def _candidate_check(
    scope: CampaignScope, candidate: ObservedCandidate, now: datetime
) -> tuple[str, str] | PlanReason | PlanDecision:
    if type(candidate.account_id) is not UUID or candidate.account_id != scope.account_id:
        return PlanReason.TARGET_ACCOUNT_MISMATCH
    if (
        candidate.source is CandidateSource.UNKNOWN
        or candidate.source not in scope.allowed_sources
        or candidate.provenance is ObservationProvenance.UNKNOWN
        or candidate.action not in scope.allowed_actions
    ):
        return PlanReason.UNSUPPORTED_SOURCE
    if (
        not _is_sha256(candidate.fingerprint)
        or type(candidate.score) is not int
        or candidate.score < 0
        or type(candidate.source_order) is not int
        or candidate.source_order < 0
        or type(candidate.state) is not CandidateState
        or not _is_utc(candidate.observed_at)
    ):
        return PlanReason.TARGET_UNVERIFIED
    if not _is_fresh(candidate.observed_at, now, scope.max_context_age):
        return PlanReason.CONTEXT_STALE
    if candidate.state is CandidateState.AMBIGUOUS:
        return PlanReason.AMBIGUOUS_PRIOR_ACTION
    binding = candidate.binding
    if binding is None:
        return PlanReason.TARGET_NOT_REACQUIRED
    if binding.account_id != scope.account_id or binding.account_id != candidate.account_id:
        return PlanReason.TARGET_ACCOUNT_MISMATCH
    if binding.actor_id != scope.actor_id:
        return PlanReason.TARGET_ACTOR_MISMATCH
    if binding.source is not candidate.source or binding.provenance is not candidate.provenance:
        return PlanReason.TARGET_SOURCE_MISMATCH
    if (
        binding.actor_verified is not True
        or binding.source_verified is not True
        or not _is_utc(binding.observed_at)
        or not _valid_remote_id(binding.actor_id)
    ):
        return PlanReason.TARGET_UNVERIFIED
    if not _is_fresh(binding.observed_at, now, scope.max_context_age):
        return PlanReason.CONTEXT_STALE
    if binding.context_evidence_sha256 is not None and not _is_sha256(
        binding.context_evidence_sha256
    ):
        return PlanReason.TARGET_UNVERIFIED
    required_permissions = _required_permissions(candidate)
    if any(permission not in scope.capabilities.permissions for permission in required_permissions):
        return PlanReason.API_PERMISSION_MISSING
    if candidate.action is ActionKind.REPLY:
        if (
            candidate.source is not CandidateSource.INBOUND_REPLY
            or binding.target_kind is not TargetKind.INBOUND_REPLY
            or candidate.provenance
            not in {ObservationProvenance.OFFICIAL_API, ObservationProvenance.SYNTHETIC_FIXTURE}
            or binding.relationship_verified is not True
            or binding.root_owned_by_account is not True
            or binding.reply_owned_by_operator is not False
            or binding.public_context is not True
            or binding.reply_control_allows_reply is not True
            or not _valid_remote_id(binding.thread_id)
            or not _valid_remote_id(binding.root_id)
            or binding.thread_id != binding.root_id
            or not _valid_remote_id(binding.reply_id)
            or binding.reply_id == binding.root_id
            or not _valid_remote_id(binding.parent_id)
            or type(binding.reply_observation) is not RemoteReply
            or binding.reply_observation.reply_id != binding.reply_id
            or binding.reply_observation.root_post_id != binding.root_id
            or binding.reply_observation.replied_to_id != binding.parent_id
            or binding.reply_observation.is_reply_owned_by_me is not False
        ):
            return PlanReason.TARGET_NOT_REACQUIRED
    elif candidate.action is ActionKind.POST_TEXT:
        if (
            candidate.source is not CandidateSource.OWN_ACCOUNT
            or binding.target_kind is not TargetKind.OWN_ACCOUNT
            or candidate.provenance
            not in {
                ObservationProvenance.LOCAL_OPERATOR_PACKET,
                ObservationProvenance.SYNTHETIC_FIXTURE,
            }
            or binding.root_owned_by_account is not True
            or any(
                value is not None
                for value in (
                    binding.thread_id,
                    binding.reply_id,
                    binding.root_id,
                    binding.parent_id,
                )
            )
        ):
            return PlanReason.TARGET_NOT_REACQUIRED
    else:
        return PlanReason.UNSUPPORTED_SOURCE
    if (
        binding.completeness is not ContextCompleteness.COMPLETE
        or binding.context_evidence_sha256 is None
    ):
        return PlanDecision.NEEDS_CONTEXT
    return _target_digest(scope, candidate), _context_digest(candidate)


def _state_decision(
    scope: CampaignScope, candidate: ObservedCandidate
) -> tuple[PlanDecision, tuple[PlanReason, ...]] | None:
    if candidate.binding is None:
        return PlanDecision.STOP, (PlanReason.TARGET_NOT_REACQUIRED,)
    if candidate.binding.completeness is not ContextCompleteness.COMPLETE:
        return PlanDecision.NEEDS_CONTEXT, (PlanReason.CONTEXT_INCOMPLETE,)
    if candidate.state is CandidateState.COOLDOWN:
        return PlanDecision.NOT_ELIGIBLE, (PlanReason.COOLDOWN,)
    if candidate.state in {CandidateState.PENDING, CandidateState.CONFIRMED}:
        return PlanDecision.NOT_ELIGIBLE, (PlanReason.NOT_ELIGIBLE,)
    if candidate.state is CandidateState.AMBIGUOUS:
        return PlanDecision.STOP, (PlanReason.AMBIGUOUS_PRIOR_ACTION,)
    if candidate.state is CandidateState.NOT_ELIGIBLE:
        return PlanDecision.NOT_ELIGIBLE, (PlanReason.NOT_ELIGIBLE,)
    usage, total = _quota_pair(candidate.action, scope.quota)
    if usage >= total:
        return PlanDecision.NOT_ELIGIBLE, (PlanReason.QUOTA_EXHAUSTED,)
    return None


def _quota_pair(action: ActionKind, quota: QuotaSnapshot) -> tuple[int, int]:
    if action is ActionKind.POST_TEXT:
        return quota.post_usage or 0, quota.post_total or 0
    return quota.reply_usage or 0, quota.reply_total or 0


def _packet_failure(
    scope: CampaignScope,
    candidate: ObservedCandidate,
    packet: ApprovedPacketSummary,
    target_digest: str,
    now: datetime,
) -> PlanReason | None:
    if (
        packet.account_id != scope.account_id
        or packet.action is not candidate.action
        or packet.candidate_fingerprint != candidate.fingerprint
        or packet.target_sha256 != target_digest
        or packet.policy_sha256 != scope.policy_sha256
    ):
        return PlanReason.APPROVAL_TARGET_MISMATCH
    if (
        not _is_sha256(packet.target_sha256)
        or not _is_sha256(packet.content_sha256)
        or not _is_sha256(packet.packet_revision_sha256)
        or not _is_utc(packet.expires_at)
        or packet.expires_at <= now
    ):
        return PlanReason.APPROVAL_TARGET_MISMATCH
    return None


def _binding_conflict(candidates: tuple[ObservedCandidate, ...]) -> PlanReason | None:
    fingerprints: dict[str, tuple[object, ...] | None] = {}
    replies: dict[str, tuple[object, ...]] = {}
    for candidate in candidates:
        binding = candidate.binding
        identity = None if binding is None else _binding_identity(binding)
        if (
            candidate.fingerprint in fingerprints
            and fingerprints[candidate.fingerprint] != identity
        ):
            return PlanReason.FINGERPRINT_BINDING_CONFLICT
        fingerprints[candidate.fingerprint] = identity
        if binding is None or binding.reply_id is None:
            continue
        reply_identity = (
            binding.account_id,
            binding.actor_id,
            binding.source,
            binding.root_id,
            binding.parent_id,
        )
        previous = replies.get(binding.reply_id)
        if previous is not None and previous != reply_identity:
            return PlanReason.REPLY_TARGET_COLLISION
        replies[binding.reply_id] = reply_identity
    return None


def _binding_identity(binding: VerifiedTargetBinding) -> tuple[object, ...]:
    return (
        binding.account_id,
        binding.actor_id,
        binding.source,
        binding.provenance,
        binding.target_kind,
        binding.thread_id,
        binding.reply_id,
        binding.root_id,
        binding.parent_id,
    )


def _target_digest(scope: CampaignScope, candidate: ObservedCandidate) -> str:
    binding = candidate.binding
    if binding is None:
        return ""
    payload = {
        "account": str(scope.account_id),
        "actor": scope.actor_id,
        "action": candidate.action.value,
        "source": candidate.source.value,
        "provenance": candidate.provenance.value,
        "target_kind": binding.target_kind.value,
        "thread": binding.thread_id,
        "reply": binding.reply_id,
        "root": binding.root_id,
        "parent": binding.parent_id,
    }
    return _digest(payload)


def _context_digest(candidate: ObservedCandidate) -> str:
    binding = candidate.binding
    if binding is None:
        return ""
    payload = {
        "account": str(candidate.account_id),
        "actor": binding.actor_id,
        "source": candidate.source.value,
        "provenance": candidate.provenance.value,
        "thread": binding.thread_id,
        "reply": binding.reply_id,
        "root": binding.root_id,
        "parent": binding.parent_id,
        "context_evidence": binding.context_evidence_sha256,
        "observed_at": binding.observed_at.isoformat(),
        "completeness": binding.completeness.value,
        "actor_verified": binding.actor_verified,
        "source_verified": binding.source_verified,
        "relationship_verified": binding.relationship_verified,
        "root_owned_by_account": binding.root_owned_by_account,
        "reply_owned_by_operator": binding.reply_owned_by_operator,
        "public_context": binding.public_context,
        "reply_control_allows_reply": binding.reply_control_allows_reply,
    }
    return _digest(payload)


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_permissions(candidate: ObservedCandidate) -> tuple[ApiPermission, ...]:
    source_permissions = _SOURCE_PERMISSIONS.get(candidate.source, ())
    action_permissions = (ApiPermission.THREADS_CONTENT_PUBLISH,)
    return tuple(dict.fromkeys((*source_permissions, *action_permissions)))


def _aggregate_decision(items: tuple[PlanItem, ...]) -> PlanDecision:
    decisions = {item.decision for item in items}
    if PlanDecision.STOP in decisions:
        return PlanDecision.STOP
    if PlanDecision.NEEDS_REVIEW in decisions:
        return PlanDecision.NEEDS_REVIEW
    if PlanDecision.NEEDS_CONTEXT in decisions:
        return PlanDecision.NEEDS_CONTEXT
    if PlanDecision.NOT_ELIGIBLE in decisions:
        return PlanDecision.NOT_ELIGIBLE
    return PlanDecision.OBSERVE_ONLY


def _valid_count(value: int | None) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _candidate_shape_valid(candidate: ObservedCandidate) -> bool:
    return (
        type(candidate.account_id) is UUID
        and type(candidate.source) is CandidateSource
        and type(candidate.provenance) is ObservationProvenance
        and type(candidate.action) is ActionKind
        and _is_sha256(candidate.fingerprint)
        and type(candidate.score) is int
        and candidate.score >= 0
        and type(candidate.source_order) is int
        and candidate.source_order >= 0
        and type(candidate.state) is CandidateState
        and type(candidate.observed_at) is datetime
        and (
            candidate.binding is None
            or (
                type(candidate.binding) is VerifiedTargetBinding
                and _binding_shape_valid(candidate.binding)
            )
        )
    )


def _binding_shape_valid(binding: VerifiedTargetBinding) -> bool:
    return (
        type(binding.account_id) is UUID
        and _valid_remote_id(binding.actor_id)
        and type(binding.source) is CandidateSource
        and type(binding.provenance) is ObservationProvenance
        and type(binding.target_kind) is TargetKind
        and all(
            value is None or type(value) is str
            for value in (binding.thread_id, binding.reply_id, binding.root_id, binding.parent_id)
        )
        and (binding.reply_observation is None or type(binding.reply_observation) is RemoteReply)
        and type(binding.observed_at) is datetime
        and type(binding.completeness) is ContextCompleteness
        and (binding.context_evidence_sha256 is None or _is_sha256(binding.context_evidence_sha256))
        and type(binding.actor_verified) is bool
        and type(binding.source_verified) is bool
        and type(binding.relationship_verified) is bool
        and type(binding.root_owned_by_account) is bool
        and (
            binding.reply_owned_by_operator is None or type(binding.reply_owned_by_operator) is bool
        )
        and type(binding.public_context) is bool
        and type(binding.reply_control_allows_reply) is bool
    )


def _valid_remote_id(value: object) -> bool:
    return type(value) is str and _REMOTE_ID.fullmatch(value) is not None


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _is_utc(value: object) -> bool:
    if type(value) is not datetime or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() == timedelta(0)
    except OverflowError, TypeError, ValueError:
        return False


def _is_fresh(
    observed_at: object,
    now: datetime,
    max_age: timedelta,
    *,
    allow_equal: bool = True,
) -> bool:
    if not _is_utc(observed_at):
        return False
    assert isinstance(observed_at, datetime)
    try:
        age = now - observed_at
        return age >= timedelta(0) and (age <= max_age if allow_equal else age < max_age)
    except OverflowError, TypeError, ValueError:
        return False
