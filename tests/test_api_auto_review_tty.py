from __future__ import annotations

import ast
import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import UUID

import pytest

from threads_platform.api.contracts import RemoteReply
from threads_platform.standalone import api_auto_review_tty
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
    ReviewDisplaySnapshot,
    build_private_review_preview,
    review_display_context_sha256,
)
from threads_platform.standalone.api_auto_review_tty import (
    PrivateTtyReviewDecision,
    PrivateTtyReviewReason,
    PrivateTtyReviewReceipt,
    PrivateTtyReviewResult,
    run_private_review_tty,
)

NOW = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
ACCOUNT_ID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_ACCOUNT_ID = UUID("87654321-4321-8765-4321-876543218765")
ACTOR_ID = "synthetic-actor-1"
ALL_PERMISSIONS = frozenset(ApiPermission)
_UNSET = object()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(
    *,
    now: datetime = NOW,
    permissions: frozenset[ApiPermission] = ALL_PERMISSIONS,
    reply_usage: int = 1,
    reply_total: int = 100,
) -> CampaignScope:
    return CampaignScope(
        account_id=ACCOUNT_ID,
        actor_id=ACTOR_ID,
        campaign_id="synthetic-tty-review",
        policy_sha256=_sha("synthetic-policy-v1"),
        capabilities=CapabilitySnapshot(
            account_id=ACCOUNT_ID,
            actor_id=ACTOR_ID,
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


def _post_candidate(*, now: datetime = NOW) -> ObservedCandidate:
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
        context_evidence_sha256=_sha("post-context-placeholder"),
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
        fingerprint=_sha("post-fingerprint"),
        score=1,
        source_order=0,
        state=CandidateState.ELIGIBLE,
        observed_at=observed_at,
        binding=binding,
    )


def _reply_candidate(
    *,
    now: datetime = NOW,
    root_id: str = "synthetic-root-1",
    parent_id: str = "synthetic-parent-1",
    reply_id: str = "synthetic-reply-1",
    reply_text: str = "Synthetic inbound reply asks a question?",
    state: CandidateState = CandidateState.ELIGIBLE,
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
        completeness=ContextCompleteness.COMPLETE,
        context_evidence_sha256=_sha("reply-context-placeholder"),
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
        fingerprint=_sha("reply-fingerprint"),
        score=10,
        source_order=0,
        state=state,
        observed_at=observed_at,
        binding=binding,
    )


def _display(
    candidate: ObservedCandidate,
    *,
    root_text: str = "Synthetic root post, full text.",
    parent_text: str = "Synthetic parent reply, full text.",
) -> ReviewDisplaySnapshot:
    binding = candidate.binding
    assert binding is not None
    if candidate.action is ActionKind.POST_TEXT:
        reply_text = visible_root = visible_parent = None
    else:
        observation = binding.reply_observation
        assert type(observation) is RemoteReply
        reply_text = observation.text
        visible_root = root_text
        visible_parent = parent_text
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
        reply_text=reply_text,
        root_text=visible_root,
        parent_text=visible_parent,
        observed_at=binding.observed_at,
    )


def _scenario(
    action: ActionKind = ActionKind.REPLY,
    *,
    text: str = "Synthetic final operator content.",
    attribution: str = "synthetic source attribution",
    root_text: str = "Synthetic root post, full text.",
    parent_text: str = "Synthetic parent reply, full text.",
    now: datetime = NOW,
) -> tuple[
    CampaignScope,
    ObservedCandidate,
    ReviewDisplaySnapshot,
    ReviewContentPacket,
    PrivateReviewPreview,
]:
    candidate = (
        _post_candidate(now=now) if action is ActionKind.POST_TEXT else _reply_candidate(now=now)
    )
    display = _display(candidate, root_text=root_text, parent_text=parent_text)
    binding = candidate.binding
    assert binding is not None
    candidate = replace(
        candidate,
        binding=replace(
            binding,
            context_evidence_sha256=review_display_context_sha256(display),
        ),
    )
    scope = _scope(now=now)
    plan = plan_cycle(scope, (candidate,), None, now=now)
    assert plan.decision is PlanDecision.OBSERVE_ONLY
    intents = tuple(item.review_intent for item in plan.items if item.review_intent is not None)
    assert len(intents) == 1
    intent = intents[0]
    assert type(intent) is ReviewIntent and intent.dispatchable is False
    prepared = PreparedContent(
        text=text,
        origin=ContentOrigin.SYNTHETIC_FIXTURE,
        rights=RightsEvidence(
            source_attribution=attribution,
            rights_basis=RightsBasis.SYNTHETIC_FIXTURE,
            right_to_use_declared=True,
            job_facts_sha256=_sha("synthetic job facts"),
            job_facts_declaration=JobFactsDeclaration.SYNTHETIC_FIXTURE,
        ),
        prepared_at=now - timedelta(minutes=2),
        effective_at=now - timedelta(minutes=1),
        expires_at=min(intent.expires_at, now + timedelta(minutes=8)),
    )
    evaluation = prepare_review_packet(intent, prepared, scope, (candidate,), now=now)
    assert evaluation.decision is ContentDecision.NEEDS_REVIEW
    assert evaluation.reason_codes == ()
    assert type(evaluation.packet) is ReviewContentPacket
    packet = evaluation.packet
    built = build_private_review_preview(packet, scope, (candidate,), display, now=now)
    assert built.decision is PrivateReviewDecision.READY_FOR_PRIVATE_TTY
    assert built.reason_codes == ()
    assert type(built.preview) is PrivateReviewPreview
    return scope, candidate, display, packet, built.preview


class _Terminal(StringIO):
    def __init__(self, initial: str = "", *, tty: bool = True) -> None:
        super().__init__(initial)
        self.tty = tty
        self.read_sizes: list[int] = []

    def isatty(self) -> bool:
        return self.tty

    def readline(self, size: int = -1) -> str:
        self.read_sizes.append(size)
        return super().readline(size)


def _challenge(preview: PrivateReviewPreview) -> str:
    return f"CONFIRM {preview.preview_sha256[:16]} {preview.packet_revision_sha256[:16]}"


def _run(
    scenario: tuple[
        CampaignScope,
        ObservedCandidate,
        ReviewDisplaySnapshot,
        ReviewContentPacket,
        PrivateReviewPreview,
    ],
    entered: str,
    *,
    times: tuple[datetime, ...] = (NOW, NOW),
    environment: object = _UNSET,
    stdin: object | None = None,
    stdout: object | None = None,
    stderr: object | None = None,
    now_utc: object = _UNSET,
) -> tuple[
    PrivateTtyReviewResult, _Terminal | StringIO, _Terminal | StringIO, _Terminal | StringIO
]:
    scope, candidate, display, packet, preview = scenario
    input_stream = stdin if stdin is not None else _Terminal(entered)
    output_stream = stdout if stdout is not None else _Terminal()
    error_stream = stderr if stderr is not None else _Terminal()
    moments = iter(times)
    clock = (lambda: next(moments)) if now_utc is _UNSET else now_utc
    result = run_private_review_tty(
        preview,
        packet,
        scope,
        (candidate,),
        display,
        stdin=input_stream,  # type: ignore[arg-type]
        stdout=output_stream,  # type: ignore[arg-type]
        stderr=error_stream,  # type: ignore[arg-type]
        environment={} if environment is _UNSET else environment,  # type: ignore[arg-type]
        now_utc=clock,  # type: ignore[arg-type]
    )
    return result, input_stream, output_stream, error_stream  # type: ignore[return-value]


@pytest.mark.parametrize("action", [ActionKind.POST_TEXT, ActionKind.REPLY])
def test_exact_synthetic_preview_is_shown_on_private_stderr_before_challenge(
    action: ActionKind,
) -> None:
    scenario = _scenario(action, text="Xin chào\nNội dung cuối cùng.")
    preview = scenario[4]
    output = _Terminal()

    class ChallengeInput(_Terminal):
        def readline(self, size: int = -1) -> str:
            assert size == 128
            shown = output.getvalue()
            assert "LOCAL PRIVATE REVIEW / NO DISPATCH\n" in shown
            assert "No approval is persisted and no API call occurs.\n" in shown
            assert (
                f"Confirmation challenge (not an approval token): {_challenge(preview)}\n> "
                in shown
            )
            return super().readline(size)

    input_stream = ChallengeInput(f"{_challenge(preview)}\n")
    result, _, stdout, stderr = _run(
        scenario,
        "",
        stdin=input_stream,
        stderr=output,
    )

    shown_lines = stderr.getvalue().splitlines()
    expected_lines = [
        "LOCAL PRIVATE REVIEW / NO DISPATCH",
        f"Account UUID: {json.dumps(str(ACCOUNT_ID))}",
        f"API actor identity: {json.dumps(ACTOR_ID)}",
        f"Action: {'OWN_POST' if action is ActionKind.POST_TEXT else 'EXACT_TARGET_REPLY'}",
        f"Source: {'OWN_ACCOUNT' if action is ActionKind.POST_TEXT else 'INBOUND_REPLY'}",
        "Observation provenance: SYNTHETIC_FIXTURE",
        f"Final normalized operator content (nfc-lf-v1): {json.dumps('Xin chào\nNội dung cuối cùng.', ensure_ascii=False)}",
        "Content origin: SYNTHETIC_FIXTURE",
        "Rights/use evidence:",
        "Rights basis: SYNTHETIC_FIXTURE",
        "Right to use declared: YES",
        f"Source attribution: {json.dumps('synthetic source attribution')}",
        "Job facts declaration: SYNTHETIC_FIXTURE",
        f"Job-facts digest label: {json.dumps(_sha('synthetic job facts'))}",
        f"Packet expires at UTC: {preview.packet_expires_at.isoformat()}",
        f"Content expires at UTC: {preview.content_expires_at.isoformat()}",
        f"Review expires at UTC: {preview.expires_at.isoformat()}",
        f"packet_revision_sha256: {preview.packet_revision_sha256}",
        f"preview_sha256: {preview.preview_sha256}",
        "No approval is persisted and no API call occurs.",
        f"Confirmation challenge (not an approval token): {_challenge(preview)}",
        "> ",
    ]
    if action is ActionKind.POST_TEXT:
        expected_lines.extend(
            [
                "Target: the selected account's own post",
                "Thread ID: NONE",
                "Root ID: NONE",
                "Parent ID: NONE",
                "Reply ID: NONE",
            ]
        )
    else:
        expected_lines.extend(
            [
                "Exact reply target and visible context:",
                f"Thread ID: {json.dumps('synthetic-root-1')}",
                f"Root ID: {json.dumps('synthetic-root-1')}",
                f"Root text (complete): {json.dumps('Synthetic root post, full text.')}",
                f"Parent ID: {json.dumps('synthetic-parent-1')}",
                f"Parent text (complete): {json.dumps('Synthetic parent reply, full text.')}",
                f"Reply ID: {json.dumps('synthetic-reply-1')}",
                f"Reply text (complete): {json.dumps('Synthetic inbound reply asks a question?')}",
            ]
        )
    for line in expected_lines:
        assert line in shown_lines
    assert stdout.getvalue() == ""
    assert "\x1b" not in stderr.getvalue()
    assert input_stream.read_sizes == [128]
    assert result.decision is PrivateTtyReviewDecision.CONFIRMED_FOR_REVIEW_ONLY
    assert result.reason_codes == ()
    assert type(result.receipt) is PrivateTtyReviewReceipt
    assert result.receipt.packet_revision_sha256 == preview.packet_revision_sha256
    assert result.receipt.preview_sha256 == preview.preview_sha256
    assert result.receipt.account_id == ACCOUNT_ID
    assert result.receipt.action is action
    assert result.receipt.confirmed_at == NOW
    assert not hasattr(result.receipt, "approval_id")
    assert not hasattr(result.receipt, "approved")
    assert not hasattr(result.receipt, "dispatchable")


def test_exact_context_reply_is_complete_untruncated_and_newer_snapshot_can_revalidate() -> None:
    root_text = "Root context: " + ("synthetic " * 350)
    parent_text = "Parent context: " + ("full synthetic context " * 160)
    scenario = _scenario(root_text=root_text, parent_text=parent_text)
    scope, candidate, display, packet, preview = scenario
    later = NOW + timedelta(minutes=2)
    input_stream = _Terminal(f"{_challenge(preview)}\n")
    stdout = _Terminal()
    stderr = _Terminal()
    calls: list[datetime] = []

    def clock() -> datetime:
        value = (NOW, later)[len(calls)]
        calls.append(value)
        return value

    result = run_private_review_tty(
        preview,
        packet,
        scope,
        (candidate,),
        display,
        stdin=input_stream,
        stdout=stdout,
        stderr=stderr,
        environment={},
        now_utc=clock,
    )

    assert result.decision is PrivateTtyReviewDecision.CONFIRMED_FOR_REVIEW_ONLY
    assert len(calls) == 2
    assert json.dumps(root_text, ensure_ascii=False) in stderr.getvalue()
    assert json.dumps(parent_text, ensure_ascii=False) in stderr.getvalue()
    assert "..." not in stderr.getvalue()
    assert stdout.getvalue() == ""


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("preview_sha256", _sha("changed preview")),
        ("packet_revision_sha256", _sha("changed revision")),
        ("account_id", OTHER_ACCOUNT_ID),
        ("actor_id", "synthetic-other-actor"),
        ("final_content_text", "changed reviewed text"),
        ("thread_id", "synthetic-other-root"),
        ("root_id", "synthetic-other-root"),
        ("parent_id", "synthetic-other-parent"),
        ("reply_id", "synthetic-other-reply"),
        ("reply_text", "changed inbound context"),
        ("root_text", "changed root context"),
        ("parent_text", "changed parent context"),
    ],
)
def test_any_changed_preview_binding_fails_before_prompt(
    field_name: str, replacement: object
) -> None:
    scenario = _scenario()
    scope, candidate, display, packet, preview = scenario
    changed = replace(preview, **{field_name: replacement})
    input_stream = _Terminal(f"{_challenge(preview)}\n")
    stdout = _Terminal()
    stderr = _Terminal()
    result = run_private_review_tty(
        changed,
        packet,
        scope,
        (candidate,),
        display,
        stdin=input_stream,
        stdout=stdout,
        stderr=stderr,
        environment={},
        now_utc=lambda: NOW,
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.PREVIEW_MISMATCH,)
    assert result.receipt is None
    assert input_stream.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_changed_display_context_account_target_or_observation_stops_before_output() -> None:
    scope, candidate, display, packet, preview = _scenario()
    changed_displays = (
        replace(display, account_id=OTHER_ACCOUNT_ID),
        replace(display, actor_id="synthetic-other-actor"),
        replace(display, root_id="synthetic-other-root"),
        replace(display, parent_text="changed parent context"),
        replace(display, observed_at=display.observed_at + timedelta(seconds=1)),
    )
    for changed in changed_displays:
        stdin = _Terminal(f"{_challenge(preview)}\n")
        stdout = _Terminal()
        stderr = _Terminal()
        result = run_private_review_tty(
            preview,
            packet,
            scope,
            (candidate,),
            changed,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
            now_utc=lambda: NOW,
        )
        assert result.decision is PrivateTtyReviewDecision.STOP
        assert result.receipt is None
        assert stdin.read_sizes == []
        assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.mark.parametrize(
    "entered",
    [
        "\n",
        "",
        "APPLY\n",
        "CONFIRM 0000000000000000 0000000000000000\n",
        "CONFIRM ABCDEF0123456789 0000000000000000\n",
        "CONFIRM 0000000000000000 0000000000000000 extra\n",
        "X" * 128,
        "CONFIRM 0000000000000000 0000000000000000 \n",
    ],
)
def test_empty_eof_wrong_extra_or_overlong_confirmation_never_returns_receipt(
    entered: str,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    result, stdin, stdout, stderr = _run(scenario, entered, times=(NOW,))
    assert result.decision is PrivateTtyReviewDecision.CANCELLED
    assert result.receipt is None
    assert stdin.read_sizes == [128]
    assert stdout.getvalue() == ""
    assert "LOCAL PRIVATE REVIEW / NO DISPATCH" in stderr.getvalue()


@pytest.mark.parametrize(
    "environment",
    [
        {"CI": "true"},
        {"GITHUB_ACTIONS": "true"},
        {"TF_BUILD": "1"},
        {"BUILDKITE": "1"},
        {"TEAMCITY_VERSION": "2026"},
        {"JENKINS_URL": "https://ci.invalid"},
    ],
)
def test_ci_environment_is_refused_even_when_every_stream_is_a_tty(
    environment: dict[str, str],
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    result, stdin, stdout, stderr = _run(scenario, "anything\n", environment=environment)
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.CI_FORBIDDEN,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.mark.parametrize("non_tty", ["stdin", "stdout", "stderr"])
def test_each_non_tty_stream_is_refused_before_sensitive_output(non_tty: str) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    stdin = _Terminal("unused\n", tty=non_tty != "stdin")
    stdout = _Terminal(tty=non_tty != "stdout")
    stderr = _Terminal(tty=non_tty != "stderr")
    result, _, _, _ = _run(
        scenario,
        "",
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.PRIVATE_TTY_REQUIRED,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_missing_streams_are_refused_before_sensitive_output() -> None:
    scope, candidate, display, packet, preview = _scenario(ActionKind.POST_TEXT)
    stdout = _Terminal()
    stderr = _Terminal()
    result = run_private_review_tty(
        preview,
        packet,
        scope,
        (candidate,),
        display,
        stdin=None,  # type: ignore[arg-type]
        stdout=stdout,
        stderr=stderr,
        environment={},
        now_utc=lambda: NOW,
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.INVALID_INPUT,)
    assert result.receipt is None
    assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("environment", "clock", "expected_reason"),
    [
        (None, lambda: NOW, PrivateTtyReviewReason.INVALID_INPUT),
        ({"CI": None}, lambda: NOW, PrivateTtyReviewReason.INVALID_INPUT),
        ({}, lambda: datetime(2026, 10, 13, 4), PrivateTtyReviewReason.CLOCK_INVALID),
        ({}, lambda: "not a datetime", PrivateTtyReviewReason.CLOCK_INVALID),
        (
            {},
            lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
            PrivateTtyReviewReason.CLOCK_INVALID,
        ),
    ],
)
def test_malformed_environment_or_clock_fails_closed(
    environment: object,
    clock: Callable[[], datetime],
    expected_reason: PrivateTtyReviewReason,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    result, stdin, stdout, stderr = _run(
        scenario,
        "unused\n",
        environment=environment,
        now_utc=clock,
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (expected_reason,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""
    assert "private detail" not in repr(result)


def test_missing_clock_fails_before_output() -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    result, stdin, stdout, stderr = _run(
        scenario,
        "unused\n",
        now_utc=None,
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.INVALID_INPUT,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_initial_and_post_input_expiry_stop_without_receipt() -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    _, _, _, _, preview = scenario
    challenge_line = f"{_challenge(preview)}\n"

    initial_result, initial_in, initial_out, initial_err = _run(
        scenario,
        challenge_line,
        times=(preview.expires_at, NOW),
    )
    assert initial_result.decision is PrivateTtyReviewDecision.STOP
    assert initial_result.reason_codes == (PrivateTtyReviewReason.EXPIRED,)
    assert initial_result.receipt is None
    assert initial_in.read_sizes == []
    assert initial_out.getvalue() == initial_err.getvalue() == ""

    post_result, post_in, post_out, post_err = _run(
        scenario,
        challenge_line,
        times=(NOW, preview.expires_at),
    )
    assert post_result.decision is PrivateTtyReviewDecision.STOP
    assert post_result.reason_codes == (PrivateTtyReviewReason.EXPIRED,)
    assert post_result.receipt is None
    assert post_in.read_sizes == [128]
    assert post_out.getvalue() == ""
    assert "LOCAL PRIVATE REVIEW / NO DISPATCH" in post_err.getvalue()


def test_post_input_clock_failure_returns_no_receipt_or_exception_detail() -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    preview = scenario[4]
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls == 1:
            return NOW
        raise RuntimeError("synthetic private clock detail")

    result, _, stdout, stderr = _run(
        scenario,
        f"{_challenge(preview)}\n",
        now_utc=clock,
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.CLOCK_INVALID,)
    assert result.receipt is None
    assert calls == 2
    assert stdout.getvalue() == ""
    assert "synthetic private clock detail" not in stderr.getvalue()
    assert "LOCAL PRIVATE REVIEW / NO DISPATCH" in stderr.getvalue()


def test_revalidation_runs_before_and_after_input_and_never_claims_live_reacquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    scope, candidate, display, packet, preview = scenario
    real_builder = build_private_review_preview
    calls: list[datetime] = []

    def tracking_builder(*args: object, **kwargs: object) -> PrivateReviewPreviewResult:
        calls.append(kwargs["now"])  # type: ignore[arg-type]
        return real_builder(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api_auto_review_tty, "build_private_review_preview", tracking_builder)
    result, _, stdout, stderr = _run(scenario, f"{_challenge(preview)}\n")
    assert result.decision is PrivateTtyReviewDecision.CONFIRMED_FOR_REVIEW_ONLY
    assert calls == [NOW, NOW]
    assert "no API call occurs" in stderr.getvalue()
    assert stdout.getvalue() == ""
    assert (
        scope is scenario[0]
        and candidate is scenario[1]
        and display is scenario[2]
        and packet is scenario[3]
    )


@pytest.mark.parametrize(
    "candidate_state", [CandidateState.COOLDOWN, CandidateState.PENDING, CandidateState.AMBIGUOUS]
)
def test_changed_candidate_state_or_permission_quota_fails_before_prompt(
    candidate_state: CandidateState,
) -> None:
    scope, candidate, display, packet, preview = _scenario()
    changed_candidate = replace(candidate, state=candidate_state)
    scenarios = [
        (scope, changed_candidate, display),
        (
            replace(
                scope,
                capabilities=replace(
                    scope.capabilities,
                    permissions=frozenset({ApiPermission.THREADS_BASIC}),
                ),
            ),
            candidate,
            display,
        ),
        (
            replace(
                scope,
                quota=replace(scope.quota, reply_usage=100, reply_total=100),
            ),
            candidate,
            display,
        ),
    ]
    for changed_scope, changed, changed_display in scenarios:
        stdin = _Terminal(f"{_challenge(preview)}\n")
        stdout = _Terminal()
        stderr = _Terminal()
        result = run_private_review_tty(
            preview,
            packet,
            changed_scope,
            (changed,),
            changed_display,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
            now_utc=lambda: NOW,
        )
        assert result.decision is PrivateTtyReviewDecision.STOP
        assert result.receipt is None
        assert stdin.read_sizes == []
        assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("field_name", "unsafe_value"),
    [
        ("final_content_text", "escape\x1b[31mred"),
        ("final_content_text", "bidi\u202eoverride"),
        ("final_content_text", "surrogate\ud800"),
        ("final_content_text", "line\u2028separator"),
        ("final_content_text", "paragraph\u2029separator"),
    ],
)
def test_unsafe_final_content_never_reaches_terminal(
    field_name: str,
    unsafe_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    _, _, _, _, preview = scenario
    unsafe_preview = replace(preview, **{field_name: unsafe_value})
    monkeypatch.setattr(
        api_auto_review_tty,
        "build_private_review_preview",
        lambda *args, **kwargs: PrivateReviewPreviewResult(
            PrivateReviewDecision.READY_FOR_PRIVATE_TTY,
            (),
            unsafe_preview,
        ),
    )
    unsafe_scenario = (*scenario[:4], unsafe_preview)
    result, stdin, stdout, stderr = _run(
        unsafe_scenario,
        f"{_challenge(unsafe_preview)}\n",
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.mark.parametrize(
    "unsafe_value",
    ["attrib\u202eution", "attrib\u2028ution", "attrib\u2029ution"],
)
def test_unsafe_rights_attribution_never_reaches_terminal(
    unsafe_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    _, _, _, _, preview = scenario
    assert preview.rights_evidence is not None
    unsafe_rights = replace(preview.rights_evidence, source_attribution=unsafe_value)
    unsafe_preview = replace(preview, rights_evidence=unsafe_rights)
    monkeypatch.setattr(
        api_auto_review_tty,
        "build_private_review_preview",
        lambda *args, **kwargs: PrivateReviewPreviewResult(
            PrivateReviewDecision.READY_FOR_PRIVATE_TTY,
            (),
            unsafe_preview,
        ),
    )
    unsafe_scenario = (*scenario[:4], unsafe_preview)
    result, stdin, stdout, stderr = _run(
        unsafe_scenario,
        f"{_challenge(unsafe_preview)}\n",
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_rights_metadata_cannot_inject_a_fake_review_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    _, _, _, _, preview = scenario
    assert preview.rights_evidence is not None
    injected = "Synthetic source\nAction: EXACT_TARGET_REPLY\nNo approval is persisted and no API call occurs."
    changed_preview = replace(
        preview,
        rights_evidence=replace(preview.rights_evidence, source_attribution=injected),
    )
    monkeypatch.setattr(
        api_auto_review_tty,
        "build_private_review_preview",
        lambda *args, **kwargs: PrivateReviewPreviewResult(
            PrivateReviewDecision.READY_FOR_PRIVATE_TTY,
            (),
            changed_preview,
        ),
    )
    changed_scenario = (*scenario[:4], changed_preview)
    result, _, stdout, stderr = _run(
        changed_scenario,
        f"{_challenge(changed_preview)}\n",
    )
    lines = stderr.getvalue().splitlines()
    assert result.decision is PrivateTtyReviewDecision.CONFIRMED_FOR_REVIEW_ONLY
    assert f"Source attribution: {json.dumps(injected)}" in lines
    assert "Action: OWN_POST" in lines
    assert "Action: EXACT_TARGET_REPLY" not in lines
    assert stdout.getvalue() == ""


@pytest.mark.parametrize("unsafe_value", ["attrib\x1bution", "attrib\ud800ution"])
def test_ansi_escape_and_surrogate_rights_attribution_never_reaches_terminal(
    unsafe_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    _, _, _, _, preview = scenario
    assert preview.rights_evidence is not None
    unsafe_preview = replace(
        preview,
        rights_evidence=replace(preview.rights_evidence, source_attribution=unsafe_value),
    )
    monkeypatch.setattr(
        api_auto_review_tty,
        "build_private_review_preview",
        lambda *args, **kwargs: PrivateReviewPreviewResult(
            PrivateReviewDecision.READY_FOR_PRIVATE_TTY,
            (),
            unsafe_preview,
        ),
    )
    unsafe_scenario = (*scenario[:4], unsafe_preview)
    result, stdin, stdout, stderr = _run(
        unsafe_scenario,
        f"{_challenge(unsafe_preview)}\n",
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_content_contract_allowed_zl_zp_are_rejected_by_tty_before_output() -> None:
    for unsafe in ("line\u2028separator", "paragraph\u2029separator", "bidi\u202eoverride"):
        scope, candidate, display, packet, preview = _scenario(
            ActionKind.POST_TEXT,
            text=unsafe,
        )
        result, stdin, stdout, stderr = _run(
            (scope, candidate, display, packet, preview),
            f"{_challenge(preview)}\n",
        )
        assert result.decision is PrivateTtyReviewDecision.STOP
        assert result.reason_codes == (PrivateTtyReviewReason.INVALID_VISIBLE_TEXT,)
        assert result.receipt is None
        assert stdin.read_sizes == []
        assert stdout.getvalue() == stderr.getvalue() == ""


def test_terminal_write_flush_and_read_failures_return_fixed_safe_reasons() -> None:
    scenario = _scenario(ActionKind.POST_TEXT)

    class BrokenWrite(_Terminal):
        def write(self, value: str) -> int:
            raise OSError("synthetic secret detail")

    class BrokenFlush(_Terminal):
        def flush(self) -> None:
            raise OSError("synthetic secret detail")

    class BrokenRead(_Terminal):
        def readline(self, size: int = -1) -> str:
            raise OSError("synthetic secret detail")

    class InterruptedRead(_Terminal):
        def readline(self, size: int = -1) -> str:
            raise KeyboardInterrupt

    for stream, reason in (
        (BrokenWrite(), PrivateTtyReviewReason.IO_ERROR),
        (BrokenFlush(), PrivateTtyReviewReason.IO_ERROR),
        (BrokenRead(), PrivateTtyReviewReason.IO_ERROR),
        (InterruptedRead(), PrivateTtyReviewReason.INTERRUPTED),
    ):
        result, _, stdout, stderr = (
            _run(
                scenario,
                "unused\n",
                stderr=stream,
            )
            if isinstance(stream, (BrokenWrite, BrokenFlush))
            else _run(
                scenario,
                "",
                stdin=stream,
            )
        )
        assert result.decision is PrivateTtyReviewDecision.STOP
        assert result.reason_codes == (reason,)
        assert result.receipt is None
        assert stdout.getvalue() == ""
        assert "synthetic secret detail" not in repr(result)
        assert "synthetic secret detail" not in stderr.getvalue()


def test_output_size_limit_stops_without_truncation_or_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    _, _, _, _, preview = scenario
    assert preview.rights_evidence is not None
    oversized = replace(
        preview,
        rights_evidence=replace(
            preview.rights_evidence,
            source_attribution="x" * (64 * 1024),
        ),
    )
    monkeypatch.setattr(
        api_auto_review_tty,
        "build_private_review_preview",
        lambda *args, **kwargs: PrivateReviewPreviewResult(
            PrivateReviewDecision.READY_FOR_PRIVATE_TTY,
            (),
            oversized,
        ),
    )
    oversized_scenario = (*scenario[:4], oversized)
    result, stdin, stdout, stderr = _run(
        oversized_scenario,
        f"{_challenge(oversized)}\n",
    )
    assert result.decision is PrivateTtyReviewDecision.STOP
    assert result.reason_codes == (PrivateTtyReviewReason.OUTPUT_LIMIT_EXCEEDED,)
    assert result.receipt is None
    assert stdin.read_sizes == []
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_receipt_and_results_are_immutable_redacted_and_have_no_authority_fields() -> None:
    scenario = _scenario(ActionKind.POST_TEXT)
    preview = scenario[4]
    result, _, _, _ = _run(scenario, f"{_challenge(preview)}\n")
    receipt = result.receipt
    assert type(receipt) is PrivateTtyReviewReceipt
    assert str(ACCOUNT_ID) not in repr(receipt)
    assert preview.preview_sha256 not in repr(receipt)
    assert preview.packet_revision_sha256 not in repr(receipt)
    assert preview.preview_sha256 not in repr(result)
    assert {field.name for field in fields(receipt)} == {
        "packet_revision_sha256",
        "preview_sha256",
        "account_id",
        "action",
        "confirmed_at",
    }
    assert not any(
        "approval" in field.name or "dispatch" in field.name for field in fields(receipt)
    )
    with pytest.raises(FrozenInstanceError):
        receipt.action = ActionKind.REPLY  # type: ignore[misc]
    assert "approved" not in repr(result).lower()


def test_module_has_no_runtime_import_or_call_for_api_store_credentials_or_dispatch() -> None:
    tree = ast.parse(inspect.getsource(api_auto_review_tty))
    allowed_prefixes = (
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "json",
        "re",
        "unicodedata",
        "typing",
        "uuid",
        "threads_platform.standalone.api_auto_content",
        "threads_platform.standalone.api_auto_plan",
        "threads_platform.standalone.api_auto_review_context",
    )
    imported: list[str] = []
    called_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.append(node.func.attr)
    assert all(
        any(name == prefix or name.startswith(prefix + ".") for prefix in allowed_prefixes)
        for name in imported
    )
    forbidden = {
        "open",
        "print",
        "requests",
        "httpx",
        "Client",
        "LocalOperationStore",
        "dispatch",
        "write_text",
        "read_text",
        "mkdir",
        "unlink",
        "resolve",
        "getenv",
        "create_task",
    }
    assert not (set(called_names) & forbidden)
    assert not any(isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree))
