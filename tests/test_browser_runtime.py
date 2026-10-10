from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

import threads_platform.standalone.runtime as runtime_module
from threads_platform.browser.capabilities import BrowserFeedResultV1
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
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


class _FakeSession:
    def __init__(
        self,
        *,
        feed_batch: tuple[str, ...] = (),
        browser_error: BrowserAdapterError | None = None,
    ) -> None:
        self.feed_batch = feed_batch
        self.browser_error = browser_error
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
        return self.feed_batch

    async def scroll_feed(self) -> None:
        self.scrolls += 1

    async def verify_profile_target(self, *, target_ref: str, ancestor_bound: int) -> None:
        self.profile_checks.append((target_ref, ancestor_bound))
        if self.browser_error is not None:
            raise self.browser_error

    async def verify_thread_target(self, *, target_ref: str, author_username: str) -> None:
        self.thread_checks.append((target_ref, author_username))
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


def test_browser_read_timeout_is_sanitized_and_releases_session_and_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = _add_account(tmp_path)
    account = accounts.get("synthetic_account")
    profile_directory = tmp_path / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)

    class _SlowSession(_FakeSession):
        async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
            await asyncio.sleep(0.05)

    engine = _FakeEngine(_SlowSession)
    runtime = LocalRuntime(tmp_path, accounts, engine)
    monkeypatch.setattr(runtime_module, "_BROWSER_READ_TIMEOUT_SECONDS", 0.005)

    with pytest.raises(StandaloneRuntimeError) as caught:
        asyncio.run(runtime.open_profile("synthetic_account", "synthetic"))

    assert caught.value.code == "BROWSER_NAVIGATION_TIMEOUT"
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
