"""Private, durable REVIEW_ONLY staging for API-AUTO operator packets.

This module stores one immutable, account-scoped JSON record per review stage.
It does not issue approval, prove human consent, reacquire remote targets, or
grant dispatch authority.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone.api_auto_content import (
    ContentOrigin,
    JobFactsDeclaration,
    PacketReviewState,
    ReviewContentPacket,
    RightsBasis,
    RightsEvidence,
    content_sha256_for,
)
from threads_platform.standalone.api_auto_plan import (
    ActionKind,
    ApiPermission,
    CampaignScope,
    CandidateSource,
    ObservationProvenance,
    ObservedCandidate,
)
from threads_platform.standalone.api_auto_review_context import (
    PrivateReviewDecision,
    PrivateReviewPreview,
    PrivateReviewPreviewResult,
    ReviewDisplaySnapshot,
    build_private_review_preview,
)

_NAMESPACE = "api_auto_review_stage_v1"
_SCHEMA = "api_auto_review_stage_v1"
_SCHEMA_VERSION = 1
_MAX_STAGES_PER_ACCOUNT = 64
_MAX_RECORD_BYTES = 32 * 1024
_MAX_CONTENT_CODEPOINTS = 500
_MAX_ATTRIBUTION_CODEPOINTS = 256
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STAGE_TEMP = re.compile(r"\.stage-([0-9a-f-]{36})\.tmp\Z")
_STAGE_FILE = re.compile(r"([0-9a-f-]{36})\.json\Z")
_CONTENT_REFERENCE = "private_content.normalized_text"
_RECORD_HASH_DOMAIN = b"api-auto-review-stage-record-v1\0"
_CAPABILITY_HASH_DOMAIN = b"api-auto-review-stage-capabilities-v1\0"
_QUOTA_HASH_DOMAIN = b"api-auto-review-stage-quota-v1\0"


class ReviewStageState(StrEnum):
    """The only durable state this slice can represent."""

    REVIEW_ONLY = "REVIEW_ONLY"


class ReviewStageDecision(StrEnum):
    STAGED = "STAGED"
    LOADED = "LOADED"
    STOP = "STOP"


class ReviewStageReason(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    PREVIEW_NOT_READY = "PREVIEW_NOT_READY"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    EXPIRED = "EXPIRED"
    UNSAFE_DATA_ROOT = "UNSAFE_DATA_ROOT"
    UNSAFE_PATH = "UNSAFE_PATH"
    ACL_UNSAFE = "ACL_UNSAFE"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    LOCK_BUSY = "LOCK_BUSY"
    STAGE_NOT_FOUND = "STAGE_NOT_FOUND"
    INCOMPLETE_TEMPORARY_RECORD = "INCOMPLETE_TEMPORARY_RECORD"
    DUPLICATE_STAGE = "DUPLICATE_STAGE"
    QUEUE_FULL = "QUEUE_FULL"
    CORRUPT_RECORD = "CORRUPT_RECORD"
    PUBLICATION_AMBIGUOUS = "PUBLICATION_AMBIGUOUS"
    IO_FAILURE = "IO_FAILURE"


@dataclass(frozen=True, slots=True, repr=False)
class StagedReviewV1:
    """Redacted immutable metadata for a single REVIEW_ONLY record."""

    schema_version: int
    stage_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    action: ActionKind
    source: CandidateSource
    provenance: ObservationProvenance
    actor_sha256: str = field(repr=False)
    candidate_fingerprint: str = field(repr=False)
    packet_revision_sha256: str = field(repr=False)
    preview_sha256: str = field(repr=False)
    display_context_sha256: str = field(repr=False)
    target_sha256: str = field(repr=False)
    context_sha256: str = field(repr=False)
    root_sha256: str | None = field(repr=False)
    parent_sha256: str | None = field(repr=False)
    reply_sha256: str | None = field(repr=False)
    context_evidence_sha256: str = field(repr=False)
    policy_sha256: str = field(repr=False)
    content_sha256: str = field(repr=False)
    content_origin: ContentOrigin
    normalization_version: str
    rights_basis: RightsBasis
    right_to_use_declared: bool
    source_attribution_sha256: str = field(repr=False)
    job_facts_sha256: str = field(repr=False)
    job_facts_declaration: JobFactsDeclaration
    prepared_at: datetime
    effective_at: datetime
    packet_expires_at: datetime
    content_expires_at: datetime
    expires_at: datetime
    staged_at: datetime
    required_permissions: tuple[ApiPermission, ...]
    capability_snapshot_sha256: str = field(repr=False)
    quota_snapshot_sha256: str = field(repr=False)
    capabilities_observed_at: datetime
    quota_observed_at: datetime
    candidate_observed_at: datetime
    binding_observed_at: datetime
    quota_used: int
    quota_total: int
    state: ReviewStageState = ReviewStageState.REVIEW_ONLY
    content_reference: str = field(default=_CONTENT_REFERENCE, repr=False)

    @property
    def dispatchable(self) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            "StagedReviewV1("
            f"schema_version={self.schema_version}, action={self.action.value}, "
            f"source={self.source.value}, state={self.state.value}, "
            f"expires_at={self.expires_at.isoformat()}, private_binding=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class StagedReviewContent:
    """Private content returned only by the explicit local store method."""

    stage_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    content_sha256: str = field(repr=False)
    normalized_text: str = field(repr=False)
    source_attribution: str = field(repr=False)
    state: ReviewStageState = ReviewStageState.REVIEW_ONLY

    @property
    def dispatchable(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "StagedReviewContent(state=REVIEW_ONLY, private_content=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ReviewStageResult:
    decision: ReviewStageDecision
    reason_codes: tuple[ReviewStageReason, ...]
    stage_id: UUID | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"ReviewStageResult(decision={self.decision.value}, "
            f"reason_codes={tuple(item.value for item in self.reason_codes)!r}, "
            "stage_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ReviewStageReadResult:
    decision: ReviewStageDecision
    reason_codes: tuple[ReviewStageReason, ...]
    stage: StagedReviewV1 | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"ReviewStageReadResult(decision={self.decision.value}, "
            f"reason_codes={tuple(item.value for item in self.reason_codes)!r}, "
            f"stage_present={self.stage is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ReviewStageContentResult:
    decision: ReviewStageDecision
    reason_codes: tuple[ReviewStageReason, ...]
    content: StagedReviewContent | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"ReviewStageContentResult(decision={self.decision.value}, "
            f"reason_codes={tuple(item.value for item in self.reason_codes)!r}, "
            f"content_present={self.content is not None})"
        )


class _StoreFault(Exception):
    def __init__(self, reason: ReviewStageReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class PrivateReviewStageStore:
    """Account-scoped durable staging with no approval or dispatch operation."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    def __repr__(self) -> str:
        return "PrivateReviewStageStore(data_root=<redacted>)"

    def stage_review(
        self,
        packet: ReviewContentPacket,
        preview: PrivateReviewPreview,
        scope: CampaignScope,
        candidates: tuple[ObservedCandidate, ...],
        display: ReviewDisplaySnapshot,
        *,
        now: datetime,
    ) -> ReviewStageResult:
        if (
            type(packet) is not ReviewContentPacket
            or type(preview) is not PrivateReviewPreview
            or type(scope) is not CampaignScope
            or type(candidates) is not tuple
            or any(type(item) is not ObservedCandidate for item in candidates)
            or type(display) is not ReviewDisplaySnapshot
            or not _is_utc(now)
        ):
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.INVALID_INPUT)
        if packet.review_state is not PacketReviewState.NEEDS_OPERATOR_REVIEW:
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.PREVIEW_NOT_READY)
        if packet.binding.account_id != scope.account_id or preview.account_id != scope.account_id:
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.ACCOUNT_MISMATCH)

        try:
            rebuilt = build_private_review_preview(packet, scope, candidates, display, now=now)
        except Exception:  # noqa: BLE001 - all malformed supplied evidence fails closed
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.PREVIEW_NOT_READY)
        if (
            type(rebuilt) is not PrivateReviewPreviewResult
            or rebuilt.decision is not PrivateReviewDecision.READY_FOR_PRIVATE_TTY
            or rebuilt.reason_codes != ()
            or rebuilt.preview is None
            or rebuilt.preview != preview
        ):
            if preview.expires_at <= now:
                return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.EXPIRED)
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.PREVIEW_NOT_READY)
        if preview.expires_at <= now:
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.EXPIRED)

        try:
            account_id = scope.account_id
            namespace, account_dir = self._prepare_account(account_id)
            del namespace
            lock = _acquire_account_lock(account_dir)
            try:
                _recover_temporary_records(account_dir)
                records = _scan_records(account_dir, account_id)
                if any(
                    existing.packet_revision_sha256 == packet.packet_revision_sha256
                    or existing.preview_sha256 == preview.preview_sha256
                    for existing, _ in records
                ):
                    return _stage_result(
                        ReviewStageDecision.STOP, ReviewStageReason.DUPLICATE_STAGE
                    )
                if len(records) >= _MAX_STAGES_PER_ACCOUNT:
                    return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.QUEUE_FULL)

                stage_id = uuid4()
                staged = _make_staged_review(packet, preview, scope, stage_id, now)
                content = packet.prepared_content
                rights = content.rights
                if type(rights) is not RightsEvidence or type(rights.source_attribution) is not str:
                    return _stage_result(
                        ReviewStageDecision.STOP, ReviewStageReason.PREVIEW_NOT_READY
                    )
                record = _encode_record(staged, content.text, rights.source_attribution)
                if len(record) > _MAX_RECORD_BYTES:
                    return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.INVALID_INPUT)
                _atomic_create_record(account_dir, staged, record)
                return ReviewStageResult(ReviewStageDecision.STAGED, (), stage_id)
            finally:
                lock.release()
        except _StoreFault as error:
            return _stage_result(ReviewStageDecision.STOP, error.reason)
        except ProcessAlreadyRunning:
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.LOCK_BUSY)
        except Exception:  # noqa: BLE001 - never return filesystem detail or private paths
            return _stage_result(ReviewStageDecision.STOP, ReviewStageReason.IO_FAILURE)

    def get_stage(
        self,
        account_id: UUID,
        stage_id: UUID,
        *,
        now: datetime,
    ) -> ReviewStageReadResult:
        if type(account_id) is not UUID or type(stage_id) is not UUID or not _is_utc(now):
            return _read_result(ReviewStageDecision.STOP, ReviewStageReason.INVALID_INPUT)
        try:
            account_dir = self._existing_account(account_id)
            if account_dir is None:
                return _read_result(ReviewStageDecision.STOP, ReviewStageReason.STAGE_NOT_FOUND)
            lock = _acquire_existing_account_lock(account_dir)
            try:
                _recover_temporary_records(account_dir)
                records = _scan_records(account_dir, account_id)
                stage = next((item for item, _ in records if item.stage_id == stage_id), None)
                if stage is None:
                    return _read_result(ReviewStageDecision.STOP, ReviewStageReason.STAGE_NOT_FOUND)
                if stage.expires_at <= now:
                    return _read_result(ReviewStageDecision.STOP, ReviewStageReason.EXPIRED)
                return ReviewStageReadResult(ReviewStageDecision.LOADED, (), stage)
            finally:
                lock.release()
        except _StoreFault as error:
            return _read_result(ReviewStageDecision.STOP, error.reason)
        except ProcessAlreadyRunning:
            return _read_result(ReviewStageDecision.STOP, ReviewStageReason.LOCK_BUSY)
        except Exception:  # noqa: BLE001 - keep path and data details private
            return _read_result(ReviewStageDecision.STOP, ReviewStageReason.IO_FAILURE)

    def load_private_text(
        self,
        account_id: UUID,
        stage_id: UUID,
        *,
        now: datetime,
    ) -> ReviewStageContentResult:
        if type(account_id) is not UUID or type(stage_id) is not UUID or not _is_utc(now):
            return _content_result(ReviewStageDecision.STOP, ReviewStageReason.INVALID_INPUT)
        try:
            account_dir = self._existing_account(account_id)
            if account_dir is None:
                return _content_result(ReviewStageDecision.STOP, ReviewStageReason.STAGE_NOT_FOUND)
            lock = _acquire_existing_account_lock(account_dir)
            try:
                _recover_temporary_records(account_dir)
                records = _scan_records(account_dir, account_id)
                pair = next(
                    ((item, body) for item, body in records if item.stage_id == stage_id), None
                )
                if pair is None:
                    return _content_result(
                        ReviewStageDecision.STOP, ReviewStageReason.STAGE_NOT_FOUND
                    )
                staged, body = pair
                if staged.expires_at <= now:
                    return _content_result(ReviewStageDecision.STOP, ReviewStageReason.EXPIRED)
                private_content = body["private_content"]
                assert type(private_content) is dict
                private_record = cast(dict[str, object], private_content)
                text = cast(str, private_record["normalized_text"])
                attribution = cast(str, private_record["source_attribution"])
                loaded = StagedReviewContent(
                    stage_id=staged.stage_id,
                    account_id=staged.account_id,
                    content_sha256=staged.content_sha256,
                    normalized_text=text,
                    source_attribution=attribution,
                )
                return ReviewStageContentResult(ReviewStageDecision.LOADED, (), loaded)
            finally:
                lock.release()
        except _StoreFault as error:
            return _content_result(ReviewStageDecision.STOP, error.reason)
        except ProcessAlreadyRunning:
            return _content_result(ReviewStageDecision.STOP, ReviewStageReason.LOCK_BUSY)
        except Exception:  # noqa: BLE001 - keep path and data details private
            return _content_result(ReviewStageDecision.STOP, ReviewStageReason.IO_FAILURE)

    def _prepare_account(self, account_id: UUID) -> tuple[Path, Path]:
        root = self._validated_root()
        namespace = root / _NAMESPACE
        _ensure_private_directory(namespace)
        account_dir = namespace / str(account_id)
        _ensure_private_directory(account_dir)
        _ensure_private_lock_file(account_dir / ".lock")
        return namespace, account_dir

    def _existing_account(self, account_id: UUID) -> Path | None:
        root = self._validated_root()
        namespace = root / _NAMESPACE
        if not _path_lexists(namespace):
            return None
        _verify_private_directory(namespace)
        account_dir = namespace / str(account_id)
        if not _path_lexists(account_dir):
            return None
        _verify_private_directory(account_dir)
        _verify_private_regular_file(account_dir / ".lock")
        return account_dir

    def _validated_root(self) -> Path:
        if not self._data_root.is_absolute():
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT)
        if any(part in {".", ".."} for part in self._data_root.parts):
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT)
        try:
            absolute = Path(os.path.abspath(self._data_root))
            resolved = absolute.resolve(strict=True)
        except OSError, RuntimeError, ValueError:
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT) from None
        if _path_key(absolute) != _path_key(resolved) or not resolved.is_dir():
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT)
        if _is_inside_checkout(resolved):
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT)
        _verify_path_components(resolved)
        _verify_private_data_root(resolved)
        return resolved


def _make_staged_review(
    packet: ReviewContentPacket,
    preview: PrivateReviewPreview,
    scope: CampaignScope,
    stage_id: UUID,
    now: datetime,
) -> StagedReviewV1:
    binding = packet.binding
    prepared = packet.prepared_content
    rights = prepared.rights
    if type(rights) is not RightsEvidence or type(rights.rights_basis) is not RightsBasis:
        raise _StoreFault(ReviewStageReason.PREVIEW_NOT_READY)
    source_attribution = rights.source_attribution
    if type(source_attribution) is not str:
        raise _StoreFault(ReviewStageReason.PREVIEW_NOT_READY)
    required = binding.required_permissions
    if (
        type(required) is not tuple
        or not required
        or any(type(item) is not ApiPermission for item in required)
        or len(set(required)) != len(required)
    ):
        raise _StoreFault(ReviewStageReason.PREVIEW_NOT_READY)
    quota_used = binding.quota_used
    quota_total = binding.quota_total
    if (
        type(quota_used) is not int
        or type(quota_total) is not int
        or quota_used < 0
        or quota_total <= 0
        or quota_used >= quota_total
    ):
        raise _StoreFault(ReviewStageReason.PREVIEW_NOT_READY)
    return StagedReviewV1(
        schema_version=_SCHEMA_VERSION,
        stage_id=stage_id,
        account_id=binding.account_id,
        action=binding.action,
        source=binding.source,
        provenance=binding.provenance,
        actor_sha256=binding.actor_sha256,
        candidate_fingerprint=binding.candidate_fingerprint,
        packet_revision_sha256=packet.packet_revision_sha256,
        preview_sha256=preview.preview_sha256,
        display_context_sha256=preview.display_context_sha256,
        target_sha256=binding.target_sha256,
        context_sha256=binding.context_sha256,
        root_sha256=binding.root_sha256,
        parent_sha256=binding.parent_sha256,
        reply_sha256=binding.reply_sha256,
        context_evidence_sha256=binding.context_evidence_sha256,
        policy_sha256=binding.policy_sha256,
        content_sha256=packet.content_sha256,
        content_origin=prepared.origin,
        normalization_version=packet.normalization_version,
        rights_basis=rights.rights_basis,
        right_to_use_declared=rights.right_to_use_declared,
        source_attribution_sha256=_sha_text(source_attribution),
        job_facts_sha256=rights.job_facts_sha256 or "",
        job_facts_declaration=rights.job_facts_declaration,
        prepared_at=prepared.prepared_at,
        effective_at=prepared.effective_at,
        packet_expires_at=binding.expires_at,
        content_expires_at=prepared.expires_at,
        expires_at=preview.expires_at,
        staged_at=now,
        required_permissions=required,
        capability_snapshot_sha256=_capability_digest(scope),
        quota_snapshot_sha256=_quota_digest(scope),
        capabilities_observed_at=binding.capabilities_observed_at,
        quota_observed_at=binding.quota_observed_at,
        candidate_observed_at=binding.candidate_observed_at,
        binding_observed_at=binding.binding_observed_at,
        quota_used=quota_used,
        quota_total=quota_total,
    )


_RECORD_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "stage_id",
        "account_id",
        "action",
        "source",
        "provenance",
        "actor_sha256",
        "candidate_fingerprint",
        "packet_revision_sha256",
        "preview_sha256",
        "display_context_sha256",
        "target_sha256",
        "context_sha256",
        "root_sha256",
        "parent_sha256",
        "reply_sha256",
        "context_evidence_sha256",
        "policy_sha256",
        "content_sha256",
        "content_origin",
        "normalization_version",
        "rights_basis",
        "right_to_use_declared",
        "source_attribution_sha256",
        "job_facts_sha256",
        "job_facts_declaration",
        "prepared_at",
        "effective_at",
        "packet_expires_at",
        "content_expires_at",
        "expires_at",
        "staged_at",
        "required_permissions",
        "capability_snapshot_sha256",
        "quota_snapshot_sha256",
        "capabilities_observed_at",
        "quota_observed_at",
        "candidate_observed_at",
        "binding_observed_at",
        "quota_used",
        "quota_total",
        "state",
        "content_reference",
        "private_content",
        "record_sha256",
    }
)


def _encode_record(staged: StagedReviewV1, text: str, attribution: str) -> bytes:
    metadata: dict[str, object] = {
        "schema": _SCHEMA,
        "schema_version": staged.schema_version,
        "stage_id": str(staged.stage_id),
        "account_id": str(staged.account_id),
        "action": staged.action.value,
        "source": staged.source.value,
        "provenance": staged.provenance.value,
        "actor_sha256": staged.actor_sha256,
        "candidate_fingerprint": staged.candidate_fingerprint,
        "packet_revision_sha256": staged.packet_revision_sha256,
        "preview_sha256": staged.preview_sha256,
        "display_context_sha256": staged.display_context_sha256,
        "target_sha256": staged.target_sha256,
        "context_sha256": staged.context_sha256,
        "root_sha256": staged.root_sha256,
        "parent_sha256": staged.parent_sha256,
        "reply_sha256": staged.reply_sha256,
        "context_evidence_sha256": staged.context_evidence_sha256,
        "policy_sha256": staged.policy_sha256,
        "content_sha256": staged.content_sha256,
        "content_origin": staged.content_origin.value,
        "normalization_version": staged.normalization_version,
        "rights_basis": staged.rights_basis.value,
        "right_to_use_declared": staged.right_to_use_declared,
        "source_attribution_sha256": staged.source_attribution_sha256,
        "job_facts_sha256": staged.job_facts_sha256,
        "job_facts_declaration": staged.job_facts_declaration.value,
        "prepared_at": _format_utc(staged.prepared_at),
        "effective_at": _format_utc(staged.effective_at),
        "packet_expires_at": _format_utc(staged.packet_expires_at),
        "content_expires_at": _format_utc(staged.content_expires_at),
        "expires_at": _format_utc(staged.expires_at),
        "staged_at": _format_utc(staged.staged_at),
        "required_permissions": [item.value for item in staged.required_permissions],
        "capability_snapshot_sha256": staged.capability_snapshot_sha256,
        "quota_snapshot_sha256": staged.quota_snapshot_sha256,
        "capabilities_observed_at": _format_utc(staged.capabilities_observed_at),
        "quota_observed_at": _format_utc(staged.quota_observed_at),
        "candidate_observed_at": _format_utc(staged.candidate_observed_at),
        "binding_observed_at": _format_utc(staged.binding_observed_at),
        "quota_used": staged.quota_used,
        "quota_total": staged.quota_total,
        "state": ReviewStageState.REVIEW_ONLY.value,
        "content_reference": _CONTENT_REFERENCE,
        "private_content": {"normalized_text": text, "source_attribution": attribution},
    }
    metadata["record_sha256"] = _record_digest(metadata)
    return json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _record_digest(record: dict[str, object]) -> str:
    unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
    payload = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(_RECORD_HASH_DOMAIN + payload).hexdigest()


def _scan_records(
    account_dir: Path,
    account_id: UUID,
) -> list[tuple[StagedReviewV1, dict[str, object]]]:
    _verify_private_directory(account_dir)
    try:
        entries = list(os.scandir(account_dir))
    except OSError:
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
    if len(entries) > _MAX_STAGES_PER_ACCOUNT * 2 + 1:
        raise _StoreFault(ReviewStageReason.QUEUE_FULL)
    result: list[tuple[StagedReviewV1, dict[str, object]]] = []
    for entry in entries:
        if entry.name == ".lock":
            _verify_private_regular_file(account_dir / entry.name)
            continue
        match = _STAGE_FILE.fullmatch(entry.name)
        if match is None:
            if _STAGE_TEMP.fullmatch(entry.name) is not None:
                _verify_private_regular_file(account_dir / entry.name)
                raise _StoreFault(ReviewStageReason.INCOMPLETE_TEMPORARY_RECORD)
            raise _StoreFault(ReviewStageReason.UNSAFE_PATH)
        try:
            path_stage_id = UUID(match.group(1))
        except ValueError:
            raise _StoreFault(ReviewStageReason.UNSAFE_PATH) from None
        staged, body = _read_record(account_dir / entry.name, account_id, path_stage_id)
        result.append((staged, body))
    if len(result) > _MAX_STAGES_PER_ACCOUNT:
        raise _StoreFault(ReviewStageReason.QUEUE_FULL)
    return result


def _read_record(
    path: Path,
    account_id: UUID,
    path_stage_id: UUID,
) -> tuple[StagedReviewV1, dict[str, object]]:
    _verify_private_regular_file(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD) from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_RECORD_BYTES
        ):
            raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
        chunks: list[bytes] = []
        remaining = _MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > _MAX_RECORD_BYTES:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except UnicodeError, ValueError, TypeError, json.JSONDecodeError:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD) from None
    staged, body = _parse_record(value, account_id, path_stage_id)
    return staged, body


def _parse_record(
    value: object,
    expected_account: UUID,
    expected_stage: UUID,
) -> tuple[StagedReviewV1, dict[str, object]]:
    if type(value) is not dict:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    record = cast(dict[str, object], value)
    if frozenset(record) != _RECORD_KEYS:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    if (
        record["schema"] != _SCHEMA
        or type(record["schema_version"]) is not int
        or record["schema_version"] != _SCHEMA_VERSION
        or record["state"] != ReviewStageState.REVIEW_ONLY.value
        or record["content_reference"] != _CONTENT_REFERENCE
        or type(record["right_to_use_declared"]) is not bool
        or record["right_to_use_declared"] is not True
    ):
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    try:
        stage_id = _parse_uuid(record["stage_id"])
        account_id = _parse_uuid(record["account_id"])
        action = ActionKind(record["action"])
        source = CandidateSource(record["source"])
        provenance = ObservationProvenance(record["provenance"])
        content_origin = ContentOrigin(record["content_origin"])
        rights_basis = RightsBasis(record["rights_basis"])
        facts_declaration = JobFactsDeclaration(record["job_facts_declaration"])
        permissions_value = record["required_permissions"]
        if type(permissions_value) is not list or not permissions_value:
            raise ValueError
        permissions_raw = cast(list[object], permissions_value)
        if any(type(item) is not str for item in permissions_raw):
            raise ValueError
        permissions = tuple(ApiPermission(cast(str, item)) for item in permissions_raw)
        dates: dict[str, datetime] = {
            key: _parse_utc(record[key])
            for key in (
                "prepared_at",
                "effective_at",
                "packet_expires_at",
                "content_expires_at",
                "expires_at",
                "staged_at",
                "capabilities_observed_at",
                "quota_observed_at",
                "candidate_observed_at",
                "binding_observed_at",
            )
        }
    except ValueError, TypeError, KeyError:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD) from None

    if stage_id != expected_stage or account_id != expected_account:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    if len(set(permissions)) != len(permissions):
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    for key in (
        "actor_sha256",
        "candidate_fingerprint",
        "packet_revision_sha256",
        "preview_sha256",
        "display_context_sha256",
        "target_sha256",
        "context_sha256",
        "context_evidence_sha256",
        "policy_sha256",
        "content_sha256",
        "source_attribution_sha256",
        "job_facts_sha256",
        "capability_snapshot_sha256",
        "quota_snapshot_sha256",
        "record_sha256",
    ):
        if not _is_sha256(record[key]):
            raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    for key in ("root_sha256", "parent_sha256", "reply_sha256"):
        if record[key] is not None and not _is_sha256(record[key]):
            raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    if (
        type(record["quota_used"]) is not int
        or type(record["quota_total"]) is not int
        or record["quota_used"] < 0
        or record["quota_total"] <= 0
        or record["quota_used"] >= record["quota_total"]
    ):
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    if (
        type(record["normalization_version"]) is not str
        or record["normalization_version"] != "nfc-lf-v1"
        or dates["prepared_at"] > dates["effective_at"]
        or dates["effective_at"] >= dates["content_expires_at"]
        or dates["content_expires_at"] > dates["packet_expires_at"]
        or dates["expires_at"] != min(dates["content_expires_at"], dates["packet_expires_at"])
        or dates["staged_at"] >= dates["expires_at"]
        or any(
            dates[key] > dates["staged_at"]
            for key in (
                "prepared_at",
                "effective_at",
                "capabilities_observed_at",
                "quota_observed_at",
                "candidate_observed_at",
                "binding_observed_at",
            )
        )
    ):
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    if action is ActionKind.POST_TEXT:
        expected_permissions = (ApiPermission.THREADS_BASIC, ApiPermission.THREADS_CONTENT_PUBLISH)
        valid_binding = (
            source is CandidateSource.OWN_ACCOUNT
            and record["root_sha256"] is None
            and record["parent_sha256"] is None
            and record["reply_sha256"] is None
            and provenance
            in {
                ObservationProvenance.LOCAL_OPERATOR_PACKET,
                ObservationProvenance.SYNTHETIC_FIXTURE,
            }
        )
    elif action is ActionKind.REPLY:
        expected_permissions = (
            ApiPermission.THREADS_BASIC,
            ApiPermission.THREADS_READ_REPLIES,
            ApiPermission.THREADS_CONTENT_PUBLISH,
        )
        valid_binding = (
            source is CandidateSource.INBOUND_REPLY
            and all(
                record[key] is not None for key in ("root_sha256", "parent_sha256", "reply_sha256")
            )
            and provenance
            in {ObservationProvenance.OFFICIAL_API, ObservationProvenance.SYNTHETIC_FIXTURE}
        )
    else:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    if content_origin is ContentOrigin.SYNTHETIC_FIXTURE:
        valid_rights = (
            provenance is ObservationProvenance.SYNTHETIC_FIXTURE
            and rights_basis is RightsBasis.SYNTHETIC_FIXTURE
            and facts_declaration is JobFactsDeclaration.SYNTHETIC_FIXTURE
        )
    else:
        valid_rights = (
            content_origin is ContentOrigin.OPERATOR_AUTHORED
            and provenance is not ObservationProvenance.SYNTHETIC_FIXTURE
            and rights_basis not in {RightsBasis.SYNTHETIC_FIXTURE, RightsBasis.UNKNOWN}
            and facts_declaration is JobFactsDeclaration.OPERATOR_VERIFIED
        )
    if not valid_binding or not valid_rights or permissions != expected_permissions:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    private_content_value = record["private_content"]
    if type(private_content_value) is not dict:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    private_content = cast(dict[str, object], private_content_value)
    if set(private_content) != {
        "normalized_text",
        "source_attribution",
    }:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    text_value = cast(str, private_content["normalized_text"])
    attribution = cast(str, private_content["source_attribution"])
    if (
        type(private_content["normalized_text"]) is not str
        or type(private_content["source_attribution"]) is not str
        or not _valid_normalized_content(text_value)
        or not attribution.strip()
        or len(attribution) > _MAX_ATTRIBUTION_CODEPOINTS
        or any(unicodedata.category(character) == "Cc" for character in attribution)
    ):
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    try:
        text_value.encode("utf-8", errors="strict")
        attribution.encode("utf-8", errors="strict")
    except UnicodeError:
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD) from None
    if (
        content_sha256_for(text_value) != record["content_sha256"]
        or _sha_text(attribution) != record["source_attribution_sha256"]
        or _record_digest(record) != record["record_sha256"]
    ):
        raise _StoreFault(ReviewStageReason.CORRUPT_RECORD)
    staged = StagedReviewV1(
        schema_version=cast(int, record["schema_version"]),
        stage_id=stage_id,
        account_id=account_id,
        action=action,
        source=source,
        provenance=provenance,
        actor_sha256=cast(str, record["actor_sha256"]),
        candidate_fingerprint=cast(str, record["candidate_fingerprint"]),
        packet_revision_sha256=cast(str, record["packet_revision_sha256"]),
        preview_sha256=cast(str, record["preview_sha256"]),
        display_context_sha256=cast(str, record["display_context_sha256"]),
        target_sha256=cast(str, record["target_sha256"]),
        context_sha256=cast(str, record["context_sha256"]),
        root_sha256=cast(str | None, record["root_sha256"]),
        parent_sha256=cast(str | None, record["parent_sha256"]),
        reply_sha256=cast(str | None, record["reply_sha256"]),
        context_evidence_sha256=cast(str, record["context_evidence_sha256"]),
        policy_sha256=cast(str, record["policy_sha256"]),
        content_sha256=cast(str, record["content_sha256"]),
        content_origin=content_origin,
        normalization_version=cast(str, record["normalization_version"]),
        rights_basis=rights_basis,
        right_to_use_declared=True,
        source_attribution_sha256=cast(str, record["source_attribution_sha256"]),
        job_facts_sha256=cast(str, record["job_facts_sha256"]),
        job_facts_declaration=facts_declaration,
        prepared_at=dates["prepared_at"],
        effective_at=dates["effective_at"],
        packet_expires_at=dates["packet_expires_at"],
        content_expires_at=dates["content_expires_at"],
        expires_at=dates["expires_at"],
        staged_at=dates["staged_at"],
        required_permissions=permissions,
        capability_snapshot_sha256=cast(str, record["capability_snapshot_sha256"]),
        quota_snapshot_sha256=cast(str, record["quota_snapshot_sha256"]),
        capabilities_observed_at=dates["capabilities_observed_at"],
        quota_observed_at=dates["quota_observed_at"],
        candidate_observed_at=dates["candidate_observed_at"],
        binding_observed_at=dates["binding_observed_at"],
        quota_used=record["quota_used"],
        quota_total=record["quota_total"],
    )
    return staged, record


def _atomic_create_record(account_dir: Path, staged: StagedReviewV1, record: bytes) -> None:
    stage_id = staged.stage_id
    final_path = account_dir / f"{stage_id}.json"
    temporary_path = account_dir / f".stage-{stage_id}.tmp"
    if _path_lexists(final_path) or _path_lexists(temporary_path):
        raise _StoreFault(ReviewStageReason.DUPLICATE_STAGE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary_path, flags, 0o600)
    except FileExistsError:
        raise _StoreFault(ReviewStageReason.DUPLICATE_STAGE) from None
    except OSError:
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
    published = False
    try:
        _secure_new_file(temporary_path)
        _stage_failpoint("before_temp_write")
        view = memoryview(record)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise _StoreFault(ReviewStageReason.IO_FAILURE)
            view = view[written:]
        _stage_failpoint("after_temp_write_before_fsync")
        os.fsync(descriptor)
        _stage_failpoint("after_file_fsync")
        _verify_private_regular_file(temporary_path)
    finally:
        os.close(descriptor)
    try:
        if _path_lexists(final_path):
            raise _StoreFault(ReviewStageReason.DUPLICATE_STAGE)
        _stage_failpoint("before_atomic_publish")
        _atomic_rename_no_clobber(temporary_path, final_path)
        published = True
        _stage_failpoint("after_atomic_publish")
        _sync_directory(account_dir)
        _stage_failpoint("after_directory_fsync")
        _verify_private_regular_file(final_path)
    except _StoreFault:
        raise
    except Exception:  # noqa: BLE001 - a post-rename failure has an ambiguous durable outcome
        if published:
            raise _StoreFault(ReviewStageReason.PUBLICATION_AMBIGUOUS) from None
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None


def _atomic_rename_no_clobber(source: Path, destination: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError:
            raise _StoreFault(ReviewStageReason.DUPLICATE_STAGE) from None
        except OSError:
            raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
        return
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise _StoreFault(ReviewStageReason.UNSUPPORTED_PLATFORM)
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == 17:
            raise _StoreFault(ReviewStageReason.DUPLICATE_STAGE)
        raise _StoreFault(ReviewStageReason.IO_FAILURE)
    raise _StoreFault(ReviewStageReason.UNSUPPORTED_PLATFORM)


def _recover_temporary_records(account_dir: Path) -> None:
    try:
        entries = list(os.scandir(account_dir))
    except OSError:
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
    temporaries = [entry for entry in entries if _STAGE_TEMP.fullmatch(entry.name)]
    if len(temporaries) > _MAX_STAGES_PER_ACCOUNT:
        raise _StoreFault(ReviewStageReason.QUEUE_FULL)
    for entry in temporaries:
        temporary_path = account_dir / entry.name
        _verify_private_regular_file(temporary_path)
    if temporaries:
        raise _StoreFault(ReviewStageReason.INCOMPLETE_TEMPORARY_RECORD)


def _ensure_private_directory(path: Path) -> None:
    if _path_lexists(path):
        _verify_private_directory(path)
        return
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        _verify_private_directory(path)
        return
    except OSError:
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
    try:
        if os.name == "nt":
            _set_windows_private_acl(path, directory=True)
        else:
            os.chmod(path, 0o700, follow_symlinks=False)
        _verify_private_directory(path)
        _sync_directory(path.parent)
    except _StoreFault:
        raise
    except Exception:  # noqa: BLE001 - ACL setup must fail closed
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE) from None


def _verify_private_directory(path: Path) -> None:
    _verify_path_components(path)
    try:
        info = path.lstat()
    except OSError:
        raise _StoreFault(ReviewStageReason.UNSAFE_PATH) from None
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
        raise _StoreFault(ReviewStageReason.UNSAFE_PATH)
    if os.name == "nt":
        _verify_windows_acl(path, directory=True, exact=True)
    else:
        if stat.S_IMODE(info.st_mode) != 0o700 or not _owned_by_current_user(info):
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        _reject_posix_acl(path)


def _verify_private_regular_file(path: Path) -> None:
    _verify_path_components(path)
    try:
        info = path.lstat()
    except OSError:
        raise _StoreFault(ReviewStageReason.UNSAFE_PATH) from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _is_reparse_point(info):
        raise _StoreFault(ReviewStageReason.UNSAFE_PATH)
    if os.name == "nt":
        _verify_windows_acl(path, directory=False, exact=True)
    else:
        if stat.S_IMODE(info.st_mode) != 0o600 or not _owned_by_current_user(info):
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        _reject_posix_acl(path)


def _ensure_private_lock_file(path: Path) -> None:
    if _path_lexists(path):
        _verify_private_regular_file(path)
        return
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _verify_private_regular_file(path)
        return
    except OSError:
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
    try:
        if os.name == "nt":
            # The account directory has an explicit current-user-only DACL;
            # this file inherits and verifies that exact single-user grant.
            pass
        else:
            os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except Exception:  # noqa: BLE001 - ACL setup must fail closed
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE) from None
    finally:
        os.close(descriptor)
    _verify_private_regular_file(path)
    _sync_directory(path.parent)


def _secure_new_file(path: Path) -> None:
    if os.name == "nt":
        # The account directory has an explicit current-user-only DACL;
        # verify the inherited single-user ACL before writing private bytes.
        _verify_private_regular_file(path)
    else:
        os.chmod(path, 0o600, follow_symlinks=False)
        _verify_private_regular_file(path)


def _verify_private_data_root(root: Path) -> None:
    if os.name == "nt":
        try:
            _verify_windows_ancestor_chain(root)
            _verify_windows_acl(root, directory=True, exact=False)
        except _StoreFault as error:
            if error.reason is ReviewStageReason.ACL_UNSAFE:
                raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT) from None
            raise
        return
    _verify_posix_ancestor_chain(root)
    try:
        info = root.stat(follow_symlinks=False)
    except OSError:
        raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT) from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or not _owned_by_current_user(info)
    ):
        raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT)
    _reject_posix_acl(root, root=True)


def _verify_path_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {".", ".."}:
            raise _StoreFault(ReviewStageReason.UNSAFE_PATH)
        if os.name == "nt" and (part.endswith((".", " ")) or ":" in part):
            raise _StoreFault(ReviewStageReason.UNSAFE_PATH)
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            raise _StoreFault(ReviewStageReason.UNSAFE_PATH) from None
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise _StoreFault(ReviewStageReason.UNSAFE_PATH)


def _verify_posix_ancestor_chain(root: Path) -> None:
    current = root.parent
    while current != current.parent:
        try:
            info = current.stat(follow_symlinks=False)
        except OSError:
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT) from None
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise _StoreFault(ReviewStageReason.UNSAFE_DATA_ROOT)
        _reject_posix_acl(current, root=True)
        current = current.parent


def _reject_posix_acl(path: Path, *, root: bool = False) -> None:
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        return
    try:
        names = listxattr(path, follow_symlinks=False)
    except OSError:
        if sys.platform == "linux":
            raise _StoreFault(
                ReviewStageReason.UNSAFE_DATA_ROOT if root else ReviewStageReason.ACL_UNSAFE
            ) from None
        return
    if any(name in {"system.posix_acl_access", "system.posix_acl_default"} for name in names):
        raise _StoreFault(
            ReviewStageReason.UNSAFE_DATA_ROOT if root else ReviewStageReason.ACL_UNSAFE
        )


def _verify_windows_ancestor_chain(root: Path) -> None:
    current = root.parent
    while True:
        _verify_windows_acl(current, directory=True, exact=False, ancestors=True)
        if current == Path(current.anchor):
            break
        current = current.parent


def _set_windows_private_acl(path: Path, *, directory: bool) -> None:
    sid = _windows_current_user_sid()
    system_root = os.environ.get("SystemRoot")
    if type(system_root) is not str or not system_root:
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
    icacls = Path(system_root) / "System32" / "icacls.exe"
    if not icacls.is_file():
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
    permissions = ":(OI)(CI)F" if directory else ":F"

    def run_icacls(*arguments: str) -> None:
        command = [str(icacls), str(path), *arguments, "/q"]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError, subprocess.SubprocessError:
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE) from None
        if result.returncode != 0:
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)

    # These paths were just created by this process, so replacing their DACL
    # cannot relax or rewrite an existing operator-owned directory.
    run_icacls("/inheritance:r")
    inherited_sids = {entry_sid for entry_sid, _mask, _kind, _flags in _windows_acl_entries(path)}
    for inherited_sid in inherited_sids:
        run_icacls("/remove", f"*{inherited_sid}")
    run_icacls("/grant:r", f"*{sid}{permissions}")
    _verify_windows_acl(path, directory=directory, exact=True)


def _verify_windows_acl(
    path: Path,
    *,
    directory: bool,
    exact: bool,
    ancestors: bool = False,
) -> None:
    current_sid = _windows_current_user_sid()
    safe_sids = {current_sid, "S-1-5-18", "S-1-5-32-544"}
    entries = _windows_acl_entries(path)
    if not entries:
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
    if exact:
        expected_flags = 0x03 if directory else 0
        if len(entries) != 1:
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        sid, mask, ace_type, flags = entries[0]
        flags_match = flags == expected_flags or (not directory and flags == 0x10)
        if (
            sid != current_sid
            or ace_type != 0
            or not flags_match
            or mask & 0x001F01FF != 0x001F01FF
        ):
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        return

    user_has_full = False
    path_replace_mask = 0x00000040 | 0x00010000 | 0x00040000 | 0x00080000 | 0x40000000 | 0x10000000
    for sid, mask, ace_type, flags in entries:
        if ace_type != 0:
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        if sid == current_sid:
            user_has_full = user_has_full or (mask & 0x001F01FF) == 0x001F01FF
            continue
        if sid in safe_sids:
            continue
        if ancestors:
            # Inherit-only ACEs do not apply to the current path component.
            # Broad create-only rights cannot replace an existing component;
            # delete-child, DACL/owner changes, and generic write can.
            if flags & 0x08:
                continue
            if mask & path_replace_mask:
                raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
            continue
        # A shared data-root may grant traversal so callers can reach a private
        # child, but broad read/list or write rights would expose stored text.
        if mask & ~(0x00000020 | 0x00100000):
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
    if not user_has_full and not ancestors:
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE)


def _windows_acl_entries(path: Path) -> list[tuple[str, int, int, int]]:
    try:
        from ctypes import wintypes
    except ImportError:
        raise _StoreFault(ReviewStageReason.UNSUPPORTED_PLATFORM) from None
    advapi = _load_windows_dll("advapi32")
    kernel = _load_windows_dll("kernel32")
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    get_security = advapi.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    status = get_security(
        str(path),
        1,
        0x00000004,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    )
    if status != 0 or not dacl.value:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE)

    class _Acl(ctypes.Structure):
        _fields_ = [
            ("revision", ctypes.c_ubyte),
            ("reserved_1", ctypes.c_ubyte),
            ("size", ctypes.c_ushort),
            ("ace_count", ctypes.c_ushort),
            ("reserved_2", ctypes.c_ushort),
        ]

    get_ace = advapi.GetAce
    get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    get_ace.restype = wintypes.BOOL
    convert_sid = advapi.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    local_free = kernel.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    entries: list[tuple[str, int, int, int]] = []
    try:
        acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
        for index in range(acl.ace_count):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
            header = ctypes.string_at(ace.value, 4)
            ace_type, ace_flags = header[0], header[1]
            if ace_type not in {0, 1}:
                raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid_pointer = ctypes.c_void_p(ace.value + 8)
            sid_text = wintypes.LPWSTR()
            if not convert_sid(sid_pointer, ctypes.byref(sid_text)) or not sid_text.value:
                raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
            entries.append((sid_text.value.upper(), mask, ace_type, ace_flags))
            local_free(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        local_free(descriptor)
    return entries


def _windows_current_user_sid() -> str:
    try:
        from ctypes import wintypes
    except ImportError:
        raise _StoreFault(ReviewStageReason.UNSUPPORTED_PLATFORM) from None
    advapi = _load_windows_dll("advapi32")
    kernel = _load_windows_dll("kernel32")
    token = wintypes.HANDLE()
    get_current_process = kernel.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi.OpenProcessToken
    open_process_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not open_process_token(get_current_process(), 0x0008, ctypes.byref(token)):
        raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
    try:
        size = wintypes.DWORD()
        get_token_information(token, 1, None, 0, ctypes.byref(size))
        if size.value <= 0:
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        buffer = ctypes.create_string_buffer(size.value)
        if not get_token_information(token, 1, buffer, size, ctypes.byref(size)):
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        convert_sid = advapi.ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        convert_sid.restype = wintypes.BOOL
        if not convert_sid(sid_pointer, ctypes.byref(sid_text)) or not sid_text.value:
            raise _StoreFault(ReviewStageReason.ACL_UNSAFE)
        value = sid_text.value.upper()
        kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        return value
    finally:
        close_handle(token)


def _load_windows_dll(name: str) -> Any:
    loader = cast(Callable[..., Any] | None, getattr(ctypes, "WinDLL", None))
    if loader is None:
        raise _StoreFault(ReviewStageReason.UNSUPPORTED_PLATFORM)
    return loader(name, use_last_error=True)


def _acquire_account_lock(account_dir: Path) -> FilesystemProcessLock:
    _verify_private_directory(account_dir)
    lock_path = account_dir / ".lock"
    _ensure_private_lock_file(lock_path)
    lock = FilesystemProcessLock(lock_path)
    lock.acquire()
    try:
        _verify_private_regular_file(lock_path)
        return lock
    except Exception:
        lock.release()
        raise


def _acquire_existing_account_lock(account_dir: Path) -> FilesystemProcessLock:
    _verify_private_directory(account_dir)
    lock_path = account_dir / ".lock"
    _verify_private_regular_file(lock_path)
    lock = FilesystemProcessLock(lock_path)
    lock.acquire()
    try:
        _verify_private_regular_file(lock_path)
        return lock
    except Exception:
        lock.release()
        raise


def _stage_result(
    decision: ReviewStageDecision,
    reason: ReviewStageReason | None = None,
) -> ReviewStageResult:
    return ReviewStageResult(decision, (reason,) if reason is not None else (), None)


def _read_result(
    decision: ReviewStageDecision,
    reason: ReviewStageReason | None = None,
    stage: StagedReviewV1 | None = None,
) -> ReviewStageReadResult:
    return ReviewStageReadResult(decision, (reason,) if reason is not None else (), stage)


def _content_result(
    decision: ReviewStageDecision,
    reason: ReviewStageReason | None = None,
    content: StagedReviewContent | None = None,
) -> ReviewStageContentResult:
    return ReviewStageContentResult(decision, (reason,) if reason is not None else (), content)


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _parse_uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if not _is_utc(parsed) or _format_utc(parsed) != value:
        raise ValueError
    return parsed


def _format_utc(value: datetime) -> str:
    if not _is_utc(value):
        raise _StoreFault(ReviewStageReason.INVALID_INPUT)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)
    )


def _valid_normalized_content(value: str) -> bool:
    if (
        not value
        or len(value) > _MAX_CONTENT_CODEPOINTS
        or value != unicodedata.normalize("NFC", value)
        or "\r" in value
        or not value.strip()
    ):
        return False
    return not any(
        unicodedata.category(character) == "Cc" and character != "\n" for character in value
    )


def _capability_digest(scope: CampaignScope) -> str:
    snapshot = scope.capabilities
    payload = {
        "account_id": str(snapshot.account_id),
        "actor_sha256": _sha_text(snapshot.actor_id),
        "permissions": sorted(item.value for item in snapshot.permissions),
        "verified": snapshot.verified,
        "observed_at": _format_utc(snapshot.observed_at),
    }
    return _domain_digest(_CAPABILITY_HASH_DOMAIN, payload)


def _quota_digest(scope: CampaignScope) -> str:
    quota = scope.quota
    payload = {
        "post_usage": quota.post_usage,
        "post_total": quota.post_total,
        "reply_usage": quota.reply_usage,
        "reply_total": quota.reply_total,
        "verified": quota.verified,
        "observed_at": _format_utc(quota.observed_at),
    }
    return _domain_digest(_QUOTA_HASH_DOMAIN, payload)


def _domain_digest(domain: bytes, payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(domain + raw).hexdigest()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _path_lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise _StoreFault(ReviewStageReason.UNSAFE_PATH) from None


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _owned_by_current_user(info: os.stat_result) -> bool:
    getuid = cast(Callable[[], int] | None, getattr(os, "getuid", None))
    return getuid is not None and info.st_uid == getuid()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_inside_checkout(path: Path) -> bool:
    module_path = Path(__file__).resolve()
    search_paths = (module_path.parents, (path, *path.parents), Path.cwd().resolve().parents)
    checkout_roots: set[Path] = set()
    for chain in search_paths:
        for parent in chain:
            if (parent / "pyproject.toml").is_file() and (parent / ".git").exists():
                checkout_roots.add(parent)
    key = _path_key(path)
    for root in checkout_roots:
        root_key = _path_key(root)
        try:
            if os.path.commonpath((key, root_key)) == root_key:
                return True
        except ValueError:
            continue
    return False


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if sys.platform == "linux":
            raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
        return
    try:
        os.fsync(descriptor)
    except OSError:
        raise _StoreFault(ReviewStageReason.IO_FAILURE) from None
    finally:
        os.close(descriptor)


def _stage_failpoint(_name: str) -> None:
    """Private test seam for crash-boundary regression tests."""
