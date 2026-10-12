"""Pure, deterministic qualification of private S6 opportunity queue entries."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TextIO
from uuid import UUID

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import normalize_manual_permalink
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.manual_opportunities import private_review_terminal_available
from threads_platform.standalone.manual_outcomes import ManualOutcomeStore
from threads_platform.standalone.nurture import get_nurture_preset
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueue,
    OpportunityQueueEntry,
    OpportunityQueueStore,
)

IntentClass = Literal["JOB_SEEKER", "RECRUITER", "GENERAL_DISCUSSION", "UNKNOWN"]
QualificationState = Literal["NEEDS_CONTEXT", "ELIGIBLE_FOR_REVIEW", "SKIPPED"]
JobFamily = Literal[
    "IT",
    "SALES",
    "MARKETING",
    "FINANCE",
    "HUMAN_RESOURCES",
    "CUSTOMER_SUPPORT",
    "ENGINEERING",
    "MULTIPLE",
    "UNKNOWN",
]
LocationSignal = Literal[
    "REMOTE",
    "HYBRID",
    "ONSITE",
    "PLACE_SPECIFIED",
    "MULTIPLE",
    "UNSPECIFIED",
]
RecencySignal = Literal["OPERATOR_ASSERTED_UNVERIFIED", "NOT_PROVIDED"]

_MAX_RETAINED_OPPORTUNITIES = 40
_MAX_ACTIVE_OPPORTUNITIES = 20
_MAX_RANK_LIMIT = 20
_MAX_CONTEXT_CHARS = 4_000
_MAX_TARGET_CHARS = 2_048
_POLICY_ID = "recruitment"
_RECRUITMENT_EXCLUDE_TERMS = get_nurture_preset(_POLICY_ID).exclude_terms
_SCORE_BASE = {
    "JOB_SEEKER": 60,
    "RECRUITER": 35,
    "GENERAL_DISCUSSION": 15,
    "UNKNOWN": 0,
}
_STATE_ORDER = {"ELIGIBLE_FOR_REVIEW": 0, "NEEDS_CONTEXT": 1, "SKIPPED": 2}
_VALID_QUEUE_STATES = frozenset({"NEEDS_CONTEXT", "READY", "SKIPPED"})
_VALID_COMPLETENESS = frozenset({"UNKNOWN", "PARTIAL", "COMPLETE"})

_JOB_SEEKER_POSITIVE = (
    "đang tìm việc",
    "đang tìm việc làm",
    "tìm việc",
    "tìm việc làm",
    "tìm job",
    "tìm kiếm việc làm",
    "kiếm việc",
    "kiếm việc làm",
    "cần tìm việc",
    "cần tìm việc làm",
    "cần việc làm",
    "xin việc",
    "đang ứng tuyển",
    "đang nộp hồ sơ xin việc",
    "gửi cv xin việc",
    "tìm cơ hội việc làm",
    "tìm vị trí công việc",
    "tìm thực tập",
    "tìm internship",
    "tìm việc cho mình",
    "looking for a job",
    "looking for work",
    "seeking a job",
    "seeking work",
    "job seeker",
    "open to work",
    "looking for an internship",
    "seeking an internship",
)
_JOB_SEEKER_NEGATIVE = (
    "không tìm việc",
    "không tìm việc làm",
    "không cần tìm việc",
    "không cần việc làm",
    "không còn tìm việc",
    "không còn tìm việc làm",
    "đã tìm được việc",
    "đã nhận việc",
    "đã nhận offer",
    "không muốn tìm việc",
    "không ứng tuyển",
    "not looking for a job",
    "not looking for work",
    "no longer looking for work",
    "not seeking employment",
    "already found a job",
)
_GENERIC_SEEKING_ACTIONS = frozenset(
    {
        "tìm việc",
        "tìm việc làm",
        "tìm job",
        "tìm kiếm việc làm",
        "kiếm việc",
        "kiếm việc làm",
        "cần tìm việc",
        "cần tìm việc làm",
        "cần việc làm",
        "xin việc",
        "đang ứng tuyển",
        "đang nộp hồ sơ xin việc",
        "gửi cv xin việc",
        "tìm cơ hội việc làm",
        "tìm vị trí công việc",
        "tìm thực tập",
        "tìm internship",
        "looking for a job",
        "looking for work",
        "seeking a job",
        "seeking work",
    }
)
_DIRECT_PROGRESSIVE_SEEKING_FORMS = frozenset(
    {"đang tìm việc", "đang tìm việc làm", "đang tìm job", "đang ứng tuyển"}
)
_FIRST_PERSON_TERMS = frozenset({"i", "me", "mình", "tôi", "em", "anh", "chị", "we"})
_RECRUITER_POSITIVE = (
    "đang tuyển",
    "cần tuyển",
    "tuyển nhân viên",
    "tuyển dụng vị trí",
    "tuyển dụng nhân viên",
    "công ty đang tuyển",
    "công ty tuyển",
    "đăng tuyển",
    "tìm ứng viên",
    "cần ứng viên",
    "tìm nhân sự",
    "mời ứng tuyển",
    "we are hiring",
    "we re hiring",
    "now hiring",
    "hiring for",
    "job opening",
    "open position",
    "position available",
    "looking for candidates",
    "seeking candidates",
    "recruiting for",
    "apply now",
)
_RECRUITER_NEGATIVE = (
    "không tuyển",
    "chưa tuyển",
    "đã tuyển đủ",
    "không còn tuyển",
    "không tuyển dụng",
    "vị trí đã có người",
    "đã đủ nhân sự",
    "not hiring",
    "no longer hiring",
    "position filled",
    "hiring is closed",
    "recruitment is closed",
)
_GENERAL_EMPLOYMENT_TERMS = (
    "tìm việc",
    "tìm job",
    "việc làm",
    "công việc",
    "nghề nghiệp",
    "tuyển dụng",
    "phỏng vấn",
    "cv",
    "lương",
    "job",
    "career",
    "employment",
    "work",
    "hiring",
    "recruiter",
    "recruitment",
    "nhân viên",
)
_JOB_FAMILY_TERMS: dict[str, tuple[str, ...]] = {
    "IT": (
        "it",
        "công nghệ thông tin",
        "lập trình",
        "developer",
        "dev",
        "software",
        "software engineer",
        "kỹ sư phần mềm",
        "python",
        "data analyst",
        "data engineer",
        "tester",
        "qa engineer",
        "cybersecurity",
    ),
    "SALES": ("sales", "kinh doanh", "bán hàng", "sales executive"),
    "MARKETING": ("marketing", "tiếp thị", "truyền thông", "social media"),
    "FINANCE": ("kế toán", "tài chính", "accountant", "finance"),
    "HUMAN_RESOURCES": (
        "nhân sự",
        "human resources",
        "hr",
        "recruitment specialist",
    ),
    "CUSTOMER_SUPPORT": (
        "chăm sóc khách hàng",
        "customer support",
        "customer service",
    ),
    "ENGINEERING": ("kỹ sư", "engineering", "mechanical engineer", "electrical engineer"),
}
_LOCATION_TERMS: dict[str, tuple[str, ...]] = {
    "REMOTE": ("remote", "work from home", "wfh", "làm việc từ xa", "làm từ xa"),
    "HYBRID": ("hybrid", "làm việc kết hợp", "kết hợp tại nhà và văn phòng"),
    "ONSITE": ("onsite", "on site", "tại văn phòng", "làm trực tiếp"),
    "PLACE_SPECIFIED": (
        "hà nội",
        "tp hcm",
        "thành phố hồ chí minh",
        "hồ chí minh",
        "sài gòn",
        "đà nẵng",
        "việt nam",
    ),
}
_STUDENT_TERMS = ("sinh viên", "student", "undergraduate")
_PART_TIME_TERMS = ("part time", "part-time", "bán thời gian", "làm bán thời gian")
_INTERNSHIP_TERMS = ("thực tập", "internship", "intern")
_RECENCY_TERMS = (
    "hôm nay",
    "hôm qua",
    "hôm kia",
    "mới đăng",
    "vừa đăng",
    "bài đăng hôm",
    "posted today",
    "posted yesterday",
    "days ago",
    "hours ago",
)
_DUPLICATE_MARKERS = (
    "đăng lại",
    "như bài trước",
    "bài trước của mình",
    "lặp lại bài",
    "nội dung giống bài",
    "same as my previous post",
    "same request as before",
    "repost",
    "duplicate post",
)
_PRIOR_CONTACT_MARKERS = (
    "đã liên hệ",
    "đã nhắn tin",
    "đã bình luận",
    "đã trả lời bài này",
    "already contacted",
    "already replied",
    "already reached out",
)
_NO_CONTACT_MARKERS = (
    "đừng liên hệ",
    "xin đừng nhắn tin",
    "không muốn được liên hệ",
    "không nhận tin nhắn",
    "do not contact",
    "don t contact",
    "stop contacting",
    "no outreach",
)
_SENSITIVE_MARKERS = (
    "số căn cước",
    "số cccd",
    "số tài khoản ngân hàng",
    "mã otp",
    "mật khẩu",
    "bank account number",
    "password",
    "one time password",
)
_DUPLICATE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "i",
        "in",
        "is",
        "it",
        "looking",
        "mình",
        "my",
        "of",
        "on",
        "the",
        "to",
        "tôi",
        "đang",
        "em",
        "một",
        "cần",
        "tìm",
        "việc",
        "làm",
        "người",
        "công",
        "ty",
    }
)


class RecruitmentQualificationError(Exception):
    """Fixed-code error for safe local ranking output."""

    _CODES = frozenset(
        {
            "PRIVATE_REVIEW_TERMINAL_REQUIRED",
            "OPPORTUNITY_RANK_LIMIT_INVALID",
            "OPPORTUNITY_RANK_QUEUE_INVALID",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported recruitment qualification error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityScoreComponent:
    reason_code: str
    points: int


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityQualification:
    rank: int
    input_position: int
    account_uuid: str
    opportunity_id: str
    target_url: str
    source: Literal["MANUAL_URL"]
    classification: IntentClass
    state: QualificationState
    score: int
    job_family: JobFamily
    location_signal: LocationSignal
    location_relevance: Literal["UNASSESSED_NO_ACCOUNT_PREFERENCE"]
    context_completeness: Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
    recency_signal: RecencySignal
    unanswered_status: Literal["UNASSESSED_COVERAGE_UNVERIFIED"]
    reason_codes: tuple[str, ...]
    score_components: tuple[OpportunityScoreComponent, ...]

    def __repr__(self) -> str:
        return (
            "OpportunityQualification("
            f"rank={self.rank}, input_position={self.input_position}, "
            f"account_uuid={self.account_uuid!r}, opportunity_id={self.opportunity_id!r}, "
            f"classification={self.classification!r}, state={self.state!r}, "
            f"score={self.score}, reason_codes={self.reason_codes!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityRanking:
    account_uuid: str
    policy_id: Literal["recruitment"]
    input_count: int
    returned_count: int
    items: tuple[OpportunityQualification, ...]


@dataclass(frozen=True, slots=True)
class _TextSignals:
    classification: IntentClass
    job_family: JobFamily
    location_signal: LocationSignal
    recency_signal: RecencySignal
    question_present: bool
    seeker_positive: bool
    recruiter_positive: bool
    seeker_negative: bool
    recruiter_negative: bool
    seeker_contradictory: bool
    recruiter_contradictory: bool
    mixed_intent: bool
    ambiguous_seeking_reference: bool
    excluded: bool
    duplicate_marker: bool
    prior_contact: bool
    no_contact: bool
    sensitive_request: bool
    student: bool
    part_time: bool
    internship: bool


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = [character if character.isalnum() else " " for character in normalized]
    return " ".join("".join(characters).split())


def _phrase_occurrences(text: str, phrase: str) -> tuple[tuple[int, int], ...]:
    needle = _normalize_text(phrase)
    if not needle:
        return ()
    padded_text = f" {text} "
    padded_needle = f" {needle} "
    positions: list[tuple[int, int]] = []
    start = 0
    while (match_start := padded_text.find(padded_needle, start)) >= 0:
        span_start = match_start + 1
        positions.append((span_start, span_start + len(needle)))
        start = match_start + 1
    return tuple(positions)


def _matched(text: str, phrases: Sequence[str]) -> bool:
    return any(_phrase_occurrences(text, phrase) for phrase in phrases)


def _positive_not_negated(
    text: str,
    positive_phrases: Sequence[str],
    negative_phrases: Sequence[str],
) -> bool:
    negative_spans = tuple(
        span for phrase in negative_phrases for span in _phrase_occurrences(text, phrase)
    )
    return any(
        not any(
            negative_start <= positive_start and positive_end <= negative_end
            for negative_start, negative_end in negative_spans
        )
        for phrase in positive_phrases
        for positive_start, positive_end in _phrase_occurrences(text, phrase)
    )


def _job_seeking_positive(text: str) -> bool:
    negative_spans = tuple(
        span for phrase in _JOB_SEEKER_NEGATIVE for span in _phrase_occurrences(text, phrase)
    )
    for phrase in _JOB_SEEKER_POSITIVE:
        for start, end in _phrase_occurrences(text, phrase):
            if any(
                negative_start <= start and end <= negative_end
                for negative_start, negative_end in negative_spans
            ):
                continue
            normalized_phrase = _normalize_text(phrase)
            if (
                normalized_phrase in _GENERIC_SEEKING_ACTIONS
                and normalized_phrase not in _DIRECT_PROGRESSIVE_SEEKING_FORMS
            ):
                preceding = text[:start].split()[-8:]
                if not any(token in _FIRST_PERSON_TERMS for token in preceding):
                    continue
            return True
    return False


def _job_family(text: str) -> JobFamily:
    matches = tuple(
        family for family, phrases in _JOB_FAMILY_TERMS.items() if _matched(text, phrases)
    )
    if not matches:
        return "UNKNOWN"
    if len(matches) > 1:
        return "MULTIPLE"
    return matches[0]  # type: ignore[return-value]


def _location_signal(text: str) -> LocationSignal:
    matches = tuple(kind for kind, phrases in _LOCATION_TERMS.items() if _matched(text, phrases))
    if not matches:
        return "UNSPECIFIED"
    if len(matches) > 1:
        return "MULTIPLE"
    return matches[0]  # type: ignore[return-value]


def _text_signals(context: str) -> _TextSignals:
    text = _normalize_text(context)
    seeker_negative = _matched(text, _JOB_SEEKER_NEGATIVE)
    recruiter_negative = _matched(text, _RECRUITER_NEGATIVE)
    seeker_positive = _job_seeking_positive(text)
    recruiter_positive = _positive_not_negated(text, _RECRUITER_POSITIVE, _RECRUITER_NEGATIVE)
    seeker_contradictory = seeker_positive and seeker_negative
    recruiter_contradictory = recruiter_positive and recruiter_negative
    mixed_intent = seeker_positive and recruiter_positive
    ambiguous_seeking_reference = (
        not seeker_positive
        and not seeker_negative
        and _matched(text, tuple(_GENERIC_SEEKING_ACTIONS))
    )

    if seeker_contradictory or recruiter_contradictory or mixed_intent:
        classification: IntentClass = "UNKNOWN"
    elif seeker_positive:
        classification = "JOB_SEEKER"
    elif recruiter_positive:
        classification = "RECRUITER"
    elif _matched(text, _GENERAL_EMPLOYMENT_TERMS) or seeker_negative or recruiter_negative:
        classification = "GENERAL_DISCUSSION"
    else:
        classification = "UNKNOWN"

    job_family = _job_family(text)
    location_signal = _location_signal(text)
    recency_signal: RecencySignal = (
        "OPERATOR_ASSERTED_UNVERIFIED" if _matched(text, _RECENCY_TERMS) else "NOT_PROVIDED"
    )
    excluded = _matched(text, _RECRUITMENT_EXCLUDE_TERMS)

    return _TextSignals(
        classification=classification,
        job_family=job_family,
        location_signal=location_signal,
        recency_signal=recency_signal,
        question_present="?" in unicodedata.normalize("NFKC", context),
        seeker_positive=seeker_positive,
        recruiter_positive=recruiter_positive,
        seeker_negative=seeker_negative,
        recruiter_negative=recruiter_negative,
        seeker_contradictory=seeker_contradictory,
        recruiter_contradictory=recruiter_contradictory,
        mixed_intent=mixed_intent,
        ambiguous_seeking_reference=ambiguous_seeking_reference,
        excluded=excluded,
        duplicate_marker=_matched(text, _DUPLICATE_MARKERS),
        prior_contact=_matched(text, _PRIOR_CONTACT_MARKERS),
        no_contact=_matched(text, _NO_CONTACT_MARKERS),
        sensitive_request=_matched(text, _SENSITIVE_MARKERS),
        student=_matched(text, _STUDENT_TERMS),
        part_time=_matched(text, _PART_TIME_TERMS),
        internship=_matched(text, _INTERNSHIP_TERMS),
    )


def _duplicate_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token for token in text.split() if len(token) >= 3 and token not in _DUPLICATE_STOP_WORDS
    )


def _possible_context_duplicate(left: str, right: str) -> bool:
    left_tokens = _duplicate_tokens(left)
    right_tokens = _duplicate_tokens(right)
    if len(left_tokens) < 5 or len(right_tokens) < 5:
        return False
    overlap = len(left_tokens & right_tokens)
    if overlap < 3:
        return False
    return overlap / len(left_tokens | right_tokens) >= 0.65


def _validate_queue(queue: OpportunityQueue, limit: int) -> UUID:
    if type(limit) is not int or not 1 <= limit <= _MAX_RANK_LIMIT:
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_LIMIT_INVALID")
    if type(queue) is not OpportunityQueue:
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
    try:
        account_id = UUID(queue.account_uuid)
    except ValueError, TypeError, AttributeError:
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID") from None
    if account_id.version != 4 or str(account_id) != queue.account_uuid:
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
    if (
        type(queue.opportunities) is not tuple
        or len(queue.opportunities) > _MAX_RETAINED_OPPORTUNITIES
    ):
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
    seen_ids: set[str] = set()
    active_count = 0
    for entry in queue.opportunities:
        if type(entry) is not OpportunityQueueEntry:
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        try:
            opportunity_id = UUID(entry.id)
            canonical_target = normalize_manual_permalink(entry.target_url).thread_ref
        except BrowserContractError, TypeError, ValueError, AttributeError:
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID") from None
        if (
            opportunity_id.version != 4
            or str(opportunity_id) != entry.id
            or canonical_target != entry.target_url
            or len(entry.target_url) > _MAX_TARGET_CHARS
            or entry.source != "MANUAL_URL"
            or entry.state not in _VALID_QUEUE_STATES
            or entry.context_completeness not in _VALID_COMPLETENESS
            or entry.context_provenance not in {"NONE", "OPERATOR_SUPPLIED"}
        ):
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        if entry.context is not None and (
            type(entry.context) is not str or len(entry.context) > _MAX_CONTEXT_CHARS
        ):
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        if (entry.context is None) != (entry.context_provenance == "NONE"):
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        if entry.context is None and entry.context_completeness != "UNKNOWN":
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        if entry.state == "READY" and (
            entry.context is None or entry.context_completeness != "COMPLETE"
        ):
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        if entry.state == "NEEDS_CONTEXT" and entry.context_completeness == "COMPLETE":
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        if entry.id in seen_ids:
            raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
        seen_ids.add(entry.id)
        active_count += entry.state != "SKIPPED"
    if active_count > _MAX_ACTIVE_OPPORTUNITIES:
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")
    return account_id


def _base_reasons(entry: OpportunityQueueEntry) -> list[str]:
    reasons = [
        "SOURCE_MANUAL_URL",
        "AUTHOR_IDENTITY_UNVERIFIED",
        "CONVERSATION_COVERAGE_UNVERIFIED",
    ]
    reasons.append("LOCATION_FIT_UNASSESSED_NO_ACCOUNT_PREFERENCE")
    reasons.append("UNANSWERED_STATUS_UNASSESSED")
    if entry.context is None:
        reasons.append("CONTEXT_MISSING")
    elif entry.context_completeness == "PARTIAL":
        reasons.append("CONTEXT_PARTIAL")
    elif entry.context_completeness == "UNKNOWN":
        reasons.append("CONTEXT_COMPLETENESS_UNKNOWN")
    else:
        reasons.append("CONTEXT_COMPLETE_OPERATOR_ASSERTION")
    return reasons


def _assess_entry(
    entry: OpportunityQueueEntry,
    *,
    account_uuid: str,
    input_position: int,
    s4_status: str | None,
    duplicate_target: bool,
    duplicate_context: Literal["NONE", "EXACT", "POSSIBLE"],
) -> OpportunityQualification:
    context = entry.context
    signals = _text_signals(context) if context is not None else None
    classification: IntentClass = "UNKNOWN" if signals is None else signals.classification
    job_family: JobFamily = "UNKNOWN" if signals is None else signals.job_family
    location_signal: LocationSignal = "UNSPECIFIED" if signals is None else signals.location_signal
    recency_signal: RecencySignal = "NOT_PROVIDED" if signals is None else signals.recency_signal
    reasons = _base_reasons(entry)
    components = [OpportunityScoreComponent(f"{classification}_BASE", _SCORE_BASE[classification])]

    if signals is not None:
        if classification == "JOB_SEEKER":
            reasons.append("JOB_SEEKING_INTENT")
        elif classification == "RECRUITER":
            reasons.append("RECRUITMENT_JOB_OFFER_INTENT")
        elif classification == "GENERAL_DISCUSSION":
            reasons.append("GENERAL_EMPLOYMENT_DISCUSSION")
        else:
            reasons.append("INTENT_UNKNOWN_OR_CONTRADICTORY")

        if job_family == "UNKNOWN":
            reasons.append("JOB_FAMILY_UNSPECIFIED")
        elif job_family == "MULTIPLE":
            reasons.append("JOB_FAMILY_MULTIPLE")
        else:
            reasons.append("JOB_FAMILY_EXPLICIT")
            components.append(OpportunityScoreComponent("JOB_FAMILY_EXPLICIT", 8))

        if location_signal == "UNSPECIFIED":
            reasons.append("LOCATION_SIGNAL_UNSPECIFIED")
        else:
            reasons.append(f"LOCATION_SIGNAL_{location_signal}")
        reasons.append("LOCATION_FIT_UNASSESSED_NO_ACCOUNT_PREFERENCE")
        if signals.question_present:
            reasons.append("QUESTION_PRESENT")
            components.append(OpportunityScoreComponent("QUESTION_PRESENT", 3))
        else:
            reasons.append("QUESTION_NOT_PRESENT")

        if recency_signal == "OPERATOR_ASSERTED_UNVERIFIED":
            reasons.append("RECENCY_OPERATOR_ASSERTED_UNVERIFIED")
        else:
            reasons.append("RECENCY_NOT_PROVIDED")
        reasons.append("UNANSWERED_STATUS_UNASSESSED")

        if signals.student:
            reasons.append("STUDENT_SIGNAL")
        if signals.part_time:
            reasons.append("PART_TIME_SIGNAL")
        if signals.internship:
            reasons.append("INTERNSHIP_SIGNAL")
        if signals.seeker_negative:
            reasons.append("JOB_SEEKING_NEGATION_PRESENT")
        if signals.recruiter_negative:
            reasons.append("RECRUITMENT_NEGATION_PRESENT")
        if signals.seeker_contradictory:
            reasons.append("CONTRADICTORY_JOB_SEEKER_WORDING")
        if signals.recruiter_contradictory:
            reasons.append("CONTRADICTORY_RECRUITMENT_WORDING")
        if signals.mixed_intent:
            reasons.append("MIXED_JOB_SEEKER_AND_RECRUITER_INTENT")
        if signals.ambiguous_seeking_reference:
            reasons.append("AMBIGUOUS_JOB_SEEKER_REFERENCE")

    suppressing_codes: list[str] = []
    if entry.state == "SKIPPED":
        suppressing_codes.append("QUEUE_ENTRY_SKIPPED")
    if duplicate_target:
        suppressing_codes.append("DUPLICATE_TARGET")
    if duplicate_context == "EXACT":
        suppressing_codes.append("DUPLICATE_CONTEXT")
    elif duplicate_context == "POSSIBLE":
        reasons.append("POSSIBLE_DUPLICATE_CONTEXT")
    if s4_status == "HANDOFF_UNRESOLVED":
        suppressing_codes.append("S4_HANDOFF_UNRESOLVED")
    elif s4_status in {"AMBIGUOUS", "OPERATOR_REPORTED_SENT_UNVERIFIED"}:
        suppressing_codes.append("S4_OUTCOME_BLOCKING")

    if signals is not None:
        if signals.excluded:
            suppressing_codes.append("EXCLUDED_BY_RECRUITMENT_PRESET")
        if signals.duplicate_marker:
            suppressing_codes.append("DUPLICATE_OR_REPOST_SIGNAL")
        if signals.prior_contact:
            suppressing_codes.append("PRIOR_CONTACT_SIGNAL")
        if signals.no_contact:
            suppressing_codes.append("NO_CONTACT_SUPPRESSION_SIGNAL")
        if signals.sensitive_request:
            suppressing_codes.append("SENSITIVE_PERSONAL_DATA_SIGNAL")

    for code in suppressing_codes:
        reasons.append(code)
        components.append(OpportunityScoreComponent(code, -100))

    has_context = context is not None and bool(context.strip())
    context_complete = entry.context_completeness == "COMPLETE"
    context_uncertain = not has_context or not context_complete
    mixed_or_contradictory = signals is not None and (
        signals.mixed_intent
        or signals.seeker_contradictory
        or signals.recruiter_contradictory
        or signals.ambiguous_seeking_reference
    )
    if suppressing_codes:
        state: QualificationState = "SKIPPED"
    elif context_uncertain or mixed_or_contradictory or duplicate_context == "POSSIBLE":
        state = "NEEDS_CONTEXT"
        if not has_context:
            reasons.append("NEEDS_CONTEXT_MISSING")
        elif not context_complete:
            reasons.append("NEEDS_CONTEXT_COMPLETENESS_UNVERIFIED")
        if mixed_or_contradictory:
            reasons.append("NEEDS_CONTEXT_INTENT_AMBIGUOUS")
    elif classification == "JOB_SEEKER":
        state = "ELIGIBLE_FOR_REVIEW"
        reasons.append("ELIGIBLE_FOR_REVIEW_ONLY")
    elif classification == "RECRUITER":
        state = "SKIPPED"
        reasons.append("RECRUITER_NOT_JOB_SEEKER_TARGET")
    elif classification == "GENERAL_DISCUSSION":
        state = "SKIPPED"
        reasons.append("GENERAL_DISCUSSION_NOT_JOB_SEEKER_TARGET")
    else:
        state = "SKIPPED"
        reasons.append("NO_CLEAR_JOB_SEEKER_INTENT")

    score = max(0, min(100, sum(component.points for component in components)))
    stable_reasons = tuple(dict.fromkeys(reasons))
    return OpportunityQualification(
        rank=0,
        input_position=input_position,
        account_uuid=account_uuid,
        opportunity_id=entry.id,
        target_url=entry.target_url,
        source=entry.source,
        classification=classification,
        state=state,
        score=score,
        job_family=job_family,
        location_signal=location_signal,
        location_relevance="UNASSESSED_NO_ACCOUNT_PREFERENCE",
        context_completeness=entry.context_completeness,
        recency_signal=recency_signal,
        unanswered_status="UNASSESSED_COVERAGE_UNVERIFIED",
        reason_codes=stable_reasons,
        score_components=tuple(components),
    )


def rank_opportunities(
    queue: OpportunityQueue,
    *,
    limit: int = 10,
    s4_outcomes: Mapping[str, str] | None = None,
) -> OpportunityRanking:
    """Return a deterministic ephemeral ranking without modifying queue records."""

    account_id = _validate_queue(queue, limit)
    outcomes = {} if s4_outcomes is None else dict(s4_outcomes)
    valid_targets = {entry.target_url for entry in queue.opportunities}
    if any(
        target not in valid_targets
        or status
        not in {
            "HANDOFF_UNRESOLVED",
            "ABORTED_NOT_SENT",
            "OPERATOR_REPORTED_SENT_UNVERIFIED",
            "AMBIGUOUS",
        }
        for target, status in outcomes.items()
    ):
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_QUEUE_INVALID")

    entries = queue.opportunities
    seen_targets: set[str] = set()
    seen_contexts: list[str] = []
    assessments: list[OpportunityQualification] = []
    for input_position, entry in enumerate(entries, start=1):
        duplicate_target = entry.target_url in seen_targets
        seen_targets.add(entry.target_url)
        duplicate_context: Literal["NONE", "EXACT", "POSSIBLE"] = "NONE"
        if entry.context is not None and entry.context_completeness == "COMPLETE":
            normalized_context = _normalize_text(entry.context)
            for prior_context in seen_contexts:
                if normalized_context == prior_context:
                    duplicate_context = "EXACT"
                    break
                if _possible_context_duplicate(normalized_context, prior_context):
                    duplicate_context = "POSSIBLE"
                    break
            if not duplicate_target and duplicate_context == "NONE" and entry.state != "SKIPPED":
                seen_contexts.append(normalized_context)

        assessments.append(
            _assess_entry(
                entry,
                account_uuid=str(account_id),
                input_position=input_position,
                s4_status=outcomes.get(entry.target_url),
                duplicate_target=duplicate_target,
                duplicate_context=duplicate_context,
            )
        )

    ranked = sorted(
        assessments,
        key=lambda item: (_STATE_ORDER[item.state], -item.score, item.input_position),
    )[:limit]
    ranked_items = tuple(replace(item, rank=index) for index, item in enumerate(ranked, start=1))
    return OpportunityRanking(
        account_uuid=str(account_id),
        policy_id=_POLICY_ID,
        input_count=len(entries),
        returned_count=len(ranked_items),
        items=ranked_items,
    )


def _read_s4_outcomes(
    data_root: Path,
    account_id: UUID,
    entries: Sequence[OpportunityQueueEntry],
) -> dict[str, str]:
    outcome_store = ManualOutcomeStore(data_root)
    statuses: dict[str, str] = {}
    for entry in entries:
        outcome = outcome_store.read(account_id, entry.target_url)
        if outcome is not None:
            statuses[entry.target_url] = outcome.latest.status
    return statuses


def _render_ranking(account_alias: str, ranking: OpportunityRanking) -> str:
    eligible_count = sum(item.state == "ELIGIBLE_FOR_REVIEW" for item in ranking.items)
    needs_context_count = sum(item.state == "NEEDS_CONTEXT" for item in ranking.items)
    skipped_count = sum(item.state == "SKIPPED" for item in ranking.items)
    lines = [
        "RECRUITMENT OPPORTUNITY RANKING — PRIVATE LOCAL TERMINAL",
        f"account alias: {account_alias}",
        f"account UUID: {ranking.account_uuid}",
        f"policy: {ranking.policy_id}",
        (
            f"shown={ranking.returned_count}/{ranking.input_count} "
            f"eligible={eligible_count} needs_context={needs_context_count} skipped={skipped_count}"
        ),
    ]
    if not ranking.items:
        lines.append("<empty>")
    for item in ranking.items:
        components = ",".join(
            f"{component.reason_code}:{component.points:+d}" for component in item.score_components
        )
        reasons = ",".join(item.reason_codes)
        lines.append(
            f"{item.rank}. id={item.opportunity_id} state={item.state} "
            f"class={item.classification} score={item.score} "
            f"job_family={item.job_family} location_signal={item.location_signal} "
            f"location_relevance={item.location_relevance} "
            f"evidence={item.context_completeness} recency={item.recency_signal} "
            f"unanswered={item.unanswered_status} source={item.source} "
            f"input_position={item.input_position} target={item.target_url}\n"
            f"   score_components={components or '-'}\n"
            f"   reasons={reasons}"
        )
    return "\n".join(lines) + "\n"


def run_opportunity_rank_cli(
    alias: str,
    *,
    limit: int = 10,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run the read-only rank command, writing all private output to TTY stderr."""

    private_stdin = sys.stdin if stdin is None else stdin
    private_stdout = sys.stdout if stdout is None else stdout
    private_stderr = sys.stderr if stderr is None else stderr
    env = os.environ if environment is None else environment
    if not private_review_terminal_available(
        stdin=private_stdin,
        stdout=private_stdout,
        stderr=private_stderr,
        environment=env,
    ):
        raise RecruitmentQualificationError("PRIVATE_REVIEW_TERMINAL_REQUIRED")

    if type(limit) is not int or not 1 <= limit <= _MAX_RANK_LIMIT:
        raise RecruitmentQualificationError("OPPORTUNITY_RANK_LIMIT_INVALID")

    data_root = Path(data_root_factory())
    account = LocalAccountStore(data_root).get(alias)
    queue = OpportunityQueueStore(data_root).list(account.id)
    s4_outcomes = _read_s4_outcomes(data_root, account.id, queue.opportunities)
    ranking = rank_opportunities(queue, limit=limit, s4_outcomes=s4_outcomes)
    private_stderr.write(_render_ranking(account.alias, ranking))
    private_stderr.flush()
    return 0
