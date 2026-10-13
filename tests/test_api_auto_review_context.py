from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from threads_platform.api.contracts import RemoteReply
from threads_platform.standalone import api_auto_review_context
from threads_platform.standalone.api_auto_content import (
    ContentDecision,
    ContentOrigin,
    JobFactsDeclaration,
    PreparedContent,
    ReviewContentPacket,
    RightsBasis,
    RightsEvidence,
    prepare_review_packet,
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
from threads_platform.standalone.api_auto_review_context import (
    PrivateReviewDecision,
    PrivateReviewPreview,
    PrivateReviewPreviewResult,
    PrivateReviewReason,
    ReviewDisplaySnapshot,
    build_private_review_preview,
    review_display_context_sha256,
)

NOW = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
ACCOUNT_ID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_ACCOUNT_ID = UUID("87654321-4321-8765-4321-876543218765")
ACTOR_ID = "synthetic-actor-1"
ALL_PERMISSIONS = frozenset(ApiPermission)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(
    *,
    now: datetime = NOW,
    account_id: UUID = ACCOUNT_ID,
    actor_id: str = ACTOR_ID,
    permissions: frozenset[ApiPermission] = ALL_PERMISSIONS,
    reply_usage: int = 1,
    reply_total: int = 100,
) -> CampaignScope:
    return CampaignScope(
        account_id=account_id,
        actor_id=actor_id,
        campaign_id="synthetic-review-context",
        policy_sha256=_sha("synthetic-review-policy"),
        capabilities=CapabilitySnapshot(
            account_id=account_id,
            actor_id=actor_id,
            permissions=permissions,
            verified=True,
            observed_at=now - timedelta(minutes=1),
        ),
        quota=QuotaSnapshot(
            post_usage=1,
            post_total=100,
            reply_usage=reply_usage,
            reply_total=reply_total,
            verified=True,
            observed_at=now - timedelta(minutes=1),
        ),
        allowed_sources=frozenset(CandidateSource),
        allowed_actions=frozenset(ActionKind),
        policy_approved=True,
    )


def _post_candidate(*, now: datetime = NOW, label: str = "post") -> ObservedCandidate:
    observed_at = now - timedelta(minutes=1)
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
        observed_at=observed_at,
        completeness=ContextCompleteness.COMPLETE,
        context_evidence_sha256=_sha(f"post-context-placeholder-{label}"),
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
        fingerprint=_sha(f"post-fingerprint-{label}"),
        score=1,
        source_order=0,
        state=CandidateState.ELIGIBLE,
        observed_at=observed_at,
        binding=binding,
    )


def _reply_candidate(
    *,
    now: datetime = NOW,
    label: str = "reply",
    root_id: str = "synthetic-root-1",
    parent_id: str = "synthetic-parent-1",
    reply_id: str = "synthetic-reply-1",
    reply_text: str = "Synthetic inbound reply asks a question?",
    state: CandidateState = CandidateState.ELIGIBLE,
    completeness: ContextCompleteness = ContextCompleteness.COMPLETE,
) -> ObservedCandidate:
    observed_at = now - timedelta(minutes=1)
    binding = VerifiedTargetBinding(
        account_id=ACCOUNT_ID,
        actor_id=ACTOR_ID,
        source=CandidateSource.INBOUND_REPLY,
        provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
        target_kind=TargetKind.INBOUND_REPLY,
        thread_id=root_id,
        reply_id=reply_id,
        root_id=root_id,
        parent_id=parent_id,
        reply_observation=RemoteReply(
            reply_id=reply_id,
            text=reply_text,
            timestamp="2026-10-13T03:59:00Z",
            root_post_id=root_id,
            replied_to_id=parent_id,
            is_reply_owned_by_me=False,
        ),
        observed_at=observed_at,
        completeness=completeness,
        context_evidence_sha256=_sha(f"reply-context-placeholder-{label}"),
        actor_verified=True,
        source_verified=True,
        relationship_verified=True,
        root_owned_by_account=True,
        reply_owned_by_operator=False,
        public_context=True,
        reply_control_allows_reply=True,
    )
    return ObservedCandidate(
        account_id=ACCOUNT_ID,
        source=CandidateSource.INBOUND_REPLY,
        provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
        action=ActionKind.REPLY,
        fingerprint=_sha(f"reply-fingerprint-{label}"),
        score=10,
        source_order=0,
        state=state,
        observed_at=observed_at,
        binding=binding,
    )


def _display(
    candidate: ObservedCandidate,
    *,
    reply_text: str | None = None,
    root_text: str | None = None,
    parent_text: str | None = None,
) -> ReviewDisplaySnapshot:
    binding = candidate.binding
    assert binding is not None
    if candidate.action is ActionKind.POST_TEXT:
        visible_reply = visible_root = visible_parent = None
    else:
        observation = binding.reply_observation
        assert type(observation) is RemoteReply
        visible_reply = reply_text if reply_text is not None else observation.text
        visible_root = root_text if root_text is not None else "Synthetic root post, full text."
        visible_parent = (
            parent_text if parent_text is not None else "Synthetic parent reply, full text."
        )
    return ReviewDisplaySnapshot(
        account_id=candidate.account_id,
        actor_id=binding.actor_id,
        action=candidate.action,
        source=candidate.source,
        provenance=candidate.provenance,
        candidate_fingerprint=candidate.fingerprint,
        thread_id=binding.thread_id,
        reply_id=binding.reply_id,
        root_id=binding.root_id,
        parent_id=binding.parent_id,
        reply_text=visible_reply,
        root_text=visible_root,
        parent_text=visible_parent,
        observed_at=binding.observed_at,
    )


def _seed_context(
    candidate: ObservedCandidate,
    display: ReviewDisplaySnapshot,
) -> ObservedCandidate:
    binding = candidate.binding
    assert binding is not None
    digest = review_display_context_sha256(display)
    assert len(digest) == 64
    return replace(candidate, binding=replace(binding, context_evidence_sha256=digest))


def _post_scenario(*, now: datetime = NOW, label: str = "post"):
    candidate = _post_candidate(now=now, label=label)
    display = _display(candidate)
    candidate = _seed_context(candidate, display)
    return _scope(now=now), candidate, display


def _reply_scenario(
    *,
    now: datetime = NOW,
    label: str = "reply",
    root_id: str = "synthetic-root-1",
    parent_id: str = "synthetic-parent-1",
    reply_id: str = "synthetic-reply-1",
    reply_text: str = "Synthetic inbound reply asks a question?",
    root_text: str = "Synthetic root post, full text.",
    parent_text: str = "Synthetic parent reply, full text.",
    state: CandidateState = CandidateState.ELIGIBLE,
    completeness: ContextCompleteness = ContextCompleteness.COMPLETE,
):
    candidate = _reply_candidate(
        now=now,
        label=label,
        root_id=root_id,
        parent_id=parent_id,
        reply_id=reply_id,
        reply_text=reply_text,
        state=state,
        completeness=completeness,
    )
    display = _display(
        candidate,
        reply_text=reply_text,
        root_text=root_text,
        parent_text=parent_text,
    )
    candidate = _seed_context(candidate, display)
    return _scope(now=now), candidate, display


def _packet(
    scope: CampaignScope,
    candidate: ObservedCandidate,
    *,
    now: datetime = NOW,
    text: str = "Synthetic final operator content.",
) -> ReviewContentPacket:
    candidates = (candidate,)
    plan = plan_cycle(scope, candidates, None, now=now)
    assert plan.decision is PlanDecision.OBSERVE_ONLY
    intents = tuple(item.review_intent for item in plan.items if item.review_intent is not None)
    assert len(intents) == 1
    intent = intents[0]
    assert type(intent) is ReviewIntent and intent.dispatchable is False
    prepared = PreparedContent(
        text=text,
        origin=ContentOrigin.SYNTHETIC_FIXTURE,
        rights=RightsEvidence(
            source_attribution="synthetic test attribution",
            rights_basis=RightsBasis.SYNTHETIC_FIXTURE,
            right_to_use_declared=True,
            job_facts_sha256=_sha("synthetic job facts"),
            job_facts_declaration=JobFactsDeclaration.SYNTHETIC_FIXTURE,
        ),
        prepared_at=now - timedelta(minutes=2),
        effective_at=now - timedelta(minutes=1),
        expires_at=min(intent.expires_at, now + timedelta(minutes=8)),
    )
    result = prepare_review_packet(intent, prepared, scope, candidates, now=now)
    assert result.decision is ContentDecision.NEEDS_REVIEW
    assert result.reason_codes == ()
    assert type(result.packet) is ReviewContentPacket
    return result.packet


def _build(
    packet: ReviewContentPacket,
    scope: CampaignScope,
    candidates: tuple[ObservedCandidate, ...],
    display: ReviewDisplaySnapshot,
    *,
    now: datetime = NOW,
):
    return build_private_review_preview(packet, scope, candidates, display, now=now)


def _ready_preview(result: PrivateReviewPreviewResult) -> PrivateReviewPreview:
    assert result.decision is PrivateReviewDecision.READY_FOR_PRIVATE_TTY
    assert result.reason_codes == ()
    assert type(result.preview) is PrivateReviewPreview
    return result.preview


def test_own_post_full_synthetic_path_returns_inert_preview_without_target_ids() -> None:
    scope, candidate, display = _post_scenario()
    packet = _packet(scope, candidate)

    result = _build(packet, scope, (candidate,), display)

    preview = _ready_preview(result)
    assert candidate.provenance is ObservationProvenance.SYNTHETIC_FIXTURE
    assert preview.account_id == ACCOUNT_ID
    assert preview.actor_id == ACTOR_ID
    assert preview.action is ActionKind.POST_TEXT
    assert preview.thread_id is None
    assert preview.reply_id is None
    assert preview.root_id is None
    assert preview.parent_id is None
    assert preview.reply_text is None
    assert preview.root_text is None
    assert preview.parent_text is None
    assert preview.final_content_text == packet.prepared_content.text
    assert preview.rights_evidence is packet.prepared_content.rights
    assert preview.packet_revision_sha256 == packet.packet_revision_sha256
    assert preview.display_context_sha256 == candidate.binding.context_evidence_sha256
    assert not hasattr(preview, "dispatchable")
    assert not hasattr(preview, "approved")
    assert not hasattr(preview, "consent")


def test_reply_preview_contains_full_exact_root_parent_and_reply_context() -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)

    preview = _ready_preview(_build(packet, scope, (candidate,), display))

    assert preview.thread_id == display.root_id
    assert preview.reply_id == display.reply_id
    assert preview.root_id == display.root_id
    assert preview.parent_id == display.parent_id
    assert preview.reply_text == display.reply_text
    assert preview.root_text == display.root_text
    assert preview.parent_text == display.parent_text
    assert preview.expires_at == min(packet.binding.expires_at, packet.prepared_content.expires_at)
    for raw_value in (
        str(ACCOUNT_ID),
        ACTOR_ID,
        display.thread_id,
        display.reply_id,
        display.root_id,
        display.parent_id,
        display.reply_text,
        display.root_text,
        display.parent_text,
        packet.prepared_content.text,
        packet.prepared_content.rights.source_attribution,
        packet.packet_revision_sha256,
        preview.preview_sha256,
    ):
        assert raw_value not in repr(preview)
        assert raw_value not in repr(_build(packet, scope, (candidate,), display))


def test_same_context_with_newer_observation_time_keeps_digest_and_preview_hash() -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    first = _ready_preview(_build(packet, scope, (candidate,), display))
    later = NOW + timedelta(minutes=3)
    later_binding = replace(candidate.binding, observed_at=later - timedelta(minutes=1))
    later_candidate = replace(
        candidate,
        observed_at=later_binding.observed_at,
        binding=later_binding,
    )
    later_display = replace(display, observed_at=later_binding.observed_at)
    later_scope = _scope(now=later)

    assert review_display_context_sha256(display) == review_display_context_sha256(later_display)
    result = _build(packet, later_scope, (later_candidate,), later_display, now=later)

    refreshed = _ready_preview(result)
    assert refreshed.preview_sha256 == first.preview_sha256
    assert refreshed.packet_revision_sha256 == first.packet_revision_sha256


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("account_id", OTHER_ACCOUNT_ID),
        ("actor_id", "synthetic-other-actor"),
        ("candidate_fingerprint", _sha("other fingerprint")),
        ("reply_id", "synthetic-other-reply"),
        ("root_id", "synthetic-other-root"),
        ("parent_id", "synthetic-other-parent"),
        ("observed_at", NOW + timedelta(seconds=1)),
    ],
)
def test_changed_display_binding_or_timestamp_stops(field_name: str, value: object) -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    changed = replace(display, **{field_name: value})

    result = _build(packet, scope, (candidate,), changed)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None
    assert all(type(reason) is PrivateReviewReason for reason in result.reason_codes)


@pytest.mark.parametrize(
    "field_name",
    ["reply_text", "root_text", "parent_text"],
)
def test_changed_visible_context_text_stops_even_when_target_ids_match(field_name: str) -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    changed = replace(display, **{field_name: f"Truncated or substituted {field_name}."})

    result = _build(packet, scope, (candidate,), changed)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None
    assert result.reason_codes == (PrivateReviewReason.CONTEXT_MISMATCH,)


def test_changed_context_evidence_cannot_rebind_an_existing_packet() -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    changed_display = replace(display, root_text="Changed synthetic root text.")
    new_evidence = review_display_context_sha256(changed_display)
    assert len(new_evidence) == 64
    changed_candidate = replace(
        candidate,
        binding=replace(candidate.binding, context_evidence_sha256=new_evidence),
    )

    result = _build(packet, scope, (changed_candidate,), changed_display)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("reply_text", None),
        ("root_text", None),
        ("parent_text", None),
        ("reply_text", "  \n "),
        ("root_text", ""),
        ("parent_text", "\t"),
        ("reply_text", "x" * 4097),
        ("root_text", "danger\x1b[31mred"),
        ("parent_text", "danger\u202eoverride"),
        ("reply_text", "invalid\ud800surrogate"),
    ],
)
def test_missing_blank_oversize_or_unsafe_context_never_creates_preview(
    field_name: str,
    value: str | None,
) -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    invalid = replace(display, **{field_name: value})

    result = _build(packet, scope, (candidate,), invalid)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None
    assert review_display_context_sha256(invalid) == ""
    rendered = repr(result)
    assert str(ACCOUNT_ID) not in rendered
    assert ACTOR_ID not in rendered
    assert "Synthetic" not in rendered
    assert "surrogate" not in rendered


def test_reply_observation_text_must_match_display_even_with_matching_display_digest() -> None:
    scope, candidate, display = _reply_scenario()
    altered_display = replace(display, reply_text="Different synthetic visible reply.")
    altered_candidate = replace(
        candidate,
        binding=replace(
            candidate.binding,
            context_evidence_sha256=review_display_context_sha256(altered_display),
        ),
    )
    packet = _packet(scope, altered_candidate)

    result = _build(packet, scope, (altered_candidate,), altered_display)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.reason_codes == (PrivateReviewReason.CONTEXT_MISMATCH,)
    assert result.preview is None


@pytest.mark.parametrize(
    ("root_text", "parent_text"),
    [("One exact synthetic root.", "A different synthetic parent.")],
)
def test_same_root_and_parent_id_requires_identical_visible_text(
    root_text: str,
    parent_text: str,
) -> None:
    scope, candidate, matching_display = _reply_scenario(
        root_id="synthetic-same-root",
        parent_id="synthetic-same-root",
        root_text=root_text,
        parent_text=root_text,
    )
    display = replace(matching_display, parent_text=parent_text)

    result = _build(_packet(scope, candidate), scope, (candidate,), display)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None
    assert review_display_context_sha256(display) == ""


@pytest.mark.parametrize(
    "state",
    [CandidateState.COOLDOWN, CandidateState.CONFIRMED],
)
def test_bounded_batch_keeps_one_eligible_intent_with_normal_neighbors(
    state: CandidateState,
) -> None:
    scope, selected, display = _post_scenario(label=f"batch-{state.value}")
    neighbor = _reply_candidate(label=f"normal-{state.value}", state=state)
    candidates = (selected, neighbor)
    packet = _packet(scope, selected)

    result = _build(packet, scope, candidates, display)

    preview = _ready_preview(result)
    assert preview.candidate_fingerprint == selected.fingerprint


@pytest.mark.parametrize(
    ("scope_change", "candidate_change", "extra"),
    [
        ("permission", "none", "none"),
        ("quota", "none", "none"),
        ("none", "stale", "none"),
        ("none", "cooldown", "none"),
        ("none", "ambiguous", "none"),
        ("none", "none", "duplicate"),
        ("none", "none", "collision"),
        ("none", "none", "needs_context"),
        ("none", "none", "stop"),
    ],
)
def test_revalidation_gates_permissions_quota_freshness_and_unsafe_batches(
    scope_change: str,
    candidate_change: str,
    extra: str,
) -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    current_scope = scope
    current_candidate = candidate
    current_display = display

    if scope_change == "permission":
        current_scope = _scope(
            permissions=frozenset(ALL_PERMISSIONS - {ApiPermission.THREADS_READ_REPLIES})
        )
    elif scope_change == "quota":
        current_scope = _scope(reply_usage=100, reply_total=100)
    if candidate_change == "stale":
        stale_at = NOW - timedelta(hours=1)
        stale_binding = replace(candidate.binding, observed_at=stale_at)
        current_candidate = replace(candidate, observed_at=stale_at, binding=stale_binding)
        current_display = replace(display, observed_at=stale_at)
    elif candidate_change == "cooldown":
        current_candidate = replace(candidate, state=CandidateState.COOLDOWN)
    elif candidate_change == "ambiguous":
        current_candidate = replace(candidate, state=CandidateState.AMBIGUOUS)

    candidates = (current_candidate,)
    if extra == "duplicate":
        candidates = (current_candidate, replace(current_candidate))
    elif extra == "collision":
        collision = _reply_candidate(
            label="reply-collision",
            root_id="synthetic-other-root",
            parent_id="synthetic-other-parent",
            reply_id=display.reply_id or "synthetic-reply-collision",
        )
        candidates = (current_candidate, collision)
    elif extra == "needs_context":
        incomplete = _reply_candidate(
            label="reply-incomplete",
            root_id="synthetic-incomplete-root",
            parent_id="synthetic-incomplete-parent",
            reply_id="synthetic-incomplete-reply",
            completeness=ContextCompleteness.PARTIAL,
        )
        candidates = (current_candidate, incomplete)
    elif extra == "stop":
        unavailable = replace(
            _reply_candidate(
                label="reply-unavailable",
                root_id="synthetic-unavailable-root",
                parent_id="synthetic-unavailable-parent",
                reply_id="synthetic-unavailable-reply",
            ),
            binding=None,
        )
        candidates = (current_candidate, unavailable)

    result = _build(packet, current_scope, candidates, current_display)

    assert result.decision in {PrivateReviewDecision.STOP, PrivateReviewDecision.NOT_ELIGIBLE}
    assert result.preview is None


def test_expired_original_packet_stops_after_new_observation() -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    later = packet.binding.expires_at + timedelta(seconds=1)
    current_scope = _scope(now=later)
    refreshed_binding = replace(candidate.binding, observed_at=later - timedelta(minutes=1))
    current_candidate = replace(
        candidate,
        observed_at=refreshed_binding.observed_at,
        binding=refreshed_binding,
    )
    current_display = replace(display, observed_at=refreshed_binding.observed_at)

    result = _build(packet, current_scope, (current_candidate,), current_display, now=later)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None


@pytest.mark.parametrize("changed", ["packet_revision", "text", "rights"])
def test_changed_packet_revision_text_or_rights_never_produces_preview(changed: str) -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    if changed == "packet_revision":
        packet = replace(packet, packet_revision_sha256=_sha("wrong revision"))
    elif changed == "text":
        packet = replace(
            packet,
            prepared_content=replace(packet.prepared_content, text="Changed synthetic final text."),
        )
    else:
        assert packet.prepared_content.rights is not None
        packet = replace(
            packet,
            prepared_content=replace(
                packet.prepared_content,
                rights=replace(packet.prepared_content.rights, right_to_use_declared=False),
            ),
        )

    result = _build(packet, scope, (candidate,), display)

    assert result.decision is PrivateReviewDecision.STOP
    assert result.preview is None


def test_two_account_isolation_and_provenance_pairing() -> None:
    scope, candidate, display = _post_scenario()
    packet = _packet(scope, candidate)
    other_scope, other_candidate, other_display = _post_scenario(label="other-account")
    other_scope = _scope(account_id=OTHER_ACCOUNT_ID)
    other_binding = replace(
        other_candidate.binding,
        account_id=OTHER_ACCOUNT_ID,
    )
    other_candidate = replace(
        other_candidate,
        account_id=OTHER_ACCOUNT_ID,
        binding=other_binding,
    )
    other_display = replace(other_display, account_id=OTHER_ACCOUNT_ID)
    other_candidate = _seed_context(other_candidate, other_display)
    wrong_account = _build(packet, other_scope, (other_candidate,), other_display)
    wrong_provenance = _build(
        packet,
        scope,
        (candidate,),
        replace(display, provenance=ObservationProvenance.OFFICIAL_API),
    )

    assert wrong_account.decision is PrivateReviewDecision.STOP
    assert wrong_account.preview is None
    assert wrong_provenance.decision is PrivateReviewDecision.STOP
    assert wrong_provenance.preview is None


def test_results_and_preview_repr_are_redacted_and_types_have_no_authority_fields() -> None:
    scope, candidate, display = _reply_scenario()
    packet = _packet(scope, candidate)
    result = _build(packet, scope, (candidate,), display)
    preview = _ready_preview(result)
    rendered = " ".join((repr(display), repr(preview), repr(result)))

    for private_value in (
        str(ACCOUNT_ID),
        ACTOR_ID,
        display.reply_id,
        display.root_id,
        display.parent_id,
        display.reply_text,
        display.root_text,
        display.parent_text,
        packet.prepared_content.text,
        packet.prepared_content.rights.source_attribution,
        candidate.fingerprint,
        packet.binding.target_sha256,
        packet.binding.context_sha256,
        packet.packet_revision_sha256,
        preview.preview_sha256,
    ):
        assert private_value not in rendered
    assert not hasattr(preview, "dispatchable")
    assert not hasattr(preview, "approved")
    assert not hasattr(preview, "consent")
    assert not hasattr(result, "dispatchable")
    assert not hasattr(result, "approved")
    assert not hasattr(result, "consent")


def test_module_imports_and_ast_have_no_io_clock_tty_or_effects() -> None:
    source = Path(api_auto_review_context.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_imports = {
        "__future__",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "re",
        "unicodedata",
        "uuid",
        "threads_platform.api.contracts",
        "threads_platform.standalone.api_auto_content",
        "threads_platform.standalone.api_auto_plan",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert imported <= allowed_imports

    forbidden_calls = {
        "open",
        "input",
        "print",
        "sleep",
        "create_task",
        "Thread",
        "Popen",
        "run",
        "get",
        "post",
        "request",
        "write_text",
        "write_bytes",
    }
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.append(node.func.attr)
    assert not forbidden_calls.intersection(calls)
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in {"now", "utcnow"}
        for node in ast.walk(tree)
    )
