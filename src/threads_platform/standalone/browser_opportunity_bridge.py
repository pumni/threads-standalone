"""Pure, source-preserving bridge from synthetic browser results to review proposals."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from threads_platform.browser.capabilities import (
    BrowserConversationItemResultV1,
    BrowserConversationResultV1,
    BrowserSearchItemResultV1,
    BrowserSearchResultV1,
)
from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import (
    normalize_manual_permalink,
    normalize_search_query,
)

SyntheticSource = Literal["SYNTHETIC_FIXTURE"]
ContextStatus = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
ProposalState = Literal["NEEDS_CONTEXT"]
AuthorIdentityStatus = Literal["UNVERIFIED"]
BridgeFailureCode = Literal[
    "BRIDGE_ACCOUNT_MISMATCH",
    "BRIDGE_INVALID_PROVENANCE",
    "BRIDGE_INVALID_RESULT",
    "BRIDGE_INVALID_TARGET",
    "BRIDGE_RESULT_LIMIT_EXCEEDED",
    "BRIDGE_TARGET_MISMATCH",
    "BRIDGE_CONTRADICTORY_OBSERVATIONS",
]
ProposalReasonCode = Literal[
    "AUTHOR_IDENTITY_UNVERIFIED",
    "CONTEXT_UNKNOWN",
    "CONTEXT_PARTIAL",
    "CONVERSATION_NOT_PROVIDED",
    "CONVERSATION_TRUNCATED",
    "EXCERPT_TRUNCATED",
    "SEARCH_RESULTS_TRUNCATED",
    "SYNTHETIC_SOURCE_REQUIRES_VERSIONED_QUEUE",
]

_MAX_ACTIVE_PROPOSALS = 20
_MAX_EXCERPT_CHARS = 500
_CONTEXT_STATUSES = frozenset({"UNKNOWN", "PARTIAL", "COMPLETE"})


class BrowserOpportunityBridgeError(ValueError):
    """Fixed-code bridge failure that does not expose account or post content."""

    def __init__(self, code: BridgeFailureCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AccountScopedSearchResult:
    """V1 search output paired with its caller-supplied local account scope."""

    account_uuid: UUID
    result: BrowserSearchResultV1


@dataclass(frozen=True, slots=True)
class AccountScopedConversationResult:
    """V1 conversation output and the exact permalink requested from its adapter."""

    account_uuid: UUID
    requested_target: str
    result: BrowserConversationResultV1


@dataclass(frozen=True, slots=True)
class ConversationObservationEvidence:
    """One bounded post observation; a parent is present only when explicitly observed."""

    target_permalink: str
    author_marker: str
    author_identity_status: AuthorIdentityStatus
    excerpt: str | None
    position: int
    parent_permalink: str | None
    is_root: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class BrowserOpportunityProposal:
    """Synthetic source-preserving review item; never an S6 v1 queue entry."""

    account_uuid: UUID
    target_permalink: str
    source: SyntheticSource
    context_provenance: SyntheticSource
    author_marker: str
    author_identity_status: AuthorIdentityStatus
    excerpt: str | None
    position: int
    search_context_status: ContextStatus
    context_status: ContextStatus
    review_state: ProposalState
    truncated: bool
    search_observation_truncated: bool
    search_result_truncated: bool
    conversation_result_truncated: bool | None
    conversation_observations: tuple[ConversationObservationEvidence, ...]
    reason_codes: tuple[ProposalReasonCode, ...]


@dataclass(frozen=True, slots=True)
class BrowserOpportunityProposalBatch:
    """Bounded pure output from one explicit synthetic search result."""

    account_uuid: UUID
    query: str
    result_version: Literal[1]
    source: SyntheticSource
    search_result_truncated: bool
    proposals: tuple[BrowserOpportunityProposal, ...]


@dataclass(frozen=True, slots=True)
class _SearchObservation:
    target_permalink: str
    author_marker: str
    excerpt: str | None
    position: int
    context_status: ContextStatus
    truncated: bool


@dataclass(frozen=True, slots=True)
class _ConversationObservation:
    target_permalink: str
    author_marker: str
    excerpt: str | None
    position: int
    parent_permalink: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class _ConversationEvidence:
    target_permalink: str
    context_status: ContextStatus
    truncated: bool
    observations: tuple[_ConversationObservation, ...]


def build_browser_opportunity_proposals(
    search: AccountScopedSearchResult,
    *,
    expected_account_uuid: UUID,
    conversations: Sequence[AccountScopedConversationResult] = (),
) -> BrowserOpportunityProposalBatch:
    """Convert accepted V1 adapter output to at most 20 review-only proposals.

    This function has no browser, network, queue, or filesystem dependency. The
    account UUID in each envelope must be stamped from the caller's local account
    and synthetic session context; it is an isolation binding, not actor
    authentication.
    """

    if expected_account_uuid.version != 4 or search.account_uuid != expected_account_uuid:
        raise BrowserOpportunityBridgeError("BRIDGE_ACCOUNT_MISMATCH")

    result = search.result
    search_observations = _normalize_search_result(result)
    by_target = {item.target_permalink: item for item in search_observations}

    if not _is_sequence(conversations) or len(conversations) > _MAX_ACTIVE_PROPOSALS:
        raise BrowserOpportunityBridgeError("BRIDGE_RESULT_LIMIT_EXCEEDED")

    conversation_by_target: dict[str, _ConversationEvidence] = {}
    for raw_scoped_conversation in cast(Sequence[object], conversations):
        if not isinstance(raw_scoped_conversation, AccountScopedConversationResult):
            raise BrowserOpportunityBridgeError("BRIDGE_ACCOUNT_MISMATCH")
        scoped_conversation = raw_scoped_conversation
        if scoped_conversation.account_uuid != expected_account_uuid:
            raise BrowserOpportunityBridgeError("BRIDGE_ACCOUNT_MISMATCH")
        requested_target, _ = _canonical_target(scoped_conversation.requested_target)
        search_observation = by_target.get(requested_target)
        if search_observation is None:
            raise BrowserOpportunityBridgeError("BRIDGE_TARGET_MISMATCH")

        conversation = _normalize_conversation_result(
            scoped_conversation.result,
            requested_target=requested_target,
        )
        if conversation.target_permalink != search_observation.target_permalink:
            raise BrowserOpportunityBridgeError("BRIDGE_TARGET_MISMATCH")
        previous = conversation_by_target.get(requested_target)
        if previous is not None and previous != conversation:
            raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
        conversation_by_target.setdefault(requested_target, conversation)

    proposals: list[BrowserOpportunityProposal] = []
    for item in search_observations:
        conversation = conversation_by_target.get(item.target_permalink)
        if conversation is None:
            context_status = item.context_status
            conversation_rows: tuple[ConversationObservationEvidence, ...] = ()
            conversation_truncated = None
            relationship_is_partial = False
        else:
            root = next(
                row
                for row in conversation.observations
                if row.target_permalink == item.target_permalink
            )
            if root.author_marker.casefold() != item.author_marker.casefold():
                raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
            if not _excerpts_compatible(
                item.excerpt,
                item.truncated,
                root.excerpt,
                root.truncated,
            ):
                raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
            relationship_is_partial = _conversation_has_missing_relationship(conversation)
            context_status = conversation.context_status
            if relationship_is_partial or conversation.truncated:
                context_status = "PARTIAL"
            conversation_rows = tuple(
                ConversationObservationEvidence(
                    target_permalink=row.target_permalink,
                    author_marker=row.author_marker,
                    author_identity_status="UNVERIFIED",
                    excerpt=row.excerpt,
                    position=row.position,
                    parent_permalink=row.parent_permalink,
                    is_root=row.target_permalink == item.target_permalink,
                    truncated=row.truncated,
                )
                for row in conversation.observations
            )
            conversation_truncated = conversation.truncated

        reasons = _proposal_reasons(
            context_status=context_status,
            excerpt_truncated=item.truncated or any(row.truncated for row in conversation_rows),
            search_result_truncated=result.truncated,
            conversation_not_provided=conversation is None,
            conversation_truncated=conversation_truncated is True
            or any(row.truncated for row in conversation_rows),
        )
        proposals.append(
            BrowserOpportunityProposal(
                account_uuid=expected_account_uuid,
                target_permalink=item.target_permalink,
                source="SYNTHETIC_FIXTURE",
                context_provenance="SYNTHETIC_FIXTURE",
                author_marker=item.author_marker,
                author_identity_status="UNVERIFIED",
                excerpt=item.excerpt,
                position=item.position,
                search_context_status=item.context_status,
                context_status=context_status,
                review_state="NEEDS_CONTEXT",
                truncated=(
                    item.truncated
                    or result.truncated
                    or (conversation_truncated is True)
                    or any(row.truncated for row in conversation_rows)
                ),
                search_observation_truncated=item.truncated,
                search_result_truncated=result.truncated,
                conversation_result_truncated=conversation_truncated,
                conversation_observations=conversation_rows,
                reason_codes=reasons,
            )
        )

    return BrowserOpportunityProposalBatch(
        account_uuid=expected_account_uuid,
        query=result.query,
        result_version=1,
        source="SYNTHETIC_FIXTURE",
        search_result_truncated=result.truncated,
        proposals=tuple(proposals),
    )


def _normalize_search_result(result: object) -> tuple[_SearchObservation, ...]:
    if not isinstance(result, BrowserSearchResultV1):
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
    if type(result.result_version) is not int or result.result_version != 1:
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
    if result.source != "SYNTHETIC_FIXTURE":
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_PROVENANCE")
    try:
        normalized_query = normalize_search_query(result.query)
    except BrowserContractError:
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT") from None
    if (
        normalized_query != result.query
        or type(result.truncated) is not bool
        or not _is_sequence(result.observations)
    ):
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
    if len(result.observations) > _MAX_ACTIVE_PROPOSALS:
        raise BrowserOpportunityBridgeError("BRIDGE_RESULT_LIMIT_EXCEEDED")

    deduplicated: dict[str, _SearchObservation] = {}
    for raw_item in cast(Sequence[object], result.observations):
        if not isinstance(raw_item, BrowserSearchItemResultV1):
            raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
        item = raw_item
        if item.source != "SYNTHETIC_FIXTURE":
            raise BrowserOpportunityBridgeError("BRIDGE_INVALID_PROVENANCE")
        if item.context_status not in _CONTEXT_STATUSES:
            raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
        # S11A search does not fetch conversation context; a stronger status is
        # contradictory to the accepted V1 search adapter contract.
        if item.context_status != "UNKNOWN":
            raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
        target, permalink_author = _canonical_target(item.thread_ref)
        _validate_author_marker(item.author_username, permalink_author)
        _validate_excerpt(item.text_excerpt)
        _validate_position(item.position)
        if type(item.truncated) is not bool:
            raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
        observation = _SearchObservation(
            target_permalink=target,
            author_marker=item.author_username,
            excerpt=item.text_excerpt,
            position=item.position,
            context_status=item.context_status,
            truncated=item.truncated,
        )
        previous = deduplicated.get(target)
        if previous is None:
            deduplicated[target] = observation
        else:
            deduplicated[target] = _merge_search_observations(previous, observation)

    return tuple(
        sorted(deduplicated.values(), key=lambda item: (item.position, item.target_permalink))
    )


def _merge_search_observations(
    left: _SearchObservation,
    right: _SearchObservation,
) -> _SearchObservation:
    if left.author_marker.casefold() != right.author_marker.casefold():
        raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
    if left.context_status != right.context_status or not _excerpts_compatible(
        left.excerpt,
        left.truncated,
        right.excerpt,
        right.truncated,
    ):
        raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
    marker = min(
        (left.author_marker, right.author_marker), key=lambda value: (value.casefold(), value)
    )
    excerpt, _ = _merge_excerpts(
        left.excerpt,
        left.truncated,
        right.excerpt,
        right.truncated,
    )
    return _SearchObservation(
        target_permalink=left.target_permalink,
        author_marker=marker,
        excerpt=excerpt,
        position=min(left.position, right.position),
        context_status=left.context_status,
        truncated=left.truncated or right.truncated,
    )


def _normalize_conversation_result(
    result: object,
    *,
    requested_target: str,
) -> _ConversationEvidence:
    if not isinstance(result, BrowserConversationResultV1):
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
    if type(result.result_version) is not int or result.result_version != 1:
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
    if result.source != "SYNTHETIC_FIXTURE":
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_PROVENANCE")
    target, _ = _canonical_target(result.root_thread_ref)
    if target != requested_target:
        raise BrowserOpportunityBridgeError("BRIDGE_TARGET_MISMATCH")
    if (
        result.context_status not in _CONTEXT_STATUSES
        or type(result.truncated) is not bool
        or not _is_sequence(result.observations)
    ):
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
    if len(result.observations) > _MAX_ACTIVE_PROPOSALS:
        raise BrowserOpportunityBridgeError("BRIDGE_RESULT_LIMIT_EXCEEDED")

    deduplicated: dict[str, _ConversationObservation] = {}
    for raw_item in cast(Sequence[object], result.observations):
        if not isinstance(raw_item, BrowserConversationItemResultV1):
            raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
        item = raw_item
        item_target, permalink_author = _canonical_target(item.thread_ref)
        _validate_author_marker(item.author_username, permalink_author)
        _validate_excerpt(item.text_excerpt)
        _validate_position(item.position)
        if type(item.truncated) is not bool:
            raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")
        parent_target: str | None = None
        if item.parent_ref is not None:
            parent_target, _ = _canonical_target(item.parent_ref)
        observation = _ConversationObservation(
            target_permalink=item_target,
            author_marker=item.author_username,
            excerpt=item.text_excerpt,
            position=item.position,
            parent_permalink=parent_target,
            truncated=item.truncated,
        )
        previous = deduplicated.get(item_target)
        if previous is None:
            deduplicated[item_target] = observation
        else:
            deduplicated[item_target] = _merge_conversation_observations(previous, observation)

    root = deduplicated.get(target)
    if root is None:
        raise BrowserOpportunityBridgeError("BRIDGE_TARGET_MISMATCH")
    if root.parent_permalink is not None or root.position != 0:
        raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")

    _validate_parent_cycles(deduplicated)
    observations = tuple(
        sorted(deduplicated.values(), key=lambda item: (item.position, item.target_permalink))
    )
    truncated = result.truncated or any(item.truncated for item in observations)
    status = result.context_status
    if truncated or _conversation_has_missing_relationship(
        _ConversationEvidence(target, status, truncated, observations)
    ):
        status = "PARTIAL"
    return _ConversationEvidence(
        target_permalink=target,
        context_status=status,
        truncated=result.truncated,
        observations=observations,
    )


def _merge_conversation_observations(
    left: _ConversationObservation,
    right: _ConversationObservation,
) -> _ConversationObservation:
    if (
        left.author_marker.casefold() != right.author_marker.casefold()
        or left.parent_permalink != right.parent_permalink
        or not _excerpts_compatible(
            left.excerpt,
            left.truncated,
            right.excerpt,
            right.truncated,
        )
    ):
        raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
    excerpt, _ = _merge_excerpts(
        left.excerpt,
        left.truncated,
        right.excerpt,
        right.truncated,
    )
    marker = min(
        (left.author_marker, right.author_marker), key=lambda value: (value.casefold(), value)
    )
    return _ConversationObservation(
        target_permalink=left.target_permalink,
        author_marker=marker,
        excerpt=excerpt,
        position=min(left.position, right.position),
        parent_permalink=left.parent_permalink,
        truncated=left.truncated or right.truncated,
    )


def _validate_parent_cycles(observations: dict[str, _ConversationObservation]) -> None:
    for target in observations:
        seen: set[str] = set()
        current: str | None = target
        while current is not None and current in observations:
            if current in seen:
                raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")
            seen.add(current)
            current = observations[current].parent_permalink


def _conversation_has_missing_relationship(evidence: _ConversationEvidence) -> bool:
    for item in evidence.observations:
        if item.target_permalink == evidence.target_permalink:
            continue
        if item.parent_permalink is None or item.parent_permalink not in {
            observation.target_permalink for observation in evidence.observations
        }:
            return True
    return False


def _proposal_reasons(
    *,
    context_status: ContextStatus,
    excerpt_truncated: bool,
    search_result_truncated: bool,
    conversation_not_provided: bool,
    conversation_truncated: bool,
) -> tuple[ProposalReasonCode, ...]:
    reasons: list[ProposalReasonCode] = [
        "AUTHOR_IDENTITY_UNVERIFIED",
        "SYNTHETIC_SOURCE_REQUIRES_VERSIONED_QUEUE",
    ]
    if conversation_not_provided:
        reasons.append("CONVERSATION_NOT_PROVIDED")
    if context_status == "UNKNOWN":
        reasons.append("CONTEXT_UNKNOWN")
    elif context_status == "PARTIAL":
        reasons.append("CONTEXT_PARTIAL")
    if excerpt_truncated:
        reasons.append("EXCERPT_TRUNCATED")
    if search_result_truncated:
        reasons.append("SEARCH_RESULTS_TRUNCATED")
    if conversation_truncated:
        reasons.append("CONVERSATION_TRUNCATED")
    return tuple(reasons)


def _canonical_target(value: object) -> tuple[str, str]:
    # Reuse the strict permalink parser only; its MANUAL_URL label is discarded.
    try:
        normalized = normalize_manual_permalink(value)
    except BrowserContractError:
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_TARGET") from None
    return normalized.thread_ref, normalized.author_username


def _validate_author_marker(marker: object, permalink_author: str) -> None:
    if (
        not isinstance(marker, str)
        or not 1 <= len(marker) <= 30
        or marker.casefold() != permalink_author.casefold()
    ):
        raise BrowserOpportunityBridgeError("BRIDGE_CONTRADICTORY_OBSERVATIONS")


def _validate_excerpt(value: object) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > _MAX_EXCERPT_CHARS):
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")


def _validate_position(value: object) -> None:
    if type(value) is not int or not 0 <= value < _MAX_ACTIVE_PROPOSALS:
        raise BrowserOpportunityBridgeError("BRIDGE_INVALID_RESULT")


def _excerpts_compatible(
    left: str | None,
    left_truncated: bool,
    right: str | None,
    right_truncated: bool,
) -> bool:
    if left is None or right is None or left == right:
        return True
    return (left_truncated and right.startswith(left)) or (
        right_truncated and left.startswith(right)
    )


def _merge_excerpts(
    left: str | None,
    left_truncated: bool,
    right: str | None,
    right_truncated: bool,
) -> tuple[str | None, bool]:
    if left is None:
        return right, right_truncated
    if right is None:
        return left, left_truncated
    if len(left) > len(right):
        return left, left_truncated or right_truncated
    if len(right) > len(left):
        return right, left_truncated or right_truncated
    return min(left, right), left_truncated or right_truncated


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
