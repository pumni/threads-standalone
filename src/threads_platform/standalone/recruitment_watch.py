"""Read-only foreground polling for recruitment observation."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID

from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone.nurture_store import NurtureStateError, NurtureStore

DEFAULT_WATCH_INTERVAL_MINUTES = 30
MIN_WATCH_INTERVAL_MINUTES = 15
MAX_WATCH_INTERVAL_MINUTES = 1_440
MAX_WATCH_CYCLES = 1_000
WATCH_PHASE_TIMEOUT_SECONDS = 300

_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_DECISION = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_MAX_SUMMARY_COUNTER = 1_000_000
_INBOUND_STATUSES = frozenset(
    {
        "INBOUND_READY",
        "INBOUND_NOT_CONFIGURED",
        "INBOUND_ACCOUNT_MISMATCH",
        "INBOUND_OWNERSHIP_MISMATCH",
        "INBOUND_OWNERSHIP_UNVERIFIED",
        "INBOUND_PERMISSION_MISSING",
        "INBOUND_READ_FAILED",
        "INBOUND_CONFIGURATION_INVALID",
    }
)


class WatchTiming(Protocol):
    """Clock and sleeper injection point for deterministic synthetic tests."""

    def monotonic(self) -> float: ...

    def utc_now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemWatchTiming:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class WatchPreflight:
    status: str
    credential_status: str | None
    publishing_quota_status: str | None
    reply_quota_status: str | None


@dataclass(frozen=True, slots=True)
class WatchCycle:
    status: str
    outcome: str
    inbound_status: str
    discovered_count: int
    deduped_count: int
    selected_count: int
    skipped_count: int
    decision_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WatchRunResult:
    status: Literal["COMPLETED", "STOP", "INTERRUPTED"]
    cycles: int
    reason_code: str | None = None


class WatchLockError(Exception):
    """Safe local watcher-lock failure."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_CODE.fullmatch(code) is not None else "WATCH_LOCK_UNAVAILABLE"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"WatchLockError(code={self.code})"


class AccountRecruitmentWatchLock:
    """Exclusive foreground watcher guard, separate from per-cycle Nurture locks."""

    def __init__(self, store: NurtureStore, account_id: UUID) -> None:
        if type(account_id) is not UUID or account_id.version != 4:
            raise WatchLockError("WATCH_LOCK_UNAVAILABLE")
        self._store = store
        self._account_id = account_id
        self._lock: FilesystemProcessLock | None = None

    @property
    def held(self) -> bool:
        return self._lock is not None and self._lock.held

    def acquire(self) -> Self:
        if self.held:
            raise WatchLockError("WATCH_LOCK_UNAVAILABLE")
        try:
            directory = self._store.lock_directory()
            path = directory / f"{self._account_id}.watch.lock"
            self._prepare_lock_file(path, directory)
            lock = FilesystemProcessLock(path)
            lock.acquire()
        except ProcessAlreadyRunning:
            raise WatchLockError("WATCH_ALREADY_RUNNING") from None
        except NurtureStateError, OSError:
            raise WatchLockError("WATCH_LOCK_UNAVAILABLE") from None

        try:
            self._store.validate_lock_file(path, directory)
            self._lock = lock
        except BaseException:  # noqa: BLE001 — release the process lock before propagating stop.
            lock.release()
            raise WatchLockError("WATCH_LOCK_UNAVAILABLE") from None
        return self

    def release(self) -> None:
        lock = self._lock
        if lock is None:
            return
        self._lock = None
        try:
            lock.release()
        except OSError:
            return

    def _prepare_lock_file(self, path: Path, parent: Path) -> None:
        try:
            path.lstat()
        except FileNotFoundError:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            except OSError:
                raise WatchLockError("WATCH_LOCK_UNAVAILABLE") from None
            else:
                try:
                    os.close(descriptor)
                except OSError:
                    raise WatchLockError("WATCH_LOCK_UNAVAILABLE") from None
        except OSError:
            raise WatchLockError("WATCH_LOCK_UNAVAILABLE") from None
        try:
            self._store.validate_lock_file(path, parent)
        except NurtureStateError:
            raise WatchLockError("WATCH_LOCK_UNAVAILABLE") from None

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def parse_watch_interval_minutes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("watch interval must be an integer from 15 to 1440 minutes") from None
    if not MIN_WATCH_INTERVAL_MINUTES <= value <= MAX_WATCH_INTERVAL_MINUTES:
        raise ValueError("watch interval must be an integer from 15 to 1440 minutes")
    return value


def parse_watch_max_cycles(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("max cycles must be an integer from 1 to 1000") from None
    if not 1 <= value <= MAX_WATCH_CYCLES:
        raise ValueError("max cycles must be an integer from 1 to 1000")
    return value


async def run_foreground_watch(
    *,
    store: NurtureStore,
    account_id: UUID,
    interval_minutes: int,
    max_cycles: int | None,
    preflight: Callable[[], Awaitable[WatchPreflight]],
    observe: Callable[[], Awaitable[WatchCycle]],
    timing: WatchTiming | None = None,
    reporter: Callable[[str], None] | None = None,
) -> WatchRunResult:
    """Poll through the existing session callbacks; stop on any unclassified state."""

    interval = parse_watch_interval_minutes(str(interval_minutes))
    cycles_limit = None if max_cycles is None else parse_watch_max_cycles(str(max_cycles))
    clock = timing if timing is not None else _SystemWatchTiming()
    emit = reporter if reporter is not None else _stdout_report
    lock = AccountRecruitmentWatchLock(store, account_id)
    cycles = 0
    active_phase = "lock"
    active_cycle: int | None = None
    status: Literal["COMPLETED", "STOP", "INTERRUPTED"] = "STOP"
    reason: str | None = None

    try:
        lock.acquire()
        active_phase = "preflight"
        try:
            async with asyncio.timeout(WATCH_PHASE_TIMEOUT_SECONDS):
                readiness = await preflight()
        except TimeoutError:
            reason = "WATCH_PREFLIGHT_TIMEOUT"
            _try_report(emit, f"watch preflight status=STOP reason={reason}")
        else:
            if not _valid_preflight(readiness):
                reason = "WATCH_PREFLIGHT_INVALID"
                _try_report(emit, f"watch preflight status=STOP reason={reason}")
            else:
                if not _try_report(emit, _format_preflight(readiness)):
                    reason = "WATCH_OUTPUT_UNAVAILABLE"
                elif readiness.status != "OBSERVED":
                    reason = "PREFLIGHT_STOP"
                else:
                    status = "COMPLETED"
                    while cycles_limit is None or cycles < cycles_limit:
                        started = clock.monotonic()
                        active_phase = "observe"
                        active_cycle = cycles + 1
                        cycles += 1
                        try:
                            async with asyncio.timeout(WATCH_PHASE_TIMEOUT_SECONDS):
                                observation = await observe()
                        except TimeoutError:
                            reason = "WATCH_CYCLE_TIMEOUT"
                            status = "STOP"
                            _try_report(
                                emit,
                                f"watch cycle={active_cycle} status=STOP reason={reason} "
                                "inbound=UNAVAILABLE",
                            )
                            active_cycle = None
                            break

                        if not _valid_cycle(observation):
                            reason = "WATCH_RESULT_INVALID"
                            status = "STOP"
                            _try_report(
                                emit,
                                f"watch cycle={active_cycle} status=STOP reason={reason} "
                                "inbound=UNAVAILABLE",
                            )
                            active_cycle = None
                            break

                        if not _try_report(
                            emit,
                            _format_cycle(active_cycle, observation, clock.utc_now()),
                        ):
                            reason = "WATCH_OUTPUT_UNAVAILABLE"
                            status = "STOP"
                            active_cycle = None
                            break
                        active_cycle = None
                        if observation.status != "SUCCESS" or observation.outcome != "SUCCESS":
                            reason = "WATCH_OBSERVE_STOP"
                            status = "STOP"
                            break

                        if cycles_limit is not None and cycles >= cycles_limit:
                            break
                        remaining = started + interval * 60 - clock.monotonic()
                        if remaining > 0:
                            active_phase = "sleep"
                            await clock.sleep(remaining)
                    else:
                        status = "COMPLETED"
    except asyncio.CancelledError:
        status = "INTERRUPTED"
        reason = "INTERRUPTED"
        if active_cycle is not None:
            _try_report(
                emit,
                f"watch cycle={active_cycle} status=INTERRUPTED reason=INTERRUPTED "
                "inbound=UNAVAILABLE",
            )
    except KeyboardInterrupt:
        status = "INTERRUPTED"
        reason = "INTERRUPTED"
        if active_cycle is not None:
            _try_report(
                emit,
                f"watch cycle={active_cycle} status=INTERRUPTED reason=INTERRUPTED "
                "inbound=UNAVAILABLE",
            )
    except Exception as error:  # noqa: BLE001 — reports contain only fixed safe codes.
        status = "STOP"
        reason = _error_code(error)
        if active_phase == "preflight":
            _try_report(emit, f"watch preflight status=STOP reason={reason}")
        elif active_cycle is not None:
            _try_report(
                emit,
                f"watch cycle={active_cycle} status=STOP reason={reason} inbound=UNAVAILABLE",
            )
    finally:
        lock.release()

    _try_report(emit, _format_shutdown(status, cycles, reason))
    return WatchRunResult(status, cycles, reason)


def _valid_preflight(value: object) -> bool:
    valid = (
        type(value) is WatchPreflight
        and value.status in {"OBSERVED", "STOP"}
        and value.credential_status in {"verified", "unverified", None}
        and value.publishing_quota_status in {"available", "exhausted", "unverified", None}
        and value.reply_quota_status in {"available", "exhausted", "unverified", None}
    )
    if not valid or type(value) is not WatchPreflight:
        return False
    return value.status != "OBSERVED" or (
        value.credential_status == "verified"
        and value.publishing_quota_status == "available"
        and value.reply_quota_status == "available"
    )


def _valid_cycle(value: object) -> bool:
    return (
        type(value) is WatchCycle
        and value.status in {"SUCCESS", "FAILED", "INTERRUPTED", "AMBIGUOUS"}
        and value.outcome in {"SUCCESS", "FAILED", "INTERRUPTED", "AMBIGUOUS"}
        and value.inbound_status in _INBOUND_STATUSES
        and all(
            type(count) is int and 0 <= count <= _MAX_SUMMARY_COUNTER
            for count in (
                value.discovered_count,
                value.deduped_count,
                value.selected_count,
                value.skipped_count,
            )
        )
        and type(value.decision_codes) is tuple
        and len(value.decision_codes) <= 32
        and all(
            type(code) is str and _SAFE_DECISION.fullmatch(code) is not None
            for code in value.decision_codes
        )
    )


def _format_preflight(readiness: WatchPreflight) -> str:
    return (
        f"watch preflight status={readiness.status} "
        f"credential={readiness.credential_status or 'unverified'} "
        f"post_quota={readiness.publishing_quota_status or 'unverified'} "
        f"reply_quota={readiness.reply_quota_status or 'unverified'}"
    )


def _format_cycle(cycle_number: int, observation: WatchCycle, timestamp: datetime) -> str:
    safe_time = timestamp.astimezone(UTC).replace(microsecond=0).isoformat()
    decisions = ",".join(observation.decision_codes) or "NO_ACTION"
    return (
        f"watch cycle={cycle_number} at={safe_time} status={observation.status} "
        f"outcome={observation.outcome} discovered={observation.discovered_count} "
        f"deduped={observation.deduped_count} selected={observation.selected_count} "
        f"skipped={observation.skipped_count} decisions={decisions} "
        f"inbound={observation.inbound_status}"
    )


def _format_shutdown(
    status: Literal["COMPLETED", "STOP", "INTERRUPTED"],
    cycles: int,
    reason: str | None,
) -> str:
    suffix = "" if reason is None else f" reason={reason}"
    return f"watch shutdown status={status} cycles={cycles}{suffix}"


def _error_code(error: object) -> str:
    code = getattr(error, "code", None)
    return code if type(code) is str and _SAFE_CODE.fullmatch(code) is not None else "WATCH_FAILED"


def _try_report(reporter: Callable[[str], None], line: str) -> bool:
    try:
        reporter(line)
    except Exception:  # noqa: BLE001 — output errors are not safe to expose.
        return False
    return True


def _stdout_report(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


__all__ = [
    "DEFAULT_WATCH_INTERVAL_MINUTES",
    "MAX_WATCH_CYCLES",
    "MAX_WATCH_INTERVAL_MINUTES",
    "MIN_WATCH_INTERVAL_MINUTES",
    "WATCH_PHASE_TIMEOUT_SECONDS",
    "AccountRecruitmentWatchLock",
    "WatchCycle",
    "WatchPreflight",
    "WatchRunResult",
    "WatchTiming",
    "parse_watch_interval_minutes",
    "parse_watch_max_cycles",
    "run_foreground_watch",
]
