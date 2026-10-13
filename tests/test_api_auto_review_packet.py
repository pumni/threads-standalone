from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from threads_platform.api.contracts import RemoteReply
from threads_platform.standalone.api_auto_content import (
    ApprovalInput,
    ContentDecision,
    ContentOrigin,
    ContentReason,
    JobFactsDeclaration,
    PacketReviewState,
    PreparedContent,
    ReviewContentPacket,
    RightsBasis,
    RightsEvidence,
    approval_input_for_review_packet,
    content_sha256_for,
    prepare_review_packet,
    revalidate_review_packet,
)
from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    ApiPermission,
    CampaignScope,
    CandidateSource,
    CandidateState,
    CapabilitySnapshot,
    ContextCompleteness,
    ObservationProvenance,
    ObservedCandidate,
    PlanDecision,
    QuotaSnapshot,
    ReviewIntent,
    TargetKind,
    VerifiedTargetBinding,
    plan_cycle,
)

NOW = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
ACCOUNT_ID = UUID("12345678-1234-5678-1234-567812345678")
ACTOR_ID = "synthetic-actor-1"
ALL_PERMISSIONS = frozenset(ApiPermission)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(
    *,
    now: datetime = NOW,
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    policy_sha256: str | None = None,
    permissions: frozenset[ApiPermission] = ALL_PERMISSIONS,
    post_usage: int = 1,
    post_total: int = 100,
    reply_usage: int = 1,
    reply_total: int = 100,
    max_actions_per_cycle: int = 1,
) -> CampaignScope:
    return CampaignScope(
        account_id=account_id,
        actor_id=actor_id,
        campaign_id="synthetic-campaign",
        policy_sha256=policy_sha256 or _sha("policy-v1"),
        capabilities=CapabilitySnapshot(
            account_id=account_id,
            actor_id=actor_id,
            permissions=permissions,
            verified=True,
            observed_at=now - timedelta(minutes=1),
        ),
        quota=QuotaSnapshot(
            post_usage=post_usage,
            post_total=post_total,
            reply_usage=reply_usage,
            reply_total=reply_total,
            verified=True,
            observed_at=now - timedelta(minutes=1),
        ),
        allowed_sources=frozenset(CandidateSource),
        allowed_actions=frozenset(ActionKind),
        policy_approved=True,
        max_actions_per_cycle=max_actions_per_cycle,
    )


def _post_candidate(
    *,
    now: datetime = NOW,
    label: str = "post",
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    provenance: ObservationProvenance = ObservationProvenance.LOCAL_OPERATOR_PACKET,
    state: CandidateState = CandidateState.ELIGIBLE,
) -> ObservedCandidate:
    observed_at = now - timedelta(minutes=1)
    binding = VerifiedTargetBinding(
        account_id=account_id,
        actor_id=actor_id,
        source=CandidateSource.OWN_ACCOUNT,
        provenance=provenance,
        target_kind=TargetKind.OWN_ACCOUNT,
        thread_id=None,
        reply_id=None,
        root_id=None,
        parent_id=None,
        reply_observation=None,
        observed_at=observed_at,
        completeness=ContextCompleteness.COMPLETE,
        context_evidence_sha256=_sha(f"post-context-{label}"),
        actor_verified=True,
        source_verified=True,
        relationship_verified=True,
        root_owned_by_account=True,
        reply_owned_by_operator=None,
        public_context=True,
        reply_control_allows_reply=False,
    )
    return ObservedCandidate(
        account_id=account_id,
        source=CandidateSource.OWN_ACCOUNT,
        provenance=provenance,
        action=ActionKind.POST_TEXT,
        fingerprint=_sha(f"post-fingerprint-{label}"),
        score=1,
        source_order=0,
        state=state,
        observed_at=observed_at,
        binding=binding,
    )


def _reply_candidate(
    *,
    now: datetime = NOW,
    label: str = "reply",
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    root_id: str = "synthetic-root-1",
    parent_id: str = "synthetic-parent-1",
    reply_id: str = "synthetic-reply-1",
    context_evidence_sha256: str | None = None,
    completeness: ContextCompleteness = ContextCompleteness.COMPLETE,
    state: CandidateState = CandidateState.ELIGIBLE,
) -> ObservedCandidate:
    observed_at = now - timedelta(minutes=1)
    provenance = ObservationProvenance.SYNTHETIC_FIXTURE
    binding = VerifiedTargetBinding(
        account_id=account_id,
        actor_id=actor_id,
        source=CandidateSource.INBOUND_REPLY,
        provenance=provenance,
        target_kind=TargetKind.INBOUND_REPLY,
        thread_id=root_id,
        reply_id=reply_id,
        root_id=root_id,
        parent_id=parent_id,
        reply_observation=RemoteReply(
            reply_id=reply_id,
            text="synthetic inbound reply",
            timestamp="2026-10-13T03:59:00Z",
            root_post_id=root_id,
            replied_to_id=parent_id,
            is_reply_owned_by_me=False,
        ),
        observed_at=observed_at,
        completeness=completeness,
        context_evidence_sha256=context_evidence_sha256 or _sha(f"reply-context-{label}"),
        actor_verified=True,
        source_verified=True,
        relationship_verified=True,
        root_owned_by_account=True,
        reply_owned_by_operator=False,
        public_context=True,
        reply_control_allows_reply=True,
    )
    return ObservedCandidate(
        account_id=account_id,
        source=CandidateSource.INBOUND_REPLY,
        provenance=provenance,
        action=ActionKind.REPLY,
        fingerprint=_sha(f"reply-fingerprint-{label}"),
        score=10,
        source_order=0,
        state=state,
        observed_at=observed_at,
        binding=binding,
    )


def _intent(
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    *,
    now: datetime = NOW,
) -> ReviewIntent:
    plan = plan_cycle(scope, candidates, None, now=now)
    assert plan.decision is PlanDecision.OBSERVE_ONLY
    intents = tuple(item.review_intent for item in plan.items if item.review_intent is not None)
    assert len(intents) == 1
    intent = intents[0]
    assert type(intent) is ReviewIntent
    assert intent.dispatchable is False
    return intent


def _prepared(
    intent: ReviewIntent,
    *,
    text: str = "Synthetic operator prepared text.",
    expires_at: datetime | None = None,
    origin: ContentOrigin | None = None,
    rights: RightsEvidence | None = None,
) -> PreparedContent:
    actual_origin = origin or (
        ContentOrigin.SYNTHETIC_FIXTURE
        if intent.provenance is ObservationProvenance.SYNTHETIC_FIXTURE
        else ContentOrigin.OPERATOR_AUTHORED
    )
    if rights is None:
        if actual_origin is ContentOrigin.SYNTHETIC_FIXTURE:
            rights = RightsEvidence(
                source_attribution="synthetic test fixture",
                rights_basis=RightsBasis.SYNTHETIC_FIXTURE,
                right_to_use_declared=True,
                job_facts_sha256=_sha("synthetic-job-facts"),
                job_facts_declaration=JobFactsDeclaration.SYNTHETIC_FIXTURE,
            )
        else:
            rights = RightsEvidence(
                source_attribution="operator authored synthetic test",
                rights_basis=RightsBasis.OPERATOR_CREATED,
                right_to_use_declared=True,
                job_facts_sha256=_sha("operator-declared-job-facts"),
                job_facts_declaration=JobFactsDeclaration.OPERATOR_VERIFIED,
            )
    return PreparedContent(
        text=text,
        origin=actual_origin,
        rights=rights,
        prepared_at=NOW - timedelta(minutes=2),
        effective_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or min(intent.expires_at, NOW + timedelta(minutes=10)),
    )


def _prepare(
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    *,
    now: datetime = NOW,
    text: str = "Synthetic operator prepared text.",
    expires_at: datetime | None = None,
):
    intent = _intent(scope, candidates, now=now)
    prepared = _prepared(intent, text=text, expires_at=expires_at)
    result = prepare_review_packet(intent, prepared, scope, candidates, now=now)
    return intent, prepared, result


def _intent_from_plan(
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
) -> ReviewIntent:
    plan = plan_cycle(scope, candidates, None, now=NOW)
    intents = tuple(item.review_intent for item in plan.items if item.review_intent is not None)
    assert len(intents) == 1
    intent = intents[0]
    assert type(intent) is ReviewIntent
    return intent


@pytest.mark.parametrize("action", ["post", "reply"])
def test_planner_intent_connects_to_review_only_content_packet(action: str) -> None:
    scope = _scope()
    candidates = (_post_candidate(),) if action == "post" else (_reply_candidate(),)

    intent, prepared, result = _prepare(scope, candidates)

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.reason_codes == ()
    assert type(result.packet) is ReviewContentPacket
    packet = result.packet
    assert packet.review_state is PacketReviewState.NEEDS_OPERATOR_REVIEW
    assert packet.dispatchable is False
    assert packet.content_sha256 == content_sha256_for(prepared.text)
    assert packet.binding.account_id == scope.account_id
    assert packet.binding.target_sha256 == intent.target_sha256
    assert packet.binding.context_sha256 == intent.context_sha256
    assert not hasattr(packet, "proposal_binding")
    assert not hasattr(packet, "approved_packet_summary")

    projection = approval_input_for_review_packet(result)
    assert type(projection) is ApprovalInput
    assert projection.review_state is PacketReviewState.NEEDS_OPERATOR_REVIEW
    assert projection.action is intent.action
    assert projection.packet_revision_sha256 == packet.packet_revision_sha256
    assert projection.expires_at == prepared.expires_at


@pytest.mark.parametrize(
    "state",
    [CandidateState.COOLDOWN, CandidateState.PENDING, CandidateState.CONFIRMED],
)
def test_prepare_review_packet_accepts_eligible_intent_with_normal_ineligible_neighbor(
    state: CandidateState,
) -> None:
    scope = _scope()
    selected = _post_candidate(label=f"selected-{state.value}")
    neighbor = _reply_candidate(label=f"neighbor-{state.value}", state=state)
    candidates = (selected, neighbor)
    plan = plan_cycle(scope, candidates, None, now=NOW)
    intent = _intent_from_plan(scope, candidates)

    assert plan.decision is PlanDecision.NOT_ELIGIBLE
    result = prepare_review_packet(intent, _prepared(intent), scope, candidates, now=NOW)

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.packet is not None
    assert result.packet.binding.candidate_fingerprint == intent.candidate_fingerprint


@pytest.mark.parametrize(
    "state",
    [CandidateState.COOLDOWN, CandidateState.PENDING, CandidateState.CONFIRMED],
)
def test_revalidate_review_packet_accepts_eligible_intent_with_normal_ineligible_neighbor(
    state: CandidateState,
) -> None:
    scope = _scope()
    selected = _post_candidate(label=f"selected-{state.value}")
    original_candidates = (selected,)
    intent = _intent(scope, original_candidates)
    prepared_result = prepare_review_packet(
        intent,
        _prepared(intent),
        scope,
        original_candidates,
        now=NOW,
    )
    assert prepared_result.packet is not None
    neighbor = _reply_candidate(label=f"neighbor-{state.value}", state=state)
    candidates = (selected, neighbor)
    plan = plan_cycle(scope, candidates, None, now=NOW)

    assert plan.decision is PlanDecision.NOT_ELIGIBLE
    result = revalidate_review_packet(prepared_result.packet, scope, candidates, now=NOW)

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.packet == prepared_result.packet


@pytest.mark.parametrize(
    "blocker",
    [
        "duplicate_fingerprint",
        "root_collision",
        "parent_collision",
        "ambiguous",
        "needs_context",
        "stop",
    ],
)
def test_mixed_batch_blockers_prevent_prepare_and_revalidation(
    blocker: str,
) -> None:
    scope = _scope()
    selected = _post_candidate(label=f"selected-{blocker}")
    if blocker == "duplicate_fingerprint":
        candidates = (selected, replace(selected))
    elif blocker in {"root_collision", "parent_collision"}:
        shared = _reply_candidate(
            label=f"collision-first-{blocker}",
            root_id="synthetic-collision-root",
            parent_id="synthetic-collision-parent",
            reply_id=f"synthetic-collision-reply-{blocker}",
        )
        collision = _reply_candidate(
            label=f"collision-second-{blocker}",
            root_id=(
                "synthetic-other-root"
                if blocker == "root_collision"
                else "synthetic-collision-root"
            ),
            parent_id=(
                "synthetic-collision-parent"
                if blocker == "root_collision"
                else "synthetic-other-parent"
            ),
            reply_id=f"synthetic-collision-reply-{blocker}",
        )
        candidates = (selected, shared, collision)
    elif blocker == "ambiguous":
        candidates = (
            selected,
            _reply_candidate(label="ambiguous-neighbor", state=CandidateState.AMBIGUOUS),
        )
    elif blocker == "needs_context":
        candidates = (
            selected,
            _reply_candidate(
                label="incomplete-neighbor",
                completeness=ContextCompleteness.PARTIAL,
            ),
        )
    else:
        candidates = (
            selected,
            replace(_reply_candidate(label="unavailable-neighbor"), binding=None),
        )

    intent = _intent(scope, (selected,))
    prepared = _prepared(intent)
    initial = prepare_review_packet(intent, prepared, scope, (selected,), now=NOW)
    assert initial.packet is not None

    prepared_with_blocker = prepare_review_packet(intent, prepared, scope, candidates, now=NOW)
    revalidated_with_blocker = revalidate_review_packet(
        initial.packet,
        scope,
        candidates,
        now=NOW,
    )

    for result in (prepared_with_blocker, revalidated_with_blocker):
        assert result.decision is ContentDecision.STOP
        assert result.packet is None


def test_own_post_has_no_remote_target_ids_and_reply_binds_all_three_references() -> None:
    post_scope = _scope()
    post_intent = _intent(post_scope, (_post_candidate(),))
    post_result = prepare_review_packet(
        post_intent,
        _prepared(post_intent),
        post_scope,
        (_post_candidate(),),
        now=NOW,
    )
    assert post_result.packet is not None
    assert post_result.packet.binding.root_sha256 is None
    assert post_result.packet.binding.parent_sha256 is None
    assert post_result.packet.binding.reply_sha256 is None

    reply_scope = _scope()
    reply_candidate = _reply_candidate()
    reply_intent = _intent(reply_scope, (reply_candidate,))
    reply_result = prepare_review_packet(
        reply_intent,
        _prepared(reply_intent),
        reply_scope,
        (reply_candidate,),
        now=NOW,
    )
    assert reply_result.packet is not None
    binding = reply_result.packet.binding
    assert binding.root_sha256 is not None
    assert binding.parent_sha256 is not None
    assert binding.reply_sha256 is not None
    assert len({binding.root_sha256, binding.parent_sha256, binding.reply_sha256}) == 3
    assert not hasattr(binding, "actor_id")
    assert not hasattr(binding, "root_id")
    rendered = repr(reply_result.packet)
    for raw_id in (ACTOR_ID, "synthetic-root-1", "synthetic-parent-1", "synthetic-reply-1"):
        assert raw_id not in rendered


def test_text_is_normalized_before_content_hash_and_revision_are_created() -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    intent = _intent(scope, candidates)
    prepared = _prepared(intent, text="Cafe\u0301\r\nsecond line\rthird line")

    result = prepare_review_packet(intent, prepared, scope, candidates, now=NOW)

    assert result.packet is not None
    assert result.packet.prepared_content.text == "Café\nsecond line\nthird line"
    assert result.packet.content_sha256 == content_sha256_for("Café\nsecond line\nthird line")
    assert result.packet.normalization_version == "nfc-lf-v1"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("   \r\n  ", ContentReason.CONTENT_BLANK),
        ("x" * 501, ContentReason.CONTENT_TOO_LONG),
        ("bad \ud800 unicode", ContentReason.TEXT_INVALID_UNICODE),
    ],
)
def test_invalid_or_unbounded_operator_text_never_creates_a_packet(
    text: str, reason: ContentReason
) -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    intent = _intent(scope, candidates)

    result = prepare_review_packet(intent, _prepared(intent, text=text), scope, candidates, now=NOW)

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (reason,)
    assert result.packet is None


@pytest.mark.parametrize(
    "field_name",
    [
        "account_id",
        "actor_id",
        "source",
        "provenance",
        "candidate_fingerprint",
        "target_sha256",
        "context_sha256",
        "root_sha256",
        "parent_sha256",
        "reply_sha256",
        "context_evidence_sha256",
        "policy_sha256",
    ],
)
def test_caller_supplied_intent_must_match_current_planner_output(field_name: str) -> None:
    scope = _scope()
    candidates = (_reply_candidate(),)
    intent = _intent(scope, candidates)
    value: object = _sha(f"wrong-{field_name}")
    if field_name == "account_id":
        value = UUID("87654321-4321-8765-4321-876543218765")
    elif field_name == "actor_id":
        value = "other-synthetic-actor"
    elif field_name == "source":
        value = CandidateSource.MENTIONS
    elif field_name == "provenance":
        value = ObservationProvenance.OFFICIAL_API
    wrong_intent = replace(intent, **{field_name: value})

    result = prepare_review_packet(
        wrong_intent,
        _prepared(intent),
        scope,
        candidates,
        now=NOW,
    )

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (ContentReason.REVIEW_INTENT_MISMATCH,)
    assert result.packet is None


@pytest.mark.parametrize("changed", ["root", "parent", "reply", "context", "actor", "policy"])
def test_revalidation_stops_when_fresh_binding_changes(changed: str) -> None:
    scope = _scope()
    original_candidate = _reply_candidate()
    candidates = (original_candidate,)
    _, _, prepared_result = _prepare(scope, candidates)
    assert prepared_result.packet is not None
    packet = prepared_result.packet

    current_scope = scope
    if changed == "root":
        current_candidates = (_reply_candidate(root_id="synthetic-root-2"),)
    elif changed == "parent":
        current_candidates = (_reply_candidate(parent_id="synthetic-parent-2"),)
    elif changed == "reply":
        current_candidates = (_reply_candidate(reply_id="synthetic-reply-2"),)
    elif changed == "context":
        current_candidates = (_reply_candidate(context_evidence_sha256=_sha("changed-context")),)
    elif changed == "actor":
        current_scope = _scope(actor_id="synthetic-actor-2")
        current_candidates = (_reply_candidate(actor_id="synthetic-actor-2"),)
    else:
        current_scope = _scope(policy_sha256=_sha("policy-v2"))
        current_candidates = candidates

    result = revalidate_review_packet(packet, current_scope, current_candidates, now=NOW)

    assert result.decision is ContentDecision.STOP
    assert result.packet is None
    assert result.reason_codes[0] in {
        ContentReason.TARGET_MISMATCH,
        ContentReason.CONTEXT_MISMATCH,
        ContentReason.POLICY_MISMATCH,
    }


def test_revalidation_stops_for_wrong_account_and_missing_target_after_restart() -> None:
    scope = _scope()
    candidates = (_reply_candidate(),)
    _, _, prepared_result = _prepare(scope, candidates)
    assert prepared_result.packet is not None
    packet = prepared_result.packet

    other_account = UUID("87654321-4321-8765-4321-876543218765")
    account_scope = _scope(account_id=other_account)
    account_candidate = _reply_candidate(account_id=other_account)
    account_result = revalidate_review_packet(packet, account_scope, (account_candidate,), now=NOW)
    missing_result = revalidate_review_packet(packet, scope, (), now=NOW)

    assert account_result.decision is ContentDecision.STOP
    assert account_result.packet is None
    assert missing_result.decision is ContentDecision.STOP
    assert missing_result.packet is None


def test_fresh_reobservation_of_same_target_and_context_passes_revalidation() -> None:
    scope = _scope()
    candidate = _reply_candidate()
    _, _, prepared_result = _prepare(scope, (candidate,), expires_at=NOW + timedelta(minutes=10))
    assert prepared_result.packet is not None

    later = NOW + timedelta(minutes=3)
    refreshed_scope = _scope(now=later)
    refreshed_candidate = _reply_candidate(now=later)
    result = revalidate_review_packet(
        prepared_result.packet,
        refreshed_scope,
        (refreshed_candidate,),
        now=later,
    )

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.packet == prepared_result.packet


def test_original_intent_expiry_is_not_extended_by_fresh_restart_evidence() -> None:
    scope = _scope()
    candidate = _reply_candidate()
    _, _, prepared_result = _prepare(scope, (candidate,))
    assert prepared_result.packet is not None
    packet = prepared_result.packet
    later = packet.binding.expires_at + timedelta(seconds=1)
    refreshed_scope = _scope(now=later)
    refreshed_candidate = _reply_candidate(now=later)

    result = revalidate_review_packet(packet, refreshed_scope, (refreshed_candidate,), now=later)

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (ContentReason.WINDOW_EXPIRED,)
    assert result.packet is None


def test_quota_change_and_stale_candidate_block_packet_revalidation_and_preparation() -> None:
    scope = _scope()
    candidate = _reply_candidate()
    _, _, prepared_result = _prepare(scope, (candidate,))
    assert prepared_result.packet is not None
    later = NOW + timedelta(minutes=2)
    changed_quota = _scope(now=later, reply_usage=2)
    refreshed_candidate = _reply_candidate(now=later)
    quota_result = revalidate_review_packet(
        prepared_result.packet,
        changed_quota,
        (refreshed_candidate,),
        now=later,
    )

    stale_candidate = _reply_candidate(now=NOW - timedelta(hours=1))
    stale_result = prepare_review_packet(
        _intent(scope, (candidate,)),
        _prepared(_intent(scope, (candidate,))),
        scope,
        (stale_candidate,),
        now=NOW,
    )

    assert quota_result.decision is ContentDecision.STOP
    assert quota_result.reason_codes == (ContentReason.QUOTA_UNAVAILABLE,)
    assert quota_result.packet is None
    assert stale_result.decision is ContentDecision.STOP
    assert stale_result.reason_codes == (ContentReason.EVIDENCE_STALE,)
    assert stale_result.packet is None


@pytest.mark.parametrize(
    ("scope_kwargs", "candidate_kwargs", "expected_reason"),
    [
        (
            {"permissions": frozenset({ApiPermission.THREADS_BASIC})},
            {},
            ContentReason.API_PERMISSION_MISSING,
        ),
        ({"reply_usage": 100, "reply_total": 100}, {}, ContentReason.QUOTA_EXHAUSTED),
        ({"max_actions_per_cycle": 0}, {}, ContentReason.ACTION_NOT_ALLOWED),
        ({}, {"state": CandidateState.AMBIGUOUS}, ContentReason.AMBIGUOUS_PRIOR_ACTION),
        ({}, {"state": CandidateState.COOLDOWN}, ContentReason.COOLDOWN),
        ({}, {"completeness": ContextCompleteness.PARTIAL}, ContentReason.CONTEXT_MISMATCH),
    ],
)
def test_planner_stop_permissions_quota_or_state_never_yield_packet(
    scope_kwargs: dict[str, Any],
    candidate_kwargs: dict[str, Any],
    expected_reason: ContentReason,
) -> None:
    baseline_scope = _scope()
    baseline_candidate = _reply_candidate()
    baseline_intent = _intent(baseline_scope, (baseline_candidate,))
    scope = _scope(**scope_kwargs)
    candidate = _reply_candidate(**candidate_kwargs)
    result = prepare_review_packet(
        baseline_intent,
        _prepared(baseline_intent),
        scope,
        (candidate,),
        now=NOW,
    )

    assert result.packet is None
    assert result.reason_codes == (expected_reason,)


def test_planner_collisions_and_stale_evidence_never_yield_packet() -> None:
    scope = _scope()
    first = _reply_candidate()
    collision = _reply_candidate(root_id="synthetic-root-other")
    collision = replace(collision, fingerprint=first.fingerprint)
    collision_result = prepare_review_packet(
        _intent(scope, (first,)),
        _prepared(_intent(scope, (first,))),
        scope,
        (first, collision),
        now=NOW,
    )
    stale = _reply_candidate(now=NOW - timedelta(hours=1))
    stale_plan = plan_cycle(scope, (stale,), None, now=NOW)

    assert collision_result.decision is ContentDecision.STOP
    assert collision_result.packet is None
    assert stale_plan.decision is PlanDecision.STOP


def test_missing_provider_ai_and_external_content_are_unavailable() -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    intent = _intent(scope, candidates)

    absent = prepare_review_packet(intent, None, scope, candidates, now=NOW)
    ai = prepare_review_packet(
        intent,
        _prepared(intent, origin=ContentOrigin.AI_PROVIDER, rights=None),
        scope,
        candidates,
        now=NOW,
    )
    external = prepare_review_packet(
        intent,
        _prepared(intent, origin=ContentOrigin.EXTERNAL_SOURCE, rights=None),
        scope,
        candidates,
        now=NOW,
    )

    for result in (absent, ai, external):
        assert result.decision is ContentDecision.UNAVAILABLE
        assert result.packet is None


def test_changed_text_rights_hash_or_revision_cannot_revalidate_packet() -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    _, _, prepared_result = _prepare(scope, candidates)
    assert prepared_result.packet is not None
    packet = prepared_result.packet

    edited = replace(
        packet,
        prepared_content=replace(packet.prepared_content, text="edited final text"),
    )
    rights_changed = replace(
        packet,
        prepared_content=replace(
            packet.prepared_content,
            rights=replace(packet.prepared_content.rights, right_to_use_declared=False),
        ),
    )
    content_hash_changed = replace(packet, content_sha256=_sha("different content"))
    revision_changed = replace(packet, packet_revision_sha256=_sha("different revision"))

    for changed in (edited, rights_changed, content_hash_changed, revision_changed):
        result = revalidate_review_packet(changed, scope, candidates, now=NOW)
        assert result.decision is ContentDecision.STOP
        assert result.packet is None


def test_duplicate_packet_revision_is_stopped_in_prepare_and_revalidate() -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    intent = _intent(scope, candidates)
    prepared = _prepared(intent)
    first = prepare_review_packet(intent, prepared, scope, candidates, now=NOW)
    assert first.packet is not None

    duplicate_prepare = prepare_review_packet(
        intent,
        prepared,
        scope,
        candidates,
        now=NOW,
        known_packet_revisions=frozenset({first.packet.packet_revision_sha256}),
    )
    duplicate_revalidate = revalidate_review_packet(
        first.packet,
        scope,
        candidates,
        now=NOW,
        known_packet_revisions=frozenset({first.packet.packet_revision_sha256}),
    )

    assert duplicate_prepare.decision is ContentDecision.STOP
    assert duplicate_prepare.reason_codes == (ContentReason.DUPLICATE_PACKET,)
    assert duplicate_revalidate.decision is ContentDecision.STOP
    assert duplicate_revalidate.reason_codes == (ContentReason.DUPLICATE_PACKET,)


def test_content_expiry_must_not_outlive_intent_or_survive_elapsed_window() -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    intent = _intent(scope, candidates)
    too_late = _prepared(intent, expires_at=intent.expires_at + timedelta(seconds=1))
    expired = _prepared(intent, expires_at=NOW)

    outlives_intent = prepare_review_packet(intent, too_late, scope, candidates, now=NOW)
    already_expired = prepare_review_packet(intent, expired, scope, candidates, now=NOW)

    assert outlives_intent.decision is ContentDecision.STOP
    assert outlives_intent.reason_codes == (ContentReason.WINDOW_INVALID,)
    assert outlives_intent.packet is None
    assert already_expired.decision is ContentDecision.STOP
    assert already_expired.reason_codes == (ContentReason.WINDOW_EXPIRED,)
    assert already_expired.packet is None


def test_review_projection_rejects_invalid_evaluation_and_keeps_repr_redacted() -> None:
    scope = _scope()
    candidates = (_reply_candidate(),)
    intent, prepared, result = _prepare(scope, candidates)
    assert result.packet is not None
    packet = result.packet

    assert approval_input_for_review_packet(replace(result, decision=ContentDecision.STOP)) is None
    assert (
        approval_input_for_review_packet(
            replace(result, packet=replace(packet, packet_revision_sha256=_sha("bad")))
        )
        is None
    )
    rendered = " ".join(
        repr(value)
        for value in (
            intent,
            prepared,
            packet.binding,
            packet,
            result,
            approval_input_for_review_packet(result),
        )
    )
    for private_value in (
        str(ACCOUNT_ID),
        ACTOR_ID,
        prepared.text,
        "synthetic-root-1",
        "synthetic-parent-1",
        "synthetic-reply-1",
        intent.candidate_fingerprint,
        intent.target_sha256,
        intent.context_sha256,
        packet.content_sha256,
        packet.packet_revision_sha256,
    ):
        assert private_value not in rendered


def test_review_contract_uses_no_action_proposal_or_external_effects() -> None:
    scope = _scope()
    candidates = (_post_candidate(),)
    intent = _intent(scope, candidates)
    result = prepare_review_packet(intent, _prepared(intent), scope, candidates, now=NOW)

    assert result.packet is not None
    assert result.packet.dispatchable is False
    assert "ActionProposal" not in repr(result.packet)
    assert "approval" not in repr(result.packet).lower()
