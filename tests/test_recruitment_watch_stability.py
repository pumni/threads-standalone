from __future__ import annotations

import asyncio
import gc
import importlib.util
import os
import subprocess
import sys
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from threads_platform.standalone.nurture_store import NurtureStateError, NurtureStore
from threads_platform.standalone.recruitment_watch import (
    AccountRecruitmentWatchLock,
    WatchCycle,
    WatchPreflight,
    run_foreground_watch,
)

_ROOT = Path(__file__).resolve().parents[1]
_DEMO_SCRIPT = _ROOT / "scripts" / "demo_recruitment_watch.py"
_PRIVATE_MARKERS = (
    "AO02_SYNTHETIC_TOKEN_MARKER",
    "ao02_synthetic_target_001",
    "ao02_synthetic_author_001",
    "Synthetic career question about hiring",
    "synthetic_owner",
)


def _load_demo_module() -> Any:
    name = "_threads_standalone_ao02_demo"
    spec = importlib.util.spec_from_file_location(name, _DEMO_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") == "1",
    reason="source-only demo script is not shipped in the installed wheel",
)
def test_demo_is_a_one_command_synthetic_read_only_run(tmp_path: Path) -> None:
    temporary_home = tmp_path / "synthetic-home"
    temporary_home.mkdir()
    ignored_root = tmp_path / "operator-root-must-not-be-used"
    token_marker = "AO02_SYNTHETIC_TOKEN_MARKER"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(temporary_home),
        "USERPROFILE": str(temporary_home),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "THREADS_LOCAL_DATA_ROOT": str(ignored_root),
        "THREADS_PLATFORM_THREADS_TOKEN_AO02_SYNTHETIC": token_marker,
        "UV_PYTHON": "3.14",
        "PYTHONUTF8": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
        if name in os.environ:
            environment[name] = os.environ[name]

    completed = subprocess.run(
        ["uv", "run", "--locked", "python", "scripts/demo_recruitment_watch.py", "--cycles", "3"],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "demo start backend=FAKE identity=FAKE cycles=3" in completed.stdout
    assert "watch preflight status=OBSERVED credential=verified" in completed.stdout
    assert completed.stdout.count("watch cycle=") == 3
    cycle_lines = [
        line for line in completed.stdout.splitlines() if line.startswith("watch cycle=")
    ]
    assert "discovered=1" in cycle_lines[0] and "selected=1" in cycle_lines[0]
    assert "deduped=1" in cycle_lines[1] and "selected=0" in cycle_lines[1]
    assert "inbound=INBOUND_NOT_CONFIGURED" in cycle_lines[0]
    assert "watch shutdown status=COMPLETED cycles=3" in completed.stdout
    assert "demo storage cycles=3 receipts=3" in completed.stdout
    assert "receipt_bytes=" in completed.stdout and "target_state_bytes=" in completed.stdout
    assert "demo safety mutation_runtime=DISABLED remote_mutations=0 credentials=NONE" in (
        completed.stdout
    )
    assert not ignored_root.exists()
    assert str(ignored_root) not in output
    assert str(temporary_home) not in output
    assert all(marker not in output for marker in _PRIVATE_MARKERS)


def test_demo_reports_receipt_and_target_state_growth_by_cycle_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    demo = _load_demo_module()
    measurements: list[tuple[int, int, int, int]] = []
    for cycles in (3, 10, 100):
        lines: list[str] = []
        summary = demo.run_demo(cycles, lines.append)
        assert summary.status == "COMPLETED"
        assert summary.cycles == cycles
        assert summary.receipt_count == cycles
        assert summary.receipt_bytes > 0
        assert summary.target_state_bytes > 0
        assert summary.fake_wait_seconds == 900 * (cycles - 1)
        assert (
            lines[-1] == "demo safety mutation_runtime=DISABLED remote_mutations=0 credentials=NONE"
        )
        assert all(marker not in "\n".join(lines) for marker in _PRIVATE_MARKERS)
        measurements.append(
            (cycles, summary.receipt_count, summary.receipt_bytes, summary.target_state_bytes)
        )

    assert [row[1] for row in measurements] == [3, 10, 100]
    assert measurements[0][2] < measurements[1][2] < measurements[2][2]
    assert len({row[3] for row in measurements}) == 1
    with capsys.disabled():
        print(
            f"AO-02 synthetic storage measurements cycles/count/receipt-bytes/state-bytes={measurements}"
        )


class _TraceTiming:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.now = datetime(2026, 10, 11, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.monotonic_value

    def utc_now(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def test_watch_coordinator_retained_tracemalloc_stays_bounded_for_1000_cycles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "synthetic-watch-root"
    root.mkdir()
    timing = _TraceTiming()
    cycles = 0
    traced_samples: dict[int, int] = {}

    async def preflight() -> WatchPreflight:
        return WatchPreflight("OBSERVED", "verified", "available", "available")

    async def observe() -> WatchCycle:
        nonlocal cycles
        cycles += 1
        return WatchCycle("SUCCESS", "SUCCESS", "INBOUND_NOT_CONFIGURED", 1, 1, 0, 1, ())

    def report(line: str) -> None:
        if line.startswith("watch cycle="):
            cycle_number = int(line.split("=", maxsplit=1)[1].split(" ", maxsplit=1)[0])
            if cycle_number in {3, 10, 100, 1_000}:
                traced_samples[cycle_number] = tracemalloc.get_traced_memory()[0]

    tracing_was_enabled = tracemalloc.is_tracing()
    if not tracing_was_enabled:
        tracemalloc.start()
    try:
        result = asyncio.run(
            run_foreground_watch(
                store=NurtureStore(root),
                account_id=uuid4(),
                interval_minutes=15,
                max_cycles=1_000,
                preflight=preflight,
                observe=observe,
                timing=timing,
                reporter=report,
            )
        )
        gc.collect()
        assert result.status == "COMPLETED"
        assert result.cycles == cycles == 1_000
        assert set(traced_samples) == {3, 10, 100, 1_000}
        growth = max(traced_samples.values()) - min(traced_samples.values())
        assert growth <= 128 * 1024, f"traced retained-memory samples: {traced_samples}"
        with capsys.disabled():
            print(f"AO-02 coordinator traced-current-by-cycles={traced_samples}")
    finally:
        if not tracing_was_enabled:
            tracemalloc.stop()


@pytest.mark.parametrize(
    ("phase", "code"),
    [
        ("preflight", "THREADS_CREDENTIAL_EXPIRED"),
        ("observe", "THREADS_RATE_LIMITED"),
        ("observe", "THREADS_TRANSPORT_FAILURE"),
    ],
)
def test_read_failures_stop_once_without_busy_loop(
    tmp_path: Path,
    phase: str,
    code: str,
) -> None:
    root = tmp_path / f"{phase}-{code}"
    root.mkdir()
    preflight_calls = 0
    observe_calls = 0
    sleep_calls: list[float] = []

    class _SyntheticReadFailure(Exception):
        def __init__(self, error_code: str) -> None:
            self.code = error_code
            super().__init__("synthetic private detail")

    async def preflight() -> WatchPreflight:
        nonlocal preflight_calls
        preflight_calls += 1
        if phase == "preflight":
            raise _SyntheticReadFailure(code)
        return WatchPreflight("OBSERVED", "verified", "available", "available")

    async def observe() -> WatchCycle:
        nonlocal observe_calls
        observe_calls += 1
        raise _SyntheticReadFailure(code)

    class _NoWaitTiming(_TraceTiming):
        async def sleep(self, seconds: float) -> None:
            sleep_calls.append(seconds)
            await super().sleep(seconds)

    reports: list[str] = []
    result = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(root),
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=5,
            preflight=preflight,
            observe=observe,
            timing=_NoWaitTiming(),
            reporter=reports.append,
        )
    )
    assert result.status == "STOP"
    assert result.reason_code == code
    assert preflight_calls == 1
    assert observe_calls == (0 if phase == "preflight" else 1)
    assert sleep_calls == []
    assert "synthetic private detail" not in "\n".join(reports)


def test_read_failure_on_later_cycle_stops_without_retry_or_catch_up(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-watch-root"
    root.mkdir()
    observe_calls = 0
    sleep_calls: list[float] = []
    reports: list[str] = []

    class _RateLimited(Exception):
        code = "THREADS_RATE_LIMITED"

    async def preflight() -> WatchPreflight:
        return WatchPreflight("OBSERVED", "verified", "available", "available")

    async def observe() -> WatchCycle:
        nonlocal observe_calls
        observe_calls += 1
        if observe_calls == 2:
            raise _RateLimited("private network details")
        return WatchCycle("SUCCESS", "SUCCESS", "INBOUND_NOT_CONFIGURED", 1, 0, 1, 0, ())

    class _Timing(_TraceTiming):
        async def sleep(self, seconds: float) -> None:
            sleep_calls.append(seconds)
            await super().sleep(seconds)

    result = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(root),
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=10,
            preflight=preflight,
            observe=observe,
            timing=_Timing(),
            reporter=reports.append,
        )
    )
    assert result.status == "STOP"
    assert result.reason_code == "THREADS_RATE_LIMITED"
    assert result.cycles == observe_calls == 2
    assert sleep_calls == [900]
    assert reports[-1] == "watch shutdown status=STOP cycles=2 reason=THREADS_RATE_LIMITED"
    assert "private network details" not in "\n".join(reports)


@pytest.mark.parametrize("interrupt_phase", ["sleep", "observe"])
def test_ctrl_c_or_cancellation_stops_and_releases_watch_lock(
    tmp_path: Path,
    interrupt_phase: str,
) -> None:
    root = tmp_path / f"synthetic-{interrupt_phase}"
    root.mkdir()
    account_id = uuid4()
    observes = 0
    reports: list[str] = []

    async def preflight() -> WatchPreflight:
        return WatchPreflight("OBSERVED", "verified", "available", "available")

    async def observe() -> WatchCycle:
        nonlocal observes
        observes += 1
        if interrupt_phase == "observe":
            raise asyncio.CancelledError
        return WatchCycle("SUCCESS", "SUCCESS", "INBOUND_NOT_CONFIGURED", 1, 1, 1, 0, ())

    class _InterruptingTiming(_TraceTiming):
        async def sleep(self, seconds: float) -> None:
            if interrupt_phase == "sleep":
                raise KeyboardInterrupt
            await super().sleep(seconds)

    result = asyncio.run(
        run_foreground_watch(
            store=NurtureStore(root),
            account_id=account_id,
            interval_minutes=15,
            max_cycles=3,
            preflight=preflight,
            observe=observe,
            timing=_InterruptingTiming(),
            reporter=reports.append,
        )
    )
    assert result.status == "INTERRUPTED"
    assert result.cycles == observes == 1
    assert reports[-1] == "watch shutdown status=INTERRUPTED cycles=1 reason=INTERRUPTED"
    lock = AccountRecruitmentWatchLock(NurtureStore(root), account_id).acquire()
    assert lock.held
    lock.release()


@pytest.mark.parametrize("failure_kind", ["permission", "state"])
def test_injected_lock_path_failures_stop_without_touching_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    root = tmp_path / f"synthetic-{failure_kind}"
    root.mkdir()
    store = NurtureStore(root)
    preflight_calls = 0

    def reject_lock_directory() -> Path:
        if failure_kind == "permission":
            raise PermissionError("synthetic private permission detail")
        raise NurtureStateError("NURTURE_STATE_INVALID")

    monkeypatch.setattr(store, "lock_directory", reject_lock_directory)

    async def preflight() -> WatchPreflight:
        nonlocal preflight_calls
        preflight_calls += 1
        return WatchPreflight("OBSERVED", "verified", "available", "available")

    async def forbidden_observe() -> WatchCycle:
        pytest.fail("unsafe synthetic lock path must stop before reads")

    reports: list[str] = []
    result = asyncio.run(
        run_foreground_watch(
            store=store,
            account_id=uuid4(),
            interval_minutes=15,
            max_cycles=3,
            preflight=preflight,
            observe=forbidden_observe,
            reporter=reports.append,
        )
    )
    assert result.status == "STOP"
    assert result.reason_code == "WATCH_LOCK_UNAVAILABLE"
    assert preflight_calls == 0
    assert reports[-1] == "watch shutdown status=STOP cycles=0 reason=WATCH_LOCK_UNAVAILABLE"
    assert "synthetic private permission detail" not in "\n".join(reports)
