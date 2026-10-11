"""Run the recruitment watcher with a synthetic read-only backend."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from threads_platform.api.contracts import (
    DiscoveryPage,
    DiscoverySearchMode,
    DiscoverySearchType,
    PublishingQuota,
    RemoteAuthenticatedProfile,
    RemoteDiscoveryThread,
)
from threads_platform.standalone import recruitment_session as recruitment_module
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.nurture import get_nurture_preset
from threads_platform.standalone.recruitment_session import RecruitmentSession
from threads_platform.standalone.recruitment_watch import (
    MAX_WATCH_CYCLES,
    WatchTiming,
)

_PRESET = get_nurture_preset("recruitment")
_DEMO_THREAD = RemoteDiscoveryThread(
    remote_thread_id="ao02_synthetic_target_001",
    author_remote_id="ao02_synthetic_author_001",
    username="synthetic_author",
    text="Synthetic career question about hiring and interview preparation.",
    permalink=None,
    media_type=None,
    timestamp=datetime(2026, 10, 11, tzinfo=UTC),
    is_quote_post=False,
    has_replies=True,
)
_EMPTY_PAGE = DiscoveryPage((), None, False)


@dataclass(frozen=True, slots=True)
class DemoSummary:
    status: str
    cycles: int
    receipt_count: int
    receipt_bytes: int
    target_state_bytes: int
    fake_wait_seconds: int


class _SyntheticTiming(WatchTiming):
    def __init__(self) -> None:
        self._monotonic = 0.0
        self._now = datetime(2026, 10, 11, tzinfo=UTC)
        self.fake_wait_seconds = 0

    def monotonic(self) -> float:
        return self._monotonic

    def utc_now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.fake_wait_seconds += int(seconds)
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)


class _SyntheticApiRuntime:
    """Read-only API shape used by this demo; it has no credential or mutation methods."""

    def __init__(self) -> None:
        self._candidate_returned = False
        self.read_calls = 0

    async def authenticated_profile(self, _alias: str) -> RemoteAuthenticatedProfile:
        self.read_calls += 1
        return RemoteAuthenticatedProfile("ao02_synthetic_owner_id", "synthetic_owner")

    async def quota(self, _alias: str) -> PublishingQuota:
        self.read_calls += 1
        return PublishingQuota(usage=0, total=100, reply_usage=0, reply_total=50)

    async def mentions(
        self,
        _alias: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        if after is not None or limit <= 0:
            raise ValueError("synthetic backend received an invalid read request")
        self.read_calls += 1
        self._candidate_returned = False
        return _EMPTY_PAGE

    async def search(
        self,
        _alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        if after is not None or limit <= 0 or search_type is not DiscoverySearchType.RECENT:
            raise ValueError("synthetic backend received an invalid read request")
        self.read_calls += 1
        if (
            not self._candidate_returned
            and search_mode is DiscoverySearchMode.KEYWORD
            and query == _PRESET.keyword_queries[0]
        ):
            self._candidate_returned = True
            return DiscoveryPage((_DEMO_THREAD,), None, False)
        return _EMPTY_PAGE


def _synthetic_builder(
    expected_root: Path,
    expected_accounts: LocalAccountStore,
    api: _SyntheticApiRuntime,
) -> Callable[..., object]:
    @asynccontextmanager
    async def build(
        app_root: Path,
        accounts: LocalAccountStore | None = None,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = False,
    ) -> AsyncGenerator[StandaloneAppContext]:
        if (
            accounts is None
            or accounts is not expected_accounts
            or app_root.resolve() != expected_root.resolve()
            or not include_api
            or include_mutations
            or include_browser
        ):
            raise RuntimeError("synthetic demo permits API reads only")
        if accounts.get("demo").credential_ref is not None:
            raise RuntimeError("synthetic demo account must not contain credentials")
        yield StandaloneAppContext(
            accounts=accounts,
            api=cast(LocalThreadsApiRuntime, api),
            mutations=None,
        )

    return build


def run_demo(
    cycles: int,
    reporter: Callable[[str], None] | None = None,
) -> DemoSummary:
    """Run real observe/store policy against a fake-only app in a fresh temporary root."""

    if type(cycles) is not int or not 1 <= cycles <= MAX_WATCH_CYCLES:
        raise ValueError(f"cycles must be an integer from 1 to {MAX_WATCH_CYCLES}")
    emit = reporter if reporter is not None else _stdout
    api = _SyntheticApiRuntime()
    timing = _SyntheticTiming()

    with tempfile.TemporaryDirectory(prefix="threads-standalone-ao02-") as temporary_root:
        root = Path(temporary_root).resolve(strict=True)
        accounts = LocalAccountStore(root)
        account = accounts.add("demo")
        if account.credential_ref is not None:
            raise RuntimeError("synthetic demo account must not contain credentials")

        session = RecruitmentSession(root, accounts)
        original_builder = recruitment_module.build_standalone_app
        recruitment_module.build_standalone_app = _synthetic_builder(root, accounts, api)  # type: ignore[assignment]
        try:
            emit(
                f"demo start backend=FAKE identity=FAKE cycles={cycles} "
                "credential_source=NONE data_root=TEMPORARY interval_minutes=15 sleeper=FAKE"
            )
            result = asyncio.run(
                session.watch_observe(
                    "demo",
                    interval_minutes=15,
                    max_cycles=cycles,
                    timing=timing,
                    reporter=emit,
                )
            )
        finally:
            recruitment_module.build_standalone_app = original_builder

        completed = root / "nurture" / "runs" / str(account.id) / "completed"
        receipt_paths = tuple(completed.glob("*.json")) if completed.is_dir() else ()
        receipt_bytes = sum(path.stat().st_size for path in receipt_paths)
        state_path = root / "nurture" / "state" / str(account.id) / "recruitment.json"
        target_state_bytes = state_path.stat().st_size if state_path.is_file() else 0
        summary = DemoSummary(
            status=result.status,
            cycles=result.cycles,
            receipt_count=len(receipt_paths),
            receipt_bytes=receipt_bytes,
            target_state_bytes=target_state_bytes,
            fake_wait_seconds=timing.fake_wait_seconds,
        )
        emit(
            f"demo storage cycles={summary.cycles} receipts={summary.receipt_count} "
            f"receipt_bytes={summary.receipt_bytes} "
            f"target_state_bytes={summary.target_state_bytes} "
            f"fake_wait_seconds={summary.fake_wait_seconds}"
        )
        emit("demo safety mutation_runtime=DISABLED remote_mutations=0 credentials=NONE")
        return summary


def _parse_cycles(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("cycles must be an integer from 1 to 1000") from None
    if not 1 <= value <= MAX_WATCH_CYCLES:
        raise argparse.ArgumentTypeError("cycles must be an integer from 1 to 1000")
    return value


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_recruitment_watch")
    parser.add_argument("--cycles", type=_parse_cycles, default=3)
    args = parser.parse_args(argv)
    try:
        summary = run_demo(args.cycles)
    except KeyboardInterrupt:
        _stdout("demo status=INTERRUPTED")
        return 130
    except Exception:  # noqa: BLE001 — the standalone demo must not echo paths or data.
        sys.stderr.write("demo status=STOP reason=SYNTHETIC_DEMO_FAILED\n")
        sys.stderr.flush()
        return 1
    return 0 if summary.status == "COMPLETED" and summary.cycles == args.cycles else 1


if __name__ == "__main__":
    raise SystemExit(main())
