"""Ephemeral synthetic reply proposals for human review only."""

from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
from uuid import UUID

from threads_platform.standalone.nurture_discovery import NurtureDiscoverySource
from threads_platform.standalone.nurture_runner import (
    NurtureShortlistCandidate,
    NurtureShortlistResult,
)

_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_MANUAL_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MAX_SHORTLIST_CANDIDATES = 10
_MAX_REPLY_CHARS = 500
_MAX_GENERATION_TIMEOUT_SECONDS = 30.0
_LOW_INFORMATION_PHRASES = frozenset(
    {
        "i dont know",
        "i don t know",
        "idk",
        "no idea",
        "no comment",
        "not sure",
        "i am not sure",
        "n a",
        "na",
        "ok",
        "okay",
        "yes",
        "no",
        "maybe",
        "thanks",
        "thank you",
    }
)
_QUALITY_WORD_SEPARATOR = re.compile(r"[^a-z0-9]+")

ReplyQualityFlag = Literal["LOW_INFORMATION", "DUPLICATE_WORDING"]
ReplyQualityOutcome = Literal["REVIEW_RECOMMENDED", "NO_OBVIOUS_ISSUE_DETECTED"]


class ReplyProposalError(ValueError):
    """Safe proposal failure containing only a stable error code."""

    code: str

    def __init__(self, code: str) -> None:
        if code not in {
            "INVALID_SHORTLIST",
            "INVALID_SELECTION",
            "CANDIDATE_NOT_ELIGIBLE",
            "CANDIDATE_CONTEXT_UNAVAILABLE",
            "PROPOSAL_TIMEOUT",
            "GENERATOR_FAILED",
            "INVALID_PROPOSAL_OUTPUT",
            "TARGET_BINDING_MISMATCH",
            "INVALID_EDITOR_SESSION",
            "EDITOR_SESSION_CLOSED",
            "INVALID_EDITED_REPLY",
            "INVALID_QUALITY_ADVISORY",
        }:
            raise ValueError("invalid reply proposal error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class EmployerJobFacts:
    """Small operator-asserted fact packet; values stay out of object reprs."""

    role: str | None = field(default=None, repr=False)
    location: str | None = field(default=None, repr=False)
    pay_range: str | None = field(default=None, repr=False)
    eligibility: str | None = field(default=None, repr=False)
    verified_contact_available: bool = False
    provenance: Literal["OPERATOR_ASSERTED_VERIFIED"] = "OPERATOR_ASSERTED_VERIFIED"

    def __post_init__(self) -> None:
        values = (self.role, self.location, self.pay_range, self.eligibility)
        if (
            type(self.provenance) is not str
            or self.provenance != "OPERATOR_ASSERTED_VERIFIED"
            or type(self.verified_contact_available) is not bool
            or (
                not any(value is not None for value in values)
                and not self.verified_contact_available
            )
            or any(
                value is not None
                and (
                    type(value) is not str
                    or not value
                    or value != value.strip()
                    or len(value) > 120
                    or any(
                        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                        for character in value
                    )
                )
                for value in values
            )
        ):
            raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")

    def __repr__(self) -> str:
        return (
            "EmployerJobFacts(values=<redacted>, "
            f"verified_contact_available={self.verified_contact_available}, "
            f"provenance={self.provenance})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ReplyProposalInput:
    """Bounded candidate context. Its text is untrusted and never appears in repr."""

    source: NurtureDiscoverySource | Literal["INBOUND_REPLY", "MANUAL_URL"]
    score: int | None
    reason_codes: tuple[str, ...]
    candidate_text: str = field(repr=False)
    account_uuid: str | None = field(default=None, repr=False)
    target_id: str | None = field(default=None, repr=False)
    context_provenance: Literal["OPERATOR_SUPPLIED"] | None = None
    context_completeness: Literal["UNKNOWN", "PARTIAL", "COMPLETE"] | None = None
    recipient_intent: Literal["JOB_SEEKER", "RECRUITER", "GENERAL_DISCUSSION", "UNKNOWN"] | None = (
        None
    )
    language: Literal["vi", "en"] | None = None
    job_family: str | None = None
    location_signal: str | None = None
    student_signal: bool = False
    part_time_signal: bool = False
    internship_signal: bool = False
    employer_facts: EmployerJobFacts | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        manual_source = self.source == "MANUAL_URL"
        reason_code_limit = 24 if manual_source else 16
        reason_code_pattern = _MANUAL_REASON_CODE if manual_source else _REASON_CODE
        if (
            self.source not in {*NurtureDiscoverySource, "INBOUND_REPLY", "MANUAL_URL"}
            or (self.score is not None and (type(self.score) is not int or self.score < 0))
            or type(self.reason_codes) is not tuple
            or len(self.reason_codes) > reason_code_limit
            or any(
                type(code) is not str or reason_code_pattern.fullmatch(code) is None
                for code in self.reason_codes
            )
            or type(self.candidate_text) is not str
            or not self.candidate_text.strip()
            or len(self.candidate_text) > 500
        ):
            raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")
        if self.source == "MANUAL_URL":
            try:
                account_id = UUID(cast(str, self.account_uuid))
                target_id = UUID(cast(str, self.target_id))
            except TypeError, ValueError, AttributeError:
                raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE") from None
            if (
                account_id.version != 4
                or str(account_id) != self.account_uuid
                or target_id.version != 4
                or str(target_id) != self.target_id
                or self.context_provenance != "OPERATOR_SUPPLIED"
                or self.context_completeness != "COMPLETE"
                or self.recipient_intent != "JOB_SEEKER"
                or self.language not in {"vi", "en"}
                or self.job_family
                not in {
                    "IT",
                    "SALES",
                    "MARKETING",
                    "FINANCE",
                    "HUMAN_RESOURCES",
                    "CUSTOMER_SUPPORT",
                    "ENGINEERING",
                    "MULTIPLE",
                    "UNKNOWN",
                }
                or self.location_signal
                not in {"REMOTE", "HYBRID", "ONSITE", "PLACE_SPECIFIED", "MULTIPLE", "UNSPECIFIED"}
                or type(self.student_signal) is not bool
                or type(self.part_time_signal) is not bool
                or type(self.internship_signal) is not bool
                or (
                    self.employer_facts is not None
                    and type(self.employer_facts) is not EmployerJobFacts
                )
            ):
                raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")
        elif (
            any(
                value is not None
                for value in (
                    self.account_uuid,
                    self.target_id,
                    self.context_provenance,
                    self.context_completeness,
                    self.recipient_intent,
                    self.language,
                    self.job_family,
                    self.location_signal,
                    self.employer_facts,
                )
            )
            or self.student_signal
            or self.part_time_signal
            or self.internship_signal
        ):
            raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")

    def __repr__(self) -> str:
        return (
            "ReplyProposalInput("
            f"source={self.source}, score={self.score!r}, "
            f"reason_codes={self.reason_codes!r}, candidate_text=<redacted>, "
            "account_uuid=<redacted>, target_id=<redacted>, employer_facts=<redacted>)"
        )


class ReplyProposalGenerator(Protocol):
    """Injectable async generator boundary; production code provides no live client."""

    async def generate_reply(self, request: ReplyProposalInput) -> str: ...


class DeterministicFakeReplyGenerator:
    """Offline generator used by the demo and synthetic tests only."""

    async def generate_reply(self, request: ReplyProposalInput) -> str:
        if request.source == "MANUAL_URL":
            return _generate_manual_queue_reply(request)
        return (
            "Thanks for sharing this opportunity. What skills or experience would help "
            "someone thrive in this role?"
        )


def _generate_manual_queue_reply(request: ReplyProposalInput) -> str:
    """Build a deterministic clarification from bounded evidence and operator facts."""

    facts = request.employer_facts
    role_matches = (
        facts is not None
        and facts.role is not None
        and _role_matches_family(
            facts.role,
            cast(str, request.job_family),
        )
    )
    location_matches = (
        facts is not None
        and facts.location is not None
        and _location_matches_signal(
            facts.location,
            cast(str, request.location_signal),
        )
    )
    if role_matches and location_matches:
        assert facts is not None and facts.role is not None and facts.location is not None
        if request.language == "vi":
            if facts.pay_range is not None:
                contact_question = (
                    " Bạn muốn nhận thông tin liên hệ đã xác nhận không?"
                    if facts.verified_contact_available
                    else " Bạn muốn xem điều kiện ứng tuyển đã xác nhận không?"
                )
                return (
                    f"Vị trí {facts.role} tại {facts.location} có mức lương "
                    f"{facts.pay_range}." + contact_question
                )
            return (
                f"Với vị trí {facts.role} tại {facts.location}, bạn ưu tiên khoảng lương "
                "nào và muốn xem điều kiện ứng tuyển đã xác nhận không?"
            )
        if facts.pay_range is not None:
            contact_question = (
                " Would you like the verified contact details?"
                if facts.verified_contact_available
                else " Would you like the confirmed eligibility details?"
            )
            return (
                f"The {facts.role} role in {facts.location} lists {facts.pay_range}."
                + contact_question
            )
        return (
            f"For the {facts.role} role in {facts.location}, what pay range are you targeting? "
            "Would you like the confirmed eligibility details?"
        )

    if role_matches and facts is not None and facts.role is not None:
        if request.language == "vi":
            return (
                f"Với vị trí {facts.role}, bạn ưu tiên địa điểm làm việc nào "
                "và khoảng lương kỳ vọng ra sao?"
            )
        return f"For the {facts.role} role, what location and pay range do you prefer?"

    family_vi = {
        "IT": "IT",
        "SALES": "kinh doanh",
        "MARKETING": "marketing",
        "FINANCE": "tài chính",
        "HUMAN_RESOURCES": "nhân sự",
        "CUSTOMER_SUPPORT": "chăm sóc khách hàng",
        "ENGINEERING": "kỹ thuật",
        "MULTIPLE": "các nhóm công việc",
        "UNKNOWN": "",
    }
    family_en = {
        "IT": "IT",
        "SALES": "sales",
        "MARKETING": "marketing",
        "FINANCE": "finance",
        "HUMAN_RESOURCES": "human resources",
        "CUSTOMER_SUPPORT": "customer support",
        "ENGINEERING": "engineering",
        "MULTIPLE": "multiple job families",
        "UNKNOWN": "",
    }
    location_vi = {
        "REMOTE": "từ xa",
        "HYBRID": "hybrid",
        "ONSITE": "tại văn phòng",
        "PLACE_SPECIFIED": "",
        "MULTIPLE": "",
        "UNSPECIFIED": "",
    }
    location_en = {
        "REMOTE": "remote",
        "HYBRID": "hybrid",
        "ONSITE": "on-site",
        "PLACE_SPECIFIED": "",
        "MULTIPLE": "",
        "UNSPECIFIED": "",
    }

    if request.language == "vi":
        if request.student_signal and request.part_time_signal:
            return (
                "Bạn đang là sinh viên và tìm việc bán thời gian. "
                "Khung giờ nào phù hợp với lịch học của bạn?"
            )
        family = family_vi[cast(str, request.job_family)]
        location = location_vi[cast(str, request.location_signal)]
        details = " ".join(part for part in (family, location) if part)
        if details:
            return (
                f"Bạn đang tìm việc {details}. Bạn ưu tiên vai trò cụ thể nào "
                "và khoảng lương mong muốn ra sao?"
            )
        return (
            "Bạn có thể chia sẻ nhóm công việc, địa điểm ưu tiên và khoảng lương mong muốn không?"
        )

    if request.student_signal and request.part_time_signal:
        return "For the part-time work you mentioned, what hours would fit your study schedule?"
    family = family_en[cast(str, request.job_family)]
    location = location_en[cast(str, request.location_signal)]
    details = " ".join(part for part in (location, family) if part)
    if details:
        return f"For the {details} work you mentioned, which role and pay range are you targeting?"
    return "Which job family, preferred location, and pay range are you targeting?"


def _role_matches_family(role: str, family: str) -> bool:
    markers = {
        "IT": ("it", "software", "developer", "programmer", "python", "data", "qa", "tester"),
        "SALES": ("sales", "kinh doanh", "bán hàng", "account executive"),
        "MARKETING": ("marketing", "tiếp thị", "truyền thông", "social media"),
        "FINANCE": ("finance", "kế toán", "tài chính", "accountant"),
        "HUMAN_RESOURCES": ("human resources", "nhân sự", "hr", "recruiter"),
        "CUSTOMER_SUPPORT": ("customer support", "customer service", "chăm sóc khách hàng"),
        "ENGINEERING": ("engineering", "kỹ sư", "mechanical", "electrical engineer"),
    }
    family_markers = markers.get(family)
    if family_markers is None:
        return False
    normalized_role = _normalize_evidence_label(role)
    return any(
        f" {_normalize_evidence_label(marker)} " in f" {normalized_role} "
        for marker in family_markers
    )


def _location_matches_signal(location: str, signal: str) -> bool:
    markers = {
        "REMOTE": ("remote", "wfh", "work from home", "làm việc từ xa", "làm từ xa"),
        "HYBRID": ("hybrid", "kết hợp", "làm việc kết hợp"),
        "ONSITE": ("onsite", "on site", "tại văn phòng", "làm trực tiếp"),
    }
    signal_markers = markers.get(signal)
    if signal == "UNSPECIFIED":
        return bool(location.strip())
    if signal_markers is None:
        return False
    normalized_location = _normalize_evidence_label(location)
    return any(
        f" {_normalize_evidence_label(marker)} " in f" {normalized_location} "
        for marker in signal_markers
    )


def _normalize_evidence_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in normalized).split()
    )


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticReplyProposal:
    """An ephemeral proposal bound to the exact in-memory shortlist candidate."""

    _bound_candidate: NurtureShortlistCandidate = field(repr=False)
    suggested_reply: str = field(repr=False)
    source: Literal["FAKE_SYNTHETIC"] = "FAKE_SYNTHETIC"
    status: Literal["PROPOSED_FOR_HUMAN_REVIEW"] = "PROPOSED_FOR_HUMAN_REVIEW"
    approval_status: Literal["NOT_APPROVED"] = "NOT_APPROVED"
    delivery_status: Literal["NOT_SENT"] = "NOT_SENT"

    def __post_init__(self) -> None:
        if (
            type(self._bound_candidate) is not NurtureShortlistCandidate
            or type(self._bound_candidate.fingerprint) is not str
            or _FINGERPRINT.fullmatch(self._bound_candidate.fingerprint) is None
            or self._bound_candidate.status != "ELIGIBLE"
            or self._bound_candidate.action_hint != "REVIEW_CANDIDATE"
            or self._bound_candidate.text is None
            or not self._bound_candidate.text.strip()
            or self._bound_candidate.text_truncated
            or type(self.source) is not str
            or self.source != "FAKE_SYNTHETIC"
            or type(self.status) is not str
            or self.status != "PROPOSED_FOR_HUMAN_REVIEW"
            or type(self.approval_status) is not str
            or self.approval_status != "NOT_APPROVED"
            or type(self.delivery_status) is not str
            or self.delivery_status != "NOT_SENT"
        ):
            raise ReplyProposalError("TARGET_BINDING_MISMATCH")
        _validate_reply(self.suggested_reply)

    @property
    def target_fingerprint(self) -> str:
        return self._bound_candidate.fingerprint

    @property
    def candidate_source(self) -> NurtureDiscoverySource | Literal["INBOUND_REPLY"]:
        return self._bound_candidate.source

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return self._bound_candidate.reason_codes

    def is_bound_to(self, candidate: NurtureShortlistCandidate) -> bool:
        """Return whether this proposal retains the exact selected candidate object."""

        return self._bound_candidate is candidate

    def __repr__(self) -> str:
        return (
            "SyntheticReplyProposal("
            "target_fingerprint=<redacted>, source=FAKE_SYNTHETIC, "
            "status=PROPOSED_FOR_HUMAN_REVIEW, approval_status=NOT_APPROVED, "
            "delivery_status=NOT_SENT, suggested_reply=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HumanReplyPreview:
    """Preview carrying the same candidate object that was used for generation."""

    candidate: NurtureShortlistCandidate = field(repr=False)
    proposal: SyntheticReplyProposal = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not NurtureShortlistCandidate
            or type(self.proposal) is not SyntheticReplyProposal
            or not self.proposal.is_bound_to(self.candidate)
            or self.proposal.target_fingerprint != self.candidate.fingerprint
        ):
            raise ReplyProposalError("TARGET_BINDING_MISMATCH")

    def __repr__(self) -> str:
        return "HumanReplyPreview(candidate=<bound>, proposal=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class HumanReplyEditorialSession:
    """Ephemeral edits tied to the exact proposal target; never an approval."""

    preview: HumanReplyPreview = field(repr=False)
    target_fingerprint: str = field(repr=False)
    current_reply: str | None = field(repr=False)
    editorial_disposition: Literal["EDITING", "KEPT_FOR_SESSION", "DISCARDED"] = "EDITING"

    def __post_init__(self) -> None:
        if (
            type(self.preview) is not HumanReplyPreview
            or self.preview.candidate.status != "ELIGIBLE"
            or self.preview.candidate.action_hint != "REVIEW_CANDIDATE"
            or type(self.target_fingerprint) is not str
            or self.target_fingerprint != self.preview.candidate.fingerprint
            or self.target_fingerprint != self.preview.proposal.target_fingerprint
            or not self.preview.proposal.is_bound_to(self.preview.candidate)
            or type(self.editorial_disposition) is not str
            or self.editorial_disposition not in {"EDITING", "KEPT_FOR_SESSION", "DISCARDED"}
        ):
            raise ReplyProposalError("INVALID_EDITOR_SESSION")
        if self.editorial_disposition == "DISCARDED":
            if self.current_reply is not None:
                raise ReplyProposalError("INVALID_EDITOR_SESSION")
        else:
            _validate_reply(self.current_reply, error_code="INVALID_EDITED_REPLY")

    @classmethod
    def begin(cls, preview: HumanReplyPreview) -> HumanReplyEditorialSession:
        if type(preview) is not HumanReplyPreview:
            raise ReplyProposalError("INVALID_EDITOR_SESSION")
        return cls(preview, preview.proposal.target_fingerprint, preview.proposal.suggested_reply)

    @property
    def candidate(self) -> NurtureShortlistCandidate:
        return self.preview.candidate

    @property
    def approval_status(self) -> Literal["NOT_APPROVED_FOR_SENDING"]:
        return "NOT_APPROVED_FOR_SENDING"

    @property
    def delivery_status(self) -> Literal["NOT_SENT"]:
        return "NOT_SENT"

    def is_bound_to(self, candidate: NurtureShortlistCandidate) -> bool:
        return (
            type(candidate) is NurtureShortlistCandidate
            and self.preview.candidate is candidate
            and self.target_fingerprint == candidate.fingerprint
            and self.preview.proposal.is_bound_to(candidate)
        )

    def edit(self, value: object) -> HumanReplyEditorialSession:
        self._require_editing()
        _validate_reply(value, error_code="INVALID_EDITED_REPLY")
        return HumanReplyEditorialSession(
            self.preview,
            self.target_fingerprint,
            cast(str, value),
            "EDITING",
        )

    def keep_for_session(self) -> HumanReplyEditorialSession:
        self._require_editing()
        return HumanReplyEditorialSession(
            self.preview,
            self.target_fingerprint,
            self.current_reply,
            "KEPT_FOR_SESSION",
        )

    def discard(self) -> HumanReplyEditorialSession:
        self._require_editing()
        return HumanReplyEditorialSession(
            self.preview,
            self.target_fingerprint,
            None,
            "DISCARDED",
        )

    def _require_editing(self) -> None:
        if self.editorial_disposition != "EDITING":
            raise ReplyProposalError("EDITOR_SESSION_CLOSED")

    def __repr__(self) -> str:
        return (
            "HumanReplyEditorialSession("
            "candidate=<bound>, target_fingerprint=<redacted>, "
            f"editorial_disposition={self.editorial_disposition}, current_reply=<redacted>, "
            "approval_status=NOT_APPROVED_FOR_SENDING, delivery_status=NOT_SENT)"
        )


@dataclass(frozen=True, slots=True)
class ReplyQualityAdvisory:
    """Bounded safe codes from a soft heuristic; it contains no reply content."""

    outcome: ReplyQualityOutcome
    flags: tuple[ReplyQualityFlag, ...]

    def __post_init__(self) -> None:
        if (
            type(self.outcome) is not str
            or self.outcome not in {"REVIEW_RECOMMENDED", "NO_OBVIOUS_ISSUE_DETECTED"}
            or type(self.flags) is not tuple
            or len(self.flags) > 2
            or any(
                type(flag) is not str or flag not in {"LOW_INFORMATION", "DUPLICATE_WORDING"}
                for flag in self.flags
            )
            or len(set(self.flags)) != len(self.flags)
            or (self.outcome == "REVIEW_RECOMMENDED") != bool(self.flags)
        ):
            raise ReplyProposalError("INVALID_QUALITY_ADVISORY")


def evaluate_synthetic_reply_quality(
    session: HumanReplyEditorialSession,
) -> ReplyQualityAdvisory:
    """Flag a few low-information English phrases on the exact bound editor session.

    This check does not assess correctness, relevance, safety, or factual support.
    """

    if type(session) is not HumanReplyEditorialSession:
        raise ReplyProposalError("INVALID_EDITOR_SESSION")
    candidate = session.candidate
    context = candidate.text
    reply = session.current_reply
    if not session.is_bound_to(candidate):
        raise ReplyProposalError("TARGET_BINDING_MISMATCH")
    if type(context) is not str or not context.strip() or len(context) > _MAX_REPLY_CHARS:
        raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")
    if reply is None or session.editorial_disposition == "DISCARDED":
        raise ReplyProposalError("INVALID_EDITOR_SESSION")
    _validate_reply(reply, error_code="INVALID_EDITED_REPLY")

    return evaluate_reply_quality_text(reply)


def evaluate_reply_quality_text(value: object) -> ReplyQualityAdvisory:
    """Return bounded human-review advisories without judging factual correctness."""

    _validate_reply(value, error_code="INVALID_EDITED_REPLY")
    normalized = unicodedata.normalize("NFKC", cast(str, value)).casefold()
    normalized_phrase = " ".join(_QUALITY_WORD_SEPARATOR.sub(" ", normalized).split())
    flags: list[ReplyQualityFlag] = []
    if normalized_phrase in _LOW_INFORMATION_PHRASES:
        flags.append("LOW_INFORMATION")
    sentence_tokens = [
        " ".join(_QUALITY_WORD_SEPARATOR.sub(" ", sentence).split())
        for sentence in re.split(r"[.!?\u3002]+", normalized)
    ]
    sentence_tokens = [sentence for sentence in sentence_tokens if len(sentence.split()) >= 3]
    if len(sentence_tokens) != len(set(sentence_tokens)):
        flags.append("DUPLICATE_WORDING")
    stable_flags = tuple(flags)
    outcome: ReplyQualityOutcome = (
        "REVIEW_RECOMMENDED" if stable_flags else "NO_OBVIOUS_ISSUE_DETECTED"
    )
    return ReplyQualityAdvisory(outcome, stable_flags)


def validate_reply_text(value: object) -> str:
    """Validate a generated or edited reply under the existing S3 500-char bound."""

    _validate_reply(value)
    return cast(str, value)


async def generate_bounded_reply(
    request: ReplyProposalInput,
    generator: ReplyProposalGenerator,
    *,
    timeout_seconds: float = 5.0,
) -> str:
    """Call the injected generator under a bounded timeout and safe error contract."""

    if (
        type(request) is not ReplyProposalInput
        or type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= _MAX_GENERATION_TIMEOUT_SECONDS
    ):
        raise ReplyProposalError("GENERATOR_FAILED")
    try:
        generated = await asyncio.wait_for(
            generator.generate_reply(request),
            timeout=float(timeout_seconds),
        )
    except TimeoutError:
        raise ReplyProposalError("PROPOSAL_TIMEOUT") from None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — exception details may contain private prompt data.
        raise ReplyProposalError("GENERATOR_FAILED") from None
    return validate_reply_text(generated)


async def create_human_reply_preview(
    shortlist: NurtureShortlistResult,
    selection: int,
    generator: ReplyProposalGenerator,
    *,
    timeout_seconds: float = 5.0,
) -> HumanReplyPreview:
    """Generate one bounded proposal for a selected eligible candidate, without writes."""

    if (
        type(shortlist) is not NurtureShortlistResult
        or type(shortlist.candidates) is not tuple
        or not 1 <= len(shortlist.candidates) <= _MAX_SHORTLIST_CANDIDATES
        or any(type(item) is not NurtureShortlistCandidate for item in shortlist.candidates)
    ):
        raise ReplyProposalError("INVALID_SHORTLIST")
    if type(selection) is not int or not 1 <= selection <= len(shortlist.candidates):
        raise ReplyProposalError("INVALID_SELECTION")
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= _MAX_GENERATION_TIMEOUT_SECONDS
    ):
        raise ReplyProposalError("GENERATOR_FAILED")

    candidate = shortlist.candidates[selection - 1]
    if candidate.status != "ELIGIBLE" or candidate.action_hint != "REVIEW_CANDIDATE":
        raise ReplyProposalError("CANDIDATE_NOT_ELIGIBLE")
    if candidate.text is None or not candidate.text.strip() or candidate.text_truncated:
        raise ReplyProposalError("CANDIDATE_CONTEXT_UNAVAILABLE")

    request = ReplyProposalInput(
        source=candidate.source,
        score=candidate.score,
        reason_codes=candidate.reason_codes,
        candidate_text=candidate.text,
    )
    generated = await generate_bounded_reply(
        request,
        generator,
        timeout_seconds=timeout_seconds,
    )
    proposal = SyntheticReplyProposal(candidate, generated)
    return HumanReplyPreview(candidate, proposal)


def _validate_reply(
    value: object,
    *,
    error_code: Literal["INVALID_PROPOSAL_OUTPUT", "INVALID_EDITED_REPLY"] = (
        "INVALID_PROPOSAL_OUTPUT"
    ),
) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_REPLY_CHARS
        or not value.strip()
        or value != value.strip()
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in value
        )
    ):
        raise ReplyProposalError(error_code)
