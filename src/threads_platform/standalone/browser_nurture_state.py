"""Private v1 configuration and checkpoint state for Browser Nurture."""

# Browser Nurture shares the established private path and ACL primitives. It owns
# separate versioned files and never opens or rewrites NurtureStore v1 records.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID

from threads_platform.standalone import recruitment_job_store as _store_fs
from threads_platform.standalone.recruitment_job_store import RecruitmentJobStoreError

CONFIG_MAX_BYTES = 16 * 1024
CHECKPOINT_MAX_BYTES = 512 * 1024
MAX_NURTURE_TOPICS = 5
MAX_NURTURE_RELEVANCE_TERMS = 32
MAX_NURTURE_POSTS_PER_CYCLE = 20
MAX_NURTURE_IDLE_SECONDS = 86_400
MAX_SEEN_POST_FINGERPRINTS = 5_000
MAX_CHECKPOINT_COUNTER = 1_000_000_000_000

_NAMESPACE = "browser_nurture_v1"
_ACCOUNT_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
_SAFE_TOPIC_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "account_uuid",
        "expected_username",
        "topics",
        "relevance_terms",
        "max_posts_per_cycle",
        "idle_seconds",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "schema_version",
        "account_uuid",
        "completed_cycles",
        "scanned",
        "relevant",
        "irrelevant",
        "unknown",
        "duplicates",
        "scrolls",
        "seen_fingerprints",
        "crash_recovery_pending",
    }
)


class BrowserNurtureStateError(Exception):
    """Fixed-code error for private Browser Nurture state operations."""

    def __init__(self, code: str) -> None:
        if code not in {
            "CONFIG_INVALID",
            "CONFIG_NOT_FOUND",
            "CHECKPOINT_INVALID",
            "STATE_UNSAFE",
            "STATE_WRITE_FAILED",
            "CRASH_RECOVERY_EXHAUSTED",
        }:
            raise ValueError("unsupported Browser Nurture state error")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"BrowserNurtureStateError({self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserNurtureConfigV1:
    account_uuid: UUID
    expected_username: str = field(repr=False)
    topics: tuple[str, ...] = field(repr=False)
    relevance_terms: tuple[str, ...] = field(repr=False)
    max_posts_per_cycle: int = MAX_NURTURE_POSTS_PER_CYCLE
    idle_seconds: int = 900
    schema_version: int = 1

    def __repr__(self) -> str:
        return (
            "BrowserNurtureConfigV1("
            f"account=<redacted>, topics={len(self.topics)}, "
            f"relevance_terms={len(self.relevance_terms)}, "
            f"max_posts_per_cycle={self.max_posts_per_cycle}, "
            f"idle_seconds={self.idle_seconds})"
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "account_uuid": str(self.account_uuid),
            "expected_username": self.expected_username,
            "topics": list(self.topics),
            "relevance_terms": list(self.relevance_terms),
            "max_posts_per_cycle": self.max_posts_per_cycle,
            "idle_seconds": self.idle_seconds,
        }


@dataclass(frozen=True, slots=True, repr=False)
class BrowserNurtureCheckpointV1:
    account_uuid: UUID
    completed_cycles: int = 0
    scanned: int = 0
    relevant: int = 0
    irrelevant: int = 0
    unknown: int = 0
    duplicates: int = 0
    scrolls: int = 0
    seen_fingerprints: tuple[str, ...] = field(default=(), repr=False)
    crash_recovery_pending: bool = False
    schema_version: int = 1

    def __repr__(self) -> str:
        return (
            "BrowserNurtureCheckpointV1("
            f"account=<redacted>, completed_cycles={self.completed_cycles}, "
            f"scanned={self.scanned}, relevant={self.relevant}, "
            f"duplicates={self.duplicates}, seen_count={len(self.seen_fingerprints)}, "
            f"crash_recovery_pending={self.crash_recovery_pending})"
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "account_uuid": str(self.account_uuid),
            "completed_cycles": self.completed_cycles,
            "scanned": self.scanned,
            "relevant": self.relevant,
            "irrelevant": self.irrelevant,
            "unknown": self.unknown,
            "duplicates": self.duplicates,
            "scrolls": self.scrolls,
            "seen_fingerprints": list(self.seen_fingerprints),
            "crash_recovery_pending": self.crash_recovery_pending,
        }


def validate_browser_nurture_config(value: object) -> BrowserNurtureConfigV1:
    if type(value) is not dict:
        raise BrowserNurtureStateError("CONFIG_INVALID")
    document = cast(dict[str, object], value)
    if frozenset(document) != _CONFIG_KEYS:
        raise BrowserNurtureStateError("CONFIG_INVALID")
    try:
        account_uuid = UUID(_required_string(document["account_uuid"]))
    except TypeError, ValueError, AttributeError:
        raise BrowserNurtureStateError("CONFIG_INVALID") from None
    schema_version = document["schema_version"]
    expected_username = document["expected_username"]
    max_posts = document["max_posts_per_cycle"]
    idle_seconds = document["idle_seconds"]
    topics = _validated_string_tuple(document["topics"], MAX_NURTURE_TOPICS, 120)
    relevance_terms = _validated_string_tuple(
        document["relevance_terms"], MAX_NURTURE_RELEVANCE_TERMS, 80
    )
    if (
        type(schema_version) is not int
        or schema_version != 1
        or not isinstance(expected_username, str)
        or _ACCOUNT_HANDLE.fullmatch(expected_username) is None
        or type(max_posts) is not int
        or not 1 <= max_posts <= MAX_NURTURE_POSTS_PER_CYCLE
        or type(idle_seconds) is not int
        or not 60 <= idle_seconds <= MAX_NURTURE_IDLE_SECONDS
        or not topics
        or not relevance_terms
    ):
        raise BrowserNurtureStateError("CONFIG_INVALID")
    return BrowserNurtureConfigV1(
        account_uuid=account_uuid,
        expected_username=expected_username,
        topics=topics,
        relevance_terms=relevance_terms,
        max_posts_per_cycle=max_posts,
        idle_seconds=idle_seconds,
        schema_version=schema_version,
    )


def validate_browser_nurture_checkpoint(value: object) -> BrowserNurtureCheckpointV1:
    if type(value) is not dict:
        raise BrowserNurtureStateError("CHECKPOINT_INVALID")
    document = cast(dict[str, object], value)
    if frozenset(document) != _CHECKPOINT_KEYS:
        raise BrowserNurtureStateError("CHECKPOINT_INVALID")
    try:
        account_uuid = UUID(_required_string(document["account_uuid"]))
    except TypeError, ValueError, AttributeError:
        raise BrowserNurtureStateError("CHECKPOINT_INVALID") from None
    version = document["schema_version"]
    counters = tuple(
        document[key]
        for key in (
            "completed_cycles",
            "scanned",
            "relevant",
            "irrelevant",
            "unknown",
            "duplicates",
            "scrolls",
        )
    )
    raw_fingerprints = document["seen_fingerprints"]
    recovery_pending = document["crash_recovery_pending"]
    if (
        type(version) is not int
        or version != 1
        or any(
            type(counter) is not int or not 0 <= counter <= MAX_CHECKPOINT_COUNTER
            for counter in counters
        )
        or type(raw_fingerprints) is not list
        or type(recovery_pending) is not bool
    ):
        raise BrowserNurtureStateError("CHECKPOINT_INVALID")
    fingerprints = cast(list[object], raw_fingerprints)
    if (
        len(fingerprints) > MAX_SEEN_POST_FINGERPRINTS
        or any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in fingerprints
        )
        or len(set(cast(list[str], fingerprints))) != len(fingerprints)
    ):
        raise BrowserNurtureStateError("CHECKPOINT_INVALID")
    return BrowserNurtureCheckpointV1(
        account_uuid=account_uuid,
        completed_cycles=cast(int, counters[0]),
        scanned=cast(int, counters[1]),
        relevant=cast(int, counters[2]),
        irrelevant=cast(int, counters[3]),
        unknown=cast(int, counters[4]),
        duplicates=cast(int, counters[5]),
        scrolls=cast(int, counters[6]),
        seen_fingerprints=tuple(cast(list[str], fingerprints)),
        crash_recovery_pending=recovery_pending,
        schema_version=version,
    )


class BrowserNurtureStateStore:
    """Store strict private Browser Nurture v1 files under the existing data root."""

    def __init__(self, data_root: Path) -> None:
        self._root = Path(data_root).absolute()

    def load_config(self, account_uuid: UUID) -> BrowserNurtureConfigV1 | None:
        path, directory = self._file_path(account_uuid, "config")
        document = self._read(path, directory, CONFIG_MAX_BYTES)
        if document is None:
            return None
        config = validate_browser_nurture_config(document)
        if config.account_uuid != account_uuid:
            raise BrowserNurtureStateError("CONFIG_INVALID")
        return config

    def save_config(self, config: BrowserNurtureConfigV1) -> None:
        validated = validate_browser_nurture_config(config.to_document())
        path, directory = self._file_path(validated.account_uuid, "config")
        self._write(path, directory, validated.to_document(), CONFIG_MAX_BYTES)

    def load_checkpoint(self, account_uuid: UUID) -> BrowserNurtureCheckpointV1:
        path, directory = self._file_path(account_uuid, "checkpoints")
        document = self._read(path, directory, CHECKPOINT_MAX_BYTES)
        if document is None:
            return BrowserNurtureCheckpointV1(account_uuid=account_uuid)
        checkpoint = validate_browser_nurture_checkpoint(document)
        if checkpoint.account_uuid != account_uuid:
            raise BrowserNurtureStateError("CHECKPOINT_INVALID")
        return checkpoint

    def save_checkpoint(self, checkpoint: BrowserNurtureCheckpointV1) -> None:
        validated = validate_browser_nurture_checkpoint(checkpoint.to_document())
        path, directory = self._file_path(validated.account_uuid, "checkpoints")
        self._write(path, directory, validated.to_document(), CHECKPOINT_MAX_BYTES)

    def mark_crash_recovery(
        self, checkpoint: BrowserNurtureCheckpointV1
    ) -> BrowserNurtureCheckpointV1:
        if checkpoint.crash_recovery_pending:
            raise BrowserNurtureStateError("CRASH_RECOVERY_EXHAUSTED")
        updated = BrowserNurtureCheckpointV1(
            account_uuid=checkpoint.account_uuid,
            completed_cycles=checkpoint.completed_cycles,
            scanned=checkpoint.scanned,
            relevant=checkpoint.relevant,
            irrelevant=checkpoint.irrelevant,
            unknown=checkpoint.unknown,
            duplicates=checkpoint.duplicates,
            scrolls=checkpoint.scrolls,
            seen_fingerprints=checkpoint.seen_fingerprints,
            crash_recovery_pending=True,
        )
        self.save_checkpoint(updated)
        return updated

    def _file_path(self, account_uuid: UUID, directory_name: str) -> tuple[Path, Path]:
        _namespace, configs, checkpoints = self._directories()
        directory = configs if directory_name == "config" else checkpoints
        path = directory / f"{account_uuid}.json"
        _store_fs._require_parented_child(path, directory)
        return path, directory

    def _directories(self) -> tuple[Path, Path, Path]:
        try:
            _store_fs._validate_path_components(self._root)
            info = self._root.lstat()
            if not stat.S_ISDIR(info.st_mode) or _store_fs._is_reparse_point(info):
                raise BrowserNurtureStateError("STATE_UNSAFE")
            _store_fs._ensure_outside_checkout(self._root)
            _store_fs._verify_private_directory(self._root)
            namespace, created = _store_fs._ensure_directory(self._root, _NAMESPACE)
            if created:
                _store_fs._secure_directory(namespace, configure_windows_acl=True)
            else:
                _store_fs._verify_private_directory(namespace)
            config, config_created = _store_fs._ensure_directory(namespace, "config")
            checkpoints, checkpoints_created = _store_fs._ensure_directory(namespace, "checkpoints")
            if config_created:
                _store_fs._secure_directory(config, configure_windows_acl=True)
            else:
                _store_fs._verify_private_directory(config)
            if checkpoints_created:
                _store_fs._secure_directory(checkpoints, configure_windows_acl=True)
            else:
                _store_fs._verify_private_directory(checkpoints)
            return namespace, config, checkpoints
        except BrowserNurtureStateError:
            raise
        except RecruitmentJobStoreError:
            raise BrowserNurtureStateError("STATE_UNSAFE") from None
        except OSError, RuntimeError, ValueError:
            raise BrowserNurtureStateError("STATE_UNSAFE") from None

    def _read(self, path: Path, directory: Path, max_bytes: int) -> dict[str, object] | None:
        try:
            exists = _store_fs._verify_regular_file(
                path,
                directory,
                allow_missing=True,
                max_bytes=max_bytes,
            )
            if not exists:
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                    raise BrowserNurtureStateError("STATE_UNSAFE")
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise BrowserNurtureStateError("STATE_UNSAFE")
            decoded = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            if type(decoded) is not dict:
                raise BrowserNurtureStateError("STATE_UNSAFE")
            return cast(dict[str, object], decoded)
        except BrowserNurtureStateError:
            raise
        except RecruitmentJobStoreError:
            raise BrowserNurtureStateError("STATE_UNSAFE") from None
        except OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise BrowserNurtureStateError("STATE_UNSAFE") from None

    def _write(
        self,
        path: Path,
        directory: Path,
        document: dict[str, object],
        max_bytes: int,
    ) -> None:
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            raw = (
                json.dumps(
                    document,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("ascii")
            if len(raw) > max_bytes:
                raise BrowserNurtureStateError("STATE_WRITE_FAILED")
            _store_fs._verify_private_directory(directory)
            _store_fs._verify_regular_file(
                path,
                directory,
                allow_missing=True,
                max_bytes=max_bytes,
            )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".browser-nurture-",
                suffix=".tmp",
                dir=directory,
            )
            temporary_path = Path(temporary_name)
            _store_fs._secure_file(temporary_path)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _store_fs._verify_regular_file(
                temporary_path,
                directory,
                allow_missing=False,
                max_bytes=max_bytes,
            )
            _store_fs._verify_regular_file(
                path,
                directory,
                allow_missing=True,
                max_bytes=max_bytes,
            )
            os.replace(temporary_path, path)
            temporary_path = None
            _store_fs._verify_regular_file(
                path,
                directory,
                allow_missing=False,
                max_bytes=max_bytes,
            )
            if os.name != "nt":
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BrowserNurtureStateError:
            raise
        except RecruitmentJobStoreError:
            raise BrowserNurtureStateError("STATE_UNSAFE") from None
        except OSError, TypeError, ValueError, UnicodeError:
            raise BrowserNurtureStateError("STATE_WRITE_FAILED") from None
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


def _validated_string_tuple(value: object, max_count: int, max_length: int) -> tuple[str, ...]:
    if type(value) is not list:
        raise BrowserNurtureStateError("CONFIG_INVALID")
    values = cast(list[object], value)
    if not 1 <= len(values) <= max_count:
        raise BrowserNurtureStateError("CONFIG_INVALID")
    normalized: list[str] = []
    for item in values:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > max_length
            or item != item.strip()
            or _SAFE_TOPIC_CONTROL.search(item) is not None
        ):
            raise BrowserNurtureStateError("CONFIG_INVALID")
        normalized.append(item)
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise BrowserNurtureStateError("CONFIG_INVALID")
    return tuple(normalized)


def _required_string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")
