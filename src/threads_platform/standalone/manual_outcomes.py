"""Private local audit records for operator-reported recruitment outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, TypeGuard, cast
from uuid import UUID, uuid4

from threads_platform.browser.contracts import BrowserContractError
from threads_platform.browser.read_adapters import normalize_manual_permalink
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning

ManualOutcomeValue = Literal[
    "ABORTED_NOT_SENT",
    "OPERATOR_REPORTED_SENT_UNVERIFIED",
    "AMBIGUOUS",
]
ManualOutcomeStatus = Literal[
    "HANDOFF_UNRESOLVED",
    "ABORTED_NOT_SENT",
    "OPERATOR_REPORTED_SENT_UNVERIFIED",
    "AMBIGUOUS",
]
ManualOutcomeSource = Literal["SOFTWARE_HANDOFF", "OPERATOR", "LEGACY_OPERATOR"]
_OUTCOMES = frozenset({"ABORTED_NOT_SENT", "OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"})
_BLOCKING_OUTCOMES = frozenset({"OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"})
_PENDING_STATUS = "HANDOFF_UNRESOLVED"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_DOCUMENT_BYTES = 16 * 1024
_MAX_EVENTS = 16
_MAX_ACCOUNT_RECORDS = 1_024
_NAMESPACE = "manual_recruitment_v1"
_DOCUMENT_KEYS = frozenset({"version", "account_uuid", "target_fingerprint", "events"})
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "attempt_id",
        "draft_sha256",
        "recorded_at",
        "status",
        "source",
        "verification",
    }
)
_LEGACY_EVENT_KEYS = frozenset(
    {"event_id", "draft_sha256", "recorded_at", "operator_declaration", "verification"}
)
_LOCK_LEASE_AUTHORITY = object()


class ManualOutcomeError(Exception):
    """Safe error containing a fixed local-journal code only."""

    _CODES = frozenset(
        {
            "MANUAL_OUTCOME_INVALID",
            "MANUAL_OUTCOME_VERSION_UNSUPPORTED",
            "MANUAL_OUTCOME_BUSY",
            "MANUAL_OUTCOME_BLOCKED",
            "MANUAL_OUTCOME_NO_PENDING",
            "MANUAL_OUTCOME_ATTEMPT_INVALID",
            "MANUAL_OUTCOME_ATTEMPT_MISMATCH",
            "MANUAL_OUTCOME_APPROVAL_MISMATCH",
            "MANUAL_OUTCOME_LIMIT_REACHED",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported manual outcome error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ManualOutcomeEvent:
    event_id: str
    attempt_id: str
    draft_sha256: str
    recorded_at: str
    status: ManualOutcomeStatus
    source: ManualOutcomeSource
    verification: Literal["UNVERIFIED"]

    @property
    def operator_declaration(self) -> ManualOutcomeValue | None:
        if self.status == _PENDING_STATUS:
            return None
        return self.status


@dataclass(frozen=True, slots=True, repr=False)
class ManualOutcomeRecord:
    account_uuid: str
    target_fingerprint: str
    events: tuple[ManualOutcomeEvent, ...]

    @property
    def latest(self) -> ManualOutcomeEvent:
        return self.events[-1]

    @property
    def blocks_handoff(self) -> bool:
        return self.latest.status == _PENDING_STATUS or self.latest.status in _BLOCKING_OUTCOMES


class ManualOutcomeLockLease:
    """Opaque proof that the shared account lock is active for one data root."""

    __slots__ = ("_account_id", "_active", "_authority", "_lock", "_lock_path", "_root")

    def __init__(
        self,
        *,
        authority: object,
        account_id: UUID,
        root: Path,
        lock_path: Path,
        lock: FilesystemProcessLock,
    ) -> None:
        if authority is not _LOCK_LEASE_AUTHORITY:
            raise ValueError("invalid account lock lease")
        self._account_id = account_id
        self._active = True
        self._authority = authority
        self._lock = lock
        self._lock_path = lock_path
        self._root = root

    def assert_active(self, account_id: UUID, root: Path) -> None:
        expected_path = root / _NAMESPACE / "locks" / f"{account_id}.lock"
        if (
            type(self) is not ManualOutcomeLockLease
            or type(account_id) is not UUID
            or account_id.version != 4
            or self._authority is not _LOCK_LEASE_AUTHORITY
            or not self._active
            or not self._lock.held
            or self._account_id != account_id
            or self._root != root
            or self._lock_path != expected_path
        ):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")

    def deactivate(self) -> None:
        self._active = False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _is_outcome(value: object) -> bool:
    return isinstance(value, str) and value in _OUTCOMES


def _is_outcome_status(value: str) -> TypeGuard[ManualOutcomeStatus]:
    return value in _OUTCOMES or value == _PENDING_STATUS


def _is_event_source(value: str) -> TypeGuard[ManualOutcomeSource]:
    return value in {"SOFTWARE_HANDOFF", "OPERATOR", "LEGACY_OPERATOR"}


def _same_outcome(status: str, outcome: object) -> bool:
    return status == outcome


def _is_reparse_point(info: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_attribute)


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


class ManualOutcomeStore:
    """Strict, bounded journal in a namespace separate from all existing v1 state."""

    def __init__(self, data_root: Path) -> None:
        self._configured_root = Path(data_root).absolute()

    @staticmethod
    def target_fingerprint(account_id: UUID, target_url: str) -> str:
        if account_id.version != 4:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        try:
            normalized = normalize_manual_permalink(target_url)
        except BrowserContractError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        if normalized.thread_ref != target_url:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        identity = json.dumps(
            [str(account_id), target_url], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def read(self, account_id: UUID, target_url: str) -> ManualOutcomeRecord | None:
        fingerprint = self.target_fingerprint(account_id, target_url)
        return self._read_fingerprint(self._existing_root(), account_id, fingerprint)

    def read_under_account_lock(
        self,
        account_id: UUID,
        target_url: str,
        *,
        lock_lease: ManualOutcomeLockLease,
    ) -> ManualOutcomeRecord | None:
        if type(account_id) is not UUID or account_id.version != 4:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        fingerprint = self.target_fingerprint(account_id, target_url)
        root = self._required_root()
        self._assert_lock_lease(lock_lease, account_id, root)
        return self._read_fingerprint(root, account_id, fingerprint)

    def read_account_under_account_lock(
        self,
        account_id: UUID,
        *,
        lock_lease: ManualOutcomeLockLease,
    ) -> tuple[ManualOutcomeRecord, ...]:
        """Load every bounded S4 record and event history for one account."""

        if type(account_id) is not UUID or account_id.version != 4:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        root = self._required_root()
        self._assert_lock_lease(lock_lease, account_id, root)
        namespace = self._directory(root, _NAMESPACE, create=False)
        if namespace is None:
            return ()
        records_directory = self._directory(namespace, "records", create=False)
        if records_directory is None:
            return ()
        account_directory = self._directory(records_directory, str(account_id), create=False)
        if account_directory is None:
            return ()

        records: list[ManualOutcomeRecord] = []
        try:
            for path in account_directory.iterdir():
                if len(records) >= _MAX_ACCOUNT_RECORDS:
                    raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")
                if path.suffix != ".json" or _SHA256.fullmatch(path.stem) is None:
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
                if not self._regular_file(path, account_directory, allow_missing=False):
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
                record = self._read_fingerprint(root, account_id, path.stem)
                if record is None:
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
                records.append(record)
        except ManualOutcomeError:
            raise
        except OSError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        records.sort(key=lambda item: item.target_fingerprint)
        return tuple(records)

    @staticmethod
    def _assert_lock_lease(
        lock_lease: ManualOutcomeLockLease,
        account_id: UUID,
        root: Path,
    ) -> None:
        if type(lock_lease) is not ManualOutcomeLockLease:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        lock_lease.assert_active(account_id, root)

    def _read_fingerprint(
        self,
        root: Path | None,
        account_id: UUID,
        fingerprint: str,
    ) -> ManualOutcomeRecord | None:
        if root is None:
            return None
        namespace = self._directory(root, _NAMESPACE, create=False)
        if namespace is None:
            return None
        records = self._directory(namespace, "records", create=False)
        if records is None:
            return None
        account_dir = self._directory(records, str(account_id), create=False)
        if account_dir is None:
            return None
        target = account_dir / f"{fingerprint}.json"
        if not self._regular_file(target, account_dir, allow_missing=True):
            return None
        try:
            with target.open("rb") as source:
                raw = source.read(_MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        return self._decode(value, account_id, fingerprint)

    @contextmanager
    def account_lock(self, account_id: UUID) -> Generator[None]:
        with self._locked_account(account_id):
            yield

    @contextmanager
    def account_lock_lease(
        self,
        account_id: UUID,
    ) -> Generator[ManualOutcomeLockLease]:
        if type(account_id) is not UUID or account_id.version != 4:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        with self._locked_account(account_id) as (root, lock, lock_path):
            lease = ManualOutcomeLockLease(
                authority=_LOCK_LEASE_AUTHORITY,
                account_id=account_id,
                root=root,
                lock_path=lock_path,
                lock=lock,
            )
            try:
                yield lease
            finally:
                lease.deactivate()

    @contextmanager
    def _locked_account(
        self,
        account_id: UUID,
    ) -> Generator[tuple[Path, FilesystemProcessLock, Path]]:
        if account_id.version != 4:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        root = self._required_root()
        self._ensure_outside_checkout(root / _NAMESPACE)
        namespace = self._directory(root, _NAMESPACE, create=True)
        assert namespace is not None
        lock_directory = self._directory(namespace, "locks", create=True)
        assert lock_directory is not None
        lock_path = lock_directory / f"{account_id}.lock"
        self._regular_file(lock_path, lock_directory, allow_missing=True, max_bytes=1)
        lock = FilesystemProcessLock(lock_path)
        try:
            lock.acquire()
        except ProcessAlreadyRunning:
            raise ManualOutcomeError("MANUAL_OUTCOME_BUSY") from None
        except OSError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        try:
            self._regular_file(lock_path, lock_directory, allow_missing=False, max_bytes=1)
            yield root, lock, lock_path
        finally:
            if lock.held:
                try:
                    lock.release()
                except OSError:
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None

    def reserve_approved_handoff_under_account_lock(
        self,
        account_id: UUID,
        target_url: str,
        draft_sha256: str,
        attempt_id: str,
        recorded_at: datetime,
        expected_record: ManualOutcomeRecord | None,
        expected_account_records: tuple[ManualOutcomeRecord, ...],
        *,
        lock_lease: ManualOutcomeLockLease,
    ) -> ManualOutcomeEvent:
        """Append the one approved S4 v1 reservation without reacquiring the lock."""

        if type(account_id) is not UUID or account_id.version != 4:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        fingerprint = self.target_fingerprint(account_id, target_url)
        if (
            _SHA256.fullmatch(draft_sha256) is None
            or not self._valid_attempt_id(attempt_id)
            or type(recorded_at) is not datetime
            or recorded_at.tzinfo is None
            or recorded_at.utcoffset() != UTC.utcoffset(recorded_at)
        ):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        if (
            (expected_record is not None and type(expected_record) is not ManualOutcomeRecord)
            or type(expected_account_records) is not tuple
            or any(type(record) is not ManualOutcomeRecord for record in expected_account_records)
        ):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")

        root = self._required_root()
        self._assert_lock_lease(lock_lease, account_id, root)
        current_records = self.read_account_under_account_lock(
            account_id,
            lock_lease=lock_lease,
        )
        if current_records != expected_account_records:
            raise ManualOutcomeError("MANUAL_OUTCOME_APPROVAL_MISMATCH")
        current = next(
            (record for record in current_records if record.target_fingerprint == fingerprint),
            None,
        )
        if current != expected_record:
            raise ManualOutcomeError("MANUAL_OUTCOME_APPROVAL_MISMATCH")
        if any(
            attempt_id in {event.event_id, event.attempt_id}
            for record in current_records
            for event in record.events
        ):
            raise ManualOutcomeError("MANUAL_OUTCOME_ATTEMPT_INVALID")
        if current is None and len(current_records) >= _MAX_ACCOUNT_RECORDS:
            raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")
        if current is not None:
            if current.blocks_handoff or current.latest.status != "ABORTED_NOT_SENT":
                raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
            if len(current.events) > _MAX_EVENTS - 2:
                raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")
            events = current.events
        else:
            events = ()

        event = ManualOutcomeEvent(
            event_id=attempt_id,
            attempt_id=attempt_id,
            draft_sha256=draft_sha256,
            recorded_at=recorded_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            status="HANDOFF_UNRESOLVED",
            source="SOFTWARE_HANDOFF",
            verification="UNVERIFIED",
        )
        record = ManualOutcomeRecord(
            account_uuid=str(account_id),
            target_fingerprint=fingerprint,
            events=(*events, event),
        )
        self._write(record, lock_lease=lock_lease)
        return event

    def begin_handoff(
        self,
        account_id: UUID,
        target_url: str,
        draft_sha256: str,
    ) -> ManualOutcomeEvent:
        fingerprint = self.target_fingerprint(account_id, target_url)
        if _SHA256.fullmatch(draft_sha256) is None:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")

        with self.account_lock(account_id):
            existing = self.read(account_id, target_url)
            if existing is not None:
                if existing.blocks_handoff:
                    raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
                if existing.latest.status != "ABORTED_NOT_SENT":
                    raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
                # Reserve room for the matching operator resolution so a bounded journal
                # cannot strand a newly opened browser session without room to attest it.
                if len(existing.events) > _MAX_EVENTS - 2:
                    raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")
                events = existing.events
            else:
                events = ()

            event = self._new_event(
                draft_sha256=draft_sha256,
                status="HANDOFF_UNRESOLVED",
                source="SOFTWARE_HANDOFF",
            )
            record = ManualOutcomeRecord(
                account_uuid=str(account_id),
                target_fingerprint=fingerprint,
                events=(*events, event),
            )
            self._write(record)
            return event

    def resolve_handoff(
        self,
        account_id: UUID,
        target_url: str,
        draft_sha256: str,
        attempt_id: str,
        outcome: ManualOutcomeValue,
    ) -> tuple[ManualOutcomeEvent, bool]:
        fingerprint = self.target_fingerprint(account_id, target_url)
        if _SHA256.fullmatch(draft_sha256) is None or not _is_outcome(outcome):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        if not self._valid_attempt_id(attempt_id):
            raise ManualOutcomeError("MANUAL_OUTCOME_ATTEMPT_INVALID")

        with self.account_lock(account_id):
            existing = self.read(account_id, target_url)
            if existing is None:
                raise ManualOutcomeError("MANUAL_OUTCOME_NO_PENDING")
            latest = existing.latest
            if latest.status == _PENDING_STATUS:
                if latest.attempt_id != attempt_id:
                    raise ManualOutcomeError("MANUAL_OUTCOME_ATTEMPT_MISMATCH")
                if latest.draft_sha256 != draft_sha256:
                    raise ManualOutcomeError("MANUAL_OUTCOME_APPROVAL_MISMATCH")
                if len(existing.events) >= _MAX_EVENTS:
                    raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")
            elif latest.source == "OPERATOR" and latest.attempt_id == attempt_id:
                if latest.draft_sha256 != draft_sha256:
                    raise ManualOutcomeError("MANUAL_OUTCOME_APPROVAL_MISMATCH")
                if _same_outcome(latest.status, outcome):
                    return latest, True
                if latest.status in _BLOCKING_OUTCOMES:
                    raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
                raise ManualOutcomeError("MANUAL_OUTCOME_ATTEMPT_MISMATCH")
            else:
                if latest.status in _BLOCKING_OUTCOMES:
                    raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
                raise ManualOutcomeError("MANUAL_OUTCOME_ATTEMPT_MISMATCH")

            if not any(
                event.source == "SOFTWARE_HANDOFF" and event.attempt_id == attempt_id
                for event in existing.events
            ):
                raise ManualOutcomeError("MANUAL_OUTCOME_ATTEMPT_MISMATCH")
            event = self._new_event(
                draft_sha256=draft_sha256,
                status=outcome,
                source="OPERATOR",
                attempt_id=attempt_id,
            )
            record = ManualOutcomeRecord(
                account_uuid=str(account_id),
                target_fingerprint=fingerprint,
                events=(*existing.events, event),
            )
            self._write(record)
            return event, False

    @staticmethod
    def _valid_attempt_id(attempt_id: str) -> bool:
        try:
            parsed = UUID(attempt_id)
        except ValueError, TypeError, AttributeError:
            return False
        return parsed.version == 4 and str(parsed) == attempt_id

    def _new_event(
        self,
        *,
        draft_sha256: str,
        status: ManualOutcomeStatus,
        source: ManualOutcomeSource,
        attempt_id: str | None = None,
    ) -> ManualOutcomeEvent:
        event_id = str(uuid4())
        return ManualOutcomeEvent(
            event_id=event_id,
            attempt_id=event_id if attempt_id is None else attempt_id,
            draft_sha256=draft_sha256,
            recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            status=status,
            source=source,
            verification="UNVERIFIED",
        )

    def _existing_root(self) -> Path | None:
        try:
            root = self._configured_root.resolve(strict=False)
            if not root.exists():
                return None
            if not root.is_dir():
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            self._ensure_outside_checkout(root / _NAMESPACE)
            return root
        except ManualOutcomeError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None

    def _required_root(self) -> Path:
        root = self._existing_root()
        if root is None:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        return root

    def _ensure_outside_checkout(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=False)
        except OSError, RuntimeError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        for checkout in _checkout_roots():
            try:
                resolved.relative_to(checkout)
            except ValueError:
                continue
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")

    def _directory(self, parent: Path, name: str, *, create: bool) -> Path | None:
        path = parent / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                path.mkdir()
                info = path.lstat()
            except OSError:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        except OSError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        try:
            if path.resolve(strict=True) != path:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        except ManualOutcomeError:
            raise
        except OSError, RuntimeError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
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
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        except OSError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info) or info.st_size > max_bytes:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        try:
            if path.resolve(strict=True) != path or path.parent != parent:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        except ManualOutcomeError:
            raise
        except OSError, RuntimeError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
        return True

    def _decode(
        self,
        value: object,
        account_id: UUID,
        target_fingerprint: str,
    ) -> ManualOutcomeRecord:
        if not isinstance(value, dict):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        document = cast(dict[str, object], value)
        if document.keys() != _DOCUMENT_KEYS:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        version = document["version"]
        if type(version) is not int:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        if version != 1:
            raise ManualOutcomeError("MANUAL_OUTCOME_VERSION_UNSUPPORTED")
        if document["account_uuid"] != str(account_id) or document["target_fingerprint"] != (
            target_fingerprint
        ):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        event_values = document["events"]
        if not isinstance(event_values, list):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        decoded_events = cast(list[object], event_values)
        if not 1 <= len(decoded_events) <= _MAX_EVENTS:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        events: list[ManualOutcomeEvent] = []
        event_ids: set[str] = set()
        attempt_ids: set[str] = set()
        pending: ManualOutcomeEvent | None = None
        protocol_started = False
        for event_item in decoded_events:
            if not isinstance(event_item, dict):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event_value = cast(dict[str, object], event_item)
            keys = event_value.keys()
            legacy = keys == _LEGACY_EVENT_KEYS
            if not legacy and keys != _EVENT_KEYS:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event_id = event_value["event_id"]
            draft_sha256 = event_value["draft_sha256"]
            recorded_at = event_value["recorded_at"]
            verification = event_value["verification"]
            if legacy:
                status = event_value["operator_declaration"]
                source = "LEGACY_OPERATOR"
                attempt_id = event_id
            else:
                status = event_value["status"]
                source = event_value["source"]
                attempt_id = event_value["attempt_id"]
            if not isinstance(event_id, str) or not isinstance(attempt_id, str):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            if not isinstance(draft_sha256, str) or not isinstance(recorded_at, str):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            if not isinstance(status, str) or not isinstance(source, str):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            if verification != "UNVERIFIED":
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            try:
                parsed_event_id = UUID(event_id)
                parsed_attempt_id = UUID(attempt_id)
                timestamp = (
                    datetime.fromisoformat(recorded_at) if recorded_at.endswith("Z") else None
                )
            except ValueError, TypeError, AttributeError:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
            if (
                parsed_event_id.version != 4
                or event_id in event_ids
                or parsed_attempt_id.version != 4
                or str(parsed_attempt_id) != attempt_id
                or _SHA256.fullmatch(draft_sha256) is None
                or len(recorded_at) != 20
                or timestamp is None
                or timestamp.utcoffset() != UTC.utcoffset(timestamp)
            ):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event_ids.add(event_id)
            if source == "LEGACY_OPERATOR":
                if (
                    protocol_started
                    or pending is not None
                    or status not in _OUTCOMES
                    or attempt_id != event_id
                    or (events and events[-1].status in _BLOCKING_OUTCOMES)
                ):
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            elif source == "SOFTWARE_HANDOFF":
                if (
                    status != _PENDING_STATUS
                    or attempt_id != event_id
                    or attempt_id in attempt_ids
                    or pending is not None
                    or (events and events[-1].status != "ABORTED_NOT_SENT")
                ):
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
                protocol_started = True
                attempt_ids.add(attempt_id)
            elif source == "OPERATOR":
                if (
                    status not in _OUTCOMES
                    or pending is None
                    or pending.attempt_id != attempt_id
                    or pending.draft_sha256 != draft_sha256
                ):
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
                protocol_started = True
                pending = None
            else:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            if not _is_outcome_status(status) or not _is_event_source(source):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event = ManualOutcomeEvent(
                event_id=event_id,
                attempt_id=attempt_id,
                draft_sha256=draft_sha256,
                recorded_at=recorded_at,
                status=status,
                source=source,
                verification="UNVERIFIED",
            )
            events.append(event)
            if source == "SOFTWARE_HANDOFF":
                pending = event
        return ManualOutcomeRecord(
            account_uuid=str(account_id),
            target_fingerprint=target_fingerprint,
            events=tuple(events),
        )

    def _write(
        self,
        record: ManualOutcomeRecord,
        *,
        lock_lease: ManualOutcomeLockLease | None = None,
    ) -> None:
        root = self._required_root()
        if lock_lease is not None:
            self._assert_lock_lease(lock_lease, UUID(record.account_uuid), root)
        self._ensure_outside_checkout(root / _NAMESPACE)
        namespace = self._directory(root, _NAMESPACE, create=True)
        assert namespace is not None
        records = self._directory(namespace, "records", create=True)
        assert records is not None
        account_dir = self._directory(records, record.account_uuid, create=True)
        assert account_dir is not None
        target = account_dir / f"{record.target_fingerprint}.json"
        existing = self._regular_file(target, account_dir, allow_missing=True)
        document = (
            json.dumps(
                {
                    "version": 1,
                    "account_uuid": record.account_uuid,
                    "target_fingerprint": record.target_fingerprint,
                    "events": [
                        {
                            "event_id": event.event_id,
                            "attempt_id": event.attempt_id,
                            "draft_sha256": event.draft_sha256,
                            "recorded_at": event.recorded_at,
                            "status": event.status,
                            "source": event.source,
                            "verification": event.verification,
                        }
                        for event in record.events
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        if len(document.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
            raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")

        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=account_dir
            )
            temporary_path = Path(temporary_name)
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            if existing:
                self._regular_file(target, account_dir, allow_missing=False)
                os.replace(temporary_path, target)
            else:
                try:
                    os.link(temporary_path, target)
                except FileExistsError:
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
            self._regular_file(target, account_dir, allow_missing=False)
        except ManualOutcomeError:
            raise
        except OSError:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
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
