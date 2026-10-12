"""Private, offline calendar for original operator-authored recruitment content."""

from __future__ import annotations

import difflib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NoReturn, cast
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from threads_platform.standalone.manual_outcomes import ManualOutcomeError, ManualOutcomeStore

ContentProvenanceV1 = Literal["OPERATOR_AUTHORED"]
ContentEditorialStatusV1 = Literal[
    "DRAFT",
    "READY_FOR_MANUAL_PUBLICATION",
    "REJECTED",
]
AttachmentKindV1 = Literal["IMAGE", "VIDEO", "REFERENCE_LINK"]
DuplicateWarningCode = Literal["EXACT_DUPLICATE", "NEAR_DUPLICATE"]

_NAMESPACE = "recruitment_content_planner_v1"
_VERSION = 1
_MAX_DOCUMENT_BYTES = 512 * 1024
_MAX_ACTIVE = 20
_MAX_RETAINED = 40
_MAX_TEXT_CHARS = 500
_MAX_TEXT_BYTES = 4_096
_MAX_NOTES_CHARS = 1_000
_MAX_REVIEW_NOTE_CHARS = 500
_MAX_LABEL_CHARS = 120
_MAX_ATTRIBUTION_CHARS = 200
_MAX_URL_BYTES = 2_048
_MAX_SCHEDULE_HORIZON = timedelta(days=180)
_NEAR_DUPLICATE_THRESHOLD = 0.86
_NEAR_DUPLICATE_MIN_CHARS = 32
_REVIEW_FINGERPRINT_DOMAIN = b"threads-standalone/recruitment-content-packet-fingerprint/v1\x00"
_REVIEW_FINGERPRINT_PREFIX = "content-packet-v1:sha256:"
_REVIEW_FINGERPRINT_RE = re.compile(r"content-packet-v1:sha256:[0-9a-f]{64}")
_REVIEW_DECISIONS = frozenset({"APPROVE", "REJECT"})
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_TOP_LEVEL_KEYS = frozenset({"version", "account_uuid", "packets"})
_PACKET_KEYS = frozenset(
    {
        "version",
        "account_uuid",
        "packet_id",
        "original_text",
        "content_provenance",
        "rights_asserted",
        "consent_asserted",
        "rights_assertion_status",
        "platform_permission_status",
        "attachment",
        "private_notes",
        "scheduled_at",
        "created_at",
        "updated_at",
        "editorial_status",
        "reviewed_at",
        "review_note",
        "remote_publication_status",
    }
)
_ATTACHMENT_KEYS = frozenset(
    {
        "kind",
        "url",
        "label",
        "attribution",
        "rights_asserted",
        "consent_asserted",
        "rights_assertion_status",
    }
)
_EDITORIAL_STATUSES = frozenset({"DRAFT", "READY_FOR_MANUAL_PUBLICATION", "REJECTED"})
_ATTACHMENT_KINDS = frozenset({"IMAGE", "VIDEO", "REFERENCE_LINK"})
_EMAIL = re.compile(
    r"(?i)(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+"
)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)")
_SENSITIVE_TERMS = re.compile(
    r"(?i)\b(?:social security number|national id(?: number)?|passport number|"
    r"date of birth|dob|medical records?|diagnos(?:is|ed)|bank account|"
    r"credit card number|personal data|private applicant data)\b"
)
_PRIVATE_CV = re.compile(
    r"(?i)\b(?:applicant|candidate|job seeker)(?:'s)?\s+"
    r"(?:private\s+|confidential\s+)?(?:cv|resume|curriculum vitae)\b|"
    r"\b(?:attached|uploaded|pasted|confidential|private)\b.{0,60}"
    r"\b(?:cv|resume|curriculum vitae)\b|"
    r"\b(?:cv|resume|curriculum vitae)\s+(?:of|for)\s+"
    r"(?:the\s+)?(?:applicant|candidate)\b"
)
_PRIVATE_FIELD = re.compile(
    r"(?im)^\s*(?:name|full name|first name|last name|applicant name|candidate name|"
    r"e-?mail|phone|mobile|home address|address|dob|date of birth|"
    r"passport|national id|ssn|social security)\s*[:=-]"
)
_STREET_ADDRESS = re.compile(
    r"(?i)\b\d{1,5}\s+[\w.'-]+(?:\s+[\w.'-]+){0,3}\s+"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|boulevard|blvd\.?|drive|dr\.?)\b"
)
_URL_IN_TEXT = re.compile(r"(?i)\bhttps?://|\bwww\.")


class RecruitmentContentPlannerError(ValueError):
    """Fixed-code local failure that never includes packet text or filesystem paths."""

    _CODES = frozenset(
        {
            "CONTENT_PLANNER_ACCOUNT_MISMATCH",
            "CONTENT_PLANNER_BUSY",
            "CONTENT_PLANNER_DUPLICATE_ID",
            "CONTENT_PLANNER_ENTRY_NOT_FOUND",
            "CONTENT_PLANNER_INVALID",
            "CONTENT_PLANNER_LIMIT_REACHED",
            "CONTENT_PLANNER_PRIVATE_CONTENT_REJECTED",
            "CONTENT_PLANNER_REVIEW_INVALID",
            "CONTENT_PLANNER_REVIEW_STALE",
            "CONTENT_PLANNER_RIGHTS_REQUIRED",
            "CONTENT_PLANNER_SCHEDULE_INVALID",
            "CONTENT_PLANNER_SOURCE_INVALID",
            "CONTENT_PLANNER_STATE_INVALID",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported recruitment content planner error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"RecruitmentContentPlannerError(code={self.code})"


@dataclass(frozen=True, slots=True, repr=False)
class ContentAttachmentMetadataV1:
    """Bounded URL metadata only; planner never opens, fetches, or uploads it."""

    kind: AttachmentKindV1
    url: str = field(repr=False)
    label: str | None = field(default=None, repr=False)
    attribution: str | None = field(default=None, repr=False)
    rights_asserted: bool = False
    consent_asserted: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentContentPacketV1:
    version: Literal[1]
    account_uuid: UUID
    packet_id: UUID
    original_text: str = field(repr=False)
    content_provenance: ContentProvenanceV1
    rights_asserted: Literal[True]
    consent_asserted: Literal[True]
    rights_assertion_status: Literal["UNVERIFIED_OPERATOR_ASSERTION"]
    platform_permission_status: Literal["NOT_ESTABLISHED"]
    attachment: ContentAttachmentMetadataV1 | None
    private_notes: str | None = field(repr=False)
    scheduled_at: datetime
    created_at: datetime
    updated_at: datetime
    editorial_status: ContentEditorialStatusV1
    reviewed_at: datetime | None
    review_note: str | None = field(repr=False)
    remote_publication_status: Literal["NOT_PUBLISHED_BY_PLANNER"]


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentContentCalendarV1:
    account_uuid: UUID
    packets: tuple[RecruitmentContentPacketV1, ...]

    @property
    def active_count(self) -> int:
        return sum(packet.editorial_status != "REJECTED" for packet in self.packets)


@dataclass(frozen=True, slots=True)
class ContentDuplicateWarning:
    code: DuplicateWarningCode
    matched_packet_id: UUID
    similarity_percent: int


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentContentCreateResult:
    packet: RecruitmentContentPacketV1
    warnings: tuple[ContentDuplicateWarning, ...]
    created: bool


@dataclass(frozen=True, slots=True, repr=False)
class RecruitmentContentReviewSnapshotV1:
    """Immutable packet snapshot and versioned fingerprint for one review."""

    packet: RecruitmentContentPacketV1
    fingerprint: str


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("invalid JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate planner field")
        result[key] = value
    return result


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT)


def _validate_account_id(value: object) -> UUID:
    if type(value) is not UUID or value.version != 4:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    return value


def _parse_uuid4(value: object) -> UUID:
    if type(value) is not str:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    try:
        parsed = UUID(value)
    except ValueError, AttributeError:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
    if parsed.version != 4 or str(parsed) != value:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    return parsed


def _normalize_text(value: object, *, maximum: int, empty_ok: bool = False) -> str | None:
    if value is None and empty_ok:
        return None
    if type(value) is not str:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    for character in normalized:
        category = unicodedata.category(character)
        if character == "\n" or character == "\u200d":
            continue
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
        if category == "Zs" and character != " ":
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    normalized = normalized.strip()
    if not normalized or len(normalized) > maximum:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    try:
        if len(normalized.encode("utf-8", errors="strict")) > maximum * 8:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    except UnicodeError:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
    return normalized


def _has_private_content(value: str) -> bool:
    return bool(_has_sensitive_data(value) or _URL_IN_TEXT.search(value))


def _has_sensitive_data(value: str) -> bool:
    if (
        _EMAIL.search(value)
        or _SENSITIVE_TERMS.search(value)
        or _PRIVATE_CV.search(value)
        or _PRIVATE_FIELD.search(value)
        or _STREET_ADDRESS.search(value)
    ):
        return True
    for match in _PHONE.finditer(value):
        if sum(character.isdigit() for character in match.group()) >= 8:
            return True
    return False


def _require_public_url(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= _MAX_URL_BYTES:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    try:
        if len(value.encode("utf-8", errors="strict")) > _MAX_URL_BYTES:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except UnicodeError, ValueError:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    host = host.casefold().rstrip(".")
    if (
        not host
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
    ):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
        if "." not in ascii_host or len(ascii_host) > 253:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    else:
        if not address.is_global:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    if port is not None and not 1 <= port <= 65_535:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    if _has_sensitive_data(unquote(parsed.path)):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_PRIVATE_CONTENT_REJECTED")
    return value


def _utc_datetime(value: object, *, code: str = "CONTENT_PLANNER_SCHEDULE_INVALID") -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise RecruitmentContentPlannerError(code)
    try:
        if value.utcoffset() != timedelta(0):
            raise RecruitmentContentPlannerError(code)
        return value.astimezone(UTC)
    except OverflowError, ValueError:
        raise RecruitmentContentPlannerError(code) from None


def _format_utc(value: datetime) -> str:
    return _utc_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
    normalized = _utc_datetime(parsed, code="CONTENT_PLANNER_INVALID")
    if _format_utc(normalized) != value:
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
    return normalized


def _content_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _private_mode(metadata: os.stat_result) -> bool:
    return os.name == "nt" or metadata.st_mode & 0o077 == 0


class RecruitmentContentPlannerStore:
    """Strict local per-account content calendar with no remote action capability."""

    def __init__(
        self,
        data_root: Path,
        *,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._configured_root = Path(data_root).absolute()
        self._utc_now = utc_now if utc_now is not None else lambda: datetime.now(UTC)

    @contextmanager
    def account_lock(self, account_id: UUID) -> Generator[None]:
        account = _validate_account_id(account_id)
        root = self._required_root()
        try:
            with ManualOutcomeStore(root).account_lock(account):
                yield
        except ManualOutcomeError as error:
            code = (
                "CONTENT_PLANNER_BUSY"
                if error.code == "MANUAL_OUTCOME_BUSY"
                else "CONTENT_PLANNER_STATE_INVALID"
            )
            raise RecruitmentContentPlannerError(code) from None

    def create(
        self,
        account_id: UUID,
        original_text: str,
        scheduled_at: datetime,
        *,
        provenance: object = None,
        rights_asserted: object = False,
        consent_asserted: object = False,
        attachment: ContentAttachmentMetadataV1 | None = None,
        private_notes: str | None = None,
    ) -> RecruitmentContentCreateResult:
        account = _validate_account_id(account_id)
        if type(provenance) is not str or provenance != "OPERATOR_AUTHORED":
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_SOURCE_INVALID")
        if rights_asserted is not True or consent_asserted is not True:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_RIGHTS_REQUIRED")
        text = _normalize_text(original_text, maximum=_MAX_TEXT_CHARS)
        assert text is not None
        self._reject_private_content(text)
        notes = _normalize_text(
            private_notes,
            maximum=_MAX_NOTES_CHARS,
            empty_ok=True,
        )
        if notes is not None:
            self._reject_private_content(notes)
        normalized_attachment = self._normalize_attachment(attachment)

        with self.account_lock(account):
            root = self._required_root()
            calendar = self._read_unlocked(root, account)
            now = self._now()
            scheduled = _utc_datetime(scheduled_at)
            if not now - _MAX_SCHEDULE_HORIZON <= scheduled <= now + _MAX_SCHEDULE_HORIZON:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_SCHEDULE_INVALID")

            exact_key = _content_key(text)
            for packet in calendar.packets:
                if _content_key(packet.original_text) == exact_key:
                    return RecruitmentContentCreateResult(
                        packet=packet,
                        warnings=(
                            ContentDuplicateWarning(
                                code="EXACT_DUPLICATE",
                                matched_packet_id=packet.packet_id,
                                similarity_percent=100,
                            ),
                        ),
                        created=False,
                    )

            if calendar.active_count >= _MAX_ACTIVE or len(calendar.packets) >= _MAX_RETAINED:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_LIMIT_REACHED")

            warnings = self._duplicate_warnings(calendar.packets, text)
            packet = RecruitmentContentPacketV1(
                version=1,
                account_uuid=account,
                packet_id=uuid4(),
                original_text=text,
                content_provenance="OPERATOR_AUTHORED",
                rights_asserted=True,
                consent_asserted=True,
                rights_assertion_status="UNVERIFIED_OPERATOR_ASSERTION",
                platform_permission_status="NOT_ESTABLISHED",
                attachment=normalized_attachment,
                private_notes=notes,
                scheduled_at=scheduled,
                created_at=now,
                updated_at=now,
                editorial_status="DRAFT",
                reviewed_at=None,
                review_note=None,
                remote_publication_status="NOT_PUBLISHED_BY_PLANNER",
            )
            updated = RecruitmentContentCalendarV1(
                account_uuid=account,
                packets=(*calendar.packets, packet),
            )
            self._write_unlocked(root, updated)
            return RecruitmentContentCreateResult(packet, warnings, True)

    def list(self, account_id: UUID) -> RecruitmentContentCalendarV1:
        account = _validate_account_id(account_id)
        with self.account_lock(account):
            calendar = self._read_unlocked(self._required_root(), account)
            return replace(calendar, packets=self._chronological(calendar.packets))

    def get(self, account_id: UUID, packet_id: UUID) -> RecruitmentContentPacketV1:
        account = _validate_account_id(account_id)
        identifier = _validate_account_id(packet_id)
        calendar = self.list(account)
        for packet in calendar.packets:
            if packet.packet_id == identifier:
                return packet
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_ENTRY_NOT_FOUND")

    def get_review_snapshot(
        self,
        account_uuid: UUID,
        packet_id: UUID,
    ) -> RecruitmentContentReviewSnapshotV1:
        """Read one immutable packet and its complete, versioned review fingerprint."""

        account = _validate_account_id(account_uuid)
        identifier = _validate_account_id(packet_id)
        with self.account_lock(account):
            root = self._required_root()
            calendar = self._read_unlocked(root, account)
            for packet in calendar.packets:
                if packet.packet_id == identifier:
                    return RecruitmentContentReviewSnapshotV1(
                        packet=packet,
                        fingerprint=self._review_fingerprint(packet),
                    )
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_ENTRY_NOT_FOUND")

    def review_if_unchanged(
        self,
        account_uuid: UUID,
        packet_id: UUID,
        *,
        expected_fingerprint: str,
        expected_editorial_status: ContentEditorialStatusV1,
        decision: Literal["APPROVE", "REJECT"],
        note: str | None = None,
    ) -> RecruitmentContentPacketV1:
        """Apply a confirmed editorial decision only to its unchanged packet snapshot."""

        account = _validate_account_id(account_uuid)
        identifier = _validate_account_id(packet_id)
        if (
            type(expected_fingerprint) is not str
            or _REVIEW_FINGERPRINT_RE.fullmatch(expected_fingerprint) is None
            or type(expected_editorial_status) is not str
            or expected_editorial_status not in _EDITORIAL_STATUSES
            or type(decision) is not str
            or decision not in _REVIEW_DECISIONS
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")

        if decision == "REJECT" and note is None:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_REVIEW_INVALID")
        normalized_note = _normalize_text(
            note,
            maximum=_MAX_REVIEW_NOTE_CHARS,
            empty_ok=decision == "APPROVE",
        )
        if decision == "REJECT" and normalized_note is None:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_REVIEW_INVALID")
        if normalized_note is not None:
            self._reject_private_content(normalized_note)

        with self.account_lock(account):
            root = self._required_root()
            try:
                calendar = self._read_unlocked(root, account)
            except RecruitmentContentPlannerError:
                # A packet that cannot be strictly reread is no longer the reviewed
                # v1 snapshot. Fail closed without exposing document details.
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_REVIEW_STALE") from None

            index = next(
                (
                    index
                    for index, packet in enumerate(calendar.packets)
                    if packet.packet_id == identifier
                ),
                None,
            )
            if index is None:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_REVIEW_STALE")
            current = calendar.packets[index]
            current_fingerprint = self._review_fingerprint(current)
            if (
                current.account_uuid != account
                or current.packet_id != identifier
                or current.editorial_status != expected_editorial_status
                or current.editorial_status != "DRAFT"
                or not hmac.compare_digest(current_fingerprint, expected_fingerprint)
            ):
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_REVIEW_STALE")

            reviewed = max(self._now(), current.updated_at, current.created_at)
            updated = replace(
                current,
                editorial_status=(
                    "READY_FOR_MANUAL_PUBLICATION" if decision == "APPROVE" else "REJECTED"
                ),
                updated_at=reviewed,
                reviewed_at=reviewed,
                review_note=normalized_note,
            )
            return self._replace_packet(root, calendar, index, updated)

    def approve(
        self,
        account_id: UUID,
        packet_id: UUID,
        *,
        review_note: str | None = None,
    ) -> RecruitmentContentPacketV1:
        account = _validate_account_id(account_id)
        identifier = _validate_account_id(packet_id)
        note = _normalize_text(review_note, maximum=_MAX_REVIEW_NOTE_CHARS, empty_ok=True)
        if note is not None:
            self._reject_private_content(note)
        with self.account_lock(account):
            root = self._required_root()
            calendar = self._read_unlocked(root, account)
            index = self._index(calendar, identifier)
            previous = calendar.packets[index]
            if previous.editorial_status == "REJECTED":
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_REVIEW_INVALID")
            if previous.editorial_status == "READY_FOR_MANUAL_PUBLICATION":
                return previous
            reviewed = max(self._now(), previous.updated_at, previous.created_at)
            approved = replace(
                previous,
                editorial_status="READY_FOR_MANUAL_PUBLICATION",
                updated_at=reviewed,
                reviewed_at=reviewed,
                review_note=note,
            )
            return self._replace_packet(root, calendar, index, approved)

    def reject(
        self,
        account_id: UUID,
        packet_id: UUID,
        *,
        reason: str,
    ) -> RecruitmentContentPacketV1:
        account = _validate_account_id(account_id)
        identifier = _validate_account_id(packet_id)
        note = _normalize_text(reason, maximum=_MAX_REVIEW_NOTE_CHARS)
        assert note is not None
        self._reject_private_content(note)
        with self.account_lock(account):
            root = self._required_root()
            calendar = self._read_unlocked(root, account)
            index = self._index(calendar, identifier)
            previous = calendar.packets[index]
            if previous.editorial_status == "REJECTED":
                return previous
            reviewed = max(self._now(), previous.updated_at, previous.created_at)
            rejected = replace(
                previous,
                editorial_status="REJECTED",
                updated_at=reviewed,
                reviewed_at=reviewed,
                review_note=note,
            )
            return self._replace_packet(root, calendar, index, rejected)

    def due(
        self,
        account_id: UUID,
        *,
        now: datetime | None = None,
    ) -> tuple[RecruitmentContentPacketV1, ...]:
        account = _validate_account_id(account_id)
        with self.account_lock(account):
            calendar = self._read_unlocked(self._required_root(), account)
            cutoff = self._now() if now is None else _utc_datetime(now)
            return tuple(
                packet
                for packet in self._chronological(calendar.packets)
                if packet.editorial_status != "REJECTED" and packet.scheduled_at <= cutoff
            )

    def upcoming(
        self,
        account_id: UUID,
        *,
        now: datetime | None = None,
    ) -> tuple[RecruitmentContentPacketV1, ...]:
        account = _validate_account_id(account_id)
        with self.account_lock(account):
            calendar = self._read_unlocked(self._required_root(), account)
            cutoff = self._now() if now is None else _utc_datetime(now)
            return tuple(
                packet
                for packet in self._chronological(calendar.packets)
                if packet.editorial_status != "REJECTED" and packet.scheduled_at > cutoff
            )

    def _now(self) -> datetime:
        try:
            return _utc_datetime(self._utc_now())
        except TypeError, ValueError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None

    def _index(self, calendar: RecruitmentContentCalendarV1, packet_id: UUID) -> int:
        for index, packet in enumerate(calendar.packets):
            if packet.packet_id == packet_id:
                return index
        raise RecruitmentContentPlannerError("CONTENT_PLANNER_ENTRY_NOT_FOUND")

    @staticmethod
    def _chronological(
        packets: tuple[RecruitmentContentPacketV1, ...],
    ) -> tuple[RecruitmentContentPacketV1, ...]:
        return tuple(
            sorted(
                packets,
                key=lambda packet: (
                    packet.scheduled_at,
                    packet.created_at,
                    str(packet.packet_id),
                ),
            )
        )

    def _replace_packet(
        self,
        root: Path,
        calendar: RecruitmentContentCalendarV1,
        index: int,
        packet: RecruitmentContentPacketV1,
    ) -> RecruitmentContentPacketV1:
        packets = list(calendar.packets)
        packets[index] = packet
        self._write_unlocked(
            root,
            RecruitmentContentCalendarV1(
                account_uuid=calendar.account_uuid, packets=tuple(packets)
            ),
        )
        return packet

    @staticmethod
    def _review_fingerprint(packet: RecruitmentContentPacketV1) -> str:
        """Hash the complete persisted packet using a stable, domain-separated codec."""

        try:
            canonical = json.dumps(
                RecruitmentContentPlannerStore._encode_packet(packet),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", errors="strict")
        except TypeError, UnicodeError, ValueError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
        if len(canonical) > _MAX_DOCUMENT_BYTES:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
        digest = hashlib.sha256(_REVIEW_FINGERPRINT_DOMAIN + canonical).hexdigest()
        return f"{_REVIEW_FINGERPRINT_PREFIX}{digest}"

    def _normalize_attachment(
        self,
        value: ContentAttachmentMetadataV1 | None,
    ) -> ContentAttachmentMetadataV1 | None:
        if value is None:
            return None
        if type(value) is not ContentAttachmentMetadataV1:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
        if type(value.kind) is not str or value.kind not in _ATTACHMENT_KINDS:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID")
        if value.rights_asserted is not True or value.consent_asserted is not True:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_RIGHTS_REQUIRED")
        url = _require_public_url(value.url)
        label = _normalize_text(value.label, maximum=_MAX_LABEL_CHARS, empty_ok=True)
        attribution = _normalize_text(
            value.attribution,
            maximum=_MAX_ATTRIBUTION_CHARS,
            empty_ok=True,
        )
        for text in (label, attribution):
            if text is not None:
                self._reject_private_content(text)
        return ContentAttachmentMetadataV1(
            kind=cast(AttachmentKindV1, value.kind),
            url=url,
            label=label,
            attribution=attribution,
            rights_asserted=True,
            consent_asserted=True,
        )

    @staticmethod
    def _reject_private_content(value: str) -> None:
        if _has_private_content(value):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_PRIVATE_CONTENT_REJECTED")

    @staticmethod
    def _duplicate_warnings(
        packets: tuple[RecruitmentContentPacketV1, ...],
        original_text: str,
    ) -> tuple[ContentDuplicateWarning, ...]:
        key = _content_key(original_text)
        if len(key) < _NEAR_DUPLICATE_MIN_CHARS:
            return ()
        matches: list[tuple[float, UUID]] = []
        for packet in packets:
            prior = _content_key(packet.original_text)
            if len(prior) < _NEAR_DUPLICATE_MIN_CHARS:
                continue
            similarity = difflib.SequenceMatcher(None, key, prior, autojunk=False).ratio()
            if similarity >= _NEAR_DUPLICATE_THRESHOLD:
                matches.append((similarity, packet.packet_id))
        matches.sort(key=lambda match: (-match[0], str(match[1])))
        return tuple(
            ContentDuplicateWarning(
                code="NEAR_DUPLICATE",
                matched_packet_id=packet_id,
                similarity_percent=round(similarity * 100),
            )
            for similarity, packet_id in matches[:5]
        )

    def _required_root(self) -> Path:
        try:
            metadata = self._configured_root.lstat()
            resolved = self._configured_root.resolve(strict=True)
        except OSError, RuntimeError, ValueError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        if (
            _is_reparse_point(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != self._configured_root
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        return resolved

    def _directory(self, parent: Path, name: str, root: Path, *, create: bool) -> Path | None:
        path = parent / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
            try:
                metadata = path.lstat()
            except OSError:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        except OSError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        try:
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        if (
            _is_reparse_point(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or not _private_mode(metadata)
            or resolved != path
            or path.parent != parent
            or not resolved.is_relative_to(root)
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        return path

    def _calendar_path(self, root: Path, account: UUID, *, create: bool) -> Path | None:
        namespace = self._directory(root, _NAMESPACE, root, create=create)
        if namespace is None:
            return None
        accounts = self._directory(namespace, "accounts", root, create=create)
        if accounts is None:
            return None
        return accounts / f"{account}.json"

    def _regular_file(self, path: Path, parent: Path, *, allow_missing: bool) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return False
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        except OSError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        try:
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        if (
            _is_reparse_point(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or not _private_mode(metadata)
            or metadata.st_size > _MAX_DOCUMENT_BYTES
            or path.parent != parent
            or resolved != path
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        return True

    def _read_file(self, path: Path, parent: Path) -> bytes:
        if not self._regular_file(path, parent, allow_missing=False):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        try:
            before = path.lstat()
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                after = path.lstat()
                if (
                    _is_reparse_point(opened)
                    or not stat.S_ISREG(opened.st_mode)
                    or not _private_mode(opened)
                    or _file_identity(before) != _file_identity(opened)
                    or _file_identity(opened) != _file_identity(after)
                ):
                    raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
                raw = stream.read(_MAX_DOCUMENT_BYTES + 1)
        except RecruitmentContentPlannerError:
            raise
        except OSError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        return raw

    def _parse_json(self, raw: bytes) -> object:
        try:
            return json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None

    def _read_unlocked(self, root: Path, account: UUID) -> RecruitmentContentCalendarV1:
        target = self._calendar_path(root, account, create=False)
        if target is None or not self._regular_file(
            target,
            target.parent,
            allow_missing=True,
        ):
            return RecruitmentContentCalendarV1(account_uuid=account, packets=())
        raw = self._read_file(target, target.parent)
        return self._decode_calendar(self._parse_json(raw), account)

    def _decode_calendar(self, value: object, account: UUID) -> RecruitmentContentCalendarV1:
        if type(value) is not dict:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _TOP_LEVEL_KEYS:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if type(data["version"]) is not int or data["version"] != _VERSION:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if data["account_uuid"] != str(account) or type(data["account_uuid"]) is not str:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_ACCOUNT_MISMATCH")
        raw_packets = data["packets"]
        if type(raw_packets) is not list:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        packet_values = cast(list[object], raw_packets)
        if len(packet_values) > _MAX_RETAINED:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        packets = tuple(self._decode_packet(item, account) for item in packet_values)
        ids = [packet.packet_id for packet in packets]
        keys = [_content_key(packet.original_text) for packet in packets]
        if len(set(ids)) != len(ids):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_DUPLICATE_ID")
        if len(set(keys)) != len(keys):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        calendar = RecruitmentContentCalendarV1(account_uuid=account, packets=packets)
        if calendar.active_count > _MAX_ACTIVE:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        return calendar

    def _decode_packet(self, value: object, account: UUID) -> RecruitmentContentPacketV1:
        if type(value) is not dict:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _PACKET_KEYS:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if type(data["version"]) is not int or data["version"] != _VERSION:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if data["account_uuid"] != str(account) or type(data["account_uuid"]) is not str:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_ACCOUNT_MISMATCH")
        packet_id = _parse_uuid4(data["packet_id"])
        text = _normalize_text(data["original_text"], maximum=_MAX_TEXT_CHARS)
        assert text is not None
        if text != data["original_text"]:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        self._reject_private_content(text)
        if data["content_provenance"] != "OPERATOR_AUTHORED":
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_SOURCE_INVALID")
        if (
            data["rights_asserted"] is not True
            or data["consent_asserted"] is not True
            or data["rights_assertion_status"] != "UNVERIFIED_OPERATOR_ASSERTION"
            or data["platform_permission_status"] != "NOT_ESTABLISHED"
            or data["remote_publication_status"] != "NOT_PUBLISHED_BY_PLANNER"
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_RIGHTS_REQUIRED")
        raw_attachment = data["attachment"]
        attachment = None if raw_attachment is None else self._decode_attachment(raw_attachment)
        notes = _normalize_text(data["private_notes"], maximum=_MAX_NOTES_CHARS, empty_ok=True)
        if notes != data["private_notes"]:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if notes is not None:
            self._reject_private_content(notes)
        scheduled = _parse_utc(data["scheduled_at"])
        created = _parse_utc(data["created_at"])
        updated = _parse_utc(data["updated_at"])
        if updated < created or abs(scheduled - created) > _MAX_SCHEDULE_HORIZON:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        raw_status = data["editorial_status"]
        if type(raw_status) is not str or raw_status not in _EDITORIAL_STATUSES:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        status = cast(ContentEditorialStatusV1, raw_status)
        raw_reviewed = data["reviewed_at"]
        reviewed = None if raw_reviewed is None else _parse_utc(raw_reviewed)
        review_note = _normalize_text(
            data["review_note"],
            maximum=_MAX_REVIEW_NOTE_CHARS,
            empty_ok=True,
        )
        if review_note != data["review_note"]:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if review_note is not None:
            self._reject_private_content(review_note)
        if status == "DRAFT":
            if reviewed is not None or review_note is not None:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        elif (
            reviewed is None
            or reviewed < created
            or reviewed > updated
            or status == "REJECTED"
            and review_note is None
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        return RecruitmentContentPacketV1(
            version=1,
            account_uuid=account,
            packet_id=packet_id,
            original_text=text,
            content_provenance="OPERATOR_AUTHORED",
            rights_asserted=True,
            consent_asserted=True,
            rights_assertion_status="UNVERIFIED_OPERATOR_ASSERTION",
            platform_permission_status="NOT_ESTABLISHED",
            attachment=attachment,
            private_notes=notes,
            scheduled_at=scheduled,
            created_at=created,
            updated_at=updated,
            editorial_status=status,
            reviewed_at=reviewed,
            review_note=review_note,
            remote_publication_status="NOT_PUBLISHED_BY_PLANNER",
        )

    def _decode_attachment(self, value: object) -> ContentAttachmentMetadataV1:
        if type(value) is not dict:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _ATTACHMENT_KEYS:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        kind = data["kind"]
        if type(kind) is not str or kind not in _ATTACHMENT_KINDS:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        if (
            data["rights_asserted"] is not True
            or data["consent_asserted"] is not True
            or data["rights_assertion_status"] != "UNVERIFIED_OPERATOR_ASSERTION"
        ):
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_RIGHTS_REQUIRED")
        url = _require_public_url(data["url"])
        label = _normalize_text(data["label"], maximum=_MAX_LABEL_CHARS, empty_ok=True)
        attribution = _normalize_text(
            data["attribution"],
            maximum=_MAX_ATTRIBUTION_CHARS,
            empty_ok=True,
        )
        if label != data["label"] or attribution != data["attribution"]:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        for text in (label, attribution):
            if text is not None:
                self._reject_private_content(text)
        return ContentAttachmentMetadataV1(
            kind=cast(AttachmentKindV1, kind),
            url=url,
            label=label,
            attribution=attribution,
            rights_asserted=True,
            consent_asserted=True,
        )

    def _encode_calendar(self, calendar: RecruitmentContentCalendarV1) -> bytes:
        document = {
            "version": _VERSION,
            "account_uuid": str(calendar.account_uuid),
            "packets": [self._encode_packet(packet) for packet in calendar.packets],
        }
        try:
            raw = (
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode(
                "utf-8",
                errors="strict",
            )
        except UnicodeError, ValueError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_INVALID") from None
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_LIMIT_REACHED")
        return raw

    @staticmethod
    def _encode_packet(packet: RecruitmentContentPacketV1) -> dict[str, object]:
        attachment = packet.attachment
        attachment_data: dict[str, object] | None = None
        if attachment is not None:
            attachment_data = {
                "kind": attachment.kind,
                "url": attachment.url,
                "label": attachment.label,
                "attribution": attachment.attribution,
                "rights_asserted": attachment.rights_asserted,
                "consent_asserted": attachment.consent_asserted,
                "rights_assertion_status": "UNVERIFIED_OPERATOR_ASSERTION",
            }
        return {
            "version": 1,
            "account_uuid": str(packet.account_uuid),
            "packet_id": str(packet.packet_id),
            "original_text": packet.original_text,
            "content_provenance": packet.content_provenance,
            "rights_asserted": packet.rights_asserted,
            "consent_asserted": packet.consent_asserted,
            "rights_assertion_status": packet.rights_assertion_status,
            "platform_permission_status": packet.platform_permission_status,
            "attachment": attachment_data,
            "private_notes": packet.private_notes,
            "scheduled_at": _format_utc(packet.scheduled_at),
            "created_at": _format_utc(packet.created_at),
            "updated_at": _format_utc(packet.updated_at),
            "editorial_status": packet.editorial_status,
            "reviewed_at": None if packet.reviewed_at is None else _format_utc(packet.reviewed_at),
            "review_note": packet.review_note,
            "remote_publication_status": packet.remote_publication_status,
        }

    def _write_unlocked(self, root: Path, calendar: RecruitmentContentCalendarV1) -> None:
        account = _validate_account_id(calendar.account_uuid)
        document = self._encode_calendar(calendar)
        decoded = self._decode_calendar(self._parse_json(document), account)
        if decoded != calendar:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
        target = self._calendar_path(root, account, create=True)
        assert target is not None
        self._regular_file(target, target.parent, allow_missing=True)
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            staged = self._decode_calendar(
                self._parse_json(self._read_file(temporary_path, target.parent)),
                account,
            )
            if staged != calendar:
                raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID")
            self._regular_file(target, target.parent, allow_missing=True)
            os.replace(temporary_path, target)
        except RecruitmentContentPlannerError:
            raise
        except OSError:
            raise RecruitmentContentPlannerError("CONTENT_PLANNER_STATE_INVALID") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
