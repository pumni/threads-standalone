"""Offline, deterministic extraction of synthetic or operator-supplied job text.

This module performs no I/O. It does not fetch URLs, verify authors, persist
records, or authorize contact processing. Contact values are parsed only from
synthetic fixtures; operator-supplied text keeps contact fields unassessed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Generic, Literal, Never, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

MAX_TEXT_CODEPOINTS = 10_000
MAX_ROLE_MATCHES = 20
MAX_TARGET_URL_LENGTH = 2_048
MAX_LOCAL_ID_LENGTH = 128
EXTRACTION_VERSION = "bj01-v1"


class ObservationSource(StrEnum):
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    MANUAL_URL = "MANUAL_URL"
    BROWSER_UI = "BROWSER_UI"


class TextProvenance(StrEnum):
    SYNTHETIC_FIXTURE_TEXT = "SYNTHETIC_FIXTURE_TEXT"
    OPERATOR_SUPPLIED_TEXT = "OPERATOR_SUPPLIED_TEXT"
    BROWSER_UI_VISIBLE_TEXT = "BROWSER_UI_VISIBLE_TEXT"
    NONE = "NONE"


class TextCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class CompletenessBasis(StrEnum):
    OPERATOR_ASSERTION = "OPERATOR_ASSERTION"
    FIXTURE_DECLARED = "FIXTURE_DECLARED"
    BROWSER_VISIBLE_VERIFIED = "BROWSER_VISIBLE_VERIFIED"
    UNKNOWN = "UNKNOWN"


class PostKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    REPOST = "REPOST"
    QUOTE = "QUOTE"
    REPLY = "REPLY"
    UNKNOWN = "UNKNOWN"


class AuthorEvidenceStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    UNVERIFIED = "UNVERIFIED"
    BROWSER_VISIBLE_VERIFIED = "BROWSER_VISIBLE_VERIFIED"


class VacancyClassification(StrEnum):
    RECRUITER_VACANCY = "RECRUITER_VACANCY"
    JOB_SEEKER = "JOB_SEEKER"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceState(StrEnum):
    SOURCE_SPAN = "SOURCE_SPAN"
    ABSENT = "ABSENT"
    AMBIGUOUS = "AMBIGUOUS"


class ExtractionStatus(StrEnum):
    EXTRACTED = "EXTRACTED"
    PARTIAL = "PARTIAL"
    NON_VACANCY = "NON_VACANCY"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


class ReasonCode(StrEnum):
    SOURCE_SYNTHETIC_FIXTURE = "SOURCE_SYNTHETIC_FIXTURE"
    SOURCE_MANUAL_URL = "SOURCE_MANUAL_URL"
    URL_ONLY_NO_TEXT = "URL_ONLY_NO_TEXT"
    BROWSER_SOURCE_DISABLED = "BROWSER_SOURCE_DISABLED"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    AUTHOR_PROVENANCE_MISMATCH = "AUTHOR_PROVENANCE_MISMATCH"
    INVALID_OBSERVATION_IDS = "INVALID_OBSERVATION_IDS"
    INVALID_ACCOUNT_UUID = "INVALID_ACCOUNT_UUID"
    INVALID_CANONICAL_TARGET = "INVALID_CANONICAL_TARGET"
    MISSING_POST_REVISION_ID = "MISSING_POST_REVISION_ID"
    INVALID_POLICY = "INVALID_POLICY"
    TEXT_EMPTY = "TEXT_EMPTY"
    TEXT_LIMIT_EXCEEDED = "TEXT_LIMIT_EXCEEDED"
    TEXT_PARTIAL = "TEXT_PARTIAL"
    TEXT_COMPLETENESS_UNKNOWN = "TEXT_COMPLETENESS_UNKNOWN"
    JOB_SEEKER_INTENT = "JOB_SEEKER_INTENT"
    INTENT_CONFLICT = "INTENT_CONFLICT"
    RECRUITMENT_CUE_FOUND = "RECRUITMENT_CUE_FOUND"
    ROLE_EVIDENCE_FOUND = "ROLE_EVIDENCE_FOUND"
    ROLE_NOT_IDENTIFIED = "ROLE_NOT_IDENTIFIED"
    ROLE_ID_AMBIGUOUS = "ROLE_ID_AMBIGUOUS"
    ROLE_LIMIT_EXCEEDED = "ROLE_LIMIT_EXCEEDED"
    NON_VACANCY_TEXT = "NON_VACANCY_TEXT"
    POST_KIND_UNSUPPORTED = "POST_KIND_UNSUPPORTED"
    SALARY_AMBIGUOUS = "SALARY_AMBIGUOUS"
    LOCATION_AMBIGUOUS = "LOCATION_AMBIGUOUS"
    CONTACT_EXTRACTION_BLOCKED = "CONTACT_EXTRACTION_BLOCKED"
    AUTHOR_UNVERIFIED = "AUTHOR_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Half-open Python Unicode code-point offsets into the exact input text."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("INVALID_SOURCE_SPAN")


_EvidenceValue_co = TypeVar("_EvidenceValue_co", covariant=True)


@dataclass(frozen=True, slots=True, repr=False)
class FieldEvidence(Generic[_EvidenceValue_co]):  # noqa: UP046
    """Typed leaf evidence; repr deliberately hides raw and normalized values."""

    state: EvidenceState
    value: _EvidenceValue_co | None = field(default=None, repr=False)
    raw_value: str | None = field(default=None, repr=False)
    spans: tuple[SourceSpan, ...] = ()

    def __repr__(self) -> str:
        return f"FieldEvidence(state={self.state.value!r}, span_count={len(self.spans)}, value=<redacted>)"


@dataclass(frozen=True, slots=True)
class ExtractionPolicy:
    """Local parser bounds; callers may lower, but never raise, the text cap."""

    max_text_codepoints: int = MAX_TEXT_CODEPOINTS
    max_role_matches: int = MAX_ROLE_MATCHES

    def __post_init__(self) -> None:
        if not 1 <= self.max_text_codepoints <= MAX_TEXT_CODEPOINTS:
            raise ValueError("INVALID_POLICY")
        if not 1 <= self.max_role_matches <= MAX_ROLE_MATCHES:
            raise ValueError("INVALID_POLICY")


_DEFAULT_POLICY = ExtractionPolicy()


@dataclass(frozen=True, slots=True, repr=False)
class PostObservation:
    """Typed caller-supplied metadata and text; no field is fetched by this module."""

    observation_id: str = field(repr=False)
    account_uuid: str = field(repr=False)
    canonical_post_url: str = field(repr=False)
    source: ObservationSource
    text_provenance: TextProvenance
    text_completeness: TextCompleteness
    completeness_basis: CompletenessBasis
    post_revision_id: str | None = field(repr=False)
    observed_text: str | None = field(repr=False)
    text_truncated: bool = False
    post_kind: PostKind = PostKind.ORIGINAL
    observed_author: str | None = field(default=None, repr=False)
    author_status: AuthorEvidenceStatus = AuthorEvidenceStatus.UNKNOWN

    def __repr__(self) -> str:
        text_state = "missing" if self.observed_text is None else "<redacted>"
        return (
            "PostObservation("
            f"source={self.source!s}, text_provenance={self.text_provenance!s}, "
            f"text_completeness={self.text_completeness!s}, text={text_state})"
        )


@dataclass(frozen=True, slots=True)
class LocationFields:
    raw: FieldEvidence[tuple[str, ...]]
    address: FieldEvidence[str]
    district: FieldEvidence[str]
    province_city: FieldEvidence[str]


@dataclass(frozen=True, slots=True)
class SalaryFields:
    raw: FieldEvidence[str]
    amount_min: FieldEvidence[int]
    amount_max: FieldEvidence[int]
    currency: FieldEvidence[str]
    pay_period: FieldEvidence[str]
    commission: FieldEvidence[str]


@dataclass(frozen=True, slots=True)
class ContactFields:
    phone: FieldEvidence[str]
    zalo: FieldEvidence[str]
    email: FieldEvidence[str]
    application_url: FieldEvidence[str]


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentJobPostV1:
    schema_version: Literal[1]
    post_identity_key: tuple[str, str] = field(repr=False)
    post_revision_id: str = field(repr=False)
    observation_id: str = field(repr=False)
    account_uuid: str = field(repr=False)
    vacancy_id: str = field(repr=False)
    vacancy_revision_id: str = field(repr=False)
    role_anchor: str = field(repr=False)
    classification: VacancyClassification
    extraction_version: str
    confidence: Confidence
    employer: FieldEvidence[str]
    role_title: FieldEvidence[str]
    role_category: FieldEvidence[str]
    headcount: FieldEvidence[int]
    location: LocationFields
    work_mode: FieldEvidence[str]
    schedule: FieldEvidence[str]
    salary: SalaryFields
    experience: FieldEvidence[str]
    requirements: FieldEvidence[tuple[str, ...]]
    benefits: FieldEvidence[tuple[str, ...]]
    contacts: ContactFields
    reason_codes: tuple[ReasonCode, ...]

    def __repr__(self) -> str:
        return (
            "RecruitmentJobPostV1("
            f"classification={self.classification.value!r}, confidence={self.confidence.value!r}, "
            f"reason_codes={tuple(code.value for code in self.reason_codes)!r}, "
            "field_values=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ExtractionResult:
    status: ExtractionStatus
    classification: VacancyClassification
    confidence: Confidence
    reason_codes: tuple[ReasonCode, ...]
    records: tuple[RecruitmentJobPostV1, ...]
    post_identity_key: tuple[str, str] | None = field(default=None, repr=False)
    post_revision_id: str | None = field(default=None, repr=False)
    observation_id: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "ExtractionResult("
            f"status={self.status.value!r}, classification={self.classification.value!r}, "
            f"confidence={self.confidence.value!r}, "
            f"reason_codes={tuple(code.value for code in self.reason_codes)!r}, "
            f"record_count={len(self.records)})"
        )


@dataclass(frozen=True, slots=True)
class _MappedText:
    value: str
    source_ranges: tuple[tuple[int, int], ...]

    def source_span(self, start: int, end: int) -> SourceSpan:
        if start < 0 or end <= start or end > len(self.source_ranges):
            raise ValueError("INVALID_SOURCE_SPAN")
        return SourceSpan(self.source_ranges[start][0], self.source_ranges[end - 1][1])


@dataclass(frozen=True, slots=True)
class _RoleOccurrence:
    key: str
    label: str
    category: str
    span: SourceSpan
    location_anchor: str | None = None


@dataclass(frozen=True, slots=True)
class _SalaryCandidate:
    span: SourceSpan
    raw: str
    amount_min: int | None
    amount_min_span: SourceSpan | None
    amount_max: int | None
    amount_max_span: SourceSpan | None
    period: str | None
    period_span: SourceSpan | None
    currency: str | None
    currency_span: SourceSpan | None
    commission: str | None
    commission_span: SourceSpan | None


_ROLE_ALIASES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "sales",
        "Sales",
        "BUSINESS_SALES",
        (
            "nhân viên kinh doanh",
            "nhan vien kinh doanh",
            "nhân viên sales",
            "nhan vien sales",
            "sales",
        ),
    ),
    (
        "accounting",
        "Kế toán",
        "ACCOUNTING",
        ("nhân viên kế toán", "nhan vien ke toan", "kế toán", "ke toan"),
    ),
    (
        "labor",
        "Công nhân",
        "MANUAL_LABOR",
        ("công nhân", "cong nhan", "lao động phổ thông", "lao dong pho thong"),
    ),
    (
        "retail_sales",
        "CTV bán hàng",
        "RETAIL",
        ("cộng tác viên bán hàng", "cong tac vien ban hang", "ctv bán hàng", "ctv ban hang"),
    ),
    (
        "intern_it",
        "Thực tập sinh IT",
        "INTERN",
        ("thực tập sinh it", "thuc tap sinh it", "intern it"),
    ),
    (
        "it",
        "IT",
        "TECHNOLOGY",
        ("lập trình viên", "lap trinh vien", "nhân viên it", "nhan vien it", "it"),
    ),
    (
        "marketing",
        "Marketing",
        "MARKETING",
        ("nhân viên marketing", "nhan vien marketing", "marketing", "tiếp thị", "tiep thi"),
    ),
    (
        "service",
        "Phục vụ",
        "SERVICE",
        ("nhân viên phục vụ", "nhan vien phuc vu", "phục vụ", "phuc vu"),
    ),
    (
        "security",
        "Bảo vệ",
        "SECURITY",
        ("nhân viên bảo vệ", "nhan vien bao ve", "bảo vệ", "bao ve"),
    ),
    ("driver", "Tài xế", "TRANSPORT", ("tài xế", "tai xe", "lái xe", "lai xe")),
    ("generic_staff", "Nhân viên", "UNKNOWN", ("nhân viên", "nhan vien")),
    ("generic_intern", "Thực tập sinh", "INTERN", ("thực tập sinh", "thuc tap sinh")),
    (
        "generic_collaborator",
        "Cộng tác viên",
        "CONTRACT",
        ("cộng tác viên", "cong tac vien", "ctv"),
    ),
)

_RECRUITER_CUES = (
    "cần tuyển",
    "can tuyen",
    "đang tuyển",
    "dang tuyen",
    "tuyển gấp",
    "tuyen gap",
    "tuyển dụng",
    "tuyen dung",
    "cần nhân sự",
    "can nhan su",
    "đang tìm nhân sự",
    "dang tim nhan su",
    "tìm nhân viên",
    "tim nhan vien",
    "tìm ứng viên",
    "tim ung vien",
    "tuyển",
    "tuyen",
)
_RECRUITER_FALSE_POSITIVES = (
    "tuyển sinh",
    "tuyen sinh",
    "đội tuyển",
    "doi tuyen",
    "tuyển chọn",
    "tuyen chon",
)
_JOB_SEEKER_CUES = (
    "em đang tìm việc",
    "em dang tim viec",
    "em tìm việc",
    "em tim viec",
    "mình đang tìm việc",
    "minh dang tim viec",
    "tôi đang tìm việc",
    "toi dang tim viec",
    "cần tìm việc",
    "can tim viec",
    "cần việc làm",
    "can viec lam",
    "em xin việc",
    "em xin viec",
    "tôi xin việc",
    "toi xin viec",
    "ứng tuyển vị trí",
    "ung tuyen vi tri",
)

_FIELD_BOUNDARIES = (
    "lương",
    "luong",
    "thu nhập",
    "thu nhap",
    "tại",
    "tai",
    "ở",
    "địa chỉ",
    "dia chi",
    "yêu cầu",
    "yeu cau",
    "liên hệ",
    "lien he",
    "sđt",
    "sdt",
    "ca đêm",
    "ca dem",
    "ca ngày",
    "ca ngay",
    "kinh nghiệm",
    "kinh nghiem",
    "phúc lợi",
    "phuc loi",
)

_CITY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TP.HCM", ("thành phố hồ chí minh", "thanh pho ho chi minh", "tp.hcm", "tp hcm", "tphcm")),
    ("Hà Nội", ("hà nội", "ha noi")),
    ("Đà Nẵng", ("đà nẵng", "da nang")),
    ("Bình Dương", ("bình dương", "binh duong")),
    ("Đồng Nai", ("đồng nai", "dong nai")),
    ("Cần Thơ", ("cần thơ", "can tho")),
)

_WORK_MODE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("REMOTE", ("làm từ xa", "lam tu xa", "remote", "work from home")),
    ("ONSITE", ("tại văn phòng", "tai van phong", "làm tại xưởng", "lam tai xuong", "onsite")),
)

_SCHEDULE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("NIGHT_SHIFT", ("ca đêm", "ca dem", "ca tối", "ca toi")),
    ("DAY_SHIFT", ("ca ngày", "ca ngay")),
    ("FULL_TIME", ("toàn thời gian", "toan thoi gian", "full-time", "full time")),
    ("PART_TIME", ("bán thời gian", "ban thoi gian", "part-time", "part time")),
)

_AMOUNT = r"\d{1,4}(?:[.,]\d{1,2})?"
_RANGE_PATTERN = re.compile(
    rf"(?P<first>{_AMOUNT})\s*(?P<unit1>triệu|tr|k)?\s*(?:[-–~]|đến)\s*"
    rf"(?P<second>{_AMOUNT})\s*(?P<unit2>triệu|tr|k)(?!\w)"
)
_FIXED_PATTERN = re.compile(rf"(?P<number>{_AMOUNT})\s*(?P<unit>triệu|tr|k)(?!\w)")
_NEGOTIATED_PHRASES = (
    "lương thỏa thuận",
    "luong thoa thuan",
    "mức lương thỏa thuận",
    "muc luong thoa thuan",
)
_PERIOD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MONTH", ("tháng", "thang", "tháng/tháng")),
    ("HOUR", ("giờ", "gio", "h")),
    ("DAY", ("ngày", "ngay")),
    ("WEEK", ("tuần", "tuan")),
    ("YEAR", ("năm", "nam")),
)
_CURRENCY_ALIASES = ("vnd", "vnđ", "đồng", "dong", "₫")
_COMMISSION_ALIASES = ("%hh", "% hoa hồng", "% hoa hong", "hoa hồng", "hoa hong", "commission")


def extract_vietnamese_vacancies(
    observation: PostObservation,
    policy: ExtractionPolicy = _DEFAULT_POLICY,
) -> ExtractionResult:
    """Classify and parse one offline observation without side effects.

    The caller supplies all text and source metadata. Browser observations are
    disabled, URL-only observations stay unknown, and contacts are considered
    only for explicitly synthetic fixtures.
    """

    validation = _validate_observation(observation)
    if validation is not None:
        status, reason = validation
        return _result(status, VacancyClassification.UNKNOWN, Confidence.UNKNOWN, (reason,))

    post_identity_key = (observation.account_uuid, observation.canonical_post_url)
    if observation.observed_text is None:
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.SOURCE_MANUAL_URL, ReasonCode.URL_ONLY_NO_TEXT),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    text = observation.observed_text
    if len(text) > policy.max_text_codepoints:
        return _result(
            ExtractionStatus.PARTIAL,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.TEXT_LIMIT_EXCEEDED,),
            post_identity_key=post_identity_key,
            observation=observation,
        )
    if not text.strip():
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.TEXT_EMPTY,),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    if observation.post_kind is not PostKind.ORIGINAL:
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.POST_KIND_UNSUPPORTED,),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    view = _mapped_nfc_casefold(text)
    recruiter_cues = _find_recruiter_cues(view)
    seeker_cues = _find_phrases(view, _JOB_SEEKER_CUES)
    is_partial = observation.text_completeness is not TextCompleteness.COMPLETE

    if seeker_cues and recruiter_cues:
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.INTENT_CONFLICT,),
            post_identity_key=post_identity_key,
            observation=observation,
        )
    if seeker_cues:
        return _result(
            ExtractionStatus.NON_VACANCY,
            VacancyClassification.JOB_SEEKER,
            Confidence.HIGH,
            (ReasonCode.JOB_SEEKER_INTENT,),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    role_occurrences = _find_roles(view, recruiter_cues)
    if not recruiter_cues:
        if observation.text_completeness is TextCompleteness.COMPLETE:
            return _result(
                ExtractionStatus.NON_VACANCY,
                VacancyClassification.OTHER,
                Confidence.HIGH,
                (ReasonCode.NON_VACANCY_TEXT,),
                post_identity_key=post_identity_key,
                observation=observation,
            )
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.TEXT_PARTIAL if is_partial else ReasonCode.TEXT_COMPLETENESS_UNKNOWN,),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    if not role_occurrences:
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.RECRUITMENT_CUE_FOUND, ReasonCode.ROLE_NOT_IDENTIFIED),
            post_identity_key=post_identity_key,
            observation=observation,
        )
    if len(role_occurrences) > policy.max_role_matches:
        return _result(
            ExtractionStatus.REJECTED,
            VacancyClassification.UNKNOWN,
            Confidence.UNKNOWN,
            (ReasonCode.ROLE_LIMIT_EXCEEDED,),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    accepted_roles, ambiguous_roles = _resolve_duplicate_roles(view, role_occurrences)
    reason_codes = [
        ReasonCode.SOURCE_SYNTHETIC_FIXTURE
        if observation.source is ObservationSource.SYNTHETIC_FIXTURE
        else ReasonCode.SOURCE_MANUAL_URL
    ]
    reason_codes.append(ReasonCode.RECRUITMENT_CUE_FOUND)
    if accepted_roles:
        reason_codes.append(ReasonCode.ROLE_EVIDENCE_FOUND)
    if ambiguous_roles:
        reason_codes.append(ReasonCode.ROLE_ID_AMBIGUOUS)
    if observation.author_status is AuthorEvidenceStatus.UNVERIFIED:
        reason_codes.append(ReasonCode.AUTHOR_UNVERIFIED)
    if observation.text_completeness is TextCompleteness.PARTIAL or observation.text_truncated:
        reason_codes.append(ReasonCode.TEXT_PARTIAL)
    elif observation.text_completeness is TextCompleteness.UNKNOWN:
        reason_codes.append(ReasonCode.TEXT_COMPLETENESS_UNKNOWN)

    if not accepted_roles:
        return _result(
            ExtractionStatus.UNKNOWN,
            VacancyClassification.RECRUITER_VACANCY,
            Confidence.UNKNOWN,
            tuple(reason_codes),
            post_identity_key=post_identity_key,
            observation=observation,
        )

    records = tuple(
        _build_record(
            observation=observation,
            text=text,
            view=view,
            role=role,
            completeness=observation.text_completeness,
            policy=policy,
            reason_codes=tuple(reason_codes),
        )
        for role in accepted_roles
    )
    fields_ambiguous = any(record.confidence is Confidence.MEDIUM for record in records)
    confidence = (
        Confidence.MEDIUM if is_partial or ambiguous_roles or fields_ambiguous else Confidence.HIGH
    )
    status = (
        ExtractionStatus.PARTIAL
        if is_partial or ambiguous_roles or fields_ambiguous
        else ExtractionStatus.EXTRACTED
    )
    return _result(
        status,
        VacancyClassification.RECRUITER_VACANCY,
        confidence,
        tuple(reason_codes),
        records,
        post_identity_key=post_identity_key,
        observation=observation,
    )


def _validate_observation(
    observation: PostObservation,
) -> tuple[ExtractionStatus, ReasonCode] | None:
    if observation.source is ObservationSource.BROWSER_UI:
        return ExtractionStatus.REJECTED, ReasonCode.BROWSER_SOURCE_DISABLED

    if not _valid_local_id(observation.observation_id):
        return ExtractionStatus.REJECTED, ReasonCode.INVALID_OBSERVATION_IDS
    if not _valid_https_target(observation.canonical_post_url):
        return ExtractionStatus.REJECTED, ReasonCode.INVALID_CANONICAL_TARGET
    try:
        account_id = UUID(observation.account_uuid)
    except ValueError, TypeError, AttributeError:
        return ExtractionStatus.REJECTED, ReasonCode.INVALID_ACCOUNT_UUID
    if str(account_id) != observation.account_uuid:
        return ExtractionStatus.REJECTED, ReasonCode.INVALID_ACCOUNT_UUID

    if observation.author_status is AuthorEvidenceStatus.BROWSER_VISIBLE_VERIFIED:
        return ExtractionStatus.REJECTED, ReasonCode.AUTHOR_PROVENANCE_MISMATCH
    if (
        observation.observed_author is not None
        and observation.author_status is AuthorEvidenceStatus.UNKNOWN
    ):
        return ExtractionStatus.REJECTED, ReasonCode.AUTHOR_PROVENANCE_MISMATCH

    if observation.source is ObservationSource.MANUAL_URL:
        if observation.text_provenance is TextProvenance.NONE:
            if (
                observation.observed_text is None
                and observation.text_completeness is TextCompleteness.UNKNOWN
                and observation.completeness_basis is CompletenessBasis.UNKNOWN
                and observation.post_revision_id is None
                and not observation.text_truncated
            ):
                return None
            return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH
        if (
            observation.text_provenance is not TextProvenance.OPERATOR_SUPPLIED_TEXT
            or observation.observed_text is None
            or observation.completeness_basis is not CompletenessBasis.OPERATOR_ASSERTION
        ):
            return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH
    elif observation.source is ObservationSource.SYNTHETIC_FIXTURE:
        if (
            observation.text_provenance is not TextProvenance.SYNTHETIC_FIXTURE_TEXT
            or observation.observed_text is None
            or observation.completeness_basis is not CompletenessBasis.FIXTURE_DECLARED
        ):
            return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH
    else:
        return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH

    if not _valid_local_id(observation.post_revision_id):
        return ExtractionStatus.REJECTED, ReasonCode.MISSING_POST_REVISION_ID
    if observation.text_truncated and observation.text_completeness is not TextCompleteness.PARTIAL:
        return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH
    if observation.text_completeness is TextCompleteness.COMPLETE and observation.text_truncated:
        return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH
    if observation.completeness_basis in {
        CompletenessBasis.UNKNOWN,
        CompletenessBasis.BROWSER_VISIBLE_VERIFIED,
    }:
        return ExtractionStatus.REJECTED, ReasonCode.PROVENANCE_MISMATCH
    return None


def _valid_local_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_LOCAL_ID_LENGTH
        and re.fullmatch(r"[A-Za-z0-9._:-]+", value) is not None
    )


def _valid_https_target(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TARGET_URL_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _result(
    status: ExtractionStatus,
    classification: VacancyClassification,
    confidence: Confidence,
    reason_codes: tuple[ReasonCode, ...],
    records: tuple[RecruitmentJobPostV1, ...] = (),
    *,
    post_identity_key: tuple[str, str] | None = None,
    observation: PostObservation | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        status=status,
        classification=classification,
        confidence=confidence,
        reason_codes=reason_codes,
        records=records,
        post_identity_key=post_identity_key,
        post_revision_id=None if observation is None else observation.post_revision_id,
        observation_id=None if observation is None else observation.observation_id,
    )


def _mapped_nfc_casefold(text: str) -> _MappedText:
    characters: list[str] = []
    source_ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        start = index
        index += 1
        while index < len(text) and unicodedata.combining(text[index]) != 0:
            index += 1
        cluster = unicodedata.normalize("NFC", text[start:index]).casefold()
        for character in cluster:
            characters.append(character)
            source_ranges.append((start, index))
    return _MappedText("".join(characters), tuple(source_ranges))


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    tokens = phrase.split()
    body = r"\s+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def _find_phrases(view: _MappedText, phrases: tuple[str, ...]) -> tuple[SourceSpan, ...]:
    matches: list[SourceSpan] = []
    for phrase in phrases:
        pattern = _phrase_pattern(phrase)
        matches.extend(
            view.source_span(match.start(), match.end()) for match in pattern.finditer(view.value)
        )
    return tuple(sorted(set(matches), key=lambda span: (span.start, span.end)))


def _find_recruiter_cues(view: _MappedText) -> tuple[SourceSpan, ...]:
    false_positive_spans = _find_phrases(view, _RECRUITER_FALSE_POSITIVES)
    candidates = _find_phrases(view, _RECRUITER_CUES)
    return tuple(
        span
        for span in candidates
        if not any(
            false_span.start <= span.start and span.end <= false_span.end
            for false_span in false_positive_spans
        )
    )


def _find_roles(
    view: _MappedText, recruiter_cues: tuple[SourceSpan, ...]
) -> tuple[_RoleOccurrence, ...]:
    if not recruiter_cues:
        return ()
    cue_ranges = tuple((span.start, span.end) for span in recruiter_cues)
    boundaries = tuple(
        (span.start, span.end)
        for phrase in _FIELD_BOUNDARIES
        for span in _find_phrases(view, (phrase,))
    )
    candidates: list[_RoleOccurrence] = []
    for key, label, category, aliases in _ROLE_ALIASES:
        for alias in aliases:
            pattern = _phrase_pattern(alias)
            for match in pattern.finditer(view.value):
                span = view.source_span(match.start(), match.end())
                if not any(_inside_role_segment(span, cue, boundaries) for cue in cue_ranges):
                    continue
                candidates.append(_RoleOccurrence(key, label, category, span))
    candidates.sort(
        key=lambda role: (-(role.span.end - role.span.start), role.span.start, role.key)
    )
    accepted: list[_RoleOccurrence] = []
    for candidate in candidates:
        if any(
            candidate.span.start < existing.span.end and existing.span.start < candidate.span.end
            for existing in accepted
        ):
            continue
        accepted.append(candidate)
    return tuple(sorted(accepted, key=lambda role: (role.span.start, role.span.end, role.key)))


def _resolve_duplicate_roles(
    view: _MappedText,
    role_occurrences: tuple[_RoleOccurrence, ...],
) -> tuple[tuple[_RoleOccurrence, ...], bool]:
    groups: dict[str, list[_RoleOccurrence]] = {}
    for occurrence in role_occurrences:
        groups.setdefault(occurrence.key, []).append(occurrence)

    city_spans = tuple(
        (city, span) for city, aliases in _CITY_ALIASES for span in _find_phrases(view, aliases)
    )
    district_spans = tuple(
        (match.group("number"), view.source_span(*match.span()))
        for match in re.finditer(r"(?<!\w)(?:q\.?|quận)\s*(?P<number>\d{1,2})(?!\w)", view.value)
    )
    accepted: list[_RoleOccurrence] = []
    ambiguous = False
    for group in groups.values():
        if len(group) == 1:
            accepted.extend(group)
            continue

        disambiguated: list[_RoleOccurrence] = []
        for occurrence in group:
            next_role_start = min(
                (
                    other.span.start
                    for other in role_occurrences
                    if other.span.start > occurrence.span.start
                ),
                default=len(view.source_ranges),
            )
            segment_end = min(next_role_start, occurrence.span.end + 180)
            cities = {
                city for city, span in city_spans if occurrence.span.end <= span.start < segment_end
            }
            districts = {
                district
                for district, span in district_spans
                if occurrence.span.end <= span.start < segment_end
            }
            if len(cities) > 1 or len(districts) > 1:
                disambiguated = []
                break
            location_parts = [f"city:{city}" for city in cities]
            location_parts.extend(f"district:{district}" for district in districts)
            if not location_parts:
                disambiguated = []
                break
            location_anchor = "+".join(sorted(location_parts))
            disambiguated.append(
                _RoleOccurrence(
                    occurrence.key,
                    occurrence.label,
                    occurrence.category,
                    occurrence.span,
                    location_anchor,
                )
            )
        if len(disambiguated) == len(group) and len(
            {occurrence.location_anchor for occurrence in disambiguated}
        ) == len(group):
            accepted.extend(disambiguated)
        else:
            ambiguous = True

    accepted.sort(key=lambda role: (role.span.start, role.span.end, role.key))
    return tuple(accepted), ambiguous


def _inside_role_segment(
    role_span: SourceSpan,
    cue: tuple[int, int],
    boundaries: tuple[tuple[int, int], ...],
) -> bool:
    cue_start, cue_end = cue
    if role_span.start < cue_start or role_span.start - cue_end > 180:
        return False
    if role_span.start < cue_end and role_span.end <= cue_end:
        return False
    next_boundary = min(
        (start for start, _ in boundaries if cue_end <= start < role_span.start),
        default=None,
    )
    return next_boundary is None


def _spanned[T](
    value: T,
    raw_value: str,
    spans: tuple[SourceSpan, ...] | SourceSpan,
) -> FieldEvidence[T]:
    if isinstance(spans, SourceSpan):
        normalized_spans = (spans,)
    else:
        normalized_spans = spans
    return FieldEvidence(EvidenceState.SOURCE_SPAN, value, raw_value, normalized_spans)


def _missing(completeness: TextCompleteness) -> FieldEvidence[Never]:
    state = (
        EvidenceState.ABSENT
        if completeness is TextCompleteness.COMPLETE
        else EvidenceState.AMBIGUOUS
    )
    return FieldEvidence(state)


def _ambiguous(spans: tuple[SourceSpan, ...] = ()) -> FieldEvidence[Never]:
    return FieldEvidence(EvidenceState.AMBIGUOUS, spans=spans)


def _field_from_norm_match(
    text: str,
    view: _MappedText,
    match: re.Match[str],
    group: str | int = 0,
) -> FieldEvidence[str]:
    start, end = match.span(group)
    span = view.source_span(start, end)
    raw = text[span.start : span.end]
    return _spanned(raw, raw, span)


def _resolve_evidence[T](
    evidence: tuple[FieldEvidence[T], ...],
    completeness: TextCompleteness,
) -> FieldEvidence[T]:
    if not evidence:
        return _missing(completeness)
    distinct = {item.value for item in evidence}
    spans = tuple(span for item in evidence for span in item.spans)
    if len(distinct) > 1:
        return _ambiguous(spans)
    first = evidence[0]
    return FieldEvidence(first.state, first.value, first.raw_value, spans)


def _build_record(
    *,
    observation: PostObservation,
    text: str,
    view: _MappedText,
    role: _RoleOccurrence,
    completeness: TextCompleteness,
    policy: ExtractionPolicy,
    reason_codes: tuple[ReasonCode, ...],
) -> RecruitmentJobPostV1:
    role_raw = text[role.span.start : role.span.end]
    role_title = _spanned(role_raw, role_raw, role.span)
    role_category = _spanned(role.category, role_raw, role.span)
    role_anchor = f"role-v1:{role.key}"
    if role.location_anchor is not None:
        role_anchor += f"|{role.location_anchor}"
    post_identity_key = (observation.account_uuid, observation.canonical_post_url)
    vacancy_id = _opaque_id("vac", (*post_identity_key, role_anchor))

    headcount = _headcount_for_role(text, role.span, completeness)
    employer = _employer_evidence(text, view, completeness)
    location = _location_evidence(text, view, completeness)
    work_mode = _alias_evidence(text, view, _WORK_MODE_ALIASES, completeness)
    schedule = _schedule_evidence(text, view, completeness)
    salary = _salary_evidence(text, view, completeness)
    experience = _experience_evidence(text, view, completeness)
    requirements = _labelled_list_evidence(text, view, "yêu cầu", "yeu cau", completeness)
    benefits = _benefits_evidence(text, view, completeness)
    contacts = _contact_evidence(text, view, completeness, observation.source)

    content_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    vacancy_revision_id = _opaque_id(
        "vrev",
        (vacancy_id, observation.post_revision_id or "", content_digest, EXTRACTION_VERSION),
    )
    record_reason_codes = reason_codes
    if salary.raw.state is EvidenceState.AMBIGUOUS:
        record_reason_codes += (ReasonCode.SALARY_AMBIGUOUS,)
    if location.raw.state is EvidenceState.AMBIGUOUS:
        record_reason_codes += (ReasonCode.LOCATION_AMBIGUOUS,)
    if observation.source is ObservationSource.MANUAL_URL:
        record_reason_codes += (ReasonCode.CONTACT_EXTRACTION_BLOCKED,)

    evidence_fields = (
        employer,
        role_title,
        role_category,
        headcount,
        location.raw,
        location.address,
        location.district,
        location.province_city,
        work_mode,
        schedule,
        salary.raw,
        salary.amount_min,
        salary.amount_max,
        salary.currency,
        salary.pay_period,
        salary.commission,
        experience,
        requirements,
        benefits,
        contacts.phone,
        contacts.zalo,
        contacts.email,
        contacts.application_url,
    )
    fields_ambiguous = any(
        evidence.state is EvidenceState.AMBIGUOUS for evidence in evidence_fields
    )
    return RecruitmentJobPostV1(
        schema_version=1,
        post_identity_key=post_identity_key,
        post_revision_id=observation.post_revision_id or "",
        observation_id=observation.observation_id,
        account_uuid=observation.account_uuid,
        vacancy_id=vacancy_id,
        vacancy_revision_id=vacancy_revision_id,
        role_anchor=role_anchor,
        classification=VacancyClassification.RECRUITER_VACANCY,
        extraction_version=EXTRACTION_VERSION,
        confidence=Confidence.MEDIUM
        if completeness is not TextCompleteness.COMPLETE or fields_ambiguous
        else Confidence.HIGH,
        employer=employer,
        role_title=role_title,
        role_category=role_category,
        headcount=headcount,
        location=location,
        work_mode=work_mode,
        schedule=schedule,
        salary=salary,
        experience=experience,
        requirements=requirements,
        benefits=benefits,
        contacts=contacts,
        reason_codes=record_reason_codes,
    )


def _headcount_for_role(
    text: str,
    role_span: SourceSpan,
    completeness: TextCompleteness,
) -> FieldEvidence[int]:
    prefix = text[max(0, role_span.start - 24) : role_span.start]
    count_match = re.search(r"(?P<count>\d{1,3})\s*(?:vị trí\s*)?$", prefix, re.IGNORECASE)
    if count_match is not None:
        start = max(0, role_span.start - 24) + count_match.start("count")
        end = max(0, role_span.start - 24) + count_match.end("count")
        span = SourceSpan(start, end)
        count = int(text[start:end])
        if count > 0:
            return _spanned(count, text[start:end], span)
        return _ambiguous((span,))
    vague_match = re.search(r"(?:nhiều|vài|some)\s*$", prefix, re.IGNORECASE)
    if vague_match is not None:
        start = max(0, role_span.start - 24) + vague_match.start()
        end = max(0, role_span.start - 24) + vague_match.end()
        return _ambiguous((SourceSpan(start, end),))
    return _missing(completeness)


def _employer_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
) -> FieldEvidence[str]:
    pattern = re.compile(
        r"(?<!\w)(?:công\s+ty|nhà\s+tuyển\s+dụng|doanh\s+nghiệp)\s*[:：]\s*"
        r"(?P<value>[^,;\n|]{1,80})"
    )
    found: list[FieldEvidence[str]] = []
    for match in pattern.finditer(view.value):
        start, end = match.span("value")
        while start < end and view.value[start].isspace():
            start += 1
        while end > start and view.value[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        span = view.source_span(start, end)
        raw = text[span.start : span.end]
        found.append(_spanned(raw, raw, span))
    return _resolve_evidence(tuple(found), completeness)


def _alias_evidence(
    text: str,
    view: _MappedText,
    aliases: tuple[tuple[str, tuple[str, ...]], ...],
    completeness: TextCompleteness,
) -> FieldEvidence[str]:
    found: list[FieldEvidence[str]] = []
    for normalized_value, phrases in aliases:
        for span in _find_phrases(view, phrases):
            raw = text[span.start : span.end]
            found.append(_spanned(normalized_value, raw, span))
    return _resolve_evidence(tuple(found), completeness)


def _location_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
) -> LocationFields:
    district_matches: list[FieldEvidence[str]] = []
    district_pattern = re.compile(r"(?<!\w)(?:q\.?|quận)\s*(?P<number>\d{1,2})(?!\w)")
    for match in district_pattern.finditer(view.value):
        span = view.source_span(match.start(), match.end())
        raw = text[span.start : span.end]
        number = match.group("number")
        district_matches.append(_spanned(f"Quận {int(number)}", raw, span))
    district = _resolve_evidence(tuple(district_matches), completeness)

    city_matches: list[FieldEvidence[str]] = []
    for city, aliases in _CITY_ALIASES:
        for span in _find_phrases(view, aliases):
            raw = text[span.start : span.end]
            city_matches.append(_spanned(city, raw, span))
    city = _resolve_evidence(tuple(city_matches), completeness)

    address_matches: list[FieldEvidence[str]] = []
    address_pattern = re.compile(r"(?<!\w)địa\s+chỉ\s*[:：]\s*(?P<value>[^;\n|]{1,120})")
    for match in address_pattern.finditer(view.value):
        start, end = match.span("value")
        while start < end and view.value[start].isspace():
            start += 1
        while end > start and view.value[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        span = view.source_span(start, end)
        raw = text[span.start : span.end]
        address_matches.append(_spanned(raw, raw, span))
    address = _resolve_evidence(tuple(address_matches), completeness)

    raw_spans = tuple(
        sorted(
            (*district.spans, *city.spans, *address.spans),
            key=lambda span: (span.start, span.end),
        )
    )
    if not raw_spans:
        raw_field = _missing(completeness)
    elif any(evidence.state is EvidenceState.AMBIGUOUS for evidence in (address, district, city)):
        raw_field = _ambiguous(raw_spans)
    else:
        raw_values = tuple(text[span.start : span.end] for span in raw_spans)
        raw_field = FieldEvidence(EvidenceState.SOURCE_SPAN, raw_values, None, raw_spans)
    return LocationFields(raw_field, address, district, city)


def _schedule_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
) -> FieldEvidence[str]:
    found: list[FieldEvidence[str]] = []
    for schedule, aliases in _SCHEDULE_ALIASES:
        for span in _find_phrases(view, aliases):
            raw = text[span.start : span.end]
            found.append(_spanned(schedule, raw, span))
    time_pattern = re.compile(r"(?<!\w)\d{1,2}(?:h|:\d{2})\s*[-–]\s*\d{1,2}(?:h|:\d{2})(?!\w)")
    for match in time_pattern.finditer(view.value):
        span = view.source_span(match.start(), match.end())
        raw = text[span.start : span.end]
        found.append(_spanned(raw, raw, span))
    return _resolve_evidence(tuple(found), completeness)


def _experience_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
) -> FieldEvidence[str]:
    patterns = (
        re.compile(r"(?<!\w)không\s+yêu\s+cầu\s+kinh\s+nghiệm(?!\w)"),
        re.compile(r"(?<!\w)\d{1,2}\s*(?:năm|tháng)\s+kinh\s+nghiệm(?!\w)"),
    )
    found: list[FieldEvidence[str]] = []
    for pattern in patterns:
        for match in pattern.finditer(view.value):
            item = _field_from_norm_match(text, view, match)
            found.append(item)
    return _resolve_evidence(tuple(found), completeness)


def _labelled_list_evidence(
    text: str,
    view: _MappedText,
    label: str,
    no_diacritic_label: str,
    completeness: TextCompleteness,
) -> FieldEvidence[tuple[str, ...]]:
    pattern = re.compile(
        rf"(?<!\w)(?:{re.escape(label)}|{re.escape(no_diacritic_label)})\s*[:：]\s*"
        r"(?P<value>[^;\n|]{1,240})"
    )
    values: list[str] = []
    spans: list[SourceSpan] = []
    for match in pattern.finditer(view.value):
        start, end = match.span("value")
        while start < end and view.value[start].isspace():
            start += 1
        while end > start and view.value[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        span = view.source_span(start, end)
        spans.append(span)
        values.append(text[span.start : span.end])
    if not values:
        return _missing(completeness)
    return FieldEvidence(EvidenceState.SOURCE_SPAN, tuple(values), None, tuple(spans))


def _benefits_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
) -> FieldEvidence[tuple[str, ...]]:
    phrases = (
        "bao ăn",
        "bao an",
        "hỗ trợ ăn ở",
        "ho tro an o",
        "có chỗ ở",
        "co cho o",
        "bảo hiểm xã hội",
        "bao hiem xa hoi",
        "bhxh",
        "phụ cấp",
        "phu cap",
    )
    found = _find_phrases(view, phrases)
    labelled = _labelled_list_evidence(text, view, "phúc lợi", "phuc loi", completeness)
    labelled_spans = labelled.spans if labelled.state is EvidenceState.SOURCE_SPAN else ()
    if labelled_spans:
        found = tuple(
            span
            for span in found
            if not any(
                label.start <= span.start and span.end <= label.end for label in labelled_spans
            )
        )
        for label_span in labelled_spans:
            segment_start = label_span.start
            segment = text[label_span.start : label_span.end]
            for piece in re.finditer(r"[^,;]+", segment):
                left, right = piece.span()
                while left < right and segment[left].isspace():
                    left += 1
                while right > left and segment[right - 1].isspace():
                    right -= 1
                if left < right:
                    found += (SourceSpan(segment_start + left, segment_start + right),)
    if not found:
        return _missing(completeness)
    unique_spans = tuple(dict.fromkeys(found))
    values = tuple(text[span.start : span.end] for span in unique_spans)
    return FieldEvidence(EvidenceState.SOURCE_SPAN, values, None, unique_spans)


def _salary_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
) -> SalaryFields:
    candidates = _salary_candidates(text, view)
    negotiated_spans = _find_phrases(view, _NEGOTIATED_PHRASES)
    if negotiated_spans:
        if candidates:
            all_spans = tuple(candidate.span for candidate in candidates) + negotiated_spans
            ambiguous_raw: FieldEvidence[str] = _ambiguous(all_spans)
            ambiguous_amount: FieldEvidence[int] = _ambiguous(all_spans)
            return SalaryFields(
                ambiguous_raw,
                ambiguous_amount,
                ambiguous_amount,
                _ambiguous(all_spans),
                _ambiguous(all_spans),
                _ambiguous(all_spans),
            )
        spans = tuple(negotiated_spans)
        raw = FieldEvidence(
            EvidenceState.SOURCE_SPAN,
            next(text[span.start : span.end] for span in spans),
            text[spans[0].start : spans[0].end],
            spans,
        )
        absent_text = _missing(completeness)
        absent_number = _missing(completeness)
        return SalaryFields(
            raw, absent_number, absent_number, absent_text, absent_text, absent_text
        )

    if not candidates:
        missing_text = _missing(completeness)
        missing_number = _missing(completeness)
        return SalaryFields(
            missing_text,
            missing_number,
            missing_number,
            missing_text,
            missing_text,
            missing_text,
        )

    distinct_salary_values = {
        (candidate.amount_min, candidate.amount_max, candidate.period, candidate.currency)
        for candidate in candidates
    }
    if len(distinct_salary_values) > 1:
        spans = tuple(candidate.span for candidate in candidates)
        return SalaryFields(
            _ambiguous(spans),
            _ambiguous(spans),
            _ambiguous(spans),
            _ambiguous(spans),
            _ambiguous(spans),
            _ambiguous(spans),
        )

    candidate = candidates[0]
    raw = _spanned(candidate.raw, candidate.raw, candidate.span)
    amount_min = (
        _missing(completeness)
        if candidate.amount_min is None or candidate.amount_min_span is None
        else _spanned(
            candidate.amount_min,
            text[candidate.amount_min_span.start : candidate.amount_min_span.end],
            candidate.amount_min_span,
        )
    )
    amount_max = (
        _missing(completeness)
        if candidate.amount_max is None or candidate.amount_max_span is None
        else _spanned(
            candidate.amount_max,
            text[candidate.amount_max_span.start : candidate.amount_max_span.end],
            candidate.amount_max_span,
        )
    )
    currency = (
        _missing(completeness)
        if candidate.currency is None or candidate.currency_span is None
        else _spanned(
            candidate.currency,
            text[candidate.currency_span.start : candidate.currency_span.end],
            candidate.currency_span,
        )
    )
    period = (
        _missing(completeness)
        if candidate.period is None or candidate.period_span is None
        else _spanned(
            candidate.period,
            text[candidate.period_span.start : candidate.period_span.end],
            candidate.period_span,
        )
    )
    commission = (
        _missing(completeness)
        if candidate.commission is None or candidate.commission_span is None
        else _spanned(
            candidate.commission,
            text[candidate.commission_span.start : candidate.commission_span.end],
            candidate.commission_span,
        )
    )
    return SalaryFields(raw, amount_min, amount_max, currency, period, commission)


def _salary_candidates(text: str, view: _MappedText) -> list[_SalaryCandidate]:
    range_matches = list(_RANGE_PATTERN.finditer(view.value))
    candidates: list[_SalaryCandidate] = []
    covered: list[tuple[int, int]] = []
    for match in range_matches:
        unit1 = match.group("unit1")
        unit2 = match.group("unit2")
        if unit1 and unit1 != unit2:
            continue
        if not _salary_context(view.value, match.start(), match.end()):
            continue
        full_end = _salary_tail_end(view.value, match.end())
        candidate = _candidate_from_match(
            text,
            view,
            match,
            range_mode=True,
            end_override=full_end,
        )
        if candidate is not None:
            candidates.append(candidate)
            covered.append(match.span())

    for match in _FIXED_PATTERN.finditer(view.value):
        if any(match.start() < end and start < match.end() for start, end in covered):
            continue
        full_end = _salary_tail_end(view.value, match.end())
        if not _salary_context(view.value, match.start(), match.end()):
            continue
        candidate = _candidate_from_match(
            text, view, match, range_mode=False, end_override=full_end
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.span.start, item.span.end))
    return candidates


def _salary_tail_end(text: str, end: int) -> int:
    tail = text[end : min(len(text), end + 50)]
    suffix_patterns = (
        re.compile(r"\s*/\s*(?:tháng|thang|giờ|gio|ngày|ngay|tuần|tuan|năm|nam|h)\b"),
        re.compile(
            r"\s*\+\s*(?:%\s*hh|%\s*hoa\s+hồng|%\s*hoa\s+hong|hoa\s+hồng|hoa\s+hong|commission)"
        ),
        re.compile(r"\s*(?:vnd|vnđ|đồng|dong|₫)(?!\w)"),
    )
    lengths = [match.end() for pattern in suffix_patterns if (match := pattern.match(tail))]
    return end + max(lengths, default=0)


def _salary_context(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 40) : start]
    tail = text[end : min(len(text), end + 40)]
    has_label = any(
        _phrase_pattern(label).search(prefix)
        for label in ("lương", "luong", "thu nhập", "thu nhap", "mức lương", "muc luong")
    )
    has_unit_context = re.match(
        r"\s*(?:/\s*(?:tháng|thang|giờ|gio|ngày|ngay|tuần|tuan|năm|nam|h)|"
        r"\+\s*(?:%\s*hh|%\s*hoa\s+hồng|%\s*hoa\s+hong|hoa\s+hồng|hoa\s+hong|commission)|"
        r"(?:vnd|vnđ|đồng|dong|₫))\b",
        tail,
    )
    return has_label or has_unit_context is not None


def _candidate_from_match(
    text: str,
    view: _MappedText,
    match: re.Match[str],
    *,
    range_mode: bool,
    end_override: int | None = None,
) -> _SalaryCandidate | None:
    start = match.start()
    end = match.end() if end_override is None else end_override
    span = view.source_span(start, end)
    raw = text[span.start : span.end]
    first_group = "first" if range_mode else "number"
    first_start, first_end = match.span(first_group)
    first_span = view.source_span(first_start, first_end)
    unit = match.group("unit2") if range_mode else match.group("unit")
    amount_min = _amount_value(text[first_span.start : first_span.end], unit)
    if amount_min is None:
        return None
    amount_max = amount_min
    amount_max_span: SourceSpan | None = first_span
    if range_mode:
        second_start, second_end = match.span("second")
        amount_max_span = view.source_span(second_start, second_end)
        amount_max = _amount_value(text[amount_max_span.start : amount_max_span.end], unit)
        if amount_max is None or amount_min > amount_max:
            return None

    period_value: str | None = None
    period_span: SourceSpan | None = None
    currency_value: str | None = None
    currency_span: SourceSpan | None = None
    commission_value: str | None = None
    commission_span: SourceSpan | None = None

    suffix = view.value[match.end() : end]
    period_match = re.search(
        r"/\s*(?P<period>tháng|thang|giờ|gio|ngày|ngay|tuần|tuan|năm|nam|h)\b", suffix
    )
    if period_match is not None:
        period_token = period_match.group("period")
        period_value = _period_value(period_token)
        period_span = view.source_span(
            match.end() + period_match.start("period"),
            match.end() + period_match.end("period"),
        )
    commission_match = re.search(
        r"\+\s*(?P<commission>%\s*hh|%\s*hoa\s+hồng|%\s*hoa\s+hong|hoa\s+hồng|hoa\s+hong|commission)",
        suffix,
    )
    if commission_match is not None:
        commission_span = view.source_span(
            match.end() + commission_match.start("commission"),
            match.end() + commission_match.end("commission"),
        )
        commission_token = view.value[
            match.end() + commission_match.start("commission") : match.end()
            + commission_match.end("commission")
        ]
        commission_value = (
            "PERCENTAGE_UNSPECIFIED" if "%" in commission_token else "COMMISSION_UNSPECIFIED"
        )

    currency_search_start = max(0, start - 8)
    currency_search_end = min(len(view.value), end + 12)
    currency_match = re.search(
        r"(?<!\w)(?:vnd|vnđ|đồng|dong|₫)(?!\w)",
        view.value[currency_search_start:currency_search_end],
    )
    if currency_match is not None:
        absolute_start = currency_search_start + currency_match.start()
        absolute_end = currency_search_start + currency_match.end()
        currency_span = view.source_span(absolute_start, absolute_end)
        currency_value = "VND"

    return _SalaryCandidate(
        span=span,
        raw=raw,
        amount_min=amount_min,
        amount_min_span=first_span,
        amount_max=amount_max,
        amount_max_span=amount_max_span,
        period=period_value,
        period_span=period_span,
        currency=currency_value,
        currency_span=currency_span,
        commission=commission_value,
        commission_span=commission_span,
    )


def _amount_value(raw_number: str, unit: str | None) -> int | None:
    multiplier = {"k": 1_000, "tr": 1_000_000, "triệu": 1_000_000}.get(unit or "")
    if multiplier is None:
        return None
    try:
        amount = Decimal(raw_number.replace(",", ".")) * multiplier
    except InvalidOperation:
        return None
    if amount <= 0 or amount != amount.to_integral_value():
        return None
    return int(amount)


def _period_value(token: str) -> str | None:
    folded = token.casefold()
    aliases = {
        "tháng": "MONTH",
        "thang": "MONTH",
        "giờ": "HOUR",
        "gio": "HOUR",
        "h": "HOUR",
        "ngày": "DAY",
        "ngay": "DAY",
        "tuần": "WEEK",
        "tuan": "WEEK",
        "năm": "YEAR",
        "nam": "YEAR",
    }
    return aliases.get(folded)


def _contact_evidence(
    text: str,
    view: _MappedText,
    completeness: TextCompleteness,
    source: ObservationSource,
) -> ContactFields:
    if source is not ObservationSource.SYNTHETIC_FIXTURE:
        blocked = _ambiguous()
        return ContactFields(blocked, blocked, blocked, blocked)

    phone_pattern = re.compile(
        r"(?<!\w)(?:sđt|sdt|số\s+điện\s+thoại|so\s+dien\s+thoai|điện\s+thoại|"
        r"dien\s+thoai|phone|hotline)\s*[:：-]?\s*"
        r"(?P<value>\[(?:synthetic|fake)[^\]]{1,40}\]|\+?\d[\d\s().-]{5,20})"
    )
    zalo_pattern = re.compile(
        r"(?<!\w)zalo\s*[:：-]?\s*(?P<value>\[(?:synthetic|fake)[^\]]{1,40}\]|\+?\d[\d\s().-]{5,20})"
    )
    email_pattern = re.compile(
        r"(?<![\w.+-])(?P<value>[a-z0-9._%+-]+@[a-z0-9.-]+\.(?:invalid|example))(?!\w)"
    )
    url_pattern = re.compile(
        r"(?<!\w)(?:ứng\s+tuyển|ung\s+tuyen|apply|nộp\s+hồ\s+sơ|nop\s+ho\s+so)\s*[:：-]?\s*"
        r"(?P<value>https?://[^\s<>]+\.invalid[^\s<>]*)"
    )
    return ContactFields(
        _contact_kind_evidence(text, view, phone_pattern, completeness),
        _contact_kind_evidence(text, view, zalo_pattern, completeness),
        _contact_kind_evidence(text, view, email_pattern, completeness),
        _contact_kind_evidence(text, view, url_pattern, completeness),
    )


def _contact_kind_evidence(
    text: str,
    view: _MappedText,
    pattern: re.Pattern[str],
    completeness: TextCompleteness,
) -> FieldEvidence[str]:
    matches: list[FieldEvidence[str]] = []
    for match in pattern.finditer(view.value):
        item = _field_from_norm_match(text, view, match, "value")
        matches.append(
            FieldEvidence(EvidenceState.SOURCE_SPAN, str(item.value), item.raw_value, item.spans)
        )
    return _resolve_evidence(tuple(matches), completeness)


def _opaque_id(prefix: str, components: tuple[str, ...]) -> str:
    serialized = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-v1-{digest}"
