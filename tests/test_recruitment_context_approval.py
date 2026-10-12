from __future__ import annotations

import hashlib
import io
import socket
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest

from threads_platform.standalone.manual_outcomes import (
    ManualOutcomeEvent,
    ManualOutcomeRecord,
    ManualOutcomeStatus,
    ManualOutcomeStore,
)
from threads_platform.standalone.opportunity_queue import (
    ContextCompleteness,
    ContextProvenance,
    OpportunityQueue,
    OpportunityQueueEntry,
    OpportunityState,
)
from threads_platform.standalone.recruitment_context_approval import (
    ExactContextApprovalError,
    ExactContextApprovalV2,
    approve_exact_context_review,
    build_exact_context_review_packet,
    preflight_exact_context_approval,
    render_exact_context_review_packet,
    write_exact_context_review_packet,
)
from threads_platform.standalone.recruitment_qualification import rank_opportunities
from threads_platform.standalone.recruitment_reply_assistant import (
    OpportunityReplyEditorialSession,
    build_opportunity_reply_preview,
)
from threads_platform.standalone.recruitment_reply_proposal import DeterministicFakeReplyGenerator

ACCOUNT_A = "123e4567-e89b-42d3-a456-426614174000"
ACCOUNT_B = "123e4567-e89b-42d3-a456-426614174001"
OPPORTUNITY_A = "223e4567-e89b-42d3-a456-426614174000"
TARGET_A = "https://www.threads.com/@synthetic/post/AbC123"
CONTEXT = "Mình đang tìm việc IT remote. Đây là nội dung tổng hợp do operator cung cấp."
EDITED_DRAFT = "Bạn có thể chia sẻ thêm về công việc IT từ xa mà bạn đang tìm không?"
ATTEMPT_A = "323e4567-e89b-42d3-a456-426614174000"
NOW = 1_800_000_000


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def _entry(
    *,
    account_uuid: str = ACCOUNT_A,
    opportunity_id: str = OPPORTUNITY_A,
    target_url: str = TARGET_A,
    context: str | None = CONTEXT,
    provenance: str = "OPERATOR_SUPPLIED",
    completeness: str = "COMPLETE",
    state: str = "READY",
    updated_at: str = "2026-10-12T00:00:00Z",
) -> OpportunityQueueEntry:
    return OpportunityQueueEntry(
        id=opportunity_id,
        target_url=target_url,
        source="MANUAL_URL",
        context=context,
        context_provenance=cast(ContextProvenance, provenance),
        context_completeness=cast(ContextCompleteness, completeness),
        created_at="2026-10-12T00:00:00Z",
        updated_at=updated_at,
        state=cast(OpportunityState, state),
        skip_reason=None,
    )


async def _chain(
    *,
    account_uuid: str = ACCOUNT_A,
    entry: OpportunityQueueEntry | None = None,
    context: str = CONTEXT,
    draft: str = EDITED_DRAFT,
    keep: bool = False,
) -> tuple[OpportunityQueue, OpportunityReplyEditorialSession]:
    selected = entry or _entry(account_uuid=account_uuid, context=context)
    queue = OpportunityQueue(account_uuid, (selected,))
    qualification = rank_opportunities(queue, limit=20).items[0]
    assert qualification.state == "ELIGIBLE_FOR_REVIEW"
    preview = await build_opportunity_reply_preview(
        selected,
        qualification,
        DeterministicFakeReplyGenerator(),
        account_uuid=account_uuid,
    )
    session = OpportunityReplyEditorialSession.begin(preview).edit(draft)
    if keep:
        session = session.keep_for_session()
    return queue, session


async def _packet(
    *,
    account_uuid: str = ACCOUNT_A,
    entry: OpportunityQueueEntry | None = None,
    context: str = CONTEXT,
    draft: str = EDITED_DRAFT,
    keep: bool = False,
    outcomes: dict[str, ManualOutcomeStatus] | None = None,
):
    queue, session = await _chain(
        account_uuid=account_uuid,
        entry=entry,
        context=context,
        draft=draft,
        keep=keep,
    )
    packet = build_exact_context_review_packet(
        queue,
        session,
        account_uuid=account_uuid,
        s4_outcomes={} if outcomes is None else outcomes,
    )
    assert packet.context_sha256 == hashlib.sha256(packet.context.encode("utf-8")).hexdigest()
    return queue, session, packet


def _approve(packet, *, attempt_id: str = ATTEMPT_A, now: int = NOW) -> ExactContextApprovalV2:
    return approve_exact_context_review(
        packet,
        confirmation=packet.confirmation_phrase,
        attempt_id=attempt_id,
        now_epoch_seconds=now,
    )


@pytest.mark.asyncio
async def test_exact_confirmation_and_preflight_are_separate_from_s8_keep() -> None:
    queue, session, packet = await _packet(keep=True)
    assert session.disposition == "KEPT_FOR_SESSION"
    rendered = render_exact_context_review_packet(packet)
    assert CONTEXT in rendered
    assert EDITED_DRAFT in rendered
    assert packet.confirmation_phrase in rendered
    assert "operator assertion only" in rendered
    assert "coverage remain unverified" in rendered
    approval = _approve(packet)

    permit = preflight_exact_context_approval(
        queue,
        session,
        approval,
        account_uuid=ACCOUNT_A,
        s4_outcomes={},
        attempt_id=ATTEMPT_A,
        now_epoch_seconds=NOW + 1,
    )
    assert permit.packet_fingerprint == packet.fingerprint
    assert permit.approval_fingerprint == approval.fingerprint
    assert permit.attempt_id == ATTEMPT_A
    assert "handoff=<not performed>" in repr(permit)


@pytest.mark.asyncio
async def test_confirmation_is_exact_and_approval_evidence_is_immutable() -> None:
    _, _, packet = await _packet()
    with pytest.raises(ExactContextApprovalError) as error:
        approve_exact_context_review(
            packet,
            confirmation="wrong confirmation PRIVATE_SENTINEL",
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW,
        )
    assert error.value.code == "S9A_CONFIRMATION_REQUIRED"
    assert "PRIVATE_SENTINEL" not in str(error.value)

    approval = _approve(packet)
    altered = replace(approval, confirmation_sha256="0" * 64)
    with pytest.raises(ExactContextApprovalError) as altered_error:
        preflight_exact_context_approval(
            await _queue_and_session(),
            (await _queue_and_session())[1],
            altered,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert altered_error.value.code == "S9A_APPROVAL_INVALID"


async def _queue_and_session() -> tuple[OpportunityQueue, OpportunityReplyEditorialSession]:
    queue, session, _ = await _packet()
    return queue, session


@pytest.mark.asyncio
async def test_wrong_account_target_source_and_provenance_fail_closed() -> None:
    queue, session, _ = await _packet()
    with pytest.raises(ExactContextApprovalError) as wrong_account:
        build_exact_context_review_packet(
            queue,
            session,
            account_uuid=ACCOUNT_B,
            s4_outcomes={},
        )
    assert wrong_account.value.code == "S9A_ACCOUNT_MISMATCH"

    changed_target = replace(_entry(), target_url="https://www.threads.com/@synthetic/post/Other1")
    target_queue = OpportunityQueue(ACCOUNT_A, (changed_target,))
    with pytest.raises(ExactContextApprovalError) as wrong_target:
        build_exact_context_review_packet(
            target_queue,
            session,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
        )
    assert wrong_target.value.code == "S9A_TARGET_INVALID"

    queue, session, _ = await _packet()
    changed_source = replace(_entry(), source=cast(Literal["MANUAL_URL"], "OTHER"))
    changed_source_queue = OpportunityQueue(ACCOUNT_A, (changed_source,))
    with pytest.raises(ExactContextApprovalError) as wrong_source:
        build_exact_context_review_packet(
            changed_source_queue,
            session,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
        )
    assert wrong_source.value.code == "S9A_QUALIFICATION_INVALID"

    changed_provenance = replace(_entry(), context_provenance=cast(ContextProvenance, "NONE"))
    changed_provenance_queue = OpportunityQueue(ACCOUNT_A, (changed_provenance,))
    with pytest.raises(ExactContextApprovalError) as wrong_provenance:
        build_exact_context_review_packet(
            changed_provenance_queue,
            session,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
        )
    assert wrong_provenance.value.code == "S9A_QUALIFICATION_INVALID"
    assert queue.account_uuid == ACCOUNT_A


@pytest.mark.asyncio
async def test_full_context_suffix_after_s8_excerpt_is_committed() -> None:
    long_context = ("Mình đang tìm việc IT remote. " + ("Synthetic context. " * 40)).rstrip()
    queue, session, packet = await _packet(context=long_context)
    assert len(session.candidate.evidence_excerpt) == 500
    approval = _approve(packet)

    changed_entry = replace(queue.opportunities[0], context=long_context[:-1] + "!")
    changed_queue = OpportunityQueue(ACCOUNT_A, (changed_entry,))
    with pytest.raises(ExactContextApprovalError) as stale_candidate:
        build_exact_context_review_packet(
            changed_queue,
            session,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
        )
    assert stale_candidate.value.code == "S9A_CONTEXT_INVALID"

    changed_queue, changed_session = await _chain(entry=changed_entry)
    assert changed_session.candidate.evidence_excerpt == session.candidate.evidence_excerpt
    with pytest.raises(ExactContextApprovalError) as stale:
        preflight_exact_context_approval(
            changed_queue,
            changed_session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert stale.value.code == "S9A_APPROVAL_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_context",
    [
        "Mình đang tìm việc IT  remote. Đây là nội dung tổng hợp do operator cung cấp.",
        "mình đang tìm việc IT remote. Đây là nội dung tổng hợp do operator cung cấp.",
        "Mình đang tìm việc IT remote. Đây là nội dung tổng hợp do operator cung cấp.",
    ],
)
async def test_whitespace_case_and_unicode_differences_change_context_commitment(
    changed_context: str,
) -> None:
    _, _, original = await _packet()
    queue, session, changed = await _packet(context=changed_context)
    assert original.context_sha256 != changed.context_sha256
    assert original.fingerprint != changed.fingerprint
    approval = _approve(original)
    with pytest.raises(ExactContextApprovalError) as error:
        preflight_exact_context_approval(
            queue,
            session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert error.value.code == "S9A_APPROVAL_INVALID"


@pytest.mark.asyncio
async def test_final_draft_queue_state_and_context_provenance_changes_invalidate() -> None:
    queue, session, packet = await _packet()
    approval = _approve(packet)
    changed_queue, changed_session = await _chain(
        draft="Bạn có thể cho biết công việc phù hợp không?"
    )
    with pytest.raises(ExactContextApprovalError) as draft_error:
        preflight_exact_context_approval(
            changed_queue,
            changed_session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert draft_error.value.code == "S9A_APPROVAL_INVALID"

    updated_entry = replace(queue.opportunities[0], updated_at="2026-10-12T00:01:00Z")
    updated_queue = OpportunityQueue(ACCOUNT_A, (updated_entry,))
    with pytest.raises(ExactContextApprovalError) as state_error:
        preflight_exact_context_approval(
            updated_queue,
            session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert state_error.value.code == "S9A_APPROVAL_INVALID"


@pytest.mark.asyncio
async def test_missing_or_partial_context_and_non_ready_queue_are_rejected() -> None:
    _, session, _ = await _packet()
    for entry in (
        _entry(context=None, provenance="NONE", completeness="UNKNOWN", state="NEEDS_CONTEXT"),
        _entry(completeness="PARTIAL", state="NEEDS_CONTEXT"),
        _entry(state="SKIPPED"),
    ):
        queue = OpportunityQueue(ACCOUNT_A, (entry,))
        with pytest.raises(ExactContextApprovalError):
            build_exact_context_review_packet(
                queue,
                session,
                account_uuid=ACCOUNT_A,
                s4_outcomes={},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["HANDOFF_UNRESOLVED", "AMBIGUOUS", "OPERATOR_REPORTED_SENT_UNVERIFIED"],
)
async def test_blocking_s4_statuses_are_rejected(status: str) -> None:
    with pytest.raises(ExactContextApprovalError) as error:
        await _packet(outcomes={TARGET_A: cast(ManualOutcomeStatus, status)})
    assert error.value.code == "S9A_S4_BLOCKED"


@pytest.mark.asyncio
async def test_s4_status_change_abort_and_unresolved_replay_are_not_success() -> None:
    queue, session, packet = await _packet()
    approval = _approve(packet)
    aborted: dict[str, ManualOutcomeStatus] = {TARGET_A: "ABORTED_NOT_SENT"}
    with pytest.raises(ExactContextApprovalError) as changed_status:
        preflight_exact_context_approval(
            queue,
            session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes=aborted,
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert changed_status.value.code == "S9A_APPROVAL_INVALID"

    with pytest.raises(ExactContextApprovalError) as unresolved:
        preflight_exact_context_approval(
            queue,
            session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes={TARGET_A: "HANDOFF_UNRESOLVED"},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert unresolved.value.code == "S9A_S4_BLOCKED"


@pytest.mark.asyncio
async def test_full_s4_record_history_invalidates_replay_after_same_abort_status() -> None:
    queue, session, _ = await _packet()
    first_event = ManualOutcomeEvent(
        event_id="423e4567-e89b-42d3-a456-426614174000",
        attempt_id="423e4567-e89b-42d3-a456-426614174000",
        draft_sha256="a" * 64,
        recorded_at="2026-10-12T00:00:00Z",
        status="ABORTED_NOT_SENT",
        source="OPERATOR",
        verification="UNVERIFIED",
    )
    record_before = ManualOutcomeRecord(
        account_uuid=ACCOUNT_A,
        target_fingerprint=ManualOutcomeStore.target_fingerprint(UUID(ACCOUNT_A), TARGET_A),
        events=(first_event,),
    )
    packet = build_exact_context_review_packet(
        queue,
        session,
        account_uuid=ACCOUNT_A,
        s4_outcomes={TARGET_A: record_before},
    )
    assert packet.s4_status == "ABORTED_NOT_SENT"
    assert (
        "operator declaration, not evidence of remote success"
        in render_exact_context_review_packet(packet)
    )
    approval = _approve(packet)

    handoff_event = ManualOutcomeEvent(
        event_id="523e4567-e89b-42d3-a456-426614174000",
        attempt_id="523e4567-e89b-42d3-a456-426614174000",
        draft_sha256="b" * 64,
        recorded_at="2026-10-12T00:01:00Z",
        status="HANDOFF_UNRESOLVED",
        source="SOFTWARE_HANDOFF",
        verification="UNVERIFIED",
    )
    abort_event = ManualOutcomeEvent(
        event_id="623e4567-e89b-42d3-a456-426614174000",
        attempt_id=handoff_event.attempt_id,
        draft_sha256=handoff_event.draft_sha256,
        recorded_at="2026-10-12T00:02:00Z",
        status="ABORTED_NOT_SENT",
        source="OPERATOR",
        verification="UNVERIFIED",
    )
    record_after = replace(record_before, events=(first_event, handoff_event, abort_event))
    with pytest.raises(ExactContextApprovalError) as replay:
        preflight_exact_context_approval(
            queue,
            session,
            approval,
            account_uuid=ACCOUNT_A,
            s4_outcomes={TARGET_A: record_after},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert replay.value.code == "S9A_APPROVAL_INVALID"


@pytest.mark.asyncio
async def test_two_account_isolation_attempt_binding_and_expiry() -> None:
    queue_a, session_a, packet_a = await _packet()
    approval_a = _approve(packet_a)
    queue_b, session_b, _ = await _packet(account_uuid=ACCOUNT_B)
    with pytest.raises(ExactContextApprovalError) as account_error:
        preflight_exact_context_approval(
            queue_b,
            session_b,
            approval_a,
            account_uuid=ACCOUNT_B,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 1,
        )
    assert account_error.value.code == "S9A_APPROVAL_INVALID"

    wrong_attempt = "323e4567-e89b-42d3-a456-426614174002"
    with pytest.raises(ExactContextApprovalError) as attempt_error:
        preflight_exact_context_approval(
            queue_a,
            session_a,
            approval_a,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=wrong_attempt,
            now_epoch_seconds=NOW + 1,
        )
    assert attempt_error.value.code == "S9A_ATTEMPT_MISMATCH"

    with pytest.raises(ExactContextApprovalError) as expired:
        preflight_exact_context_approval(
            queue_a,
            session_a,
            approval_a,
            account_uuid=ACCOUNT_A,
            s4_outcomes={},
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW + 300,
        )
    assert expired.value.code == "S9A_APPROVAL_EXPIRED"


@pytest.mark.asyncio
async def test_repr_and_errors_redact_context_draft_and_target() -> None:
    secret_context = "PRIVATE_CONTEXT_SENTINEL " + CONTEXT
    secret_draft = "PRIVATE_DRAFT_SENTINEL " + EDITED_DRAFT
    queue, session, packet = await _packet(context=secret_context, draft=secret_draft)
    approval = _approve(packet)
    for rendered in (repr(packet), repr(approval), repr(session)):
        assert "PRIVATE_CONTEXT_SENTINEL" not in rendered
        assert "PRIVATE_DRAFT_SENTINEL" not in rendered
        assert TARGET_A not in rendered

    with pytest.raises(ExactContextApprovalError) as error:
        approve_exact_context_review(
            packet,
            confirmation=secret_context,
            attempt_id=ATTEMPT_A,
            now_epoch_seconds=NOW,
        )
    assert "PRIVATE_CONTEXT_SENTINEL" not in str(error.value)
    assert "PRIVATE_DRAFT_SENTINEL" not in str(error.value)
    assert "PRIVATE_CONTEXT_SENTINEL" in render_exact_context_review_packet(packet)
    assert "PRIVATE_DRAFT_SENTINEL" in render_exact_context_review_packet(packet)
    assert queue.account_uuid == ACCOUNT_A


@pytest.mark.asyncio
async def test_sensitive_packet_writer_requires_private_tty() -> None:
    _, _, packet = await _packet()
    stdin = io.StringIO()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.raises(ExactContextApprovalError) as error:
        write_exact_context_review_packet(
            packet,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
    assert error.value.code == "S9A_PRIVATE_TERMINAL_REQUIRED"
    assert stdout.getvalue() == ""

    private_stdin = _TtyStringIO()
    private_stdout = _TtyStringIO()
    private_stderr = _TtyStringIO()
    write_exact_context_review_packet(
        packet,
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment={},
    )
    assert CONTEXT in private_stdout.getvalue()
    assert EDITED_DRAFT in private_stdout.getvalue()

    with pytest.raises(ExactContextApprovalError) as ci_error:
        write_exact_context_review_packet(
            packet,
            stdin=private_stdin,
            stdout=_TtyStringIO(),
            stderr=private_stderr,
            environment={"CI": "true"},
        )
    assert ci_error.value.code == "S9A_PRIVATE_TERMINAL_REQUIRED"


@pytest.mark.asyncio
async def test_multiline_context_cannot_spoof_review_packet_labels() -> None:
    spoof_text = (
        "Mình đang tìm việc IT remote.\n"
        "S4 outcome: OPERATOR_REPORTED_SENT_UNVERIFIED\n"
        "EXACT FINAL EDITED DRAFT:\n"
        "Synthetic injected line."
    )
    _, _, packet = await _packet(context=spoof_text)
    rendered = render_exact_context_review_packet(packet)
    assert "| S4 outcome: OPERATOR_REPORTED_SENT_UNVERIFIED" in rendered
    assert "| EXACT FINAL EDITED DRAFT:" in rendered
    assert "\nS4 outcome: OPERATOR_REPORTED_SENT_UNVERIFIED\n" not in rendered


@pytest.mark.asyncio
async def test_contract_has_no_file_network_browser_or_s4_handoff_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threads_platform.browser import playwright_engine

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected external side effect")

    monkeypatch.setattr(Path, "open", unexpected)
    monkeypatch.setattr(Path, "write_text", unexpected)
    monkeypatch.setattr(Path, "write_bytes", unexpected)
    monkeypatch.setattr(socket, "socket", unexpected)
    monkeypatch.setattr(ManualOutcomeStore, "begin_handoff", unexpected)
    monkeypatch.setattr(playwright_engine, "async_playwright", unexpected)

    queue, session, packet = await _packet()
    approval = _approve(packet)
    permit = preflight_exact_context_approval(
        queue,
        session,
        approval,
        account_uuid=ACCOUNT_A,
        s4_outcomes={},
        attempt_id=ATTEMPT_A,
        now_epoch_seconds=NOW + 1,
    )
    assert permit.packet_fingerprint == packet.fingerprint
