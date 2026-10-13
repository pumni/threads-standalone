"""Finite foreground orchestration for synthetic Browser Jobs fixtures.

This module has no browser, network, CLI, or background-worker implementation.
Only ``SYNTHETIC_FIXTURE`` can reach the parser and private vacancy archive.
"""

# The watch checkpoint shares BJ-03's fail-closed path and ACL primitives. BJ-03
# is leased read-only, so this slice deliberately consumes those private helpers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from threads_platform.browser import job_observation as _observation_fs
from threads_platform.browser.job_observation import (
    BrowserJobObservationV1,
    FakeClock,
    FakeJobBrowserSession,
    FakeStopSignal,
    ObservationLimits,
    ObservationReasonCode,
    ObservationRunStatus,
    observe_fake_job_feed,
    to_parser_observation,
)
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone import recruitment_job_store as _store_fs
from threads_platform.standalone.recruitment_job_store import (
    RecruitmentJobStore,
    RecruitmentJobStoreError,
    RetentionPolicy,
    StoreErrorCode,
)
from threads_platform.standalone.recruitment_job_vacancy import (
    ExtractionResult,
    extract_vietnamese_vacancies,
)

DEFAULT_MAX_CYCLES = 1
MAX_WATCH_CYCLES = 5
MAX_SELECTED_POSTS_PER_CYCLE = 20
MIN_CYCLE_INTERVAL_SECONDS = 15 * 60
MAX_CHECKPOINT_BYTES = 16 * 1024
MAX_CAPTURE_IDENTITIES = 200
MAX_QUERY_CODEPOINTS = 120
MAX_CYCLE_INTERVAL_SECONDS = 2_147_483_647

_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_NAMESPACE = "browser_job_watch_v1"
_CHECKPOINT_KEYS = frozenset(
    {
        "schema_version",
        "account_uuid",
        "generation",
        "run_id",
        "query_sha256",
        "target_plan_sha256",
        "max_cycles",
        "interval_seconds",
        "completed_cycles",
        "last_observed_at_utc",
        "pending_capture_id",
        "pending_observed_at_utc",
        "pending_targets_sha256",
        "capture_id_sha256",
    }
)
_OBSERVATION_LIMITS = ObservationLimits(max_detail_reads=MAX_SELECTED_POSTS_PER_CYCLE)


class WatchSource(StrEnum):
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    MANUAL_URL = "MANUAL_URL"
    BROWSER_UI = "BROWSER_UI"


class WatchStatus(StrEnum):
    COMPLETE = "COMPLETE"
    STOPPED = "STOPPED"
    CAP_REACHED = "CAP_REACHED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    DISABLED_PERMISSION_PENDING = "DISABLED_PERMISSION_PENDING"


class WatchReasonCode(StrEnum):
    NO_TEXT = "NO_TEXT"
    INVALID_SOURCE = "INVALID_SOURCE"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_POLICY = "INVALID_POLICY"
    INVALID_CHECKPOINT = "INVALID_CHECKPOINT"
    CHECKPOINT_VERSION_UNSUPPORTED = "CHECKPOINT_VERSION_UNSUPPORTED"
    CHECKPOINT_CONFLICT = "CHECKPOINT_CONFLICT"
    CHECKPOINT_BUSY = "CHECKPOINT_BUSY"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    CHECKPOINT_UNSAFE = "CHECKPOINT_UNSAFE"
    CAPTURE_ID_INVALID = "CAPTURE_ID_INVALID"
    CAPTURE_ID_REUSED = "CAPTURE_ID_REUSED"
    TARGET_CHANGED = "TARGET_CHANGED"
    CLOCK_INVALID = "CLOCK_INVALID"
    CLOCK_MOVED_BACKWARD = "CLOCK_MOVED_BACKWARD"
    INTERVAL_NOT_MET = "INTERVAL_NOT_MET"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOP_SIGNAL_FAILURE = "STOP_SIGNAL_FAILURE"
    OBSERVATION_FAILED = "OBSERVATION_FAILED"
    PARSER_FAILED = "PARSER_FAILED"
    ARCHIVE_FAILED = "ARCHIVE_FAILED"
    ARCHIVE_LIMIT_REACHED = "ARCHIVE_LIMIT_REACHED"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    SOURCE_DISABLED = "SOURCE_DISABLED"


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticCycleInput:
    """One explicitly injected in-memory BJ-02 fake session and selected targets."""

    session: FakeJobBrowserSession = field(repr=False)
    selected_targets: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "SyntheticCycleInput(session=<redacted>, selected_targets=<redacted>)"


class WatchClock(FakeClock, Protocol):
    """Injected UTC and monotonic clocks; implementations used by tests are fake."""

    def now_utc(self) -> datetime: ...


class WatchSleeper(Protocol):
    """Injected foreground interval seam; never replaced with an internal sleep."""

    def sleep(self, seconds: float) -> None: ...


class CaptureIDFactory(Protocol):
    """Return the same local capture ID for the same arguments after a restart."""

    def __call__(
        self,
        *,
        account_uuid: UUID,
        run_id: str,
        generation: int,
        cycle_number: int,
        observed_at_utc: datetime,
    ) -> str: ...


class WatchCheckpointIO(Protocol):
    """Private checkpoint boundary; tests may inject failures at atomic-write edges."""

    def account_lock(self, account_uuid: UUID) -> AbstractContextManager[None]: ...

    def load(self, account_uuid: UUID) -> object | None: ...

    def save(self, account_uuid: UUID, checkpoint: Mapping[str, object]) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class WatchRunResult:
    status: WatchStatus
    reason_code: WatchReasonCode | str | None = None
    scanned: int = 0
    vacancy: int = 0
    duplicate: int = 0
    partial: int = 0
    errors: int = 0
    completed_cycles: int = 0
    next_due_utc: datetime | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        reason = None if self.reason_code is None else str(self.reason_code)
        return (
            "WatchRunResult("
            f"status={self.status.value!r}, reason_code={reason!r}, "
            f"scanned={self.scanned}, vacancy={self.vacancy}, "
            f"duplicate={self.duplicate}, partial={self.partial}, errors={self.errors}, "
            "next_due=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    account_uuid: str
    generation: int
    run_id: str
    query_sha256: str
    target_plan_sha256: str
    max_cycles: int
    interval_seconds: int
    completed_cycles: int
    last_observed_at_utc: str | None
    pending_capture_id: str | None
    pending_observed_at_utc: str | None
    pending_targets_sha256: str | None
    capture_id_sha256: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "account_uuid": self.account_uuid,
            "generation": self.generation,
            "run_id": self.run_id,
            "query_sha256": self.query_sha256,
            "target_plan_sha256": self.target_plan_sha256,
            "max_cycles": self.max_cycles,
            "interval_seconds": self.interval_seconds,
            "completed_cycles": self.completed_cycles,
            "last_observed_at_utc": self.last_observed_at_utc,
            "pending_capture_id": self.pending_capture_id,
            "pending_observed_at_utc": self.pending_observed_at_utc,
            "pending_targets_sha256": self.pending_targets_sha256,
            "capture_id_sha256": list(self.capture_id_sha256),
        }

    @property
    def is_complete(self) -> bool:
        return self.completed_cycles == self.max_cycles and self.pending_capture_id is None


class _WatchFailure(Exception):
    def __init__(self, reason: WatchReasonCode) -> None:
        self.reason = reason
        super().__init__(reason.value)


class _PrivateCheckpointIO:
    """Per-account checkpoint files under the caller's explicit synthetic root."""

    def __init__(self, data_root: Path) -> None:
        self._root = data_root

    @contextmanager
    def account_lock(self, account_uuid: UUID) -> Generator[None]:
        _namespace, locks, _accounts = self._directories()
        lock_path = locks / f"{account_uuid}.lock"
        _store_fs._require_parented_child(lock_path, locks)
        try:
            _store_fs._ensure_private_lock_file(lock_path, locks)
            lock = FilesystemProcessLock(lock_path)
            lock.acquire()
        except ProcessAlreadyRunning:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_BUSY) from None
        except RecruitmentJobStoreError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE) from None
        except OSError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_FAILED) from None
        try:
            _store_fs._verify_regular_file(lock_path, locks, allow_missing=False, max_bytes=1)
            _store_fs._secure_file(lock_path)
            yield
        finally:
            if lock.held:
                try:
                    lock.release()
                except OSError:
                    raise _WatchFailure(WatchReasonCode.CHECKPOINT_FAILED) from None

    def load(self, account_uuid: UUID) -> object | None:
        _namespace, _locks, accounts = self._directories()
        path = accounts / f"{account_uuid}.json"
        _store_fs._require_parented_child(path, accounts)
        try:
            exists = _store_fs._verify_regular_file(
                path,
                accounts,
                allow_missing=True,
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
            if not exists:
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CHECKPOINT_BYTES:
                    raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE)
                raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
            if len(raw) > MAX_CHECKPOINT_BYTES:
                raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
            try:
                decoded = json.loads(
                    raw.decode("utf-8", errors="strict"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                )
            except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
                raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT) from None
            if not isinstance(decoded, dict):
                raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
            return cast(dict[str, object], decoded)
        except _WatchFailure:
            raise
        except RecruitmentJobStoreError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE) from None
        except OSError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_FAILED) from None

    def save(self, account_uuid: UUID, checkpoint: Mapping[str, object]) -> None:
        _namespace, _locks, accounts = self._directories()
        path = accounts / f"{account_uuid}.json"
        _store_fs._require_parented_child(path, accounts)
        try:
            raw = (
                json.dumps(
                    dict(checkpoint),
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("ascii")
        except TypeError, ValueError, UnicodeError:
            raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT) from None
        if len(raw) > MAX_CHECKPOINT_BYTES:
            raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)

        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            _store_fs._verify_private_directory(accounts)
            _store_fs._verify_regular_file(
                path,
                accounts,
                allow_missing=True,
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".checkpoint-",
                suffix=".tmp",
                dir=accounts,
            )
            temporary_path = Path(temporary_name)
            _store_fs._secure_file(temporary_path)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _store_fs._verify_regular_file(
                temporary_path,
                accounts,
                allow_missing=False,
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
            _store_fs._verify_regular_file(
                path,
                accounts,
                allow_missing=True,
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
            os.replace(temporary_path, path)
            temporary_path = None
            _store_fs._verify_regular_file(
                path,
                accounts,
                allow_missing=False,
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
            if os.name != "nt":
                directory_fd = os.open(accounts, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except _WatchFailure:
            raise
        except RecruitmentJobStoreError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE) from None
        except OSError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_FAILED) from None
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

    def _directories(self) -> tuple[Path, Path, Path]:
        try:
            _store_fs._validate_path_components(self._root)
            info = self._root.lstat()
            if not stat.S_ISDIR(info.st_mode) or _store_fs._is_reparse_point(info):
                raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE)
            resolved = self._root.resolve(strict=True)
            _store_fs._ensure_outside_checkout(resolved)
            if os.name == "nt":
                _store_fs._verify_windows_private_acl(self._root)
            elif stat.S_IMODE(info.st_mode) & 0o077:
                raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE)
            namespace, created = _store_fs._ensure_directory(
                self._root,
                _CHECKPOINT_NAMESPACE,
            )
            if created:
                _store_fs._secure_directory(namespace, configure_windows_acl=True)
            else:
                _store_fs._verify_private_directory(namespace)
            locks, _ = _store_fs._ensure_directory(namespace, "locks")
            accounts, _ = _store_fs._ensure_directory(namespace, "accounts")
            _store_fs._verify_private_directory(locks)
            _store_fs._verify_private_directory(accounts)
            return namespace, locks, accounts
        except _WatchFailure:
            raise
        except RecruitmentJobStoreError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE) from None
        except OSError, RuntimeError, ValueError:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_UNSAFE) from None


class BrowserJobWatchEngine:
    """Run at most five explicit cycles over synthetic fake sessions in foreground."""

    def __init__(
        self,
        data_root: Path,
        *,
        retention_policy: RetentionPolicy,
        checkpoint_io: WatchCheckpointIO | None = None,
    ) -> None:
        try:
            root = Path(data_root)
        except TypeError, ValueError:
            raise ValueError("INVALID_DATA_ROOT") from None
        if not root.is_absolute():
            raise ValueError("INVALID_DATA_ROOT")
        self._store = RecruitmentJobStore(root, retention_policy=retention_policy)
        self._checkpoint_io = checkpoint_io or _PrivateCheckpointIO(root)

    def run(
        self,
        *,
        source: WatchSource,
        account_uuid: UUID | None = None,
        query: str | None = None,
        run_id: str | None = None,
        cycles: Sequence[SyntheticCycleInput] = (),
        max_cycles: int = DEFAULT_MAX_CYCLES,
        interval_seconds: int = MIN_CYCLE_INTERVAL_SECONDS,
        clock: WatchClock | None = None,
        sleeper: WatchSleeper | None = None,
        stop_signal: FakeStopSignal | None = None,
        capture_id_factory: CaptureIDFactory | None = None,
        manual_url: str | None = None,
    ) -> WatchRunResult:
        """Run synthetic fixtures or return a fixed disabled/no-text source result.

        ``manual_url`` is deliberately never inspected. ``BROWSER_UI`` returns before
        any fixture, clock, store, checkpoint, or session is accessed.
        """

        if source is WatchSource.BROWSER_UI:
            return WatchRunResult(
                status=WatchStatus.DISABLED_PERMISSION_PENDING,
                reason_code=WatchReasonCode.SOURCE_DISABLED,
            )
        if source is WatchSource.MANUAL_URL:
            return WatchRunResult(
                status=WatchStatus.UNKNOWN,
                reason_code=WatchReasonCode.NO_TEXT,
            )
        if source is not WatchSource.SYNTHETIC_FIXTURE:
            return WatchRunResult(
                status=WatchStatus.FAILED,
                reason_code=WatchReasonCode.INVALID_SOURCE,
                errors=1,
            )

        validated = _validate_run_inputs(
            account_uuid=account_uuid,
            query=query,
            run_id=run_id,
            cycles=cycles,
            max_cycles=max_cycles,
            interval_seconds=interval_seconds,
            clock=clock,
            sleeper=sleeper,
            stop_signal=stop_signal,
            capture_id_factory=capture_id_factory,
        )
        if validated is None:
            return WatchRunResult(
                status=WatchStatus.FAILED,
                reason_code=WatchReasonCode.INVALID_POLICY,
                errors=1,
            )
        (
            account_id,
            safe_query,
            safe_run_id,
            safe_cycles,
            safe_clock,
            safe_sleeper,
            safe_stop,
            id_factory,
        ) = validated

        stop_reason = _stop_reason(safe_stop)
        if stop_reason is WatchReasonCode.STOP_REQUESTED:
            return WatchRunResult(status=WatchStatus.STOPPED, reason_code=stop_reason)
        if stop_reason is not None:
            return WatchRunResult(
                status=WatchStatus.FAILED,
                reason_code=stop_reason,
                errors=1,
            )

        try:
            # This validates the explicit root and warms only BJ-03's private namespace.
            self._store.summary(account_id)
        except RecruitmentJobStoreError as error:
            return _archive_failure(error)
        except Exception:  # noqa: BLE001 - injected/storage failures are redacted.
            return WatchRunResult(
                status=WatchStatus.FAILED,
                reason_code=WatchReasonCode.ARCHIVE_FAILED,
                errors=1,
            )

        counts = _RunCounts()
        state: _Checkpoint | None = None
        try:
            with self._checkpoint_io.account_lock(account_id):
                loaded = self._checkpoint_io.load(account_id)
                checkpoint = _decode_checkpoint(loaded, account_id)
                state, already_complete = self._start_or_resume(
                    checkpoint,
                    account_id=account_id,
                    run_id=safe_run_id,
                    query=safe_query,
                    target_plan_sha256=_target_plan_sha256(safe_cycles),
                    max_cycles=max_cycles,
                    interval_seconds=interval_seconds,
                )
                if already_complete:
                    return WatchRunResult(
                        status=WatchStatus.COMPLETE,
                        completed_cycles=max_cycles,
                    )

                for cycle_offset in range(state.completed_cycles, max_cycles):
                    stop_reason = _stop_reason(safe_stop)
                    if stop_reason is not None:
                        return _interrupted_result(state, counts, stop_reason)

                    cycle = safe_cycles[cycle_offset]
                    state, pending_time, capture_id = self._pending_capture(
                        state,
                        account_id=account_id,
                        run_id=safe_run_id,
                        query=safe_query,
                        cycle_number=cycle_offset + 1,
                        selected_targets=cycle.selected_targets,
                        clock=safe_clock,
                        sleeper=safe_sleeper,
                        stop_signal=safe_stop,
                        capture_id_factory=id_factory,
                        interval_seconds=interval_seconds,
                    )
                    if pending_time is None or capture_id is None:
                        return _interrupted_result(
                            state,
                            counts,
                            WatchReasonCode.CHECKPOINT_FAILED,
                            errors=1,
                        )

                    try:
                        observed = observe_fake_job_feed(
                            cycle.session,
                            account_uuid=account_id,
                            query=safe_query,
                            selected_targets=cycle.selected_targets,
                            clock=safe_clock,
                            stop_signal=safe_stop,
                            capture_id=capture_id,
                            observed_at_utc=pending_time,
                            limits=_OBSERVATION_LIMITS,
                        )
                    except Exception:  # noqa: BLE001 - session failures are redacted.
                        return _interrupted_result(
                            state,
                            counts,
                            WatchReasonCode.OBSERVATION_FAILED,
                            errors=1,
                        )

                    counts.scanned += observed.examined_candidate_count
                    if observed.status is not ObservationRunStatus.COMPLETE:
                        counts.partial += sum(
                            item.status.value != "COMPLETE" for item in observed.observations
                        )
                        reason = _observation_reason(observed.reason_codes)
                        status = {
                            ObservationRunStatus.STOPPED: WatchStatus.STOPPED,
                            ObservationRunStatus.CAP_REACHED: WatchStatus.CAP_REACHED,
                            ObservationRunStatus.FAILED: WatchStatus.FAILED,
                        }.get(observed.status, WatchStatus.FAILED)
                        return _result_for_state(
                            state,
                            counts,
                            status,
                            reason,
                            errors=1 if status is WatchStatus.FAILED else 0,
                        )

                    for item in observed.observations:
                        stop_reason = _stop_reason(safe_stop)
                        if stop_reason is not None:
                            return _interrupted_result(state, counts, stop_reason)
                        if (
                            type(item) is not BrowserJobObservationV1
                            or item.account_uuid != account_id
                            or item.observed_at_utc != pending_time
                            or item.source != "SYNTHETIC_FIXTURE"
                            or item.text_provenance != "SYNTHETIC_FIXTURE_TEXT"
                        ):
                            return _interrupted_result(
                                state,
                                counts,
                                WatchReasonCode.ACCOUNT_MISMATCH,
                                errors=1,
                            )
                        extraction: ExtractionResult | None = None
                        try:
                            if item.visible_text is not None:
                                extraction = extract_vietnamese_vacancies(
                                    to_parser_observation(item)
                                )
                        except Exception:  # noqa: BLE001 - parser errors are redacted.
                            return _interrupted_result(
                                state,
                                counts,
                                WatchReasonCode.PARSER_FAILED,
                                errors=1,
                            )

                        stop_reason = _stop_reason(safe_stop)
                        if stop_reason is not None:
                            return _interrupted_result(state, counts, stop_reason)

                        try:
                            previous = self._store.summary(account_id)
                            stop_reason = _stop_reason(safe_stop)
                            if stop_reason is not None:
                                return _interrupted_result(state, counts, stop_reason)
                            receipt = self._store.record_observation(
                                item,
                                extraction,
                                now_utc=pending_time,
                            )
                        except RecruitmentJobStoreError as error:
                            return _interrupted_result(
                                state,
                                counts,
                                _archive_reason(error),
                                errors=1,
                            )
                        except Exception:  # noqa: BLE001 - disk/lock errors are redacted.
                            return _interrupted_result(
                                state,
                                counts,
                                WatchReasonCode.ARCHIVE_FAILED,
                                errors=1,
                            )

                        if receipt.status == "DUPLICATE":
                            counts.duplicate += 1
                        elif (
                            item.post_revision_id is not None
                            and receipt.revision_count == previous.revision_count
                        ):
                            # A new observation of an existing body is not a new vacancy.
                            counts.duplicate += 1
                        elif extraction is not None:
                            counts.vacancy += len(extraction.records)
                        if item.status.value != "COMPLETE":
                            counts.partial += 1

                    stop_reason = _stop_reason(safe_stop)
                    if stop_reason is not None:
                        return _interrupted_result(state, counts, stop_reason)

                    advanced = replace(
                        state,
                        completed_cycles=state.completed_cycles + 1,
                        last_observed_at_utc=_format_utc(pending_time),
                        pending_capture_id=None,
                        pending_observed_at_utc=None,
                        pending_targets_sha256=None,
                    )
                    self._checkpoint_io.save(account_id, advanced.document())
                    state = advanced

                return _result_for_state(
                    state,
                    counts,
                    WatchStatus.COMPLETE,
                    None,
                )
        except _WatchFailure as error:
            status = (
                WatchStatus.STOPPED
                if error.reason
                in (WatchReasonCode.STOP_REQUESTED, WatchReasonCode.INTERVAL_NOT_MET)
                else WatchStatus.FAILED
            )
            return _result_for_state(
                state,
                counts,
                status,
                error.reason,
                errors=0 if status is WatchStatus.STOPPED else 1,
            )
        except RecruitmentJobStoreError:
            return _result_for_state(
                state,
                counts,
                WatchStatus.FAILED,
                WatchReasonCode.CHECKPOINT_UNSAFE,
                errors=1,
            )
        except Exception:  # noqa: BLE001 - injected IO failures are redacted.
            return _result_for_state(
                state,
                counts,
                WatchStatus.FAILED,
                WatchReasonCode.CHECKPOINT_FAILED,
                errors=1,
            )

    def _start_or_resume(
        self,
        current: _Checkpoint | None,
        *,
        account_id: UUID,
        run_id: str,
        query: str,
        target_plan_sha256: str,
        max_cycles: int,
        interval_seconds: int,
    ) -> tuple[_Checkpoint, bool]:
        query_digest = _sha256(query.encode("utf-8"))
        if current is None:
            state = _Checkpoint(
                account_uuid=str(account_id),
                generation=1,
                run_id=run_id,
                query_sha256=query_digest,
                target_plan_sha256=target_plan_sha256,
                max_cycles=max_cycles,
                interval_seconds=interval_seconds,
                completed_cycles=0,
                last_observed_at_utc=None,
                pending_capture_id=None,
                pending_observed_at_utc=None,
                pending_targets_sha256=None,
                capture_id_sha256=(),
            )
            self._checkpoint_io.save(account_id, state.document())
            return state, False
        if current.account_uuid != str(account_id):
            raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
        if current.run_id == run_id:
            if (
                current.query_sha256 != query_digest
                or current.target_plan_sha256 != target_plan_sha256
                or current.max_cycles != max_cycles
                or current.interval_seconds != interval_seconds
            ):
                raise _WatchFailure(WatchReasonCode.CHECKPOINT_CONFLICT)
            return current, current.is_complete
        if not current.is_complete:
            raise _WatchFailure(WatchReasonCode.CHECKPOINT_CONFLICT)
        if current.generation >= MAX_CAPTURE_IDENTITIES:
            raise _WatchFailure(WatchReasonCode.CAPTURE_ID_REUSED)
        state = _Checkpoint(
            account_uuid=str(account_id),
            generation=current.generation + 1,
            run_id=run_id,
            query_sha256=query_digest,
            target_plan_sha256=target_plan_sha256,
            max_cycles=max_cycles,
            interval_seconds=interval_seconds,
            completed_cycles=0,
            last_observed_at_utc=current.last_observed_at_utc,
            pending_capture_id=None,
            pending_observed_at_utc=None,
            pending_targets_sha256=None,
            capture_id_sha256=current.capture_id_sha256,
        )
        self._checkpoint_io.save(account_id, state.document())
        return state, False

    def _pending_capture(
        self,
        state: _Checkpoint,
        *,
        account_id: UUID,
        run_id: str,
        query: str,
        cycle_number: int,
        selected_targets: tuple[str, ...],
        clock: WatchClock,
        sleeper: WatchSleeper,
        stop_signal: FakeStopSignal,
        capture_id_factory: CaptureIDFactory,
        interval_seconds: int,
    ) -> tuple[_Checkpoint, datetime | None, str | None]:
        targets_digest = _targets_sha256(selected_targets)
        if state.pending_capture_id is not None:
            if state.pending_targets_sha256 != targets_digest:
                raise _WatchFailure(WatchReasonCode.TARGET_CHANGED)
            pending_time = _parse_utc(state.pending_observed_at_utc)
            current_time = _clock_time(clock)
            previous_time = _parse_utc(state.last_observed_at_utc)
            if pending_time is None or current_time is None:
                raise _WatchFailure(WatchReasonCode.CLOCK_INVALID)
            if previous_time is not None and current_time < previous_time:
                raise _WatchFailure(WatchReasonCode.CLOCK_MOVED_BACKWARD)
            if current_time < pending_time:
                raise _WatchFailure(WatchReasonCode.CLOCK_MOVED_BACKWARD)
            return state, pending_time, state.pending_capture_id

        now = _clock_time(clock)
        if now is None:
            raise _WatchFailure(WatchReasonCode.CLOCK_INVALID)
        previous_time = _parse_utc(state.last_observed_at_utc)
        if previous_time is not None:
            if now < previous_time or (state.completed_cycles == 0 and now == previous_time):
                raise _WatchFailure(WatchReasonCode.CLOCK_MOVED_BACKWARD)
            if state.completed_cycles > 0:
                due = previous_time + timedelta(seconds=interval_seconds)
                if now < due:
                    stop_reason = _stop_reason(stop_signal)
                    if stop_reason is not None:
                        raise _WatchFailure(stop_reason)
                    try:
                        sleeper.sleep((due - now).total_seconds())
                    except Exception:  # noqa: BLE001 - sleeper errors are fixed and redacted.
                        raise _WatchFailure(WatchReasonCode.INTERVAL_NOT_MET) from None
                    stop_reason = _stop_reason(stop_signal)
                    if stop_reason is not None:
                        raise _WatchFailure(stop_reason)
                    now = _clock_time(clock)
                    if now is None:
                        raise _WatchFailure(WatchReasonCode.CLOCK_INVALID)
                    if now < previous_time:
                        raise _WatchFailure(WatchReasonCode.CLOCK_MOVED_BACKWARD)
                    if now < due:
                        raise _WatchFailure(WatchReasonCode.INTERVAL_NOT_MET)
        try:
            raw_capture_id = cast(
                object,
                capture_id_factory(
                    account_uuid=account_id,
                    run_id=run_id,
                    generation=state.generation,
                    cycle_number=cycle_number,
                    observed_at_utc=now,
                ),
            )
        except Exception:  # noqa: BLE001 - caller factory errors are redacted.
            raise _WatchFailure(WatchReasonCode.CAPTURE_ID_INVALID) from None
        if not isinstance(raw_capture_id, str) or _CAPTURE_ID_RE.fullmatch(raw_capture_id) is None:
            raise _WatchFailure(WatchReasonCode.CAPTURE_ID_INVALID)
        capture_id = raw_capture_id
        capture_digest = _sha256(capture_id.encode("ascii"))
        if capture_digest in state.capture_id_sha256:
            raise _WatchFailure(WatchReasonCode.CAPTURE_ID_REUSED)
        if len(state.capture_id_sha256) >= MAX_CAPTURE_IDENTITIES:
            raise _WatchFailure(WatchReasonCode.CAPTURE_ID_REUSED)
        pending = replace(
            state,
            pending_capture_id=capture_id,
            pending_observed_at_utc=_format_utc(now),
            pending_targets_sha256=targets_digest,
            capture_id_sha256=(*state.capture_id_sha256, capture_digest),
        )
        self._checkpoint_io.save(account_id, pending.document())
        return pending, now, capture_id


@dataclass(slots=True)
class _RunCounts:
    scanned: int = 0
    vacancy: int = 0
    duplicate: int = 0
    partial: int = 0


def _validate_run_inputs(
    *,
    account_uuid: UUID | None,
    query: str | None,
    run_id: str | None,
    cycles: Sequence[SyntheticCycleInput],
    max_cycles: int,
    interval_seconds: int,
    clock: WatchClock | None,
    sleeper: WatchSleeper | None,
    stop_signal: FakeStopSignal | None,
    capture_id_factory: CaptureIDFactory | None,
) -> (
    tuple[
        UUID,
        str,
        str,
        Sequence[SyntheticCycleInput],
        WatchClock,
        WatchSleeper,
        FakeStopSignal,
        CaptureIDFactory,
    ]
    | None
):
    if (
        type(account_uuid) is not UUID
        or not isinstance(query, str)
        or not query
        or len(query) > MAX_QUERY_CODEPOINTS
        or not isinstance(run_id, str)
        or _RUN_ID_RE.fullmatch(run_id) is None
        or type(max_cycles) is not int
        or not 1 <= max_cycles <= MAX_WATCH_CYCLES
        or type(interval_seconds) is not int
        or interval_seconds < (MIN_CYCLE_INTERVAL_SECONDS if max_cycles > 1 else 0)
        or interval_seconds > MAX_CYCLE_INTERVAL_SECONDS
        or isinstance(cast(object, cycles), (str, bytes))
        or not isinstance(cast(object, cycles), Sequence)
        or len(cycles) != max_cycles
        or clock is None
        or not callable(getattr(clock, "monotonic", None))
        or not callable(getattr(clock, "now_utc", None))
        or sleeper is None
        or not callable(getattr(sleeper, "sleep", None))
        or stop_signal is None
        or not callable(getattr(stop_signal, "is_set", None))
        or capture_id_factory is None
        or not callable(capture_id_factory)
    ):
        return None
    try:
        query.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    try:
        safe_cycles = tuple(cycles)
    except Exception:  # noqa: BLE001 - malformed caller sequences fail closed.
        return None
    if len(safe_cycles) != max_cycles:
        return None
    for cycle in safe_cycles:
        if (
            type(cycle) is not SyntheticCycleInput
            or cast(object, cycle.session) is None
            or isinstance(cast(object, cycle.selected_targets), (str, bytes))
            or not isinstance(cast(object, cycle.selected_targets), tuple)
            or len(cycle.selected_targets) > MAX_SELECTED_POSTS_PER_CYCLE
            or any(not isinstance(cast(object, target), str) for target in cycle.selected_targets)
            or any(not _is_safe_synthetic_target(target) for target in cycle.selected_targets)
        ):
            return None
    return (
        account_uuid,
        query,
        run_id,
        safe_cycles,
        clock,
        sleeper,
        stop_signal,
        capture_id_factory,
    )


def _is_safe_synthetic_target(value: object) -> bool:
    if not isinstance(value, str) or not _observation_fs._is_synthetic_post_url(value):
        return False
    path_and_query = value.removeprefix("https://threads.example.invalid")
    path = path_and_query.split("?", 1)[0].split("#", 1)[0]
    if "%" in path or "\\" in path:
        return False
    return all(segment not in {".", ".."} for segment in path.split("/"))


def _decode_checkpoint(value: object | None, account_id: UUID) -> _Checkpoint | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    raw_mapping = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw_mapping):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    document = cast(dict[str, object], raw_mapping)
    version = document.get("schema_version")
    if type(version) is not int or version != 1:
        raise _WatchFailure(WatchReasonCode.CHECKPOINT_VERSION_UNSUPPORTED)
    if frozenset(document) != _CHECKPOINT_KEYS:
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    account_value = document.get("account_uuid")
    generation = document.get("generation")
    run_id = document.get("run_id")
    query_digest = document.get("query_sha256")
    target_plan_digest = document.get("target_plan_sha256")
    max_cycles = document.get("max_cycles")
    interval_seconds = document.get("interval_seconds")
    completed_cycles = document.get("completed_cycles")
    last_observed = document.get("last_observed_at_utc")
    pending_id = document.get("pending_capture_id")
    pending_at = document.get("pending_observed_at_utc")
    pending_targets_digest = document.get("pending_targets_sha256")
    capture_hashes = document.get("capture_id_sha256")
    if (
        account_value != str(account_id)
        or type(generation) is not int
        or not 1 <= generation <= MAX_CAPTURE_IDENTITIES
        or not isinstance(run_id, str)
        or _RUN_ID_RE.fullmatch(run_id) is None
        or not isinstance(query_digest, str)
        or _SHA256_RE.fullmatch(query_digest) is None
        or not isinstance(target_plan_digest, str)
        or _SHA256_RE.fullmatch(target_plan_digest) is None
        or type(max_cycles) is not int
        or not 1 <= max_cycles <= MAX_WATCH_CYCLES
        or type(interval_seconds) is not int
        or interval_seconds < (MIN_CYCLE_INTERVAL_SECONDS if max_cycles > 1 else 0)
        or interval_seconds > MAX_CYCLE_INTERVAL_SECONDS
        or type(completed_cycles) is not int
        or not 0 <= completed_cycles <= max_cycles
        or not isinstance(capture_hashes, list)
    ):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    raw_capture_hashes = cast(list[object], capture_hashes)
    normalized_hashes: list[str] = []
    for item in raw_capture_hashes:
        if not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None:
            raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
        normalized_hashes.append(item)
    if len(normalized_hashes) > MAX_CAPTURE_IDENTITIES or len(set(normalized_hashes)) != len(
        normalized_hashes
    ):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    if last_observed is not None and (
        not isinstance(last_observed, str) or _parse_utc(last_observed) is None
    ):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    if (pending_id is None) != (pending_at is None) or (pending_id is None) != (
        pending_targets_digest is None
    ):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    if pending_id is not None and (
        not isinstance(pending_id, str)
        or _CAPTURE_ID_RE.fullmatch(pending_id) is None
        or not isinstance(pending_at, str)
        or _parse_utc(pending_at) is None
        or not isinstance(pending_targets_digest, str)
        or _SHA256_RE.fullmatch(pending_targets_digest) is None
        or completed_cycles >= max_cycles
        or _sha256(pending_id.encode("ascii")) not in normalized_hashes
    ):
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    if completed_cycles > 0 and last_observed is None:
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    if completed_cycles == max_cycles and pending_id is not None:
        raise _WatchFailure(WatchReasonCode.INVALID_CHECKPOINT)
    return _Checkpoint(
        account_uuid=str(account_id),
        generation=generation,
        run_id=run_id,
        query_sha256=query_digest,
        target_plan_sha256=target_plan_digest,
        max_cycles=max_cycles,
        interval_seconds=interval_seconds,
        completed_cycles=completed_cycles,
        last_observed_at_utc=last_observed if isinstance(last_observed, str) else None,
        pending_capture_id=pending_id if isinstance(pending_id, str) else None,
        pending_observed_at_utc=pending_at if isinstance(pending_at, str) else None,
        pending_targets_sha256=(
            pending_targets_digest if isinstance(pending_targets_digest, str) else None
        ),
        capture_id_sha256=tuple(normalized_hashes),
    )


def _clock_time(clock: WatchClock) -> datetime | None:
    try:
        raw_value = cast(object, clock.now_utc())
        if (
            not isinstance(raw_value, datetime)
            or raw_value.tzinfo is None
            or raw_value.utcoffset() != timedelta(0)
        ):
            return None
        value = raw_value
        return value.astimezone(UTC)
    except Exception:  # noqa: BLE001 - injected clock failures are fixed and redacted.
        return None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            return None
        normalized = parsed.astimezone(UTC)
        return normalized if _format_utc(normalized) == value else None
    except OverflowError, TypeError, ValueError:
        return None


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _stop_reason(stop_signal: FakeStopSignal) -> WatchReasonCode | None:
    try:
        value = stop_signal.is_set()
    except Exception:  # noqa: BLE001 - stop callback errors are fixed and redacted.
        return WatchReasonCode.STOP_SIGNAL_FAILURE
    if type(value) is not bool:
        return WatchReasonCode.STOP_SIGNAL_FAILURE
    return WatchReasonCode.STOP_REQUESTED if value else None


def _observation_reason(reasons: tuple[ObservationReasonCode, ...]) -> WatchReasonCode | str:
    if not reasons:
        return WatchReasonCode.OBSERVATION_FAILED
    reason = reasons[0]
    if reason in (
        ObservationReasonCode.STOP_REQUESTED,
        ObservationReasonCode.TIMEOUT,
    ):
        return WatchReasonCode.STOP_REQUESTED
    if reason in (
        ObservationReasonCode.BATCH_LIMIT_REACHED,
        ObservationReasonCode.TRANCHE_LIMIT_REACHED,
        ObservationReasonCode.CANDIDATE_LIMIT_REACHED,
        ObservationReasonCode.DETAIL_LIMIT_REACHED,
        ObservationReasonCode.TEXT_LIMIT_REACHED,
    ):
        return WatchReasonCode.ARCHIVE_LIMIT_REACHED
    if reason is ObservationReasonCode.ACCOUNT_MISMATCH:
        return WatchReasonCode.ACCOUNT_MISMATCH
    if reason in (
        ObservationReasonCode.TARGET_MOVED,
        ObservationReasonCode.WRONG_ORIGIN,
        ObservationReasonCode.REDIRECT_DETECTED,
    ):
        return WatchReasonCode.TARGET_CHANGED
    if reason in (
        ObservationReasonCode.SESSION_FAILURE,
        ObservationReasonCode.UNCLASSIFIED_STATE,
        ObservationReasonCode.AUTHOR_MISMATCH,
    ):
        return WatchReasonCode.OBSERVATION_FAILED
    try:
        return WatchReasonCode(reason.value)
    except ValueError:
        pass
    return WatchReasonCode.OBSERVATION_FAILED


def _archive_reason(error: RecruitmentJobStoreError) -> WatchReasonCode:
    if error.code is StoreErrorCode.LIMIT_REACHED:
        return WatchReasonCode.ARCHIVE_LIMIT_REACHED
    if error.code is StoreErrorCode.ACCOUNT_MISMATCH:
        return WatchReasonCode.ACCOUNT_MISMATCH
    return WatchReasonCode.ARCHIVE_FAILED


def _archive_failure(error: RecruitmentJobStoreError) -> WatchRunResult:
    return WatchRunResult(
        status=WatchStatus.FAILED,
        reason_code=_archive_reason(error),
        errors=1,
    )


def _interrupted_result(
    state: _Checkpoint,
    counts: _RunCounts,
    reason: WatchReasonCode | str,
    *,
    errors: int = 0,
) -> WatchRunResult:
    status = (
        WatchStatus.STOPPED
        if reason in (WatchReasonCode.STOP_REQUESTED, WatchReasonCode.INTERVAL_NOT_MET)
        else WatchStatus.FAILED
    )
    return _result_for_state(state, counts, status, reason, errors=errors)


def _result_for_state(
    state: _Checkpoint | None,
    counts: _RunCounts,
    status: WatchStatus,
    reason: WatchReasonCode | str | None,
    *,
    errors: int = 0,
) -> WatchRunResult:
    completed = 0 if state is None else state.completed_cycles
    due: datetime | None = None
    if state is not None and not state.is_complete and state.last_observed_at_utc is not None:
        last = _parse_utc(state.last_observed_at_utc)
        if last is not None:
            due = last + timedelta(seconds=state.interval_seconds)
    return WatchRunResult(
        status=status,
        reason_code=reason,
        scanned=counts.scanned,
        vacancy=counts.vacancy,
        duplicate=counts.duplicate,
        partial=counts.partial,
        errors=errors,
        completed_cycles=completed,
        next_due_utc=due,
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _targets_sha256(targets: tuple[str, ...]) -> str:
    encoded = json.dumps(targets, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return _sha256(encoded)


def _target_plan_sha256(cycles: Sequence[SyntheticCycleInput]) -> str:
    target_plan = tuple(cycle.selected_targets for cycle in cycles)
    encoded = json.dumps(target_plan, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return _sha256(encoded)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("INVALID_JSON_CONSTANT")
