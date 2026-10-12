from __future__ import annotations

import hashlib
import socket
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

import pytest

from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.manual_opportunities import load_manual_opportunities
from threads_platform.standalone.manual_outcomes import ManualOutcomeStore
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueue,
    OpportunityQueueStore,
)
from threads_platform.standalone.recruitment_content_cli import (
    RecruitmentContentCliError,
    run_recruitment_content_cli,
)
from threads_platform.standalone.recruitment_content_planner import (
    RecruitmentContentPlannerError,
    RecruitmentContentPlannerStore,
)
from threads_platform.standalone.recruitment_context_approval import (
    ExactContextApprovalError,
    approve_exact_context_review,
    build_exact_context_review_packet,
    preflight_exact_context_approval,
)
from threads_platform.standalone.recruitment_followup_projection import (
    FollowupProjectionError,
    project_manual_followups,
)
from threads_platform.standalone.recruitment_qualification import rank_opportunities
from threads_platform.standalone.recruitment_reply_assistant import (
    OpportunityReplyEditorialSession,
    build_opportunity_reply_preview,
)
from threads_platform.standalone.recruitment_reply_proposal import DeterministicFakeReplyGenerator

_SYNTHETIC_CONTEXT_A = "SYNTHETIC A: Mình đang tìm việc IT remote tại Hà Nội."
_SYNTHETIC_CONTEXT_B = "SYNTHETIC B: Công ty đang tuyển nhân viên sales tại Đà Nẵng."
_EDITED_DRAFT = "Bạn có thể chia sẻ thêm về công việc IT từ xa mà bạn đang tìm không?"
_NOW_EPOCH = 1_800_000_000


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


def _forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("S13A acceptance tests must not access a network")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)


def _synthetic_accounts(tmp_path: Path):
    root = tmp_path / "private-synthetic-data"
    accounts = LocalAccountStore(root)
    return root, accounts.add("synthetic_a"), accounts.add("synthetic_b")


def _import_one(
    root: Path,
    tmp_path: Path,
    account_id: UUID,
    *,
    name: str,
    context: str,
):
    target = f"https://www.threads.com/@synthetic/post/{name}"
    targets_file = tmp_path / f"{name}-targets.txt"
    targets_file.write_text(f"{target}\n", encoding="utf-8")
    intake = load_manual_opportunities(targets_file)
    summary = OpportunityQueueStore(root).import_candidates(
        account_id,
        intake,
        context=context,
        context_completeness="COMPLETE",
    )
    assert summary.added_count == 1
    return next(
        entry
        for entry in OpportunityQueueStore(root).list(account_id).opportunities
        if entry.target_url == target
    )


async def _kept_s8_session(entry, account_uuid: str) -> OpportunityReplyEditorialSession:
    queue = OpportunityQueue(account_uuid, (entry,))
    qualification = rank_opportunities(queue).items[0]
    assert qualification.state == "ELIGIBLE_FOR_REVIEW"
    preview = await build_opportunity_reply_preview(
        entry,
        qualification,
        DeterministicFakeReplyGenerator(),
        account_uuid=account_uuid,
    )
    return OpportunityReplyEditorialSession.begin(preview).edit(_EDITED_DRAFT).keep_for_session()


@pytest.mark.asyncio
async def test_s6_s7_s8_and_s9a_acceptance_is_exact_fresh_and_account_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_external_network(monkeypatch)
    root, account_a, account_b = _synthetic_accounts(tmp_path)
    queue_store = OpportunityQueueStore(root)
    entry_a = _import_one(
        root,
        tmp_path,
        account_a.id,
        name="s13a-account-a",
        context=_SYNTHETIC_CONTEXT_A,
    )
    _import_one(
        root,
        tmp_path,
        account_b.id,
        name="s13a-account-b",
        context=_SYNTHETIC_CONTEXT_B,
    )
    queue_a = queue_store.list(account_a.id)
    queue_b = queue_store.list(account_b.id)
    assert queue_a.account_uuid == str(account_a.id)
    assert queue_b.account_uuid == str(account_b.id)
    assert queue_a.opportunities[0].target_url != queue_b.opportunities[0].target_url

    ranked_a = rank_opportunities(queue_a)
    ranked_b = rank_opportunities(queue_b)
    assert ranked_a.items[0].state == "ELIGIBLE_FOR_REVIEW"
    assert ranked_a.items[0].classification == "JOB_SEEKER"
    assert ranked_b.items[0].state == "SKIPPED"
    assert ranked_b.items[0].classification == "RECRUITER"

    session = await _kept_s8_session(entry_a, str(account_a.id))
    assert session.disposition == "KEPT_FOR_SESSION"
    packet = build_exact_context_review_packet(
        queue_a,
        session,
        account_uuid=str(account_a.id),
        s4_outcomes={},
    )
    approval = approve_exact_context_review(
        packet,
        confirmation=packet.confirmation_phrase,
        attempt_id=str(uuid4()),
        now_epoch_seconds=_NOW_EPOCH,
    )
    permit = preflight_exact_context_approval(
        queue_a,
        session,
        approval,
        account_uuid=str(account_a.id),
        s4_outcomes={},
        attempt_id=approval.attempt_id,
        now_epoch_seconds=_NOW_EPOCH + 1,
    )
    assert permit.packet_fingerprint == packet.fingerprint

    with pytest.raises(ExactContextApprovalError):
        preflight_exact_context_approval(
            queue_b,
            session,
            approval,
            account_uuid=str(account_b.id),
            s4_outcomes={},
            attempt_id=approval.attempt_id,
            now_epoch_seconds=_NOW_EPOCH + 1,
        )

    wrong_target_entry = replace(
        entry_a,
        target_url="https://www.threads.com/@synthetic/post/s13a-wrong-target",
    )
    with pytest.raises(ExactContextApprovalError):
        preflight_exact_context_approval(
            OpportunityQueue(queue_a.account_uuid, (wrong_target_entry,)),
            session,
            approval,
            account_uuid=str(account_a.id),
            s4_outcomes={},
            attempt_id=approval.attempt_id,
            now_epoch_seconds=_NOW_EPOCH + 1,
        )

    changed_context = f"{entry_a.context} Operator supplied a corrected detail."
    queue_store.select(
        account_a.id,
        entry_a.id,
        context=changed_context,
        update_context=True,
        context_completeness="COMPLETE",
    )
    fresh_queue = queue_store.list(account_a.id)
    with pytest.raises(ExactContextApprovalError):
        preflight_exact_context_approval(
            fresh_queue,
            session,
            approval,
            account_uuid=str(account_a.id),
            s4_outcomes={},
            attempt_id=approval.attempt_id,
            now_epoch_seconds=_NOW_EPOCH + 1,
        )


def test_s4_restart_and_s9b_projection_remain_conservative_and_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_external_network(monkeypatch)
    root, account_a, account_b = _synthetic_accounts(tmp_path)
    entry_unresolved = _import_one(
        root,
        tmp_path,
        account_a.id,
        name="s13a-unresolved",
        context=_SYNTHETIC_CONTEXT_A,
    )
    entry_aborted = _import_one(
        root,
        tmp_path,
        account_a.id,
        name="s13a-aborted",
        context="SYNTHETIC A: Mình đang tìm việc IT part-time.",
    )
    entry_b = _import_one(
        root,
        tmp_path,
        account_b.id,
        name="s13a-isolated",
        context=_SYNTHETIC_CONTEXT_B,
    )
    queue_a = OpportunityQueueStore(root).list(account_a.id)
    store = ManualOutcomeStore(root)
    draft_hash = hashlib.sha256(_EDITED_DRAFT.encode("utf-8")).hexdigest()

    pending = store.begin_handoff(account_a.id, entry_unresolved.target_url, draft_hash)
    reopened = ManualOutcomeStore(root)
    unresolved_record = reopened.read(account_a.id, entry_unresolved.target_url)
    assert unresolved_record is not None
    unresolved = project_manual_followups(
        queue_a,
        {entry_unresolved.target_url: unresolved_record},
    )
    assert unresolved.rows[0].state == "NEEDS_VERIFICATION"

    store.resolve_handoff(
        account_a.id,
        entry_unresolved.target_url,
        draft_hash,
        pending.attempt_id,
        "AMBIGUOUS",
    )
    store = ManualOutcomeStore(root)
    ambiguous_record = store.read(account_a.id, entry_unresolved.target_url)
    assert ambiguous_record is not None

    aborted_pending = store.begin_handoff(account_a.id, entry_aborted.target_url, draft_hash)
    store.resolve_handoff(
        account_a.id,
        entry_aborted.target_url,
        draft_hash,
        aborted_pending.attempt_id,
        "ABORTED_NOT_SENT",
    )
    restarted_store = ManualOutcomeStore(root)
    ambiguous_record = restarted_store.read(account_a.id, entry_unresolved.target_url)
    aborted_record = restarted_store.read(account_a.id, entry_aborted.target_url)
    assert ambiguous_record is not None and aborted_record is not None

    outcomes = {
        entry_unresolved.target_url: ambiguous_record.latest.status,
        entry_aborted.target_url: aborted_record.latest.status,
    }
    ranked = rank_opportunities(queue_a, s4_outcomes=outcomes)
    by_target = {item.target_url: item for item in ranked.items}
    assert by_target[entry_unresolved.target_url].state == "SKIPPED"
    assert "S4_OUTCOME_BLOCKING" in by_target[entry_unresolved.target_url].reason_codes
    assert by_target[entry_aborted.target_url].state == "ELIGIBLE_FOR_REVIEW"

    projection = project_manual_followups(
        queue_a,
        {
            entry_unresolved.target_url: ambiguous_record,
            entry_aborted.target_url: aborted_record,
        },
    )
    rows = {row.opportunity_id: row for row in projection.rows}
    assert rows[entry_unresolved.id].state == "NEEDS_VERIFICATION"
    assert rows[entry_aborted.id].state == "CLOSED"
    assert rows[entry_aborted.id].reason_code == "LOCAL_ATTEMPT_ABORTED_NOT_SENT"
    assert {row.state for row in projection.rows} <= {
        "NO_FOLLOWUP",
        "NEEDS_VERIFICATION",
        "CLOSED",
    }

    queue_b = OpportunityQueueStore(root).list(account_b.id)
    isolated = project_manual_followups(queue_b, {})
    assert len(isolated.rows) == 1
    assert isolated.rows[0].state == "NO_FOLLOWUP"
    with pytest.raises(FollowupProjectionError):
        project_manual_followups(queue_b, {entry_unresolved.target_url: ambiguous_record})
    assert restarted_store.read(account_b.id, entry_b.target_url) is None


def test_s10a_s10b_local_content_review_is_private_and_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_external_network(monkeypatch)
    root, account_a, account_b = _synthetic_accounts(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    planner = RecruitmentContentPlannerStore(root, utc_now=lambda: now)
    private_text = "SYNTHETIC PRIVATE CONTENT: local draft for editorial review only."
    created = planner.create(
        account_a.id,
        private_text,
        now + timedelta(days=1),
        provenance="OPERATOR_AUTHORED",
        rights_asserted=True,
        consent_asserted=True,
    )
    packet = created.packet
    assert packet.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert planner.list(account_b.id).packets == ()

    listing = _TTY()
    run_recruitment_content_cli(
        "content-list",
        account_a.alias,
        data_root_factory=lambda: root,
        stdin=_TTY(),
        stdout=listing,
        stderr=_TTY(),
        environment={},
    )
    assert private_text not in listing.getvalue()

    with pytest.raises(RecruitmentContentPlannerError) as wrong_account:
        planner.get_review_snapshot(account_b.id, packet.packet_id)
    assert wrong_account.value.code == "CONTENT_PLANNER_ENTRY_NOT_FOUND"

    snapshot = planner.get_review_snapshot(account_a.id, packet.packet_id)
    confirmation = f"APPROVE {account_a.id} {packet.packet_id} {snapshot.fingerprint}\n"
    review_output = _TTY()
    review_errors = _TTY()
    result = run_recruitment_content_cli(
        "content-approve",
        account_a.alias,
        packet_id=str(packet.packet_id),
        data_root_factory=lambda: root,
        stdin=_TTY(confirmation),
        stdout=review_output,
        stderr=review_errors,
        environment={},
    )
    assert result == 0
    reviewed = planner.get(account_a.id, packet.packet_id)
    assert reviewed.editorial_status == "READY_FOR_MANUAL_PUBLICATION"
    assert reviewed.remote_publication_status == "NOT_PUBLISHED_BY_PLANNER"
    assert private_text not in review_output.getvalue()
    assert "NOT_PUBLISHED_BY_PLANNER" in review_errors.getvalue()

    stale_packet = planner.create(
        account_a.id,
        "SYNTHETIC SECOND CONTENT: stale review fixture.",
        now + timedelta(days=2),
        provenance="OPERATOR_AUTHORED",
        rights_asserted=True,
        consent_asserted=True,
    ).packet
    stale_snapshot = planner.get_review_snapshot(account_a.id, stale_packet.packet_id)
    planner.review_if_unchanged(
        account_a.id,
        stale_packet.packet_id,
        expected_fingerprint=stale_snapshot.fingerprint,
        expected_editorial_status="DRAFT",
        decision="REJECT",
        note="Synthetic stale snapshot setup.",
    )
    with pytest.raises(RecruitmentContentPlannerError) as stale_review:
        planner.review_if_unchanged(
            account_a.id,
            stale_packet.packet_id,
            expected_fingerprint=stale_snapshot.fingerprint,
            expected_editorial_status="DRAFT",
            decision="APPROVE",
        )
    assert stale_review.value.code == "CONTENT_PLANNER_REVIEW_STALE"

    with pytest.raises(RecruitmentContentCliError) as non_tty:
        run_recruitment_content_cli(
            "content-show",
            account_a.alias,
            packet_id=str(packet.packet_id),
            data_root_factory=lambda: root,
            stdin=StringIO(),
            stdout=StringIO(),
            stderr=StringIO(),
            environment={},
        )
    assert non_tty.value.code == "CONTENT_CLI_PRIVATE_TTY_REQUIRED"
