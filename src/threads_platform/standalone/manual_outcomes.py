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
from typing import Literal, NoReturn, cast
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
_OUTCOMES = frozenset({"ABORTED_NOT_SENT", "OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"})
_BLOCKING_OUTCOMES = frozenset({"OPERATOR_REPORTED_SENT_UNVERIFIED", "AMBIGUOUS"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_DOCUMENT_BYTES = 16 * 1024
_MAX_EVENTS = 16
_NAMESPACE = "manual_recruitment_v1"
_DOCUMENT_KEYS = frozenset({"version", "account_uuid", "target_fingerprint", "events"})
_EVENT_KEYS = frozenset(
    {"event_id", "draft_sha256", "recorded_at", "operator_declaration", "verification"}
)


class ManualOutcomeError(Exception):
    """Safe error containing a fixed local-journal code only."""

    _CODES = frozenset(
        {
            "MANUAL_OUTCOME_INVALID",
            "MANUAL_OUTCOME_VERSION_UNSUPPORTED",
            "MANUAL_OUTCOME_BUSY",
            "MANUAL_OUTCOME_BLOCKED",
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
    draft_sha256: str
    recorded_at: str
    operator_declaration: ManualOutcomeValue
    verification: Literal["UNVERIFIED"]


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
        return any(event.operator_declaration in _BLOCKING_OUTCOMES for event in self.events)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


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
        root = self._existing_root()
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
            yield
        finally:
            if lock.held:
                try:
                    lock.release()
                except OSError:
                    raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None

    def record(
        self,
        account_id: UUID,
        target_url: str,
        draft_sha256: str,
        outcome: ManualOutcomeValue,
    ) -> tuple[ManualOutcomeEvent, bool]:
        fingerprint = self.target_fingerprint(account_id, target_url)
        if _SHA256.fullmatch(draft_sha256) is None or outcome not in _OUTCOMES:
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")

        with self.account_lock(account_id):
            existing = self.read(account_id, target_url)
            if existing is not None:
                latest = existing.latest
                if latest.operator_declaration in _BLOCKING_OUTCOMES:
                    if (
                        latest.operator_declaration == outcome
                        and latest.draft_sha256 == draft_sha256
                    ):
                        return latest, True
                    raise ManualOutcomeError("MANUAL_OUTCOME_BLOCKED")
                for event in existing.events:
                    if event.operator_declaration == outcome and event.draft_sha256 == draft_sha256:
                        return event, True
                if len(existing.events) >= _MAX_EVENTS:
                    raise ManualOutcomeError("MANUAL_OUTCOME_LIMIT_REACHED")
                events = (*existing.events, self._new_event(draft_sha256, outcome))
            else:
                events = (self._new_event(draft_sha256, outcome),)

            record = ManualOutcomeRecord(
                account_uuid=str(account_id),
                target_fingerprint=fingerprint,
                events=events,
            )
            self._write(record)
            return record.latest, False

    def _new_event(self, draft_sha256: str, outcome: ManualOutcomeValue) -> ManualOutcomeEvent:
        return ManualOutcomeEvent(
            event_id=str(uuid4()),
            draft_sha256=draft_sha256,
            recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            operator_declaration=outcome,
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
        for event_item in decoded_events:
            if not isinstance(event_item, dict):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event_value = cast(dict[str, object], event_item)
            if event_value.keys() != _EVENT_KEYS:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event_id = event_value["event_id"]
            draft_sha256 = event_value["draft_sha256"]
            recorded_at = event_value["recorded_at"]
            declaration = event_value["operator_declaration"]
            verification = event_value["verification"]
            if (
                not isinstance(event_id, str)
                or not isinstance(draft_sha256, str)
                or not isinstance(recorded_at, str)
                or not isinstance(declaration, str)
                or verification != "UNVERIFIED"
            ):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            try:
                parsed_event_id = UUID(event_id)
                timestamp = (
                    datetime.fromisoformat(recorded_at) if recorded_at.endswith("Z") else None
                )
            except ValueError, TypeError:
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID") from None
            if (
                parsed_event_id.version != 4
                or event_id in event_ids
                or _SHA256.fullmatch(draft_sha256) is None
                or timestamp is None
                or timestamp.utcoffset() != UTC.utcoffset(timestamp)
                or declaration not in _OUTCOMES
            ):
                raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
            event_ids.add(event_id)
            events.append(
                ManualOutcomeEvent(
                    event_id=event_id,
                    draft_sha256=draft_sha256,
                    recorded_at=recorded_at,
                    operator_declaration=declaration,  # type: ignore[arg-type]
                    verification="UNVERIFIED",
                )
            )
        if any(event.operator_declaration in _BLOCKING_OUTCOMES for event in events[:-1]):
            raise ManualOutcomeError("MANUAL_OUTCOME_INVALID")
        return ManualOutcomeRecord(
            account_uuid=str(account_id),
            target_fingerprint=target_fingerprint,
            events=tuple(events),
        )

    def _write(self, record: ManualOutcomeRecord) -> None:
        root = self._required_root()
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
                            "draft_sha256": event.draft_sha256,
                            "recorded_at": event.recorded_at,
                            "operator_declaration": event.operator_declaration,
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
