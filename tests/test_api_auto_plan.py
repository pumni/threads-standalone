from __future__ import annotations

import ast
import hashlib
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from threads_platform.api.contracts import RemoteReply
from threads_platform.standalone import api_auto_plan
from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    ApiPermission,
    ApprovedPacketSummary,
    CampaignScope,
    CandidateSource,
    CandidateState,
    CapabilitySnapshot,
    ContextCompleteness,
    ObservationProvenance,
    ObservedCandidate,
    PlanDecision,
    PlanReason,
    QuotaSnapshot,
    TargetKind,
    VerifiedTargetBinding,
    _context_digest,
    _target_digest,
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
    permissions: frozenset[ApiPermission] = ALL_PERMISSIONS,
    quota: QuotaSnapshot | None = None,
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    max_candidates: int = 20,
    max_actions_per_cycle: int = 1,
) -> CampaignScope:
    return CampaignScope(
        account_id=account_id,
        actor_id=actor_id,
        campaign_id="synthetic-campaign",
        policy_sha256=_sha("policy-v1"),
        capabilities=CapabilitySnapshot(
            account_id=account_id,
            actor_id=actor_id,
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


def _reply_candidate(
    label: str = "one",
    *,
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    fingerprint: str | None = None,
    source: CandidateSource = CandidateSource.INBOUND_REPLY,
    provenance: ObservationProvenance = ObservationProvenance.SYNTHETIC_FIXTURE,
    root_id: str | None = None,
    thread_id: str | None = None,
    reply_id: str | None = None,
    parent_id: str | None = None,
    completeness: ContextCompleteness = ContextCompleteness.COMPLETE,
    context_observed_at: datetime | None = None,
    candidate_observed_at: datetime | None = None,
    state: CandidateState = CandidateState.ELIGIBLE,
    score: int = 10,
    source_order: int = 0,
    actor_verified: bool = True,
    source_verified: bool = True,
    relationship_verified: bool = True,
    root_owned_by_account: bool = True,
    reply_owned_by_operator: bool | None = False,
    public_context: bool = True,
    reply_control_allows_reply: bool = True,
) -> ObservedCandidate:
    root = f"root-{label}" if root_id is None else root_id
    reply = f"reply-{label}" if reply_id is None else reply_id
    parent = root if parent_id is None else parent_id
    binding = VerifiedTargetBinding(
        account_id=account_id,
        actor_id=actor_id,
        source=source,
        provenance=provenance,
        target_kind=TargetKind.INBOUND_REPLY,
        thread_id=root if thread_id is None else thread_id,
        reply_id=reply,
        root_id=root,
        parent_id=parent,
        reply_observation=RemoteReply(
            reply_id=reply,
            text="synthetic reply text",
            timestamp="2026-10-13T03:59:00Z",
            root_post_id=root,
            replied_to_id=parent,
            is_reply_owned_by_me=reply_owned_by_operator,
        ),
        observed_at=context_observed_at or NOW - timedelta(minutes=1),
        completeness=completeness,
        context_evidence_sha256=_sha(f"context-{label}"),
        actor_verified=actor_verified,
        source_verified=source_verified,
        relationship_verified=relationship_verified,
        root_owned_by_account=root_owned_by_account,
        reply_owned_by_operator=reply_owned_by_operator,
        public_context=public_context,
        reply_control_allows_reply=reply_control_allows_reply,
    )
    return ObservedCandidate(
        account_id=account_id,
        source=source,
        provenance=provenance,
        action=ActionKind.REPLY,
        fingerprint=fingerprint or _sha(f"fingerprint-{label}"),
        score=score,
        source_order=source_order,
        state=state,
        observed_at=candidate_observed_at or NOW - timedelta(minutes=1),
        binding=binding,
    )


def _post_candidate(label: str = "post") -> ObservedCandidate:
    binding = VerifiedTargetBinding(
        account_id=ACCOUNT_ID,
        actor_id=ACTOR_ID,
        source=CandidateSource.OWN_ACCOUNT,
        provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
        target_kind=TargetKind.OWN_ACCOUNT,
        thread_id=None,
        reply_id=None,
        root_id=None,
        parent_id=None,
        reply_observation=None,
        observed_at=NOW - timedelta(minutes=1),
        completeness=ContextCompleteness.COMPLETE,
        context_evidence_sha256=_sha("account-context"),
        actor_verified=True,
        source_verified=True,
        relationship_verified=True,
        root_owned_by_account=True,
        reply_owned_by_operator=None,
        public_context=True,
        reply_control_allows_reply=False,
    )
    return ObservedCandidate(
        account_id=ACCOUNT_ID,
        source=CandidateSource.OWN_ACCOUNT,
        provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
        action=ActionKind.POST_TEXT,
        fingerprint=_sha(f"fingerprint-{label}"),
        score=1,
        source_order=0,
        state=CandidateState.ELIGIBLE,
        observed_at=NOW - timedelta(minutes=1),
        binding=binding,
    )


def _packet(
    candidate: ObservedCandidate,
    scope: CampaignScope,
    *,
    target_sha256: str | None = None,
    account_id: UUID = ACCOUNT_ID,
    action: ActionKind | None = None,
    policy_sha256: str | None = None,
    expires_at: datetime | None = None,
) -> ApprovedPacketSummary:
    return ApprovedPacketSummary(
        account_id=account_id,
        candidate_fingerprint=candidate.fingerprint,
        action=action or candidate.action,
        target_sha256=target_sha256 or _target_digest(scope, candidate),
        context_sha256=_context_digest(candidate),
        content_sha256=_sha("synthetic-final-content"),
        packet_revision_sha256=_sha("synthetic-packet-revision"),
        policy_sha256=policy_sha256 or scope.policy_sha256,
        expires_at=expires_at or NOW + timedelta(minutes=10),
    )


def _planned(
    candidate: ObservedCandidate,
    *,
    scope: CampaignScope | None = None,
    packet: ApprovedPacketSummary | None = None,
):
    actual_scope = scope or _scope()
    return plan_cycle(
        actual_scope,
        (candidate,),
        packet or _packet(candidate, actual_scope),
        now=NOW,
    )


def test_empty_cycle_is_observe_only() -> None:
    plan = plan_cycle(_scope(), (), now=NOW)

    assert plan.decision is PlanDecision.OBSERVE_ONLY
    assert plan.items == ()


def test_empty_cycle_with_approved_packet_stops_when_target_was_not_reacquired() -> None:
    candidate = _reply_candidate("restart-empty")
    scope = _scope()
    packet = _packet(candidate, scope)

    plan = plan_cycle(scope, (), packet, now=NOW)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.TARGET_NOT_REACQUIRED,)
    assert plan.items == ()


def test_one_verified_synthetic_reply_produces_review_only_proposal() -> None:
    candidate = _reply_candidate()
    plan = _planned(candidate)

    assert plan.decision is PlanDecision.NEEDS_REVIEW
    assert len(plan.items) == 1
    proposal = plan.items[0].proposal
    assert proposal is not None
    assert proposal.action is ActionKind.REPLY
    assert proposal.provenance is ObservationProvenance.SYNTHETIC_FIXTURE
    assert proposal.decision is PlanDecision.NEEDS_REVIEW
    assert proposal.dispatchable is False
    assert proposal.target_sha256 != candidate.binding.reply_id
    assert ApiPermission.THREADS_READ_REPLIES in proposal.required_permissions
    assert ApiPermission.THREADS_CONTENT_PUBLISH in proposal.required_permissions


def test_twenty_candidates_are_bounded_and_stably_sorted() -> None:
    candidates = tuple(
        _reply_candidate(str(index), score=20 - index, source_order=index) for index in range(20)
    )
    plan = plan_cycle(_scope(), candidates, now=NOW)

    assert len(plan.items) == 20
    assert plan.decision is PlanDecision.OBSERVE_ONLY
    assert [item.score for item in plan.items] == list(range(20, 0, -1))
    assert all(item.reason_codes == (PlanReason.DRAFT_UNAVAILABLE,) for item in plan.items)


def test_candidate_overflow_stops_the_entire_cycle() -> None:
    candidates = tuple(_reply_candidate(str(index)) for index in range(21))
    plan = plan_cycle(_scope(), candidates, now=NOW)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.CANDIDATE_LIMIT_EXCEEDED,)
    assert plan.items == ()


def test_original_post_binds_only_to_verified_account() -> None:
    candidate = _post_candidate()
    plan = _planned(candidate)

    assert plan.decision is PlanDecision.NEEDS_REVIEW
    proposal = plan.items[0].proposal
    assert proposal is not None
    assert proposal.action is ActionKind.POST_TEXT
    assert proposal.provenance is ObservationProvenance.SYNTHETIC_FIXTURE
    assert ApiPermission.THREADS_BASIC in proposal.required_permissions
    assert ApiPermission.THREADS_CONTENT_PUBLISH in proposal.required_permissions


def test_duplicate_fingerprint_is_dedupe_only() -> None:
    first = _reply_candidate("same")
    duplicate = replace(first, score=first.score - 1, source_order=1)
    plan = plan_cycle(_scope(), (first, duplicate), now=NOW)

    assert [item.decision for item in plan.items] == [
        PlanDecision.OBSERVE_ONLY,
        PlanDecision.NOT_ELIGIBLE,
    ]
    assert plan.items[1].reason_codes == (PlanReason.DUPLICATE_FINGERPRINT,)
    assert all(item.proposal is None for item in plan.items)


def test_same_fingerprint_cannot_rebind_to_another_root() -> None:
    first = _reply_candidate("first", fingerprint=_sha("same-fingerprint"))
    second = _reply_candidate(
        "second", fingerprint=first.fingerprint, reply_id="reply-first", root_id="root-other"
    )
    plan = plan_cycle(_scope(), (first, second), now=NOW)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.FINGERPRINT_BINDING_CONFLICT,)


def test_same_reply_id_cannot_be_rebound_to_another_root_or_parent() -> None:
    first = _reply_candidate("first", reply_id="same-reply")
    second = _reply_candidate(
        "second", reply_id="same-reply", root_id="other-root", parent_id="other-parent"
    )
    plan = plan_cycle(_scope(), (first, second), now=NOW)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.REPLY_TARGET_COLLISION,)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"account_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")},
            PlanReason.TARGET_ACCOUNT_MISMATCH,
        ),
        ({"actor_id": "different-actor"}, PlanReason.TARGET_ACTOR_MISMATCH),
        ({"root_id": ""}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"thread_id": "different-root"}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"reply_id": ""}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"parent_id": ""}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"relationship_verified": False}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"root_owned_by_account": False}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"reply_owned_by_operator": True}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"public_context": False}, PlanReason.TARGET_NOT_REACQUIRED),
        ({"reply_control_allows_reply": False}, PlanReason.TARGET_NOT_REACQUIRED),
    ],
)
def test_invalid_account_or_reply_binding_stops(kwargs, reason) -> None:
    candidate = _reply_candidate(**kwargs)
    plan = _planned(candidate)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (reason,)
    assert all(item.proposal is None for item in plan.items)


def test_changed_root_or_parent_invalidates_the_approved_target_digest() -> None:
    original = _reply_candidate("target")
    scope = _scope()
    packet = _packet(original, scope)
    changed_binding = replace(
        original.binding,
        parent_id="changed-parent",
        reply_observation=replace(
            original.binding.reply_observation,
            replied_to_id="changed-parent",
        ),
    )
    changed = replace(original, binding=changed_binding)

    plan = _planned(changed, scope=scope, packet=packet)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.APPROVAL_TARGET_MISMATCH,)


def test_changed_target_still_stops_when_other_context_is_partial() -> None:
    original = _reply_candidate("partial-target")
    scope = _scope()
    packet = _packet(original, scope)
    changed_binding = replace(
        original.binding,
        parent_id="changed-parent",
        completeness=ContextCompleteness.PARTIAL,
        reply_observation=replace(
            original.binding.reply_observation,
            replied_to_id="changed-parent",
        ),
    )
    plan = _planned(replace(original, binding=changed_binding), scope=scope, packet=packet)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.APPROVAL_TARGET_MISMATCH,)


def test_remote_reply_dto_must_match_the_private_binding() -> None:
    candidate = _reply_candidate("dto-mismatch")
    changed_reply = replace(candidate.binding.reply_observation, replied_to_id="different-parent")
    changed_binding = replace(candidate.binding, reply_observation=changed_reply)
    plan = _planned(replace(candidate, binding=changed_binding))

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.TARGET_NOT_REACQUIRED,)


@pytest.mark.parametrize("field_name", ["root_id", "reply_id", "parent_id"])
def test_missing_exact_remote_id_stops(field_name: str) -> None:
    original = _reply_candidate("missing")
    changed_binding = replace(original.binding, **{field_name: None})
    plan = _planned(replace(original, binding=changed_binding))

    assert plan.decision is PlanDecision.STOP
    assert plan.items[0].proposal is None


def test_missing_binding_after_restart_stops_even_when_fingerprint_matches() -> None:
    original = _reply_candidate("restart")
    candidate_without_reacquired_target = replace(original, binding=None)
    plan = _planned(candidate_without_reacquired_target)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.TARGET_NOT_REACQUIRED,)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_observed_at": NOW - timedelta(minutes=16)},
        {"candidate_observed_at": NOW - timedelta(minutes=16)},
    ],
)
def test_stale_context_stops(kwargs) -> None:
    plan = _planned(_reply_candidate(**kwargs))

    assert plan.decision is PlanDecision.STOP
    assert plan.items[0].proposal is None


@pytest.mark.parametrize("completeness", [ContextCompleteness.PARTIAL, ContextCompleteness.UNKNOWN])
def test_partial_context_needs_context_without_proposal(completeness: ContextCompleteness) -> None:
    plan = _planned(_reply_candidate(completeness=completeness))

    assert plan.decision is PlanDecision.NEEDS_CONTEXT
    assert plan.items[0].reason_codes == (PlanReason.CONTEXT_INCOMPLETE,)
    assert plan.items[0].proposal is None


@pytest.mark.parametrize(
    ("permissions", "reason"),
    [
        (ALL_PERMISSIONS - {ApiPermission.THREADS_BASIC}, PlanReason.API_PERMISSION_MISSING),
        (ALL_PERMISSIONS - {ApiPermission.THREADS_READ_REPLIES}, PlanReason.API_PERMISSION_MISSING),
        (
            ALL_PERMISSIONS - {ApiPermission.THREADS_CONTENT_PUBLISH},
            PlanReason.API_PERMISSION_MISSING,
        ),
    ],
)
def test_missing_api_permission_stops_without_proposal(permissions, reason) -> None:
    scope = _scope(permissions=permissions)
    candidate = _reply_candidate()
    plan = plan_cycle(scope, (candidate,), _packet(candidate, scope), now=NOW)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (reason,)
    assert all(item.proposal is None for item in plan.items)


def test_stale_identity_and_quota_snapshots_stop() -> None:
    stale_scope = _scope()
    stale_capability = replace(stale_scope.capabilities, observed_at=NOW - timedelta(minutes=16))
    plan = plan_cycle(
        replace(stale_scope, capabilities=stale_capability),
        (_reply_candidate(),),
        now=NOW,
    )
    assert plan.decision is PlanDecision.STOP

    stale_quota = replace(stale_scope.quota, observed_at=NOW - timedelta(minutes=16))
    plan = plan_cycle(
        replace(stale_scope, quota=stale_quota),
        (_reply_candidate(),),
        now=NOW,
    )
    assert plan.decision is PlanDecision.STOP


def test_exhausted_quota_is_not_eligible() -> None:
    quota = QuotaSnapshot(1, 100, 100, 100, True, NOW - timedelta(minutes=1))
    scope = _scope(quota=quota)
    candidate = _reply_candidate()
    plan = plan_cycle(scope, (candidate,), _packet(candidate, scope), now=NOW)

    assert plan.decision is PlanDecision.NOT_ELIGIBLE
    assert plan.items[0].reason_codes == (PlanReason.QUOTA_EXHAUSTED,)
    assert plan.items[0].proposal is None


def test_zero_action_cap_never_creates_a_proposal() -> None:
    candidate = _reply_candidate()
    scope = replace(_scope(), max_actions_per_cycle=0)
    plan = plan_cycle(scope, (candidate,), _packet(candidate, scope), now=NOW)

    assert plan.decision is PlanDecision.NOT_ELIGIBLE
    assert plan.items[0].reason_codes == (PlanReason.ACTION_CAP_REACHED,)
    assert plan.items[0].proposal is None


def test_cooldown_is_not_eligible_and_ambiguous_is_stop() -> None:
    cooldown = _planned(_reply_candidate(state=CandidateState.COOLDOWN))
    ambiguous = _planned(_reply_candidate(state=CandidateState.AMBIGUOUS))

    assert cooldown.decision is PlanDecision.NOT_ELIGIBLE
    assert cooldown.items[0].reason_codes == (PlanReason.COOLDOWN,)
    assert ambiguous.decision is PlanDecision.STOP
    assert ambiguous.items[0].reason_codes == (PlanReason.AMBIGUOUS_PRIOR_ACTION,)
    assert ambiguous.items[0].proposal is None


def test_expired_or_mismatched_packet_stops() -> None:
    candidate = _reply_candidate()
    scope = _scope()
    expired = _packet(candidate, scope, expires_at=NOW)
    wrong_account = _packet(
        candidate, scope, account_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    )

    for packet in (expired, wrong_account):
        plan = plan_cycle(scope, (candidate,), packet, now=NOW)
        assert plan.decision is PlanDecision.STOP
        assert plan.items[0].proposal is None


def test_reply_proposal_requires_exact_inbound_source() -> None:
    candidate = _reply_candidate(source=CandidateSource.KEYWORD)
    plan = _planned(candidate)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.TARGET_NOT_REACQUIRED,)


def test_unmatched_approved_packet_means_exact_target_was_not_reacquired() -> None:
    candidate = _reply_candidate("other")
    scope = _scope()
    packet = _packet(_reply_candidate("approved"), scope)
    plan = plan_cycle(scope, (candidate,), packet, now=NOW)

    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.TARGET_NOT_REACQUIRED,)


def test_changed_context_stops_even_when_approved_target_is_unchanged() -> None:
    candidate = _reply_candidate("context-changed")
    scope = _scope()
    packet = _packet(candidate, scope)
    changed_binding = replace(
        candidate.binding,
        context_evidence_sha256=_sha("changed-context-evidence"),
    )
    changed_candidate = replace(candidate, binding=changed_binding)

    plan = plan_cycle(scope, (changed_candidate,), packet, now=NOW)

    assert _target_digest(scope, candidate) == _target_digest(scope, changed_candidate)
    assert _context_digest(candidate) != _context_digest(changed_candidate)
    assert plan.decision is PlanDecision.STOP
    assert plan.reason_codes == (PlanReason.APPROVAL_TARGET_MISMATCH,)
    assert all(item.proposal is None for item in plan.items)


def test_reobserving_same_context_at_new_time_preserves_approved_context_digest() -> None:
    candidate = _reply_candidate("context-reobserved")
    scope = _scope()
    packet = _packet(candidate, scope)
    later_observation = NOW - timedelta(seconds=30)
    reobserved_candidate = replace(
        candidate,
        observed_at=later_observation,
        binding=replace(candidate.binding, observed_at=later_observation),
    )

    assert _context_digest(candidate) == _context_digest(reobserved_candidate)
    plan = plan_cycle(scope, (reobserved_candidate,), packet, now=NOW)

    assert plan.decision is PlanDecision.NEEDS_REVIEW
    assert plan.items[0].proposal is not None
    assert plan.items[0].proposal.context_sha256 == packet.context_sha256


def test_missing_content_is_observe_only_and_no_generic_reply_is_created() -> None:
    candidate = _reply_candidate()
    plan = plan_cycle(_scope(), (candidate,), now=NOW)

    assert plan.decision is PlanDecision.OBSERVE_ONLY
    assert plan.items[0].reason_codes == (PlanReason.DRAFT_UNAVAILABLE,)
    assert plan.items[0].proposal is None


def test_all_rendered_representations_redact_ids_fingerprints_and_content_hashes() -> None:
    candidate = _reply_candidate("private")
    scope = _scope()
    packet = _packet(candidate, scope)
    plan = plan_cycle(scope, (candidate,), packet, now=NOW)
    rendered = " ".join(
        (
            repr(scope),
            repr(scope.capabilities),
            repr(candidate),
            repr(candidate.binding),
            repr(packet),
            repr(plan),
            repr(plan.items[0]),
            repr(plan.items[0].proposal),
        )
    )

    for private_value in (
        str(ACCOUNT_ID),
        ACTOR_ID,
        "root-private",
        "reply-private",
        candidate.binding.parent_id,
        candidate.fingerprint,
        packet.content_sha256,
        packet.packet_revision_sha256,
        candidate.binding.context_evidence_sha256,
        packet.context_sha256,
    ):
        assert private_value not in rendered


def test_synthetic_fake_observe_to_plan_never_calls_dispatcher() -> None:
    class SyntheticObservationSource:
        calls = 0

        def observe(self) -> tuple[ObservedCandidate, ...]:
            self.calls += 1
            return (_reply_candidate("fake-source"),)

    class MutationSpy:
        calls = 0

        def dispatch(self, _proposal) -> None:
            self.calls += 1

    source = SyntheticObservationSource()
    dispatcher = MutationSpy()
    candidates = source.observe()
    scope = _scope()
    plan = plan_cycle(
        scope,
        candidates,
        _packet(candidates[0], scope),
        now=NOW,
    )

    assert source.calls == 1
    assert candidates[0].provenance is ObservationProvenance.SYNTHETIC_FIXTURE
    assert plan.decision is PlanDecision.NEEDS_REVIEW
    assert dispatcher.calls == 0
    assert all(item.proposal is None or item.proposal.dispatchable is False for item in plan.items)


def test_planner_imports_no_network_filesystem_browser_or_mutation_runtime() -> None:
    tree = ast.parse(inspect.getsource(api_auto_plan))
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
        {"httpx", "httpx2", "pathlib", "playwright", "requests", "sqlite3", "urllib"}
    )
    assert imported == {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "re",
        "threads_platform",
        "typing",
        "uuid",
    }
