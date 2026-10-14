# pyright: reportPrivateUsage=false
"""Account-scoped, read-only Browser Nurture orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs, urlsplit

from threads_platform.browser.contracts import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserProcessCrashed,
)
from threads_platform.browser.threads_job_ui_reader import (
    UiBodyVisibility,
    UiFeedBatch,
    UiPageState,
    UiPostDetail,
    UiPostTarget,
)
from threads_platform.standalone import recruitment_job_store as _store_fs
from threads_platform.standalone.browser_nurture_state import (
    MAX_CHECKPOINT_COUNTER,
    MAX_SEEN_POST_FINGERPRINTS,
    BrowserNurtureCheckpointV2,
    BrowserNurtureConfigV1,
    BrowserNurtureStateError,
    BrowserNurtureStateStore,
    browser_nurture_policy_fingerprint,
    validate_browser_nurture_config,
)
from threads_platform.standalone.nurture_policy import (
    matches_nurture_terms,
    normalize_nurture_text,
)
from threads_platform.standalone.recruitment_job_store import RecruitmentJobStoreError
from threads_platform.standalone.runtime import (
    BrowserProfileLease,
    LocalRuntime,
    StandaloneRuntimeError,
)

SYNTHETIC_NURTURE_ORIGIN = "https://threads.example.invalid"
MAX_SYNTHETIC_NURTURE_FIXTURE_BYTES = 1_100_000
MAX_NURTURE_FEED_CANDIDATES_PER_CYCLE = 100
MAX_NURTURE_CYCLES_PER_INVOCATION = 5
MAX_NURTURE_CYCLE_SECONDS = 180
MAX_MANUAL_ASSIST_RETRIES = 3

_ACCOUNT_ALIAS = re.compile(r"^(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})$")
_SYNTHETIC_ACTOR = re.compile(r"^(?:test|synthetic|fixture)[._-][A-Za-z0-9._-]{1,28}$")
_POST_PATH = re.compile(r"/@[A-Za-z0-9._]{1,30}/post/[A-Za-z0-9_-]{1,120}\Z")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_PHONE = re.compile(r"(?<![\w])(?:\+?84|0)(?:[ \t().-]*\d){8,10}(?!\w)")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+")
_FIXTURE_KEYS = frozenset({"schema_version", "fixture_kind", "pages"})


class BrowserNurtureError(Exception):
    """Fixed safe error code for Browser Nurture orchestration."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError("invalid Browser Nurture error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"BrowserNurtureError({self.code!r})"


class BrowserNurtureStatus(StrEnum):
    COMPLETE = "COMPLETE"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class BrowserNurtureSource(StrEnum):
    BROWSER_UI = "browser"
    UI_FIXTURE = "ui-fixture"


@dataclass(frozen=True, slots=True, repr=False)
class SyntheticBrowserNurtureFixtureV1:
    pages: Mapping[str, str] = field(repr=False)
    origin: str = SYNTHETIC_NURTURE_ORIGIN
    schema_version: int = 1

    def __repr__(self) -> str:
        return f"SyntheticBrowserNurtureFixtureV1(page_count={len(self.pages)})"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserNurtureCycleMetrics:
    feed_candidates: int
    scanned: int
    relevant: int
    irrelevant: int
    unknown: int
    duplicates: int
    scrolls: int
    topic_queries: int
    fingerprints: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "BrowserNurtureCycleMetrics("
            f"feed_candidates={self.feed_candidates}, scanned={self.scanned}, "
            f"relevant={self.relevant}, irrelevant={self.irrelevant}, "
            f"unknown={self.unknown}, duplicates={self.duplicates}, "
            f"scrolls={self.scrolls}, topic_queries={self.topic_queries})"
        )


@dataclass(frozen=True, slots=True)
class BrowserNurtureRunResult:
    status: BrowserNurtureStatus
    reason_code: str | None
    cycles: int
    feed_candidates: int
    scanned: int
    relevant: int
    irrelevant: int
    unknown: int
    duplicates: int
    scrolls: int
    sessions_opened: int
    checkpoint_cycles: int


@dataclass(frozen=True, slots=True)
class BrowserNurtureAssistResult:
    status: BrowserNurtureStatus
    reason_code: str | None
    stage: str
    search_candidates: int
    scanned: int
    classification: str
    body_status: str
    sessions_opened: int


class BrowserNurtureUiSession(Protocol):
    async def install_synthetic_fixture_pages(
        self,
        pages: Mapping[str, str],
        *,
        origin: str,
    ) -> None: ...

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None: ...

    async def verify_ui_account(self, expected_actor: str, *, origin: str) -> UiPageState: ...

    async def search_ui_feed(
        self,
        query: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> tuple[UiFeedBatch, ...]: ...

    async def assisted_search_ui_results(
        self,
        query: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> tuple[UiPostTarget, ...]: ...

    async def read_exact_ui_post(
        self,
        target: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> UiPostDetail: ...

    async def arm_ui_diagnostic_operator_click(self, *, source_route: str) -> None: ...

    async def wait_for_assisted_post_click(
        self,
        candidates: tuple[UiPostTarget, ...],
        *,
        origin: str,
    ) -> str: ...

    async def read_current_ui_post(
        self,
        target: str,
        *,
        expected_actor: str,
        origin: str,
    ) -> UiPostDetail: ...

    async def close(self) -> None: ...


ManualAssistStep = Callable[[str], Awaitable[None]]
ManualAssistDecision = Callable[[str, str, tuple[str, ...]], Awaitable[str]]
ManualAssistProgress = Callable[[str, int | None], None]


@dataclass(frozen=True, slots=True)
class _ManualAssistAttempt[AssistValue]:
    action: str
    value: AssistValue | None = None
    reason_code: str | None = None


async def _retry_manual_assist_step[AssistValue](
    stage: str,
    operation: Callable[[], Awaitable[AssistValue]],
    decide: ManualAssistDecision,
) -> _ManualAssistAttempt[AssistValue]:
    retries = 0
    while True:
        try:
            return _ManualAssistAttempt("READY", await operation())
        except BrowserProcessCrashed:
            return _ManualAssistAttempt("FAILED", reason_code="BROWSER_PROCESS_CRASHED")
        except (BrowserAdapterError, BrowserNurtureError) as error:
            code = error.code
            if _manual_assist_fatal_error(code):
                return _ManualAssistAttempt("FAILED", reason_code=code)
            choices = (
                ("RETRY", "SKIP", "STOP")
                if retries < MAX_MANUAL_ASSIST_RETRIES
                else ("SKIP", "STOP")
            )
            decision = await decide(stage, code, choices)
            if decision == "RETRY" and retries < MAX_MANUAL_ASSIST_RETRIES:
                retries += 1
                continue
            if decision == "SKIP":
                return _ManualAssistAttempt("SKIP", reason_code=code)
            return _ManualAssistAttempt("STOP", reason_code=code)


def _manual_assist_fatal_error(code: str) -> bool:
    return code.startswith("REMOTE_") or code in {
        "BROWSER_NAVIGATION_BLOCKED",
        "BROWSER_PROCESS_CRASHED",
        "BROWSER_SESSION_MISMATCH",
        "CHALLENGE_REQUIRED",
        "RATE_LIMITED",
        "SESSION_EXPIRED",
    }


def validate_synthetic_nurture_root(path: Path) -> Path:
    """Require an explicit existing private synthetic root outside the checkout."""

    try:
        root = Path(path).expanduser().absolute()
        _store_fs._validate_path_components(root)
        _store_fs._ensure_outside_checkout(root)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or _store_fs._is_reparse_point(info):
            raise BrowserNurtureError("DATA_ROOT_INVALID")
        _store_fs._verify_private_directory(root)
        return root
    except BrowserNurtureError:
        raise
    except RecruitmentJobStoreError:
        raise BrowserNurtureError("DATA_ROOT_INVALID") from None
    except OSError, RuntimeError, ValueError:
        raise BrowserNurtureError("DATA_ROOT_INVALID") from None


def load_synthetic_nurture_fixture(
    path: Path,
    config: BrowserNurtureConfigV1,
) -> SyntheticBrowserNurtureFixtureV1:
    """Read a private, bounded local fixture without accepting remote URLs or contacts."""

    try:
        fixture_path = Path(path).absolute()
        parent = fixture_path.parent
        _store_fs._validate_path_components(parent)
        _store_fs._ensure_outside_checkout(parent)
        _store_fs._verify_regular_file(
            fixture_path,
            parent,
            allow_missing=False,
            max_bytes=MAX_SYNTHETIC_NURTURE_FIXTURE_BYTES,
        )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(fixture_path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SYNTHETIC_NURTURE_FIXTURE_BYTES:
                raise BrowserNurtureError("FIXTURE_INVALID")
            raw = stream.read(MAX_SYNTHETIC_NURTURE_FIXTURE_BYTES + 1)
        if len(raw) > MAX_SYNTHETIC_NURTURE_FIXTURE_BYTES:
            raise BrowserNurtureError("FIXTURE_INVALID")
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        return decode_synthetic_nurture_fixture(document, config)
    except BrowserNurtureError:
        raise
    except OSError, RecruitmentJobStoreError, UnicodeError, json.JSONDecodeError, ValueError:
        raise BrowserNurtureError("FIXTURE_INVALID") from None


def decode_synthetic_nurture_fixture(
    value: object,
    config: BrowserNurtureConfigV1,
) -> SyntheticBrowserNurtureFixtureV1:
    if type(value) is not dict:
        raise BrowserNurtureError("FIXTURE_INVALID")
    document = cast(dict[str, object], value)
    if (
        frozenset(document) != _FIXTURE_KEYS
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["fixture_kind"] != "SYNTHETIC_BROWSER_NURTURE"
    ):
        raise BrowserNurtureError("FIXTURE_INVALID")
    if _SYNTHETIC_ACTOR.fullmatch(config.expected_username) is None:
        raise BrowserNurtureError("SYNTHETIC_ACTOR_REQUIRED")
    raw_pages = document["pages"]
    if type(raw_pages) is not list:
        raise BrowserNurtureError("FIXTURE_INVALID")
    fixture_pages = cast(list[object], raw_pages)
    if not 1 <= len(fixture_pages) <= 128:
        raise BrowserNurtureError("FIXTURE_INVALID")
    pages: dict[str, str] = {}
    search_queries: set[str] = set()
    for raw_page in fixture_pages:
        if type(raw_page) is not dict:
            raise BrowserNurtureError("FIXTURE_INVALID")
        page = cast(dict[str, object], raw_page)
        if frozenset(page) != frozenset({"url", "html"}):
            raise BrowserNurtureError("FIXTURE_INVALID")
        url = page["url"]
        html = page["html"]
        if (
            not isinstance(url, str)
            or not isinstance(html, str)
            or not html
            or len(html) > MAX_SYNTHETIC_NURTURE_FIXTURE_BYTES
            or _contains_sensitive_fixture_content(html)
        ):
            raise BrowserNurtureError("FIXTURE_INVALID")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "threads.example.invalid"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
            or url in pages
        ):
            raise BrowserNurtureError("FIXTURE_INVALID")
        if parsed.path == "/":
            if parsed.query:
                raise BrowserNurtureError("FIXTURE_INVALID")
        elif parsed.path == "/search":
            try:
                query = parse_qs(parsed.query, strict_parsing=True)
            except ValueError:
                raise BrowserNurtureError("FIXTURE_INVALID") from None
            if set(query) != {"q"} or len(query["q"]) != 1:
                raise BrowserNurtureError("FIXTURE_INVALID")
            search_queries.add(query["q"][0])
        elif _POST_PATH.fullmatch(parsed.path) is not None and not parsed.query:
            pass
        else:
            raise BrowserNurtureError("FIXTURE_INVALID")
        pages[url] = html
    if f"{SYNTHETIC_NURTURE_ORIGIN}/" not in pages:
        raise BrowserNurtureError("FIXTURE_INVALID")
    if not set(config.topics).issubset(search_queries):
        raise BrowserNurtureError("FIXTURE_TOPIC_PAGE_MISSING")
    return SyntheticBrowserNurtureFixtureV1(pages=pages)


class BrowserNurtureRunner:
    """Own one persistent Chromium session while running bounded read-only cycles."""

    def __init__(self, runtime: LocalRuntime, state: BrowserNurtureStateStore) -> None:
        self._runtime = runtime
        self._state = state

    async def run(
        self,
        alias: str,
        fixture: SyntheticBrowserNurtureFixtureV1 | None = None,
        *,
        source: BrowserNurtureSource | str | None = None,
        max_cycles: int | None = 1,
        continuous: bool = False,
        idle_seconds: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> BrowserNurtureRunResult:
        if source is None and fixture is None:
            raise BrowserNurtureError("RUN_POLICY_INVALID")
        try:
            selected_source = (
                BrowserNurtureSource(source)
                if source is not None
                else BrowserNurtureSource.UI_FIXTURE
            )
        except TypeError, ValueError:
            raise BrowserNurtureError("RUN_POLICY_INVALID") from None
        if (
            type(continuous) is not bool
            or (not continuous and max_cycles is None)
            or (
                selected_source is BrowserNurtureSource.BROWSER_UI
                and not continuous
                and max_cycles != 1
            )
            or (selected_source is BrowserNurtureSource.UI_FIXTURE) != (fixture is not None)
        ):
            raise BrowserNurtureError("RUN_POLICY_INVALID")
        if max_cycles is not None and (
            type(max_cycles) is not int or not 1 <= max_cycles <= MAX_NURTURE_CYCLES_PER_INVOCATION
        ):
            raise BrowserNurtureError("RUN_POLICY_INVALID")
        try:
            async with self._runtime.own_browser_profile(
                alias,
                headless=selected_source is BrowserNurtureSource.UI_FIXTURE,
            ) as lease:
                config = self._state.load_config(lease.account.id)
                if config is None:
                    raise BrowserNurtureError("CONFIG_NOT_FOUND")
                config = validate_browser_nurture_config(config.to_document())
                if selected_source is BrowserNurtureSource.UI_FIXTURE:
                    if not _ACCOUNT_ALIAS.fullmatch(lease.account.alias):
                        raise BrowserNurtureError("SYNTHETIC_ACCOUNT_REQUIRED")
                    assert fixture is not None
                    if (
                        fixture.schema_version != 1
                        or fixture.origin != SYNTHETIC_NURTURE_ORIGIN
                        or not set(config.topics).issubset(_fixture_search_queries(fixture.pages))
                    ):
                        raise BrowserNurtureError("FIXTURE_TOPIC_PAGE_MISSING")
                    origin = fixture.origin
                else:
                    if _ACCOUNT_ALIAS.fullmatch(lease.account.alias):
                        raise BrowserNurtureError("LIVE_ACCOUNT_REQUIRED")
                    origin = BROWSER_FEED_ORIGIN
                active_idle_seconds = config.idle_seconds if idle_seconds is None else idle_seconds
                if (
                    type(active_idle_seconds) is not int
                    or not 0 <= active_idle_seconds <= 86_400
                    or (continuous and active_idle_seconds == 0 and max_cycles is None)
                    or (
                        selected_source is BrowserNurtureSource.BROWSER_UI
                        and continuous
                        and active_idle_seconds < config.idle_seconds
                    )
                ):
                    raise BrowserNurtureError("RUN_POLICY_INVALID")
                checkpoint = self._state.load_checkpoint(
                    lease.account.id,
                    policy_fingerprint=browser_nurture_policy_fingerprint(config),
                )
                if (
                    continuous
                    and selected_source is BrowserNurtureSource.BROWSER_UI
                    and (
                        checkpoint.completed_cycles < 1
                        or not self._state.continuous_opt_in_enabled(
                            lease.account.id,
                            policy_fingerprint=checkpoint.policy_fingerprint,
                        )
                    )
                ):
                    raise BrowserNurtureError("CONTINUOUS_OPT_IN_REQUIRED")
                run_counts = _RunCounts()
                await lease.start()
                session = _leased_nurture_session(lease)
                await _prepare_session(session, fixture, origin=origin)
                session_open_count = lease.open_count
                attempt_count = 0
                crash_recoveries = 0
                while max_cycles is None or attempt_count < max_cycles:
                    if stop_event is not None and stop_event.is_set():
                        return _run_result(
                            BrowserNurtureStatus.STOPPED,
                            "STOP_REQUESTED",
                            run_counts,
                            checkpoint,
                            session_open_count,
                        )
                    try:
                        cycle = await self._run_cycle(session, config, origin, checkpoint)
                    except BrowserProcessCrashed:
                        if checkpoint.crash_recovery_pending:
                            return _run_result(
                                BrowserNurtureStatus.FAILED,
                                "CRASH_RECOVERY_EXHAUSTED",
                                run_counts,
                                checkpoint,
                                session_open_count,
                            )
                        if crash_recoveries >= 1:
                            checkpoint = self._state.mark_crash_recovery(checkpoint)
                            return _run_result(
                                BrowserNurtureStatus.FAILED,
                                "CRASH_RECOVERY_EXHAUSTED",
                                run_counts,
                                checkpoint,
                                session_open_count,
                            )
                        checkpoint = self._state.mark_crash_recovery(checkpoint)
                        crash_recoveries += 1
                        await lease.restart()
                        session_open_count = lease.open_count
                        session = _leased_nurture_session(lease)
                        await _prepare_session(session, fixture, origin=origin)
                        try:
                            cycle = await self._run_cycle(session, config, origin, checkpoint)
                        except BrowserProcessCrashed:
                            return _run_result(
                                BrowserNurtureStatus.FAILED,
                                "CRASH_RECOVERY_EXHAUSTED",
                                run_counts,
                                checkpoint,
                                session_open_count,
                            )
                    checkpoint = self._commit_cycle(checkpoint, cycle, len(config.topics))
                    run_counts.add(cycle)
                    attempt_count += 1
                    if stop_event is not None and stop_event.is_set():
                        return _run_result(
                            BrowserNurtureStatus.STOPPED,
                            "STOP_REQUESTED",
                            run_counts,
                            checkpoint,
                            session_open_count,
                        )
                    if max_cycles is not None and attempt_count >= max_cycles:
                        break
                    if stop_event is not None:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=active_idle_seconds)
                            return _run_result(
                                BrowserNurtureStatus.STOPPED,
                                "STOP_REQUESTED",
                                run_counts,
                                checkpoint,
                                session_open_count,
                            )
                        except TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(active_idle_seconds)
                return _run_result(
                    BrowserNurtureStatus.COMPLETE,
                    None,
                    run_counts,
                    checkpoint,
                    session_open_count,
                )
        except BrowserNurtureError:
            raise
        except BrowserNurtureStateError as error:
            raise BrowserNurtureError(error.code) from None
        except StandaloneRuntimeError as error:
            raise BrowserNurtureError(error.code) from None
        except BrowserAdapterError as error:
            raise BrowserNurtureError(error.code) from None

    async def manual_assist(
        self,
        alias: str,
        *,
        operator_step: ManualAssistStep,
        decide: ManualAssistDecision,
        progress: ManualAssistProgress,
    ) -> BrowserNurtureAssistResult:
        """Read one manually selected Search result without touching cycle checkpoints."""

        try:
            async with self._runtime.own_browser_profile(alias, headless=False) as lease:
                config = self._state.load_config(lease.account.id)
                if config is None:
                    raise BrowserNurtureError("CONFIG_NOT_FOUND")
                config = validate_browser_nurture_config(config.to_document())
                if _ACCOUNT_ALIAS.fullmatch(lease.account.alias):
                    raise BrowserNurtureError("LIVE_ACCOUNT_REQUIRED")
                origin = BROWSER_FEED_ORIGIN
                await lease.start()
                session = _leased_nurture_session(lease)

                def result(
                    status: BrowserNurtureStatus,
                    reason_code: str | None,
                    stage: str,
                    *,
                    search_candidates: int = 0,
                    scanned: int = 0,
                    classification: str = "UNKNOWN",
                    body_status: str = "NOT_READ",
                ) -> BrowserNurtureAssistResult:
                    return BrowserNurtureAssistResult(
                        status=status,
                        reason_code=reason_code,
                        stage=stage,
                        search_candidates=search_candidates,
                        scanned=scanned,
                        classification=classification,
                        body_status=body_status,
                        sessions_opened=lease.open_count,
                    )

                async def finish(outcome: BrowserNurtureAssistResult) -> BrowserNurtureAssistResult:
                    if outcome.status is not BrowserNurtureStatus.FAILED:
                        await operator_step("CONFIRM_CLOSE")
                    return outcome

                async def verify_home() -> None:
                    await session.navigate(
                        f"{origin}/",
                        allowed_origins=frozenset({origin}),
                    )
                    state = await session.verify_ui_account(config.expected_username, origin=origin)
                    _require_ready_state(state)

                home = await _retry_manual_assist_step("HOME", verify_home, decide)
                if home.action != "READY":
                    status = (
                        BrowserNurtureStatus.FAILED
                        if home.action == "FAILED"
                        else BrowserNurtureStatus.STOPPED
                    )
                    reason = (
                        home.reason_code
                        if status is BrowserNurtureStatus.FAILED
                        else ("OPERATOR_STOP" if home.action == "STOP" else "HOME_SKIPPED")
                    )
                    return await finish(result(status, reason, "HOME"))
                progress("HOME_VERIFIED", None)

                async def read_search_region() -> tuple[UiPostTarget, ...]:
                    return await session.assisted_search_ui_results(
                        config.topics[0],
                        expected_actor=config.expected_username,
                        origin=origin,
                    )

                async def open_search_and_read() -> tuple[UiPostTarget, ...]:
                    await operator_step("OPEN_SEARCH")
                    return await read_search_region()

                search = await _retry_manual_assist_step("SEARCH", open_search_and_read, decide)
                if search.action != "READY" or search.value is None:
                    status = (
                        BrowserNurtureStatus.FAILED
                        if search.action == "FAILED"
                        else BrowserNurtureStatus.STOPPED
                    )
                    reason = (
                        search.reason_code
                        if status is BrowserNurtureStatus.FAILED
                        else ("OPERATOR_STOP" if search.action == "STOP" else "SEARCH_SKIPPED")
                    )
                    return await finish(result(status, reason, "SEARCH"))
                targets = search.value
                progress("SEARCH_RESULTS_VERIFIED", len(targets))
                if not targets:
                    return await finish(
                        result(
                            BrowserNurtureStatus.STOPPED,
                            "SEARCH_NO_RESULTS",
                            "SEARCH",
                        )
                    )

                detail_attempts = 0

                async def read_selected_detail() -> tuple[UiPostTarget, UiPostDetail, str]:
                    nonlocal targets, detail_attempts
                    if detail_attempts > 0:
                        await operator_step("RETURN_TO_SEARCH")
                        targets = await read_search_region()
                    detail_attempts += 1
                    await session.arm_ui_diagnostic_operator_click(source_route="SEARCH")
                    await operator_step("OPEN_DETAIL")
                    target_url = await session.wait_for_assisted_post_click(targets, origin=origin)
                    matching_targets = tuple(
                        target for target in targets if target.canonical_post_url == target_url
                    )
                    if len(matching_targets) != 1:
                        raise BrowserNurtureError("DETAIL_SELECTION_NOT_IN_RESULTS")
                    target = matching_targets[0]
                    detail = await session.read_current_ui_post(
                        target_url,
                        expected_actor=config.expected_username,
                        origin=origin,
                    )
                    _require_ready_state(detail.state)
                    if (
                        detail.canonical_post_url != target.canonical_post_url
                        or detail.author_username != target.author_username
                    ):
                        raise BrowserNurtureError("DETAIL_PROVENANCE_UNVERIFIED")
                    if detail.body is None or detail.body_visibility is UiBodyVisibility.UNKNOWN:
                        raise BrowserNurtureError("DETAIL_BODY_UNVERIFIED")
                    classification = classify_browser_nurture_post(
                        detail,
                        config.relevance_terms,
                    )
                    return target, detail, classification

                selected = await _retry_manual_assist_step(
                    "DETAIL",
                    read_selected_detail,
                    decide,
                )
                if selected.action != "READY" or selected.value is None:
                    status = (
                        BrowserNurtureStatus.FAILED
                        if selected.action == "FAILED"
                        else BrowserNurtureStatus.STOPPED
                    )
                    reason = (
                        selected.reason_code
                        if status is BrowserNurtureStatus.FAILED
                        else ("OPERATOR_STOP" if selected.action == "STOP" else "DETAIL_SKIPPED")
                    )
                    return await finish(
                        result(
                            status,
                            reason,
                            "DETAIL",
                            search_candidates=len(targets),
                        )
                    )

                _target, detail, classification = selected.value
                status = (
                    BrowserNurtureStatus.PARTIAL
                    if classification == "UNKNOWN"
                    else BrowserNurtureStatus.COMPLETE
                )
                reason = (
                    "DETAIL_CLASSIFICATION_UNKNOWN"
                    if status is BrowserNurtureStatus.PARTIAL
                    else None
                )
                progress("DETAIL_PROVENANCE_VERIFIED", 1)
                return await finish(
                    result(
                        status,
                        reason,
                        "COMPLETE" if status is BrowserNurtureStatus.COMPLETE else "PARTIAL",
                        search_candidates=len(targets),
                        scanned=1,
                        classification=classification,
                        body_status=detail.body_visibility.value,
                    )
                )
        except BrowserNurtureError:
            raise
        except BrowserNurtureStateError as error:
            raise BrowserNurtureError(error.code) from None
        except StandaloneRuntimeError as error:
            raise BrowserNurtureError(error.code) from None
        except BrowserAdapterError as error:
            raise BrowserNurtureError(error.code) from None

    async def _run_cycle(
        self,
        session: BrowserNurtureUiSession,
        config: BrowserNurtureConfigV1,
        origin: str,
        checkpoint: BrowserNurtureCheckpointV2,
    ) -> BrowserNurtureCycleMetrics:
        try:
            async with asyncio.timeout(MAX_NURTURE_CYCLE_SECONDS):
                state = await session.verify_ui_account(
                    config.expected_username,
                    origin=origin,
                )
                _require_ready_state(state)
                already_seen = set(checkpoint.seen_fingerprints)
                current_fingerprints: set[str] = set()
                targets: list[tuple[str, UiPostTarget]] = []
                duplicates = 0
                feed_candidates = 0
                scrolls = 0
                topic_queries = 0
                topic_start = checkpoint.next_topic_index % len(config.topics)
                ordered_topics = config.topics[topic_start:] + config.topics[:topic_start]
                for topic in ordered_topics:
                    batches = await session.search_ui_feed(
                        topic,
                        expected_actor=config.expected_username,
                        origin=origin,
                    )
                    topic_queries += 1
                    scrolls += _ui_reader_scroll_count(session)
                    for batch in batches:
                        _require_ready_state(batch.state)
                        for target in batch.cards:
                            feed_candidates += 1
                            fingerprint = _target_fingerprint(target.canonical_post_url)
                            if fingerprint in already_seen or fingerprint in current_fingerprints:
                                duplicates += 1
                                continue
                            if len(targets) >= config.max_posts_per_cycle:
                                continue
                            current_fingerprints.add(fingerprint)
                            targets.append((fingerprint, target))
                            if feed_candidates >= MAX_NURTURE_FEED_CANDIDATES_PER_CYCLE:
                                break
                        if feed_candidates >= MAX_NURTURE_FEED_CANDIDATES_PER_CYCLE:
                            break
                    if feed_candidates >= MAX_NURTURE_FEED_CANDIDATES_PER_CYCLE:
                        break
                relevant = 0
                irrelevant = 0
                unknown = 0
                for fingerprint, target in targets:
                    detail = await session.read_exact_ui_post(
                        target.canonical_post_url,
                        expected_actor=config.expected_username,
                        origin=origin,
                    )
                    _require_ready_state(detail.state)
                    if (
                        detail.canonical_post_url != target.canonical_post_url
                        or detail.author_username != target.author_username
                    ):
                        raise BrowserNurtureError("BROWSER_TARGET_MISMATCH")
                    classification = classify_browser_nurture_post(
                        detail,
                        config.relevance_terms,
                    )
                    if classification == "RELEVANT":
                        relevant += 1
                    elif classification == "IRRELEVANT":
                        irrelevant += 1
                    else:
                        unknown += 1
                return BrowserNurtureCycleMetrics(
                    feed_candidates=feed_candidates,
                    scanned=len(targets),
                    relevant=relevant,
                    irrelevant=irrelevant,
                    unknown=unknown,
                    duplicates=duplicates,
                    scrolls=scrolls,
                    topic_queries=topic_queries,
                    fingerprints=tuple(fingerprint for fingerprint, _ in targets),
                )
        except TimeoutError:
            raise BrowserNurtureError("CYCLE_TIMEOUT") from None

    def _commit_cycle(
        self,
        checkpoint: BrowserNurtureCheckpointV2,
        metrics: BrowserNurtureCycleMetrics,
        topic_count: int,
    ) -> BrowserNurtureCheckpointV2:
        seen = tuple(dict.fromkeys((*checkpoint.seen_fingerprints, *metrics.fingerprints)))[
            -MAX_SEEN_POST_FINGERPRINTS:
        ]
        updated = BrowserNurtureCheckpointV2(
            account_uuid=checkpoint.account_uuid,
            completed_cycles=_add_counter(checkpoint.completed_cycles, 1),
            scanned=_add_counter(checkpoint.scanned, metrics.scanned),
            relevant=_add_counter(checkpoint.relevant, metrics.relevant),
            irrelevant=_add_counter(checkpoint.irrelevant, metrics.irrelevant),
            unknown=_add_counter(checkpoint.unknown, metrics.unknown),
            duplicates=_add_counter(checkpoint.duplicates, metrics.duplicates),
            scrolls=_add_counter(checkpoint.scrolls, metrics.scrolls),
            policy_fingerprint=checkpoint.policy_fingerprint,
            next_topic_index=(checkpoint.next_topic_index + 1) % topic_count,
            seen_fingerprints=seen,
            crash_recovery_pending=False,
        )
        self._state.save_checkpoint(updated)
        return updated


def classify_browser_nurture_post(
    detail: UiPostDetail,
    relevance_terms: tuple[str, ...],
) -> str:
    """Classify full visible text conservatively; partial and image-only posts stay unknown."""

    if (
        detail.body_visibility is not UiBodyVisibility.FULL_VISIBLE
        or detail.body is None
        or not detail.body.strip()
    ):
        return "UNKNOWN"
    normalized = normalize_nurture_text(detail.body)
    if normalized is None:
        return "UNKNOWN"
    return "RELEVANT" if matches_nurture_terms(normalized, relevance_terms) else "IRRELEVANT"


def _require_ready_state(state: UiPageState) -> None:
    if state is UiPageState.READY:
        return
    reason = {
        UiPageState.REDIRECTED: "REMOTE_STATE_UNCERTAIN",
        UiPageState.CHALLENGE: "CHALLENGE_REQUIRED",
        UiPageState.LOGIN_WALL: "SESSION_EXPIRED",
        UiPageState.RATE_LIMITED: "RATE_LIMITED",
        UiPageState.UNKNOWN: "BROWSER_SELECTOR_DRIFT",
    }[state]
    raise BrowserNurtureError(reason)


def _leased_nurture_session(lease: BrowserProfileLease) -> BrowserNurtureUiSession:
    if lease.session is None:
        raise BrowserNurtureError("BROWSER_SESSION_UNAVAILABLE")
    return cast(BrowserNurtureUiSession, lease.session)


async def _prepare_session(
    session: BrowserNurtureUiSession,
    fixture: SyntheticBrowserNurtureFixtureV1 | None,
    *,
    origin: str,
) -> None:
    if fixture is not None:
        await session.install_synthetic_fixture_pages(fixture.pages, origin=fixture.origin)
    await session.navigate(
        f"{origin}/",
        allowed_origins=frozenset({origin}),
    )


def _ui_reader_scroll_count(session: BrowserNurtureUiSession) -> int:
    value = getattr(session, "ui_scroll_count", 0)
    return value if type(value) is int and value >= 0 else 0


def _target_fingerprint(target: str) -> str:
    return hashlib.sha256(b"threads-browser-nurture:v1\x00" + target.encode("utf-8")).hexdigest()


def _add_counter(current: int, added: int) -> int:
    return min(MAX_CHECKPOINT_COUNTER, current + added)


@dataclass(slots=True)
class _RunCounts:
    cycles: int = 0
    feed_candidates: int = 0
    scanned: int = 0
    relevant: int = 0
    irrelevant: int = 0
    unknown: int = 0
    duplicates: int = 0
    scrolls: int = 0

    def add(self, metrics: BrowserNurtureCycleMetrics) -> None:
        self.cycles += 1
        self.feed_candidates += metrics.feed_candidates
        self.scanned += metrics.scanned
        self.relevant += metrics.relevant
        self.irrelevant += metrics.irrelevant
        self.unknown += metrics.unknown
        self.duplicates += metrics.duplicates
        self.scrolls += metrics.scrolls


def _run_result(
    status: BrowserNurtureStatus,
    reason_code: str | None,
    counts: _RunCounts,
    checkpoint: BrowserNurtureCheckpointV2,
    sessions_opened: int,
) -> BrowserNurtureRunResult:
    return BrowserNurtureRunResult(
        status=status,
        reason_code=reason_code,
        cycles=counts.cycles,
        feed_candidates=counts.feed_candidates,
        scanned=counts.scanned,
        relevant=counts.relevant,
        irrelevant=counts.irrelevant,
        unknown=counts.unknown,
        duplicates=counts.duplicates,
        scrolls=counts.scrolls,
        sessions_opened=sessions_opened,
        checkpoint_cycles=checkpoint.completed_cycles,
    )


def _fixture_search_queries(pages: Mapping[str, str]) -> set[str]:
    queries: set[str] = set()
    for url in pages:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme == "https"
                and parsed.netloc == "threads.example.invalid"
                and parsed.path == "/search"
            ):
                values = parse_qs(parsed.query, strict_parsing=True)
                if set(values) == {"q"} and len(values["q"]) == 1:
                    queries.add(values["q"][0])
        except ValueError:
            continue
    return queries


def _contains_sensitive_fixture_content(value: str) -> bool:
    if _EMAIL.search(value) is not None or _PHONE.search(value) is not None:
        return True
    for match in _URL.finditer(value):
        raw_url = match.group(0)
        candidate = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
        try:
            host = urlsplit(candidate).hostname
        except ValueError:
            return True
        if host is None or not host.endswith(".invalid"):
            return True
    return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")
