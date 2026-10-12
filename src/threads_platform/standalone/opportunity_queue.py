"""Private, local-only queue for operator-captured Threads opportunities."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, TextIO, cast
from uuid import UUID, uuid4

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import normalize_manual_permalink
from threads_platform.standalone.accounts import (
    LocalAccount,
    LocalAccountStore,
)
from threads_platform.standalone.manual_opportunities import (
    ManualDraftReview,
    ManualOpportunity,
    ManualOpportunityError,
    ManualOpportunityIntake,
    build_manual_draft_review,
    load_manual_opportunities,
    load_operator_context,
    load_operator_draft,
    private_review_terminal_available,
    render_manual_draft_review,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeError, ManualOutcomeStore

OpportunityState = Literal["NEEDS_CONTEXT", "READY", "SKIPPED"]
ContextCompleteness = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
ContextProvenance = Literal["NONE", "OPERATOR_SUPPLIED"]

_NAMESPACE = "opportunity_queue_v1"
_VERSION = 1
_MAX_ACTIVE_OPPORTUNITIES = 20
_MAX_RETAINED_OPPORTUNITIES = 40
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_URL_BYTES = 2_048
_MAX_CONTEXT_CHARS = 4_000
_MAX_SKIP_REASON_CHARS = 1_000
_CONTEXT_COMPLETENESS = ("UNKNOWN", "PARTIAL", "COMPLETE")
_STATES = ("NEEDS_CONTEXT", "READY", "SKIPPED")
_TOP_LEVEL_KEYS = frozenset({"version", "account_uuid", "opportunities"})
_ENTRY_KEYS = frozenset(
    {
        "id",
        "target_url",
        "source",
        "context",
        "context_provenance",
        "context_completeness",
        "created_at",
        "updated_at",
        "state",
        "skip_reason",
    }
)


class OpportunityQueueError(Exception):
    """Safe queue failure that exposes a fixed code and no local data."""

    _CODES = frozenset(
        {
            "PRIVATE_REVIEW_TERMINAL_REQUIRED",
            "OPPORTUNITY_QUEUE_INVALID",
            "OPPORTUNITY_QUEUE_BUSY",
            "OPPORTUNITY_QUEUE_LIMIT_REACHED",
            "OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND",
            "OPPORTUNITY_QUEUE_CONTEXT_INVALID",
            "OPPORTUNITY_QUEUE_CONTEXT_REQUIRED",
            "OPPORTUNITY_QUEUE_SKIPPED",
            "OPPORTUNITY_QUEUE_DELETE_NOT_CONFIRMED",
            "OPPORTUNITY_QUEUE_HANDOFF_UNRESOLVED",
            "OPPORTUNITY_QUEUE_DELETE_INTERRUPTED",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported opportunity queue error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityQueueEntry:
    id: str
    target_url: str
    source: Literal["MANUAL_URL"]
    context: str | None
    context_provenance: ContextProvenance
    context_completeness: ContextCompleteness
    created_at: str
    updated_at: str
    state: OpportunityState
    skip_reason: str | None


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityQueue:
    account_uuid: str
    opportunities: tuple[OpportunityQueueEntry, ...]

    @property
    def active_count(self) -> int:
        return sum(entry.state != "SKIPPED" for entry in self.opportunities)


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityImportSummary:
    added_count: int
    duplicate_count: int
    active_count: int
    total_count: int


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _is_reparse_point(info: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_attribute)


def _normalize_context(value: object) -> str:
    if not isinstance(value, str):
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
    normalized = value.replace("\r\n", "\n")
    for character in normalized:
        category = unicodedata.category(character)
        if character == "\n":
            continue
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
        if category == "Zs" and character != " ":
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
    if "\r" in normalized:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
    normalized = normalized.strip()
    if not normalized or len(normalized) > _MAX_CONTEXT_CHARS:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
    return normalized


def _canonical_target(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_URL_BYTES:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    try:
        if len(value.encode("utf-8", errors="strict")) > _MAX_URL_BYTES:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        normalized = normalize_manual_permalink(value)
    except OpportunityQueueError:
        raise
    except BrowserContractError, UnicodeError, ValueError:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
    if normalized.thread_ref != value:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    return normalized.thread_ref


def _normalize_skip_reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_SKIP_REASON_CHARS:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in normalized):
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    return normalized


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
    return parsed


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_to_json(entry: OpportunityQueueEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "target_url": entry.target_url,
        "source": entry.source,
        "context": entry.context,
        "context_provenance": entry.context_provenance,
        "context_completeness": entry.context_completeness,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "state": entry.state,
        "skip_reason": entry.skip_reason,
    }


class OpportunityQueueStore:
    """Strict account-isolated JSON queue with atomic writes and process locks."""

    def __init__(self, data_root: Path) -> None:
        self._configured_root = Path(data_root).absolute()

    @contextmanager
    def account_lock(self, account_id: UUID) -> Generator[None]:
        if account_id.version != 4:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        root = self._required_root()
        try:
            with ManualOutcomeStore(root).account_lock(account_id):
                yield
        except ManualOutcomeError as error:
            code = (
                "OPPORTUNITY_QUEUE_BUSY"
                if error.code == "MANUAL_OUTCOME_BUSY"
                else "OPPORTUNITY_QUEUE_INVALID"
            )
            raise OpportunityQueueError(code) from None

    def list(self, account_id: UUID) -> OpportunityQueue:
        with self.account_lock(account_id):
            root = self._required_root()
            return self._read_unlocked(root, account_id)

    def get(self, account_id: UUID, opportunity_id: str) -> OpportunityQueueEntry:
        identifier = self._validate_opportunity_id(opportunity_id)
        queue = self.list(account_id)
        for entry in queue.opportunities:
            if entry.id == identifier:
                return entry
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND")

    def import_candidates(
        self,
        account_id: UUID,
        intake: ManualOpportunityIntake,
        *,
        context: str | None = None,
        context_completeness: ContextCompleteness = "UNKNOWN",
    ) -> OpportunityImportSummary:
        if account_id.version != 4 or not intake.candidates:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if context_completeness not in _CONTEXT_COMPLETENESS:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
        normalized_context = None if context is None else _normalize_context(context)
        if normalized_context is None and context_completeness != "UNKNOWN":
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")

        with self.account_lock(account_id):
            root = self._required_root()
            queue = self._read_unlocked(root, account_id)
            known_targets = {entry.target_url for entry in queue.opportunities}
            additions: list[str] = []
            duplicate_count = len(intake.duplicates)
            for candidate in intake.candidates:
                target_url = _canonical_target(candidate.target_url)
                if target_url in known_targets:
                    duplicate_count += 1
                    continue
                known_targets.add(target_url)
                additions.append(target_url)

            if queue.active_count + len(additions) > _MAX_ACTIVE_OPPORTUNITIES:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_LIMIT_REACHED")
            if len(queue.opportunities) + len(additions) > _MAX_RETAINED_OPPORTUNITIES:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_LIMIT_REACHED")

            if additions:
                created_at = _timestamp()
                provenance: ContextProvenance = (
                    "OPERATOR_SUPPLIED" if normalized_context is not None else "NONE"
                )
                state: OpportunityState = (
                    "READY"
                    if normalized_context is not None and context_completeness == "COMPLETE"
                    else "NEEDS_CONTEXT"
                )
                new_entries = tuple(
                    OpportunityQueueEntry(
                        id=str(uuid4()),
                        target_url=target_url,
                        source="MANUAL_URL",
                        context=normalized_context,
                        context_provenance=provenance,
                        context_completeness=(
                            context_completeness if normalized_context is not None else "UNKNOWN"
                        ),
                        created_at=created_at,
                        updated_at=created_at,
                        state=state,
                        skip_reason=None,
                    )
                    for target_url in additions
                )
                updated = OpportunityQueue(
                    account_uuid=str(account_id),
                    opportunities=(*queue.opportunities, *new_entries),
                )
                self._write_unlocked(root, updated)
                queue = updated

            return OpportunityImportSummary(
                added_count=len(additions),
                duplicate_count=duplicate_count,
                active_count=queue.active_count,
                total_count=len(queue.opportunities),
            )

    def select(
        self,
        account_id: UUID,
        opportunity_id: str,
        *,
        context: str | None = None,
        update_context: bool = False,
        context_completeness: ContextCompleteness | None = None,
    ) -> OpportunityQueueEntry:
        identifier = self._validate_opportunity_id(opportunity_id)
        if context_completeness is not None and context_completeness not in _CONTEXT_COMPLETENESS:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
        if update_context:
            normalized_context = _normalize_context(context)
        elif context is not None:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_INVALID")
        else:
            normalized_context = None

        with self.account_lock(account_id):
            root = self._required_root()
            queue = self._read_unlocked(root, account_id)
            entries = list(queue.opportunities)
            selected_index = next(
                (index for index, entry in enumerate(entries) if entry.id == identifier),
                None,
            )
            if selected_index is None:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND")
            entry = entries[selected_index]
            if entry.state == "SKIPPED":
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_SKIPPED")

            if update_context or context_completeness is not None:
                next_context = normalized_context if update_context else entry.context
                if next_context is None:
                    raise OpportunityQueueError("OPPORTUNITY_QUEUE_CONTEXT_REQUIRED")
                next_completeness: ContextCompleteness = (
                    context_completeness
                    if context_completeness is not None
                    else "UNKNOWN"
                    if update_context
                    else entry.context_completeness
                )
                updated_entry = replace(
                    entry,
                    context=next_context,
                    context_provenance="OPERATOR_SUPPLIED",
                    context_completeness=next_completeness,
                    updated_at=_timestamp(),
                    state="READY" if next_completeness == "COMPLETE" else "NEEDS_CONTEXT",
                )
                entries[selected_index] = updated_entry
                self._write_unlocked(
                    root,
                    OpportunityQueue(account_uuid=str(account_id), opportunities=tuple(entries)),
                )
                return updated_entry
            return entry

    def skip(
        self,
        account_id: UUID,
        opportunity_id: str,
        *,
        reason: str | None = None,
    ) -> OpportunityQueueEntry:
        identifier = self._validate_opportunity_id(opportunity_id)
        normalized_reason = _normalize_skip_reason(reason)
        with self.account_lock(account_id):
            root = self._required_root()
            queue = self._read_unlocked(root, account_id)
            entries = list(queue.opportunities)
            selected_index = next(
                (index for index, entry in enumerate(entries) if entry.id == identifier),
                None,
            )
            if selected_index is None:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND")
            updated_entry = replace(
                entries[selected_index],
                state="SKIPPED",
                skip_reason=normalized_reason,
                updated_at=_timestamp(),
            )
            entries[selected_index] = updated_entry
            self._write_unlocked(
                root,
                OpportunityQueue(account_uuid=str(account_id), opportunities=tuple(entries)),
            )
            return updated_entry

    def remove(self, account_id: UUID, opportunity_id: str) -> OpportunityQueueEntry:
        identifier = self._validate_opportunity_id(opportunity_id)
        with self.account_lock(account_id):
            root = self._required_root()
            queue = self._read_unlocked(root, account_id)
            removed = next(
                (entry for entry in queue.opportunities if entry.id == identifier),
                None,
            )
            if removed is None:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND")
            outcome = ManualOutcomeStore(root).read(account_id, removed.target_url)
            if outcome is not None and outcome.latest.status == "HANDOFF_UNRESOLVED":
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_HANDOFF_UNRESOLVED")
            remaining = tuple(entry for entry in queue.opportunities if entry.id != identifier)
            if remaining:
                self._write_unlocked(
                    root,
                    OpportunityQueue(account_uuid=str(account_id), opportunities=remaining),
                )
            else:
                target = self._queue_path(root, account_id, create=False)
                if target is not None:
                    self._regular_file(target, target.parent, allow_missing=False)
                    try:
                        target.unlink()
                    except OSError:
                        raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
            return removed

    @staticmethod
    def _validate_opportunity_id(value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError, TypeError, AttributeError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND") from None
        if parsed.version != 4 or str(parsed) != value:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_ENTRY_NOT_FOUND")
        return value

    def _required_root(self) -> Path:
        try:
            root = self._configured_root.resolve(strict=True)
            if not root.is_dir():
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
            return root
        except OpportunityQueueError:
            raise
        except OSError, RuntimeError, ValueError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None

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
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        except OSError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        try:
            if path.resolve(strict=True) != path:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        except OpportunityQueueError:
            raise
        except OSError, RuntimeError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        return path

    def _regular_file(
        self,
        path: Path,
        parent: Path,
        *,
        allow_missing: bool,
        max_bytes: int = _MAX_DOCUMENT_BYTES,
    ) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return False
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        except OSError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info) or info.st_size > max_bytes:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        try:
            if path.resolve(strict=True) != path or path.parent != parent:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        except OpportunityQueueError:
            raise
        except OSError, RuntimeError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        return True

    def _queue_path(self, root: Path, account_id: UUID, *, create: bool) -> Path | None:
        namespace = self._directory(root, _NAMESPACE, create=create)
        if namespace is None:
            return None
        accounts_directory = self._directory(namespace, "accounts", create=create)
        if accounts_directory is None:
            return None
        return accounts_directory / f"{account_id}.json"

    def _read_unlocked(self, root: Path, account_id: UUID) -> OpportunityQueue:
        target = self._queue_path(root, account_id, create=False)
        if target is None:
            return OpportunityQueue(account_uuid=str(account_id), opportunities=())
        if not self._regular_file(target, target.parent, allow_missing=True):
            return OpportunityQueue(account_uuid=str(account_id), opportunities=())
        try:
            with target.open("rb") as source:
                raw = source.read(_MAX_DOCUMENT_BYTES + 1)
            if len(raw) > _MAX_DOCUMENT_BYTES:
                raise ValueError
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
        return self._decode_queue(value, account_id)

    def _decode_queue(self, value: object, account_id: UUID) -> OpportunityQueue:
        if not isinstance(value, dict):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _TOP_LEVEL_KEYS:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if type(data["version"]) is not int or data["version"] != _VERSION:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if data["account_uuid"] != str(account_id):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        raw_entries = data["opportunities"]
        if not isinstance(raw_entries, list):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        entry_values = cast(list[object], raw_entries)
        if len(entry_values) > _MAX_RETAINED_OPPORTUNITIES:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")

        entries: list[OpportunityQueueEntry] = []
        seen_ids: set[str] = set()
        seen_targets: set[str] = set()
        for raw_entry in entry_values:
            entry = self._decode_entry(raw_entry)
            if entry.id in seen_ids or entry.target_url in seen_targets:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
            seen_ids.add(entry.id)
            seen_targets.add(entry.target_url)
            entries.append(entry)
        queue = OpportunityQueue(str(account_id), tuple(entries))
        if queue.active_count > _MAX_ACTIVE_OPPORTUNITIES:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        return queue

    def _decode_entry(self, value: object) -> OpportunityQueueEntry:
        if not isinstance(value, dict):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        data = cast(dict[str, object], value)
        if frozenset(data) != _ENTRY_KEYS:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        raw_id = data["id"]
        raw_target = data["target_url"]
        if not isinstance(raw_id, str) or not isinstance(raw_target, str):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        identifier = self._validate_opportunity_id(raw_id)
        _canonical_target(raw_target)
        if data["source"] != "MANUAL_URL":
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")

        raw_context = data["context"]
        if raw_context is None:
            context = None
        else:
            context = _normalize_context(raw_context)
            if context != raw_context:
                raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        raw_provenance = data["context_provenance"]
        raw_completeness = data["context_completeness"]
        if not isinstance(raw_provenance, str) or raw_provenance not in {
            "NONE",
            "OPERATOR_SUPPLIED",
        }:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if not isinstance(raw_completeness, str) or raw_completeness not in _CONTEXT_COMPLETENESS:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if (context is None) != (raw_provenance == "NONE"):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if context is None and raw_completeness != "UNKNOWN":
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")

        created_at = data["created_at"]
        updated_at = data["updated_at"]
        created_time = _parse_timestamp(created_at)
        updated_time = _parse_timestamp(updated_at)
        if updated_time < created_time:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        raw_state = data["state"]
        if not isinstance(raw_state, str) or raw_state not in _STATES:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        state = raw_state
        raw_skip_reason = data["skip_reason"]
        skip_reason = _normalize_skip_reason(raw_skip_reason)
        if state == "READY" and (context is None or raw_completeness != "COMPLETE"):
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if state == "NEEDS_CONTEXT" and raw_completeness == "COMPLETE":
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        if state != "SKIPPED" and skip_reason is not None:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")

        return OpportunityQueueEntry(
            id=identifier,
            target_url=raw_target,
            source="MANUAL_URL",
            context=context,
            context_provenance=cast(ContextProvenance, raw_provenance),
            context_completeness=raw_completeness,
            created_at=cast(str, created_at),
            updated_at=cast(str, updated_at),
            state=state,
            skip_reason=skip_reason,
        )

    def _write_unlocked(self, root: Path, queue: OpportunityQueue) -> None:
        target = self._queue_path(root, UUID(queue.account_uuid), create=True)
        if target is None:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID")
        self._regular_file(target, target.parent, allow_missing=True)
        document = (
            json.dumps(
                {
                    "version": _VERSION,
                    "account_uuid": queue.account_uuid,
                    "opportunities": [_entry_to_json(entry) for entry in queue.opportunities],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        raw = document.encode("utf-8")
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_LIMIT_REACHED")
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
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
        except OSError:
            raise OpportunityQueueError("OPPORTUNITY_QUEUE_INVALID") from None
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


def _private_streams(
    stdin: TextIO | None,
    stdout: TextIO | None,
    stderr: TextIO | None,
    environment: Mapping[str, str] | None,
) -> tuple[TextIO, TextIO, TextIO]:
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
        raise OpportunityQueueError("PRIVATE_REVIEW_TERMINAL_REQUIRED")
    return private_stdin, private_stdout, private_stderr


def _account(root: Path, alias: str) -> LocalAccount:
    return LocalAccountStore(root).get(alias)


def _render_queue(account: LocalAccount, queue: OpportunityQueue) -> str:
    lines = [
        "OPPORTUNITY QUEUE — PRIVATE LOCAL TERMINAL",
        f"account alias: {account.alias}",
        f"account UUID: {account.id}",
        (
            f"active={queue.active_count}/{_MAX_ACTIVE_OPPORTUNITIES} "
            f"retained={len(queue.opportunities)}/{_MAX_RETAINED_OPPORTUNITIES}"
        ),
    ]
    if not queue.opportunities:
        lines.append("<empty>")
    for position, entry in enumerate(queue.opportunities, start=1):
        context = "<not supplied>" if entry.context is None else json.dumps(entry.context)
        reason = "-" if entry.skip_reason is None else json.dumps(entry.skip_reason)
        lines.append(
            f"{position}. id={entry.id} state={entry.state} source={entry.source} "
            f"target={entry.target_url} context_provenance={entry.context_provenance} "
            f"context_completeness={entry.context_completeness} context={context} "
            f"skip_reason={reason} created_at={entry.created_at} updated_at={entry.updated_at}"
        )
    return "\n".join(lines) + "\n"


def run_opportunity_import_cli(
    alias: str,
    targets_file: Path,
    context_file: Path | None,
    context_completeness: ContextCompleteness | None,
    *,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    _private_stdin, _private_stdout, private_stderr = _private_streams(
        stdin, stdout, stderr, environment
    )
    root = Path(data_root_factory())
    account = _account(root, alias)
    intake = load_manual_opportunities(targets_file)
    context = load_operator_context(context_file)
    completeness = "UNKNOWN" if context_completeness is None else context_completeness
    queue_store = OpportunityQueueStore(root)
    summary = queue_store.import_candidates(
        account.id,
        intake,
        context=context,
        context_completeness=completeness,
    )
    private_stderr.write(
        "OPPORTUNITY QUEUE IMPORT — PRIVATE LOCAL TERMINAL\n"
        f"account alias: {account.alias}\n"
        f"account UUID: {account.id}\n"
        f"added={summary.added_count} duplicates={summary.duplicate_count} "
        f"active={summary.active_count}/{_MAX_ACTIVE_OPPORTUNITIES} "
        f"retained={summary.total_count}/{_MAX_RETAINED_OPPORTUNITIES}\n"
    )
    private_stderr.flush()
    return 0


def run_opportunity_list_cli(
    alias: str,
    *,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    _private_stdin, _private_stdout, private_stderr = _private_streams(
        stdin, stdout, stderr, environment
    )
    root = Path(data_root_factory())
    account = _account(root, alias)
    queue = OpportunityQueueStore(root).list(account.id)
    private_stderr.write(_render_queue(account, queue))
    private_stderr.flush()
    return 0


def run_opportunity_select_cli(
    alias: str,
    opportunity_id: str,
    *,
    data_root_factory: Callable[[], Path],
    targets_confirmed: str | None = None,
    draft_file: Path | None = None,
    context_file: Path | None = None,
    context_completeness: ContextCompleteness | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    _private_stdin, _private_stdout, private_stderr = _private_streams(
        stdin, stdout, stderr, environment
    )
    root = Path(data_root_factory())
    account = _account(root, alias)
    queue_store = OpportunityQueueStore(root)
    current_entry = queue_store.get(account.id, opportunity_id)
    if targets_confirmed is not None and targets_confirmed != current_entry.target_url:
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH")
    if draft_file is not None and targets_confirmed is None:
        raise ManualOpportunityError("MANUAL_TARGET_MISMATCH")
    draft = None if draft_file is None else load_operator_draft(draft_file)
    context_update = load_operator_context(context_file)
    entry = queue_store.select(
        account.id,
        opportunity_id,
        context=context_update,
        update_context=context_file is not None,
        context_completeness=context_completeness,
    )
    if draft_file is None:
        private_stderr.write(
            "OPPORTUNITY SELECTED — PRIVATE LOCAL TERMINAL\n"
            f"account alias: {account.alias}\n"
            f"account UUID: {account.id}\n"
            f"opportunity id: {entry.id}\n"
            f"state: {entry.state}\n"
            f"source: {entry.source}\n"
            f"exact target: {entry.target_url}\n"
            f"context provenance: {entry.context_provenance}\n"
            f"context completeness: {entry.context_completeness}\n"
            "operator context: "
            + ("<not supplied>\n" if entry.context is None else json.dumps(entry.context) + "\n")
        )
        private_stderr.flush()
        return 0

    assert draft is not None
    normalized = normalize_manual_permalink(entry.target_url)
    opportunity = ManualOpportunity(
        input_position=1,
        source_line=1,
        target_url=entry.target_url,
        author_username=normalized.author_username,
    )
    intake = ManualOpportunityIntake(candidates=(opportunity,), duplicates=())
    review: ManualDraftReview = build_manual_draft_review(
        account,
        opportunity,
        draft,
        entry.context,
        context_completeness=entry.context_completeness,
    )
    private_stderr.write(
        f"queued opportunity id: {entry.id}\n"
        + render_manual_draft_review(intake, review, queue_persistence=True)
    )
    private_stderr.flush()
    return 0


def run_opportunity_skip_cli(
    alias: str,
    opportunity_id: str,
    reason_file: Path | None,
    *,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    _private_stdin, _private_stdout, private_stderr = _private_streams(
        stdin, stdout, stderr, environment
    )
    root = Path(data_root_factory())
    account = _account(root, alias)
    reason = load_operator_context(reason_file)
    entry = OpportunityQueueStore(root).skip(account.id, opportunity_id, reason=reason)
    private_stderr.write(
        f"opportunity skipped id={entry.id} state={entry.state} account={account.alias}\n"
    )
    private_stderr.flush()
    return 0


def run_opportunity_remove_cli(
    alias: str,
    opportunity_id: str,
    *,
    data_root_factory: Callable[[], Path],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    private_stdin, _private_stdout, private_stderr = _private_streams(
        stdin, stdout, stderr, environment
    )
    root = Path(data_root_factory())
    account = _account(root, alias)
    queue_store = OpportunityQueueStore(root)
    entry = queue_store.get(account.id, opportunity_id)
    private_stderr.write(
        "OPPORTUNITY REMOVAL — PRIVATE LOCAL TERMINAL\n"
        f"account alias: {account.alias}\n"
        f"opportunity id: {entry.id}\n"
        f"exact target: {entry.target_url}\n"
        "Only this local queue entry will be removed. Existing S4 outcome history is retained.\n"
        "Type DELETE and press Enter to confirm: "
    )
    private_stderr.flush()
    try:
        response = private_stdin.readline(16)
    except KeyboardInterrupt:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_DELETE_INTERRUPTED") from None
    except EOFError, OSError:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_DELETE_NOT_CONFIRMED") from None
    if response not in {"DELETE\n", "DELETE\r\n"}:
        raise OpportunityQueueError("OPPORTUNITY_QUEUE_DELETE_NOT_CONFIRMED")

    removed = queue_store.remove(account.id, opportunity_id)
    private_stderr.write(f"opportunity removed id={removed.id} account={account.alias}\n")
    private_stderr.flush()
    return 0
