from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import threads_platform.standalone.recruitment_watch as recruitment_watch_module
from threads_platform.standalone.nurture_store import NurtureStateError, NurtureStore
from threads_platform.standalone.recruitment_watch import (
    AccountRecruitmentWatchLock,
    WatchCycle,
    WatchLockError,
    WatchPreflight,
    WatchRunResult,
    parse_watch_interval_minutes,
    parse_watch_max_cycles,
    run_foreground_watch,
)


@dataclass
class _FakeTiming:
    current: datetime = field(default_factory=lambda: datetime(2026, 10, 11, tzinfo=UTC))
    monotonic_value: float = 0.0
    interrupt_on_sleep: bool = False
    sleep_calls: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.monotonic_value

    def utc_now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        if self.interrupt_on_sleep:
            raise asyncio.CancelledError
        self.monotonic_value += seconds
        self.current += timedelta(seconds=seconds)


def _ready() -> WatchPreflight:
    return WatchPreflight("OBSERVED", "verified", "available", "available")


def _cycle(*, inbound: str = "INBOUND_NOT_CONFIGURED") -> WatchCycle:
    return WatchCycle(
        status="SUCCESS",
        outcome="SUCCESS",
        inbound_status=inbound,
        discovered_count=1,
        deduped_count=1,
        selected_count=0,
        skipped_count=1,
        decision_codes=("NO_ACTION",),
    )


def test_watch_interval_and_cycle_bounds() -> None:
    assert parse_watch_interval_minutes("15") == 15
    assert parse_watch_interval_minutes("1440") == 1440
    assert parse_watch_max_cycles("1") == 1
    assert parse_watch_max_cycles("1000") == 1000

    for raw in ("14", "1441", "0", "-1", "x"):
        with pytest.raises(ValueError):
            parse_watch_interval_minutes(raw)
    for raw in ("0", "1001", "-1", "x"):
        with pytest.raises(ValueError):
            parse_watch_max_cycles(raw)


def test_finite_watch_uses_bounded_reports_and_skips_catch_up(tmp_path: Path) -> None:
    timing = _FakeTiming()
    reports: list[str] = []
    account_id = uuid4()
    calls = {"preflight": 0, "observe": 0}

    async def preflight() -> WatchPreflight:
        calls["preflight"] += 1
        return _ready()

    async def observe() -> WatchCycle:
        calls["observe"] += 1
        return _cycle()

    result = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(tmp_path),
            account_id=account_id,
            interval_minutes=15,
            max_cycles=3,
            preflight=preflight,
            observe=observe,
            timing=timing,
            reporter=reports.append,
        )
    )

    assert result == WatchRunResult("COMPLETED", 3)
    assert calls == {"preflight": 1, "observe": 3}
    assert timing.sleep_calls == [900, 900]
    assert sum(line.startswith("watch cycle=") for line in reports) == 3
    assert reports[0] == (
        "watch preflight status=OBSERVED credential=verified "
        "post_quota=available reply_quota=available"
    )
    assert reports[-1] == "watch shutdown status=COMPLETED cycles=3"
    assert all("INBOUND_NOT_CONFIGURED" in line for line in reports if "watch cycle=" in line)
    assert all("synthetic" not in line and "token" not in line for line in reports)
    lock_path = tmp_path / "nurture" / "locks" / f"{account_id}.watch.lock"
    assert lock_path.is_file()


def test_preflight_stop_and_read_failure_never_retry(tmp_path: Path) -> None:
    reports: list[str] = []
    observes = 0

    async def stopped_preflight() -> WatchPreflight:
        return WatchPreflight("STOP", "verified", "exhausted", "available")

    async def forbidden_observe() -> WatchCycle:
        pytest.fail("preflight STOP must not reach discovery")

    stopped = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(tmp_path / "quota-root"),
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=3,
            preflight=stopped_preflight,
            observe=forbidden_observe,
            reporter=reports.append,
        )
    )
    assert stopped.status == "STOP"
    assert stopped.reason_code == "PREFLIGHT_STOP"
    assert reports[0] == (
        "watch preflight status=STOP credential=verified post_quota=exhausted reply_quota=available"
    )
    assert reports[-1] == "watch shutdown status=STOP cycles=0 reason=PREFLIGHT_STOP"

    class _ReadFailure(Exception):
        code = "THREADS_RATE_LIMITED"

    async def ready_preflight() -> WatchPreflight:
        return _ready()

    async def failed_observe() -> WatchCycle:
        nonlocal observes
        observes += 1
        raise _ReadFailure("synthetic private API detail")

    failure_reports: list[str] = []
    failed = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(tmp_path / "failure-root"),
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=3,
            preflight=ready_preflight,
            observe=failed_observe,
            timing=_FakeTiming(),
            reporter=failure_reports.append,
        )
    )
    assert failed.status == "STOP"
    assert failed.reason_code == "THREADS_RATE_LIMITED"
    assert observes == 1
    assert failure_reports[-2] == (
        "watch cycle=1 status=STOP reason=THREADS_RATE_LIMITED inbound=UNAVAILABLE"
    )
    assert failure_reports[-1] == (
        "watch shutdown status=STOP cycles=1 reason=THREADS_RATE_LIMITED"
    )
    assert all("private API detail" not in line for line in failure_reports)


def test_watch_phase_timeout_stops_without_fast_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recruitment_watch_module, "WATCH_PHASE_TIMEOUT_SECONDS", 0.01)
    timing = _FakeTiming()
    calls = 0
    reports: list[str] = []

    async def preflight() -> WatchPreflight:
        return _ready()

    async def slow_observe() -> WatchCycle:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return _cycle()

    result = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(tmp_path),
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=3,
            preflight=preflight,
            observe=slow_observe,
            timing=timing,
            reporter=reports.append,
        )
    )

    assert result.status == "STOP"
    assert result.reason_code == "WATCH_CYCLE_TIMEOUT"
    assert calls == 1
    assert timing.sleep_calls == []
    assert reports[-2] == (
        "watch cycle=1 status=STOP reason=WATCH_CYCLE_TIMEOUT inbound=UNAVAILABLE"
    )


def test_cancel_between_cycles_reports_interruption_and_does_not_start_next_cycle(
    tmp_path: Path,
) -> None:
    timing = _FakeTiming(interrupt_on_sleep=True)
    reports: list[str] = []
    observes = 0

    async def preflight() -> WatchPreflight:
        return _ready()

    async def observe() -> WatchCycle:
        nonlocal observes
        observes += 1
        return _cycle()

    result = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(tmp_path),
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=3,
            preflight=preflight,
            observe=observe,
            timing=timing,
            reporter=reports.append,
        )
    )

    assert result.status == "INTERRUPTED"
    assert result.cycles == 1
    assert observes == 1
    assert reports[-1] == "watch shutdown status=INTERRUPTED cycles=1 reason=INTERRUPTED"


def test_watch_lock_path_error_fails_closed_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = NurtureStore(tmp_path)
    account_id = uuid4()
    path = tmp_path / "nurture" / "locks" / f"{account_id}.watch.lock"
    original_validate = store.validate_lock_file

    def reject_path(_path: Path, _parent: Path) -> None:
        raise NurtureStateError("NURTURE_STATE_INVALID")

    monkeypatch.setattr(store, "validate_lock_file", reject_path)
    reports: list[str] = []
    preflight_calls = 0

    async def preflight() -> WatchPreflight:
        nonlocal preflight_calls
        preflight_calls += 1
        return _ready()

    async def observe() -> WatchCycle:
        pytest.fail("an unsafe watch lock path must stop before API reads")

    result = asyncio.run(
        run_foreground_watch(
            store=store,
            account_id=account_id,
            interval_minutes=15,
            max_cycles=1,
            preflight=preflight,
            observe=observe,
            reporter=reports.append,
        )
    )
    monkeypatch.setattr(store, "validate_lock_file", original_validate)

    assert result.status == "STOP"
    assert result.reason_code == "WATCH_LOCK_UNAVAILABLE"
    assert preflight_calls == 0
    assert reports[-1] == "watch shutdown status=STOP cycles=0 reason=WATCH_LOCK_UNAVAILABLE"
    assert path.is_file()


def test_watch_module_comes_from_installed_wheel_in_wheel_smoke() -> None:
    import os

    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1":
        assert "site-packages" in str(Path(recruitment_watch_module.__file__).resolve()).casefold()


@pytest.mark.asyncio
async def test_watch_lock_is_per_account_and_restart_keeps_lock_file(tmp_path: Path) -> None:
    store = NurtureStore(tmp_path)
    first_account = uuid4()
    second_account = uuid4()
    first = AccountRecruitmentWatchLock(store, first_account).acquire()
    first_path = tmp_path / "nurture" / "locks" / f"{first_account}.watch.lock"

    with pytest.raises(WatchLockError) as busy:
        AccountRecruitmentWatchLock(store, first_account).acquire()
    assert busy.value.code == "WATCH_ALREADY_RUNNING"

    second = AccountRecruitmentWatchLock(store, second_account).acquire()
    assert first.held and second.held
    second.release()
    first.release()
    assert first_path.is_file()

    restarted = AccountRecruitmentWatchLock(store, first_account).acquire()
    assert restarted.held
    restarted.release()


@pytest.mark.asyncio
async def test_cancelled_read_releases_account_guard_and_other_account_progresses(
    tmp_path: Path,
) -> None:
    store = NurtureStore(tmp_path)
    account_id = uuid4()
    other_account_id = uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def ready() -> WatchPreflight:
        return _ready()

    async def success_observe() -> WatchCycle:
        return _cycle()

    async def blocked_observe() -> WatchCycle:
        entered.set()
        await release.wait()
        return _cycle()

    pending = asyncio.create_task(
        run_foreground_watch(
            store=store,
            account_id=account_id,
            interval_minutes=15,
            max_cycles=1,
            preflight=ready,
            observe=blocked_observe,
            reporter=lambda _line: None,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    async def forbidden_preflight() -> WatchPreflight:
        pytest.fail("a second same-account watcher must fail before preflight")

    same_account = await run_foreground_watch(
        store=store,
        account_id=account_id,
        interval_minutes=15,
        max_cycles=1,
        preflight=forbidden_preflight,
        observe=blocked_observe,
        reporter=lambda _line: None,
    )
    assert same_account.status == "STOP"
    assert same_account.reason_code == "WATCH_ALREADY_RUNNING"

    other_account = await run_foreground_watch(
        store=store,
        account_id=other_account_id,
        interval_minutes=15,
        max_cycles=1,
        preflight=ready,
        observe=success_observe,
        reporter=lambda _line: None,
    )
    assert other_account.status == "COMPLETED"
    assert other_account.cycles == 1

    pending.cancel()
    interrupted = await pending
    assert interrupted.status == "INTERRUPTED"
    assert interrupted.reason_code == "INTERRUPTED"

    restart = await run_foreground_watch(
        store=store,
        account_id=account_id,
        interval_minutes=15,
        max_cycles=1,
        preflight=ready,
        observe=success_observe,
        reporter=lambda _line: None,
    )
    assert restart.status == "COMPLETED"
