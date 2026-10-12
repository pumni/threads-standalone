from __future__ import annotations

import io
import socket
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import threads_platform.standalone.__main__ as cli
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.manual_opportunities import (
    build_manual_draft_review,
    load_manual_opportunities,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeStore
from threads_platform.standalone.opportunity_queue import (
    ContextCompleteness,
    OpportunityQueue,
    OpportunityQueueEntry,
    OpportunityQueueStore,
)
from threads_platform.standalone.recruitment_qualification import (
    OpportunityQualification,
    RecruitmentQualificationError,
    rank_opportunities,
)


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def _entry(
    post_id: str,
    context: str | None,
    *,
    completeness: ContextCompleteness | None = None,
    state: str | None = None,
    target_url: str | None = None,
) -> OpportunityQueueEntry:
    context_completeness: ContextCompleteness = (
        completeness
        if completeness is not None
        else "COMPLETE"
        if context is not None
        else "UNKNOWN"
    )
    opportunity_state = (
        state
        if state is not None
        else "READY"
        if context is not None and context_completeness == "COMPLETE"
        else "NEEDS_CONTEXT"
    )
    return OpportunityQueueEntry(
        id=str(uuid4()),
        target_url=target_url or f"https://www.threads.com/@synthetic/post/{post_id}",
        source="MANUAL_URL",
        context=context,
        context_provenance="OPERATOR_SUPPLIED" if context is not None else "NONE",
        context_completeness=context_completeness,
        created_at="2026-10-12T00:00:00Z",
        updated_at="2026-10-12T00:00:00Z",
        state=opportunity_state,  # type: ignore[arg-type]
        skip_reason="synthetic operator skip" if opportunity_state == "SKIPPED" else None,
    )


def _rank(
    entries: tuple[OpportunityQueueEntry, ...],
    *,
    account_id: UUID | None = None,
    limit: int = 10,
    outcomes: dict[str, str] | None = None,
) -> tuple[OpportunityQualification, ...]:
    queue = OpportunityQueue(str(account_id or uuid4()), entries)
    return rank_opportunities(queue, limit=limit, s4_outcomes=outcomes).items


def _write_targets(path: Path, target: str) -> Path:
    path.write_text(target + "\n", encoding="utf-8", newline="")
    return path


def _add_queue_item(
    root: Path,
    account: LocalAccount,
    post_id: str,
    context: str,
) -> OpportunityQueueEntry:
    target = f"https://www.threads.com/@synthetic/post/{post_id}"
    targets_file = _write_targets(root.parent / f"{post_id}.txt", target)
    intake = load_manual_opportunities(targets_file)
    OpportunityQueueStore(root).import_candidates(
        account.id,
        intake,
        context=context,
        context_completeness="COMPLETE",
    )
    queue = OpportunityQueueStore(root).list(account.id)
    return next(entry for entry in queue.opportunities if entry.target_url == target)


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    arguments: list[str],
    *,
    tty: bool = True,
) -> tuple[int, str, str]:
    stream_type = _TtyStringIO if tty else io.StringIO
    stdin = stream_type()
    stdout = stream_type()
    stderr = stream_type()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return cli.main(arguments), stdout.getvalue(), stderr.getvalue()


def _entry_by_position(
    ranked: tuple[OpportunityQualification, ...], position: int
) -> OpportunityQualification:
    return next(item for item in ranked if item.input_position == position)


def test_vietnamese_job_seeker_recruiter_student_part_time_it_and_remote_signals() -> None:
    seeker = _entry(
        "s7_student_remote",
        "Mình là sinh viên năm 3 đang tìm việc part-time ngành IT, ưu tiên remote. Có công ty nào phù hợp không?",
    )
    recruiter = _entry(
        "s7_recruiter_sales",
        "Công ty đang tuyển nhân viên sales tại Hà Nội. Mời ứng tuyển!",
    )

    seeker_result, recruiter_result = _rank((seeker, recruiter))

    assert seeker_result.classification == "JOB_SEEKER"
    assert seeker_result.state == "ELIGIBLE_FOR_REVIEW"
    assert seeker_result.job_family == "IT"
    assert seeker_result.location_signal == "REMOTE"
    assert seeker_result.location_relevance == "UNASSESSED_NO_ACCOUNT_PREFERENCE"
    assert seeker_result.score == 71
    assert "STUDENT_SIGNAL" in seeker_result.reason_codes
    assert "PART_TIME_SIGNAL" in seeker_result.reason_codes
    assert "QUESTION_PRESENT" in seeker_result.reason_codes
    assert recruiter_result.classification == "RECRUITER"
    assert recruiter_result.state == "SKIPPED"
    assert recruiter_result.job_family == "SALES"
    assert recruiter_result.location_signal == "PLACE_SPECIFIED"
    assert "RECRUITMENT_JOB_OFFER_INTENT" in recruiter_result.reason_codes
    assert "RECRUITER_NOT_JOB_SEEKER_TARGET" in recruiter_result.reason_codes


def test_it_remote_case_and_student_status_alone_do_not_create_job_seeking_intent() -> None:
    explicit_seeker = _entry(
        "s7_it_remote",
        "Tôi đang tìm việc IT remote, ưu tiên Python developer.",
    )
    status_only = _entry(
        "s7_student_only",
        "Mình là sinh viên, đang học IT và thích làm việc remote.",
    )
    explicit_progressive = _entry("s7_progressive", "Đang tìm việc IT remote.")

    ranked = _rank((explicit_seeker, status_only, explicit_progressive))
    by_position = {item.input_position: item for item in ranked}

    assert by_position[1].classification == "JOB_SEEKER"
    assert by_position[1].state == "ELIGIBLE_FOR_REVIEW"
    assert by_position[1].job_family == "IT"
    assert by_position[1].location_signal == "REMOTE"
    assert by_position[2].classification == "UNKNOWN"
    assert by_position[2].state == "SKIPPED"
    assert "NO_CLEAR_JOB_SEEKER_INTENT" in by_position[2].reason_codes
    assert by_position[3].classification == "JOB_SEEKER"
    assert by_position[3].state == "ELIGIBLE_FOR_REVIEW"


def test_vague_complaint_and_irrelevant_post_are_not_ranked_as_job_seekers() -> None:
    vague_complaint = _entry(
        "s7_vague_complaint",
        "Tìm việc khó quá, công việc nhiều áp lực và lương thấp.",
    )
    irrelevant = _entry("s7_off_topic", "Tối nay ăn phở ngon quá.")

    complaint_result, irrelevant_result = _rank((vague_complaint, irrelevant))

    assert complaint_result.classification == "GENERAL_DISCUSSION"
    assert complaint_result.state == "NEEDS_CONTEXT"
    assert "AMBIGUOUS_JOB_SEEKER_REFERENCE" in complaint_result.reason_codes
    assert irrelevant_result.classification == "UNKNOWN"
    assert irrelevant_result.state == "SKIPPED"
    assert "NO_CLEAR_JOB_SEEKER_INTENT" in irrelevant_result.reason_codes


def test_negative_contradictory_and_mixed_intent_are_fail_closed() -> None:
    negated = _entry(
        "s7_negated",
        "Mình không tìm việc nữa vì đã nhận offer rồi.",
    )
    contradictory = _entry(
        "s7_contradictory",
        "Tôi đang tìm việc IT nhưng hiện tôi không còn tìm việc.",
    )
    mixed = _entry(
        "s7_mixed",
        "Tôi đang tìm việc IT, còn công ty tôi đang tuyển nhân viên sales.",
    )
    recruiter_negated = _entry(
        "s7_recruiter_negated",
        "Công ty không tuyển nữa; vị trí đã có người.",
    )

    ranked = _rank((negated, contradictory, mixed, recruiter_negated))
    by_position = {item.input_position: item for item in ranked}

    assert by_position[1].state == "SKIPPED"
    assert "JOB_SEEKING_NEGATION_PRESENT" in by_position[1].reason_codes
    assert by_position[2].classification == "UNKNOWN"
    assert by_position[2].state == "NEEDS_CONTEXT"
    assert "CONTRADICTORY_JOB_SEEKER_WORDING" in by_position[2].reason_codes
    assert by_position[3].classification == "UNKNOWN"
    assert by_position[3].state == "NEEDS_CONTEXT"
    assert "MIXED_JOB_SEEKER_AND_RECRUITER_INTENT" in by_position[3].reason_codes
    assert by_position[4].state == "SKIPPED"
    assert "RECRUITMENT_NEGATION_PRESENT" in by_position[4].reason_codes


def test_missing_partial_and_unknown_completeness_remain_needs_context() -> None:
    missing = _entry("s7_missing", None)
    partial = _entry(
        "s7_partial",
        "Mình đang tìm việc kế toán ở Hà Nội.",
        completeness="PARTIAL",
    )
    unknown = _entry(
        "s7_unknown_completeness",
        "Mình đang tìm việc kế toán ở Hà Nội.",
        completeness="UNKNOWN",
    )

    ranked = _rank((missing, partial, unknown))
    by_position = {item.input_position: item for item in ranked}

    assert by_position[1].classification == "UNKNOWN"
    assert by_position[1].state == "NEEDS_CONTEXT"
    assert "CONTEXT_MISSING" in by_position[1].reason_codes
    assert by_position[2].classification == "JOB_SEEKER"
    assert by_position[2].state == "NEEDS_CONTEXT"
    assert "CONTEXT_PARTIAL" in by_position[2].reason_codes
    assert by_position[3].state == "NEEDS_CONTEXT"
    assert "CONTEXT_COMPLETENESS_UNKNOWN" in by_position[3].reason_codes


def test_complete_is_an_operator_assertion_not_proven_conversation_coverage() -> None:
    seeker = _entry(
        "s7_complete_assertion",
        "Mình đang tìm việc marketing ở Đà Nẵng. Bài đăng hôm qua có ai tư vấn không?",
    )

    (result,) = _rank((seeker,))

    assert result.state == "ELIGIBLE_FOR_REVIEW"
    assert result.context_completeness == "COMPLETE"
    assert result.recency_signal == "OPERATOR_ASSERTED_UNVERIFIED"
    assert result.unanswered_status == "UNASSESSED_COVERAGE_UNVERIFIED"
    assert "CONTEXT_COMPLETE_OPERATOR_ASSERTION" in result.reason_codes
    assert "CONVERSATION_COVERAGE_UNVERIFIED" in result.reason_codes
    assert "UNANSWERED_STATUS_UNASSESSED" in result.reason_codes
    assert "LOCATION_FIT_UNASSESSED_NO_ACCOUNT_PREFERENCE" in result.reason_codes
    assert "RECENCY_OPERATOR_ASSERTED_UNVERIFIED" in result.reason_codes


def test_nurture_exclusions_sensitive_requests_and_contact_suppression_skip() -> None:
    excluded = _entry(
        "s7_excluded",
        "Mình đang tìm việc, nhưng bài này quảng cáo đa cấp.",
    )
    sensitive = _entry(
        "s7_sensitive",
        "Mình đang tìm việc IT, vui lòng gửi mã OTP và mật khẩu để được nhận.",
    )
    no_contact = _entry(
        "s7_no_contact",
        "Mình đang tìm việc kế toán nhưng xin đừng nhắn tin hoặc liên hệ.",
    )

    excluded_result, sensitive_result, no_contact_result = _rank((excluded, sensitive, no_contact))

    assert excluded_result.state == "SKIPPED"
    assert "EXCLUDED_BY_RECRUITMENT_PRESET" in excluded_result.reason_codes
    assert sensitive_result.state == "SKIPPED"
    assert "SENSITIVE_PERSONAL_DATA_SIGNAL" in sensitive_result.reason_codes
    assert no_contact_result.state == "SKIPPED"
    assert "NO_CONTACT_SUPPRESSION_SIGNAL" in no_contact_result.reason_codes


def test_duplicate_targets_exact_context_and_paraphrases_are_suppressed_or_held() -> None:
    original_context = "Mình đang tìm việc IT remote tại Hà Nội, ưu tiên backend Python developer."
    first = _entry("s7_dup_first", original_context)
    exact = _entry("s7_dup_exact", original_context.upper())
    paraphrase = _entry(
        "s7_dup_paraphrase",
        "Mình tìm việc IT remote ở Hà Nội, ưu tiên backend Python developer.",
    )
    repeated_target = _entry(
        "s7_duplicate_target",
        "Tôi đang tìm việc sales tại Hà Nội.",
        target_url=first.target_url,
    )

    ranked = _rank((first, exact, paraphrase, repeated_target))
    by_position = {item.input_position: item for item in ranked}

    assert by_position[1].state == "ELIGIBLE_FOR_REVIEW"
    assert by_position[2].state == "SKIPPED"
    assert "DUPLICATE_CONTEXT" in by_position[2].reason_codes
    assert by_position[3].state == "NEEDS_CONTEXT"
    assert "POSSIBLE_DUPLICATE_CONTEXT" in by_position[3].reason_codes
    assert by_position[4].state == "SKIPPED"
    assert "DUPLICATE_TARGET" in by_position[4].reason_codes


def test_duplicate_repost_and_prior_contact_text_are_suppressed() -> None:
    repost = _entry(
        "s7_repost",
        "Đăng lại bài trước: mình đang tìm việc IT remote tại Hà Nội.",
    )
    prior_contact = _entry(
        "s7_prior_contact",
        "Mình đang tìm việc kế toán, nhưng đã liên hệ nhà tuyển dụng trong bài này.",
    )

    repost_result, contact_result = _rank((repost, prior_contact))

    assert repost_result.state == "SKIPPED"
    assert "DUPLICATE_OR_REPOST_SIGNAL" in repost_result.reason_codes
    assert contact_result.state == "SKIPPED"
    assert "PRIOR_CONTACT_SIGNAL" in contact_result.reason_codes


def test_job_family_location_and_score_ties_are_stable_in_input_order() -> None:
    first = _entry("s7_tie_first", "Mình đang tìm việc IT remote ở Hà Nội.")
    second = _entry("s7_tie_second", "Mình đang tìm việc IT remote tại Đà Nẵng.")
    finance = _entry("s7_finance", "Tôi đang tìm việc kế toán tại Hà Nội.")
    multiple_families = _entry(
        "s7_multiple_families",
        "Tôi đang tìm việc IT và marketing tại Hà Nội.",
    )

    ranked = _rank((first, second, finance, multiple_families))
    eligible = [item for item in ranked if item.state == "ELIGIBLE_FOR_REVIEW"]

    assert [item.input_position for item in eligible] == [1, 2, 3, 4]
    assert [item.score for item in eligible] == sorted(
        (item.score for item in eligible), reverse=True
    )
    assert [item.input_position for item in eligible[:2]] == [1, 2]
    assert _entry_by_position(ranked, 3).job_family == "FINANCE"
    assert _entry_by_position(ranked, 3).location_signal == "PLACE_SPECIFIED"
    assert _entry_by_position(ranked, 4).job_family == "MULTIPLE"
    assert "JOB_FAMILY_MULTIPLE" in _entry_by_position(ranked, 4).reason_codes


def test_s4_unresolved_and_ambiguous_outcomes_suppress_without_mutation() -> None:
    unresolved = _entry("s7_s4_unresolved", "Mình đang tìm việc IT remote.")
    ambiguous = _entry("s7_s4_ambiguous", "Tôi đang tìm việc sales ở Hà Nội.")
    aborted = _entry("s7_s4_aborted", "Tôi đang tìm việc kế toán ở Hà Nội.")
    outcomes = {
        unresolved.target_url: "HANDOFF_UNRESOLVED",
        ambiguous.target_url: "AMBIGUOUS",
        aborted.target_url: "ABORTED_NOT_SENT",
    }

    ranked = _rank((unresolved, ambiguous, aborted), outcomes=outcomes)
    by_position = {item.input_position: item for item in ranked}

    assert by_position[1].state == "SKIPPED"
    assert "S4_HANDOFF_UNRESOLVED" in by_position[1].reason_codes
    assert by_position[2].state == "SKIPPED"
    assert "S4_OUTCOME_BLOCKING" in by_position[2].reason_codes
    assert by_position[3].state == "ELIGIBLE_FOR_REVIEW"


def test_rank_limit_is_bounded_and_limit_ties_remain_stable() -> None:
    entries = tuple(
        _entry(f"s7_limit_{index}", "Mình đang tìm việc IT remote.") for index in range(20)
    )
    queue = OpportunityQueue(str(uuid4()), entries)

    assert rank_opportunities(queue, limit=1).returned_count == 1
    assert rank_opportunities(queue, limit=10).returned_count == 10
    twenty = rank_opportunities(queue, limit=20)
    assert twenty.returned_count == 20
    assert [item.input_position for item in twenty.items] == list(range(1, 21))
    for limit in (0, 21, True):
        with pytest.raises(RecruitmentQualificationError) as error:
            rank_opportunities(queue, limit=limit)  # type: ignore[arg-type]
        assert error.value.code == "OPPORTUNITY_RANK_LIMIT_INVALID"


def test_rank_queue_rejects_oversized_or_wrong_account_snapshots() -> None:
    account_id = uuid4()
    entry = _entry("s7_account", "Mình đang tìm việc IT remote.")
    valid_queue = OpportunityQueue(str(account_id), (entry,))
    ranked = rank_opportunities(valid_queue)
    assert ranked.account_uuid == str(account_id)
    assert ranked.items[0].account_uuid == str(account_id)
    assert ranked.items[0].target_url == entry.target_url
    assert ranked.items[0].source == "MANUAL_URL"

    invalid_account = OpportunityQueue("not-a-uuid", (entry,))
    with pytest.raises(RecruitmentQualificationError) as error:
        rank_opportunities(invalid_account)
    assert error.value.code == "OPPORTUNITY_RANK_QUEUE_INVALID"

    oversized = OpportunityQueue(
        str(account_id),
        tuple(
            _entry(f"s7_over_limit_{index}", "Mình đang tìm việc IT remote.") for index in range(21)
        ),
    )
    with pytest.raises(RecruitmentQualificationError) as error:
        rank_opportunities(oversized)
    assert error.value.code == "OPPORTUNITY_RANK_QUEUE_INVALID"


def test_cli_is_private_account_scoped_read_only_and_reads_s4_suppression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account_a = LocalAccountStore(root).add("synthetic_a")
    account_b = LocalAccountStore(root).add("synthetic_b")
    context_a = "PRIVATE_CONTEXT_SENTINEL: Mình đang tìm việc IT remote tại Hà Nội."
    context_b = "PRIVATE_CONTEXT_SENTINEL: Công ty đang tuyển nhân viên sales ở Đà Nẵng."
    entry_a = _add_queue_item(root, account_a, "s7_cli_a", context_a)
    entry_b = _add_queue_item(root, account_b, "s7_cli_b", context_b)
    ManualOutcomeStore(root).begin_handoff(account_a.id, entry_a.target_url, "a" * 64)
    queue_a_path = root / "opportunity_queue_v1" / "accounts" / f"{account_a.id}.json"
    queue_b_path = root / "opportunity_queue_v1" / "accounts" / f"{account_b.id}.json"
    fingerprint = ManualOutcomeStore.target_fingerprint(account_a.id, entry_a.target_url)
    outcome_path = (
        root / "manual_recruitment_v1" / "records" / str(account_a.id) / f"{fingerprint}.json"
    )
    queue_a_bytes = queue_a_path.read_bytes()
    queue_b_bytes = queue_b_path.read_bytes()
    outcome_bytes = outcome_path.read_bytes()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("qualification ranking must not construct a remote or browser runtime")

    monkeypatch.setattr(cli, "_run_browser_command", forbidden)
    monkeypatch.setattr(cli, "_run_api_command", forbidden)
    monkeypatch.setattr(cli, "_run_post_command", forbidden)
    monkeypatch.setattr(cli, "_run_reply_command", forbidden)
    monkeypatch.setattr(cli, "build_standalone_app", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-rank", account_a.alias, "--limit", "10"],
    )

    assert result == 0
    assert stdout == ""
    assert "RECRUITMENT OPPORTUNITY RANKING — PRIVATE LOCAL TERMINAL" in stderr
    assert f"account UUID: {account_a.id}" in stderr
    assert entry_a.id in stderr
    assert entry_a.target_url in stderr
    assert entry_b.target_url not in stderr
    assert "S4_HANDOFF_UNRESOLVED" in stderr
    assert context_a not in stderr
    assert context_b not in stderr
    assert queue_a_path.read_bytes() == queue_a_bytes
    assert queue_b_path.read_bytes() == queue_b_bytes
    assert outcome_path.read_bytes() == outcome_bytes
    assert (
        OpportunityQueueStore(root).list(account_a.id).opportunities[0].target_url
        == entry_a.target_url
    )
    assert (
        OpportunityQueueStore(root).list(account_b.id).opportunities[0].target_url
        == entry_b.target_url
    )


def test_s6_rank_to_s3_manual_review_keeps_exact_target_without_queue_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = LocalAccountStore(root).add("synthetic_account")
    context = "SYNTHETIC_CONTEXT_SENTINEL: Mình đang tìm việc IT remote tại Hà Nội."
    draft = "SYNTHETIC_DRAFT_SENTINEL: Chúc bạn sớm tìm được công việc phù hợp."
    entry = _add_queue_item(root, account, "s7_s3_review", context)
    queue_file = root / "opportunity_queue_v1" / "accounts" / f"{account.id}.json"
    queue_bytes = queue_file.read_bytes()

    ranked, rank_stdout, rank_stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-rank", account.alias],
    )

    targets_file = _write_targets(tmp_path / "s3-targets.txt", entry.target_url)
    context_file = tmp_path / "s3-context.txt"
    context_file.write_text(context, encoding="utf-8", newline="")
    draft_file = tmp_path / "s3-draft.txt"
    draft_file.write_text(draft, encoding="utf-8", newline="")
    reviewed, review_stdout, review_stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "manual-review",
            account.alias,
            "--targets-file",
            str(targets_file),
            "--select",
            "1",
            "--confirm-target",
            entry.target_url,
            "--draft-file",
            str(draft_file),
            "--context-file",
            str(context_file),
        ],
    )
    expected_review = build_manual_draft_review(
        account,
        load_manual_opportunities(targets_file).candidates[0],
        draft,
        context,
    )

    assert ranked == reviewed == 0
    assert rank_stdout == review_stdout == ""
    assert entry.id in rank_stderr
    assert entry.target_url in rank_stderr
    assert context not in rank_stderr
    assert "state=ELIGIBLE_FOR_REVIEW" in rank_stderr
    assert "MANUAL OPPORTUNITY REVIEW — PRIVATE LOCAL TERMINAL" in review_stderr
    assert f"selected target: {entry.target_url}" in review_stderr
    assert context in review_stderr
    assert draft in review_stderr
    assert f"draft sha256: {expected_review.draft_sha256}" in review_stderr
    assert queue_file.read_bytes() == queue_bytes


def test_cli_requires_private_tty_and_valid_limit_before_reading_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "must-not-be-created"
    not_tty, stdout, stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-rank", "synthetic"],
        tty=False,
    )

    assert not_tty == 1
    assert stdout == ""
    assert "ERROR PRIVATE_REVIEW_TERMINAL_REQUIRED" in stderr
    assert not root.exists()

    for limit in ("0", "21"):
        invalid_limit, stdout, stderr = _invoke(
            monkeypatch,
            root,
            ["recruitment", "opportunity-rank", "synthetic", "--limit", limit],
        )
        assert invalid_limit == 1
        assert stdout == ""
        assert "ERROR OPPORTUNITY_RANK_LIMIT_INVALID" in stderr
        assert not root.exists()


def test_cli_queue_read_errors_are_sanitized_and_do_not_echo_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = LocalAccountStore(root).add("synthetic_account")
    queue_file = root / "opportunity_queue_v1" / "accounts" / f"{account.id}.json"
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text("PRIVATE_CORRUPT_SENTINEL", encoding="utf-8")

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-rank", account.alias],
    )

    assert result == 1
    assert stdout == ""
    assert "ERROR OPPORTUNITY_QUEUE_INVALID" in stderr
    assert "PRIVATE_CORRUPT_SENTINEL" not in stderr


def test_empty_queue_and_default_cli_limit_are_bounded_and_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = LocalAccountStore(root).add("synthetic_account")

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-rank", account.alias],
    )

    assert result == 0
    assert stdout == ""
    assert "shown=0/0" in stderr
    assert "<empty>" in stderr


def test_assessment_repr_omits_private_target_and_context() -> None:
    private_context = "PRIVATE_CONTEXT_SENTINEL: Mình đang tìm việc IT remote."
    entry = _entry("s7_repr", private_context)
    (result,) = _rank((entry,))

    assert private_context not in repr(result)
    assert entry.target_url not in repr(result)
