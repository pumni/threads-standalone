import ast
import hashlib
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from threads_platform.standalone import api_auto_content
from threads_platform.standalone.api_auto_content import (
    ApprovalInput,
    ContentAction,
    ContentDecision,
    ContentOrigin,
    ContentReason,
    DraftRequest,
    JobFactsDeclaration,
    PacketReviewState,
    PreparedContent,
    RightsBasis,
    RightsEvidence,
    approval_input_for_review,
    content_sha256_for,
    evaluate_review_eligibility,
    packet_revision_sha256_for,
    prepare_packet,
)
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

NOW = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
ACCOUNT_A = UUID("12345678-1234-5678-1234-567812345678")
ACCOUNT_B = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACTOR_ID = "synthetic-actor"
ALL_PERMISSIONS = frozenset(ApiPermission)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(
    *,
    account_id: UUID = ACCOUNT_A,
    permissions: frozenset[ApiPermission] = ALL_PERMISSIONS,
    quota: QuotaSnapshot | None = None,
    policy_sha256: str | None = None,
) -> CampaignScope:
    return CampaignScope(
        account_id=account_id,
        actor_id=ACTOR_ID,
        campaign_id="synthetic-campaign",
        policy_sha256=policy_sha256 or _sha("policy-v1"),
        capabilities=CapabilitySnapshot(
            account_id=account_id,
            actor_id=ACTOR_ID,
            permissions=permissions,
            verified=True,
            observed_at=NOW - timedelta(minutes=1),
        ),
        quota=quota
        or QuotaSnapshot(
            post_usage=1,
            post_total=100,
            reply_usage=1,
            reply_total=100,
            verified=True,
            observed_at=NOW - timedelta(minutes=1),
        ),
        allowed_sources=frozenset(CandidateSource),
        allowed_actions=frozenset(ActionKind),
        policy_approved=True,
    )


def _rights(origin: ContentOrigin) -> RightsEvidence:
    if origin is ContentOrigin.SYNTHETIC_FIXTURE:
        return RightsEvidence(
            source_attribution="synthetic fixture",
            rights_basis=RightsBasis.SYNTHETIC_FIXTURE,
            right_to_use_declared=True,
            job_facts_sha256=_sha("synthetic job facts"),
            job_facts_declaration=JobFactsDeclaration.SYNTHETIC_FIXTURE,
        )
    return RightsEvidence(
        source_attribution="operator-authored",
        rights_basis=RightsBasis.OPERATOR_CREATED,
        right_to_use_declared=True,
        job_facts_sha256=_sha("operator verified job facts"),
        job_facts_declaration=JobFactsDeclaration.OPERATOR_VERIFIED,
    )


def _prepared_content(
    text: str = "Operator prepared recruitment text.",
    *,
    origin: ContentOrigin = ContentOrigin.OPERATOR_AUTHORED,
    rights: RightsEvidence | None = None,
    prepared_at: datetime | None = None,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> PreparedContent:
    return PreparedContent(
        text=text,
        origin=origin,
        rights=rights or _rights(origin),
        prepared_at=prepared_at or NOW - timedelta(minutes=1),
        effective_at=effective_at or NOW - timedelta(seconds=30),
        expires_at=expires_at or NOW + timedelta(minutes=10),
    )


def _proposal(
    scope: CampaignScope,
    prepared: PreparedContent,
    *,
    action: ActionKind = ActionKind.POST_TEXT,
    label: str = "one",
    provenance: ObservationProvenance | None = None,
    source: CandidateSource | None = None,
    target_sha256: str | None = None,
    context_sha256: str | None = None,
    account_id: UUID | None = None,
) -> ActionProposal:
    if action is ActionKind.POST_TEXT:
        actual_source = source or CandidateSource.OWN_ACCOUNT
        actual_provenance = provenance or (
            ObservationProvenance.SYNTHETIC_FIXTURE
            if prepared.origin is ContentOrigin.SYNTHETIC_FIXTURE
            else ObservationProvenance.LOCAL_OPERATOR_PACKET
        )
        required = (
            ApiPermission.THREADS_BASIC,
            ApiPermission.THREADS_CONTENT_PUBLISH,
        )
    else:
        actual_source = source or CandidateSource.INBOUND_REPLY
        actual_provenance = provenance or (
            ObservationProvenance.SYNTHETIC_FIXTURE
            if prepared.origin is ContentOrigin.SYNTHETIC_FIXTURE
            else ObservationProvenance.OFFICIAL_API
        )
        required = (
            ApiPermission.THREADS_BASIC,
            ApiPermission.THREADS_READ_REPLIES,
            ApiPermission.THREADS_CONTENT_PUBLISH,
        )
    proposal = ActionProposal(
        account_id=account_id or scope.account_id,
        action=action,
        source=actual_source,
        provenance=actual_provenance,
        candidate_fingerprint=_sha(f"fingerprint-{label}"),
        target_sha256=target_sha256 or _sha(f"target-{label}"),
        context_sha256=context_sha256 or _sha(f"context-{label}"),
        content_sha256=content_sha256_for(prepared.text),
        packet_revision_sha256=_sha("packet-revision-placeholder"),
        required_permissions=required,
        expires_at=prepared.expires_at,
        decision=PlanDecision.NEEDS_REVIEW,
        dispatchable=False,
    )
    revision = packet_revision_sha256_for(
        proposal,
        prepared,
        policy_revision_sha256=scope.policy_sha256,
    )
    return replace(proposal, packet_revision_sha256=revision)


def _case(
    *,
    action: ActionKind = ActionKind.POST_TEXT,
    label: str = "one",
    account_id: UUID = ACCOUNT_A,
    origin: ContentOrigin | None = None,
    text: str = "Operator prepared recruitment text.",
) -> tuple[CampaignScope, PreparedContent, ActionProposal]:
    if origin is None:
        origin = (
            ContentOrigin.SYNTHETIC_FIXTURE
            if action is ActionKind.REPLY
            else ContentOrigin.OPERATOR_AUTHORED
        )
    scope = _scope(account_id=account_id)
    prepared = _prepared_content(text, origin=origin)
    proposal = _proposal(scope, prepared, action=action, label=label)
    return scope, prepared, proposal


def _prepare(
    scope: CampaignScope,
    prepared: PreparedContent,
    proposal: ActionProposal,
    *,
    state: CandidateState = CandidateState.ELIGIBLE,
    now: datetime = NOW,
    known: frozenset[str] = frozenset(),
):
    return prepare_packet(
        proposal,
        prepared,
        scope,
        state,
        now=now,
        known_packet_revisions=known,
    )


def test_operator_authored_original_post_is_review_only() -> None:
    scope, prepared, proposal = _case()

    result = _prepare(scope, prepared, proposal)

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.reason_codes == ()
    assert result.packet is not None
    assert result.packet.action is ContentAction.OWN_POST
    assert result.packet.account_id == ACCOUNT_A
    assert result.packet.review_state is PacketReviewState.NEEDS_OPERATOR_REVIEW
    assert result.packet.content_sha256 == proposal.content_sha256
    assert not hasattr(result, "dispatchable")
    assert "APPROVED" not in {state.value for state in PacketReviewState}
    assert "APPROVED" not in {decision.value for decision in ContentDecision}


def test_exact_synthetic_reply_proposal_is_bound_but_not_approved() -> None:
    scope, prepared, proposal = _case(action=ActionKind.REPLY, label="reply")

    result = _prepare(scope, prepared, proposal)

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.packet is not None
    assert result.packet.action is ContentAction.EXACT_TARGET_REPLY
    assert result.packet.target_sha256 == proposal.target_sha256
    assert result.packet.context_sha256 == proposal.context_sha256
    assert result.packet.proposal_binding.candidate_fingerprint == proposal.candidate_fingerprint
    assert result.packet.review_state is PacketReviewState.NEEDS_OPERATOR_REVIEW


def test_absent_content_provider_returns_unavailable_without_fabricating_text() -> None:
    scope, prepared, proposal = _case()
    result = prepare_packet(proposal, None, scope, CandidateState.ELIGIBLE, now=NOW)

    assert result.decision is ContentDecision.UNAVAILABLE
    assert result.reason_codes == (ContentReason.PROVIDER_UNAVAILABLE,)
    assert result.packet is None
    assert prepared.text not in repr(result)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("   \r\n\t ", ContentReason.CONTENT_BLANK),
        ("x" * 501, ContentReason.CONTENT_TOO_LONG),
        ("invalid \ud800 unicode", ContentReason.TEXT_INVALID_UNICODE),
    ],
)
def test_invalid_or_unbounded_text_fails_closed(text: str, reason: ContentReason) -> None:
    scope, valid_content, proposal = _case()

    result = _prepare(scope, replace(valid_content, text=text), proposal)

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (reason,)
    assert result.packet is None


def test_text_normalization_is_nfc_and_lf_and_hashes_the_reviewed_text() -> None:
    text = "Cafe\u0301\r\nsecond line\rthird line"
    scope, prepared, proposal = _case(text=text)

    result = _prepare(scope, prepared, proposal)

    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.packet is not None
    assert result.packet.prepared_content.text == "Café\nsecond line\nthird line"
    assert result.packet.content_sha256 == content_sha256_for("Café\nsecond line\nthird line")


def test_changed_text_or_content_digest_cannot_reuse_a_packet() -> None:
    scope, prepared, proposal = _case()
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None

    changed_text = replace(
        result.packet,
        prepared_content=replace(result.packet.prepared_content, text="edited final text"),
    )
    changed_hash = replace(result.packet, content_sha256=_sha("different content"))

    for packet in (changed_text, changed_hash):
        evaluated = evaluate_review_eligibility(
            packet,
            proposal,
            scope,
            CandidateState.ELIGIBLE,
            now=NOW,
        )
        assert evaluated.decision is ContentDecision.STOP
        assert evaluated.reason_codes == (ContentReason.CONTENT_HASH_MISMATCH,)


@pytest.mark.parametrize(
    ("field_name", "reason"),
    [
        ("target_sha256", ContentReason.TARGET_MISMATCH),
        ("context_sha256", ContentReason.CONTEXT_MISMATCH),
    ],
)
def test_changed_target_or_context_invalidates_review_binding(
    field_name: str, reason: ContentReason
) -> None:
    scope, prepared, proposal = _case(action=ActionKind.REPLY)
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None
    current_proposal = replace(proposal, **{field_name: _sha(f"changed-{field_name}")})

    evaluated = evaluate_review_eligibility(
        result.packet,
        current_proposal,
        scope,
        CandidateState.ELIGIBLE,
        now=NOW,
    )

    assert evaluated.decision is ContentDecision.STOP
    assert evaluated.reason_codes == (reason,)


def test_account_and_policy_changes_cannot_reuse_a_packet() -> None:
    scope_a, prepared_a, proposal_a = _case()
    result_a = _prepare(scope_a, prepared_a, proposal_a)
    assert result_a.packet is not None

    scope_b, prepared_b, proposal_b = _case(account_id=ACCOUNT_B, label="account-b")
    account_changed = evaluate_review_eligibility(
        result_a.packet,
        proposal_b,
        scope_b,
        CandidateState.ELIGIBLE,
        now=NOW,
    )
    policy_changed = evaluate_review_eligibility(
        result_a.packet,
        proposal_a,
        replace(scope_a, policy_sha256=_sha("changed-policy")),
        CandidateState.ELIGIBLE,
        now=NOW,
    )

    assert account_changed.decision is ContentDecision.STOP
    assert account_changed.reason_codes == (ContentReason.TARGET_ACCOUNT_MISMATCH,)
    assert policy_changed.decision is ContentDecision.STOP
    assert policy_changed.reason_codes == (ContentReason.POLICY_MISMATCH,)
    assert prepared_b.text not in repr(account_changed)


def test_missing_rights_or_job_fact_declaration_fails_closed() -> None:
    scope, prepared, proposal = _case()
    assert prepared.rights is not None
    missing_rights = replace(
        prepared,
        rights=replace(prepared.rights, right_to_use_declared=False),
    )
    missing_facts = replace(
        prepared,
        rights=replace(
            prepared.rights,
            job_facts_sha256=None,
            job_facts_declaration=JobFactsDeclaration.UNKNOWN,
        ),
    )
    invalid_attribution = replace(
        prepared,
        rights=replace(prepared.rights, source_attribution="invalid \ud800 attribution"),
    )

    rights_result = _prepare(scope, missing_rights, proposal)
    facts_result = _prepare(scope, missing_facts, proposal)
    attribution_result = _prepare(scope, invalid_attribution, proposal)

    assert rights_result.decision is ContentDecision.NOT_ELIGIBLE
    assert rights_result.reason_codes == (ContentReason.RIGHTS_NOT_DECLARED,)
    assert facts_result.decision is ContentDecision.NOT_ELIGIBLE
    assert facts_result.reason_codes == (ContentReason.JOB_FACTS_MISSING,)
    assert attribution_result.decision is ContentDecision.NOT_ELIGIBLE
    assert attribution_result.reason_codes == (ContentReason.RIGHTS_NOT_DECLARED,)


def test_synthetic_fixture_cannot_be_mislabeled_as_live_operator_content() -> None:
    scope, prepared, proposal = _case()
    mislabeled = replace(
        prepared,
        origin=ContentOrigin.SYNTHETIC_FIXTURE,
        rights=_rights(ContentOrigin.SYNTHETIC_FIXTURE),
    )

    result = _prepare(scope, mislabeled, proposal)

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (ContentReason.PROVENANCE_MISMATCH,)


def test_expired_stale_and_not_yet_effective_windows_stop() -> None:
    scope, prepared, proposal = _case()
    initial = _prepare(scope, prepared, proposal)
    assert initial.packet is not None
    expired = evaluate_review_eligibility(
        initial.packet,
        proposal,
        scope,
        CandidateState.ELIGIBLE,
        now=NOW + timedelta(minutes=11),
    )
    stale_content = replace(prepared, prepared_at=NOW - timedelta(hours=25))
    stale = _prepare(scope, stale_content, proposal)

    future_content = replace(prepared, effective_at=NOW + timedelta(minutes=1))
    future_proposal = _proposal(scope, future_content, label="future")
    future = _prepare(scope, future_content, future_proposal)

    assert expired.decision is ContentDecision.STOP
    assert expired.reason_codes == (ContentReason.WINDOW_EXPIRED,)
    assert stale.decision is ContentDecision.STOP
    assert stale.reason_codes == (ContentReason.EVIDENCE_STALE,)
    assert future.decision is ContentDecision.STOP
    assert future.reason_codes == (ContentReason.WINDOW_NOT_ACTIVE,)


@pytest.mark.parametrize(
    ("action", "missing_permission"),
    [
        (ActionKind.POST_TEXT, ApiPermission.THREADS_CONTENT_PUBLISH),
        (ActionKind.REPLY, ApiPermission.THREADS_READ_REPLIES),
        (ActionKind.REPLY, ApiPermission.THREADS_CONTENT_PUBLISH),
    ],
)
def test_missing_read_or_publish_permission_stops(
    action: ActionKind, missing_permission: ApiPermission
) -> None:
    scope, prepared, proposal = _case(action=action)
    scope_without_permission = replace(
        scope,
        capabilities=replace(
            scope.capabilities,
            permissions=scope.capabilities.permissions - {missing_permission},
        ),
    )

    result = _prepare(scope_without_permission, prepared, proposal)

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (ContentReason.API_PERMISSION_MISSING,)
    assert result.packet is None


def test_quota_exhaustion_and_cooldown_are_read_only_ineligibility_inputs() -> None:
    scope, prepared, proposal = _case()
    exhausted_scope = replace(
        scope,
        quota=replace(scope.quota, post_usage=100, post_total=100),
    )

    quota_result = _prepare(exhausted_scope, prepared, proposal)
    cooldown_result = _prepare(
        scope,
        prepared,
        proposal,
        state=CandidateState.COOLDOWN,
    )

    assert quota_result.decision is ContentDecision.NOT_ELIGIBLE
    assert quota_result.reason_codes == (ContentReason.QUOTA_EXHAUSTED,)
    assert cooldown_result.decision is ContentDecision.NOT_ELIGIBLE
    assert cooldown_result.reason_codes == (ContentReason.COOLDOWN,)


def test_ambiguous_prior_action_stops_instead_of_recommending_a_retry() -> None:
    scope, prepared, proposal = _case(action=ActionKind.REPLY)

    result = _prepare(scope, prepared, proposal, state=CandidateState.AMBIGUOUS)

    assert result.decision is ContentDecision.STOP
    assert result.reason_codes == (ContentReason.AMBIGUOUS_PRIOR_ACTION,)


def test_duplicate_or_replayed_packet_revision_stops_without_a_store() -> None:
    scope, prepared, proposal = _case()
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None

    duplicate_prepare = _prepare(
        scope,
        prepared,
        proposal,
        known=frozenset({result.packet.packet_revision_sha256}),
    )
    duplicate_evaluate = evaluate_review_eligibility(
        result.packet,
        proposal,
        scope,
        CandidateState.ELIGIBLE,
        now=NOW,
        known_packet_revisions=frozenset({result.packet.packet_revision_sha256}),
    )

    assert duplicate_prepare.decision is ContentDecision.STOP
    assert duplicate_prepare.reason_codes == (ContentReason.DUPLICATE_PACKET,)
    assert duplicate_evaluate.decision is ContentDecision.STOP
    assert duplicate_evaluate.reason_codes == (ContentReason.DUPLICATE_PACKET,)


def test_unknown_review_state_and_unrecognized_input_shape_stop() -> None:
    scope, prepared, proposal = _case()
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None
    unknown_state = replace(result.packet, review_state="APPROVED")

    evaluated = evaluate_review_eligibility(
        unknown_state,
        proposal,
        scope,
        CandidateState.ELIGIBLE,
        now=NOW,
    )

    assert evaluated.decision is ContentDecision.STOP
    assert evaluated.reason_codes == (ContentReason.REVIEW_STATE_INVALID,)
    extra_input = cast(PreparedContent, {"text": prepared.text})
    assert _prepare(scope, extra_input, proposal).reason_codes == (ContentReason.INVALID_CONTENT,)
    unsupported_fields = {
        "text": prepared.text,
        "origin": prepared.origin,
        "rights": prepared.rights,
        "prepared_at": prepared.prepared_at,
        "effective_at": prepared.effective_at,
        "expires_at": prepared.expires_at,
        "unexpected_field": "reject me",
    }
    with pytest.raises(TypeError):
        inspect.signature(PreparedContent).bind(**unsupported_fields)


def test_review_projection_is_not_an_approved_packet_or_dispatch_authority() -> None:
    scope, prepared, proposal = _case(action=ActionKind.REPLY)
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None
    assert prepared.rights is not None

    projection = approval_input_for_review(result)

    assert type(projection) is ApprovalInput
    assert projection is not None
    assert projection.review_state is PacketReviewState.NEEDS_OPERATOR_REVIEW
    assert projection.account_id == proposal.account_id
    assert projection.target_sha256 == proposal.target_sha256
    assert projection.context_sha256 == proposal.context_sha256
    assert projection.content_sha256 == proposal.content_sha256
    assert projection.packet_revision_sha256 == proposal.packet_revision_sha256
    assert not hasattr(projection, "dispatchable")
    assert not hasattr(projection, "approved")
    assert "APPROVED" not in repr(projection)
    assert approval_input_for_review(replace(result, decision=ContentDecision.STOP)) is None


def test_repr_redacts_ids_fingerprints_text_attribution_and_hashes() -> None:
    scope, prepared, proposal = _case(action=ActionKind.REPLY)
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None
    assert prepared.rights is not None
    assert prepared.rights.source_attribution is not None
    assert prepared.rights.job_facts_sha256 is not None
    projection = approval_input_for_review(result)
    assert projection is not None
    rendered = " ".join(
        repr(value)
        for value in (
            prepared,
            prepared.rights,
            result.packet.proposal_binding,
            result.packet,
            result,
            projection,
        )
    )

    for private_value in (
        str(ACCOUNT_A),
        prepared.text,
        prepared.rights.source_attribution,
        prepared.rights.job_facts_sha256,
        proposal.candidate_fingerprint,
        proposal.target_sha256,
        proposal.context_sha256,
        proposal.content_sha256,
        proposal.packet_revision_sha256,
    ):
        assert private_value not in rendered


def test_provider_protocol_is_only_a_future_seam_and_module_imports_are_pure() -> None:
    request = DraftRequest(
        action=ContentAction.OWN_POST,
        account_id=ACCOUNT_A,
        candidate_fingerprint=_sha("fingerprint"),
        target_sha256=_sha("target"),
        context_sha256=_sha("context"),
        policy_revision_sha256=_sha("policy"),
        job_facts_sha256=_sha("facts"),
    )
    assert request.max_codepoints == 500

    tree = ast.parse(inspect.getsource(api_auto_content))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert not imported.intersection(
        {
            "httpx",
            "io",
            "os",
            "pathlib",
            "playwright",
            "requests",
            "sqlite3",
            "socket",
            "subprocess",
            "urllib",
        }
    )
    assert "provider" not in inspect.signature(prepare_packet).parameters
    assert imported == {
        "__future__",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "re",
        "threads_platform",
        "typing",
        "unicodedata",
        "uuid",
    }


def test_invalid_projection_is_not_exposed_as_approval_input() -> None:
    scope, prepared, proposal = _case()
    result = _prepare(scope, prepared, proposal)
    assert result.packet is not None

    invalid_packet = replace(result.packet, action=cast(ContentAction, "unexpected"))
    invalid_evaluation = replace(result, packet=invalid_packet)

    assert approval_input_for_review(invalid_evaluation) is None
