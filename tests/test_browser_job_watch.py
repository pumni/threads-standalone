from __future__ import annotations

import ast
import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from threads_platform.browser.job_observation import (
    SYNTHETIC_ORIGIN,
    SYNTHETIC_SEARCH_URL,
    FakeBodyVisibility,
    FakeJobCardV1,
    FakeJobFeedBatchV1,
    FakeJobPostDetailV1,
    FakePageState,
    ObservedPostKind,
)
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.standalone import browser_job_watch as watch_module
from threads_platform.standalone.browser_job_watch import (
    DEFAULT_MAX_CYCLES,
    MAX_SELECTED_POSTS_PER_CYCLE,
    MIN_CYCLE_INTERVAL_SECONDS,
    BrowserJobWatchEngine,
    SyntheticCycleInput,
    WatchReasonCode,
    WatchRunResult,
    WatchSource,
    WatchStatus,
)
from threads_platform.standalone.recruitment_job_store import (
    RecruitmentJobStore,
    RecruitmentJobStoreError,
    RetentionPolicy,
    StoreErrorCode,
)

_ACCOUNT_ONE = UUID("00000000-0000-4000-8000-000000000101")
_ACCOUNT_TWO = UUID("00000000-0000-4000-8000-000000000102")
_QUERY = "synthetic recruitment"
_BASE = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
_BODY = "Cần tuyển 02 sales tại Q.7, TP.HCM, lương 8–12tr/tháng"
_POLICY = RetentionPolicy(raw_text_days=30, derived_vacancy_days=90)


class _Clock:
    def __init__(self, now: datetime = _BASE) -> None:
        self.now = now
        self.tick = 10.0

    def now_utc(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.tick

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


class _Sleeper:
    def __init__(
        self,
        clock: _Clock,
        *,
        stop_after_wait: _Stop | None = None,
        stop_on_wait_number: int = 1,
    ) -> None:
        self.clock = clock
        self.stop_after_wait = stop_after_wait
        self.stop_on_wait_number = stop_on_wait_number
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)
        if self.stop_after_wait is not None and len(self.waits) == self.stop_on_wait_number:
            self.stop_after_wait.stopped = True


class _Stop:
    def __init__(self, stopped: bool = False) -> None:
        self.stopped = stopped

    def is_set(self) -> bool:
        return self.stopped


class _RegressingMonotonicClock(_Clock):
    def __init__(self) -> None:
        super().__init__()
        self.monotonic_calls = 0

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        return 10.0 if self.monotonic_calls == 1 else 9.0


class _Session:
    def __init__(
        self,
        batches: Sequence[FakeJobFeedBatchV1],
        details: dict[str, FakeJobPostDetailV1],
        *,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        self.batches = tuple(batches)
        self.details = details
        self.on_read = on_read
        self.read_calls = 0
        self.scroll_calls = 0
        self.opened: list[str] = []

    def read_initial_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        self.read_calls += 1
        if self.on_read is not None:
            self.on_read()
        batch = self.batches[0]
        assert batch.account_uuid == account_uuid
        assert batch.query == query
        return batch

    def scroll_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        next_batch_index: int,
        increment: int,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        self.scroll_calls += 1
        assert increment == 1
        batch = self.batches[next_batch_index]
        assert batch.account_uuid == account_uuid
        assert batch.query == query
        return batch

    def open_exact_post(
        self,
        *,
        account_uuid: UUID,
        canonical_post_url: str,
    ) -> FakeJobPostDetailV1 | None:
        self.opened.append(canonical_post_url)
        detail = self.details.get(canonical_post_url)
        if detail is not None:
            assert detail.account_uuid == account_uuid
        return detail


def _url(index: int, account_uuid: UUID = _ACCOUNT_ONE) -> str:
    suffix = "one" if account_uuid == _ACCOUNT_ONE else "two"
    return f"{SYNTHETIC_ORIGIN}/@employer-{suffix}-{index:03d}/post-{index:03d}"


def _detail(
    target: str,
    *,
    account_uuid: UUID = _ACCOUNT_ONE,
    body: str | None = _BODY,
    visibility: FakeBodyVisibility = FakeBodyVisibility.FULL_VISIBLE,
    kind: ObservedPostKind = ObservedPostKind.ORIGINAL,
    image_only: bool = False,
    quote_url: str | None = None,
) -> FakeJobPostDetailV1:
    return FakeJobPostDetailV1(
        requested_url=target,
        page_url=target,
        canonical_post_url=target,
        account_uuid=account_uuid,
        state=FakePageState.READY,
        author_marker="synthetic-employer",
        body=body,
        body_visibility=visibility,
        post_kind=kind,
        image_only=image_only,
        published_at_utc=datetime(2026, 10, 12, tzinfo=UTC),
        quoted_post_url=quote_url,
        quoted_author_label="synthetic-quoted" if quote_url else None,
    )


def _session(
    count: int = 1,
    *,
    account_uuid: UUID = _ACCOUNT_ONE,
    bodies: Sequence[str | None] | None = None,
    visibilities: Sequence[FakeBodyVisibility] | None = None,
    kinds: Sequence[ObservedPostKind] | None = None,
    image_only: Sequence[bool] | None = None,
    on_read: Callable[[], None] | None = None,
) -> tuple[_Session, tuple[str, ...]]:
    urls = tuple(_url(index + 1, account_uuid) for index in range(count))
    cards = tuple(
        FakeJobCardV1(
            account_uuid=account_uuid,
            local_card_id=f"card-{index + 1:03d}",
            canonical_post_url=target,
            author_marker="synthetic-employer",
        )
        for index, target in enumerate(urls)
    )
    batch = FakeJobFeedBatchV1(
        batch_index=0,
        account_uuid=account_uuid,
        query=_QUERY,
        page_url=SYNTHETIC_SEARCH_URL,
        state=FakePageState.READY,
        cards=cards,
        has_more=False,
    )
    details: dict[str, FakeJobPostDetailV1] = {}
    for index, target in enumerate(urls):
        body = _BODY if bodies is None else bodies[index]
        visibility = (
            FakeBodyVisibility.FULL_VISIBLE if visibilities is None else visibilities[index]
        )
        kind = ObservedPostKind.ORIGINAL if kinds is None else kinds[index]
        is_image_only = False if image_only is None else image_only[index]
        quote_url = (
            f"{SYNTHETIC_ORIGIN}/@quoted/post-{index + 1:03d}"
            if kind is ObservedPostKind.QUOTE
            else None
        )
        details[target] = _detail(
            target,
            account_uuid=account_uuid,
            body=body,
            visibility=visibility,
            kind=kind,
            image_only=is_image_only,
            quote_url=quote_url,
        )
    return _Session((batch,), details, on_read=on_read), urls


def _cycle(
    count: int = 1,
    *,
    account_uuid: UUID = _ACCOUNT_ONE,
    bodies: Sequence[str | None] | None = None,
    visibilities: Sequence[FakeBodyVisibility] | None = None,
    kinds: Sequence[ObservedPostKind] | None = None,
    image_only: Sequence[bool] | None = None,
    on_read: Callable[[], None] | None = None,
) -> tuple[SyntheticCycleInput, _Session, tuple[str, ...]]:
    session, urls = _session(
        count,
        account_uuid=account_uuid,
        bodies=bodies,
        visibilities=visibilities,
        kinds=kinds,
        image_only=image_only,
        on_read=on_read,
    )
    return SyntheticCycleInput(session=session, selected_targets=urls), session, urls


def _engine(tmp_path: Path) -> tuple[BrowserJobWatchEngine, Path]:
    root = tmp_path / "synthetic-watch-root"
    root.mkdir(mode=0o700)
    return BrowserJobWatchEngine(root, retention_policy=_POLICY), root


def _capture_id_factory(
    *,
    account_uuid: UUID,
    run_id: str,
    generation: int,
    cycle_number: int,
    observed_at_utc: datetime,
) -> str:
    account_tag = "one" if account_uuid == _ACCOUNT_ONE else "two"
    return f"cap-{account_tag}-{generation}-{cycle_number}-{int(observed_at_utc.timestamp())}"


def _run(
    engine: BrowserJobWatchEngine,
    cycle_inputs: Sequence[SyntheticCycleInput],
    *,
    clock: _Clock | None = None,
    sleeper: _Sleeper | None = None,
    stop: _Stop | None = None,
    account_uuid: UUID = _ACCOUNT_ONE,
    run_id: str = "run-synthetic-001",
    max_cycles: int = DEFAULT_MAX_CYCLES,
    interval_seconds: int = MIN_CYCLE_INTERVAL_SECONDS,
    capture_id_factory: Callable[..., str] = _capture_id_factory,
) -> WatchRunResult:
    active_clock = clock or _Clock()
    active_stop = stop or _Stop()
    active_sleeper = sleeper or _Sleeper(active_clock)
    return engine.run(
        source=WatchSource.SYNTHETIC_FIXTURE,
        account_uuid=account_uuid,
        query=_QUERY,
        run_id=run_id,
        cycles=cycle_inputs,
        max_cycles=max_cycles,
        interval_seconds=interval_seconds,
        clock=active_clock,
        sleeper=active_sleeper,
        stop_signal=active_stop,
        capture_id_factory=capture_id_factory,
    )


def _archive_path(root: Path, account_uuid: UUID = _ACCOUNT_ONE) -> Path:
    return root / "job_vacancies_v1" / "accounts" / f"{account_uuid}.json"


def _checkpoint_path(root: Path, account_uuid: UUID = _ACCOUNT_ONE) -> Path:
    return root / "browser_job_watch_v1" / "accounts" / f"{account_uuid}.json"


def _document(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def test_default_run_uses_one_cycle_and_persists_only_synthetic_observations(
    tmp_path: Path,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, urls = _cycle()

    result = _run(engine, (cycle_input,))

    assert result.status is WatchStatus.COMPLETE
    assert result.completed_cycles == 1
    assert result.scanned == 1
    assert result.vacancy == 1
    assert result.duplicate == 0
    assert session.read_calls == 1
    assert session.opened == list(urls)
    summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE)
    assert summary.observation_count == 1
    checkpoint = _document(_checkpoint_path(root))
    assert checkpoint["schema_version"] == 1
    assert checkpoint["completed_cycles"] == 1
    assert checkpoint["pending_capture_id"] is None
    assert "query" not in checkpoint
    assert "visible_text" not in checkpoint


@pytest.mark.parametrize("count", [0, 1, MAX_SELECTED_POSTS_PER_CYCLE])
def test_selected_post_bounds_allow_zero_one_and_twenty(
    tmp_path: Path,
    count: int,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, _session_value, _urls = _cycle(count)

    result = _run(engine, (cycle_input,))

    assert result.status is WatchStatus.COMPLETE
    assert result.scanned == count
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == count
    )


def test_twenty_one_selected_posts_fail_before_session_or_storage(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, _urls = _cycle(MAX_SELECTED_POSTS_PER_CYCLE + 1)

    result = _run(engine, (cycle_input,))

    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.INVALID_POLICY
    assert session.read_calls == 0
    assert not (root / "job_vacancies_v1").exists()
    assert not (root / "browser_job_watch_v1").exists()


@pytest.mark.parametrize(
    "path",
    ["/@synthetic/../post", "/@synthetic/%2e%2e/post", "/@synthetic/%252e%252e/post"],
)
def test_traversal_target_fails_before_session_or_storage(tmp_path: Path, path: str) -> None:
    engine, root = _engine(tmp_path)
    _cycle_input, session, _urls = _cycle()
    invalid_cycle = SyntheticCycleInput(
        session=session,
        selected_targets=(f"{SYNTHETIC_ORIGIN}{path}",),
    )

    result = _run(engine, (invalid_cycle,))

    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.INVALID_POLICY
    assert session.read_calls == 0
    assert not (root / "job_vacancies_v1").exists()
    assert not (root / "browser_job_watch_v1").exists()


@pytest.mark.parametrize("max_cycles", [0, 6])
def test_cycle_limit_rejects_zero_and_six_before_storage(
    tmp_path: Path,
    max_cycles: int,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, _urls = _cycle()
    inputs = (cycle_input,) if max_cycles == 0 else (cycle_input,) * max_cycles

    result = _run(engine, inputs, max_cycles=max_cycles)

    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.INVALID_POLICY
    assert session.read_calls == 0
    assert not (root / "job_vacancies_v1").exists()


@pytest.mark.parametrize("max_cycles", [1, 5])
def test_cycle_limit_accepts_one_and_five_with_simulated_interval(
    tmp_path: Path,
    max_cycles: int,
) -> None:
    engine, root = _engine(tmp_path)
    inputs = tuple(_cycle()[0] for _ in range(max_cycles))
    clock = _Clock()
    sleeper = _Sleeper(clock)

    result = _run(
        engine,
        inputs,
        clock=clock,
        sleeper=sleeper,
        max_cycles=max_cycles,
    )

    assert result.status is WatchStatus.COMPLETE
    assert result.completed_cycles == max_cycles
    assert len(sleeper.waits) == max_cycles - 1
    assert all(seconds >= MIN_CYCLE_INTERVAL_SECONDS for seconds in sleeper.waits)
    summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE)
    assert summary.observation_count == max_cycles
    assert summary.revision_count == 1
    if max_cycles > 1:
        assert result.vacancy == 1
        assert result.duplicate == max_cycles - 1


def test_five_batch_candidate_cap_fails_closed_before_archive_write(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    all_cards = tuple(
        FakeJobCardV1(
            account_uuid=_ACCOUNT_ONE,
            local_card_id=f"cap-card-{index:03d}",
            canonical_post_url=_url(index),
            author_marker="synthetic-employer",
        )
        for index in range(1, 101)
    )
    batches = tuple(
        FakeJobFeedBatchV1(
            batch_index=batch_index,
            account_uuid=_ACCOUNT_ONE,
            query=_QUERY,
            page_url=SYNTHETIC_SEARCH_URL,
            state=FakePageState.READY,
            cards=all_cards[batch_index * 20 : (batch_index + 1) * 20],
            has_more=True,
        )
        for batch_index in range(5)
    )
    selected = _url(1)
    session = _Session(
        batches,
        {selected: _detail(selected)},
    )
    cycle_input = SyntheticCycleInput(session=session, selected_targets=(selected,))

    result = _run(engine, (cycle_input,))

    assert result.status is WatchStatus.CAP_REACHED
    assert result.scanned == 100
    assert result.completed_cycles == 0
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 0
    )


def test_same_post_revisions_are_deduplicated_but_changed_text_is_new(
    tmp_path: Path,
) -> None:
    engine, root = _engine(tmp_path)
    first, _session_one, _ = _cycle(bodies=(_BODY,))
    clock = _Clock()
    first_result = _run(engine, (first,), clock=clock, run_id="run-first")
    assert first_result.vacancy == 1

    clock.advance(1)
    unchanged, _session_two, _ = _cycle(bodies=(_BODY,))
    unchanged_result = _run(
        engine,
        (unchanged,),
        clock=clock,
        run_id="run-unchanged",
    )
    assert unchanged_result.duplicate == 1
    assert unchanged_result.vacancy == 0

    changed_body = "Cần tuyển kế toán tại Hà Nội; lương 10tr/tháng"
    clock.advance(1)
    changed, _session_three, _ = _cycle(bodies=(changed_body,))
    changed_result = _run(
        engine,
        (changed,),
        clock=clock,
        run_id="run-changed",
    )
    assert changed_result.vacancy == 1
    summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE)
    assert summary.observation_count == 3
    assert summary.revision_count == 2


def test_restart_reuses_pending_event_after_fake_read_is_stopped(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    stop = _Stop()
    cycle_input, session, _urls = _cycle(on_read=lambda: setattr(stop, "stopped", True))
    clock = _Clock()

    stopped = _run(
        engine,
        (cycle_input,),
        clock=clock,
        stop=stop,
        run_id="run-restart-read",
    )

    assert stopped.status is WatchStatus.STOPPED
    assert stopped.completed_cycles == 0
    assert session.read_calls == 1
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 0
    )
    pending_before = _document(_checkpoint_path(root))["pending_capture_id"]
    session.on_read = None
    stop.stopped = False
    changed_session, changed_urls = _session(2)
    changed_targets = SyntheticCycleInput(
        session=changed_session,
        selected_targets=(changed_urls[1],),
    )
    changed = _run(
        engine,
        (changed_targets,),
        clock=clock,
        stop=stop,
        run_id="run-restart-read",
    )
    assert changed.status is WatchStatus.FAILED
    assert changed.reason_code is WatchReasonCode.TARGET_CHANGED
    assert changed_session.read_calls == 0
    assert _document(_checkpoint_path(root))["pending_capture_id"] == pending_before

    resumed = _run(
        engine,
        (cycle_input,),
        clock=clock,
        stop=stop,
        run_id="run-restart-read",
    )

    assert resumed.status is WatchStatus.COMPLETE
    assert session.read_calls == 2
    assert _document(_checkpoint_path(root))["pending_capture_id"] is None
    assert pending_before is not None
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 1
    )


def test_restart_after_archive_before_checkpoint_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, _session_value, _urls = _cycle()
    stop = _Stop()
    original_record = engine._store.record_observation

    def store_then_stop(*args: object, **kwargs: object) -> object:
        receipt = original_record(*args, **kwargs)
        stop.stopped = True
        return receipt

    monkeypatch.setattr(engine._store, "record_observation", store_then_stop)
    interrupted = _run(
        engine,
        (cycle_input,),
        stop=stop,
        run_id="run-after-store",
    )
    assert interrupted.status is WatchStatus.STOPPED
    assert interrupted.completed_cycles == 0
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 1
    )
    pending_id = _document(_checkpoint_path(root))["pending_capture_id"]

    monkeypatch.undo()
    resumed = _run(engine, (cycle_input,), run_id="run-after-store")

    assert resumed.status is WatchStatus.COMPLETE
    assert resumed.duplicate == 1
    assert resumed.vacancy == 0
    checkpoint = _document(_checkpoint_path(root))
    assert checkpoint["completed_cycles"] == 1
    assert checkpoint["pending_capture_id"] is None
    assert pending_id is not None
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 1
    )


def test_atomic_checkpoint_failure_keeps_pending_for_safe_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, _session_value, _urls = _cycle()
    original_replace = watch_module.os.replace
    replace_calls = 0

    def fail_completion_replace(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal replace_calls
        if Path(target) == _checkpoint_path(root):
            replace_calls += 1
        if Path(target) == _checkpoint_path(root) and replace_calls == 3:
            raise OSError("synthetic checkpoint interruption")
        original_replace(source, target)

    monkeypatch.setattr(watch_module.os, "replace", fail_completion_replace)
    interrupted = _run(engine, (cycle_input,), run_id="run-atomic-replay")
    assert interrupted.status is WatchStatus.FAILED
    assert interrupted.reason_code is WatchReasonCode.CHECKPOINT_FAILED
    pending = _document(_checkpoint_path(root))
    assert pending["completed_cycles"] == 0
    assert isinstance(pending["pending_capture_id"], str)
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 1
    )

    monkeypatch.undo()
    resumed = _run(engine, (cycle_input,), run_id="run-atomic-replay")
    assert resumed.status is WatchStatus.COMPLETE
    assert resumed.duplicate == 1
    assert resumed.vacancy == 0


def test_stop_during_interval_wait_does_not_start_next_capture(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    cycle_inputs = tuple(_cycle()[0] for _ in range(2))
    clock = _Clock()
    stop = _Stop()
    sleeper = _Sleeper(clock, stop_after_wait=stop)

    result = _run(
        engine,
        cycle_inputs,
        clock=clock,
        sleeper=sleeper,
        stop=stop,
        max_cycles=2,
        run_id="run-stop-sleep",
    )

    assert result.status is WatchStatus.STOPPED
    assert result.completed_cycles == 1
    assert len(sleeper.waits) == 1
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 1
    )


@pytest.mark.parametrize(
    ("body", "visibility", "kind", "image_only"),
    [
        (_BODY, FakeBodyVisibility.PARTIAL, ObservedPostKind.ORIGINAL, False),
        (_BODY, FakeBodyVisibility.FULL_VISIBLE, ObservedPostKind.QUOTE, False),
        (None, FakeBodyVisibility.UNKNOWN, ObservedPostKind.ORIGINAL, True),
    ],
)
def test_partial_quote_and_image_only_evidence_stays_unpromoted(
    tmp_path: Path,
    body: str | None,
    visibility: FakeBodyVisibility,
    kind: ObservedPostKind,
    image_only: bool,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, _session_value, _urls = _cycle(
        bodies=(body,),
        visibilities=(visibility,),
        kinds=(kind,),
        image_only=(image_only,),
    )

    result = _run(engine, (cycle_input,))

    assert result.status is WatchStatus.COMPLETE
    assert result.completed_cycles == 1
    if visibility is not FakeBodyVisibility.FULL_VISIBLE or body is None:
        assert result.partial == 1
    archive = _document(_archive_path(root))
    posts = archive["posts"]
    assert isinstance(posts, list)
    first_post = posts[0]
    assert isinstance(first_post, dict)
    revisions = first_post["revisions"]
    assert isinstance(revisions, list)
    if body is None:
        assert revisions == []
    elif visibility is FakeBodyVisibility.PARTIAL:
        revision = revisions[0]
        assert isinstance(revision, dict)
        assert revision["text_completeness"] == "PARTIAL"
    if kind is ObservedPostKind.QUOTE:
        observations = first_post["observations"]
        assert isinstance(observations, list)
        assert observations[0]["post_kind"] == "QUOTE"


def test_two_accounts_have_independent_archives_and_checkpoints(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    first, _session_one, _ = _cycle(account_uuid=_ACCOUNT_ONE)
    second, _session_two, _ = _cycle(account_uuid=_ACCOUNT_TWO)

    first_result = _run(engine, (first,), account_uuid=_ACCOUNT_ONE, run_id="run-one")
    second_result = _run(engine, (second,), account_uuid=_ACCOUNT_TWO, run_id="run-two")

    assert first_result.status is WatchStatus.COMPLETE
    assert second_result.status is WatchStatus.COMPLETE
    assert _checkpoint_path(root, _ACCOUNT_ONE).exists()
    assert _checkpoint_path(root, _ACCOUNT_TWO).exists()
    first_summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE)
    second_summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_TWO)
    assert first_summary.observation_count == 1
    assert second_summary.observation_count == 1


def test_reused_capture_id_fails_before_reading_next_cycle(tmp_path: Path) -> None:
    engine, _root = _engine(tmp_path)
    first, first_session, _ = _cycle()
    second, second_session, _ = _cycle()

    result = _run(
        engine,
        (first, second),
        max_cycles=2,
        run_id="run-id-reuse",
        capture_id_factory=lambda **_kwargs: "capture-always-same",
    )

    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.CAPTURE_ID_REUSED
    assert result.completed_cycles == 1
    assert first_session.read_calls == 1
    assert second_session.read_calls == 0


def test_manual_url_and_browser_ui_are_no_io_fixed_outcomes(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, _ = _cycle()

    manual = engine.run(
        source=WatchSource.MANUAL_URL,
        manual_url="https://threads.example.invalid/@synthetic/post-001",
        cycles=(cycle_input,),
    )
    browser = engine.run(source=WatchSource.BROWSER_UI, cycles=(cycle_input,))

    assert manual.status is WatchStatus.UNKNOWN
    assert manual.reason_code is WatchReasonCode.NO_TEXT
    assert browser.status is WatchStatus.DISABLED_PERMISSION_PENDING
    assert session.read_calls == 0
    assert not (root / "job_vacancies_v1").exists()
    assert not (root / "browser_job_watch_v1").exists()


def test_checkpoint_rejects_corruption_and_unsupported_version_without_migration(
    tmp_path: Path,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, _ = _cycle()
    first = _run(engine, (cycle_input,), run_id="run-checkpoint-corrupt")
    assert first.status is WatchStatus.COMPLETE
    checkpoint_path = _checkpoint_path(root)
    good_bytes = checkpoint_path.read_bytes()

    checkpoint = json.loads(good_bytes)
    assert isinstance(checkpoint, dict)
    checkpoint["schema_version"] = 2
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    unsupported = _run(engine, (cycle_input,), run_id="run-checkpoint-corrupt")
    assert unsupported.reason_code is WatchReasonCode.CHECKPOINT_VERSION_UNSUPPORTED
    assert checkpoint_path.read_text(encoding="utf-8") == json.dumps(checkpoint)
    assert session.read_calls == 1

    checkpoint_path.write_bytes(b"{invalid synthetic checkpoint")
    corrupt = _run(engine, (cycle_input,), run_id="run-checkpoint-corrupt")
    assert corrupt.reason_code is WatchReasonCode.INVALID_CHECKPOINT
    assert session.read_calls == 1


def test_checkpoint_symlink_and_insecure_directory_fail_closed(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, _session_value, _ = _cycle()
    assert _run(engine, (cycle_input,), run_id="run-checkpoint-path").status is WatchStatus.COMPLETE
    checkpoint_path = _checkpoint_path(root)
    external = tmp_path / "synthetic-external-checkpoint.json"
    external.write_text(checkpoint_path.read_text(encoding="utf-8"), encoding="utf-8")
    checkpoint_path.unlink()
    try:
        checkpoint_path.symlink_to(external)
    except OSError, NotImplementedError:
        pytest.skip("file symlink creation is unavailable")

    symlink_result = _run(engine, (cycle_input,), run_id="run-checkpoint-path")
    assert symlink_result.status is WatchStatus.FAILED
    assert symlink_result.reason_code is WatchReasonCode.CHECKPOINT_UNSAFE
    checkpoint_path.unlink()
    checkpoint_path.write_text(external.read_text(encoding="utf-8"), encoding="utf-8")

    namespace = root / "browser_job_watch_v1"
    if os.name != "nt":
        namespace.chmod(0o755)
        insecure = _run(engine, (cycle_input,), run_id="run-checkpoint-path")
        assert insecure.reason_code is WatchReasonCode.CHECKPOINT_UNSAFE


def test_checkpoint_acl_verification_failure_stops_before_fixture_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _root = _engine(tmp_path)
    cycle_input, session, _ = _cycle()
    assert _run(engine, (cycle_input,), run_id="run-acl-check").status is WatchStatus.COMPLETE
    original_verify = watch_module._store_fs._verify_private_directory

    def deny_watch_namespace(path: Path) -> None:
        if path.name == "browser_job_watch_v1":
            raise RecruitmentJobStoreError(StoreErrorCode.PERMISSIONS_UNVERIFIED)
        original_verify(path)

    monkeypatch.setattr(watch_module._store_fs, "_verify_private_directory", deny_watch_namespace)
    result = _run(engine, (cycle_input,), run_id="run-acl-check")

    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.CHECKPOINT_UNSAFE
    assert session.read_calls == 1


def test_checkpoint_account_lock_is_nonblocking(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, _ = _cycle()
    assert _run(engine, (cycle_input,), run_id="run-lock-busy").status is WatchStatus.COMPLETE
    lock_path = root / "browser_job_watch_v1" / "locks" / f"{_ACCOUNT_ONE}.lock"
    held_lock = FilesystemProcessLock(lock_path)
    held_lock.acquire()
    try:
        result = _run(engine, (cycle_input,), run_id="run-lock-busy")
    finally:
        held_lock.release()
    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.CHECKPOINT_BUSY
    assert session.read_calls == 1


def test_backward_utc_clock_stops_without_checkpoint_advance(tmp_path: Path) -> None:
    engine, root = _engine(tmp_path)
    first, _first_session, _ = _cycle()
    clock = _Clock()
    first_result = _run(
        engine,
        (first,),
        clock=clock,
        run_id="run-clock-backward-1",
    )
    assert first_result.status is WatchStatus.COMPLETE
    clock.now = _BASE - timedelta(minutes=1)
    second, second_session, _ = _cycle()

    result = _run(
        engine,
        (second,),
        clock=clock,
        run_id="run-clock-backward-2",
    )

    assert result.status is WatchStatus.FAILED
    assert result.reason_code is WatchReasonCode.CLOCK_MOVED_BACKWARD
    assert second_session.read_calls == 0
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 1
    )


@pytest.mark.parametrize(
    ("stop_on_wait_number", "completed_before_resume"),
    [(1, 1), (2, 2)],
)
def test_restart_after_first_or_second_cycle_resumes_next_cycle(
    tmp_path: Path,
    stop_on_wait_number: int,
    completed_before_resume: int,
) -> None:
    engine, root = _engine(tmp_path)
    inputs = tuple(_cycle()[0] for _ in range(3))
    clock = _Clock()
    stop = _Stop()
    sleeper = _Sleeper(
        clock,
        stop_after_wait=stop,
        stop_on_wait_number=stop_on_wait_number,
    )

    first = _run(
        engine,
        inputs,
        clock=clock,
        sleeper=sleeper,
        stop=stop,
        max_cycles=3,
        run_id=f"run-resume-cycle-{completed_before_resume + 1}",
    )
    assert first.status is WatchStatus.STOPPED
    assert first.completed_cycles == completed_before_resume
    stop.stopped = False
    resumed_sleeper = _Sleeper(clock)
    second = _run(
        engine,
        inputs,
        clock=clock,
        sleeper=resumed_sleeper,
        stop=stop,
        max_cycles=3,
        run_id=f"run-resume-cycle-{completed_before_resume + 1}",
    )
    assert second.status is WatchStatus.COMPLETE
    assert second.completed_cycles == 3
    assert (
        RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE).observation_count
        == 3
    )


def test_no_network_browser_cli_or_customer_dependencies_are_imported() -> None:
    source_path = Path(watch_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    forbidden = (
        "playwright",
        "requests",
        "httpx",
        "urllib",
        "threads_platform.api",
        "threads_platform.crm",
        "threads_platform.standalone.__main__",
        "threads_platform.standalone.opportunity_queue",
    )
    assert not any(
        module == forbidden_name or module.startswith(f"{forbidden_name}.")
        for module in modules
        for forbidden_name in forbidden
    )


def test_result_and_cycle_reprs_do_not_expose_fixture_content(tmp_path: Path) -> None:
    engine, _root = _engine(tmp_path)
    cycle_input, _session_value, urls = _cycle()
    result = _run(engine, (cycle_input,))
    rendered = repr(result) + repr(cycle_input)

    assert _QUERY not in rendered
    assert _BODY not in rendered
    assert urls[0] not in rendered
    assert str(_ACCOUNT_ONE) not in rendered


def test_synthetic_contact_placeholder_is_not_in_result_or_checkpoint(
    tmp_path: Path,
) -> None:
    engine, root = _engine(tmp_path)
    placeholder = "[SYNTHETIC_PHONE_001]"
    body = f"Cần tuyển sales; SĐT: {placeholder}"
    cycle_input, _session_value, urls = _cycle(bodies=(body,))

    result = _run(engine, (cycle_input,))

    assert result.status is WatchStatus.COMPLETE
    checkpoint_text = _checkpoint_path(root).read_text(encoding="utf-8")
    rendered = repr(result) + checkpoint_text
    assert placeholder not in rendered
    assert body not in rendered
    assert urls[0] not in rendered


def test_backward_monotonic_clock_stops_fake_observation_before_read(
    tmp_path: Path,
) -> None:
    engine, root = _engine(tmp_path)
    cycle_input, session, _ = _cycle()

    result = _run(
        engine,
        (cycle_input,),
        clock=cast(_Clock, _RegressingMonotonicClock()),
        run_id="run-monotonic-backward",
    )

    assert result.status is WatchStatus.FAILED
    assert result.reason_code == "CLOCK_MOVED_BACKWARD"
    assert session.read_calls == 0
    summary = RecruitmentJobStore(root, retention_policy=_POLICY).summary(_ACCOUNT_ONE)
    assert summary.observation_count == 0
