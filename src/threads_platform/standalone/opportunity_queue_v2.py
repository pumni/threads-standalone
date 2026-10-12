"""Private, account-scoped v2 opportunity queue for manual and synthetic sources."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, cast
from uuid import UUID, uuid4

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import (
    normalize_manual_permalink,
    normalize_search_query,
)
from threads_platform.standalone.browser_opportunity_bridge import (
    BrowserOpportunityProposal,
    BrowserOpportunityProposalBatch,
    ConversationObservationEvidence,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeError, ManualOutcomeStore
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueue,
    OpportunityQueueEntry,
    OpportunityQueueError,
    OpportunityQueueStore,
)

QueueSourceV2 = Literal["MANUAL_URL", "SYNTHETIC_FIXTURE"]
OpportunityStateV2 = Literal["NEEDS_CONTEXT", "READY", "SKIPPED"]
ContextCompletenessV2 = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
ContextProvenanceV2 = Literal["NONE", "OPERATOR_SUPPLIED", "SYNTHETIC_FIXTURE"]
AuthorIdentityStatusV2 = Literal["UNKNOWN", "UNVERIFIED"]

_NAMESPACE = "opportunity_queue_v2"
_VERSION = 2
_MAX_ACTIVE = 20
_MAX_RETAINED = 40
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_URL_BYTES = 2_048
_MAX_CONTEXT_CHARS = 4_000
_MAX_EXCERPT_CHARS = 500
_MAX_QUERY_CHARS = 120
_MAX_SKIP_REASON_CHARS = 1_000
_CONTEXT_STATUSES = frozenset({"UNKNOWN", "PARTIAL", "COMPLETE"})
_STATES = frozenset({"NEEDS_CONTEXT", "READY", "SKIPPED"})
_CONTEXT_PROVENANCES = frozenset({"NONE", "OPERATOR_SUPPLIED", "SYNTHETIC_FIXTURE"})
_REASONS = frozenset(
    {
        "AUTHOR_IDENTITY_UNVERIFIED",
        "CONTEXT_UNKNOWN",
        "CONTEXT_PARTIAL",
        "CONVERSATION_NOT_PROVIDED",
        "CONVERSATION_TRUNCATED",
        "EXCERPT_TRUNCATED",
        "SEARCH_RESULTS_TRUNCATED",
        "SYNTHETIC_SOURCE_REQUIRES_VERSIONED_QUEUE",
    }
)
_TOP_LEVEL_KEYS = frozenset({"version", "account_uuid", "opportunities"})
_ENTRY_KEYS = frozenset(
    {
        "id",
        "target_url",
        "source",
        "author_marker",
        "author_identity_status",
        "context",
        "context_provenance",
        "context_completeness",
        "source_evidence",
        "created_at",
        "updated_at",
        "state",
        "skip_reason",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "proposal_result_version",
        "query",
        "excerpt",
        "position",
        "search_context_status",
        "context_status",
        "truncated",
        "search_observation_truncated",
        "search_result_truncated",
        "conversation_result_truncated",
        "conversation_observations",
        "reason_codes",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "target_url",
        "author_marker",
        "author_identity_status",
        "excerpt",
        "position",
        "parent_url",
        "is_root",
        "truncated",
    }
)


class OpportunityQueueV2Error(ValueError):
    """Fixed-code local failure with no account, URL, or content in its message."""

    _CODES = frozenset(
        {
            "OPPORTUNITY_QUEUE_V2_ACCOUNT_MISMATCH",
            "OPPORTUNITY_QUEUE_V2_BUSY",
            "OPPORTUNITY_QUEUE_V2_CONFIRMATION_REQUIRED",
            "OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID",
            "OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND",
            "OPPORTUNITY_QUEUE_V2_INVALID",
            "OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE",
            "OPPORTUNITY_QUEUE_V2_LIMIT_REACHED",
            "OPPORTUNITY_QUEUE_V2_MIGRATION_SOURCE_CHANGED",
            "OPPORTUNITY_QUEUE_V2_MIGRATION_SOURCE_MISSING",
            "OPPORTUNITY_QUEUE_V2_MIGRATION_TARGET_EXISTS",
            "OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported opportunity queue v2 error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ConversationObservationV2:
    target_url: str
    author_marker: str
    author_identity_status: Literal["UNVERIFIED"]
    excerpt: str | None
    position: int
    parent_url: str | None
    is_root: bool
    truncated: bool


@dataclass(frozen=True, slots=True, repr=False)
class OpportunitySourceEvidenceV2:
    """Immutable source-side bridge evidence, separate from operator context."""

    proposal_result_version: Literal[1]
    query: str
    excerpt: str | None
    position: int
    search_context_status: Literal["UNKNOWN"]
    context_status: ContextCompletenessV2
    truncated: bool
    search_observation_truncated: bool
    search_result_truncated: bool
    conversation_result_truncated: bool | None
    conversation_observations: tuple[ConversationObservationV2, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityQueueEntryV2:
    id: str
    target_url: str
    source: QueueSourceV2
    author_marker: str | None
    author_identity_status: AuthorIdentityStatusV2
    context: str | None
    context_provenance: ContextProvenanceV2
    context_completeness: ContextCompletenessV2
    source_evidence: OpportunitySourceEvidenceV2 | None
    created_at: str
    updated_at: str
    state: OpportunityStateV2
    skip_reason: str | None


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityQueueV2:
    account_uuid: str
    opportunities: tuple[OpportunityQueueEntryV2, ...]

    @property
    def active_count(self) -> int:
        return sum(entry.state != "SKIPPED" for entry in self.opportunities)


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityQueueV2Summary:
    added_count: int
    duplicate_count: int
    active_count: int
    total_count: int


@dataclass(frozen=True, slots=True, repr=False)
class V1MigrationPreview:
    account_uuid: str
    source_exists: bool
    source_sha256: str | None
    v1_total_count: int
    v1_active_count: int
    v1_ready_count: int
    v1_needs_context_count: int
    v1_skipped_count: int
    v2_target_exists: bool

    @property
    def confirmation_token(self) -> str | None:
        if not self.source_exists or self.source_sha256 is None or self.v2_target_exists:
            return None
        return f"MIGRATE_V1_TO_V2:{self.account_uuid}:{self.source_sha256}"


@dataclass(frozen=True, slots=True, repr=False)
class V1MigrationResult:
    account_uuid: str
    migrated_count: int
    active_count: int
    source_sha256: str


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _field(value: object, name: str) -> object:
    return getattr(value, name, None)


def _is_reparse_point(info: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_attribute)


def _validate_account_id(account_id: object) -> UUID:
    if type(account_id) is not UUID or account_id.version != 4:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    return account_id


def _normalize_target(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > _MAX_URL_BYTES:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    try:
        if len(value.encode("utf-8", errors="strict")) > _MAX_URL_BYTES:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        normalized = normalize_manual_permalink(value)
    except OpportunityQueueV2Error:
        raise
    except BrowserContractError, UnicodeError, ValueError:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
    return normalized.thread_ref, normalized.author_username


def _canonical_target(value: object) -> tuple[str, str]:
    target, author = _normalize_target(value)
    if target != value:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    return target, author


def _normalize_context(value: object) -> str:
    if not isinstance(value, str):
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID")
    normalized = value.replace("\r\n", "\n")
    for character in normalized:
        category = unicodedata.category(character)
        if character == "\n":
            continue
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID")
        if category == "Zs" and character != " ":
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID")
    if "\r" in normalized:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID")
    normalized = normalized.strip()
    if not normalized or len(normalized) > _MAX_CONTEXT_CHARS:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID")
    return normalized


def _normalize_skip_reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_SKIP_REASON_CHARS:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in normalized):
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    return normalized


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
    return parsed


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_excerpt(value: object) -> bool:
    return value is None or isinstance(value, str) and len(value) <= _MAX_EXCERPT_CHARS


def _validate_parent_cycles(observations: tuple[ConversationObservationV2, ...]) -> None:
    parents = {row.target_url: row.parent_url for row in observations}
    for target in parents:
        seen: set[str] = set()
        current: str | None = target
        while current is not None and current in parents:
            if current in seen:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
            seen.add(current)
            current = parents[current]


class _LockedV1QueueReader(OpportunityQueueStore):
    """Read v1 under the already-held shared account lock; never writes v1."""

    def snapshot(self, root: Path, account_id: UUID) -> tuple[OpportunityQueue, bytes | None]:
        try:
            source_path = self._queue_path(root, account_id, create=False)
            if source_path is None or not self._regular_file(
                source_path, source_path.parent, allow_missing=True
            ):
                return OpportunityQueue(str(account_id), ()), None
            with source_path.open("rb") as stream:
                raw = stream.read(_MAX_DOCUMENT_BYTES + 1)
            if len(raw) > _MAX_DOCUMENT_BYTES:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            queue = self._decode_queue(value, account_id)
        except OpportunityQueueError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        except OpportunityQueueV2Error:
            raise
        except OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        return queue, raw


def _observation_to_json(row: ConversationObservationV2) -> dict[str, object]:
    return {
        "target_url": row.target_url,
        "author_marker": row.author_marker,
        "author_identity_status": row.author_identity_status,
        "excerpt": row.excerpt,
        "position": row.position,
        "parent_url": row.parent_url,
        "is_root": row.is_root,
        "truncated": row.truncated,
    }


def _evidence_to_json(evidence: OpportunitySourceEvidenceV2) -> dict[str, object]:
    return {
        "proposal_result_version": evidence.proposal_result_version,
        "query": evidence.query,
        "excerpt": evidence.excerpt,
        "position": evidence.position,
        "search_context_status": evidence.search_context_status,
        "context_status": evidence.context_status,
        "truncated": evidence.truncated,
        "search_observation_truncated": evidence.search_observation_truncated,
        "search_result_truncated": evidence.search_result_truncated,
        "conversation_result_truncated": evidence.conversation_result_truncated,
        "conversation_observations": [
            _observation_to_json(row) for row in evidence.conversation_observations
        ],
        "reason_codes": list(evidence.reason_codes),
    }


def _entry_to_json(entry: OpportunityQueueEntryV2) -> dict[str, object]:
    return {
        "id": entry.id,
        "target_url": entry.target_url,
        "source": entry.source,
        "author_marker": entry.author_marker,
        "author_identity_status": entry.author_identity_status,
        "context": entry.context,
        "context_provenance": entry.context_provenance,
        "context_completeness": entry.context_completeness,
        "source_evidence": (
            None if entry.source_evidence is None else _evidence_to_json(entry.source_evidence)
        ),
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "state": entry.state,
        "skip_reason": entry.skip_reason,
    }


class OpportunityQueueV2Store:
    """Strict v2 queue with shared account locks and explicit source-preserving intake."""

    def __init__(self, data_root: Path) -> None:
        self._configured_root = Path(data_root).absolute()

    @contextmanager
    def account_lock(self, account_id: UUID) -> Generator[None]:
        account = _validate_account_id(account_id)
        root = self._required_root()
        try:
            with ManualOutcomeStore(root).account_lock(account):
                yield
        except ManualOutcomeError as error:
            code = (
                "OPPORTUNITY_QUEUE_V2_BUSY"
                if error.code == "MANUAL_OUTCOME_BUSY"
                else "OPPORTUNITY_QUEUE_V2_INVALID"
            )
            raise OpportunityQueueV2Error(code) from None

    def list(self, account_id: UUID) -> OpportunityQueueV2:
        account = _validate_account_id(account_id)
        with self.account_lock(account):
            return self._read_unlocked(self._required_root(), account)

    def get(self, account_id: UUID, opportunity_id: str) -> OpportunityQueueEntryV2:
        identifier = self._validate_id(opportunity_id)
        queue = self.list(account_id)
        for entry in queue.opportunities:
            if entry.id == identifier:
                return entry
        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND")

    def _proposal_entry(
        self,
        proposal: object,
        *,
        account_id: UUID,
        query: str,
        batch_truncated: bool,
        created_at: str,
    ) -> OpportunityQueueEntryV2:
        if type(proposal) is not BrowserOpportunityProposal:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        if _field(proposal, "account_uuid") != account_id:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ACCOUNT_MISMATCH")
        if (
            _field(proposal, "source") != "SYNTHETIC_FIXTURE"
            or _field(proposal, "context_provenance") != "SYNTHETIC_FIXTURE"
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE")
        if (
            _field(proposal, "review_state") != "NEEDS_CONTEXT"
            or _field(proposal, "search_result_truncated") is not batch_truncated
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")

        raw_observations = _field(proposal, "conversation_observations")
        raw_reasons = _field(proposal, "reason_codes")
        if (
            type(raw_observations) is not tuple
            or len(cast(tuple[object, ...], raw_observations)) > _MAX_ACTIVE
            or type(raw_reasons) is not tuple
            or len(cast(tuple[object, ...], raw_reasons)) > len(_REASONS)
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        observation_data: list[dict[str, object]] = []
        for row in cast(tuple[object, ...], raw_observations):
            if type(row) is not ConversationObservationEvidence:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            observation_data.append(
                {
                    "target_url": _field(row, "target_permalink"),
                    "author_marker": _field(row, "author_marker"),
                    "author_identity_status": _field(row, "author_identity_status"),
                    "excerpt": _field(row, "excerpt"),
                    "position": _field(row, "position"),
                    "parent_url": _field(row, "parent_permalink"),
                    "is_root": _field(row, "is_root"),
                    "truncated": _field(row, "truncated"),
                }
            )
        raw_entry: dict[str, object] = {
            "id": str(uuid4()),
            "target_url": _field(proposal, "target_permalink"),
            "source": _field(proposal, "source"),
            "author_marker": _field(proposal, "author_marker"),
            "author_identity_status": _field(proposal, "author_identity_status"),
            "context": None,
            "context_provenance": _field(proposal, "context_provenance"),
            "context_completeness": "UNKNOWN",
            "source_evidence": {
                "proposal_result_version": 1,
                "query": query,
                "excerpt": _field(proposal, "excerpt"),
                "position": _field(proposal, "position"),
                "search_context_status": _field(proposal, "search_context_status"),
                "context_status": _field(proposal, "context_status"),
                "truncated": _field(proposal, "truncated"),
                "search_observation_truncated": _field(proposal, "search_observation_truncated"),
                "search_result_truncated": _field(proposal, "search_result_truncated"),
                "conversation_result_truncated": _field(proposal, "conversation_result_truncated"),
                "conversation_observations": observation_data,
                "reason_codes": list(cast(tuple[object, ...], raw_reasons)),
            },
            "created_at": created_at,
            "updated_at": created_at,
            "state": "NEEDS_CONTEXT",
            "skip_reason": None,
        }
        return self._decode_entry(raw_entry)

    def import_manual_urls(
        self,
        account_id: UUID,
        target_urls: Sequence[str],
    ) -> OpportunityQueueV2Summary:
        account = _validate_account_id(account_id)
        if not _is_sequence(target_urls) or not 1 <= len(target_urls) <= _MAX_ACTIVE:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_LIMIT_REACHED")
        normalized_targets: list[str] = []
        seen: set[str] = set()
        duplicates = 0
        for value in cast(Sequence[object], target_urls):
            target, _ = _normalize_target(value)
            if target in seen:
                duplicates += 1
                continue
            seen.add(target)
            normalized_targets.append(target)

        with self.account_lock(account):
            root = self._required_root()
            queue = self._read_unlocked(root, account)
            by_target = {entry.target_url: entry for entry in queue.opportunities}
            additions: list[OpportunityQueueEntryV2] = []
            created_at = _timestamp()
            for target in normalized_targets:
                previous = by_target.get(target)
                if previous is not None:
                    if previous.source != "MANUAL_URL":
                        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
                    duplicates += 1
                    continue
                entry = OpportunityQueueEntryV2(
                    id=str(uuid4()),
                    target_url=target,
                    source="MANUAL_URL",
                    author_marker=None,
                    author_identity_status="UNKNOWN",
                    context=None,
                    context_provenance="NONE",
                    context_completeness="UNKNOWN",
                    source_evidence=None,
                    created_at=created_at,
                    updated_at=created_at,
                    state="NEEDS_CONTEXT",
                    skip_reason=None,
                )
                additions.append(entry)
                by_target[target] = entry
            self._ensure_capacity(queue, additions)
            updated = OpportunityQueueV2(
                account_uuid=str(account),
                opportunities=(*queue.opportunities, *additions),
            )
            if additions:
                self._write_unlocked(root, updated)
            return self._summary(updated, len(additions), duplicates)

    def intake_synthetic_proposals(
        self,
        account_id: UUID,
        batch: BrowserOpportunityProposalBatch,
    ) -> OpportunityQueueV2Summary:
        account = _validate_account_id(account_id)
        if type(batch) is not BrowserOpportunityProposalBatch:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        if batch.account_uuid != account:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ACCOUNT_MISMATCH")
        if batch.source != "SYNTHETIC_FIXTURE":
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE")
        if (
            type(batch.result_version) is not int
            or batch.result_version != 1
            or type(batch.search_result_truncated) is not bool
            or type(batch.proposals) is not tuple
            or len(batch.proposals) > _MAX_ACTIVE
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        try:
            query = normalize_search_query(batch.query)
        except BrowserContractError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        if query != batch.query:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")

        created_at = _timestamp()
        candidates = tuple(
            self._proposal_entry(
                proposal,
                account_id=account,
                query=query,
                batch_truncated=batch.search_result_truncated,
                created_at=created_at,
            )
            for proposal in batch.proposals
        )
        unique_candidates: dict[str, OpportunityQueueEntryV2] = {}
        duplicates = 0
        for candidate in candidates:
            previous = unique_candidates.get(candidate.target_url)
            if previous is None:
                unique_candidates[candidate.target_url] = candidate
            elif _same_source_observation(previous, candidate):
                duplicates += 1
            else:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")

        with self.account_lock(account):
            root = self._required_root()
            queue = self._read_unlocked(root, account)
            by_target = {entry.target_url: entry for entry in queue.opportunities}
            additions: list[OpportunityQueueEntryV2] = []
            for target, candidate in unique_candidates.items():
                previous = by_target.get(target)
                if previous is not None:
                    if previous.source != "SYNTHETIC_FIXTURE":
                        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
                    if not _same_source_observation(previous, candidate):
                        raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
                    duplicates += 1
                    continue
                additions.append(candidate)
                by_target[target] = candidate
            self._ensure_capacity(queue, additions)
            updated = OpportunityQueueV2(
                account_uuid=str(account),
                opportunities=(*queue.opportunities, *additions),
            )
            if additions:
                self._write_unlocked(root, updated)
            return self._summary(updated, len(additions), duplicates)

    def set_context(
        self,
        account_id: UUID,
        opportunity_id: str,
        context: str,
        *,
        completeness: object,
        operator_confirmed_complete: bool = False,
    ) -> OpportunityQueueEntryV2:
        account = _validate_account_id(account_id)
        identifier = self._validate_id(opportunity_id)
        normalized = _normalize_context(context)
        raw_completeness: object = completeness
        if (
            raw_completeness not in ("UNKNOWN", "PARTIAL", "COMPLETE")
            or type(operator_confirmed_complete) is not bool
            or (completeness == "COMPLETE") is not operator_confirmed_complete
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONTEXT_INVALID")
        normalized_completeness = raw_completeness
        with self.account_lock(account):
            root = self._required_root()
            queue = self._read_unlocked(root, account)
            entries = list(queue.opportunities)
            index = next((i for i, item in enumerate(entries) if item.id == identifier), None)
            if index is None:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND")
            previous = entries[index]
            if previous.state == "SKIPPED":
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND")
            updated_entry = replace(
                previous,
                context=normalized,
                context_provenance="OPERATOR_SUPPLIED",
                context_completeness=normalized_completeness,
                updated_at=_timestamp(),
                state="READY" if operator_confirmed_complete else "NEEDS_CONTEXT",
            )
            entries[index] = updated_entry
            self._write_unlocked(
                root,
                OpportunityQueueV2(account_uuid=str(account), opportunities=tuple(entries)),
            )
            return updated_entry

    def skip(
        self,
        account_id: UUID,
        opportunity_id: str,
        *,
        reason: str | None = None,
    ) -> OpportunityQueueEntryV2:
        account = _validate_account_id(account_id)
        identifier = self._validate_id(opportunity_id)
        normalized_reason = _normalize_skip_reason(reason)
        with self.account_lock(account):
            root = self._required_root()
            queue = self._read_unlocked(root, account)
            entries = list(queue.opportunities)
            index = next((i for i, item in enumerate(entries) if item.id == identifier), None)
            if index is None:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND")
            updated_entry = replace(
                entries[index],
                state="SKIPPED",
                skip_reason=normalized_reason,
                updated_at=_timestamp(),
            )
            entries[index] = updated_entry
            self._write_unlocked(
                root,
                OpportunityQueueV2(account_uuid=str(account), opportunities=tuple(entries)),
            )
            return updated_entry

    def preview_v1_migration(self, account_id: UUID) -> V1MigrationPreview:
        """Return bounded metadata only; never migrates or exposes context text."""

        account = _validate_account_id(account_id)
        with self.account_lock(account):
            root = self._required_root()
            queue, source_raw = self._v1_snapshot_unlocked(root, account)
            target_exists = self._target_exists_unlocked(root, account)
            return V1MigrationPreview(
                account_uuid=str(account),
                source_exists=source_raw is not None,
                source_sha256=None
                if source_raw is None
                else hashlib.sha256(source_raw).hexdigest(),
                v1_total_count=len(queue.opportunities),
                v1_active_count=queue.active_count,
                v1_ready_count=sum(item.state == "READY" for item in queue.opportunities),
                v1_needs_context_count=sum(
                    item.state == "NEEDS_CONTEXT" for item in queue.opportunities
                ),
                v1_skipped_count=sum(item.state == "SKIPPED" for item in queue.opportunities),
                v2_target_exists=target_exists,
            )

    def apply_v1_migration(
        self,
        account_id: UUID,
        *,
        confirmation: object,
    ) -> V1MigrationResult:
        """Apply one reviewed migration only when the preview-bound token matches."""

        account = _validate_account_id(account_id)
        raw_confirmation: object = confirmation
        if not isinstance(raw_confirmation, str):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONFIRMATION_REQUIRED")
        with self.account_lock(account):
            root = self._required_root()
            v1_queue, source_raw = self._v1_snapshot_unlocked(root, account)
            if source_raw is None:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_MIGRATION_SOURCE_MISSING")
            source_hash = hashlib.sha256(source_raw).hexdigest()
            expected_confirmation = f"MIGRATE_V1_TO_V2:{account}:{source_hash}"
            prefix = f"MIGRATE_V1_TO_V2:{account}:"
            if not raw_confirmation.startswith(prefix):
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_CONFIRMATION_REQUIRED")
            if raw_confirmation != expected_confirmation:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_MIGRATION_SOURCE_CHANGED")
            if self._target_exists_unlocked(root, account):
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_MIGRATION_TARGET_EXISTS")

            migrated = OpportunityQueueV2(
                account_uuid=str(account),
                opportunities=tuple(_migrate_v1_entry(entry) for entry in v1_queue.opportunities),
            )
            self._ensure_capacity(OpportunityQueueV2(str(account), ()), migrated.opportunities)
            self._atomic_write_unlocked(root, migrated, must_be_absent=True)
            return V1MigrationResult(
                account_uuid=str(account),
                migrated_count=len(migrated.opportunities),
                active_count=migrated.active_count,
                source_sha256=source_hash,
            )

    def _validate_id(self, value: object) -> str:
        if not isinstance(value, str):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND")
        try:
            parsed = UUID(value)
        except ValueError, TypeError, AttributeError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND") from None
        if parsed.version != 4 or str(parsed) != value:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_ENTRY_NOT_FOUND")
        return value

    def _required_root(self) -> Path:
        try:
            root = self._configured_root.resolve(strict=True)
            if not root.is_dir():
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            return root
        except OpportunityQueueV2Error:
            raise
        except OSError, RuntimeError, ValueError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None

    def _directory(self, parent: Path, name: str, *, create: bool) -> Path | None:
        path = parent / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                path.mkdir(mode=0o700)
                info = path.lstat()
            except OSError:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        except OSError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        try:
            if path.resolve(strict=True) != path:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        except OpportunityQueueV2Error:
            raise
        except OSError, RuntimeError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        return path

    def _regular_file(
        self,
        path: Path,
        parent: Path,
        *,
        allow_missing: bool,
    ) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return False
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        except OSError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or _is_reparse_point(info)
            or info.st_size > _MAX_DOCUMENT_BYTES
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        try:
            if path.resolve(strict=True) != path or path.parent != parent:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        except OpportunityQueueV2Error:
            raise
        except OSError, RuntimeError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        return True

    def _queue_path(self, root: Path, account_id: UUID, *, create: bool) -> Path | None:
        namespace = self._directory(root, _NAMESPACE, create=create)
        if namespace is None:
            return None
        accounts = self._directory(namespace, "accounts", create=create)
        if accounts is None:
            return None
        return accounts / f"{account_id}.json"

    def _target_exists_unlocked(self, root: Path, account_id: UUID) -> bool:
        target = self._queue_path(root, account_id, create=False)
        if target is None:
            return False
        return self._regular_file(target, target.parent, allow_missing=True)

    def _read_unlocked(self, root: Path, account_id: UUID) -> OpportunityQueueV2:
        target = self._queue_path(root, account_id, create=False)
        if target is None or not self._regular_file(target, target.parent, allow_missing=True):
            return OpportunityQueueV2(str(account_id), ())
        raw = self._read_file(target)
        value = self._parse_json(raw)
        return self._decode_queue(value, account_id)

    def _read_file(self, path: Path) -> bytes:
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        return raw

    def _parse_json(self, raw: bytes) -> object:
        try:
            return json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None

    def _decode_queue(self, value: object, account_id: UUID) -> OpportunityQueueV2:
        if not isinstance(value, dict):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _TOP_LEVEL_KEYS:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        if type(data["version"]) is not int or data["version"] != _VERSION:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        if data["account_uuid"] != str(account_id):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        raw_entries = data["opportunities"]
        if not isinstance(raw_entries, list):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        values = cast(list[object], raw_entries)
        if len(values) > _MAX_RETAINED:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        entries: list[OpportunityQueueEntryV2] = []
        seen_ids: set[str] = set()
        seen_targets: set[str] = set()
        for raw_entry in values:
            entry = self._decode_entry(raw_entry)
            if entry.id in seen_ids or entry.target_url in seen_targets:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            seen_ids.add(entry.id)
            seen_targets.add(entry.target_url)
            entries.append(entry)
        queue = OpportunityQueueV2(str(account_id), tuple(entries))
        if queue.active_count > _MAX_ACTIVE:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        return queue

    def _decode_entry(self, value: object) -> OpportunityQueueEntryV2:
        if not isinstance(value, dict):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _ENTRY_KEYS:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        raw_id = data["id"]
        if not isinstance(raw_id, str):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        identifier = self._validate_id(raw_id)
        target, target_author = _canonical_target(data["target_url"])
        raw_source = data["source"]
        if not isinstance(raw_source, str) or raw_source not in {
            "MANUAL_URL",
            "SYNTHETIC_FIXTURE",
        }:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID_PROVENANCE")
        source = cast(QueueSourceV2, raw_source)
        raw_author = data["author_marker"]
        raw_identity = data["author_identity_status"]
        raw_evidence = data["source_evidence"]
        if source == "MANUAL_URL":
            if raw_author is not None or raw_identity != "UNKNOWN" or raw_evidence is not None:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            author_marker: str | None = None
            author_identity: AuthorIdentityStatusV2 = "UNKNOWN"
            evidence = None
        else:
            if not isinstance(raw_author, str) or raw_author.casefold() != target_author.casefold():
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
            if raw_identity != "UNVERIFIED" or not isinstance(raw_evidence, dict):
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            author_marker = raw_author
            author_identity = "UNVERIFIED"
            evidence = self._decode_evidence(
                cast(dict[str, object], raw_evidence), target, author_marker
            )

        raw_context = data["context"]
        context = None if raw_context is None else _normalize_context(raw_context)
        if context != raw_context:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        raw_provenance = data["context_provenance"]
        raw_completeness = data["context_completeness"]
        if (
            not isinstance(raw_provenance, str)
            or raw_provenance not in _CONTEXT_PROVENANCES
            or not isinstance(raw_completeness, str)
            or raw_completeness not in _CONTEXT_STATUSES
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        provenance = cast(ContextProvenanceV2, raw_provenance)
        completeness = cast(ContextCompletenessV2, raw_completeness)
        if context is None:
            if completeness != "UNKNOWN":
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            expected_empty_provenance = (
                "SYNTHETIC_FIXTURE" if source == "SYNTHETIC_FIXTURE" else "NONE"
            )
            if provenance != expected_empty_provenance:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        elif provenance != "OPERATOR_SUPPLIED":
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")

        raw_created = data["created_at"]
        raw_updated = data["updated_at"]
        created_time = _parse_timestamp(raw_created)
        updated_time = _parse_timestamp(raw_updated)
        if updated_time < created_time:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        raw_state = data["state"]
        raw_skip = data["skip_reason"]
        skip_reason = _normalize_skip_reason(raw_skip)
        if not isinstance(raw_state, str) or raw_state not in _STATES:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        state = cast(OpportunityStateV2, raw_state)
        if state == "READY" and (
            context is None or provenance != "OPERATOR_SUPPLIED" or completeness != "COMPLETE"
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        if state == "NEEDS_CONTEXT" and context is not None and completeness == "COMPLETE":
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        if state != "SKIPPED" and skip_reason is not None:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        return OpportunityQueueEntryV2(
            id=identifier,
            target_url=target,
            source=source,
            author_marker=author_marker,
            author_identity_status=author_identity,
            context=context,
            context_provenance=provenance,
            context_completeness=completeness,
            source_evidence=evidence,
            created_at=cast(str, raw_created),
            updated_at=cast(str, raw_updated),
            state=state,
            skip_reason=skip_reason,
        )

    def _decode_evidence(
        self,
        value: dict[str, object],
        target: str,
        author_marker: str,
    ) -> OpportunitySourceEvidenceV2:
        if frozenset(value) != _EVIDENCE_KEYS:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        raw_version = value["proposal_result_version"]
        query = value["query"]
        try:
            normalized_query = normalize_search_query(query)
        except BrowserContractError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
        raw_excerpt = value["excerpt"]
        raw_position = value["position"]
        raw_search_status = value["search_context_status"]
        raw_context_status = value["context_status"]
        raw_truncated = value["truncated"]
        raw_search_observation_truncated = value["search_observation_truncated"]
        raw_search_result_truncated = value["search_result_truncated"]
        raw_conversation_truncated = value["conversation_result_truncated"]
        raw_observations = value["conversation_observations"]
        raw_reasons = value["reason_codes"]
        if (
            type(raw_version) is not int
            or raw_version != 1
            or normalized_query != query
            or not _valid_excerpt(raw_excerpt)
            or type(raw_position) is not int
            or not 0 <= raw_position < _MAX_ACTIVE
            or raw_search_status != "UNKNOWN"
            or not isinstance(raw_context_status, str)
            or raw_context_status not in _CONTEXT_STATUSES
            or type(raw_truncated) is not bool
            or type(raw_search_observation_truncated) is not bool
            or type(raw_search_result_truncated) is not bool
            or (
                raw_conversation_truncated is not None
                and type(raw_conversation_truncated) is not bool
            )
            or not isinstance(raw_observations, list)
            or len(cast(list[object], raw_observations)) > _MAX_ACTIVE
            or not isinstance(raw_reasons, list)
            or len(cast(list[object], raw_reasons)) > len(_REASONS)
            or any(
                not isinstance(reason, str) or reason not in _REASONS
                for reason in cast(list[object], raw_reasons)
            )
            or len(set(cast(list[str], raw_reasons))) != len(cast(list[object], raw_reasons))
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")

        observations = tuple(
            self._decode_observation(row, target) for row in cast(list[object], raw_observations)
        )
        if len({row.target_url for row in observations}) != len(observations):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
        roots = tuple(row for row in observations if row.is_root)
        if observations:
            if len(roots) != 1 or roots[0].target_url != target:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
            if roots[0].author_marker.casefold() != author_marker.casefold():
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
            if raw_conversation_truncated is None:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        elif raw_conversation_truncated is not None or raw_context_status != "UNKNOWN":
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        by_target = {row.target_url: row for row in observations}
        for row in observations:
            if row.parent_url == row.target_url:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
            if (
                raw_context_status == "COMPLETE"
                and not row.is_root
                and (row.parent_url is None or row.parent_url not in by_target)
            ):
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
        _validate_parent_cycles(observations)
        if (
            observations
            and raw_context_status == "COMPLETE"
            and (raw_conversation_truncated or any(row.truncated for row in observations))
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
        combined_truncation = (
            raw_search_observation_truncated
            or raw_search_result_truncated
            or raw_conversation_truncated is True
            or any(row.truncated for row in observations)
        )
        if raw_truncated is not combined_truncation:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        return OpportunitySourceEvidenceV2(
            proposal_result_version=1,
            query=normalized_query,
            excerpt=cast(str | None, raw_excerpt),
            position=raw_position,
            search_context_status="UNKNOWN",
            context_status=cast(ContextCompletenessV2, raw_context_status),
            truncated=raw_truncated,
            search_observation_truncated=raw_search_observation_truncated,
            search_result_truncated=raw_search_result_truncated,
            conversation_result_truncated=raw_conversation_truncated,
            conversation_observations=observations,
            reason_codes=tuple(cast(list[str], raw_reasons)),
        )

    def _decode_observation(self, value: object, root_target: str) -> ConversationObservationV2:
        if not isinstance(value, dict):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _OBSERVATION_KEYS:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        target, author = _canonical_target(data["target_url"])
        marker = data["author_marker"]
        excerpt = data["excerpt"]
        position = data["position"]
        parent = data["parent_url"]
        is_root = data["is_root"]
        truncated = data["truncated"]
        if (
            not isinstance(marker, str)
            or marker.casefold() != author.casefold()
            or data["author_identity_status"] != "UNVERIFIED"
            or not _valid_excerpt(excerpt)
            or type(position) is not int
            or not 0 <= position < _MAX_ACTIVE
            or type(is_root) is not bool
            or type(truncated) is not bool
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        parent_url: str | None = None
        if parent is not None:
            parent_url, _ = _canonical_target(parent)
        expected_root = target == root_target
        if is_root is not expected_root or (is_root and (position != 0 or parent_url is not None)):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_PROPOSAL_CONFLICT")
        return ConversationObservationV2(
            target_url=target,
            author_marker=marker,
            author_identity_status="UNVERIFIED",
            excerpt=cast(str | None, excerpt),
            position=position,
            parent_url=parent_url,
            is_root=is_root,
            truncated=truncated,
        )

    def _write_unlocked(self, root: Path, queue: OpportunityQueueV2) -> None:
        self._atomic_write_unlocked(root, queue, must_be_absent=False)

    def _atomic_write_unlocked(
        self,
        root: Path,
        queue: OpportunityQueueV2,
        *,
        must_be_absent: bool,
    ) -> None:
        account_id = _validate_account_id(UUID(queue.account_uuid))
        if queue.account_uuid != str(account_id):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        document = self._encode_queue(queue)
        validated = self._decode_queue(self._parse_json(document), account_id)
        if validated != queue:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        target = self._queue_path(root, account_id, create=True)
        if target is None:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
        exists = self._regular_file(target, target.parent, allow_missing=True)
        if must_be_absent and exists:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_MIGRATION_TARGET_EXISTS")
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
            if not self._regular_file(temporary_path, target.parent, allow_missing=False):
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            staged = self._decode_queue(
                self._parse_json(self._read_file(temporary_path)), account_id
            )
            if staged != queue:
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID")
            if must_be_absent and self._regular_file(target, target.parent, allow_missing=True):
                raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_MIGRATION_TARGET_EXISTS")
            if must_be_absent:
                try:
                    # Same-directory hard-link creation atomically fails if target exists.
                    # Unsupported filesystems fail closed; migration never uses replace here.
                    os.link(temporary_path, target)
                except FileExistsError:
                    raise OpportunityQueueV2Error(
                        "OPPORTUNITY_QUEUE_V2_MIGRATION_TARGET_EXISTS"
                    ) from None
            else:
                os.replace(temporary_path, target)
        except OpportunityQueueV2Error:
            raise
        except OSError:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_INVALID") from None
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

    def _encode_queue(self, queue: OpportunityQueueV2) -> bytes:
        document = {
            "version": _VERSION,
            "account_uuid": queue.account_uuid,
            "opportunities": [_entry_to_json(entry) for entry in queue.opportunities],
        }
        raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_LIMIT_REACHED")
        return raw

    def _v1_snapshot_unlocked(
        self,
        root: Path,
        account_id: UUID,
    ) -> tuple[OpportunityQueue, bytes | None]:
        return _LockedV1QueueReader(root).snapshot(root, account_id)

    @staticmethod
    def _ensure_capacity(
        queue: OpportunityQueueV2,
        additions: Sequence[OpportunityQueueEntryV2],
    ) -> None:
        if (
            queue.active_count + sum(entry.state != "SKIPPED" for entry in additions) > _MAX_ACTIVE
            or len(queue.opportunities) + len(additions) > _MAX_RETAINED
        ):
            raise OpportunityQueueV2Error("OPPORTUNITY_QUEUE_V2_LIMIT_REACHED")

    @staticmethod
    def _summary(
        queue: OpportunityQueueV2,
        added_count: int,
        duplicate_count: int,
    ) -> OpportunityQueueV2Summary:
        return OpportunityQueueV2Summary(
            added_count=added_count,
            duplicate_count=duplicate_count,
            active_count=queue.active_count,
            total_count=len(queue.opportunities),
        )


def _same_source_observation(
    left: OpportunityQueueEntryV2,
    right: OpportunityQueueEntryV2,
) -> bool:
    return (
        left.source == right.source
        and left.target_url == right.target_url
        and left.author_marker == right.author_marker
        and left.author_identity_status == right.author_identity_status
        and left.source_evidence == right.source_evidence
    )


def _migrate_v1_entry(entry: OpportunityQueueEntry) -> OpportunityQueueEntryV2:
    return OpportunityQueueEntryV2(
        id=entry.id,
        target_url=entry.target_url,
        source="MANUAL_URL",
        author_marker=None,
        author_identity_status="UNKNOWN",
        context=entry.context,
        context_provenance=entry.context_provenance,
        context_completeness=entry.context_completeness,
        source_evidence=None,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        state=entry.state,
        skip_reason=entry.skip_reason,
    )
