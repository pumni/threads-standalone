from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import threads_platform.browser.playwright_engine as engine_module
from threads_platform.browser.contracts import (
    BROWSER_FEED_CANDIDATE_BOUND,
    BrowserAdapterError,
    BrowserContractError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserNetworkRouteUnsupported,
    BrowserProxyCredentials,
    NavigationTimeout,
    RemoteSessionStateUncertain,
    UnsupportedUIState,
)
from threads_platform.locks.filesystem import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.runtime import LocalRuntime


class _FakeRequest:
    def __init__(self, url: str, frame: object) -> None:
        self.url = url
        self.frame = frame

    def is_navigation_request(self) -> bool:
        return True


class _FakeRoute:
    def __init__(self, request: _FakeRequest) -> None:
        self.request = request
        self.aborted: str | None = None
        self.continued = False

    async def abort(self, error: str) -> None:
        self.aborted = error

    async def continue_(self) -> None:
        self.continued = True


class _FakeBrowser:
    def is_connected(self) -> bool:
        return True


class _FakePage:
    def __init__(self, payload: object = None) -> None:
        self.url = "about:blank"
        self.main_frame = object()
        self.payload = payload
        self.route_handler: Any = None
        self.redirect_to: str | None = None
        self.timeout_on_goto = False
        self.goto_calls: list[tuple[str, str, int]] = []
        self.events: dict[str, Any] = {}
        self.feed_payloads: list[object] = []
        self.feed_anchor_hrefs: tuple[str, ...] | None = None
        self.feed_scan_calls = 0
        self.scroll_dispatches = 0

    def on(self, event: str, handler: Any) -> None:
        self.events[event] = handler

    async def route(self, pattern: str, handler: Any) -> None:
        assert pattern == "**/*"
        self.route_handler = handler

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.timeout_on_goto:
            raise PlaywrightTimeoutError("synthetic timeout")
        self.url = url
        if self.redirect_to is not None:
            self.url = self.redirect_to
            route = _FakeRoute(_FakeRequest(self.redirect_to, self.main_frame))
            assert self.route_handler is not None
            await self.route_handler(route)
            assert route.aborted == "blockedbyclient"

    async def evaluate(self, script: str, arguments: object = None) -> object:
        if "candidateBound" in script:
            self.feed_scan_calls += 1
            assert isinstance(arguments, dict)
            assert arguments.get("allowedOrigin") == "https://www.threads.com"
            candidate_bound = arguments.get("candidateBound")
            assert type(candidate_bound) is int
            assert 1 <= candidate_bound <= BROWSER_FEED_CANDIDATE_BOUND
            if self.feed_payloads:
                self.payload = self.feed_payloads.pop(0)
            if self.feed_anchor_hrefs is not None:
                return _fake_feed_scan(self.url, self.feed_anchor_hrefs, arguments)
            return self.payload
        if "targetAuthor" in script or "ancestorBound" in script:
            return self.payload
        if "window.scrollBy" in script:
            self.scroll_dispatches += 1
        return None

    def is_closed(self) -> bool:
        return False


class _FakeContext:
    def __init__(self, page: _FakePage, close_error: Exception | None = None) -> None:
        self.pages = [page]
        self.browser = _FakeBrowser()
        self.closed = False
        self.close_error = close_error
        self.close_calls = 0

    def on(self, event: str, handler: Any) -> None:
        self.close_handler = handler

    async def new_page(self) -> _FakePage:
        return self.pages[0]

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _FakeChromium:
    def __init__(self, executable: Path, context: _FakeContext) -> None:
        self.executable_path = str(executable)
        self.context = context
        self.launch_options: dict[str, object] | None = None

    async def launch_persistent_context(self, **kwargs: object) -> _FakeContext:
        self.launch_options = kwargs
        return self.context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium, stop_error: Exception | None = None) -> None:
        self.chromium = chromium
        self.stopped = False
        self.stop_error = stop_error
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped = True


class _FakeManager:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self.playwright


def _request(profile: Path) -> BrowserLaunchRequest:
    return BrowserLaunchRequest(
        profile_directory=profile,
        network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
    )


def _fake_feed_scan(url: str, hrefs: tuple[str, ...], arguments: object) -> object:
    assert isinstance(arguments, dict)
    allowed_origin = arguments.get("allowedOrigin")
    candidate_bound = arguments.get("candidateBound")
    assert isinstance(allowed_origin, str)
    assert type(candidate_bound) is int
    current_url = urlsplit(url)
    current_origin = f"{current_url.scheme}://{current_url.netloc}"
    if current_origin != allowed_origin:
        return {"ok": False, "reason": "origin_mismatch"}

    permalinks: list[str] = []
    for href in hrefs:
        candidate = urlsplit(urljoin(url, href))
        candidate_origin = f"{candidate.scheme}://{candidate.netloc}"
        if candidate_origin != allowed_origin:
            continue
        if re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/post/[A-Za-z0-9_-]{1,120}/?", candidate.path):
            permalinks.append(candidate.path)
            if len(permalinks) > candidate_bound:
                return {"ok": False, "reason": "candidate_bound_exceeded"}
    return {"ok": True, "permalinks": permalinks}


def _install_fake_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: object = None,
    context_close_error: Exception | None = None,
    playwright_stop_error: Exception | None = None,
) -> tuple[_FakePage, _FakeContext, _FakePlaywright, _FakeChromium]:
    executable = tmp_path / "synthetic-chromium"
    executable.write_text("synthetic executable marker", encoding="utf-8")
    page = _FakePage(payload)
    context = _FakeContext(page, context_close_error)
    chromium = _FakeChromium(executable, context)
    playwright = _FakePlaywright(chromium, playwright_stop_error)
    monkeypatch.setattr(engine_module, "async_playwright", lambda: _FakeManager(playwright))
    return page, context, playwright, chromium


class _CapturingEngine(engine_module.PlaywrightBrowserEngine):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[BrowserEngineSession] = []

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        session = await super().open(request)
        self.sessions.append(session)
        return session


def test_persistent_context_opens_headed_profile_and_reads_synthetic_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, context, playwright, chromium = _install_fake_playwright(
        tmp_path,
        monkeypatch,
        payload={"ok": True, "permalinks": ["/@synthetic/post/fake_1"]},
    )
    profile = tmp_path / "profiles" / "synthetic-account"
    profile.mkdir(parents=True)

    async def exercise() -> tuple[str, ...]:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        assert chromium.launch_options is not None
        assert chromium.launch_options["user_data_dir"] == str(profile.resolve())
        assert chromium.launch_options["headless"] is False
        assert chromium.launch_options["proxy"] is None
        assert chromium.launch_options["accept_downloads"] is False
        assert chromium.launch_options["service_workers"] == "block"
        await session.navigate(
            "https://www.threads.com/",
            allowed_origins=frozenset({"https://www.threads.com"}),
        )
        result = await session.collect_feed_permalinks(candidate_bound=25)
        await session.close()
        return result

    result = asyncio.run(exercise())
    assert result == ("/@synthetic/post/fake_1",)
    assert page.goto_calls == [("https://www.threads.com/", "domcontentloaded", 30_000)]
    assert context.closed
    assert playwright.stopped


def test_scroll_feed_waits_for_a_new_synthetic_tranche(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_payload = {"ok": True, "permalinks": ["/@synthetic/post/fake_1"]}
    new_payload = {
        "ok": True,
        "permalinks": ["/@synthetic/post/fake_1", "/@synthetic/post/fake_2"],
    }
    page, _, _, _ = _install_fake_playwright(tmp_path, monkeypatch, payload=old_payload)
    profile = tmp_path / "profile"
    profile.mkdir()

    async def exercise() -> tuple[str, ...]:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        try:
            await session.navigate(
                "https://www.threads.com/",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
            assert await session.collect_feed_permalinks(candidate_bound=25) == (
                "/@synthetic/post/fake_1",
            )
            page.feed_payloads = [old_payload, new_payload]
            await session.scroll_feed()
            return await session.collect_feed_permalinks(candidate_bound=25)
        finally:
            await session.close()

    assert asyncio.run(exercise()) == (
        "/@synthetic/post/fake_1",
        "/@synthetic/post/fake_2",
    )
    assert page.scroll_dispatches == 1
    assert page.feed_scan_calls == 4


def test_feed_scan_uses_post_candidates_and_fails_on_candidate_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _, _, _ = _install_fake_playwright(tmp_path, monkeypatch)
    profile = tmp_path / "profile"
    profile.mkdir()
    non_post_hrefs = tuple(f"/@synthetic/profile/fake_{index}" for index in range(1001))
    post_hrefs = tuple(f"/@synthetic/post/fake_{index}" for index in range(25))
    page.feed_anchor_hrefs = non_post_hrefs + post_hrefs

    async def exercise() -> None:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        try:
            await session.navigate(
                "https://www.threads.com/",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
            result = await session.collect_feed_permalinks(candidate_bound=25)
            assert result == post_hrefs

            page.feed_anchor_hrefs = non_post_hrefs + post_hrefs + ("/@synthetic/post/fake_25",)
            with pytest.raises(BrowserAdapterError) as caught:
                await session.collect_feed_permalinks(candidate_bound=25)
            assert caught.value.code == "BROWSER_FEED_CANDIDATE_BOUND_EXCEEDED"
        finally:
            await session.close()

    asyncio.run(exercise())
    assert "document.querySelectorAll('a[href*=\"/post/\"]')" in engine_module._FEED_SCAN_SCRIPT
    assert "anchors.length > 1000" not in engine_module._FEED_SCAN_SCRIPT
    assert "permalinks.length > candidateBound" in engine_module._FEED_SCAN_SCRIPT


def test_feed_scan_rejects_a_changed_off_origin_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _, _, _ = _install_fake_playwright(
        tmp_path,
        monkeypatch,
        payload={"ok": True, "permalinks": ["/@synthetic/post/fake_1"]},
    )
    profile = tmp_path / "profile"
    profile.mkdir()

    async def exercise() -> None:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        try:
            await session.navigate(
                "https://www.threads.com/",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
            page.url = "https://evil.invalid/"
            with pytest.raises(RemoteSessionStateUncertain):
                await session.collect_feed_permalinks(candidate_bound=25)
            assert page.feed_scan_calls == 0
        finally:
            await session.close()

    asyncio.run(exercise())


def test_cleanup_failures_are_suppressed_repeated_close_is_safe_and_login_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, context, playwright, _ = _install_fake_playwright(
        tmp_path,
        monkeypatch,
        context_close_error=RuntimeError("synthetic private context detail"),
        playwright_stop_error=RuntimeError("synthetic private Playwright detail"),
    )
    accounts = LocalAccountStore(tmp_path)
    account = accounts.add("synthetic_account")
    engine = _CapturingEngine()
    runtime = LocalRuntime(tmp_path, accounts, engine)

    asyncio.run(runtime.login("synthetic_account", wait_for_operator=lambda _: None))

    assert context.close_calls == 1
    assert playwright.stop_calls == 1
    output = capsys.readouterr()
    assert "synthetic private context detail" not in output.out + output.err
    assert "synthetic private Playwright detail" not in output.out + output.err

    lock = FilesystemProcessLock(tmp_path / "locks" / f"{account.id}.lock")
    lock.acquire()
    lock.release()

    asyncio.run(engine.sessions[0].close())
    assert context.close_calls == 1
    assert playwright.stop_calls == 1


def test_close_propagates_cancellation_after_attempting_playwright_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, context, playwright, _ = _install_fake_playwright(
        tmp_path,
        monkeypatch,
        context_close_error=asyncio.CancelledError("synthetic cancellation"),
    )
    profile = tmp_path / "profile"
    profile.mkdir()

    async def exercise() -> None:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        with pytest.raises(asyncio.CancelledError, match="synthetic cancellation"):
            await session.close()

    asyncio.run(exercise())
    assert context.close_calls == 1
    assert playwright.stop_calls == 1
    assert playwright.stopped


@pytest.mark.parametrize(
    ("script_result", "target_kind"),
    [
        ({"outcome": "recognized"}, "profile"),
        ({"outcome": "recognized"}, "thread"),
    ],
)
def test_profile_and_thread_semantics_accept_only_recognized_synthetic_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script_result: object,
    target_kind: str,
) -> None:
    _install_fake_playwright(tmp_path, monkeypatch, payload=script_result)
    profile = tmp_path / "profile"
    profile.mkdir()

    async def exercise() -> None:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        target_url = (
            "https://www.threads.com/@synthetic"
            if target_kind == "profile"
            else "https://www.threads.com/@synthetic/post/fake_1"
        )
        await session.navigate(
            target_url,
            allowed_origins=frozenset({"https://www.threads.com"}),
        )
        if target_kind == "profile":
            await session.verify_profile_target(target_ref="/@synthetic", ancestor_bound=8)
        else:
            await session.verify_thread_target(
                target_ref="/@synthetic/post/fake_1",
                author_username="synthetic",
            )
        await session.close()

    asyncio.run(exercise())


def test_off_origin_redirect_and_invalid_navigation_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _, _, _ = _install_fake_playwright(tmp_path, monkeypatch)
    profile = tmp_path / "profile"
    profile.mkdir()

    async def exercise() -> None:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        await session.navigate(
            "https://www.threads.com/",
            allowed_origins=frozenset({"https://www.threads.com"}),
        )
        page.redirect_to = "https://evil.invalid/redirect-target"
        with pytest.raises(RemoteSessionStateUncertain):
            await session.navigate(
                "https://www.threads.com/@synthetic",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
        page.redirect_to = None
        before = len(page.goto_calls)
        with pytest.raises(UnsupportedUIState):
            await session.navigate(
                "https://evil.invalid/",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
        assert len(page.goto_calls) == before
        with pytest.raises(UnsupportedUIState):
            await session.navigate(
                "https://user:secret@www.threads.com/",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
        await session.close()

    asyncio.run(exercise())


def test_navigation_timeout_and_malformed_browser_payload_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page, _, _, _ = _install_fake_playwright(
        tmp_path,
        monkeypatch,
        payload={"ok": True, "permalinks": "synthetic-invalid"},
    )
    profile = tmp_path / "profile"
    profile.mkdir()

    async def exercise() -> None:
        session = await engine_module.PlaywrightBrowserEngine().open(_request(profile))
        page.timeout_on_goto = True
        with pytest.raises(NavigationTimeout):
            await session.navigate(
                "https://www.threads.com/",
                allowed_origins=frozenset({"https://www.threads.com"}),
            )
        page.timeout_on_goto = False
        await session.navigate(
            "https://www.threads.com/",
            allowed_origins=frozenset({"https://www.threads.com"}),
        )
        with pytest.raises(BrowserContractError):
            await session.collect_feed_permalinks(candidate_bound=25)
        await session.close()

    asyncio.run(exercise())


def test_proxy_routes_are_validated_without_exposing_credentials() -> None:
    direct_with_credentials = BrowserLaunchRequest(
        profile_directory=Path("synthetic-profile"),
        network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
        proxy_credentials=BrowserProxyCredentials(password="SYNTHETIC_SECRET"),
    )
    socks_with_credentials = BrowserLaunchRequest(
        profile_directory=Path("synthetic-profile"),
        network_route=BrowserNetworkRoute(BrowserNetworkProtocol.SOCKS5, "127.0.0.1", 1080),
        proxy_credentials=BrowserProxyCredentials(password="SYNTHETIC_SECRET"),
    )

    with pytest.raises(BrowserNetworkRouteUnsupported):
        engine_module.playwright_proxy_settings(direct_with_credentials)
    with pytest.raises(BrowserNetworkRouteUnsupported):
        engine_module.playwright_proxy_settings(socks_with_credentials)
