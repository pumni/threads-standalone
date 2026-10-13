from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

import threads_platform.standalone.runtime as runtime_module
from threads_platform.browser.capabilities import BrowserFeedResultV1, BrowserTargetOpenResultV1
from threads_platform.browser.contracts import (
    BrowserAdapterError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    ChallengeDetected,
    SessionExpired,
)
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.locks.process_lock import ProcessAlreadyRunning
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


class _FakeSession:
    def __init__(
        self,
        *,
        feed_batch: tuple[str, ...] = (),
        feed_batches: tuple[tuple[str, ...], ...] | None = None,
        browser_error: BrowserAdapterError | None = None,
        thread_error: BrowserAdapterError | None = None,
    ) -> None:
        self.feed_batch = feed_batch
        self.feed_batches = feed_batches
        self.feed_collections = 0
        self.browser_error = browser_error
        self.thread_error = thread_error
        self.navigations: list[tuple[str, frozenset[str]]] = []
        self.profile_checks: list[tuple[str, int]] = []
        self.thread_checks: list[tuple[str, str]] = []
        self.closed = False
        self.scrolls = 0

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self.navigations.append((url, allowed_origins))
        if self.browser_error is not None:
            raise self.browser_error

    async def close(self) -> None:
        self.closed = True

    async def collect_feed_permalinks(self, *, candidate_bound: int) -> tuple[str, ...]:
        assert candidate_bound == 100
        if self.browser_error is not None:
            raise self.browser_error
        if self.feed_batches is None:
            batch = self.feed_batch
        else:
            batch = self.feed_batches[min(self.feed_collections, len(self.feed_batches) - 1)]
        self.feed_collections += 1
        return batch

    async def scroll_feed(self) -> None:
        self.scrolls += 1

    async def verify_profile_target(self, *, target_ref: str, ancestor_bound: int) -> None:
        self.profile_checks.append((target_ref, ancestor_bound))
        if self.browser_error is not None:
            raise self.browser_error

    async def verify_thread_target(self, *, target_ref: str, author_username: str) -> None:
        self.thread_checks.append((target_ref, author_username))
        if self.thread_error is not None:
            raise self.thread_error
        if self.browser_error is not None:
            raise self.browser_error


class _FakeEngine:
    def __init__(
        self,
        session_factory: Callable[[], _FakeSession] | None = None,
    ) -> None:
        self.requests: list[BrowserLaunchRequest] = []
        self.sessions: list[_FakeSession] = []
        self._session_factory = session_factory or _FakeSession

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        self.requests.append(request)
        session = self._session_factory()
        self.sessions.append(session)
        return session


class _ControlledTimeout:
    def __init__(self) -> None:
        self._timeout_factory = asyncio.timeout
        self._active_timeout: asyncio.Timeout | None = None

    def __call__(self, delay: float | None) -> asyncio.Timeout:
        timeout = self._timeout_factory(delay)
        self._active_timeout = timeout
        return timeout

    def expire(self) -> None:
        assert self._active_timeout is not None
        self._active_timeout.reschedule(asyncio.get_running_loop().time())


def _add_account(root: Path, alias: str = "synthetic_account") -> LocalAccountStore:
    accounts = LocalAccountStore(root)
    accounts.add(alias)
    return accounts


def test_login_creates_an_isolated_headed_profile_and_waits_for_operator(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)
    prompts: list[str] = []

    asyncio.run(runtime.login("synthetic_account", wait_for_operator=prompts.append))

    request = engine.requests[0]
    assert request.profile_directory == tmp_path / "profiles" / str(account.id)
    assert request.network_route == BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None)
    assert request.headless is False
    assert request.proxy_credentials is None
    assert request.profile_directory.is_dir()
    assert len(prompts) == 1
    assert "Log in to Threads manually" in prompts[0]
    assert engine.sessions[0].closed


def test_profile_thread_and_feed_reads_use_the_same_account_profile(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    engine = _FakeEngine(
        lambda: _FakeSession(
            feed_batch=(
                "https://www.threads.com/@synthetic/post/fake_1",
                "https://www.threads.com/@synthetic/post/fake_1",
                "/@other/post/fake_2",
            )
        )
    )
    runtime = LocalRuntime(tmp_path, accounts, engine)
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)

    profile = asyncio.run(runtime.open_profile("synthetic_account", "synthetic"))
    thread = asyncio.run(runtime.open_thread("synthetic_account", "/@synthetic/post/fake_1"))
    feed = asyncio.run(runtime.browse_feed("synthetic_account", max_items=3))

    assert profile.target_kind == "PROFILE"
    assert profile.target_ref == "/@synthetic"
    assert thread.target_kind == "THREAD"
    assert thread.target_ref == "/@synthetic/post/fake_1"
    assert isinstance(feed, BrowserFeedResultV1)
    assert [item.thread_ref for item in feed.observations] == [
        "https://www.threads.com/@synthetic/post/fake_1",
        "https://www.threads.com/@other/post/fake_2",
    ]
    assert all(
        request.profile_directory == tmp_path / "profiles" / str(account.id)
        for request in engine.requests
    )
    assert all(request.headless is False for request in engine.requests)
    assert engine.sessions[0].profile_checks == [("/@synthetic", 8)]
    assert engine.sessions[1].thread_checks == [("/@synthetic/post/fake_1", "synthetic")]
    assert all(session.closed for session in engine.sessions)


def test_browse_feed_waits_through_a_duplicate_batch_for_a_later_post(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    first_post = "/@synthetic/post/fake_1"
    second_post = "/@synthetic/post/fake_2"
    engine = _FakeEngine(
        lambda: _FakeSession(feed_batches=((first_post,), (first_post,), (first_post, second_post)))
    )
    runtime = LocalRuntime(tmp_path, accounts, engine)
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)

    result = asyncio.run(runtime.browse_feed("synthetic_account", max_items=2))

    assert [item.thread_ref for item in result.observations] == [
        "https://www.threads.com/@synthetic/post/fake_1",
        "https://www.threads.com/@synthetic/post/fake_2",
    ]
    assert result.truncated
    assert engine.sessions[0].feed_collections == 3
    assert engine.sessions[0].scrolls == 2
    assert engine.sessions[0].closed


def test_browse_feed_duplicate_batches_stop_at_the_iteration_bound(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    engine = _FakeEngine(lambda: _FakeSession(feed_batch=("/@synthetic/post/fake_1",)))
    runtime = LocalRuntime(tmp_path, accounts, engine)
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)

    result = asyncio.run(runtime.browse_feed("synthetic_account", max_items=3))

    assert len(result.observations) == 1
    assert result.truncated
    assert engine.sessions[0].feed_collections == runtime_module.BROWSER_FEED_ITERATION_BOUND
    assert engine.sessions[0].scrolls == runtime_module.BROWSER_FEED_ITERATION_BOUND - 1
    assert engine.sessions[0].closed


def test_interactive_thread_holds_account_lock_until_operator_ends_session(
    tmp_path: Path,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    profile_directory = tmp_path / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)
    prompts: list[str] = []
    lock_path = tmp_path / "locks" / f"{account.id}.lock"

    async def end_session(prompt: str) -> None:
        session = engine.sessions[0]
        assert not session.closed
        assert "target=/@synthetic/post/fake_1" in prompt
        assert "author=@synthetic" in prompt
        assert "identity=UNKNOWN_IDENTITY" in prompt
        probe = FilesystemProcessLock(lock_path)
        with pytest.raises(ProcessAlreadyRunning):
            probe.acquire()
        prompts.append(prompt)

    result = asyncio.run(
        runtime.open_thread_interactive(
            "synthetic_account",
            "/@synthetic/post/fake_1",
            wait_for_operator=end_session,
        )
    )

    assert result.target_kind == "THREAD"
    assert result.target_ref == "/@synthetic/post/fake_1"
    assert result.recognized
    assert len(prompts) == 1
    assert engine.requests[0].profile_directory == profile_directory
    assert engine.requests[0].headless is False
    assert engine.requests[0].proxy_credentials is None
    assert engine.sessions[0].navigations == [
        (
            "https://www.threads.com/@synthetic/post/fake_1",
            frozenset({"https://www.threads.com"}),
        )
    ]
    assert engine.sessions[0].thread_checks == [
        ("/@synthetic/post/fake_1", "synthetic"),
        ("/@synthetic/post/fake_1", "synthetic"),
    ]
    assert engine.sessions[0].closed

    probe = FilesystemProcessLock(lock_path)
    probe.acquire()
    probe.release()


def test_interactive_thread_keeps_browser_loop_responsive_before_operator_enters(
    tmp_path: Path,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)
    operator_wait_started = asyncio.Event()
    keys: list[str] = []
    enter_pressed = asyncio.Event()
    browser_callback_progressed = asyncio.Event()

    def read_key() -> str:
        key = keys.pop(0)
        if key in {"\r", "\n"}:
            enter_pressed.set()
        return key

    async def wait_for_enter(prompt: str) -> None:
        assert "identity=UNKNOWN_IDENTITY" in prompt
        operator_wait_started.set()
        await runtime_module._wait_for_windows_console_enter(lambda: bool(keys), read_key)

    async def simulated_playwright_callback() -> None:
        await operator_wait_started.wait()
        await asyncio.sleep(0)
        await engine.sessions[0].verify_thread_target(
            target_ref="/@synthetic/post/fake_1",
            author_username="synthetic",
        )
        assert not enter_pressed.is_set()
        assert not engine.sessions[0].closed
        browser_callback_progressed.set()

    async def exercise_handoff() -> BrowserTargetOpenResultV1:
        handoff = asyncio.create_task(
            runtime.open_thread_interactive(
                "synthetic_account",
                "/@synthetic/post/fake_1",
                wait_for_operator=wait_for_enter,
            )
        )
        browser_work = asyncio.create_task(simulated_playwright_callback())
        try:
            await asyncio.wait_for(browser_callback_progressed.wait(), timeout=1)
            assert not enter_pressed.is_set()
        finally:
            keys.append("\r")
        result = await handoff
        await browser_work
        return result

    result = asyncio.run(exercise_handoff())

    assert result.recognized
    assert browser_callback_progressed.is_set()
    assert enter_pressed.is_set()
    assert engine.sessions[0].closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


def test_interactive_thread_keeps_synthetic_alias_profiles_and_sessions_isolated(
    tmp_path: Path,
) -> None:
    accounts = _add_account(tmp_path, "first_account")
    second_account = accounts.add("second_account")
    first = accounts.get("first_account")
    first_profile = tmp_path / "profiles" / str(first.id)
    second_profile = tmp_path / "profiles" / str(second_account.id)
    first_profile.mkdir(parents=True)
    second_profile.mkdir(parents=True)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    async def end_session(_: str) -> None:
        return None

    asyncio.run(
        runtime.open_thread_interactive(
            "first_account",
            "/@synthetic/post/fake_1",
            wait_for_operator=end_session,
        )
    )
    asyncio.run(
        runtime.open_thread_interactive(
            "second_account",
            "/@synthetic/post/fake_1",
            wait_for_operator=end_session,
        )
    )

    assert [request.profile_directory for request in engine.requests] == [
        first_profile,
        second_profile,
    ]
    assert engine.sessions[0] is not engine.sessions[1]
    assert all(session.closed for session in engine.sessions)


@pytest.mark.parametrize(
    ("session", "expected_code"),
    [
        (_FakeSession(browser_error=ChallengeDetected()), "CHALLENGE_REQUIRED"),
        (_FakeSession(browser_error=SessionExpired()), "SESSION_EXPIRED"),
        (
            _FakeSession(thread_error=BrowserAdapterError("BROWSER_CONTRACT_MISMATCH")),
            "BROWSER_CONTRACT_MISMATCH",
        ),
    ],
)
def test_interactive_thread_fails_closed_before_handoff_on_unverified_target(
    tmp_path: Path,
    session: _FakeSession,
    expected_code: str,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine(lambda: session)
    runtime = LocalRuntime(tmp_path, accounts, engine)
    prompts: list[str] = []

    async def record_prompt(prompt: str) -> None:
        prompts.append(prompt)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(
            runtime.open_thread_interactive(
                "synthetic_account",
                "/@synthetic/post/fake_1",
                wait_for_operator=record_prompt,
            )
        )

    assert caught.value.code == expected_code
    assert prompts == []
    assert session.closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


def test_interactive_thread_timeout_closes_session_and_releases_account_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)

    class _SlowSession(_FakeSession):
        async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
            await asyncio.sleep(0.05)

    engine = _FakeEngine(_SlowSession)
    runtime = LocalRuntime(tmp_path, accounts, engine)
    prompts: list[str] = []

    async def record_prompt(prompt: str) -> None:
        prompts.append(prompt)

    monkeypatch.setattr(runtime_module, "_BROWSER_READ_TIMEOUT_SECONDS", 0.005)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(
            runtime.open_thread_interactive(
                "synthetic_account",
                "/@synthetic/post/fake_1",
                wait_for_operator=record_prompt,
            )
        )

    assert caught.value.code == "BROWSER_NAVIGATION_TIMEOUT"
    assert prompts == []
    assert engine.sessions[0].closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


def test_interactive_thread_cancellation_closes_session_and_releases_account_lock(
    tmp_path: Path,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    async def cancel_session(_: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            runtime.open_thread_interactive(
                "synthetic_account",
                "/@synthetic/post/fake_1",
                wait_for_operator=cancel_session,
            )
        )

    assert engine.sessions[0].closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


def test_interactive_thread_eof_closes_session_and_releases_account_lock(
    tmp_path: Path,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    async def eof_session(_: str) -> None:
        raise EOFError

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(
            runtime.open_thread_interactive(
                "synthetic_account",
                "/@synthetic/post/fake_1",
                wait_for_operator=eof_session,
            )
        )

    assert caught.value.code == "OPERATOR_HANDOFF_ABORTED"
    assert engine.sessions[0].closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


@pytest.mark.parametrize(
    ("key", "expected_exception"),
    [("\x1a", EOFError), ("\x03", KeyboardInterrupt)],
)
def test_windows_console_wait_preserves_eof_and_ctrl_c(
    key: str,
    expected_exception: type[BaseException],
) -> None:
    with pytest.raises(expected_exception):
        asyncio.run(
            runtime_module._wait_for_windows_console_enter(
                lambda: True,
                lambda: key,
            )
        )


def test_interactive_thread_reports_browser_crash_after_operator_handoff(
    tmp_path: Path,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    async def crash_session(_: str) -> None:
        engine.sessions[0].browser_error = BrowserAdapterError("BROWSER_PROCESS_CRASHED")

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(
            runtime.open_thread_interactive(
                "synthetic_account",
                "/@synthetic/post/fake_1",
                wait_for_operator=crash_session,
            )
        )

    assert caught.value.code == "BROWSER_PROCESS_CRASHED"
    assert engine.sessions[0].closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [(ChallengeDetected(), "CHALLENGE_REQUIRED"), (SessionExpired(), "SESSION_EXPIRED")],
)
def test_challenge_and_expired_session_are_surfaced_without_retry(
    tmp_path: Path,
    error: BrowserAdapterError,
    expected_code: str,
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    profile_directory = tmp_path / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)
    engine = _FakeEngine(lambda: _FakeSession(browser_error=error))
    runtime = LocalRuntime(tmp_path, accounts, engine)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(runtime.open_profile("synthetic_account", "synthetic"))

    assert caught.value.code == expected_code
    assert len(engine.requests) == 1
    assert engine.sessions[0].closed


def test_profile_lock_collision_is_account_scoped_and_fails_closed(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    profile_directory = tmp_path / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)
    lock = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    lock.acquire()
    runtime = LocalRuntime(tmp_path, accounts, _FakeEngine())

    try:
        with pytest.raises(StandaloneRuntimeError) as caught:
            asyncio.run(runtime.open_profile("synthetic_account", "synthetic"))
        assert caught.value.code == "ACCOUNT_BUSY"
    finally:
        lock.release()


def test_profile_and_lock_paths_do_not_cross_accounts(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path, "first_account")
    second = accounts.add("second_account")
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    asyncio.run(runtime.login("first_account", wait_for_operator=lambda _: None))
    asyncio.run(runtime.login("second_account", wait_for_operator=lambda _: None))

    first_request, second_request = engine.requests
    assert first_request.profile_directory != second_request.profile_directory
    assert str(first_request.profile_directory).endswith(str(accounts.get("first_account").id))
    assert str(second_request.profile_directory).endswith(str(second.id))
    assert first_request.profile_directory.parent == second_request.profile_directory.parent

    first_lock = FilesystemProcessLock(
        tmp_path / "locks" / f"{accounts.get('first_account').id}.lock"
    )
    second_lock = FilesystemProcessLock(tmp_path / "locks" / f"{second.id}.lock")
    try:
        first_lock.acquire()
        second_lock.acquire()
        assert first_lock.held and second_lock.held
    finally:
        second_lock.release()
        first_lock.release()


def test_browser_read_timeout_before_session_is_sanitized_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    profile_directory = tmp_path / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)

    timeout = _ControlledTimeout()
    monkeypatch.setattr(runtime_module.asyncio, "timeout", timeout)

    class _PausedOpenEngine(_FakeEngine):
        async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
            await asyncio.Event().wait()
            self.requests.append(request)
            return await super().open(request)

    engine = _PausedOpenEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)
    resolve_profile_directory = runtime._profile_directory

    def expire_before_open(local_account: LocalAccount) -> Path:
        directory = resolve_profile_directory(local_account)
        timeout.expire()
        return directory

    monkeypatch.setattr(runtime, "_profile_directory", expire_before_open)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(runtime.open_profile("synthetic_account", "synthetic"))

    assert caught.value.code == "BROWSER_NAVIGATION_TIMEOUT"
    assert engine.requests == []
    assert engine.sessions == []
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


def test_browser_read_timeout_during_navigation_closes_session_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    profile_directory = tmp_path / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)
    timeout = _ControlledTimeout()
    monkeypatch.setattr(runtime_module.asyncio, "timeout", timeout)

    class _PausedNavigationSession(_FakeSession):
        async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
            self.navigations.append((url, allowed_origins))
            timeout.expire()
            await asyncio.Event().wait()

    engine = _FakeEngine(_PausedNavigationSession)
    runtime = LocalRuntime(tmp_path, accounts, engine)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(runtime.open_profile("synthetic_account", "synthetic"))

    assert caught.value.code == "BROWSER_NAVIGATION_TIMEOUT"
    assert len(engine.requests) == 1
    assert len(engine.sessions) == 1
    assert engine.sessions[0].navigations
    assert engine.sessions[0].closed
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    probe.acquire()
    probe.release()


def test_invalid_browser_targets_do_not_open_profile(tmp_path: Path) -> None:
    accounts = _add_account(tmp_path)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    with pytest.raises(StandaloneRuntimeError) as profile_error:
        asyncio.run(runtime.open_profile("synthetic_account", "https://evil.invalid/"))
    with pytest.raises(StandaloneRuntimeError) as thread_error:
        asyncio.run(runtime.open_thread("synthetic_account", "//evil.invalid/@x/post/fake"))

    assert profile_error.value.code == "INVALID_PROFILE_TARGET"
    assert thread_error.value.code == "INVALID_THREAD_TARGET"
    assert engine.requests == []
    assert not (tmp_path / "profiles").exists()


@pytest.mark.parametrize("limit", [0, 21])
def test_feed_limit_is_rejected_before_profile_access(tmp_path: Path, limit: int) -> None:
    accounts = _add_account(tmp_path)
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(runtime.browse_feed("synthetic_account", max_items=limit))

    assert caught.value.code == "INVALID_FEED_LIMIT"
    assert engine.requests == []


def test_browser_cli_runs_only_requested_synthetic_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    engine = _FakeEngine(
        lambda: _FakeSession(
            feed_batch=(
                "https://www.threads.com/@synthetic/post/fake_1",
                "https://www.threads.com/@synthetic/post/fake_2",
            )
        )
    )
    prompts: list[str] = []
    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", lambda: engine)
    original_login = runtime_module.LocalRuntime.login

    async def fake_login(runtime: LocalRuntime, alias: str) -> None:
        await original_login(runtime, alias, wait_for_operator=prompts.append)

    monkeypatch.setattr(runtime_module.LocalRuntime, "login", fake_login)

    assert cli.main(["account", "add", "synthetic_account"]) == 0
    assert cli.main(["account", "login", "synthetic_account"]) == 0
    assert cli.main(["profile", "synthetic_account", "synthetic"]) == 0
    assert cli.main(["thread", "synthetic_account", "/@synthetic/post/fake_1"]) == 0
    assert cli.main(["feed", "synthetic_account", "--limit", "2"]) == 0

    output = capsys.readouterr().out
    assert "login browser closed synthetic_account" in output
    assert "profile recognized target=/@synthetic\n" in output
    assert "thread recognized target=/@synthetic/post/fake_1\n" in output
    assert "feed count=2 truncated=true" in output
    assert "text=-" in output
    assert len(prompts) == 1
    assert "Log in to Threads manually" in prompts[0]
    assert len(engine.requests) == 4


def test_browser_cli_interactive_thread_is_explicit_and_reports_unknown_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threads_platform.standalone.__main__ as cli

    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path))
    engine = _FakeEngine()
    prompts: list[str] = []
    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", lambda: engine)
    original_open_thread_interactive = LocalRuntime.open_thread_interactive

    async def open_thread_interactive(
        runtime: LocalRuntime,
        alias: str,
        thread_ref: str,
    ) -> object:
        async def record_prompt(prompt: str) -> None:
            prompts.append(prompt)

        return await original_open_thread_interactive(
            runtime,
            alias,
            thread_ref,
            wait_for_operator=record_prompt,
        )

    monkeypatch.setattr(LocalRuntime, "open_thread_interactive", open_thread_interactive)

    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["browser", "open-thread", "synthetic_account", "/@synthetic/post/fake_1"]
        )

    assert cli.main(["account", "add", "synthetic_account"]) == 0
    account = LocalAccountStore(tmp_path).get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)

    assert (
        cli.main(
            [
                "browser",
                "open-thread",
                "synthetic_account",
                "/@synthetic/post/fake_1",
                "--interactive",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "browser session closed target=/@synthetic/post/fake_1" in output
    assert "identity=UNKNOWN_IDENTITY" in output
    assert len(prompts) == 1
    assert "target recognized target=/@synthetic/post/fake_1" in prompts[0]
    assert "identity=UNKNOWN_IDENTITY" in prompts[0]
    assert engine.sessions[0].closed
    assert len(engine.requests) == 1


def test_wheel_smoke_browser_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine(
        lambda: _FakeSession(feed_batch=("https://www.threads.com/@synthetic/post/fake_1",))
    )
    monkeypatch.setattr(runtime_module, "PlaywrightBrowserEngine", lambda: engine)
    runtime = LocalRuntime(tmp_path, accounts)

    result = asyncio.run(runtime.browse_feed("synthetic_account", max_items=1))

    assert result.result_version == 1
    assert len(result.observations) == 1
    assert result.observations[0].thread_ref == "https://www.threads.com/@synthetic/post/fake_1"
