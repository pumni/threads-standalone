"""Synthetic-only, account-scoped versioned archive for recruitment vacancies.

The store requires an explicitly supplied data root and retention policy. It
does not read environment variables, account state, browser data, or network
sources. Live collection and real contact persistence remain separately gated.
"""

from __future__ import annotations

import csv
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeGuard, cast
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone.recruitment_job_vacancy import (
    AuthorEvidenceStatus,
    CompletenessBasis,
    Confidence,
    EvidenceState,
    ExtractionResult,
    ExtractionStatus,
    FieldEvidence,
    ObservationSource,
    PostKind,
    PostObservation,
    ReasonCode,
    RecruitmentJobPostV1,
    TextCompleteness,
    TextProvenance,
    VacancyClassification,
    extract_vietnamese_vacancies,
)

SCHEMA_VERSION = 1
MAX_POSTS_PER_ACCOUNT = 200
MAX_OBSERVATIONS_PER_ACCOUNT = 200
MAX_OBSERVATIONS_PER_POST = 200
MAX_REVISIONS_PER_POST = 200
MAX_VACANCY_REVISIONS_PER_ACCOUNT = 1_000
MAX_VACANCIES_PER_REVISION = 20
MAX_TEXT_CODEPOINTS = 10_000
MAX_FIELD_CODEPOINTS = 10_000
MAX_ACCOUNT_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_EXPORT_ROWS = 4_000
MAX_EXPORT_FIELDS = 32
MAX_ROLE_ANCHOR_LENGTH = 256
_NAMESPACE = "job_vacancies_v1"
_ACCOUNT_FILE = "archive.json"
_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYNTHETIC_AUTHOR_RE = re.compile(
    r"^(?:synthetic|fake)-[A-Za-z0-9._:-]{1,96}$|^\[(?:SYNTHETIC|FAKE)_AUTHOR_[0-9]{3}\]$"
)
_SAFE_CONTACT_PLACEHOLDER_RE = re.compile(
    r"^\[(?:SYNTHETIC_(?:PHONE|CONTACT)|FAKE_CONTACT)_[0-9]{3}\]$"
)
_PHONE_LIKE_RE = re.compile(r"(?<![\w])(?:\+?84|0)(?:[ \t().-]*\d){8,10}(?!\w)")
_PHONE_PLACEHOLDER_DIGITS_RE = re.compile(
    r"\[(?:SYNTHETIC_PHONE|SYNTHETIC_CONTACT|FAKE_CONTACT)_[0-9]{8,}\]",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_CONTACT_LABEL_RE = re.compile(
    r"(?i)(?:s[đd]t|điện\s+thoại|phone|zalo|liên\s+hệ|contact)\s*[:：-]?\s*"
)
_CONTACT_SEGMENT_DIGITS_RE = re.compile(r"\d")
_CSV_FORMULA_PREFIX_RE = re.compile(r"^[\s\x00-\x1f\x7f-\x9f]*[=+\-@]")
_SOURCE_RIGHTS = "UNVERIFIED_SYNTHETIC_DECLARATION"

_ARCHIVE_KEYS = frozenset({"schema_version", "account_uuid", "posts"})
_POST_KEYS = frozenset(
    {
        "identity_sha256",
        "canonical_post_url",
        "created_at_utc",
        "latest_observed_at_utc",
        "deletion_state",
        "deleted_at_utc",
        "retention_state",
        "source_rights_metadata",
        "observations",
        "revisions",
        "vacancy_states",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "observation_id",
        "post_revision_id",
        "observed_at_utc",
        "published_at_utc",
        "source",
        "text_provenance",
        "completeness_basis",
        "text_completeness",
        "text_truncated",
        "status",
        "post_kind",
        "classification",
        "extraction_status",
        "confidence",
        "reason_codes",
        "author_evidence",
        "author_label_present",
        "related_post_identity_sha256",
        "related_author_evidence",
        "source_rights_metadata",
        "vacancy_revision_ids",
        "retention_state",
    }
)
_REVISION_KEYS = frozenset(
    {
        "post_revision_id",
        "content_sha256",
        "vacancy_digest",
        "observed_text",
        "text_completeness",
        "text_truncated",
        "created_at_utc",
        "retention_state",
        "vacancy_revisions",
    }
)
_VACANCY_REVISION_KEYS = frozenset(
    {
        "vacancy_id",
        "vacancy_revision_id",
        "post_revision_id",
        "role_anchor",
        "extraction_version",
        "classification",
        "confidence",
        "reason_codes",
        "fields",
        "retention_state",
    }
)
_VACANCY_STATE_KEYS = frozenset(
    {
        "vacancy_id",
        "role_anchor",
        "latest_vacancy_revision_id",
        "state",
        "last_observation_id",
    }
)
_FIELD_GROUP_KEYS = frozenset(
    {
        "employer",
        "role_title",
        "role_category",
        "headcount",
        "location",
        "work_mode",
        "schedule",
        "salary",
        "experience",
        "requirements",
        "benefits",
        "contacts",
    }
)
_EVIDENCE_KEYS = frozenset({"state", "value", "raw_value", "spans"})
_CONTACT_EVIDENCE_KEYS = frozenset({"state", "present", "evidence_count"})
_GENERAL_EVIDENCE_STATES = frozenset(
    {"SOURCE_SPAN", "ABSENT", "AMBIGUOUS", "REDACTED_BY_RETENTION"}
)
_COMPLETENESS_VALUES = frozenset({"FULL_VISIBLE", "PARTIAL", "UNKNOWN"})
_POST_STATUS_VALUES = frozenset({"COMPLETE", "PARTIAL", "UNKNOWN"})
_POST_KIND_VALUES = frozenset({"ORIGINAL", "REPOST", "QUOTE", "REPLY", "UNKNOWN"})
_CLASSIFICATION_VALUES = frozenset({"RECRUITER_VACANCY", "JOB_SEEKER", "OTHER", "UNKNOWN"})
_EXTRACTION_STATUS_VALUES = frozenset(
    {"EXTRACTED", "PARTIAL", "NON_VACANCY", "UNKNOWN", "REJECTED"}
)
_CONFIDENCE_VALUES = frozenset({"HIGH", "MEDIUM", "LOW", "UNKNOWN"})
_RETENTION_VALUES = frozenset({"RAW_RETAINED", "RAW_PURGED", "DERIVED_PURGED"})
_VACANCY_STATE_VALUES = frozenset({"ACTIVE", "NOT_REOBSERVED", "EXPIRED"})
_OBSERVATION_REASON_VALUES = frozenset(
    {
        "INVALID_ACCOUNT",
        "INVALID_QUERY",
        "INVALID_TARGET",
        "INVALID_LIMITS",
        "INVALID_CAPTURE_ID",
        "INVALID_CAPTURE_TIMESTAMP",
        "INVALID_SESSION_STATE",
        "SESSION_FAILURE",
        "CLOCK_FAILURE",
        "CLOCK_MOVED_BACKWARD",
        "STOP_SIGNAL_FAILURE",
        "STOP_REQUESTED",
        "TIMEOUT",
        "WRONG_ORIGIN",
        "REDIRECT_DETECTED",
        "CHALLENGE",
        "LOGIN_REQUIRED",
        "RATE_LIMITED",
        "UNCLASSIFIED_STATE",
        "ACCOUNT_MISMATCH",
        "QUERY_MISMATCH",
        "BATCH_MISMATCH",
        "CARD_ID_INVALID",
        "CARD_ID_REUSED",
        "AUTHOR_MISMATCH",
        "BATCH_LIMIT_REACHED",
        "TRANCHE_LIMIT_REACHED",
        "CANDIDATE_LIMIT_REACHED",
        "DETAIL_LIMIT_REACHED",
        "TARGET_NOT_CAPTURED",
        "POST_MISSING",
        "TARGET_MOVED",
        "TEXT_LIMIT_REACHED",
        "TEXT_PARTIAL",
        "TEXT_COMPLETENESS_UNKNOWN",
        "TEXT_UNAVAILABLE",
        "IMAGE_ONLY",
        "AUTHOR_UNAVAILABLE",
        "POST_KIND_UNKNOWN",
        "RELATIONSHIP_UNAVAILABLE",
        "TIMESTAMP_UNAVAILABLE",
    }
)


class StoreErrorCode(StrEnum):
    INVALID_INPUT = "VACANCY_STORE_INVALID_INPUT"
    ROOT_INVALID = "VACANCY_STORE_ROOT_INVALID"
    PERMISSIONS_UNVERIFIED = "VACANCY_STORE_PERMISSIONS_UNVERIFIED"
    SOURCE_NOT_ALLOWED = "VACANCY_STORE_SOURCE_NOT_ALLOWED"
    CONTACT_DATA_BLOCKED = "VACANCY_STORE_CONTACT_DATA_BLOCKED"
    ARCHIVE_CORRUPT = "VACANCY_STORE_ARCHIVE_CORRUPT"
    VERSION_UNSUPPORTED = "VACANCY_STORE_VERSION_UNSUPPORTED"
    ACCOUNT_MISMATCH = "VACANCY_STORE_ACCOUNT_MISMATCH"
    OBSERVATION_CONFLICT = "VACANCY_STORE_OBSERVATION_CONFLICT"
    REVISION_CONFLICT = "VACANCY_STORE_REVISION_CONFLICT"
    POST_DELETED = "VACANCY_STORE_POST_DELETED"
    BUSY = "VACANCY_STORE_BUSY"
    LIMIT_REACHED = "VACANCY_STORE_LIMIT_REACHED"
    PERSISTENCE_FAILED = "VACANCY_STORE_PERSISTENCE_FAILED"
    EXPORT_CONFIRMATION_REQUIRED = "VACANCY_STORE_EXPORT_CONFIRMATION_REQUIRED"
    EXPORT_FIELD_INVALID = "VACANCY_STORE_EXPORT_FIELD_INVALID"
    CONTACT_EXPORT_BLOCKED = "VACANCY_STORE_CONTACT_EXPORT_BLOCKED"
    NOT_FOUND = "VACANCY_STORE_NOT_FOUND"


class RecruitmentJobStoreError(Exception):
    """Fixed-code error that never includes source content or local paths."""

    def __init__(self, code: StoreErrorCode) -> None:
        self.code = code
        super().__init__(code.value)

    def __str__(self) -> str:
        return self.code.value

    def __repr__(self) -> str:
        return f"RecruitmentJobStoreError({self.code.value!r})"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Explicit maximum retention for synthetic validation and later review."""

    raw_text_days: int
    derived_vacancy_days: int

    def __post_init__(self) -> None:
        if (
            type(self.raw_text_days) is not int
            or not 1 <= self.raw_text_days <= 30
            or type(self.derived_vacancy_days) is not int
            or not self.raw_text_days <= self.derived_vacancy_days <= 90
        ):
            raise ValueError("INVALID_RETENTION_POLICY")


class ExportField(StrEnum):
    POST_IDENTITY_SHA256 = "post_identity_sha256"
    OBSERVATION_ID = "observation_id"
    POST_REVISION_ID = "post_revision_id"
    VACANCY_ID = "vacancy_id"
    VACANCY_REVISION_ID = "vacancy_revision_id"
    VACANCY_STATE = "vacancy_state"
    OBSERVED_AT_UTC = "observed_at_utc"
    PUBLISHED_AT_UTC = "published_at_utc"
    SOURCE_KIND = "source_kind"
    TEXT_COMPLETENESS = "text_completeness"
    EMPLOYER = "employer"
    ROLE_TITLE = "role_title"
    ROLE_CATEGORY = "role_category"
    HEADCOUNT = "headcount"
    DISTRICT = "district"
    PROVINCE_CITY = "province_city"
    WORK_MODE = "work_mode"
    SCHEDULE = "schedule"
    SALARY_MIN = "salary_min"
    SALARY_MAX = "salary_max"
    SALARY_CURRENCY = "salary_currency"
    SALARY_PERIOD = "salary_period"
    SALARY_COMMISSION = "salary_commission"
    EXPERIENCE = "experience"
    REQUIREMENTS = "requirements"
    BENEFITS = "benefits"


@dataclass(frozen=True, slots=True, repr=False)
class StoreReceipt:
    status: str
    post_count: int
    observation_count: int
    revision_count: int

    def __repr__(self) -> str:
        return (
            "StoreReceipt("
            f"status={self.status!r}, posts={self.post_count}, "
            f"observations={self.observation_count}, revisions={self.revision_count})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class StoreSummary:
    account_uuid: UUID = field(repr=False)
    post_count: int
    active_post_count: int
    deleted_post_count: int
    observation_count: int
    revision_count: int
    vacancy_revision_count: int
    retained_text_revision_count: int

    def __repr__(self) -> str:
        return (
            "StoreSummary("
            f"posts={self.post_count}, active_posts={self.active_post_count}, "
            f"deleted_posts={self.deleted_post_count}, observations={self.observation_count}, "
            f"revisions={self.revision_count}, vacancy_revisions={self.vacancy_revision_count}, "
            f"retained_text_revisions={self.retained_text_revision_count})"
        )


class BrowserObservationInput(Protocol):
    """Read-only BJ-02 observation shape, declared without importing its module."""

    @property
    def schema_version(self) -> int: ...

    @property
    def observation_id(self) -> str: ...

    @property
    def account_uuid(self) -> UUID: ...

    @property
    def canonical_post_url(self) -> str: ...

    @property
    def observed_at_utc(self) -> datetime: ...

    @property
    def post_revision_id(self) -> str | None: ...

    @property
    def source(self) -> str: ...

    @property
    def text_provenance(self) -> str: ...

    @property
    def completeness_basis(self) -> str: ...

    @property
    def author_marker(self) -> str: ...

    @property
    def author_label(self) -> str | None: ...

    @property
    def visible_text(self) -> str | None: ...

    @property
    def text_completeness(self) -> str: ...

    @property
    def text_truncated(self) -> bool: ...

    @property
    def status(self) -> str: ...

    @property
    def reason_codes(self) -> tuple[str, ...]: ...

    @property
    def post_kind(self) -> str: ...

    @property
    def published_at_utc(self) -> datetime | None: ...

    @property
    def quoted_post_url(self) -> str | None: ...

    @property
    def quoted_author_label(self) -> str | None: ...

    @property
    def parent_post_url(self) -> str | None: ...

    @property
    def parent_author_label(self) -> str | None: ...

    @property
    def related_author_marker(self) -> str | None: ...


class RecruitmentJobStore:
    """Per-account synthetic archive with strict schema and atomic persistence."""

    def __init__(self, data_root: Path, *, retention_policy: RetentionPolicy) -> None:
        try:
            normalized_root = Path(data_root)
        except TypeError, ValueError:
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None
        if not normalized_root.is_absolute():
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
        if type(retention_policy) is not RetentionPolicy:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        self._configured_root = normalized_root
        self._retention_policy = retention_policy

    def record_observation(
        self,
        observation: BrowserObservationInput,
        extraction: ExtractionResult | None,
        *,
        now_utc: datetime,
    ) -> StoreReceipt:
        """Append one synthetic capture and its parser revision, preserving history."""

        normalized_now = _require_utc(now_utc)
        account_id = _validate_observation(observation, normalized_now)
        parsed = _expected_extraction(observation, extraction)
        prepared_revisions = tuple(
            _encode_vacancy_revision(record, observation.visible_text or "")
            for record in parsed.records
        )
        revision_ids = tuple(str(item["vacancy_revision_id"]) for item in prepared_revisions)
        body = observation.visible_text
        body_digest = None if body is None else _sha256(body.encode("utf-8"))
        identity_hash = _identity_sha256(account_id, observation.canonical_post_url)
        event = _encode_observation_event(
            observation,
            parsed,
            revision_ids,
        )

        with self._locked_account(account_id) as archive_path:
            archive = self._read_archive(archive_path, account_id)
            retention_changed = _apply_retention(archive, normalized_now, self._retention_policy)
            existing_event = _find_observation(archive, observation.observation_id)
            if existing_event is not None:
                if not _same_observation_event(existing_event, event):
                    raise RecruitmentJobStoreError(StoreErrorCode.OBSERVATION_CONFLICT)
                if retention_changed:
                    self._write_archive(archive_path, archive, account_id)
                return _receipt("DUPLICATE", archive)
            posts = _object_list(archive, "posts")
            post = _find_post(posts, identity_hash)
            if post is not None and post["deletion_state"] == "DELETED":
                raise RecruitmentJobStoreError(StoreErrorCode.POST_DELETED)
            if post is not None:
                stored_url = post["canonical_post_url"]
                if stored_url is not None and stored_url != observation.canonical_post_url:
                    raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
            else:
                if len(posts) >= MAX_POSTS_PER_ACCOUNT:
                    raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
                post = _new_post(
                    identity_hash,
                    observation.canonical_post_url,
                    observation.observed_at_utc,
                )
                posts.append(post)
                posts.sort(key=lambda item: str(item["identity_sha256"]))

            if _observation_count(archive) >= MAX_OBSERVATIONS_PER_ACCOUNT:
                raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
            events = _object_list(post, "observations")
            if len(events) >= MAX_OBSERVATIONS_PER_POST:
                raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)

            revisions = _object_list(post, "revisions")
            if observation.post_revision_id is not None:
                existing_revision = _find_revision(revisions, observation.post_revision_id)
                if existing_revision is not None:
                    if existing_revision["content_sha256"] != body_digest or existing_revision[
                        "vacancy_digest"
                    ] != _vacancy_digest(prepared_revisions):
                        raise RecruitmentJobStoreError(StoreErrorCode.REVISION_CONFLICT)
                    if existing_revision["retention_state"] == "DERIVED_PURGED":
                        raise RecruitmentJobStoreError(StoreErrorCode.REVISION_CONFLICT)
                else:
                    if len(revisions) >= MAX_REVISIONS_PER_POST:
                        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
                    if _vacancy_revision_count(archive) + len(prepared_revisions) > (
                        MAX_VACANCY_REVISIONS_PER_ACCOUNT
                    ):
                        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
                    revisions.append(
                        _new_revision(
                            observation,
                            body_digest,
                            prepared_revisions,
                        )
                    )

            if post["canonical_post_url"] is None:
                post["canonical_post_url"] = observation.canonical_post_url
            events.append(event)
            events.sort(
                key=lambda item: (str(item["observed_at_utc"]), str(item["observation_id"]))
            )
            post["latest_observed_at_utc"] = _format_utc(observation.observed_at_utc)
            post["retention_state"] = "RAW_RETAINED"
            _update_vacancy_states(post, event, prepared_revisions, observation)
            _apply_retention(archive, normalized_now, self._retention_policy)
            self._write_archive(archive_path, archive, account_id)
            return _receipt("STORED", archive)

    def summary(self, account_uuid: UUID) -> StoreSummary:
        account_id = _validate_account_id(account_uuid)
        with self._locked_account(account_id) as archive_path:
            archive = self._read_archive(archive_path, account_id)
            posts = _object_list(archive, "posts")
            return StoreSummary(
                account_uuid=account_id,
                post_count=len(posts),
                active_post_count=sum(post["deletion_state"] == "ACTIVE" for post in posts),
                deleted_post_count=sum(post["deletion_state"] == "DELETED" for post in posts),
                observation_count=_observation_count(archive),
                revision_count=_revision_count(archive),
                vacancy_revision_count=_vacancy_revision_count(archive),
                retained_text_revision_count=sum(
                    revision["observed_text"] is not None
                    for post in posts
                    for revision in _object_list(post, "revisions")
                ),
            )

    def apply_retention(self, account_uuid: UUID, *, now_utc: datetime) -> StoreReceipt:
        account_id = _validate_account_id(account_uuid)
        normalized_now = _require_utc(now_utc)
        with self._locked_account(account_id) as archive_path:
            archive = self._read_archive(archive_path, account_id)
            changed = _apply_retention(archive, normalized_now, self._retention_policy)
            if changed:
                self._write_archive(archive_path, archive, account_id)
            return _receipt("RETENTION_APPLIED" if changed else "NO_CHANGE", archive)

    def delete_post(
        self,
        account_uuid: UUID,
        canonical_post_url: str,
        *,
        now_utc: datetime,
    ) -> StoreReceipt:
        account_id = _validate_account_id(account_uuid)
        normalized_now = _require_utc(now_utc)
        if not _valid_synthetic_post_url(canonical_post_url):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        identity_hash = _identity_sha256(account_id, canonical_post_url)
        with self._locked_account(account_id) as archive_path:
            archive = self._read_archive(archive_path, account_id)
            post = _find_post(_object_list(archive, "posts"), identity_hash)
            if post is None or post["deletion_state"] == "DELETED":
                raise RecruitmentJobStoreError(StoreErrorCode.NOT_FOUND)
            post["canonical_post_url"] = None
            post["deletion_state"] = "DELETED"
            post["deleted_at_utc"] = _format_utc(normalized_now)
            post["retention_state"] = "DERIVED_PURGED"
            post["observations"] = []
            post["revisions"] = []
            post["vacancy_states"] = []
            self._write_archive(archive_path, archive, account_id)
            return _receipt("DELETED", archive)

    def export_json(
        self,
        account_uuid: UUID,
        *,
        fields: Sequence[ExportField | str],
        confirmed: bool,
        now_utc: datetime,
        include_public_recruiter_contact: bool = False,
    ) -> str:
        """Return a confirmed, account-scoped JSON projection without contacts."""

        selected = _validate_export_request(fields, confirmed, include_public_recruiter_contact)
        account_id = _validate_account_id(account_uuid)
        normalized_now = _require_utc(now_utc)
        with self._locked_account(account_id) as archive_path:
            archive = self._read_archive(archive_path, account_id)
            if _apply_retention(archive, normalized_now, self._retention_policy):
                self._write_archive(archive_path, archive, account_id)
            rows = _export_rows(archive, selected)
        result = {
            "schema_version": 1,
            "account_uuid": str(account_id),
            "fields": [item.value for item in selected],
            "contact_fields_included": False,
            "records": rows,
        }
        return json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def export_csv(
        self,
        account_uuid: UUID,
        *,
        fields: Sequence[ExportField | str],
        confirmed: bool,
        now_utc: datetime,
        include_public_recruiter_contact: bool = False,
    ) -> str:
        """Return a confirmed local CSV projection with spreadsheet formula defense."""

        selected = _validate_export_request(fields, confirmed, include_public_recruiter_contact)
        account_id = _validate_account_id(account_uuid)
        normalized_now = _require_utc(now_utc)
        with self._locked_account(account_id) as archive_path:
            archive = self._read_archive(archive_path, account_id)
            if _apply_retention(archive, normalized_now, self._retention_policy):
                self._write_archive(archive_path, archive, account_id)
            rows = _export_rows(archive, selected)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(item.value for item in selected)
        for row in rows:
            writer.writerow(_csv_cell(row.get(item.value)) for item in selected)
        return stream.getvalue()

    @contextmanager
    def _locked_account(self, account_id: UUID) -> Generator[Path]:
        root = self._validate_root()
        namespace, created = _ensure_directory(root, _NAMESPACE)
        if created:
            _secure_directory(namespace, configure_windows_acl=True)
        else:
            _verify_private_directory(namespace)
        locks, _ = _ensure_directory(namespace, "locks")
        accounts, _ = _ensure_directory(namespace, "accounts")
        _verify_private_directory(locks)
        _verify_private_directory(accounts)
        lock_path = locks / f"{account_id}.lock"
        _require_parented_child(lock_path, locks)
        _ensure_private_lock_file(lock_path, locks)
        lock = FilesystemProcessLock(lock_path)
        try:
            lock.acquire()
        except ProcessAlreadyRunning:
            raise RecruitmentJobStoreError(StoreErrorCode.BUSY) from None
        except OSError:
            raise RecruitmentJobStoreError(StoreErrorCode.PERSISTENCE_FAILED) from None
        try:
            _verify_regular_file(lock_path, locks, allow_missing=False, max_bytes=1)
            _secure_file(lock_path)
            archive_path = accounts / f"{account_id}.json"
            _require_parented_child(archive_path, accounts)
            yield archive_path
        finally:
            if lock.held:
                try:
                    lock.release()
                except OSError:
                    raise RecruitmentJobStoreError(StoreErrorCode.PERSISTENCE_FAILED) from None

    def _validate_root(self) -> Path:
        root = self._configured_root
        try:
            _validate_path_components(root)
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
                raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
            resolved = root.resolve(strict=True)
            _ensure_outside_checkout(resolved)
            if os.name == "nt":
                _verify_windows_private_acl(root)
            elif stat.S_IMODE(info.st_mode) & 0o077:
                raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
            return root
        except RecruitmentJobStoreError:
            raise
        except OSError, RuntimeError, ValueError:
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None

    def _read_archive(self, path: Path, account_id: UUID) -> dict[str, object]:
        parent = path.parent
        _verify_private_directory(parent)
        _verify_regular_file(path, parent, allow_missing=True, max_bytes=MAX_ACCOUNT_ARCHIVE_BYTES)
        try:
            raw = path.read_bytes() if path.exists() else None
        except OSError:
            raise RecruitmentJobStoreError(StoreErrorCode.PERSISTENCE_FAILED) from None
        if raw is None:
            return _new_archive(account_id)
        if len(raw) > MAX_ACCOUNT_ARCHIVE_BYTES:
            raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
        try:
            decoded: object = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT) from None
        return _validate_archive(decoded, account_id)

    def _write_archive(
        self,
        path: Path,
        archive: dict[str, object],
        account_id: UUID,
    ) -> None:
        _validate_archive(archive, account_id)
        try:
            raw = (
                json.dumps(
                    archive,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        except TypeError, ValueError, UnicodeError:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT) from None
        if len(raw) > MAX_ACCOUNT_ARCHIVE_BYTES:
            raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
        parent = path.parent
        _verify_private_directory(parent)
        existing = _verify_regular_file(
            path,
            parent,
            allow_missing=True,
            max_bytes=MAX_ACCOUNT_ARCHIVE_BYTES,
        )
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".archive-",
                suffix=".tmp",
                dir=parent,
            )
            temporary_path = Path(temporary_name)
            _secure_file(temporary_path)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _verify_regular_file(
                temporary_path,
                parent,
                allow_missing=False,
                max_bytes=MAX_ACCOUNT_ARCHIVE_BYTES,
            )
            _secure_file(temporary_path)
            if existing:
                _verify_regular_file(
                    path,
                    parent,
                    allow_missing=False,
                    max_bytes=MAX_ACCOUNT_ARCHIVE_BYTES,
                )
                os.replace(temporary_path, path)
            else:
                try:
                    os.link(temporary_path, path)
                except FileExistsError:
                    raise RecruitmentJobStoreError(StoreErrorCode.PERSISTENCE_FAILED) from None
            _verify_regular_file(
                path,
                parent,
                allow_missing=False,
                max_bytes=MAX_ACCOUNT_ARCHIVE_BYTES,
            )
            _secure_file(path)
            if os.name != "nt":
                directory_descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        except RecruitmentJobStoreError:
            raise
        except OSError:
            raise RecruitmentJobStoreError(StoreErrorCode.PERSISTENCE_FAILED) from None
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


def _validate_observation(
    observation: BrowserObservationInput,
    now_utc: datetime,
) -> UUID:
    try:
        if (
            type(observation).__name__ != "BrowserJobObservationV1"
            or type(observation).__module__ != "threads_platform.browser.job_observation"
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if type(observation.schema_version) is not int or observation.schema_version != 1:
            raise RecruitmentJobStoreError(StoreErrorCode.VERSION_UNSUPPORTED)
        account_id = _validate_account_id(observation.account_uuid)
        if not _valid_synthetic_post_url(observation.canonical_post_url):
            raise RecruitmentJobStoreError(StoreErrorCode.SOURCE_NOT_ALLOWED)
        observed_at = _require_utc(observation.observed_at_utc)
        if observed_at > now_utc:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.published_at_utc is not None:
            _require_utc(observation.published_at_utc)
        if (
            observation.source != "SYNTHETIC_FIXTURE"
            or observation.text_provenance != "SYNTHETIC_FIXTURE_TEXT"
            or observation.completeness_basis != "FIXTURE_DECLARED"
            or observation.author_marker != "UNVERIFIED"
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.SOURCE_NOT_ALLOWED)
        if observation.text_completeness not in _COMPLETENESS_VALUES:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.status not in _POST_STATUS_VALUES:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.post_kind not in _POST_KIND_VALUES:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if type(observation.text_truncated) is not bool:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.text_truncated and (
            observation.text_completeness != "PARTIAL" or observation.status == "COMPLETE"
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.text_completeness == "FULL_VISIBLE" and observation.text_truncated:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        body = observation.visible_text
        if body is None:
            if (
                observation.post_revision_id is not None
                or observation.text_completeness != "UNKNOWN"
                or observation.status != "UNKNOWN"
            ):
                raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        else:
            if type(body) is not str or len(body) > MAX_TEXT_CODEPOINTS:
                raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
            if not _valid_local_id(observation.post_revision_id):
                raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
            if observation.post_revision_id != _expected_post_revision_id(
                account_id,
                observation.canonical_post_url,
                body,
            ):
                raise RecruitmentJobStoreError(StoreErrorCode.REVISION_CONFLICT)
            if observation.text_completeness == "PARTIAL" and observation.status == "COMPLETE":
                raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
            if observation.text_completeness == "UNKNOWN" and observation.status != "UNKNOWN":
                raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
            _validate_synthetic_text(body)
        if not _valid_local_id(observation.observation_id):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if type(observation.reason_codes) is not tuple or any(
            str(code) not in _OBSERVATION_REASON_VALUES for code in observation.reason_codes
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.author_label is not None and not _SYNTHETIC_AUTHOR_RE.fullmatch(
            observation.author_label
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.SOURCE_NOT_ALLOWED)
        for label in (observation.quoted_author_label, observation.parent_author_label):
            if label is not None and not _SYNTHETIC_AUTHOR_RE.fullmatch(label):
                raise RecruitmentJobStoreError(StoreErrorCode.SOURCE_NOT_ALLOWED)
        for related_url in (observation.quoted_post_url, observation.parent_post_url):
            if related_url is not None and not _valid_synthetic_post_url(related_url):
                raise RecruitmentJobStoreError(StoreErrorCode.SOURCE_NOT_ALLOWED)
        if "IMAGE_ONLY" in {str(code) for code in observation.reason_codes} and body is not None:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if observation.related_author_marker not in (None, "UNVERIFIED"):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        return account_id
    except RecruitmentJobStoreError:
        raise
    except AttributeError, TypeError, ValueError, OverflowError:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT) from None


def _expected_extraction(
    observation: BrowserObservationInput,
    supplied: ExtractionResult | None,
) -> ExtractionResult:
    body = observation.visible_text
    if body is None:
        if supplied is not None:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        return ExtractionResult(
            status=ExtractionStatus.UNKNOWN,
            classification=VacancyClassification.UNKNOWN,
            confidence=Confidence.UNKNOWN,
            reason_codes=(),
            records=(),
            post_identity_key=(str(observation.account_uuid), observation.canonical_post_url),
            observation_id=observation.observation_id,
        )
    if type(supplied) is not ExtractionResult:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    try:
        parser_observation = PostObservation(
            observation_id=observation.observation_id,
            account_uuid=str(observation.account_uuid),
            canonical_post_url=observation.canonical_post_url,
            source=ObservationSource.SYNTHETIC_FIXTURE,
            text_provenance=TextProvenance.SYNTHETIC_FIXTURE_TEXT,
            text_completeness=(
                TextCompleteness.COMPLETE
                if observation.text_completeness == "FULL_VISIBLE"
                else TextCompleteness(observation.text_completeness)
            ),
            completeness_basis=CompletenessBasis.FIXTURE_DECLARED,
            post_revision_id=observation.post_revision_id,
            observed_text=body,
            text_truncated=observation.text_truncated,
            post_kind=PostKind(observation.post_kind),
            observed_author=observation.author_label,
            author_status=(
                AuthorEvidenceStatus.UNVERIFIED
                if observation.author_label is not None
                else AuthorEvidenceStatus.UNKNOWN
            ),
        )
        expected = extract_vietnamese_vacancies(parser_observation)
    except TypeError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT) from None
    if supplied != expected:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    if (
        expected.post_identity_key
        != (str(observation.account_uuid), observation.canonical_post_url)
        or expected.observation_id != observation.observation_id
        or expected.post_revision_id != observation.post_revision_id
        or len(expected.records) > MAX_VACANCIES_PER_REVISION
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    return expected


def _encode_observation_event(
    observation: BrowserObservationInput,
    extraction: ExtractionResult,
    vacancy_revision_ids: tuple[str, ...],
) -> dict[str, object]:
    related_url = observation.quoted_post_url or observation.parent_post_url
    related_author = observation.quoted_author_label or observation.parent_author_label
    return {
        "observation_id": observation.observation_id,
        "post_revision_id": observation.post_revision_id,
        "observed_at_utc": _format_utc(observation.observed_at_utc),
        "published_at_utc": (
            None
            if observation.published_at_utc is None
            else _format_utc(observation.published_at_utc)
        ),
        "source": "SYNTHETIC_FIXTURE",
        "text_provenance": "SYNTHETIC_FIXTURE_TEXT"
        if observation.visible_text is not None
        else "NONE",
        "completeness_basis": "FIXTURE_DECLARED"
        if observation.visible_text is not None
        else "UNKNOWN",
        "text_completeness": observation.text_completeness,
        "text_truncated": observation.text_truncated,
        "status": observation.status,
        "post_kind": observation.post_kind,
        "classification": extraction.classification.value,
        "extraction_status": extraction.status.value,
        "confidence": extraction.confidence.value,
        "reason_codes": [reason.value for reason in extraction.reason_codes],
        "author_evidence": "UNVERIFIED" if observation.author_label is not None else "UNKNOWN",
        "author_label_present": observation.author_label is not None,
        "related_post_identity_sha256": (
            None if related_url is None else _identity_sha256(observation.account_uuid, related_url)
        ),
        "related_author_evidence": "UNVERIFIED" if related_author is not None else "UNKNOWN",
        "source_rights_metadata": _SOURCE_RIGHTS,
        "vacancy_revision_ids": list(vacancy_revision_ids),
        "retention_state": "RAW_RETAINED",
    }


def _encode_vacancy_revision(
    record: RecruitmentJobPostV1,
    source_text: str,
) -> dict[str, object]:
    if (
        type(record) is not RecruitmentJobPostV1
        or record.schema_version != 1
        or record.classification is not VacancyClassification.RECRUITER_VACANCY
        or not _valid_local_id(record.observation_id)
        or not _valid_local_id(record.post_revision_id)
        or not _valid_local_id(record.vacancy_id)
        or not _valid_local_id(record.vacancy_revision_id)
        or not 1 <= len(record.role_anchor) <= MAX_ROLE_ANCHOR_LENGTH
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    fields_document = {
        "employer": _encode_evidence(record.employer, source_text),
        "role_title": _encode_evidence(record.role_title, source_text),
        "role_category": _encode_evidence(record.role_category, source_text),
        "headcount": _encode_evidence(record.headcount, source_text),
        "location": {
            "raw": _encode_evidence(record.location.raw, source_text),
            "address": _encode_evidence(record.location.address, source_text),
            "district": _encode_evidence(record.location.district, source_text),
            "province_city": _encode_evidence(record.location.province_city, source_text),
        },
        "work_mode": _encode_evidence(record.work_mode, source_text),
        "schedule": _encode_evidence(record.schedule, source_text),
        "salary": {
            "raw": _encode_evidence(record.salary.raw, source_text),
            "amount_min": _encode_evidence(record.salary.amount_min, source_text),
            "amount_max": _encode_evidence(record.salary.amount_max, source_text),
            "currency": _encode_evidence(record.salary.currency, source_text),
            "pay_period": _encode_evidence(record.salary.pay_period, source_text),
            "commission": _encode_evidence(record.salary.commission, source_text),
        },
        "experience": _encode_evidence(record.experience, source_text),
        "requirements": _encode_evidence(record.requirements, source_text),
        "benefits": _encode_evidence(record.benefits, source_text),
        "contacts": {
            "phone": _encode_contact_evidence(record.contacts.phone, "phone"),
            "zalo": _encode_contact_evidence(record.contacts.zalo, "zalo"),
            "email": _encode_contact_evidence(record.contacts.email, "email"),
            "application_url": _encode_contact_evidence(
                record.contacts.application_url, "application_url"
            ),
        },
    }
    return {
        "vacancy_id": record.vacancy_id,
        "vacancy_revision_id": record.vacancy_revision_id,
        "post_revision_id": record.post_revision_id,
        "role_anchor": record.role_anchor,
        "extraction_version": record.extraction_version,
        "classification": record.classification.value,
        "confidence": record.confidence.value,
        "reason_codes": [reason.value for reason in record.reason_codes],
        "fields": fields_document,
        "retention_state": "RAW_RETAINED",
    }


def _encode_evidence(evidence: FieldEvidence[object], source_text: str) -> dict[str, object]:
    if type(evidence) is not FieldEvidence:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    spans: list[list[int]] = []
    for span in evidence.spans:
        if span.start < 0 or span.end <= span.start or span.end > len(source_text):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        spans.append([span.start, span.end])
    if evidence.state is EvidenceState.SOURCE_SPAN:
        if evidence.value is None or not spans:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        if evidence.raw_value is not None and not any(
            source_text[start:end] == evidence.raw_value for start, end in spans
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    elif evidence.state is EvidenceState.ABSENT:
        if evidence.value is not None or evidence.raw_value is not None or spans:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    elif evidence.state is EvidenceState.AMBIGUOUS:
        if evidence.value is not None or evidence.raw_value is not None:
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    return {
        "state": evidence.state.value,
        "value": _json_value(evidence.value),
        "raw_value": evidence.raw_value,
        "spans": spans,
    }


def _encode_contact_evidence(
    evidence: FieldEvidence[str],
    contact_kind: str,
) -> dict[str, object]:
    if type(evidence) is not FieldEvidence:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    if evidence.state is EvidenceState.SOURCE_SPAN:
        if evidence.value is None or not _safe_synthetic_contact(evidence.value, contact_kind):
            raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_DATA_BLOCKED)
        if evidence.raw_value != evidence.value:
            raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_DATA_BLOCKED)
        return {
            "state": "SOURCE_SPAN",
            "present": True,
            "evidence_count": min(len(evidence.spans), 1),
        }
    if evidence.value is not None or evidence.raw_value is not None:
        raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_DATA_BLOCKED)
    return {
        "state": evidence.state.value,
        "present": bool(evidence.spans),
        "evidence_count": min(len(evidence.spans), 20),
    }


def _json_value(value: object) -> object:
    if value is None or type(value) in (str, int, bool):
        if isinstance(value, str) and len(value) > MAX_FIELD_CODEPOINTS:
            raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
        return value
    if type(value) is tuple:
        return [_json_value(item) for item in cast(tuple[object, ...], value)]
    if type(value) is list:
        return [_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, StrEnum):
        return value.value
    raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)


def _safe_synthetic_contact(value: str, contact_kind: str) -> bool:
    if contact_kind in {"phone", "zalo"}:
        return _SAFE_CONTACT_PLACEHOLDER_RE.fullmatch(value) is not None
    if contact_kind == "email":
        match = _EMAIL_RE.fullmatch(value)
        if match is None:
            return False
        domain = value.rsplit("@", 1)[1].casefold()
        return domain.endswith((".invalid", ".example"))
    if contact_kind == "application_url":
        return _valid_synthetic_post_url(value) or _valid_synthetic_application_url(value)
    return False


def _validate_synthetic_text(text: str) -> None:
    if _PHONE_LIKE_RE.search(text) or _PHONE_PLACEHOLDER_DIGITS_RE.search(text):
        raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_DATA_BLOCKED)
    for match in _EMAIL_RE.finditer(text):
        domain = match.group(0).rsplit("@", 1)[1].casefold()
        if not domain.endswith((".invalid", ".example")):
            raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_DATA_BLOCKED)
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        if not _valid_synthetic_application_url(candidate):
            raise RecruitmentJobStoreError(StoreErrorCode.SOURCE_NOT_ALLOWED)
    for match in _CONTACT_LABEL_RE.finditer(text):
        segment = text[match.end() : match.end() + 96].splitlines()[0]
        segment = re.split(r"[;|]", segment, maxsplit=1)[0]
        if _CONTACT_SEGMENT_DIGITS_RE.search(segment) and not re.search(
            r"\[(?:SYNTHETIC_(?:PHONE|CONTACT)|FAKE_CONTACT)_[0-9]{3}\]", segment
        ):
            digit_count = sum(character.isdigit() for character in segment)
            if digit_count >= 8:
                raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_DATA_BLOCKED)


def _valid_synthetic_post_url(value: object) -> TypeGuard[str]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "?" in value
        or "#" in value
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "threads.example.invalid"
        and parsed.hostname == "threads.example.invalid"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.path.startswith("/")
        and parsed.path != "/"
        and not any(part in {".", ".."} for part in parsed.path.split("/"))
        and not parsed.query
        and not parsed.fragment
    )


def _valid_synthetic_application_url(value: str) -> bool:
    if (
        not value
        or len(value) > 2_048
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold().endswith(".invalid")
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.fragment
    )


def _expected_post_revision_id(account_id: UUID, target: str, text: str) -> str:
    digest = hashlib.sha256(
        "\0".join((str(account_id), target, text)).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return f"bj02-rev-{digest[:32]}"


def _identity_sha256(account_id: UUID, canonical_post_url: str) -> str:
    encoded = json.dumps(
        [str(account_id), canonical_post_url],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_account_id(account_id: object) -> UUID:
    if type(account_id) is not UUID or account_id.version != 4:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    return account_id


def _require_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    try:
        if value.utcoffset() != timedelta(0):
            raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
        return value.astimezone(UTC)
    except RecruitmentJobStoreError:
        raise
    except OverflowError, TypeError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT) from None


def _format_utc(value: datetime) -> str:
    normalized = _require_utc(value)
    if normalized.microsecond:
        return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_local_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _LOCAL_ID_RE.fullmatch(value) is not None


def _json_object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    items = cast(dict[object, object], value)
    if any(type(key) is not str for key in items):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    return cast(dict[str, object], value)


def _object_list(parent: dict[str, object], key: str) -> list[dict[str, object]]:
    value = parent.get(key)
    if type(value) is not list:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    items = cast(list[object], value)
    if any(type(item) is not dict for item in items):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    return cast(list[dict[str, object]], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("INVALID_JSON_CONSTANT")


def _new_archive(account_id: UUID) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "account_uuid": str(account_id), "posts": []}


def _new_post(
    identity_hash: str,
    canonical_url: str,
    observed_at: datetime,
) -> dict[str, object]:
    timestamp = _format_utc(observed_at)
    return {
        "identity_sha256": identity_hash,
        "canonical_post_url": canonical_url,
        "created_at_utc": timestamp,
        "latest_observed_at_utc": timestamp,
        "deletion_state": "ACTIVE",
        "deleted_at_utc": None,
        "retention_state": "RAW_RETAINED",
        "source_rights_metadata": _SOURCE_RIGHTS,
        "observations": [],
        "revisions": [],
        "vacancy_states": [],
    }


def _new_revision(
    observation: BrowserObservationInput,
    body_digest: str | None,
    vacancy_revisions: tuple[dict[str, object], ...],
) -> dict[str, object]:
    if observation.visible_text is None or observation.post_revision_id is None:
        raise RecruitmentJobStoreError(StoreErrorCode.INVALID_INPUT)
    return {
        "post_revision_id": observation.post_revision_id,
        "content_sha256": body_digest,
        "vacancy_digest": _vacancy_digest(vacancy_revisions),
        "observed_text": observation.visible_text,
        "text_completeness": observation.text_completeness,
        "text_truncated": observation.text_truncated,
        "created_at_utc": _format_utc(observation.observed_at_utc),
        "retention_state": "RAW_RETAINED",
        "vacancy_revisions": list(vacancy_revisions),
    }


def _vacancy_digest(vacancy_revisions: Sequence[dict[str, object]]) -> str:
    encoded = json.dumps(
        list(vacancy_revisions),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _find_post(posts: Sequence[dict[str, object]], identity_hash: str) -> dict[str, object] | None:
    return next(
        (post for post in posts if post.get("identity_sha256") == identity_hash),
        None,
    )


def _find_observation(
    archive: dict[str, object],
    observation_id: str,
) -> dict[str, object] | None:
    for post in _object_list(archive, "posts"):
        for event in _object_list(post, "observations"):
            if event.get("observation_id") == observation_id:
                return event
    return None


def _same_observation_event(
    existing: dict[str, object],
    incoming: dict[str, object],
) -> bool:
    normalized = dict(existing)
    normalized["retention_state"] = "RAW_RETAINED"
    return normalized == incoming


def _find_revision(
    revisions: Sequence[dict[str, object]],
    post_revision_id: str,
) -> dict[str, object] | None:
    return next(
        (
            revision
            for revision in revisions
            if revision.get("post_revision_id") == post_revision_id
        ),
        None,
    )


def _update_vacancy_states(
    post: dict[str, object],
    event: dict[str, object],
    vacancy_revisions: tuple[dict[str, object], ...],
    observation: BrowserObservationInput,
) -> None:
    states = _object_list(post, "vacancy_states")
    by_id = {str(item["vacancy_id"]): item for item in states}
    present_ids = {str(item["vacancy_id"]) for item in vacancy_revisions}
    for revision in vacancy_revisions:
        vacancy_id = str(revision["vacancy_id"])
        existing = by_id.get(vacancy_id)
        state = {
            "vacancy_id": vacancy_id,
            "role_anchor": revision["role_anchor"],
            "latest_vacancy_revision_id": revision["vacancy_revision_id"],
            "state": "ACTIVE",
            "last_observation_id": observation.observation_id,
        }
        if existing is None:
            states.append(state)
            by_id[vacancy_id] = state
        else:
            if existing["role_anchor"] != revision["role_anchor"]:
                raise RecruitmentJobStoreError(StoreErrorCode.REVISION_CONFLICT)
            existing.update(state)

    if (
        observation.status == "COMPLETE"
        and observation.text_completeness == "FULL_VISIBLE"
        and not observation.text_truncated
        and observation.post_kind == "ORIGINAL"
    ):
        for vacancy_id, state in by_id.items():
            if vacancy_id not in present_ids and state["state"] == "ACTIVE":
                state["state"] = "NOT_REOBSERVED"
                state["last_observation_id"] = observation.observation_id
    states.sort(key=lambda item: str(item["vacancy_id"]))


def _receipt(status: str, archive: dict[str, object]) -> StoreReceipt:
    return StoreReceipt(
        status=status,
        post_count=len(_object_list(archive, "posts")),
        observation_count=_observation_count(archive),
        revision_count=_revision_count(archive),
    )


def _observation_count(archive: dict[str, object]) -> int:
    return sum(len(_object_list(post, "observations")) for post in _object_list(archive, "posts"))


def _revision_count(archive: dict[str, object]) -> int:
    return sum(len(_object_list(post, "revisions")) for post in _object_list(archive, "posts"))


def _vacancy_revision_count(archive: dict[str, object]) -> int:
    return sum(
        len(_object_list(revision, "vacancy_revisions"))
        for post in _object_list(archive, "posts")
        for revision in _object_list(post, "revisions")
    )


def _apply_retention(
    archive: dict[str, object],
    now_utc: datetime,
    policy: RetentionPolicy,
) -> bool:
    changed = False
    raw_cutoff = timedelta(days=policy.raw_text_days)
    derived_cutoff = timedelta(days=policy.derived_vacancy_days)
    for post in _object_list(archive, "posts"):
        if post["deletion_state"] == "DELETED":
            continue
        latest = _parse_utc(post["latest_observed_at_utc"])
        if now_utc - latest >= raw_cutoff and post["canonical_post_url"] is not None:
            post["canonical_post_url"] = None
            changed = True
        for revision in _object_list(post, "revisions"):
            created_at = _parse_utc(revision["created_at_utc"])
            age = now_utc - created_at
            if age >= raw_cutoff and revision["retention_state"] == "RAW_RETAINED":
                revision["observed_text"] = None
                revision["retention_state"] = "RAW_PURGED"
                for vacancy_revision in _object_list(revision, "vacancy_revisions"):
                    vacancy_revision["fields"] = _scrub_source_evidence(vacancy_revision["fields"])
                    vacancy_revision["retention_state"] = "RAW_PURGED"
                changed = True
            if age >= derived_cutoff and revision["retention_state"] != "DERIVED_PURGED":
                revision["observed_text"] = None
                revision["retention_state"] = "DERIVED_PURGED"
                for vacancy_revision in _object_list(revision, "vacancy_revisions"):
                    vacancy_revision["fields"] = None
                    vacancy_revision["retention_state"] = "DERIVED_PURGED"
                changed = True
        retention_by_revision = {
            str(revision["post_revision_id"]): str(revision["retention_state"])
            for revision in _object_list(post, "revisions")
        }
        for event in _object_list(post, "observations"):
            revision_id = event["post_revision_id"]
            if revision_id is None:
                age = now_utc - _parse_utc(event["observed_at_utc"])
                new_state = (
                    "DERIVED_PURGED"
                    if age >= derived_cutoff
                    else "RAW_PURGED"
                    if age >= raw_cutoff
                    else "RAW_RETAINED"
                )
            else:
                new_state = retention_by_revision.get(str(revision_id), "DERIVED_PURGED")
            if event["retention_state"] != new_state:
                event["retention_state"] = new_state
                changed = True
        for state in _object_list(post, "vacancy_states"):
            latest_revision_id = state["latest_vacancy_revision_id"]
            if latest_revision_id is None:
                continue
            revision_retention = next(
                (
                    str(vacancy_revision["retention_state"])
                    for revision in _object_list(post, "revisions")
                    for vacancy_revision in _object_list(revision, "vacancy_revisions")
                    if vacancy_revision["vacancy_revision_id"] == latest_revision_id
                ),
                None,
            )
            if revision_retention == "DERIVED_PURGED" and state["state"] != "EXPIRED":
                state["state"] = "EXPIRED"
                changed = True
        retention_states = {
            str(revision["retention_state"]) for revision in _object_list(post, "revisions")
        }
        post_state = (
            "DERIVED_PURGED"
            if retention_states and retention_states == {"DERIVED_PURGED"}
            else "RAW_PURGED"
            if retention_states and "RAW_RETAINED" not in retention_states
            else "RAW_RETAINED"
        )
        if post["retention_state"] != post_state:
            post["retention_state"] = post_state
            changed = True
    return changed


def _scrub_source_evidence(fields_value: object) -> object:
    if fields_value is None:
        return None
    fields = _json_object(fields_value)
    for name in (
        "employer",
        "role_title",
        "role_category",
        "headcount",
        "work_mode",
        "schedule",
        "experience",
        "requirements",
        "benefits",
    ):
        _scrub_evidence(fields[name], clear_value=False)
    location = _json_object(fields["location"])
    _scrub_evidence(location["raw"], clear_value=True)
    _scrub_evidence(location["address"], clear_value=False)
    _scrub_evidence(location["district"], clear_value=False)
    _scrub_evidence(location["province_city"], clear_value=False)
    salary = _json_object(fields["salary"])
    _scrub_evidence(salary["raw"], clear_value=True)
    for name in ("amount_min", "amount_max", "currency", "pay_period", "commission"):
        _scrub_evidence(salary[name], clear_value=False)
    contacts = _json_object(fields["contacts"])
    for contact in contacts.values():
        if type(contact) is dict:
            contact["evidence_count"] = 0
            if contact["state"] == "SOURCE_SPAN":
                contact["state"] = "REDACTED_BY_RETENTION"
            contact["present"] = False
    return fields


def _scrub_evidence(value: object, *, clear_value: bool) -> None:
    evidence = _json_object(value)
    if evidence.keys() != _EVIDENCE_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    evidence["raw_value"] = None
    evidence["spans"] = []
    if clear_value:
        evidence["value"] = None
    if evidence["state"] == "SOURCE_SPAN":
        evidence["state"] = "REDACTED_BY_RETENTION"


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT) from None
    return _require_utc(parsed)


def _validate_export_request(
    fields: Sequence[ExportField | str],
    confirmed: bool,
    include_public_recruiter_contact: bool,
) -> tuple[ExportField, ...]:
    if include_public_recruiter_contact is not False:
        raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_EXPORT_BLOCKED)
    if confirmed is not True:
        raise RecruitmentJobStoreError(StoreErrorCode.EXPORT_CONFIRMATION_REQUIRED)
    if not 1 <= len(fields) <= MAX_EXPORT_FIELDS:
        raise RecruitmentJobStoreError(StoreErrorCode.EXPORT_FIELD_INVALID)
    selected: list[ExportField] = []
    for value in fields:
        try:
            item = value if type(value) is ExportField else ExportField(value)
        except ValueError, TypeError:
            if value.casefold() in {
                "contact",
                "contacts",
                "phone",
                "zalo",
                "email",
                "application_url",
            }:
                raise RecruitmentJobStoreError(StoreErrorCode.CONTACT_EXPORT_BLOCKED) from None
            raise RecruitmentJobStoreError(StoreErrorCode.EXPORT_FIELD_INVALID) from None
        if item in selected:
            raise RecruitmentJobStoreError(StoreErrorCode.EXPORT_FIELD_INVALID)
        selected.append(item)
    return tuple(selected)


def _export_rows(
    archive: dict[str, object],
    fields: tuple[ExportField, ...],
) -> list[dict[str, object]]:
    posts = _object_list(archive, "posts")
    records: dict[str, dict[str, object]] = {}
    states_by_post: dict[str, dict[str, dict[str, object]]] = {}
    for post in posts:
        if post["deletion_state"] == "DELETED":
            continue
        identity_hash = str(post["identity_sha256"])
        state_map = {
            str(state["vacancy_id"]): state for state in _object_list(post, "vacancy_states")
        }
        states_by_post[identity_hash] = state_map
        revisions = _object_list(post, "revisions")
        vacancy_map = {
            str(vacancy_revision["vacancy_revision_id"]): vacancy_revision
            for revision in revisions
            for vacancy_revision in _object_list(revision, "vacancy_revisions")
        }
        for event in _object_list(post, "observations"):
            for vacancy_revision_id in cast(list[str], event["vacancy_revision_ids"]):
                vacancy_revision = vacancy_map.get(vacancy_revision_id)
                if vacancy_revision is None:
                    raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
                row = _flatten_export_row(
                    identity_hash,
                    event,
                    vacancy_revision,
                    state_map.get(str(vacancy_revision["vacancy_id"])),
                )
                row_key = f"{identity_hash}\0{event['observation_id']}\0{vacancy_revision_id}"
                records[row_key] = {item.value: row.get(item.value) for item in fields}
                if len(records) > MAX_EXPORT_ROWS:
                    raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
    return [records[key] for key in sorted(records)]


def _flatten_export_row(
    identity_hash: str,
    event: dict[str, object],
    vacancy_revision: dict[str, object],
    state: dict[str, object] | None,
) -> dict[str, object]:
    fields_value = vacancy_revision["fields"]
    fields_doc = {} if fields_value is None else _json_object(fields_value)
    location = _json_object(fields_doc.get("location", {}))
    salary = _json_object(fields_doc.get("salary", {}))
    return {
        ExportField.POST_IDENTITY_SHA256.value: identity_hash,
        ExportField.OBSERVATION_ID.value: event["observation_id"],
        ExportField.POST_REVISION_ID.value: event["post_revision_id"],
        ExportField.VACANCY_ID.value: vacancy_revision["vacancy_id"],
        ExportField.VACANCY_REVISION_ID.value: vacancy_revision["vacancy_revision_id"],
        ExportField.VACANCY_STATE.value: None if state is None else state["state"],
        ExportField.OBSERVED_AT_UTC.value: event["observed_at_utc"],
        ExportField.PUBLISHED_AT_UTC.value: event["published_at_utc"],
        ExportField.SOURCE_KIND.value: event["source"],
        ExportField.TEXT_COMPLETENESS.value: event["text_completeness"],
        ExportField.EMPLOYER.value: _evidence_value(fields_doc.get("employer")),
        ExportField.ROLE_TITLE.value: _evidence_value(fields_doc.get("role_title")),
        ExportField.ROLE_CATEGORY.value: _evidence_value(fields_doc.get("role_category")),
        ExportField.HEADCOUNT.value: _evidence_value(fields_doc.get("headcount")),
        ExportField.DISTRICT.value: _evidence_value(location.get("district")),
        ExportField.PROVINCE_CITY.value: _evidence_value(location.get("province_city")),
        ExportField.WORK_MODE.value: _evidence_value(fields_doc.get("work_mode")),
        ExportField.SCHEDULE.value: _evidence_value(fields_doc.get("schedule")),
        ExportField.SALARY_MIN.value: _evidence_value(salary.get("amount_min")),
        ExportField.SALARY_MAX.value: _evidence_value(salary.get("amount_max")),
        ExportField.SALARY_CURRENCY.value: _evidence_value(salary.get("currency")),
        ExportField.SALARY_PERIOD.value: _evidence_value(salary.get("pay_period")),
        ExportField.SALARY_COMMISSION.value: _evidence_value(salary.get("commission")),
        ExportField.EXPERIENCE.value: _evidence_value(fields_doc.get("experience")),
        ExportField.REQUIREMENTS.value: _evidence_value(fields_doc.get("requirements")),
        ExportField.BENEFITS.value: _evidence_value(fields_doc.get("benefits")),
    }


def _evidence_value(value: object) -> object:
    if value is None:
        return None
    evidence = _json_object(value)
    if evidence["state"] not in {"SOURCE_SPAN", "REDACTED_BY_RETENTION"}:
        return None
    return evidence.get("value")


def _csv_cell(value: object) -> str:
    if value is None:
        cell = ""
    elif type(value) is list:
        cell = "; ".join(str(item) for item in cast(list[object], value))
    else:
        cell = str(value)
    if _CSV_FORMULA_PREFIX_RE.match(cell):
        return "'" + cell
    return cell


def _validate_archive(value: object, account_id: UUID) -> dict[str, object]:
    archive = _json_object(value)
    if archive.keys() != _ARCHIVE_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    version = archive["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise RecruitmentJobStoreError(StoreErrorCode.VERSION_UNSUPPORTED)
    if archive["account_uuid"] != str(account_id):
        raise RecruitmentJobStoreError(StoreErrorCode.ACCOUNT_MISMATCH)
    posts = _object_list(archive, "posts")
    if len(posts) > MAX_POSTS_PER_ACCOUNT:
        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
    identity_hashes: set[str] = set()
    observation_ids: set[str] = set()
    total_vacancy_revisions = 0
    for post in posts:
        _validate_post_document(post, account_id, observation_ids)
        identity_hash = cast(str, post["identity_sha256"])
        if identity_hash in identity_hashes:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        identity_hashes.add(identity_hash)
        total_vacancy_revisions += sum(
            len(_object_list(revision, "vacancy_revisions"))
            for revision in _object_list(post, "revisions")
        )
    if len(observation_ids) > MAX_OBSERVATIONS_PER_ACCOUNT:
        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
    if total_vacancy_revisions > MAX_VACANCY_REVISIONS_PER_ACCOUNT:
        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
    return archive


def _validate_post_document(
    post: dict[str, object],
    account_id: UUID,
    observation_ids: set[str],
) -> None:
    if post.keys() != _POST_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    identity_hash = post["identity_sha256"]
    if not isinstance(identity_hash, str) or _SHA256_RE.fullmatch(identity_hash) is None:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    _parse_utc(post["created_at_utc"])
    latest_observed_at = _parse_utc(post["latest_observed_at_utc"])
    if post["source_rights_metadata"] != _SOURCE_RIGHTS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if post["deletion_state"] == "DELETED":
        if (
            post["canonical_post_url"] is not None
            or post["deleted_at_utc"] is None
            or post["observations"] != []
            or post["revisions"] != []
            or post["vacancy_states"] != []
            or post["retention_state"] != "DERIVED_PURGED"
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        _parse_utc(post["deleted_at_utc"])
        return
    if post["deletion_state"] != "ACTIVE" or post["deleted_at_utc"] is not None:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if post["retention_state"] not in _RETENTION_VALUES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    canonical_url = post["canonical_post_url"]
    if canonical_url is not None:
        if not _valid_synthetic_post_url(canonical_url):
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if _identity_sha256(account_id, canonical_url) != identity_hash:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)

    events = _object_list(post, "observations")
    revisions = _object_list(post, "revisions")
    states = _object_list(post, "vacancy_states")
    if (
        not 1 <= len(events) <= MAX_OBSERVATIONS_PER_POST
        or len(revisions) > MAX_REVISIONS_PER_POST
        or len(states) > MAX_VACANCY_REVISIONS_PER_ACCOUNT
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
    revision_by_id: dict[str, dict[str, object]] = {}
    vacancy_revision_ids: set[str] = set()
    for revision in revisions:
        _validate_post_revision(revision, account_id, canonical_url, vacancy_revision_ids)
        revision_id = cast(str, revision["post_revision_id"])
        if revision_id in revision_by_id:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        revision_by_id[revision_id] = revision
    last_event_time: datetime | None = None
    for event in events:
        _validate_observation_document(event, revision_by_id, vacancy_revision_ids)
        event_id = cast(str, event["observation_id"])
        if event_id in observation_ids:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        observation_ids.add(event_id)
        event_time = _parse_utc(event["observed_at_utc"])
        if last_event_time is not None and event_time < last_event_time:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        last_event_time = event_time
    if not events or latest_observed_at != _parse_utc(events[-1]["observed_at_utc"]):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    state_ids: set[str] = set()
    for state in states:
        _validate_vacancy_state(state, vacancy_revision_ids, observation_ids)
        vacancy_id = cast(str, state["vacancy_id"])
        if vacancy_id in state_ids:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        state_ids.add(vacancy_id)


def _validate_post_revision(
    revision: dict[str, object],
    account_id: UUID,
    canonical_url: object,
    vacancy_revision_ids: set[str],
) -> None:
    if revision.keys() != _REVISION_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    revision_id = revision["post_revision_id"]
    if not _valid_local_id(revision_id):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    content_digest = revision["content_sha256"]
    if content_digest is not None and (
        not isinstance(content_digest, str) or _SHA256_RE.fullmatch(content_digest) is None
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    vacancy_digest = revision["vacancy_digest"]
    if not isinstance(vacancy_digest, str) or _SHA256_RE.fullmatch(vacancy_digest) is None:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    text = revision["observed_text"]
    retention = revision["retention_state"]
    if retention not in _RETENTION_VALUES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if text is None:
        if retention == "RAW_RETAINED":
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    else:
        if type(text) is not str:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if canonical_url is None or not _valid_synthetic_post_url(canonical_url):
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if (
            len(text) > MAX_TEXT_CODEPOINTS
            or _sha256(text.encode("utf-8")) != content_digest
            or retention != "RAW_RETAINED"
            or revision_id != _expected_post_revision_id(account_id, canonical_url, text)
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if revision["text_completeness"] not in _COMPLETENESS_VALUES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if type(revision["text_truncated"]) is not bool:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if revision["text_truncated"] and revision["text_completeness"] != "PARTIAL":
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    _parse_utc(revision["created_at_utc"])
    vacancy_revisions = _object_list(revision, "vacancy_revisions")
    if len(vacancy_revisions) > MAX_VACANCIES_PER_REVISION:
        raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
    encoded_revisions: list[dict[str, object]] = []
    for item in vacancy_revisions:
        _validate_vacancy_revision(item, revision_id)
        vacancy_id = cast(str, item["vacancy_revision_id"])
        if vacancy_id in vacancy_revision_ids:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        vacancy_revision_ids.add(vacancy_id)
        encoded_revisions.append(item)
    if retention == "RAW_RETAINED" and _vacancy_digest(tuple(encoded_revisions)) != vacancy_digest:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _validate_vacancy_revision(
    revision: dict[str, object],
    post_revision_id: str,
) -> None:
    if revision.keys() != _VACANCY_REVISION_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    for key in ("vacancy_id", "vacancy_revision_id"):
        if not _valid_local_id(revision[key]):
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if revision["post_revision_id"] != post_revision_id:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    anchor = revision["role_anchor"]
    if not isinstance(anchor, str) or not 1 <= len(anchor) <= MAX_ROLE_ANCHOR_LENGTH:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if revision["classification"] != "RECRUITER_VACANCY":
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if revision["confidence"] not in _CONFIDENCE_VALUES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if not isinstance(revision["extraction_version"], str) or not revision["extraction_version"]:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    _validate_reason_codes(revision["reason_codes"], {item.value for item in ReasonCode})
    if revision["retention_state"] not in _RETENTION_VALUES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    fields_value = revision["fields"]
    if fields_value is not None:
        _validate_field_group(fields_value, revision["retention_state"])
    elif revision["retention_state"] != "DERIVED_PURGED":
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _validate_observation_document(
    event: dict[str, object],
    revisions: dict[str, dict[str, object]],
    vacancy_revision_ids: set[str],
) -> None:
    if event.keys() != _OBSERVATION_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    observation_id = event["observation_id"]
    if not _valid_local_id(observation_id):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    revision_id = event["post_revision_id"]
    if revision_id is not None and (
        not _valid_local_id(revision_id) or revision_id not in revisions
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    _parse_utc(event["observed_at_utc"])
    if event["published_at_utc"] is not None:
        _parse_utc(event["published_at_utc"])
    if (
        event["source"] != "SYNTHETIC_FIXTURE"
        or event["text_provenance"] not in {"SYNTHETIC_FIXTURE_TEXT", "NONE"}
        or event["completeness_basis"] not in {"FIXTURE_DECLARED", "UNKNOWN"}
        or event["source_rights_metadata"] != _SOURCE_RIGHTS
        or event["author_evidence"] not in {"UNVERIFIED", "UNKNOWN"}
        or event["related_author_evidence"] not in {"UNVERIFIED", "UNKNOWN"}
        or event["text_completeness"] not in _COMPLETENESS_VALUES
        or event["status"] not in _POST_STATUS_VALUES
        or event["post_kind"] not in _POST_KIND_VALUES
        or event["classification"] not in _CLASSIFICATION_VALUES
        or event["extraction_status"] not in _EXTRACTION_STATUS_VALUES
        or event["confidence"] not in _CONFIDENCE_VALUES
        or event["retention_state"] not in _RETENTION_VALUES
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if type(event["text_truncated"]) is not bool or type(event["author_label_present"]) is not bool:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if event["text_truncated"] and event["text_completeness"] != "PARTIAL":
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if (revision_id is None) != (event["text_provenance"] == "NONE"):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    _validate_reason_codes(event["reason_codes"], {item.value for item in ReasonCode})
    related_hash = event["related_post_identity_sha256"]
    if related_hash is not None and (
        not isinstance(related_hash, str) or _SHA256_RE.fullmatch(related_hash) is None
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    ids = event["vacancy_revision_ids"]
    if type(ids) is not list:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    revision_ids = cast(list[object], ids)
    if any(not isinstance(item, str) or item not in vacancy_revision_ids for item in revision_ids):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _validate_vacancy_state(
    state: dict[str, object],
    vacancy_revision_ids: set[str],
    observation_ids: set[str],
) -> None:
    if state.keys() != _VACANCY_STATE_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    vacancy_id = state["vacancy_id"]
    if not _valid_local_id(vacancy_id):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    anchor = state["role_anchor"]
    if not isinstance(anchor, str) or not 1 <= len(anchor) <= MAX_ROLE_ANCHOR_LENGTH:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    latest_revision_id = state["latest_vacancy_revision_id"]
    if latest_revision_id is not None and latest_revision_id not in vacancy_revision_ids:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if state["state"] not in _VACANCY_STATE_VALUES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    observation_id = state["last_observation_id"]
    if observation_id not in observation_ids:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _validate_reason_codes(value: object, allowed: set[str]) -> None:
    if type(value) is not list:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    codes = cast(list[object], value)
    if len(codes) > 64 or any(not isinstance(code, str) or code not in allowed for code in codes):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _validate_field_group(value: object, retention_state: object) -> None:
    fields_doc = _json_object(value)
    if fields_doc.keys() != _FIELD_GROUP_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    for name in (
        "employer",
        "role_title",
        "role_category",
        "headcount",
        "work_mode",
        "schedule",
        "experience",
        "requirements",
        "benefits",
    ):
        _validate_evidence(fields_doc[name], retention_state)
    location = _json_object(fields_doc["location"])
    if location.keys() != {"raw", "address", "district", "province_city"}:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    for evidence in location.values():
        _validate_evidence(evidence, retention_state)
    salary = _json_object(fields_doc["salary"])
    if salary.keys() != {
        "raw",
        "amount_min",
        "amount_max",
        "currency",
        "pay_period",
        "commission",
    }:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    for evidence in salary.values():
        _validate_evidence(evidence, retention_state)
    contacts = _json_object(fields_doc["contacts"])
    if contacts.keys() != {"phone", "zalo", "email", "application_url"}:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    for contact in contacts.values():
        evidence = _json_object(contact)
        if evidence.keys() != _CONTACT_EVIDENCE_KEYS:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if evidence["state"] not in _GENERAL_EVIDENCE_STATES:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if type(evidence["present"]) is not bool:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if type(evidence["evidence_count"]) is not int or not 0 <= evidence["evidence_count"] <= 20:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if evidence["state"] == "REDACTED_BY_RETENTION" and (
            evidence["present"] or evidence["evidence_count"] != 0
        ):
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if retention_state in {"RAW_PURGED", "DERIVED_PURGED"}:
        for name in (
            "employer",
            "role_title",
            "role_category",
            "headcount",
            "work_mode",
            "schedule",
            "experience",
            "requirements",
            "benefits",
        ):
            _validate_scrubbed_evidence(fields_doc[name])
        for evidence in location.values():
            _validate_scrubbed_evidence(evidence)
        for evidence in salary.values():
            _validate_scrubbed_evidence(evidence)


def _validate_evidence(value: object, retention_state: object) -> None:
    evidence = _json_object(value)
    if evidence.keys() != _EVIDENCE_KEYS:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if evidence["state"] not in _GENERAL_EVIDENCE_STATES:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    _validate_json_value(evidence["value"])
    raw_value = evidence["raw_value"]
    if raw_value is not None and (
        not isinstance(raw_value, str) or len(raw_value) > MAX_FIELD_CODEPOINTS
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    spans = evidence["spans"]
    if type(spans) is not list:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    span_items = cast(list[object], spans)
    if len(span_items) > 100:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    for span in span_items:
        if type(span) is not list:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        offsets = cast(list[object], span)
        if len(offsets) != 2:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        start, end = offsets
        if type(start) is not int or type(end) is not int:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
        if start < 0 or end <= start or end > MAX_TEXT_CODEPOINTS:
            raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if evidence["state"] == "SOURCE_SPAN" and (evidence["value"] is None or not spans):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if evidence["state"] == "ABSENT" and (
        evidence["value"] is not None or raw_value is not None or spans
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if evidence["state"] == "AMBIGUOUS" and (
        evidence["value"] is not None or raw_value is not None
    ):
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if retention_state in {"RAW_PURGED", "DERIVED_PURGED"}:
        _validate_scrubbed_evidence(evidence)


def _validate_scrubbed_evidence(value: object) -> None:
    evidence = _json_object(value)
    if evidence["raw_value"] is not None or evidence["spans"] != []:
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
    if evidence["state"] == "SOURCE_SPAN":
        raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in (int, bool):
        return
    if isinstance(value, str):
        if len(value) > MAX_FIELD_CODEPOINTS:
            raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
        return
    if type(value) is list:
        items = cast(list[object], value)
        if len(items) > MAX_VACANCIES_PER_REVISION:
            raise RecruitmentJobStoreError(StoreErrorCode.LIMIT_REACHED)
        for item in items:
            if item is not None and type(item) is not str and type(item) is not int:
                raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)
            _validate_json_value(item)
        return
    raise RecruitmentJobStoreError(StoreErrorCode.ARCHIVE_CORRUPT)


def _is_reparse_point(info: os.stat_result) -> bool:
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & attribute)


def _validate_path_components(path: Path) -> None:
    if not path.is_absolute():
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
            if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
            resolved = current.resolve(strict=True)
            if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(current)):
                raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
        except RecruitmentJobStoreError:
            raise
        except OSError, RuntimeError:
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None


def _checkout_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    candidates = (Path(__file__).resolve(), Path.cwd().resolve())
    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / "pyproject.toml").is_file() and (parent / ".git").exists():
                if parent not in roots:
                    roots.append(parent)
                break
    return tuple(roots)


def _ensure_outside_checkout(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        for checkout in _checkout_roots():
            try:
                resolved.relative_to(checkout.resolve(strict=True))
            except ValueError:
                continue
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
    except RecruitmentJobStoreError:
        raise
    except OSError, RuntimeError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None


def _ensure_directory(parent: Path, name: str) -> tuple[Path, bool]:
    if not name or name in {".", ".."} or any(separator in name for separator in ("/", "\\")):
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
    path = parent / name
    _require_parented_child(path, parent)
    created = False
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            created = True
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
        resolved = path.resolve(strict=True)
        if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
        if created and os.name != "nt":
            os.chmod(path, 0o700)
        return path, created
    except RecruitmentJobStoreError:
        raise
    except OSError, RuntimeError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None


def _require_parented_child(path: Path, parent: Path) -> None:
    if path.parent != parent or path.name in {"", ".", ".."}:
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)


def _verify_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
        resolved = path.resolve(strict=True)
        if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
        if os.name == "nt":
            _verify_windows_private_acl(path)
        elif stat.S_IMODE(info.st_mode) & 0o077:
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    except RecruitmentJobStoreError:
        raise
    except OSError, RuntimeError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED) from None


def _secure_directory(path: Path, *, configure_windows_acl: bool = False) -> None:
    try:
        if os.name == "nt":
            if configure_windows_acl:
                _set_windows_private_acl(path)
            _verify_windows_private_acl(path)
        else:
            os.chmod(path, 0o700)
            info = path.lstat()
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    except RecruitmentJobStoreError:
        raise
    except OSError:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED) from None


def _verify_regular_file(
    path: Path,
    parent: Path,
    *,
    allow_missing: bool,
    max_bytes: int,
) -> bool:
    _require_parented_child(path, parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None
    except OSError:
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None
    if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info) or info.st_size > max_bytes:
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
    try:
        resolved = path.resolve(strict=True)
        if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
            raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID)
    except RecruitmentJobStoreError:
        raise
    except OSError, RuntimeError:
        raise RecruitmentJobStoreError(StoreErrorCode.ROOT_INVALID) from None
    if os.name == "nt":
        _verify_windows_private_acl(path)
    elif stat.S_IMODE(info.st_mode) & 0o077:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    return True


def _secure_file(path: Path) -> None:
    try:
        if os.name == "nt":
            _verify_windows_private_acl(path)
        else:
            os.chmod(path, 0o600)
            info = path.lstat()
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    except RecruitmentJobStoreError:
        raise
    except OSError:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED) from None


def _ensure_private_lock_file(path: Path, parent: Path) -> None:
    exists = _verify_regular_file(path, parent, allow_missing=True, max_bytes=1)
    if exists:
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, b"0")
        os.fsync(descriptor)
    except FileExistsError:
        _verify_regular_file(path, parent, allow_missing=False, max_bytes=1)
        return
    except OSError:
        raise RecruitmentJobStoreError(StoreErrorCode.PERSISTENCE_FAILED) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    _secure_file(path)
    _verify_regular_file(path, parent, allow_missing=False, max_bytes=1)


def _windows_icacls() -> str:
    executable = shutil.which("icacls.exe") or shutil.which("icacls")
    if executable is None:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    return executable


def _set_windows_private_acl(path: Path) -> None:
    try:
        user = getpass.getuser()
        result = subprocess.run(
            (
                _windows_icacls(),
                os.fspath(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(OI)(CI)(F)",
                "*S-1-5-18:(OI)(CI)(F)",
                "*S-1-5-32-544:(OI)(CI)(F)",
            ),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    except RecruitmentJobStoreError:
        raise
    except OSError, subprocess.SubprocessError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED) from None


def _verify_windows_private_acl(path: Path) -> None:
    try:
        result = subprocess.run(
            (_windows_icacls(), os.fspath(path)),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
        lines = result.stdout.splitlines()[1:]
        if not lines:
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
        current_user = getpass.getuser().casefold()
        found_allowed_ace = False
        for line in lines:
            line = line.strip()
            if not line or line.casefold().startswith("successfully processed"):
                continue
            principal, separator, _ = line.partition(":")
            if not separator:
                raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
            normalized = principal.strip().casefold()
            if (
                normalized
                in {
                    "system",
                    "nt authority\\system",
                    "administrators",
                    "builtin\\administrators",
                    "owner rights",
                }
                or normalized == current_user
                or normalized.endswith("\\" + current_user)
            ):
                found_allowed_ace = True
                continue
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
        if not found_allowed_ace:
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
    except RecruitmentJobStoreError:
        raise
    except OSError, subprocess.SubprocessError, ValueError:
        raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED) from None
